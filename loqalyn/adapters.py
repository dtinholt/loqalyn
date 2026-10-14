"""Adapters for trusted loopback runtimes; runtime forwarding must be disabled."""
import asyncio
import copy
import json
import re
import time
import uuid

import aiohttp

from .core import DEFAULTS, Model, Fault, GIB, _number, inspect_payload, validate_runtime_url
from .security import loads, read_response, MAX_LINE, MAX_RUNTIME, bounded_tree


def _remote(name, *entries):
    name = name.lower()
    return ':cloud' in name or '-cloud' in name or any(
        entry.get('remote_host') or entry.get('remote_model') or entry.get('is_remote')
        or entry.get('remote') is True or entry.get('location') == 'remote'
        for entry in entries if isinstance(entry, dict))


def _runtime_data(message='Runtime returned invalid metadata.'):
    return Fault(message, 'runtime_error', 502)


def _models(value):
    if not isinstance(value, dict) or not isinstance(value.get('models'), list) or len(value['models'])>256 or any(not isinstance(x, dict) for x in value['models']):
        raise _runtime_data()
    return value['models']


def _model_id(value):
    if not isinstance(value,str) or not value or len(value)>512 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise _runtime_data('Runtime returned an invalid model ID.')
    return value


def _chunk(value):
    try: bounded_tree(value, depth=16, nodes=4096)
    except ValueError: raise _runtime_data('Runtime chunk exceeds structural limits.')
    if not isinstance(value,dict) or 'error' in value or not isinstance(value.get('choices'),list) or len(value['choices'])>1:
        raise _runtime_data('Runtime stream returned an invalid chunk.')
    for choice in value['choices']:
        if not isinstance(choice,dict) or type(choice.get('index',0)) is not int or choice.get('index',0)!=0 or not isinstance(choice.get('delta',{}),dict):
            raise _runtime_data('Runtime returned unsupported choices.')
        if choice.get('finish_reason') not in (None,'stop','length','tool_calls'):
            raise _runtime_data('Unsupported finish reason.')
        delta=choice.get('delta',{})
        for key in ('content','reasoning_content','role'):
            if key in delta and delta[key] is not None and not isinstance(delta[key],str): raise _runtime_data('Invalid runtime text.')
        if delta.get('role') not in (None,'assistant'): raise _runtime_data('Invalid runtime role.')
        calls=delta.get('tool_calls',[])
        if calls is None: calls=[]
        if not isinstance(calls,list) or len(calls)>128: raise _runtime_data('Invalid tool calls.')
        for call in calls:
            if not isinstance(call,dict) or not _number(call.get('index',0),integer=True,maximum=127) or not isinstance(call.get('function',{}),dict): raise _runtime_data('Invalid tool call.')
            if 'id' in call and (not isinstance(call['id'],str) or not call['id'] or len(call['id'])>512): raise _runtime_data('Invalid tool ID.')
            for field in ('name','arguments'):
                if field in call.get('function',{}) and not isinstance(call['function'][field],str): raise _runtime_data('Invalid function delta.')
    if value.get('usage') is not None:
        if not isinstance(value['usage'],dict) or any(not _number(v,integer=True,maximum=2**53) for v in value['usage'].values()): raise _runtime_data('Invalid runtime usage.')
    return value


def _context(value):
    if value is None: return 0
    if not _number(value, minimum=0, integer=True): raise _runtime_data()
    return value


def _size(value):
    if not _number(value, minimum=0): raise _runtime_data()
    return value / GIB


class Adapter:
    def __init__(self, name, url, session, token=None):
        self.name = name; self.url = validate_runtime_url(name, url); self.session = session; self.token = token
        self.metadata_cache = {}; self.cloud_disabled = None

    async def privacy_status(self):
        if self.name != 'ollama': return None
        async with self.session.get(self.url + '/api/status', headers=self.headers, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=5)) as response:
            # Older Ollama versions cannot attest this setting. They still need
            # explicit operator confirmation, including the per-model checks.
            if response.status in (404, 405):
                self.cloud_disabled = None; return None
            if response.status != 200: raise _runtime_data('Could not verify Ollama cloud settings.')
            value = await read_response(response, 16384)
            disabled = value.get('cloud', {}).get('disabled') if isinstance(value, dict) and isinstance(value.get('cloud'), dict) else None
            if type(disabled) is not bool: raise _runtime_data('Ollama returned unknown cloud settings.')
            self.cloud_disabled = disabled
            return disabled

    async def ensure_local(self):
        if await self.privacy_status() is False:
            raise Fault('Ollama cloud access is enabled. Disable it and restart Ollama before generating.', 'runtime_cloud_enabled', 403)

    @property
    def headers(self):
        return {'Authorization': 'Bearer ' + self.token} if self.token else {}

    async def json(self, method, path, body=None, timeout=120):
        async with self.session.request(method, self.url + path, json=body, headers=self.headers,
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status >= 300: raise Fault(f"{self.name} returned HTTP {response.status} for {path}.", 'runtime_error', 502)
            try: result = await read_response(response)
            except (ValueError, aiohttp.ContentTypeError): raise _runtime_data('Runtime returned invalid or oversized JSON.')
            if not isinstance(result, dict) or 'error' in result: raise _runtime_data()
            return result

    async def discover(self):
        await self.privacy_status()
        return await self._ollama_models() if self.name == 'ollama' else await self._lm_models()

    async def _ollama_models(self):
        tags = _models(await self.json('GET', '/api/tags', timeout=5))
        running = {}
        for entry in _models(await self.json('GET', '/api/ps', timeout=5)):
            name = entry.get('name') or entry.get('model')
            _model_id(name)
            running[name] = entry
        result = []; seen = set()
        # Cache immutable manifest metadata; revalidate mutable manifests on each
        # discovery. Before every inference, /api/show is checked again below.
        gate = asyncio.Semaphore(4)
        async def metadata(entry):
            name = _model_id(entry.get('name') or entry.get('model')); digest = entry.get('digest')
            key = (name, digest) if isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest) else None
            if _remote(name, entry): return {}
            if key in self.metadata_cache: return self.metadata_cache[key]
            async with gate:
                value = await self.json('POST', '/api/show', {'model': name}, timeout=10)
                result = {k:v for k,v in value.items() if k in ('model_info','capabilities','remote_host','remote_model','is_remote','remote','location')}
                if len(json.dumps(result).encode()) > 128*1024: raise _runtime_data('Model metadata is too large.')
                return result
        metadata_results = await asyncio.gather(*(metadata(entry) for entry in tags), return_exceptions=True)
        for outcome in metadata_results:
            if isinstance(outcome, BaseException): raise outcome
        self.metadata_cache = {}; cache_bytes = 0
        for entry, meta in zip(tags, metadata_results):
            cache_bytes += len(json.dumps(meta).encode())
            if cache_bytes <= 8*1024*1024 and isinstance(entry.get('digest'), str) and re.fullmatch('[a-f0-9]{64}', entry['digest']):
                self.metadata_cache[(entry.get('name') or entry.get('model'), entry['digest'])] = meta
        for entry, meta in zip(tags, metadata_results):
            name = entry.get('name') or entry.get('model')
            _model_id(name)
            if name in seen: raise _runtime_data('Runtime returned duplicate model IDs.')
            seen.add(name)
            # Cloud aliases can have arbitrary names: inspect their metadata too.
            if _remote(name, entry):
                if name in running: result.append(self._running_only(name, running[name]))
                continue
            if _remote(name, entry, meta):
                if name in running: result.append(self._running_only(name, running[name]))
                continue
            info = meta.get('model_info') or {}; details = entry.get('details') or {}; caps = meta.get('capabilities') or []
            if not isinstance(info, dict) or not isinstance(details, dict) or not isinstance(caps, list): raise _runtime_data()
            def prop(suffix, default=0):
                return next((v for k, v in info.items() if isinstance(k, str) and k.endswith('.' + suffix)), default)
            context = _context(prop('context_length', 0))
            layers = prop('block_count'); heads = prop('attention.head_count')
            kvheads = prop('attention.head_count_kv', heads); embed = prop('embedding_length')
            # Some architectures (including Qwen3) use a head dimension that
            # differs from embedding_length / head_count. Prefer explicit K/V.
            head_dim = embed / heads if _number(heads, minimum=1) and _number(embed, minimum=1) else 0
            key_dim = prop('attention.key_length', head_dim); value_dim = prop('attention.value_length', head_dim)
            dims = (layers, kvheads, key_dim, value_dim)
            kv = layers * kvheads * (key_dim + value_dim) * 2 * 1024 / GIB if all(_number(v, minimum=1) for v in dims) else 1.0
            loaded = running.get(name, {})
            chat = not caps or 'completion' in caps
            if not chat and not loaded: continue
            m = Model(id='ollama/' + name, name=name, runtime='ollama', handle=name,
                      size_gb=_size(entry.get('size', 0)), context_max=context,
                      capabilities=(['chat'] if chat else ['embedding']) + (['code'] if 'code' in name.lower() else [])
                      + (['vision'] if 'vision' in caps else []) + (['tools'] if 'tools' in caps else []) + (['reasoning'] if 'thinking' in caps else []),
                      quant=details.get('quantization_level') or 'Unknown', format=details.get('format') or 'gguf',
                      family=str(details.get('family') or 'Unknown')[:128], parameters=str(details.get('parameter_size') or 'Unknown')[:128],
                      kv_gb_per_1k=kv, loaded=bool(loaded), loaded_context=_context(loaded.get('context_length', 0)),
                      instance=name if loaded else None, instance_count=1 if loaded else 0, available=chat)
            # The reported digest is a manifest hash, not the weight file hash.
            if loaded: m.memory_gb = max(_size(loaded.get('size', 0)), m.footprint(m.loaded_context or 8192))
            result.append(m)
        # An externally loaded model still reserves memory if it disappeared from tags.
        for name, entry in running.items():
            if name not in seen: result.append(self._running_only(name, entry))
        return result

    def _running_only(self, name, entry):
        m = Model(id='ollama/' + name, name=name, runtime='ollama', handle=name,
                  size_gb=_size(entry.get('size', 0)), context_max=_context(entry.get('context_length', 0)),
                  loaded=True, loaded_context=_context(entry.get('context_length', 0)), instance=name,
                  instance_count=1, capabilities=[], available=False)
        m.memory_gb = max(m.size_gb, m.footprint(m.loaded_context or 8192))
        return m

    async def _lm_models(self):
        result = []; seen = set()
        for entry in _models(await self.json('GET', '/api/v1/models', timeout=10)):
            instances = entry.get('loaded_instances', [])
            if not isinstance(instances, list) or any(not isinstance(x, dict) for x in instances): raise _runtime_data()
            if entry.get('type') != 'llm' and not instances: continue
            key = entry.get('key')
            _model_id(key)
            if key in seen: raise _runtime_data()
            seen.add(key)
            remote=_remote(key,entry)
            if remote and not instances: continue
            caps = entry.get('capabilities') or {}; quant = entry.get('quantization') or {}
            if not isinstance(caps, dict) or not isinstance(quant, dict): raise _runtime_data()
            contexts = []
            for instance in instances:
                _model_id(instance.get('id'))
                if not isinstance(instance.get('config', {}), dict): raise _runtime_data()
                contexts.append(_context(instance.get('config', {}).get('context_length', 0)))
            chat = entry.get('type') == 'llm'
            m = Model(id='lmstudio/' + key, name=entry.get('display_name') or key, runtime='lmstudio', handle=key,
                      size_gb=_size(entry.get('size_bytes', 0)), context_max=_context(entry.get('max_context_length')),
                      quant=quant.get('name') or 'Unknown', format=entry.get('format') or 'Unknown',
                      capabilities=(['chat'] if chat else [entry.get('type', 'unknown')])
                      + (['code'] if 'code' in key.lower() else []) + (['tools'] if caps.get('trained_for_tool_use') else [])
                      + (['vision'] if caps.get('vision') else []) + (['reasoning'] if caps.get('reasoning') else []), loaded=bool(instances),
                      loaded_context=contexts[0] if contexts else 0, instance=instances[0]['id'] if instances else None,
                      instance_count=len(instances), available=chat and not remote)
            m.memory_gb = sum(m.footprint(context or 8192) for context in contexts)
            result.append(m)
        return result

    def _local_model(self, model):
        if not model.available or model.state_uncertain: raise Fault('Runtime/model is unavailable.', 'runtime_offline', 503)
        if model.runtime != self.name or _remote(model.handle): raise Fault('Remote models are disabled.', 'remote_model_blocked', 403)

    async def load(self, model, context):
        self._local_model(model)
        await self.ensure_local()
        if self.name == 'ollama':
            # Recheck cloud alias metadata immediately before invoking the model.
            meta = await self.json('POST', '/api/show', {'model': model.handle})
            if _remote(model.handle, meta): raise Fault('Ollama cloud models are disabled.', 'remote_model_blocked', 403)
            await self.json('POST', '/api/generate', {'model': model.handle, 'prompt': '', 'stream': False,
                           'keep_alive': -1, 'options': {'num_ctx': context}})
            running = _models(await self.json('GET', '/api/ps'))
            loaded = next((x for x in running if (x.get('name') or x.get('model')) == model.handle), None)
            if loaded is None: raise _runtime_data('Ollama did not confirm the model load.')
            actual = _context(loaded.get('context_length', 0))
            if not actual: raise _runtime_data('Ollama did not report loaded context.')
            return {'instance_id': model.handle, 'load_config': {'context_length': actual}}
        info = await self.json('POST', '/api/v1/models/load', {'model': model.handle, 'context_length': context, 'echo_load_config': True})
        if not isinstance(info.get('instance_id'), str) or not info['instance_id'] or not isinstance(info.get('load_config'), dict):
            raise _runtime_data('LM Studio did not confirm the loaded instance and context.')
        actual = _context(info['load_config'].get('context_length', 0))
        if not actual: raise _runtime_data('LM Studio did not report loaded context.')
        return info

    async def unload(self, model):
        if self.name == 'ollama':
            return await self.json('POST', '/api/generate', {'model': model.handle, 'prompt': '', 'stream': False, 'keep_alive': 0})
        if not model.instance: raise Fault('No loaded instance ID is available; restart the runtime.', 'runtime_state_unknown', 503)
        return await self.json('POST', '/api/v1/models/unload', {'instance_id': model.instance})

    async def pull(self, name, progress=None, expected=None):
        if not isinstance(name, str) or not name or name.startswith('-') or len(name) > 250 or any(c.isspace() for c in name):
            raise Fault('Invalid model name.')
        if _remote(name): raise Fault('Cloud model aliases are disabled.', 'remote_model_blocked', 403)
        if self.name == 'ollama':
            finished = False; verified = False; total_bytes = 0; layers = {}
            async with self.session.post(self.url + '/api/pull', json={'model': name, 'stream': True},
                    headers=self.headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=7200, sock_read=180)) as response:
                if response.status != 200: raise _runtime_data('Ollama rejected the model download.')
                async for line in response.content:
                    total_bytes += len(line)
                    if len(line) > 65536 or total_bytes > 16*1024*1024: raise _runtime_data('Download progress exceeded limits.')
                    if not line.strip(): continue
                    item = loads(line, limit=65536)
                    if not isinstance(item, dict) or 'error' in item: raise _runtime_data('Ollama could not download or install this GGUF. Check model compatibility and available disk space.')
                    status = item.get('status')
                    if not isinstance(status, str) or len(status) > 256: raise _runtime_data('Invalid download status.')
                    total = item.get('total', 0); completed = item.get('completed', 0); digest = item.get('digest')
                    if not _number(total, integer=True, maximum=128*GIB) or not _number(completed, integer=True, maximum=total): raise _runtime_data('Invalid download byte counts.')
                    if expected and total:
                        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest): raise _runtime_data('Download digest is missing.')
                        layers[digest] = total
                        if len(layers) > 16 or sum(layers.values()) > expected['size_bytes'] + 4*1024*1024:
                            raise _runtime_data('Download size changed; inspect the repository again.')
                        if digest == 'sha256:' + expected['sha256']:
                            if total != expected['size_bytes']: raise _runtime_data('Reviewed model size changed.')
                            verified = True
                        elif total > 1024*1024:
                            raise _runtime_data('The model digest changed; inspect the repository again.')
                    if progress:
                        progress({'phase': ''.join(c for c in status if c.isprintable()), 'total_bytes': total,
                                  'completed_bytes': completed, 'percent': round(completed/total*100, 1) if total else None})
                    if status == 'success': finished = True
            if not finished or (expected and not verified): raise _runtime_data('Download ended without confirmation of the reviewed model weights.')
            if expected:
                # Progress updates may skip the final completed==total event.
                # Require terminal success AND the reviewed content-addressed
                # blob, instead of trusting a sampled progress counter.
                async with self.session.head(self.url + '/api/blobs/sha256:' + expected['sha256'],
                        headers=self.headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as blob:
                    if blob.status != 200: raise _runtime_data('Ollama did not confirm the reviewed weight blob.')
            return {'status': 'success', 'verified_sha256': expected['sha256'] if expected else None}
        return await self.json('POST', '/api/v1/models/download', {'model': name}, timeout=7200)

    def native_body(self, model, body):
        inspect_payload(body, DEFAULTS)
        messages = []; tool_names = {}
        for message in body['messages']:
            message = copy.deepcopy(message)
            role = 'system' if message['role'] == 'developer' else message['role']
            content = message.get('content') or ''
            if isinstance(content, list): content = '\n'.join(block['text'] for block in content)
            new = {'role': role, 'content': content}
            if 'name' in message and role != 'tool': raise Fault('Ollama does not support named chat messages.', 'unsupported_feature')
            if message.get('reasoning_content'): new['thinking'] = message['reasoning_content']
            if message.get('tool_calls'):
                new['tool_calls'] = []
                for call in message['tool_calls']:
                    fn = call['function']; tool_names[call['id']] = fn['name']
                    new['tool_calls'].append({'function': {'name': fn['name'], 'arguments': loads(fn['arguments'])}})
            if role == 'tool': new['tool_name'] = tool_names[message['tool_call_id']]
            messages.append(new)
        options = {'num_ctx': model.loaded_context, 'num_predict': body.get('max_tokens', body.get('max_completion_tokens', DEFAULTS['default_max_tokens']))}
        for key in ('temperature', 'top_p', 'seed', 'stop'):
            if key in body and body[key] is not None: options[key] = [body[key]] if key == 'stop' and isinstance(body[key], str) else body[key]
        result = {'model': model.handle, 'messages': messages, 'stream': True, 'keep_alive': -1, 'options': options}
        if body.get('tools') and body.get('tool_choice') != 'none':
            result['tools'] = copy.deepcopy(body['tools'])
            for tool in result['tools']: tool['function'].pop('strict', None)
        fmt = body.get('response_format', {})
        if fmt.get('type') == 'json_object': result['format'] = 'json'
        if fmt.get('type') == 'json_schema': result['format'] = copy.deepcopy(fmt['json_schema']['schema'])
        return result

    async def chunks(self, model, body, *, timeout=600):
        """Yield canonical OpenAI chunks and reject interrupted/malformed streams."""
        self._local_model(model)
        await self.ensure_local()
        native = self.name == 'ollama'
        if native:
            meta = await self.json('POST', '/api/show', {'model': model.handle})
            if _remote(model.handle, meta): raise Fault('Ollama cloud models are disabled.', 'remote_model_blocked', 403)
            payload = self.native_body(model, body)
        else:
            inspect_payload(body, DEFAULTS)
            payload = {**copy.deepcopy(body), 'model': model.instance or model.handle, 'stream': True, 'stream_options': {'include_usage': True}}
            payload.pop('metadata', None); payload.pop('max_completion_tokens', None)
        path = '/api/chat' if native else '/v1/chat/completions'
        request_id = 'chatcmpl-' + uuid.uuid4().hex[:20]; created = int(time.time()); tool_index = 0
        first = True; finished = False; seen_tools = {}
        async with self.session.post(self.url + path, json=payload, headers=self.headers, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=timeout, sock_read=min(180, timeout))) as response:
            if response.status >= 300: raise Fault(f'{self.name} rejected generation (HTTP {response.status}).', 'runtime_error', 502)
            async for line in response.content:
                if len(line)>MAX_LINE: raise _runtime_data('Runtime stream line exceeds the byte limit.')
                if not line.strip(): continue
                if not native:
                    if not line.startswith(b'data:'): continue
                    value = line[5:].strip()
                    if value == b'[DONE]':
                        if not finished: raise _runtime_data('Runtime ended without a completion finish reason.')
                        break
                    try: chunk = _chunk(loads(value,limit=MAX_LINE))
                    except ValueError: raise _runtime_data('Runtime returned malformed SSE JSON.')
                    if not isinstance(chunk, dict) or 'error' in chunk or not isinstance(chunk.get('choices'), list): raise _runtime_data('Runtime stream returned an invalid chunk.')
                    for choice in chunk['choices']:
                        if not isinstance(choice, dict) or choice.get('index', 0) != 0 or not isinstance(choice.get('delta', {}), dict): raise _runtime_data('Runtime returned unsupported choices.')
                        if choice.get('finish_reason'): finished = True
                    chunk['model'] = model.id
                    yield chunk
                    continue
                try: data = loads(line,limit=MAX_LINE)
                except ValueError: raise _runtime_data('Runtime returned malformed NDJSON.')
                if not isinstance(data, dict) or 'error' in data or not isinstance(data.get('message', {}), dict): raise _runtime_data('Runtime stream returned an invalid chunk.')
                message = data.get('message', {}); delta = {'role': 'assistant'} if first else {}; first = False
                for source, dest in (('content', 'content'), ('thinking', 'reasoning_content')):
                    if source in message and not isinstance(message[source], str): raise _runtime_data('Runtime returned invalid message text.')
                    if message.get(source): delta[dest] = message[source]
                if message.get('tool_calls'):
                    if not isinstance(message['tool_calls'], list): raise _runtime_data('Runtime returned invalid tool calls.')
                    calls = []
                    for call in message['tool_calls']:
                        if not isinstance(call, dict) or not isinstance(call.get('function'), dict): raise _runtime_data('Runtime returned invalid tool call.')
                        fn = call['function']; args = fn.get('arguments', {}); name = fn.get('name')
                        if not isinstance(name, str) or not isinstance(args, dict): raise _runtime_data('Runtime returned invalid function arguments.')
                        supplied_index = call.get('index', fn.get('index'))
                        if supplied_index is not None:
                            if not _number(supplied_index, integer=True): raise _runtime_data('Runtime returned invalid tool index.')
                            previous = seen_tools.get(supplied_index)
                            if previous is not None:
                                if previous == (name, args): continue
                                raise _runtime_data('Runtime changed an already completed tool call.')
                            seen_tools[supplied_index] = (name, args)
                        calls.append({'index': tool_index, 'id': 'call_' + uuid.uuid4().hex[:12], 'type': 'function',
                                      'function': {'name': name, 'arguments': json.dumps(args)}})
                        tool_index += 1
                    if calls: delta['tool_calls'] = calls
                finished = data.get('done') is True
                reason = ('length' if data.get('done_reason') == 'length' else 'tool_calls' if tool_index else 'stop') if finished else None
                chunk = {'id': request_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model.id,
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
                if finished:
                    prompt = data.get('prompt_eval_count', 0); completion = data.get('eval_count', 0)
                    if not _number(prompt, integer=True) or not _number(completion, integer=True): raise _runtime_data('Runtime returned invalid usage.')
                    chunk['usage'] = {'prompt_tokens': prompt, 'completion_tokens': completion, 'total_tokens': prompt + completion}
                    duration = data.get('eval_duration', 0)
                    if _number(duration, minimum=1): model.speed_tps = round(completion / (duration / 1e9), 2)
                yield chunk
                if finished: break
        if not finished: raise Fault('Runtime stream ended before completion.', 'truncated_response', 502)


def _anthropic_text(value):
    if isinstance(value, str): return value
    if not isinstance(value, list): raise Fault('Anthropic text content must be text or text blocks.')
    values = []
    for block in value:
        if not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str):
            raise Fault('Only Anthropic text blocks are supported here.', 'unsupported_feature')
        if set(block) - {'type', 'text'}: raise Fault('Anthropic block extensions are unsupported.', 'unsupported_feature')
        values.append(block['text'])
    return '\n'.join(values)


def anthropic_request(body):
    """Translate text/function-tool subset, rejecting fields that would lose meaning."""
    if not isinstance(body, dict): raise Fault('Request must be a JSON object.')
    allowed = {'model', 'max_tokens', 'temperature', 'top_p', 'stream', 'metadata', 'system', 'messages', 'tools', 'stop_sequences', 'tool_choice'}
    if set(body) - allowed: raise Fault('Unsupported Anthropic request field.', 'unsupported_feature')
    if 'max_tokens' not in body: raise Fault('Anthropic requests require max_tokens.')
    result = {k: copy.deepcopy(body[k]) for k in ('model', 'max_tokens', 'temperature', 'top_p', 'stream', 'metadata') if k in body}
    messages = []
    if 'system' in body: messages.append({'role': 'system', 'content': _anthropic_text(body['system'])})
    incoming = body.get('messages')
    if not isinstance(incoming, list) or not incoming: raise Fault('messages must be a non-empty list.')
    for msg in incoming:
        if not isinstance(msg, dict) or msg.get('role') not in ('user', 'assistant') or set(msg) - {'role', 'content'}:
            raise Fault('Anthropic messages must have user/assistant roles and content.')
        if isinstance(msg.get('content'), str): messages.append(copy.deepcopy(msg)); continue
        if not isinstance(msg.get('content'), list) or not msg['content']: raise Fault('Anthropic message content must be text or a non-empty block list.')
        # Text following tool results stays after those results; reject mixed order
        # which cannot be represented faithfully by canonical role-based messages.
        texts = []; calls = []; results = []; seen_text = False
        for block in msg['content']:
            if not isinstance(block, dict): raise Fault('Anthropic content blocks must be objects.')
            kind = block.get('type')
            if kind == 'text':
                texts.append(_anthropic_text([block])); seen_text = True
            elif kind == 'tool_use':
                if msg['role'] != 'assistant' or set(block) - {'type', 'id', 'name', 'input'} or not isinstance(block.get('input'), dict):
                    raise Fault('Invalid Anthropic tool_use block.')
                calls.append({'id': block.get('id'), 'type': 'function', 'function': {'name': block.get('name'), 'arguments': json.dumps(block['input'])}})
            elif kind == 'tool_result':
                if msg['role'] != 'user' or seen_text or set(block) - {'type', 'tool_use_id', 'content', 'is_error'}:
                    raise Fault('Tool results must precede user text in Anthropic messages.')
                if 'is_error' in block and not isinstance(block['is_error'], bool): raise Fault('is_error must be boolean.')
                text = _anthropic_text(block.get('content', ''))
                if block.get('is_error'): text = 'Tool error: ' + text
                results.append({'role': 'tool', 'tool_call_id': block.get('tool_use_id'), 'content': text})
            else: raise Fault(f'Unsupported Anthropic block: {kind}.', 'unsupported_feature')
        messages.extend(results)
        if texts or calls:
            value = {'role': msg['role'], 'content': '\n'.join(texts)}
            if calls: value['tool_calls'] = calls
            messages.append(value)
    result['messages'] = messages
    if 'tools' in body:
        if not isinstance(body['tools'], list): raise Fault('tools must be a list.')
        result['tools'] = []
        for tool in body['tools']:
            if not isinstance(tool, dict) or set(tool) - {'name', 'description', 'input_schema'}: raise Fault('Only local Anthropic function tools are supported.', 'unsupported_feature')
            result['tools'].append({'type': 'function', 'function': {'name': tool.get('name'), 'description': tool.get('description', ''), 'parameters': tool.get('input_schema')}})
    if 'stop_sequences' in body: result['stop'] = body['stop_sequences']
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if not isinstance(choice, dict) or choice.get('type') not in ('auto', 'none') or set(choice) - {'type'}:
            raise Fault('Only automatic or disabled tool choice is supported.', 'unsupported_feature')
        result['tool_choice'] = choice['type']
    inspect_payload(result, DEFAULTS)
    return result


def complete(chunks, model):
    content = ''; reasoning = ''; calls = {}; usage = {}; reason = 'stop'; request_id = ''; created = int(time.time())
    for chunk in chunks:
        if not isinstance(chunk, dict): raise _runtime_data('Invalid completion chunk.')
        if not request_id: request_id = chunk.get('id') or 'chatcmpl-' + uuid.uuid4().hex[:20]; created = chunk.get('created', created)
        if chunk.get('usage') is not None:
            if not isinstance(chunk['usage'], dict): raise _runtime_data('Invalid runtime usage.')
            usage = chunk['usage']
        for choice in chunk.get('choices', []):
            if not isinstance(choice, dict) or choice.get('index', 0) != 0 or not isinstance(choice.get('delta', {}), dict): raise _runtime_data('Invalid completion choice.')
            delta = choice.get('delta', {})
            for key in ('content', 'reasoning_content'):
                if delta.get(key) is not None and not isinstance(delta[key], str): raise _runtime_data('Invalid completion text.')
            content += delta.get('content') or ''; reasoning += delta.get('reasoning_content') or ''
            if choice.get('finish_reason'): reason = choice['finish_reason']
            for call in delta.get('tool_calls') or []:
                if not isinstance(call, dict) or not _number(call.get('index', 0), integer=True) or not isinstance(call.get('function', {}), dict): raise _runtime_data('Invalid tool-call delta.')
                index = call.get('index', 0); dest = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                if call.get('id'): dest['id'] = call['id']
                for key in ('name', 'arguments'):
                    value = call.get('function', {}).get(key) or ''
                    if not isinstance(value, str): raise _runtime_data('Invalid function delta.')
                    dest['function'][key] += value
    message = {'role': 'assistant', 'content': content if content or not calls else None}
    if calls:
        ids=set()
        for call in calls.values():
            if not call['id'] or call['id'] in ids or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',call['function']['name']): raise _runtime_data('Invalid completed tool identity.')
            ids.add(call['id'])
            try: arguments=loads(call['function']['arguments'])
            except ValueError: raise _runtime_data('Invalid completed tool arguments.')
            if not isinstance(arguments,dict): raise _runtime_data('Tool arguments must be a JSON object.')
        message['tool_calls'] = [calls[index] for index in sorted(calls)]
    if reasoning: message['reasoning_content'] = reasoning
    return {'id': request_id, 'object': 'chat.completion', 'created': created, 'model': model,
            'choices': [{'index': 0, 'message': message, 'finish_reason': reason}], 'usage': usage}


def anthropic_response(value):
    message = value['choices'][0]['message']; blocks = []
    if message.get('content'): blocks.append({'type': 'text', 'text': message['content']})
    for call in message.get('tool_calls', []):
        try: args = loads(call['function']['arguments'])
        except (ValueError, TypeError): raise Fault('Model produced invalid tool arguments.', 'invalid_tool_arguments', 502)
        if not isinstance(args, dict): raise Fault('Model tool arguments must be a JSON object.', 'invalid_tool_arguments', 502)
        blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': args})
    usage = value.get('usage', {})
    reason = value['choices'][0]['finish_reason']
    if reason not in ('stop', 'length', 'tool_calls'): raise Fault('Runtime finish reason cannot be represented by the supported Anthropic subset.', 'unsupported_feature', 502)
    return {'id': value['id'], 'type': 'message', 'role': 'assistant', 'model': value['model'], 'content': blocks,
            'stop_reason': {'tool_calls': 'tool_use', 'length': 'max_tokens'}.get(reason, 'end_turn'), 'stop_sequence': None,
            'usage': {'input_tokens': usage.get('prompt_tokens', 0), 'output_tokens': usage.get('completion_tokens', 0)}}
