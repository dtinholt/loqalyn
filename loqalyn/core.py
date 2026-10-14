from __future__ import annotations
import asyncio, copy, contextvars, hashlib, json, math, os, re, subprocess, sys, tempfile, time, uuid
import apsw
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import yaml
from .security import (MAX_BODY, bounded_tree, loads, private_directory, private_file,
                       read_private, schema_subset, privacy_tags)

GIB = 1024 ** 3
DEFAULTS = {
    "budget_gb": 96, "headroom_gb": 4, "default_max_tokens": 1024,
    "load_timeout": 120, "request_timeout": 600, "queue_timeout": 30,
    "escalation_mode": "flag", "sensitive_terms": [], "log_retention_days": 30,
    "runtime_local_only_confirmed": False,
    "runtimes": {"ollama": "http://127.0.0.1:11434"},
    "routes": [
        {"name": "code_edit", "description": "Write, refactor, debug and review code", "keywords": ["code", "python", "function", "debug", "refactor", "typescript", "sql", "bug"], "prefer": [], "enabled": True},
        {"name": "long_document", "description": "Summarize and question long documents", "keywords": ["document", "summarize", "contract", "report"], "prefer": [], "enabled": True},
        {"name": "hard_reasoning", "description": "Multi-step mathematics and complex analysis", "keywords": ["prove", "theorem", "strategy", "reasoning", "probability", "analyze"], "prefer": [], "escalate": "suggest", "enabled": True},
        {"name": "quick_chat", "description": "Everyday questions, writing and translation", "keywords": [], "prefer": [], "enabled": True},
    ],
}

class Fault(Exception):
    def __init__(self, message, code="invalid_request", status=400, details=None):
        super().__init__(message); self.code=code; self.status=status; self.details=details


def _number(value, *, minimum=0, maximum=float('inf'), integer=False):
    try:
        return (not isinstance(value, bool) and isinstance(value, int if integer else (int, float))
                and minimum <= value <= maximum and math.isfinite(value))
    except (OverflowError, TypeError, ValueError):
        return False


def validate_runtime_url(name, url):
    from urllib.parse import urlsplit
    try:
        if not isinstance(url, str) or any(c.isspace() for c in url): raise ValueError()
        parsed = urlsplit(url)
        if (name not in ('ollama', 'lmstudio') or parsed.scheme != 'http'
                or parsed.hostname not in ('127.0.0.1', '::1') or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
    except (TypeError, ValueError):
        raise Fault("Runtime URLs must use loopback HTTP with a literal 127.0.0.1 or ::1 address.")
    return url.rstrip('/')


def validate_policy(value):
    if not isinstance(value, dict): raise Fault("Policy must be a YAML object.")
    try:
        bounded_tree(value, depth=12, nodes=4096)
        if len(json.dumps(value, allow_nan=False).encode()) > 32768: raise ValueError('Policy exceeds 32 KiB.')
    except (ValueError, TypeError): raise Fault('Policy exceeds finite JSON/complexity limits.')
    if set(value) - set(DEFAULTS): raise Fault("Unknown policy field.")
    result = copy.deepcopy(DEFAULTS); result.update(copy.deepcopy(value))
    for key in ('budget_gb', 'headroom_gb'):
        if not _number(result[key]): raise Fault(f"{key} must be a non-negative finite number.")
    if not 4 <= result['budget_gb'] <= 1024 or result['headroom_gb'] >= result['budget_gb']:
        raise Fault("Budget must be 4–1024 GiB; headroom must be smaller than budget.")
    for key in ('load_timeout', 'request_timeout', 'queue_timeout'):
        if not _number(result[key], minimum=.001, maximum=86400): raise Fault(f"{key} must be positive and at most 86400 seconds.")
    if not _number(result['default_max_tokens'], minimum=1, maximum=131072, integer=True):
        raise Fault("default_max_tokens must be an integer between 1 and 131072.")
    if not _number(result['log_retention_days'], maximum=3650, integer=True):
        raise Fault("log_retention_days must be an integer between 0 and 3650.")
    if not isinstance(result['runtime_local_only_confirmed'], bool): raise Fault("runtime_local_only_confirmed must be boolean.")
    if result['escalation_mode'] not in ('flag', 'off'):
        raise Fault("This release supports flag and off. Frontier calls require individual approval.")
    if not isinstance(result['sensitive_terms'], list) or any(not isinstance(x, str) for x in result['sensitive_terms']):
        raise Fault("sensitive_terms must be a list of strings.")
    if len(result['sensitive_terms']) > 128 or any(len(x) > 256 for x in result['sensitive_terms']):
        raise Fault('At most 128 sensitive terms of 256 characters are supported.')
    if not isinstance(result['runtimes'], dict) or not result['runtimes']: raise Fault("runtimes must be a non-empty object.")
    result['runtimes'] = {name: validate_runtime_url(name, url) for name, url in result['runtimes'].items()}
    routes = result['routes']
    if not isinstance(routes, list) or not routes: raise Fault("At least one route is required.")
    if len(routes) > 32: raise Fault('At most 32 routes are supported.')
    names = []
    for item in routes:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,60}', item['name']):
            raise Fault("Invalid route name.")
        if set(item) - {'name', 'description', 'keywords', 'prefer', 'enabled', 'escalate'}: raise Fault("Unknown route field.")
        names.append(item['name'])
        for key in ('prefer', 'keywords'):
            if not isinstance(item.get(key, []), list) or any(not isinstance(x, str) or not x for x in item.get(key, [])):
                raise Fault(f"Route {key} must be a list of non-empty strings.")
            item.setdefault(key, [])
            if len(item[key]) > (64 if key == 'keywords' else 32) or any(len(x) > (128 if key == 'keywords' else 512) for x in item[key]):
                raise Fault('Route keywords or preferred IDs exceed complexity limits.')
        if 'description' in item and not isinstance(item['description'], str): raise Fault("Route description must be a string.")
        if not isinstance(item.get('enabled', True), bool): raise Fault("Route enabled must be boolean.")
        if item.get('escalate') not in (None, 'suggest', 'off'): raise Fault("Route escalation must be suggest or off.")
    if len(set(names)) != len(names): raise Fault("Route names must be unique.")
    if sum(len(r['keywords']) for r in routes) > 64: raise Fault('At most 64 total keywords are supported.')
    if not any(r['name'] == 'quick_chat' and r.get('enabled', True) for r in routes):
        raise Fault("quick_chat must stay enabled as the fallback.")
    return result


CATALOG_FIELDS = {'context_max', 'size_gb', 'kv_gb_per_1k', 'tools_verified', 'tier', 'digest', 'task_scores', 'name', 'speed_tps', 'installation_verified'}


def validate_catalog_patch(value):
    if not isinstance(value, dict) or set(value) - CATALOG_FIELDS: raise Fault("Unknown or invalid catalog field.")
    result = copy.deepcopy(value)
    for key in ('size_gb', 'kv_gb_per_1k'):
        if key in result and not _number(result[key], minimum=.000001, maximum=1048576): raise Fault(f"{key} must be positive and finite.")
    if 'context_max' in result and not _number(result['context_max'], minimum=1, maximum=16777216, integer=True):
        raise Fault("context_max must be a positive integer.")
    if 'tools_verified' in result and not isinstance(result['tools_verified'], bool): raise Fault("tools_verified must be boolean.")
    if 'installation_verified' in result and not isinstance(result['installation_verified'], bool): raise Fault("installation_verified must be boolean.")
    if result.get('tier', 'on_demand') not in ('resident', 'on_demand', 'archive'): raise Fault("Invalid tier.")
    if 'digest' in result and result['digest'] is not None:
        if not isinstance(result['digest'], str) or not re.fullmatch('[a-f0-9]{64}', result['digest']):
            raise Fault("Digest must be a SHA-256 hash of the actual weight file, or null.")
    if 'name' in result and (not isinstance(result['name'], str) or not result['name'].strip() or len(result['name']) > 250):
        raise Fault("Model name must be a non-empty string of at most 250 characters.")
    if 'speed_tps' in result and result['speed_tps'] is not None and not _number(result['speed_tps'], minimum=.000001):
        raise Fault("speed_tps must be positive and finite, or null.")
    if 'task_scores' in result:
        scores = result['task_scores']
        if not isinstance(scores, dict) or any(not isinstance(k, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,60}', k)
                or not _number(v, maximum=1) for k, v in scores.items()):
            raise Fault("task_scores must map route names to finite scores between 0 and 1.")
    return result


def validate_catalog(value):
    try: bounded_tree(value, depth=12, nodes=16384)
    except ValueError as exc: raise Fault(str(exc))
    if not isinstance(value, dict) or set(value) != {'models'} or not isinstance(value['models'], dict):
        raise Fault("catalog.yaml must contain a models object.")
    models = {}
    for name, patch in value['models'].items():
        if not isinstance(name, str) or not name.startswith(('ollama/', 'lmstudio/')) or len(name) > 512:
            raise Fault("Catalog model IDs must use an ollama/ or lmstudio/ prefix.")
        models[name] = validate_catalog_patch(patch)
    return {'models': models}


def atomic_yaml(path: Path, data):
    path = Path(path)
    descriptor, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            yaml.safe_dump(data, handle, sort_keys=False); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def physical_memory_gb():
    try:
        if sys.platform == 'darwin':
            try:
                from Foundation import NSProcessInfo
                value=NSProcessInfo.processInfo().physicalMemory()
                if value>0: return value/GIB
            except ImportError: pass
            return int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], timeout=2,stderr=subprocess.DEVNULL).strip()) / GIB
        return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / GIB
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class Config:
    def __init__(self, path):
        self.path = private_directory(path)
        self.file = self.path / 'policy.yaml'; self.overrides = self.path / 'catalog.yaml'
        self.error = None; self.data = copy.deepcopy(DEFAULTS); self.catalog_data = {'models': {}}
        self.stamp = None; self.catalog_stamp = None; self._errors = {}
        if not self.file.exists():
            memory = physical_memory_gb()
            if memory is not None:
                self.data['budget_gb'] = max(4, min(96, math.floor(memory * .75)))
                self.data['headroom_gb'] = min(4, self.data['budget_gb'] / 4)
            else:
                self.data['budget_gb']=4; self.data['headroom_gb']=1
            atomic_yaml(self.file, self.data)
        if not self.overrides.exists(): atomic_yaml(self.overrides, self.catalog_data)
        self.reload()

    def reload(self):
        # Last valid values remain visible for repair, but admission fails closed.
        for path, stamp_name, data_name, validate in (
                (self.file, 'stamp', 'data', validate_policy),
                (self.overrides, 'catalog_stamp', 'catalog_data', validate_catalog)):
            try:
                private_file(path)
                info = path.stat(); stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
                if stamp != getattr(self, stamp_name):
                    candidate = validate(yaml.safe_load(read_private(path)))
                    setattr(self, data_name, candidate); setattr(self, stamp_name, stamp)
                self._errors.pop(path.name, None)
            except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError, Fault) as exc:
                self._errors[path.name] = str(exc)
        self.error = '; '.join(f'{name}: {error}' for name, error in self._errors.items()) or None
        return self.data

    def ensure_valid(self):
        self.reload()
        if self.error: raise Fault("Repair invalid configuration before routing requests: " + self.error, 'config_invalid', 503)
        return self.data

    def save(self, value):
        new = validate_policy(value); atomic_yaml(self.file, new); self.stamp = None; self.reload()

@dataclass
class Model:
    id: str
    name: str
    runtime: str
    handle: str
    size_gb: float = 0
    context_max: int = 0
    capabilities: list = field(default_factory=lambda:['chat'])
    tools_verified: bool = False
    tier: str = 'on_demand'
    digest: str | None = None
    quant: str = 'Unknown'
    format: str = 'gguf'
    family: str = 'Unknown'
    parameters: str = 'Unknown'
    installation_verified: bool = True
    kv_gb_per_1k: float = 1.0
    speed_tps: float | None = None
    task_scores: dict = field(default_factory=dict)
    loaded: bool = False
    loaded_context: int = 0
    instance: str | None = None
    memory_gb: float = 0
    active: int = 0
    last_used: float = 0
    load_seconds: float = 0
    managed: bool = False
    available: bool = True
    state_uncertain: bool = False
    instance_count: int = 0
    def footprint(self,context):
        return round((self.size_gb*1.1 + self.kv_gb_per_1k*context/1024 + 0.5)*1.15,3)
    def public(self): return asdict(self)

class Journal:
    def __init__(self,path):
        path=Path(path).parent.resolve()/Path(path).name
        for suffix in ('', '-wal', '-shm', '-journal'): private_file(str(path)+suffix, allow_missing=True)
        if not path.exists():
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600); os.close(fd)
        if tuple(int(x) for x in apsw.sqlitelibversion().split('.')) < (3,54,0):
            raise ValueError('Loqalyn requires SQLite 3.54.0 or newer through APSW.')
        self.db=apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READWRITE|apsw.SQLITE_OPEN_NOFOLLOW)
        self.db.enable_load_extension(False)
        self.db.config(apsw.SQLITE_DBCONFIG_DEFENSIVE,True)
        self.db.config(apsw.SQLITE_DBCONFIG_TRUSTED_SCHEMA,False)
        self.db.execute('PRAGMA trusted_schema=OFF')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, created REAL, data TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, created REAL, kind TEXT, data TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS decisions_created ON decisions(created)')
        self.db.execute('CREATE INDEX IF NOT EXISTS events_created ON events(created)')
        self.writes=0; self.retention_days=30; self.last_prune=0
        # Existing databases may be user-edited: reject executable schema objects.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') OR lower(sql) LIKE '%virtual table%' LIMIT 1").fetchone():
            self.db.close(); raise ValueError('Unexpected executable database schema.')
    def decision(self,data):
        # Never persist prompts, messages, tool definitions, or model output.
        if self.retention_days == 0: return
        self.db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?)',(data['id'],data['created'],json.dumps(data)))
        self._periodic_retention()
    def event(self,kind,data):
        if self.retention_days == 0: return
        self.db.execute('INSERT INTO events(created,kind,data) VALUES(?,?,?)',(time.time(),kind,json.dumps(data)))
        self._periodic_retention()
    def _periodic_retention(self):
        self.writes+=1
        if self.writes%100==0:
            self.prune(self.retention_days)
            self._bounded_retention('decisions',10000); self._bounded_retention('events',10000)
    def _bounded_retention(self,table,limit):
        self.db.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY created DESC LIMIT -1 OFFSET ?)',(limit,))
        self.db.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT record_id FROM (SELECT rowid AS record_id, SUM(length(CAST(data AS BLOB))) OVER (ORDER BY created DESC, rowid DESC) AS bytes FROM {table}) WHERE bytes > ?)',(32*1024*1024,))
    def recent(self,limit=200):
        result=[]; size=2
        for (data,) in self.db.execute('SELECT data FROM decisions ORDER BY created DESC LIMIT ?',(limit,)):
            size+=len(data.encode('utf-8'))+2
            if size>2*1024*1024: break
            result.append(json.loads(data))
        return result
    def get(self,id):
        row=self.db.execute('SELECT data FROM decisions WHERE id=?',(id,)).fetchone(); return json.loads(row[0]) if row else None
    def prune(self,days):
        self.retention_days=days; self.last_prune=time.monotonic()
        cutoff=time.time()-days*86400
        for table in ('decisions','events'):
            if days == 0: self.db.execute(f'DELETE FROM {table}')
            else: self.db.execute(f'DELETE FROM {table} WHERE created < ?',(cutoff,))

    def maintain(self, days):
        if days != self.retention_days or time.monotonic()-self.last_prune >= 60:
            self.prune(days)


def _schema(value):
    if not isinstance(value, dict): raise Fault("JSON schema must be an object.")
    try:
        schema_subset(value)
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(value)
    except Exception as exc:
        raise Fault("Invalid JSON schema.") from exc


def inspect_payload(body, policy):
    if not isinstance(body, dict): raise Fault("Request must be a JSON object.")
    try:
        bounded_tree(body)
        if len(json.dumps(body, allow_nan=False).encode()) > MAX_BODY: raise ValueError('Request exceeds 2 MiB.')
    except (ValueError, TypeError, RecursionError) as exc: raise Fault(str(exc))
    allowed = {'model', 'messages', 'max_tokens', 'max_completion_tokens', 'stream', 'n', 'tools', 'tool_choice',
               'metadata', 'response_format', 'temperature', 'top_p', 'seed', 'stop', 'stream_options'}
    if set(body) - allowed: raise Fault("Unsupported request field: " + ', '.join(sorted(str(x) for x in set(body) - allowed)), 'unsupported_feature')
    if not isinstance(body.get('model', 'auto'), str) or not body.get('model', 'auto').strip() or len(body.get('model','auto'))>512: raise Fault("model must be a non-empty string of at most 512 characters.")
    if 'metadata' in body and (not isinstance(body['metadata'], dict) or not all(isinstance(k, str) for k in body['metadata'])):
        raise Fault("metadata must be an object.")
    if 'private' in body.get('metadata', {}) and not isinstance(body['metadata']['private'], bool): raise Fault("metadata.private must be boolean.")
    max_tokens = body.get('max_tokens', body.get('max_completion_tokens', policy['default_max_tokens']))
    if not _number(max_tokens, minimum=1, maximum=131072, integer=True): raise Fault("max_tokens must be an integer between 1 and 131072.")
    if 'max_tokens' in body and 'max_completion_tokens' in body and body['max_tokens'] != body['max_completion_tokens']:
        raise Fault("Specify one consistent completion-token limit.")
    if not isinstance(body.get('stream', False), bool): raise Fault("stream must be boolean.")
    if type(body.get('n', 1)) is not int or body.get('n', 1) != 1: raise Fault("Only n=1 is supported.")
    for key, maximum in (('temperature', 2), ('top_p', 1)):
        if key in body and not _number(body[key], maximum=maximum): raise Fault(f"{key} must be a finite number between 0 and {maximum}.")
    if 'seed' in body and not _number(body['seed'], minimum=-(2**63), maximum=2**63-1, integer=True): raise Fault("seed must be an integer.")
    if 'stop' in body and body['stop'] is not None:
        stop = body['stop']
        if not isinstance(stop, str) and (not isinstance(stop, list) or any(not isinstance(x, str) for x in stop)):
            raise Fault("stop must be a string or string list.")
    if 'stream_options' in body:
        options = body['stream_options']
        if not isinstance(options, dict) or set(options) - {'include_usage'} or not isinstance(options.get('include_usage', True), bool):
            raise Fault("Only boolean stream_options.include_usage is supported.")
    if body.get('tool_choice', 'auto') not in ('auto', 'none'): raise Fault("Only automatic or disabled tool choice is supported.", 'unsupported_feature')
    tools = body.get('tools', [])
    if not isinstance(tools, list): raise Fault("tools must be a list.")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') != 'function' or not isinstance(tool.get('function'), dict):
            raise Fault("Only local function tools are supported; hosted tools are disabled.", 'unsupported_feature')
        fn = tool['function']
        if set(tool) - {'type', 'function'} or set(fn) - {'name', 'description', 'parameters', 'strict'}:
            raise Fault("Unsupported function-tool field.", 'unsupported_feature')
        if not isinstance(fn.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fn['name']): raise Fault("Invalid function name.")
        if 'description' in fn and not isinstance(fn['description'], str): raise Fault("Tool description must be text.")
        if 'strict' in fn and not isinstance(fn['strict'], bool): raise Fault("Tool strict must be boolean.")
        if fn.get('strict'): raise Fault("Strict tool argument enforcement is not supported by this gateway.", 'unsupported_feature')
        _schema(fn.get('parameters', {}))
    fmt = body.get('response_format', {})
    if not isinstance(fmt, dict) or set(fmt) - {'type', 'json_schema'} or fmt.get('type', 'text') not in ('text', 'json_object', 'json_schema'):
        raise Fault("Unsupported response_format.", 'unsupported_feature')
    if fmt.get('type') == 'json_schema':
        schema = fmt.get('json_schema')
        if not isinstance(schema, dict) or set(schema) - {'name', 'schema', 'strict', 'description'}: raise Fault("Invalid response JSON schema.")
        if 'strict' in schema and not isinstance(schema['strict'], bool): raise Fault("Schema strict must be boolean.")
        _schema(schema.get('schema'))
    messages = body.get('messages')
    if not isinstance(messages, list) or not messages: raise Fault("messages must be a non-empty list of valid chat messages.")
    tool_history = {}; tool_results = set(); has_tools = bool(tools)
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool'):
            raise Fault("messages must contain valid chat messages.")
        if set(message) - {'role', 'content', 'tool_calls', 'tool_call_id', 'name', 'reasoning_content'}:
            raise Fault("Unsupported message field.", 'unsupported_feature')
        content = message.get('content')
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict): raise Fault("Content blocks must be objects.")
                if block.get('type') in ('image_url', 'image'):
                    source = block.get('source') or {}; source = source if isinstance(source, dict) else {}
                    url = block.get('image_url') or {}; url = url.get('url', '') if isinstance(url, dict) else url
                    if (isinstance(url, str) and url and not url.startswith('data:image/')) or source.get('type') == 'url':
                        raise Fault("Remote image URLs are disabled; send a base64 image.", 'remote_media_blocked')
                    raise Fault("Image inputs are disabled until image-token accounting is verified on your runtime.", 'vision_not_verified', 422)
                if block.get('type') != 'text' or not isinstance(block.get('text'), str) or set(block) - {'type', 'text'}:
                    raise Fault("Only text content blocks are supported.", 'unsupported_feature')
        elif not isinstance(content, str) and not (content is None and message['role'] == 'assistant' and message.get('tool_calls')):
            raise Fault("Message content must be text or text blocks.")
        for key in ('name', 'reasoning_content'):
            if key in message and not isinstance(message[key], str): raise Fault(f"Message {key} must be text.")
        if 'tool_calls' in message:
            calls = message['tool_calls']
            if message['role'] != 'assistant' or not isinstance(calls, list): raise Fault("tool_calls must be an assistant list.")
            has_tools = has_tools or bool(calls)
            for call in calls:
                if not isinstance(call, dict) or call.get('type') != 'function' or not isinstance(call.get('function'), dict): raise Fault("Invalid tool call.")
                fn = call['function']; ident = call.get('id')
                if not isinstance(ident, str) or not ident or ident in tool_history: raise Fault("Tool call IDs must be non-empty and unique.")
                if not isinstance(fn.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', fn['name']) or not isinstance(fn.get('arguments'), str): raise Fault("Invalid tool call function.")
                try: arguments = loads(fn['arguments'])
                except (TypeError, ValueError): raise Fault("Tool arguments must contain a JSON object.")
                if not isinstance(arguments, dict): raise Fault("Tool arguments must contain a JSON object.")
                tool_history[ident] = fn['name']
        if message['role'] == 'tool':
            ident = message.get('tool_call_id')
            if not isinstance(ident, str) or ident not in tool_history or ident in tool_results: raise Fault("Tool result must reference an earlier, unanswered tool call.")
            tool_results.add(ident); has_tools = True
        elif 'tool_call_id' in message: raise Fault("tool_call_id is only valid for tool results.")
    try: text = json.dumps({k: body[k] for k in ('messages', 'tools') if k in body}, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError): raise Fault("Request fields must contain finite JSON values.")
    # A byte-count reserve is a heuristic, not a model tokenizer or a RAM guarantee.
    input_estimate = len(text.encode('utf-8')) + 32 * len(messages) + 128
    context = math.ceil((input_estimate + max_tokens) / 1024) * 1024
    if set(tool_history) != tool_results: raise Fault('Every tool call requires a matching tool result before generation.')
    tags = privacy_tags(text, policy, body.get('metadata', {}).get('private', False))
    return {"text": text, "context": context, "input_estimate": input_estimate, "max_tokens": max_tokens, "tools": has_tools, "images": 0, "tags": tags}


def route(body,models,policy):
    start=time.perf_counter(); features=inspect_payload(body,policy); text=features['text'].lower()
    virtual=body.get('model','auto'); enabled=[x for x in policy['routes'] if x.get('enabled',True)]
    chosen_route=next(x for x in enabled if x['name']=='quick_chat'); hits=0
    for r in enabled:
        n=sum(sum(1 for _ in re.finditer(r'\b'+re.escape(k.lower())+r'\b',text)) for k in r.get('keywords',[]))
        if n>hits: chosen_route=r; hits=n
    if virtual=='auto-code': chosen_route=next((r for r in enabled if r['name']=='code_edit'),chosen_route)
    if features['context']>32768: chosen_route=next((r for r in enabled if r['name']=='long_document'),chosen_route)
    exclusions=[]; candidates=[]; all_models=list(models.values()); explicit=virtual not in ('auto','auto-fast','auto-code')
    budget=policy['budget_gb']-policy['headroom_gb']
    protected={m.id:m.memory_gb for m in all_models if m.loaded and (m.active or m.tier=='resident' or not m.managed)}
    protected_total=sum(protected.values())
    for m in all_models:
        reasons=[]
        if not m.available: reasons.append('runtime_offline')
        if not m.installation_verified: reasons.append('installation_unverified')
        if m.state_uncertain: reasons.append('runtime_state_unknown')
        if 'chat' not in m.capabilities: reasons.append('not_chat_model')
        if not explicit and m.tier=='archive': reasons.append('archived')
        if m.loaded and not m.managed and m.loaded_context<features['context']: reasons.append('external_context')
        if explicit and virtual not in (m.id,m.handle): reasons.append('explicit_model')
        if features['tools'] and not m.tools_verified: reasons.append('tools_unverified')
        if not m.context_max or features['context']>m.context_max: reasons.append('context_limit')
        if m.size_gb<=0: reasons.append('memory_unknown')
        memory=max(m.memory_gb,m.footprint(m.loaded_context)) if m.loaded and m.loaded_context>=features['context'] else m.footprint(features['context'])
        locked=protected_total-protected.get(m.id,0)
        if memory+locked>budget: reasons.append('memory_budget')
        if reasons:
            exclusions.append({'id':m.id,'reasons':reasons,'memory_gb':memory,'available_gb':round(max(0,budget-locked),3)})
            continue
        prefer=chosen_route.get('prefer',[])
        preference=(len(prefer)-prefer.index(m.id))*15 if m.id in prefer else (len(prefer)-prefer.index(m.handle))*15 if m.handle in prefer else 0
        quality=m.task_scores.get(chosen_route['name'])
        # Capability priors are uncalibrated rules, not measured benchmark scores.
        prior=15 if chosen_route['name']=='code_edit' and 'code' in m.capabilities else 10 if chosen_route['name']=='hard_reasoning' and 'reasoning' in m.capabilities else 0
        score=(float(quality)*100 if quality is not None else 50)+prior+preference+(8 if m.loaded else 0)-memory*(1.5 if virtual=='auto-fast' or chosen_route['name']=='quick_chat' else .15)-(0 if m.loaded else min(10,m.load_seconds/2))
        candidates.append({'id':m.id,'score':round(score,2),'memory_gb':memory,'components':{
            'task_quality':float(quality)*100 if quality is not None else 50,'capability_prior':prior,
            'preference':preference,'warm_bonus':8 if m.loaded else 0,
            'memory_penalty':round(-memory*(1.5 if virtual=='auto-fast' or chosen_route['name']=='quick_chat' else .15),3),
            'load_penalty':0 if m.loaded else -min(10,m.load_seconds/2)}})
    candidates.sort(key=lambda x:x['score'],reverse=True)
    flag=None
    if not candidates: flag='context_exceeds_local' if all_models and all(features['context']>m.context_max for m in all_models) else 'no_eligible_model'
    elif chosen_route.get('escalate')=='suggest': flag='policy_frontier_route'
    if policy['escalation_mode']=='off' and candidates: flag=None
    selected=models[candidates[0]['id']] if candidates else None
    return {"id":'req_'+uuid.uuid4().hex[:16],"created":time.time(),"route":chosen_route['name'],"requested_model":virtual,"policy_fingerprint":hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest(),"model":selected.id if selected else None,"model_name":selected.name if selected else None,"runtime":selected.runtime if selected else None,"candidates":candidates,"exclusions":exclusions,"confidence":None,"classifier":"deterministic_rules","escalation":flag,"privacy_tags":features['tags'],"context":features['context'],"input_estimate":features['input_estimate'],"max_tokens":features['max_tokens'],"warm":bool(selected and selected.loaded),"explicit":explicit,"status":"routed" if selected else "blocked","duration_ms":round((time.perf_counter()-start)*1000,2)}

class Ledger:
    """Serialize each model and hold admission reservations across runtime operations.

    Estimates govern admission, not the runtime's allocator. On an ambiguous failure,
    keep a reservation until the operator restarts/reconciles the affected runtime.
    """
    def __init__(self, models, adapters, config, journal):
        self.models = models; self.adapters = adapters; self.config = config; self.journal = journal
        self.lock = asyncio.Lock(); self.model_locks = {}; self.uncertain_runtimes = set()
        self._leases = {}; self._context_leases = contextvars.ContextVar('loqalyn_leases', default=None)
        self.revisions = {}

    def changed(self, runtime):
        self.revisions[runtime] = self.revisions.get(runtime, 0) + 1

    def _policy(self):
        return self.config.ensure_valid() if hasattr(self.config, 'ensure_valid') else self.config.data

    def _uncertain(self, model, reserve):
        self.changed(model.runtime)
        model.loaded = True; model.memory_gb = max(model.memory_gb, reserve)
        model.state_uncertain = True; model.available = False
        self.uncertain_runtimes.add(model.runtime)

    async def acquire(self, id, context, *, tools_required=False):
        policy = self._policy()
        if not _number(context, minimum=1, integer=True): raise Fault("Context must be a positive integer.")
        sem = self.model_locks.setdefault(id, asyncio.Lock())
        try: await asyncio.wait_for(sem.acquire(), policy['queue_timeout'])
        except asyncio.TimeoutError: raise Fault("Model is busy; retry shortly.", 'queue_timeout', 503)
        held = False
        try:
            try: await asyncio.wait_for(self.lock.acquire(), policy['queue_timeout']); held = True
            except asyncio.TimeoutError: raise Fault("Memory admission is busy; retry shortly.", 'queue_timeout', 503)
            policy = self._policy()
            if not policy.get('runtime_local_only_confirmed'):
                raise Fault('Confirm local-only runtime setup before loading.', 'runtime_locality_unconfirmed', 403)
            if self.uncertain_runtimes or any(x.state_uncertain for x in self.models.values()):
                raise Fault("Runtime state is uncertain after an interrupted operation. Restart that runtime and explicitly recover it.", 'runtime_state_unknown', 503)
            m = self.models.get(id)
            if not m: raise Fault("Model disappeared from the catalog.", 'model_missing', 409)
            self.changed(m.runtime)
            if not m.available or m.runtime not in self.adapters: raise Fault("Runtime is offline.", 'runtime_offline', 503)
            if not m.installation_verified: raise Fault("This installation has not passed verification. Retry its download in Discover.", 'installation_unverified', 409)
            if tools_required and not m.tools_verified: raise Fault('Tool verification changed while queued.', 'tools_unverified', 422)
            if 'chat' not in m.capabilities or not _number(m.size_gb, minimum=.000001) or not _number(m.kv_gb_per_1k, minimum=.000001):
                raise Fault("Model metadata is insufficient for memory admission.", 'memory_unknown', 422)
            if not _number(m.context_max, minimum=context, integer=True): raise Fault("Context exceeds the model limit.", 'context_limit', 422)
            limit = policy['budget_gb'] - policy['headroom_gb']
            if not m.loaded or m.loaded_context < context:
                if m.loaded and (not m.managed or m.instance_count > 1):
                    raise Fault("This externally loaded instance has insufficient context. Unload it in its runtime first.", 'external_instance', 409)
                required = m.footprint(context)
                twins = [x for x in self.models.values() if x.loaded and x.id != id and m.digest and x.digest == m.digest]
                if any(x.active or not x.managed or x.tier == 'resident' or x.instance_count > 1 for x in twins):
                    raise Fault("These weights are already in use in another runtime.", 'duplicate_weights', 409)
                victims = sorted([x for x in self.models.values() if x.loaded and x.id != id and not x.active
                                  and x.tier != 'resident' and x.managed and x.instance_count <= 1], key=lambda x: x.last_used + x.load_seconds)
                used = sum(x.memory_gb for x in self.models.values() if x.loaded and x.id != id)
                if required + used - sum(x.memory_gb for x in victims) > limit:
                    raise Fault("Insufficient unreserved memory. Active, resident and external models are protected.", 'memory_budget', 503)
                # Check feasibility before any eviction. Duplicate files cannot coexist.
                for x in twins:
                    used -= x.memory_gb; await self._unload(x)
                for x in victims:
                    if not x.loaded: continue
                    if required + used <= limit: break
                    used -= x.memory_gb; await self._unload(x)
                if m.loaded: await self._unload(m)
                started = time.perf_counter(); m.memory_gb = required
                try:
                    info = await asyncio.wait_for(self.adapters[m.runtime].load(m, context), policy['load_timeout'])
                except BaseException:
                    self._uncertain(m, required)
                    raise
                m.instance = info.get('instance_id', m.handle); m.loaded = True; m.managed = True; m.instance_count = 1
                actual_context = (info.get('load_config') or {}).get('context_length', context)
                if not _number(actual_context, minimum=1, integer=True):
                    self._uncertain(m, required); raise Fault("Runtime returned invalid loaded context.", 'runtime_error', 502)
                m.loaded_context = actual_context; m.memory_gb = max(required, m.footprint(actual_context))
                m.load_seconds = time.perf_counter() - started
                self.journal.event('load', {'model': id, 'context': actual_context, 'reserved_gb': m.memory_gb})
                if actual_context < context: raise Fault("Runtime loaded less context than requested.", 'context_limit', 502)
            used = sum(x.memory_gb for x in self.models.values() if x.loaded)
            if used > limit: raise Fault("Estimated/observed model memory exceeds the admission budget.", 'memory_pressure', 503)
            m.active += 1; m.last_used = time.time()
            lease = uuid.uuid4().hex; self._leases[id] = lease
            leases = dict(self._context_leases.get() or {}); leases[id] = lease; self._context_leases.set(leases)
            return m
        except BaseException:
            sem.release(); raise
        finally:
            if held: self.lock.release()

    async def release(self, id):
        # shield() creates a child task but propagates ContextVars. A stale/double
        # release must never unlock a later request's semaphore.
        lease = (self._context_leases.get() or {}).get(id)
        async with self.lock:
            if lease is None or self._leases.get(id) != lease: return
            self._leases.pop(id, None)
            m = self.models.get(id)
            if m:
                m.active = max(0, m.active - 1); m.last_used = time.time(); self.changed(m.runtime)
            self.model_locks[id].release()

    async def _unload(self, m):
        if m.active: raise Fault("Cannot unload a model with an active request.", 'model_busy', 409)
        if not m.managed or m.instance_count > 1:
            raise Fault("Unload externally managed instances in their runtime.", 'external_instance', 409)
        self.changed(m.runtime)
        try:
            await asyncio.wait_for(self.adapters[m.runtime].unload(m), self.config.data['load_timeout'])
        except BaseException:
            self._uncertain(m, m.memory_gb); raise
        m.loaded = False; m.memory_gb = 0; m.loaded_context = 0; m.instance = None; m.managed = False; m.instance_count = 0
        self.journal.event('unload', {'model': m.id})

    async def unload(self, id):
        async with self.lock:
            m = self.models.get(id)
            if not m: raise Fault("Model not found.", 'not_found', 404)
            if m.state_uncertain: raise Fault("Restart the affected runtime and explicitly recover it.", 'runtime_state_unknown', 503)
            if not m.managed and m.loaded: raise Fault("Unload externally managed models in their runtime.", 'external_instance', 409)
            if m.tier == 'resident': raise Fault("Change the resident tier before unloading.", 'resident_model', 409)
            if m.loaded: await self._unload(m)

    async def confirm_reconciled(self, runtime):
        """Call only after explicit operator recovery and successful fresh discovery.

        Caller must replace uncertain entries with that discovery under this lock,
        then invoke this method outside the lock. Polling alone must not call it.
        """
        async with self.lock:
            if any(m.active for m in self.models.values() if m.runtime == runtime): raise Fault("Runtime still has active requests.", 'model_busy', 409)
            if any(m.state_uncertain for m in self.models.values() if m.runtime == runtime):
                raise Fault("Fresh runtime inventory is required before recovery.", 'runtime_state_unknown', 503)
            self.uncertain_runtimes.discard(runtime)
