from __future__ import annotations
import asyncio, contextlib, copy, hashlib, hmac, json, os, resource, secrets, sys, time
from pathlib import Path
from urllib.parse import urlparse
import aiohttp, yaml
from aiohttp import web
from . import __version__
from .core import Config, Journal, Ledger, Fault, Model, route, inspect_payload, atomic_yaml, validate_catalog_patch, validate_policy
from .adapters import Adapter, anthropic_request, anthropic_response, complete
from .security import api_token, loads, MAX_BODY, MAX_RESPONSE, read_response, privacy_tags

class Service:
    def __init__(self,path):
        self.config=Config(path); self.models={}; self.adapters={}; self.health={}; self.last_refresh=0
        self.journal=Journal(self.config.path/'decisions.sqlite3'); self.journal.prune(self.config.data['log_retention_days'])
        try: self.token=api_token(self.config.path/'token')
        except BaseException:
            self.journal.db.close(); raise
        self.ledger=Ledger(self.models,self.adapters,self.config,self.journal)
        self.pending={}; self.session=None; self.refresh_lock=asyncio.Lock(); self.jobs={}; self.started=time.time()
        self.inflight={'inference':0,'control':0}; self.generation_slots=asyncio.Semaphore(8)
        self.metrics={'accepted':0,'overloaded':0,'completed':0,'failed':0,'cancelled':0}
        self.resolved={}
    def resolve(self,id):
        self.resolved[id]=time.time()
        while len(self.resolved)>1024: self.resolved.pop(next(iter(self.resolved)))
    async def start(self):
        try:
            self.session=aiohttp.ClientSession(trust_env=False)
            self.configure_adapters(); await self.refresh(); self.task=asyncio.create_task(self.poll())
        except BaseException:
            # aiohttp does not dispatch normal application cleanup if startup
            # itself was interrupted. Release the partial session and journal.
            await self.stop()
            raise
    def configure_adapters(self):
        removed=set(self.adapters)-set(self.config.data['runtimes'])
        self.adapters.clear()
        for name in removed:
            self.health.pop(name,None)
            for id,m in list(self.models.items()):
                if m.runtime==name and not m.loaded and not m.state_uncertain: del self.models[id]
        for name,url in self.config.data['runtimes'].items():
            token=os.environ.get('SWITCHYARD_LM_TOKEN') if name=='lmstudio' else None
            self.adapters[name]=Adapter(name,url,self.session,token)
    async def stop(self):
        if getattr(self,'_stopped',False): return
        self._stopped=True
        try:
            task=getattr(self,'task',None)
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError): await task
            tasks=[v['task'] for v in self.jobs.values() if not v['task'].done()]
            for t in tasks: t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        finally:
            try:
                if self.session: await self.session.close()
            finally:
                self.journal.db.close()
    async def poll(self):
        while True:
            await asyncio.sleep(15)
            try:
                previous=self.config.data['runtimes']; self.config.reload()
                if previous!=self.config.data['runtimes']:
                    if any(m.loaded for m in self.models.values()):
                        self.config.error='Unload all models and restart Switchyard before changing runtime addresses.'
                        continue
                    self.configure_adapters()
                await self.refresh()
                if not self.config.error and self.config.data.get('runtime_local_only_confirmed',False):
                    for m in list(self.models.values()):
                        if m.tier=='resident' and not m.loaded and m.available and not m.state_uncertain:
                            try:
                                await self.ledger.acquire(m.id,min(8192,m.context_max))
                                await self.ledger.release(m.id)
                            except Exception as e:
                                self.journal.event('resident_load_failed',{'model':m.id,'type':type(e).__name__})
                for id,p in list(self.pending.items()):
                    if p['created']<time.time()-1800 and p['status']!='sending': self.resolve(id); del self.pending[id]
            except Exception as e: self.journal.event('poll_error',{'type':type(e).__name__})
    async def refresh(self):
        async with self.refresh_lock, self.ledger.lock:
            self.config.reload()
            configured={name:adapter.url for name,adapter in self.adapters.items()}
            desired={name:url.rstrip('/') for name,url in self.config.data['runtimes'].items()}
            if configured!=desired:
                if any(m.loaded or m.state_uncertain for m in self.models.values()):
                    self.config.error='Runtime addresses changed while memory is reserved. Restore the addresses or unload all models first.'
                    return
                self.configure_adapters()
            results=await asyncio.gather(*(asyncio.wait_for(a.discover(),15) for a in self.adapters.values()),return_exceptions=True)
            self.config.reload()
            overrides=self.config.catalog_data['models']
            for (name,adapter),result in zip(self.adapters.items(),results):
                if isinstance(result,BaseException):
                    self.health[name]={'online':False,'url':adapter.url,'error':'Runtime unavailable or returned invalid metadata.'}
                    for m in self.models.values():
                        if m.runtime==name: m.available=False
                    continue
                self.health[name]={'online':True,'url':adapter.url,'error':None}; seen=set()
                for fresh in result:
                    seen.add(fresh.id); old=self.models.get(fresh.id)
                    if old:
                        if old.state_uncertain:
                            old.memory_gb=max(old.memory_gb,fresh.memory_gb)
                            old.available=False
                            continue
                        fresh.active=old.active; fresh.last_used=old.last_used; fresh.load_seconds=old.load_seconds; fresh.speed_tps=old.speed_tps
                        fresh.managed=old.managed and fresh.loaded and fresh.instance_count<=1 and old.instance==fresh.instance and fresh.loaded_context==old.loaded_context
                        if old.active:
                            fresh.loaded=old.loaded; fresh.instance=old.instance; fresh.managed=old.managed; fresh.loaded_context=old.loaded_context
                            fresh.memory_gb=max(old.memory_gb,fresh.memory_gb)
                    for k,v in overrides.get(fresh.id,{}).items():
                        if k in ('context_max','size_gb','kv_gb_per_1k','tools_verified','tier','digest','task_scores','name','speed_tps'):
                            setattr(fresh,k,v)
                    if fresh.loaded: fresh.memory_gb=max(fresh.memory_gb,fresh.footprint(fresh.loaded_context or 8192))
                    self.models[fresh.id]=fresh
                for id,m in list(self.models.items()):
                    if m.runtime==name and id not in seen and not m.active and not m.state_uncertain: del self.models[id]
            self.last_refresh=time.time()
    def decision(self,body,persist=False):
        self.config.ensure_valid()
        configured={name:adapter.url for name,adapter in self.adapters.items()}
        desired={name:url.rstrip('/') for name,url in self.config.data['runtimes'].items()}
        if configured!=desired: raise Fault('Runtime settings changed. Refresh discovery before routing.','runtime_config_changed',503)
        d=route(body,self.models,self.config.data)
        if persist: self.journal.decision(d)
        return d
    def queue(self,d,body):
        if not d.get('escalation') or self.config.data['escalation_mode']=='off': return
        if d.get('approval_queued') or d['id'] in self.resolved or d['id'] in self.pending: return
        encoded=json.dumps(body).encode()
        if len(encoded)>2*1024*1024:
            d['queue_note']='Payload exceeds the 2 MiB approval retention limit. Resubmit a smaller redacted request.'
            return
        # Bound transient sensitive data. SQLite still receives metadata only.
        while len(self.pending)>=32 or sum(p.get('payload_bytes',0) for p in self.pending.values())+len(encoded)>16*1024*1024:
            removable=next((id for id,p in self.pending.items() if p['status']!='sending'),None)
            if removable is None: return
            self.resolve(removable)
            del self.pending[removable]
        self.pending[d['id']]={'id':d['id'],'created':time.time(),'route':d['route'],'reason':d['escalation'],'tags':d['privacy_tags'],'status':'pending','payload':copy.deepcopy(body),'payload_bytes':len(encoded)}
        d['approval_queued']=True
        self.resolved={k:v for k,v in self.resolved.items() if v>time.time()-86400}
    def state(self):
        for id,p in list(self.pending.items()):
            if p['created']<time.time()-1800 and p['status']!='sending':
                self.resolve(id); del self.pending[id]
        decisions=self.journal.recent(); served=[x for x in decisions if x['status']=='completed']
        return {'version':__version__,'mode':'local','runtimes':self.health,'models':[m.public() for m in self.models.values()],
            'budget_gb':self.config.data['budget_gb'],'headroom_gb':self.config.data['headroom_gb'],'used_gb':round(sum(m.memory_gb for m in self.models.values() if m.loaded),2),
            'active':sum(m.active for m in self.models.values()),'decisions':decisions,'policy':self.config.data,'config_error':self.config.error,
            'warm_rate':round(sum(x.get('warm',False) for x in served)/len(served)*100) if served else None,
            'escalations':[{k:v for k,v in p.items() if k not in ('payload','result')} for p in self.pending.values()],
            'uptime_seconds':int(time.time()-self.started), 'metrics':dict(self.metrics),
            'inflight':dict(self.inflight), 'process_peak_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024**2 if sys.platform=='darwin' else 1024),2),
            'limits':{'inference':32,'generation':8,'control':16,'body_bytes':MAX_BODY,'output_bytes':MAX_RESPONSE}}

@web.middleware
async def guard(request,handler):
    s=request.app['service']; category=None
    try:
        try:
            host=urlparse('http://'+request.host)
            valid=host.hostname in ('127.0.0.1','localhost','::1') and not host.username and not host.password and not host.path and not host.query and not host.fragment
            _=host.port
        except ValueError: valid=False
        if not valid: raise Fault('Invalid Host header.','invalid_host',403)
        origin=request.headers.get('Origin')
        if origin:
            try:
                o=urlparse(origin)
                valid=o.netloc==request.host and o.scheme==request.scheme and not o.path and not o.query and not o.fragment
            except ValueError: valid=False
            if not valid: raise Fault('Cross-origin requests are disabled.','invalid_origin',403)
        if request.path!='/health':
            auth=request.headers.get('Authorization','')
            supplied=auth[7:] if auth.startswith('Bearer ') else request.headers.get('x-api-key','')
            if not supplied or not hmac.compare_digest(supplied.encode(),s.token.encode()):
                raise Fault('Use the local API token from Switchyard Settings.','unauthorized',401)
            category='inference' if request.path in ('/v1/chat/completions','/v1/messages') else 'control'
            if s.inflight[category]>=(32 if category=='inference' else 16):
                category=None; s.metrics['overloaded']+=1
                raise Fault('Gateway is busy; retry shortly.','overloaded',503)
            s.inflight[category]+=1
            if category=='inference': s.metrics['accepted']+=1
        response=await handler(request)
    except Fault as e:
        headers={'Retry-After':'3'} if e.status==503 else {}
        response=web.json_response({'error':{'message':str(e),'code':e.code,'type':'switchyard_error','details':e.details}},status=e.status,headers=headers)
    except web.HTTPException as e:
        response=web.json_response({'error':{'message':e.reason,'code':'http_error'}},status=e.status)
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,yaml.YAMLError):
        response=web.json_response({'error':{'message':'Invalid request data.','code':'invalid_request'}},status=400)
    except (aiohttp.ClientError,asyncio.TimeoutError):
        response=web.json_response({'error':{'message':'Runtime unavailable or timed out.','code':'runtime_unavailable'}},status=503,headers={'Retry-After':'3'})
    except Exception as exc:
        s.journal.event('request_error',{'type':type(exc).__name__})
        response=web.json_response({'error':{'message':'The request failed. See local Activity for its outcome.','code':'internal_error'}},status=500)
    finally:
        if category: s.inflight[category]-=1
    response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'; response.headers['Cache-Control']='no-store'
    return response

async def body_json(request):
    if request.content_type!='application/json': raise Fault('Content-Type must be application/json.',status=415)
    try: body=loads(await asyncio.wait_for(request.read(),10))
    except asyncio.TimeoutError: raise Fault('Request body timed out.','body_timeout',408)
    except ValueError: raise Fault('Invalid JSON or request complexity limit exceeded.')
    if not isinstance(body,dict): raise Fault('Request must be a JSON object.')
    return body

def headers(d):
    return {'X-Switchyard-Request-Id':d['id'],'X-Switchyard-Model':d.get('model') or 'none','X-Switchyard-Route':d['route'],
        'X-Switchyard-Runtime':d.get('runtime') or 'none','X-Switchyard-Warming':str(not d['warm']).lower(),
        'X-Switchyard-Escalate':('suggested; reason='+d['escalation']) if d['escalation'] else 'none'}

async def index(request):
    return web.json_response({'name':'Switchyard','version':__version__,'interface':'Open the native Switchyard app or use the authenticated API.','models':'/v1/models','status':'/switchyard/status'})
async def status(request): return web.json_response(request.app['service'].state())
async def refresh(request): await request.app['service'].refresh(); return await status(request)
async def catalog(request): return web.json_response({'models':[m.public() for m in request.app['service'].models.values()]})
async def models(request):
    s=request.app['service']; names=['auto','auto-fast','auto-code']+list(s.models)
    return web.json_response({'object':'list','data':[{'id':x,'object':'model','created':0,'owned_by':'switchyard'} for x in names]})
async def dry_run(request): return web.json_response(request.app['service'].decision(await body_json(request)))
async def decisions(request): return web.json_response({'decisions':request.app['service'].journal.recent()})
async def decision_by_id(request):
    d=request.app['service'].journal.get(request.match_info['id'])
    if not d: raise Fault('Decision not found.','not_found',404)
    return web.json_response(d)
async def policy(request):
    s=request.app['service']
    if request.method=='GET': return web.json_response(s.config.data)
    data=validate_policy(await body_json(request))
    async with s.refresh_lock, s.ledger.lock:
        if sum(m.memory_gb for m in s.models.values() if m.loaded)>data['budget_gb']-data['headroom_gb']: raise Fault('Unload models before reducing the budget below current use.',status=409)
        if data['runtimes']!=s.config.data['runtimes'] and any(m.loaded or m.state_uncertain for m in s.models.values()): raise Fault('Unload all models before changing runtime addresses.',status=409)
        s.config.save(data); s.configure_adapters()
    return web.json_response(s.config.data)
async def model_action(request):
    s=request.app['service']; b=await body_json(request); id=b.get('model'); action=request.match_info['action']
    if id not in s.models: raise Fault('Model not found.','not_found',404)
    if action=='unload': await s.ledger.unload(id)
    elif action=='load':
        require_local_runtime(s)
        await s.refresh(); m=s.models[id]; context=b.get('context',8192)
        if type(context) is not int: raise Fault('Context must be an integer.')
        if context<1024 or not m.context_max or context>m.context_max or not m.available or m.size_gb<=0: raise Fault('Invalid context or unavailable model.')
        await s.ledger.acquire(id,context); await s.ledger.release(id)
    elif action=='configure':
        patch=validate_catalog_patch(b.get('values',{}))
        data=copy.deepcopy(s.config.catalog_data); data.setdefault('models',{}).setdefault(id,{}).update(patch)
        atomic_yaml(s.config.overrides,data); await s.refresh()
    return await status(request)

def require_local_runtime(s):
    s.config.ensure_valid()
    if not s.config.data.get('runtime_local_only_confirmed',False):
        raise Fault('Before loading or generating, disable Ollama cloud access and LM Link, restart the runtimes, and confirm local-only setup in Settings.','runtime_locality_unconfirmed',403)

async def recover(request):
    s=request.app['service']; b=await body_json(request); runtime=b.get('runtime')
    require_local_runtime(s)
    if b.get('confirmed_runtime_restarted') is not True or runtime not in s.adapters:
        raise Fault('Restart the affected runtime, then explicitly confirm recovery.','recovery_confirmation_required',400)
    async with s.refresh_lock, s.ledger.lock:
        if any(m.active for m in s.models.values() if m.runtime==runtime): raise Fault('Wait for active requests before recovery.','model_busy',409)
        fresh=await s.adapters[runtime].discover()
        if any(m.state_uncertain for m in fresh): raise Fault('Runtime state is still uncertain.','runtime_uncertain',503)
        for id,m in list(s.models.items()):
            if m.runtime==runtime: del s.models[id]
        for m in fresh:
            for key,value in s.config.catalog_data['models'].get(m.id,{}).items(): setattr(m,key,value)
            if m.loaded: m.memory_gb=max(m.memory_gb,m.footprint(m.loaded_context or 8192))
            s.models[m.id]=m
    await s.ledger.confirm_reconciled(runtime)
    s.journal.event('runtime_recovered',{'runtime':runtime})
    return await status(request)

async def inference(request):
    s=request.app['service']
    request['deadline']=asyncio.get_running_loop().time()+s.config.data['request_timeout']
    try: await asyncio.wait_for(s.generation_slots.acquire(),min(s.config.data['queue_timeout'],s.config.data['request_timeout']))
    except asyncio.TimeoutError: raise Fault('Generation queue is busy.','queue_timeout',503)
    try: return await _inference(request)
    finally: s.generation_slots.release()

async def _inference(request):
    s=request.app['service']; incoming=await body_json(request); anth=request.path=='/v1/messages'
    require_local_runtime(s)
    body=anthropic_request(incoming) if anth else incoming
    def remaining():
        value=request['deadline']-asyncio.get_running_loop().time()
        if value<=0: raise Fault('Request deadline exceeded.','request_timeout',503)
        return value
    if time.time()-s.last_refresh>15: await asyncio.wait_for(s.refresh(),remaining())
    d=s.decision(body,True); s.queue(d,body)
    if not d['model']: raise Fault('No eligible local model. Review the decision or reduce the request context.',d['escalation'],422,d)
    body={k:v for k,v in body.items() if k not in ('metadata','max_completion_tokens')}; body['max_tokens']=d['max_tokens']
    acquired=False; response=None; chunks=[]; start=time.perf_counter(); generator=None; response_bytes=0
    try:
        queue_start=time.perf_counter()
        async with asyncio.timeout(remaining()):
            m=await s.ledger.acquire(d['model'],d['context'],tools_required=inspect_payload(body,s.config.data)['tools']); acquired=True
        d['queue_and_load_ms']=round((time.perf_counter()-queue_start)*1000,2)
        require_local_runtime(s)
        upstream=s.adapters[m.runtime].chunks(m,body,timeout=remaining())
        async def bounded_upstream():
            try:
                while True:
                    chunk=await asyncio.wait_for(anext(upstream,None),remaining())
                    if chunk is None: return
                    yield chunk
            finally: await upstream.aclose()
        generator=bounded_upstream()
        # Fetch before sending headers so connection/load failures produce a proper JSON error.
        first=await anext(generator,None)
        d['first_token_ms']=round((time.perf_counter()-start)*1000,2)
        if first is None: raise Fault('Runtime produced an empty response.','empty_response',502)
        async def all_chunks():
            nonlocal response_bytes
            response_bytes+=len(json.dumps(first,allow_nan=False).encode())
            if response_bytes>MAX_RESPONSE: raise Fault('Output exceeded the gateway retention limit.','output_limit',502)
            yield first
            async for chunk in generator:
                response_bytes+=len(json.dumps(chunk,allow_nan=False).encode())
                if response_bytes>MAX_RESPONSE or len(chunks)>=8192: raise Fault('Output exceeded the gateway retention limit.','output_limit',502)
                yield chunk
        if not body.get('stream',False):
            async for c in all_chunks(): chunks.append(c)
            result=complete(chunks,m.id); post_check(s,d,body,result)
            d['status']='completed'; d['total_ms']=round((time.perf_counter()-start)*1000); d['usage']=result.get('usage',{})
            s.journal.decision(d); s.metrics['completed']+=1
            return web.json_response(anthropic_response(result) if anth else result,headers=headers(d))
        response=web.StreamResponse(headers={**headers(d),'Content-Type':'text/event-stream','Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        await response.prepare(request)
        async def emit(event,data):
            prefix=f'event: {event}\n' if event else ''
            await asyncio.wait_for(response.write((prefix+'data: '+json.dumps(data)+'\n\n').encode()),remaining())
        if anth:
            message={'id':first.get('id',d['id']),'type':'message','role':'assistant','model':m.id,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}
            await emit('message_start',{'type':'message_start','message':message})
            block_index=0; text_open=False; tool_indices={}
        async for c in all_chunks():
            chunks.append(c)
            if not anth: await emit(None,c); continue
            for choice in c.get('choices',[]):
                delta=choice.get('delta',{})
                if delta.get('content'):
                    if not text_open:
                        await emit('content_block_start',{'type':'content_block_start','index':block_index,'content_block':{'type':'text','text':''}}); text_open=True
                    await emit('content_block_delta',{'type':'content_block_delta','index':block_index,'delta':{'type':'text_delta','text':delta['content']}})
                for t in delta.get('tool_calls',[]):
                    if text_open:
                        await emit('content_block_stop',{'type':'content_block_stop','index':block_index}); block_index+=1; text_open=False
                    tid=t.get('index',0)
                    if tid not in tool_indices:
                        tool_indices[tid]=block_index; block_index+=1
                        await emit('content_block_start',{'type':'content_block_start','index':tool_indices[tid],'content_block':{'type':'tool_use','id':t.get('id','call_'+str(tid)),'name':t.get('function',{}).get('name',''),'input':{}}})
                    if t.get('function',{}).get('arguments'):
                        await emit('content_block_delta',{'type':'content_block_delta','index':tool_indices[tid],'delta':{'type':'input_json_delta','partial_json':t['function']['arguments']}})
        result=complete(chunks,m.id); post_check(s,d,body,result)
        if anth:
            if text_open: await emit('content_block_stop',{'type':'content_block_stop','index':block_index})
            for i in tool_indices.values(): await emit('content_block_stop',{'type':'content_block_stop','index':i})
            finish=anthropic_response(result)
            await emit('message_delta',{'type':'message_delta','delta':{'stop_reason':finish['stop_reason'],'stop_sequence':None},'usage':finish['usage']})
            await emit('message_stop',{'type':'message_stop'})
        else: await asyncio.wait_for(response.write(b'data: [DONE]\n\n'),remaining())
        d['status']='completed'; d['total_ms']=round((time.perf_counter()-start)*1000); d['usage']=result.get('usage',{}); s.journal.decision(d); s.metrics['completed']+=1
        await response.write_eof(); return response
    except (asyncio.CancelledError,ConnectionResetError):
        d['status']='cancelled'; s.journal.decision(d); s.metrics['cancelled']+=1; raise
    except Exception as e:
        d['status']='failed'; d['error']=getattr(e,'code','runtime_error'); s.journal.decision(d); s.metrics['failed']+=1
        if response and response.prepared:
            with contextlib.suppress(ConnectionResetError):
                payload={'error':{'type':'api_error','message':'Runtime stream failed; partial output was not retried.','code':d['error']}}
                await response.write((('event: error\n' if anth else '')+'data: '+json.dumps(payload)+'\n\n').encode()); await response.write_eof()
            return response
        raise
    finally:
        try:
            if generator: await generator.aclose()
        finally:
            if acquired: await asyncio.shield(s.ledger.release(d['model']))

def post_check(s,d,body,result):
    fmt=body.get('response_format',{})
    if fmt.get('type') in ('json_object','json_schema'):
        try:
            value=loads(result['choices'][0]['message']['content'])
            if fmt['type']=='json_schema':
                import jsonschema
                jsonschema.validate(value,fmt['json_schema']['schema'])
        except Exception:
            d['escalation']='failed_self_check'; s.queue(d,body)
            raise Fault('Model output did not pass the requested JSON/schema check.','failed_self_check',502)

async def queue_list(request): return web.json_response({'escalations':request.app['service'].state()['escalations']})

def pending_record(s,id):
    p=s.pending.get(id)
    if p and p['created']<time.time()-1800 and p['status']!='sending':
        s.resolve(id); del s.pending[id]; p=None
    if not p: raise Fault('Approval expired or not found.','not_found',404)
    return p

def frontier_payload(provider,model,payload,max_tokens):
    if provider not in ('openai','anthropic') or not isinstance(model,str) or not model.strip() or len(model)>250:
        raise Fault('Choose a provider and a model.')
    safe=copy.deepcopy({k:v for k,v in payload.items() if k in ('messages','max_tokens','temperature','top_p','tools','response_format')})
    safe.update(model=model,stream=False,max_tokens=max_tokens)
    if provider=='anthropic':
        if safe.get('tools') or any(x.get('role')=='tool' or x.get('tool_calls') for x in safe['messages']):
            raise Fault('Choose OpenAI for tool-history escalation in this release.')
        if safe.get('response_format'): raise Fault('Anthropic structured-output escalation is not supported in this release.')
        def text_content(message):
            value=message.get('content','')
            return value if isinstance(value,str) else '\n'.join(x.get('text','') for x in value)
        system='\n'.join(text_content(x) for x in safe['messages'] if x['role'] in ('system','developer'))
        safe['messages']=[x for x in safe['messages'] if x['role'] not in ('system','developer')]
        if system: safe['system']=system
    return safe

async def queue_detail(request):
    p=pending_record(request.app['service'],request.match_info['id'])
    return web.json_response(p)
async def escalation(request):
    s=request.app['service']; b=await body_json(request); p=pending_record(s,request.match_info['id'])
    s.config.ensure_valid()
    if p['status']!='pending': raise Fault('This request has already been resolved.','already_resolved',409)
    if b.get('action')=='deny':
        p['status']='denied'; p.pop('payload',None); p['payload_bytes']=0; s.resolve(p['id'])
        return web.json_response({'status':'denied'})
    if s.config.data['escalation_mode']=='off': raise Fault('Frontier escalation is disabled.','escalation_disabled',403)
    payload=b.get('payload')
    if not isinstance(payload,dict): raise Fault('Review the complete payload before approval.')
    check=inspect_payload(payload,s.config.data); provider=b.get('provider'); model=b.get('model')
    safe=frontier_payload(provider,model,payload,check['max_tokens'])
    check['tags']=sorted(set(check['tags']+privacy_tags(json.dumps(safe),s.config.data)))
    fingerprint=hashlib.sha256(json.dumps({'provider':provider,'model':model,'payload':safe},sort_keys=True).encode()).hexdigest()
    if b.get('action')=='preview': return web.json_response({'hash':fingerprint,'tags':check['tags'],'payload':safe,'provider':provider,'model':model})
    if b.get('action')!='approve' or b.get('confirmed') is not True or b.get('hash')!=fingerprint: raise Fault('Approval must match the reviewed payload.','approval_required',403)
    if check['tags']: raise Fault('Redact all detected sensitive content before sending. The scanner is a heuristic; review the entire payload.','privacy_blocked',403)
    if sys.platform!='darwin': raise Fault('Frontier credentials require the macOS Keychain.','keychain_required',503)
    from keyring.backends.macOS import Keyring
    key=Keyring().get_password('switchyard',provider)
    if not key: raise Fault(f'Save your {provider} API key in Switchyard Settings first, or use yard key {provider}.','provider_not_configured',409)
    p['status']='sending'; p['provider']=provider; p['frontier_model']=model
    if provider=='openai': url='https://api.openai.com/v1/chat/completions'; hdr={'Authorization':'Bearer '+key}
    else:
        url='https://api.anthropic.com/v1/messages'; hdr={'x-api-key':key,'anthropic-version':'2023-06-01'}
    try:
        async with s.session.post(url,json=safe,headers=hdr,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=180)) as r:
            if r.status>=300: raise Fault(f'Frontier provider returned HTTP {r.status}.','frontier_error',502)
            result=await read_response(r,MAX_RESPONSE)
        p['status']='approved'; p['result']=result; p.pop('payload',None); p['payload_bytes']=0
        s.journal.event('approved_escalation',{'id':p['id'],'provider':provider,'model':model,'payload_hash':fingerprint})
        return web.json_response({'status':'approved','result':result},headers={'X-Switchyard-Served-By':f'frontier/{model}'})
    except BaseException:
        # Never retry implicitly: the provider may already have accepted the request.
        p['status']='failed'; p.pop('payload',None); p['payload_bytes']=0; raise
    finally: s.resolve(p['id'])

async def pull(request):
    s=request.app['service']; b=await body_json(request)
    if b.get('confirmed') is not True: raise Fault('Explicit download confirmation is required.')
    runtime=b.get('runtime'); model=b.get('model')
    if runtime not in s.adapters or not isinstance(model,str): raise Fault('Choose a runtime and model.')
    if sum(not v['task'].done() for v in s.jobs.values())>=2: raise Fault('Two downloads are already running.','overloaded',503)
    while len(s.jobs)>=32:
        victim=next((k for k,v in s.jobs.items() if v['task'].done()),None)
        if victim is None: raise Fault('Download queue is full.','overloaded',503)
        del s.jobs[victim]
    id='job_'+secrets.token_hex(6)
    async def run():
        try:
            s.jobs[id]['result']=await s.adapters[runtime].pull(model); s.jobs[id]['status']='completed'; await s.refresh()
        except asyncio.CancelledError: s.jobs[id]['status']='cancelled'; raise
        except Exception: s.jobs[id]['status']='failed'; s.jobs[id]['error']='Runtime download failed.'
    s.jobs[id]={'id':id,'status':'running','task':asyncio.create_task(run())}
    return web.json_response({'id':id,'status':'running'},status=202)
async def job(request):
    v=request.app['service'].jobs.get(request.match_info['id'])
    if not v: raise Fault('Job not found.','not_found',404)
    return web.json_response({k:x for k,x in v.items() if k!='task'})

async def create_app(path):
    app=web.Application(middlewares=[guard],client_max_size=MAX_BODY); s=Service(path); app['service']=s
    async def health(request): return web.json_response({'ok':True,'version':__version__})
    app.router.add_get('/',index); app.router.add_get('/health',health)
    app.router.add_get('/v1/models',models); app.router.add_post('/v1/chat/completions',inference); app.router.add_post('/v1/messages',inference)
    app.router.add_get('/switchyard/status',status); app.router.add_post('/switchyard/refresh',refresh); app.router.add_get('/switchyard/catalog',catalog)
    app.router.add_post('/switchyard/route',dry_run); app.router.add_get('/switchyard/decisions',decisions); app.router.add_get('/switchyard/decisions/{id}',decision_by_id)
    app.router.add_get('/switchyard/policy',policy); app.router.add_put('/switchyard/policy',policy)
    app.router.add_post('/switchyard/models/{action:load|unload|configure}',model_action)
    app.router.add_post('/switchyard/recover',recover)
    app.router.add_get('/switchyard/escalations',queue_list); app.router.add_get('/switchyard/escalations/{id}',queue_detail); app.router.add_post('/switchyard/escalations/{id}',escalation)
    app.router.add_post('/switchyard/pull',pull); app.router.add_get('/switchyard/jobs/{id}',job)
    async def start(app): await s.start()
    async def stop(app): await s.stop()
    app.on_startup.append(start); app.on_cleanup.append(stop)
    return app
