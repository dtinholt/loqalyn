"""Bounded input and private-file primitives shared by gateway and native shell."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import stat

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_RUNTIME = 8 * 1024 * 1024
MAX_LINE = 256 * 1024
MAX_DEPTH = 32
MAX_NODES = 32768


def bounded_tree(value, *, depth=MAX_DEPTH, nodes=MAX_NODES):
    """Iterative traversal also rejects cycles, non-JSON types and nonfinite numbers."""
    stack = [(value, 0)]
    remaining = nodes
    while stack:
        item, level = stack.pop()
        remaining -= 1
        if remaining < 0 or level > depth:
            raise ValueError('JSON exceeds the depth or node limit.')
        if isinstance(item, dict):
            if len(item) > remaining or any(not isinstance(k, str) for k in item):
                raise ValueError('JSON object exceeds the node limit or has nontext keys.')
            stack.extend((x, level + 1) for x in item.values())
        elif isinstance(item, list):
            if len(item) > remaining:
                raise ValueError('JSON list exceeds the node limit.')
            stack.extend((x, level + 1) for x in item)
        elif item is None or isinstance(item, (str, bool)):
            pass
        elif isinstance(item, int):
            if item.bit_length() > 256:
                raise ValueError('JSON integer exceeds the numeric limit.')
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError('JSON numbers must be finite.')
        else:
            raise ValueError('Only JSON values are supported.')


def loads(data, *, limit=MAX_BODY):
    if len(data) > limit:
        raise ValueError('JSON exceeds the byte limit.')
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('Duplicate JSON keys are not allowed.')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('JSON numbers must be finite.')
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        bounded_tree(value)
        return value
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid or excessively nested JSON.') from exc


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Data folder must be an owned directory, not a link.')
    os.chmod(path, 0o700, follow_symlinks=False)
    return path


def private_file(path, *, allow_missing=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return
        raise
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError('Sensitive files must be owned private regular files with no links.')


def read_private(path, limit=MAX_BODY):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('Sensitive files must be private regular files with no links.')
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError('File exceeds the byte limit.')
        return data


def api_token(path):
    path = Path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32))
            handle.flush()
            os.fsync(handle.fileno())
    value = read_private(path, 128).decode('ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', value):
        raise ValueError('API token is invalid. Restore a private, valid token file.')
    return value


async def read_response(response, limit=MAX_RUNTIME):
    data = bytearray()
    async for part in response.content.iter_chunked(65536):
        if len(data) + len(part) > limit:
            raise ValueError('Runtime response exceeds the byte limit.')
        data.extend(part)
    return loads(data, limit=limit)


def schema_subset(schema):
    """Finite JSON Schema subset; no regex, combinators, or reference expansion."""
    bounded_tree(schema, depth=16, nodes=1024)
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items',
                 'enum', 'const', 'description', 'title', 'default', 'minimum',
                 'maximum', 'minLength', 'maxLength', 'minItems', 'maxItems'}
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, bool):
            continue
        if not isinstance(node, dict) or set(node) - supported:
            raise ValueError('Unsupported JSON Schema keyword; references, regex and combinators are disabled.')
        for key in ('enum',):
            if key in node and (not isinstance(node[key], list) or len(node[key]) > 128
                    or any(isinstance(v, (dict, list)) for v in node[key])):
                raise ValueError('Schema enum must contain at most 128 scalar values.')
        if 'const' in node and isinstance(node['const'], (dict, list)):
            raise ValueError('Schema const must be scalar.')
        if 'properties' in node:
            if not isinstance(node['properties'], dict):
                raise ValueError('Schema properties must be an object.')
            stack.extend(node['properties'].values())
        for key in ('items', 'additionalProperties'):
            if key in node:
                stack.append(node[key])


def privacy_tags(text, policy, private=False):
    """Bounded linear candidate scans; not a proof that text contains no secrets."""
    tags = []
    # Partition by @ rather than an unanchored variable-length email regex.
    for match in re.finditer('@', text):
        left = text[max(0, match.start() - 254):match.start()]
        right = text[match.end():match.end() + 254]
        if re.search(r'[\w.+-]+$', left) and re.match(r'[\w.-]+\.[A-Za-z]{2,}\b', right):
            tags.append('email')
            break
    if re.search(r'(?<!\w)(?:sk-[A-Za-z0-9_-]{12,256}|AKIA[A-Z0-9]{16})(?!\w)', text):
        tags.append('credential')
    if re.search(r'(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)', text):
        tags.append('ssn')
    lower = text.lower()
    if any(term.lower() in lower for term in policy['sensitive_terms'] if term):
        tags.append('sensitive_term')
    if private:
        tags.append('private')
    return tags
