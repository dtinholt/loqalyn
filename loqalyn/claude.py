"""Local Claude Code profile and durable, metadata-only task model locks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time

from .core import Fault, inspect_payload, route
from .security import api_token, private_directory

CONTEXT = 65536
CLIENT_CONTEXT = CONTEXT
MAX_TASKS = 16384
MIN_CLAUDE = (2, 1, 299)
LOCAL_TOOLS = 'Read,Edit,Write,Bash,Glob,Grep'


def task_identity(headers, *, required=False):
    """Use opaque client IDs, never prompt text, as task identity.

    Older clients without prompt hints stay pinned for their whole session.
    Auxiliary calls are isolated; compaction stays with the main task model.
    """
    session=headers.get('x-claude-code-session-id')
    if not session:
        if required: raise Fault('Launch Claude Code from Loqalyn; a session identifier is required.', 'task_identity_required', 400)
        return None
    agent=headers.get('x-claude-code-agent-id','main')
    prompt=headers.get('x-claude-code-prompt-id','session')
    role=headers.get('x-claude-code-request-class','main')
    for value in (session,agent,prompt,role):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value):
            raise Fault('Invalid Claude Code task identifier.', 'invalid_task_identity')
    lane='auxiliary' if role=='auxiliary' else 'task'
    key=hashlib.sha256(json.dumps([session,agent,prompt,lane]).encode()).hexdigest()
    return {'key':key,'session':hashlib.sha256(json.dumps([session,agent]).encode()).hexdigest(),
            'role':role,'mode':'task' if prompt!='session' else 'session'}


class TaskPins:
    def __init__(self, db):
        self.db=db
        db.execute('CREATE TABLE IF NOT EXISTS claude_tasks (key TEXT PRIMARY KEY, session TEXT, model TEXT, revision TEXT, runtime_url TEXT, created REAL, updated REAL, requests INTEGER, role TEXT, mode TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS claude_tasks_updated ON claude_tasks(updated)')
        db.execute('CREATE TABLE IF NOT EXISTS claude_verifications (model TEXT PRIMARY KEY, revision TEXT, updated REAL)')

    def verified_models(self):
        return dict(self.db.execute('SELECT model,revision FROM claude_verifications'))

    def verify_model(self,model,revision):
        if revision is None:
            self.db.execute('DELETE FROM claude_verifications WHERE model=?',(model,)); return
        if not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{64}',revision): raise Fault('Invalid model revision.')
        if self.db.execute('SELECT count(*) FROM claude_verifications').fetchone()[0]>=4096 and model not in self.verified_models():
            raise Fault('The model-verification limit has been reached.', 'verification_capacity',503)
        self.db.execute('INSERT OR REPLACE INTO claude_verifications VALUES(?,?,?)',(model,revision,time.time()))

    def recent(self):
        return [dict(zip(('task','model','updated','requests','role','mode'),row))
                for row in self.db.execute('SELECT substr(key,1,12),model,updated,requests,role,mode FROM claude_tasks ORDER BY updated DESC LIMIT 30')]

    def decide(self, identity, body, models, policy, adapters):
        feature=inspect_payload(body,policy)
        if feature['context']>CONTEXT:
            raise Fault('prompt is too long: local coding safety reserve exceeded. Compact the conversation or start a smaller task.', 'context_limit', 400)
        key=identity['key']; now=time.time()
        # Claude Code's compaction calls can omit the prompt hint. They belong
        # to the current task of this specific agent, not a new model selection.
        if identity['role']=='compaction' and identity['mode']=='session':
            previous=self.db.execute("SELECT key,mode FROM claude_tasks WHERE session=? AND role!='auxiliary' ORDER BY updated DESC LIMIT 1",(identity['session'],)).fetchone()
            if previous:
                key,mode=previous; identity={**identity,'key':key,'mode':mode}
            else:
                raise Fault('No task model is available for compaction. Start a new coding task.', 'task_identity_required',409)
        row=self.db.execute('SELECT model,revision,runtime_url FROM claude_tasks WHERE key=?',(key,)).fetchone()
        requested=body.get('model','auto-code')
        if requested not in ('auto','auto-code','auto-fast'):
            raise Fault('The Claude Code profile uses automatic local selection. Start a new task through Loqalyn.', 'invalid_coding_model', 400)
        # Every coding candidate must have verified tools and sufficient room for
        # the whole task, even if its initial request does not advertise tools.
        eligible={id:m for id,m in models.items() if m.runtime=='ollama' and m.tools_verified
                  and m.revision and m.tools_verified_revision==m.revision and m.tier!='archive' and m.context_max>=CONTEXT}
        prompt=' '.join(str(m.get('content','')) for m in body['messages'] if m.get('role')=='user')[-8192:].lower()
        hint='quick_chat' if requested=='auto-fast' or identity['role']=='auxiliary' else (
            'hard_reasoning' if re.search(r'\b(architecture|tradeoffs|trade-offs|reasoning|design review|security review)\b',prompt) else 'code_edit')
        if row:
            model,revision,url=row; current=models.get(model)
            if (not current or current.revision!=revision or current.runtime not in adapters
                    or adapters[current.runtime].url!=url):
                raise Fault('This task’s model changed or disappeared. Restore it or start a new task; Loqalyn will not switch models mid-task.', 'task_model_changed', 409)
            pinned={**body,'model':model}
            decision=route(pinned,models,policy,context_floor=CONTEXT,route_hint=hint,eligible_ids=eligible)
            if not decision['model']:
                raise Fault('This task’s model is temporarily unavailable or no longer eligible. Restore it and retry, or start a new task.', 'task_model_unavailable', 409)
            self.db.execute('UPDATE claude_tasks SET updated=?, requests=requests+1 WHERE key=?',(now,key))
            reused=True
        else:
            if self.db.execute('SELECT count(*) FROM claude_tasks').fetchone()[0]>=MAX_TASKS:
                raise Fault('The saved task limit has been reached. Existing tasks can continue; reset the coding connection in Loqalyn before starting more.', 'task_capacity', 503)
            decision=route(body,models,policy,context_floor=CONTEXT,route_hint=hint,eligible_ids=eligible)
            if not decision['model']:
                raise Fault('No coding model is ready. In Claude Code setup, verify an Ollama model with at least 64K context and enough available memory.', 'no_coding_model', 422)
            model=models[decision['model']]
            self.db.execute('INSERT INTO claude_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,identity['session'],model.id,model.revision,adapters[model.runtime].url,now,now,1,identity['role'],identity['mode']))
            reused=False
        decision.update(requested_model=requested,client='claude_code',task=key[:12],task_mode=identity['mode'],
                        task_reused=reused,escalation=None,selection_note='Same model for this task' if reused else 'Selected for a new local coding task')
        return decision


def coding_status(models, policy, cloud_disabled=None):
    protected=sum(m.memory_gb for m in models.values() if m.loaded and (m.active or m.tier=='resident' or not m.managed))
    candidates=[]
    for m in models.values():
        if m.runtime!='ollama' or 'tools' not in m.capabilities: continue
        reasons=[]
        if not policy.get('runtime_local_only_confirmed'): reasons.append('Confirm local setup in Settings')
        if cloud_disabled is False: reasons.append('Disable Ollama cloud access')
        if not m.available or m.state_uncertain: reasons.append('Runtime unavailable')
        if not m.installation_verified or not m.revision: reasons.append('Installation not verified')
        if m.context_max<CONTEXT: reasons.append('Needs at least 64K context')
        if m.tier=='archive': reasons.append('Model is archived')
        if not m.tools_verified or m.tools_verified_revision!=m.revision: reasons.append('Run the tool check')
        if m.loaded and not m.managed and m.loaded_context<CONTEXT: reasons.append('Unload this model in Ollama first')
        locked=protected-(m.memory_gb if m.loaded and (m.active or m.tier=='resident' or not m.managed) else 0)
        memory=max(m.memory_gb,m.footprint(CONTEXT)) if m.loaded else m.footprint(CONTEXT)
        if memory+locked>policy['budget_gb']-policy['headroom_gb']: reasons.append('Needs more available memory')
        candidates.append({'id':m.id,'name':m.name,'ready':not reasons,'reasons':reasons,'memory_gb':round(memory,2),
                           'can_verify':all(r=='Run the tool check' for r in reasons)})
    return {'context':CONTEXT,'client_context':CLIENT_CONTEXT,'models':candidates,'ready':any(m['ready'] for m in candidates)}


def find_claude():
    for path in (Path.home()/'.local/bin/claude',Path('/opt/homebrew/bin/claude'),Path('/usr/local/bin/claude')):
        try:
            resolved=path.resolve(strict=True); info=resolved.stat()
            if stat.S_ISREG(info.st_mode) and info.st_uid in (0,os.getuid()) and not info.st_mode & 0o022 and os.access(resolved,os.X_OK):
                return str(resolved)
        except OSError: pass
    return None


def client_info():
    binary=find_claude()
    if not binary: return {'installed':False,'compatible':False,'message':'Install Claude Code to connect it to Loqalyn.'}
    try:
        result=subprocess.run([binary,'--version'],capture_output=True,text=True,timeout=10,check=True)
        version=re.search(r'\b(\d+)\.(\d+)\.(\d+)\b',result.stdout[:1024])
        compatible=bool(version and tuple(map(int,version.groups()))>=MIN_CLAUDE)
        return {'installed':True,'compatible':compatible,'version':version.group(0) if version else 'Unknown',
                'message':'Claude Code is ready.' if compatible else 'Update Claude Code to 2.1.299 or newer.'}
    except (OSError,subprocess.SubprocessError):
        return {'installed':True,'compatible':False,'message':'Claude Code could not start. Check its installation.'}


def launch_environment(directory,port,*,environ=None):
    source=os.environ if environ is None else environ
    env={k:v for k,v in source.items() if k in ('HOME','USER','LOGNAME','SHELL','PATH','LANG','LC_ALL','LC_CTYPE','TERM','COLORTERM','TERM_PROGRAM','TMPDIR','SSH_AUTH_SOCK')}
    config=private_directory(Path(directory)/'claude-code')
    token=api_token(Path(directory)/'claude-token')
    env.update(CLAUDE_CONFIG_DIR=str(config),ANTHROPIC_BASE_URL=f'http://127.0.0.1:{port}',
        ANTHROPIC_API_KEY=token,ANTHROPIC_AUTH_TOKEN='',ANTHROPIC_MODEL='auto-code',
        ANTHROPIC_DEFAULT_SONNET_MODEL='auto-code',ANTHROPIC_DEFAULT_OPUS_MODEL='auto-code',
        ANTHROPIC_DEFAULT_FABLE_MODEL='auto-code',ANTHROPIC_DEFAULT_HAIKU_MODEL='auto-fast',
        ANTHROPIC_CUSTOM_MODEL_OPTION='auto-code',ANTHROPIC_CUSTOM_MODEL_OPTION_NAME='Loqalyn · automatic local',
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS='1',
        CLAUDE_CODE_GATEWAY_HINT_HEADERS='1',CLAUDE_CODE_MAX_OUTPUT_TOKENS='4096',MAX_THINKING_TOKENS='0',
        CLAUDE_CODE_MAX_CONTEXT_TOKENS=str(CLIENT_CONTEXT),CLAUDE_CODE_DISABLE_1M_CONTEXT='1',
        DISABLE_PROMPT_CACHING='1',API_TIMEOUT_MS='650000')
    return env


def launch_arguments(binary):
    return [binary,'--bare','--setting-sources','','--strict-mcp-config','--no-chrome',
            '--model','auto-code','--tools',LOCAL_TOOLS,'--permission-mode','manual',
            '--append-system-prompt','You are a local coding assistant. Read relevant project instructions and files before editing. Make focused changes and run relevant tests.']


def write_launcher(directory,port,project,executable):
    """The Terminal launcher contains paths only; the child reads its scoped token."""
    directory=private_directory(directory); project=Path(project).resolve(strict=True)
    if not project.is_dir(): raise ValueError('Choose a project folder.')
    args=[str(executable),'--cli','--port',str(port),'--data-dir',str(directory),'claude',str(project)]
    script='#!/bin/sh\nexec '+shlex.join(args)+'\n'
    import tempfile
    fd,path=tempfile.mkstemp(prefix='Open Claude Code-',suffix='.command',dir=directory)
    with os.fdopen(fd,'w') as handle: handle.write(script)
    os.chmod(path,0o700)
    return Path(path)
