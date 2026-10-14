"""Disposable real HTTP peers: no connection to installed models or providers."""
import asyncio
import json
import hashlib
from aiohttp import web
from aiohttp.test_utils import TestServer


class Runtime:
    def __init__(self, name='ollama', count=3):
        self.name=name
        self.names=[f'fixture-{kind}-{i}' for i,kind in enumerate(('chat','code','reasoning')[:count])]
        self.loaded={}; self.active={}; self.loads=0; self.generations=0
        self.evicted_active=0; self.unloads=0; self.cancelled=0
        self.fail_load=False; self.fail_unload=False; self.delay=.001
        self.cloud_disabled=True; self.show_calls=0
        self.app=web.Application()
        for method,path,handler in (
            ('GET','/api/status',self.status),('GET','/api/tags',self.tags),('GET','/api/ps',self.ps),('POST','/api/show',self.show),
            ('POST','/api/generate',self.generate),('POST','/api/chat',self.chat),
            ('GET','/api/v1/models',self.lm_models),('POST','/api/v1/models/load',self.lm_load),
            ('POST','/api/v1/models/unload',self.lm_unload),('POST','/v1/chat/completions',self.chat),
            ('POST','/api/pull',self.pull),('POST','/api/v1/models/download',self.pull)):
            self.app.router.add_route(method,path,handler)

    async def start(self):
        self.server=TestServer(self.app,handler_cancellation=True)
        await self.server.start_server()
        self.url=str(self.server.make_url('')).rstrip('/')
        return self

    async def stop(self):
        await self.server.close()

    async def tags(self,r):
        return web.json_response({'models':[{'name':n,'digest':hashlib.sha256(n.encode()).hexdigest(),'size':400*1024**2,'details':{'format':'gguf','quantization_level':'Q4_K_M'}} for n in self.names]})

    async def status(self,r):
        return web.json_response({'cloud':{'disabled':self.cloud_disabled}})

    async def ps(self,r):
        return web.json_response({'models':[{'name':n,'size':600*1024**2,'context_length':c} for n,c in self.loaded.items()]})

    async def show(self,r):
        self.show_calls+=1
        b=await r.json()
        if b.get('model') not in self.names: raise web.HTTPNotFound()
        return web.json_response({'model_info':{'fixture.context_length':65536,'fixture.block_count':16,
            'fixture.attention.head_count':16,'fixture.attention.head_count_kv':4,'fixture.embedding_length':2048},'capabilities':['completion','tools']})

    async def generate(self,r):
        b=await r.json(); name=b['model']
        if b.get('keep_alive')==0:
            self._unload(name)
        else:
            if self.fail_load: raise web.HTTPServiceUnavailable()
            self.loads+=1; self.loaded[name]=b.get('options',{}).get('num_ctx',8192)
        return web.json_response({'done':True})

    def _unload(self,name):
        if self.fail_unload: raise web.HTTPServiceUnavailable()
        if self.active.get(name,0): self.evicted_active+=1
        self.loaded.pop(name,None); self.unloads+=1

    async def lm_models(self,r):
        return web.json_response({'models':[{'key':n,'type':'llm','size_bytes':400*1024**2,'max_context_length':65536,
            'loaded_instances':[{'id':n,'config':{'context_length':self.loaded[n]}}] if n in self.loaded else [],
            'capabilities':{'trained_for_tool_use':True}} for n in self.names]})

    async def lm_load(self,r):
        b=await r.json()
        if self.fail_load: raise web.HTTPServiceUnavailable()
        self.loads+=1; self.loaded[b['model']]=b['context_length']
        return web.json_response({'instance_id':b['model'],'load_config':{'context_length':b['context_length']}})

    async def lm_unload(self,r):
        b=await r.json(); self._unload(b['instance_id']); return web.json_response({})

    async def pull(self,r):
        await asyncio.sleep(.05)
        return web.json_response({'status':'success'})

    async def chat(self,r):
        b=await r.json(); name=b['model']; text=json.dumps(b.get('messages',[]))
        self.generations+=1
        if '[before]' in text: raise web.HTTPServiceUnavailable()
        response=web.StreamResponse(headers={'Content-Type':'application/x-ndjson' if self.name=='ollama' else 'text/event-stream'})
        await response.prepare(r)
        self.active[name]=self.active.get(name,0)+1
        try:
            if '[oversize]' in text:
                await response.write(b'x'*(300*1024)+b'\n'); return response
            if '[malformed]' in text:
                await response.write(b'data: {nope}\n\n' if self.name!='ollama' else b'{nope}\n'); return response
            if '[empty]' in text: return response
            count=40 if '[slow]' in text else 3
            for i in range(count):
                await asyncio.sleep(.03 if '[slow]' in text else self.delay)
                finished=i==count-1
                delta={'content':'fixture reply' if i==0 else '', 'role':'assistant'}
                native={'message':delta,'done':finished,'done_reason':'stop','prompt_eval_count':12,'eval_count':3}
                chunk={'id':'chatcmpl-fixture','object':'chat.completion.chunk','model':name,'created':1,
                       'choices':[{'index':0,'delta':delta,'finish_reason':'stop' if finished else None}]}
                if b.get('tools') and i==0:
                    native['message']={'tool_calls':[{'function':{'name':'test_tool','arguments':{'value':1}}}]}
                    chunk['choices'][0]['delta']={'tool_calls':[{'index':0,'id':'fixture_call','type':'function','function':{'name':'test_tool','arguments':'{"value":1}'}}]}
                if b.get('tools') and finished: chunk['choices'][0]['finish_reason']='tool_calls'
                if finished: chunk['usage']={'prompt_tokens':12,'completion_tokens':3,'total_tokens':15}
                await response.write((json.dumps(native)+'\n').encode() if self.name=='ollama' else ('data: '+json.dumps(chunk)+'\n\n').encode())
                if '[mid]' in text: return response
            if self.name!='ollama': await response.write(b'data: [DONE]\n\n')
            return response
        except (ConnectionResetError,asyncio.CancelledError):
            self.cancelled+=1
            raise
        finally:
            self.active[name]-=1
