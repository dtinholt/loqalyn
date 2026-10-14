"""Explicit, bounded Hugging Face discovery. Never imports repository code.

Only public single-file GGUF text models are offered. Ollama owns downloads,
content-addressed verification and storage; no credentials are sent to the Hub.
"""
from __future__ import annotations
import asyncio
import re
import secrets
import time
from urllib.parse import quote
import aiohttp
from .core import Fault, GIB
from .security import read_response

HUB = 'https://huggingface.co'
MAX_WEIGHT_BYTES = 128 * GIB


def repository(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value) or '..' in value:
        raise Fault('Use a Hugging Face repository in publisher/model form.')
    return value


def gguf_file(value):
    return (isinstance(value, str) and len(value) <= 150
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.gguf', value, re.I)
            and '..' not in value and 'mmproj' not in value.lower()
            and not re.search(r'-\d{5}-of-\d{5}', value))


def label(value, limit=160):
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit]


def license_name(item):
    card = item.get('cardData') or {}
    value = card.get('license') if isinstance(card, dict) else None
    if not value:
        value = next((t[8:] for t in item.get('tags', []) if isinstance(t, str) and t.startswith('license:')), None)
    return label(value or 'Not specified; review the model card')


class HubCatalogue:
    def __init__(self, session):
        self.session = session
        self.slots = asyncio.Semaphore(4)
        self.previews = {}

    async def _get(self, path, params=None):
        try:
            async with asyncio.timeout(20):
                async with self.slots:
                    async with self.session.get(HUB + path, params=params, allow_redirects=False,
                            headers={'Accept': 'application/json'}, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        if response.status == 429: raise Fault('Hugging Face is rate limiting searches. Try again later.', 'hub_rate_limited', 503)
                        if response.status != 200: raise Fault('Hugging Face could not supply a public model. Private and gated repositories are not supported.', 'hub_unavailable', 502)
                        return await read_response(response, 2 * 1024 * 1024)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise Fault('Hugging Face is unavailable or returned invalid data. Try again later.', 'hub_unavailable', 503)

    async def search(self, query):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 120 or any(ord(c) < 32 for c in query):
            raise Fault('Enter 2–120 characters to search for a model.')
        data = await self._get('/api/models', {'search': query.strip(), 'filter': 'gguf',
            'pipeline_tag': 'text-generation', 'sort': 'downloads', 'direction': '-1', 'limit': '20', 'full': 'true'})
        if not isinstance(data, list) or len(data) > 20: raise Fault('Invalid Hugging Face search response.', 'hub_invalid', 502)
        result = []
        for item in data:
            if not isinstance(item, dict) or item.get('private') or item.get('gated'): continue
            try: repo = repository(item.get('id'))
            except Fault: continue
            downloads = item.get('downloads', 0)
            result.append({'id': repo, 'downloads': downloads if type(downloads) is int and downloads >= 0 else 0,
                           'license': license_name(item), 'url': HUB + '/' + repo})
        return {'models': result, 'source': 'Hugging Face', 'notice': 'Public GGUF text models. Compatibility is confirmed by your installed Ollama version during installation.'}

    async def inspect(self, repo):
        repo = repository(repo)
        data = await self._get('/api/models/' + quote(repo, safe='/'), {'blobs': 'true'})
        if not isinstance(data, dict) or data.get('private') or data.get('gated') or data.get('id') != repo:
            raise Fault('Only public, ungated repositories are supported.', 'hub_unsupported', 422)
        sha = data.get('sha')
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha): raise Fault('Model revision is missing.', 'hub_invalid', 502)
        files = data.get('siblings')
        if not isinstance(files, list) or len(files) > 4096: raise Fault('Model file listing is too large.', 'hub_invalid', 502)
        choices = []
        for file in files:
            if not isinstance(file, dict) or not gguf_file(file.get('rfilename')): continue
            name = file['rfilename']; lfs = file.get('lfs') or {}
            if not isinstance(lfs, dict): continue
            size = lfs.get('size'); digest = lfs.get('sha256')
            if type(size) is not int or not 1024**2 <= size <= MAX_WEIGHT_BYTES or not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest): continue
            model = 'hf.co/' + repo + ':' + name
            if len(model) > 250: continue
            quant = re.search(r'(?:^|[._-])((?:I?Q\d)[A-Z0-9_]*|BF16|F16|F32)(?=[.-]|$)', name, re.I)
            choices.append({'id': name, 'filename': name, 'size_bytes': size, 'size_gb': round(size/GIB, 2),
                'sha256': digest, 'quant': quant.group(1).upper() if quant else 'Unknown', 'model': model,
                'memory_estimate_gb': round(size/GIB*1.3 + 2, 1)})
        choices.sort(key=lambda f: (f['quant'] != 'Q4_K_M', f['size_bytes'], f['filename']))
        choices = choices[:64]
        if not choices: raise Fault('No supported single-file GGUF found. Split weights, vision projectors and files without SHA-256 metadata are excluded.', 'hub_unsupported', 422)
        now = time.monotonic()
        self.previews = {k:v for k,v in self.previews.items() if v['expires'] > now}
        while len(self.previews) >= 32: self.previews.pop(next(iter(self.previews)))
        ident = 'hf_' + secrets.token_hex(16)
        record = {'id': ident, 'repository': repo, 'revision': sha, 'license': license_name(data), 'url': HUB + '/' + repo,
                  'files': choices, 'expires': now+300}
        self.previews[ident] = record
        return {k:v for k,v in record.items() if k != 'expires'}

    async def selection(self, ident, filename):
        if not isinstance(ident, str) or not isinstance(filename, str): raise Fault('Select a reviewed model file.')
        preview = self.previews.get(ident)
        if not preview or preview['expires'] <= time.monotonic(): raise Fault('The model preview expired. Inspect it again before downloading.', 'preview_expired', 409)
        file = next((f for f in preview['files'] if f['filename'] == filename), None)
        if not file: raise Fault('Choose one of the reviewed GGUF files.')
        # Ollama's HF integration resolves the live branch. Recheck immediately
        # before pull, then require the reviewed weight digest in pull progress.
        fresh = await self.inspect(preview['repository'])
        if fresh['revision'] != preview['revision'] or file not in fresh['files']:
            raise Fault('This repository changed. Review its current files before downloading.', 'preview_changed', 409)
        return dict(file), preview['repository']
