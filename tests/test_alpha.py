import asyncio
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from loqalyn import security
from loqalyn.core import Config, Journal, Model, Fault, DEFAULTS, atomic_yaml, route, inspect_payload, validate_policy, validate_runtime_url
from loqalyn.server import create_app, Service
from loqalyn.adapters import complete, anthropic_request
from tests.fixtures import Runtime


def request(text='Hello', model='auto', **kw):
    return {'model':model,'messages':[{'role':'user','content':text}],'max_tokens':16,**kw}


class InputTests(unittest.TestCase):
    def test_json_depth(self):
        with self.assertRaises(ValueError): security.loads('['*2000+'0'+']'*2000)

    def test_json_nodes(self):
        with self.assertRaises(ValueError): security.bounded_tree([0]*40000)

    def test_json_cycles(self):
        x=[]; x.append(x)
        with self.assertRaises(ValueError): security.bounded_tree(x)

    def test_json_duplicate(self):
        with self.assertRaises(ValueError): security.loads('{"a":1,"a":2}')

    def test_json_finite(self):
        for data in ('{"x":NaN}','{"x":Infinity}','{"x":1e999}'):
            with self.subTest(data=data), self.assertRaises(ValueError): security.loads(data)

    def test_json_bytes(self):
        with self.assertRaises(ValueError): security.loads('"'+'a'*security.MAX_BODY+'"')

    def test_schema_work_limits(self):
        for schema in ({'$ref':'#'}, {'pattern':'(a+)+$'}, {'anyOf':[{}]*1000}, {'uniqueItems':True}, {'enum':[[1]]}):
            with self.subTest(schema=schema), self.assertRaises(Fault):
                inspect_payload(request(response_format={'type':'json_schema','json_schema':{'schema':schema}}),DEFAULTS)

    def test_schema_supported(self):
        schema={'type':'object','properties':{'a':{'type':'integer','minimum':1}},'required':['a'],'additionalProperties':False}
        inspect_payload(request(response_format={'type':'json_schema','json_schema':{'schema':schema}}),DEFAULTS)

    def test_policy_work_limits(self):
        for change in ({'sensitive_terms':['x']*129},{'sensitive_terms':['x'*257]}, {'routes':DEFAULTS['routes']*20}):
            with self.subTest(change=change),self.assertRaises(Fault): validate_policy(change)

    def test_input_options(self):
        for fields in ({'temperature':float('nan')},{'max_tokens':True},{'stream':1},{'n':2},{'model':''},{'seed':2**200},{'bogus':1},{'metadata':{'a':float('inf')}}):
            with self.subTest(fields=fields),self.assertRaises(Fault): inspect_payload(request(**fields),DEFAULTS)

    def test_remote_runtime_addresses(self):
        for url in ('https://127.0.0.1:1','http://localhost:1','http://127.1:1','http://8.8.8.8','http://user@127.0.0.1','http://127.0.0.1/path','http://127.0.0.1?x=1'):
            with self.subTest(url=url),self.assertRaises(Fault): validate_runtime_url('ollama',url)

    def test_private_tags(self):
        for text,tag in (('user@example.test','email'),('sk-abcdefghijklmnop','credential'),('123-45-6789','ssn')):
            self.assertIn(tag,inspect_payload(request(text),DEFAULTS)['tags'])
        self.assertIn('private',inspect_payload(request(metadata={'private':True}),DEFAULTS)['tags'])

    def test_long_privacy_scan_is_bounded(self):
        start=time.perf_counter(); inspect_payload(request('a'*500000),DEFAULTS)
        self.assertLess(time.perf_counter()-start,2)

    def test_tool_history_requires_results(self):
        b=request(); b['messages']=[{'role':'assistant','content':None,'tool_calls':[{'id':'c','type':'function','function':{'name':'f','arguments':'{}'}}]}]
        with self.assertRaises(Fault): inspect_payload(b,DEFAULTS)
        b['messages'].append({'role':'tool','tool_call_id':'c','content':'ok'})
        self.assertTrue(inspect_payload(b,DEFAULTS)['tools'])

    def test_remote_images_rejected(self):
        b=request(); b['messages'][0]['content']=[{'type':'image_url','image_url':{'url':'https://example.com/a.png'}}]
        with self.assertRaises(Fault) as c: inspect_payload(b,DEFAULTS)
        self.assertEqual(c.exception.code,'remote_media_blocked')

    def test_unknown_anthropic_features(self):
        with self.assertRaises(Fault): anthropic_request(request(thinking={'type':'enabled'}))

    def test_final_tool_arguments_checked(self):
        with self.assertRaises(Fault): complete([{'choices':[{'delta':{'tool_calls':[{'id':'c','index':0,'function':{'name':'f','arguments':'{oops'}}]}}]}],'m')

    def test_route_explanation_and_fingerprint(self):
        m=Model('ollama/m','m','ollama','m',size_gb=1,context_max=32000,kv_gb_per_1k=.01)
        a=route(request(),{m.id:m},DEFAULTS); b=route(request(),{m.id:m},DEFAULTS)
        self.assertEqual(a['model'],m.id); self.assertEqual(a['policy_fingerprint'],b['policy_fingerprint'])
        self.assertEqual(a['candidates'],b['candidates']); self.assertIsNone(a['confidence'])
        self.assertAlmostEqual(a['candidates'][0]['score'],sum(a['candidates'][0]['components'].values()),places=2)

    def test_explicit_selection_keeps_constraints(self):
        m=Model('ollama/m','m','ollama','m',size_gb=1,context_max=1024)
        d=route(request('a'*2000,model=m.id),{m.id:m},DEFAULTS)
        self.assertIsNone(d['model']); self.assertIn('context_limit',d['exclusions'][0]['reasons'])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'data'; self.c=Config(self.path)
    def tearDown(self): self.tmp.cleanup()
    def test_private_token(self):
        value=security.api_token(self.path/'token')
        self.assertEqual(len(value),43); self.assertEqual(value,security.api_token(self.path/'token'))
        self.assertEqual((self.path/'token').stat().st_mode & 0o777,0o600)
    def test_empty_token(self):
        p=self.path/'token'; p.touch(mode=0o600)
        with self.assertRaises(ValueError): security.api_token(p)
    def test_public_token(self):
        p=self.path/'token'; p.write_text('a'*43); p.chmod(0o644)
        with self.assertRaises(ValueError): security.api_token(p)
    def test_symlink_token(self):
        other=self.path/'other'; other.write_text('a'*43); other.chmod(0o600)
        (self.path/'token').symlink_to(other)
        with self.assertRaises((ValueError,OSError)): security.api_token(self.path/'token')
    def test_hardlink_token(self):
        security.api_token(self.path/'token'); os.link(self.path/'token',self.path/'other')
        with self.assertRaises(ValueError): security.api_token(self.path/'token')
    def test_directory_link(self):
        link=self.path.parent/'link'; link.symlink_to(self.path)
        with self.assertRaises(ValueError): Config(link)
    def test_config_invalid_blocks(self):
        old=copy.deepcopy(self.c.data); self.c.file.write_text('bad: [')
        with self.assertRaises(Fault): self.c.ensure_valid()
        self.assertEqual(self.c.data,old)
        atomic_yaml(self.c.file,old); self.c.ensure_valid(); self.assertIsNone(self.c.error)
    def test_yaml_cycle_blocks(self):
        self.c.file.write_text('routes: &x [*x]')
        with self.assertRaises(Fault): self.c.ensure_valid()
    def test_database_link_blocks(self):
        p=self.path/'db'; p.symlink_to(self.c.file)
        with self.assertRaises(ValueError): Journal(p)
    def test_database_patched(self):
        j=Journal(self.path/'db')
        self.assertGreaterEqual(tuple(map(int,j.db.execute('select sqlite_version()').fetchone()[0].split('.'))),(3,54,0)); j.db.close()
    def test_database_no_prompt(self):
        j=Journal(self.path/'db'); d=route(request('SENTINEL-PRIVATE-PROMPT'),{},DEFAULTS); j.decision(d); j.db.close()
        self.assertNotIn(b'SENTINEL-PRIVATE-PROMPT',(self.path/'db').read_bytes())
    def test_database_retention(self):
        j=Journal(self.path/'db')
        for i in range(10): j.event('test',{'n':i})
        j._bounded_retention('events',3)
        self.assertEqual(j.db.execute('select count(*) from events').fetchone()[0],3); j.db.close()
    def test_large_decision_history_is_bounded_by_bytes(self):
        j=Journal(self.path/'db')
        try:
            for i in range(40): j.decision({'id':str(i),'created':i,'candidate_metadata':'x'*(1024*1024)})
            self.assertLessEqual(len(json.dumps(j.recent()).encode()),2*1024*1024)
            j._bounded_retention('decisions',10000)
            self.assertLessEqual(j.db.execute('SELECT SUM(length(CAST(data AS BLOB))) FROM decisions').fetchone()[0],32*1024*1024)
        finally: j.db.close()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'data'
        self.ollama=await Runtime().start(); self.lm=await Runtime('lmstudio').start()
        c=Config(self.path); c.data.update(runtimes={'ollama':self.ollama.url,'lmstudio':self.lm.url},runtime_local_only_confirmed=True,budget_gb=12,headroom_gb=1,queue_timeout=.5,request_timeout=3)
        c.save(c.data); self.app=await create_app(self.path); self.s=self.app['service']
        self.client=TestClient(TestServer(self.app,handler_cancellation=True)); await self.client.start_server()
        self.auth={'Authorization':'Bearer '+self.s.token}
    async def asyncTearDown(self):
        await self.client.close(); await self.ollama.stop(); await self.lm.stop(); self.temp.cleanup()
    async def post(self,path,b=None,**kw):
        return await self.client.post(path,json=b if b is not None else request(),headers=self.auth,**kw)
    async def test_inventory(self): self.assertEqual(len(self.s.models),6)
    async def test_dashboard_requires_auth(self):
        r=await self.client.get('/'); self.assertEqual(r.status,401); self.assertNotIn(self.s.token,await r.text())
    async def test_authenticated_dashboard_no_embedded_token(self):
        r=await self.client.get('/',headers=self.auth); self.assertEqual(r.status,200); self.assertNotIn(self.s.token,await r.text())
    async def test_all_control_endpoints_require_auth(self):
        for method,path in [('GET','/loqalyn/status'),('GET','/v1/models'),('POST','/loqalyn/route'),('POST','/loqalyn/pull'),('PUT','/loqalyn/policy'),('GET','/app.js'),('POST','/v1/messages')]:
            r=await self.client.request(method,path,json={}); self.assertEqual(r.status,401,path)
    async def test_bad_origins_hosts(self):
        for headers in ({'Origin':'http://['},{'Origin':'null'},{'Origin':'https://evil.test'},{'Host':'evil.test'},{'Host':'attacker@localhost'}):
            r=await self.client.get('/health',headers=headers); self.assertEqual(r.status,403,headers)
    async def test_nonascii_token(self):
        r=await self.client.get('/loqalyn/status',headers={'Authorization':'Bearer é'}); self.assertEqual(r.status,401)
    async def test_health_is_minimal(self):
        r=await self.client.get('/health'); self.assertEqual(set(await r.json()),{'ok','version'})
    async def test_bad_json(self):
        for data in ('[]','{"a":NaN}','{"a":1,"a":2}','['*2000+'0'+']'*2000):
            r=await self.client.post('/loqalyn/route',data=data,headers={**self.auth,'Content-Type':'application/json'}); self.assertEqual(r.status,400)
    async def test_body_size(self):
        r=await self.post('/loqalyn/route',request('x'*(2*1024*1024))); self.assertEqual(r.status,413)
    async def test_dry_run_no_runtime_work(self):
        r=await self.post('/loqalyn/route'); self.assertEqual(r.status,200)
        self.assertEqual(self.ollama.loads+self.lm.loads+self.ollama.generations+self.lm.generations,0)
        self.assertEqual(len(self.s.journal.recent()),0)
    async def test_locality_required(self):
        p=copy.deepcopy(self.s.config.data); p['runtime_local_only_confirmed']=False; self.s.config.save(p)
        r=await self.post('/v1/chat/completions'); self.assertEqual(r.status,403)
        self.assertEqual(self.ollama.generations,0)
    async def test_text_both_runtimes(self):
        for name in ('ollama','lmstudio'):
            r=await self.post('/v1/chat/completions',request(model=f'{name}/fixture-chat-0'))
            self.assertEqual(r.status,200,await r.text()); b=await r.json(); self.assertEqual(b['choices'][0]['message']['content'],'fixture reply')
            d=self.s.journal.get(r.headers['X-Loqalyn-Request-Id']); self.assertEqual(d['model'],b['model']); self.assertEqual(d['status'],'completed')
    async def test_sse_both_runtimes(self):
        for name in ('ollama','lmstudio'):
            r=await self.post('/v1/chat/completions',request(model=f'{name}/fixture-chat-0',stream=True)); t=await r.text()
            self.assertEqual(r.status,200); self.assertIn('data: [DONE]',t); self.assertIn('fixture reply',t)
    async def test_anthropic(self):
        for stream in (False,True):
            r=await self.post('/v1/messages',request(stream=stream)); self.assertEqual(r.status,200,await r.text())
            if stream: self.assertIn('event: message_stop',await r.text())
            else: self.assertEqual((await r.json())['type'],'message')
    async def test_tools_require_verification(self):
        b=request(tools=[{'type':'function','function':{'name':'test_tool','parameters':{'type':'object'}}}])
        r=await self.post('/v1/chat/completions',b); self.assertEqual(r.status,422)
        for m in self.s.models.values(): m.tools_verified=True
        r=await self.post('/v1/chat/completions',b); self.assertEqual(r.status,200,await r.text())
        self.assertEqual((await r.json())['choices'][0]['message']['tool_calls'][0]['function']['name'],'test_tool')
    async def test_upstream_faults_no_fake_success(self):
        for runtime in ('ollama','lmstudio'):
            for text in ('[before]','[malformed]','[empty]','[oversize]','[mid]'):
                r=await self.post('/v1/chat/completions',request(text,model=f'{runtime}/fixture-chat-0'))
                self.assertIn(r.status,(502,503),(runtime,text,await r.text()))
        self.assertEqual(sum(m.active for m in self.s.models.values()),0)
    async def test_late_stream_error_no_done(self):
        r=await self.post('/v1/chat/completions',request('[mid]',stream=True)); t=await r.text()
        self.assertIn('partial output was not retried',t); self.assertNotIn('[DONE]',t)
        self.assertEqual(self.s.journal.get(r.headers['X-Loqalyn-Request-Id'])['status'],'failed')
    async def test_schema_failure_is_failure(self):
        r=await self.post('/v1/chat/completions',request(response_format={'type':'json_object'}))
        self.assertEqual(r.status,502); self.assertEqual(self.s.journal.recent()[0]['status'],'failed')
    async def test_cancel_releases(self):
        r=await self.post('/v1/chat/completions',request('[slow]',stream=True)); await r.content.read(10); r.close()
        await asyncio.sleep(.15)
        self.assertEqual(sum(m.active for m in self.s.models.values()),0)
        self.assertEqual(self.s.inflight['inference'],0)
    async def test_overload_and_recovery(self):
        tasks=[asyncio.create_task(self.post('/v1/chat/completions',request('[slow]'))) for _ in range(64)]
        await asyncio.sleep(.1)
        r=await self.client.get('/health'); self.assertEqual(r.status,200)
        statuses=[]
        for task in tasks:
            r=await task; statuses.append(r.status); await r.read()
        self.assertIn(503,statuses); self.assertLessEqual(self.s.inflight['inference'],32)
        self.assertEqual(self.s.inflight['inference'],0); self.assertEqual(sum(m.active for m in self.s.models.values()),0)
        r=await self.post('/v1/chat/completions'); self.assertEqual(r.status,200)
    async def test_overall_deadline_closes_slow_upstream(self):
        p=copy.deepcopy(self.s.config.data); p['request_timeout']=1; self.s.config.save(p)
        self.ollama.delay=2
        start=time.monotonic()
        r=await self.post('/v1/chat/completions',request(model='ollama/fixture-chat-0'))
        self.assertEqual(r.status,503); await r.read()
        self.assertLess(time.monotonic()-start,1.6)
        self.assertEqual(sum(m.active for m in self.s.models.values()),0)
        self.assertEqual(self.s.inflight['inference'],0)

    async def test_load_failure_reserves_and_blocks(self):
        self.ollama.fail_load=True
        r=await self.post('/v1/chat/completions',request(model='ollama/fixture-chat-0')); self.assertEqual(r.status,502)
        self.assertTrue(self.s.models['ollama/fixture-chat-0'].state_uncertain)
        r=await self.post('/v1/chat/completions',request(model='lmstudio/fixture-chat-0')); self.assertEqual(r.status,503)
        self.ollama.fail_load=False
        r=await self.post('/loqalyn/recover',{'runtime':'ollama','confirmed_runtime_restarted':True}); self.assertEqual(r.status,200)
        r=await self.post('/v1/chat/completions'); self.assertEqual(r.status,200)
    async def test_external_instance_not_unloaded(self):
        self.ollama.loaded['fixture-chat-0']=8192; await self.s.refresh()
        r=await self.post('/loqalyn/models/unload',{'model':'ollama/fixture-chat-0'}); self.assertEqual(r.status,409)
        self.assertEqual(self.ollama.unloads,0)
    async def test_active_model_not_evicted(self):
        r=await self.post('/v1/chat/completions',request('[slow]',stream=True)); await r.content.read(5)
        d=self.s.journal.get(r.headers['X-Loqalyn-Request-Id'])
        unload=await self.post('/loqalyn/models/unload',{'model':d['model']}); self.assertEqual(unload.status,409)
        r.close(); await asyncio.sleep(.1); self.assertEqual(self.ollama.evicted_active+self.lm.evicted_active,0)
    async def test_approval_off_blocks_old_pending(self):
        d=self.s.decision(request('prove a theorem')); self.s.queue(d,request('prove a theorem'))
        p=copy.deepcopy(self.s.config.data); p['escalation_mode']='off'; self.s.config.save(p)
        r=await self.post('/loqalyn/escalations/'+d['id'],{'action':'preview','provider':'openai','model':'test','payload':request()})
        self.assertEqual(r.status,403)
    async def test_approval_tampering_and_sensitive_content(self):
        d=self.s.decision(request('prove a theorem')); self.s.queue(d,request())
        payload={'action':'preview','provider':'openai','model':'test','payload':request('user@example.test')}
        r=await self.post('/loqalyn/escalations/'+d['id'],payload); preview=await r.json(); self.assertIn('email',preview['tags'])
        payload.update(action='approve',confirmed=True,hash=preview['hash']); r=await self.post('/loqalyn/escalations/'+d['id'],payload); self.assertEqual(r.status,403)
        payload['payload']=request('safe'); r=await self.post('/loqalyn/escalations/'+d['id'],payload); self.assertEqual(r.status,403)
    async def test_approved_send_exactly_once_without_external_network(self):
        import types
        from aiohttp import web
        received=[]
        async def provider(r):
            received.append(await r.json()); await asyncio.sleep(.05)
            return web.json_response({'choices':[{'message':{'content':'approved fixture'}}]})
        peer=web.Application(); peer.router.add_post('/fixture',provider)
        server=TestServer(peer); await server.start_server()
        d=self.s.decision(request('prove a theorem')); self.s.queue(d,request())
        body={'action':'preview','provider':'openai','model':'fixture-model','payload':request()}
        r=await self.post('/loqalyn/escalations/'+d['id'],body); preview=await r.json()
        body.update(action='approve',confirmed=True,hash=preview['hash'])
        real_post=self.s.session.post
        def local_post(url,**kwargs):
            self.assertEqual(url,'https://api.openai.com/v1/chat/completions')
            self.assertFalse(kwargs['allow_redirects'])
            return real_post(str(server.make_url('/fixture')),**kwargs)
        class FakeKeyring:
            def get_password(self,*args): return 'fixture-not-a-secret'
        try:
            with patch('loqalyn.server.sys.platform','darwin'),patch.dict('sys.modules',{'keyring.backends.macOS':types.SimpleNamespace(Keyring=FakeKeyring)}),patch.object(self.s.session,'post',side_effect=local_post):
                responses=await asyncio.gather(*(self.post('/loqalyn/escalations/'+d['id'],body) for _ in range(8)))
                self.assertEqual(sum(r.status==200 for r in responses),1)
                self.assertEqual(sum(r.status==409 for r in responses),7)
                for response in responses: await response.read()
            self.assertEqual(received,[preview['payload']])
            self.assertNotIn('payload',self.s.pending[d['id']])
            self.assertEqual(self.s.pending[d['id']]['status'],'approved')
        finally: await server.close()

    async def test_tool_streaming_both_protocols(self):
        for m in self.s.models.values(): m.tools_verified=True
        for runtime in ('ollama','lmstudio'):
            body=request(model=f'{runtime}/fixture-chat-0',stream=True,tools=[{'type':'function','function':{'name':'test_tool','parameters':{'type':'object'}}}])
            r=await self.post('/v1/chat/completions',body); text=await r.text()
            self.assertEqual(r.status,200); self.assertIn('test_tool',text); self.assertIn('[DONE]',text)
            body['tools']=[{'name':'test_tool','input_schema':{'type':'object'}}]
            r=await self.post('/v1/messages',body); text=await r.text()
            self.assertEqual(r.status,200,text); self.assertIn('input_json_delta',text); self.assertIn('message_stop',text)

    async def test_denial_cannot_reopen(self):
        d=self.s.decision(request('prove a theorem')); self.s.queue(d,request())
        r=await self.post('/loqalyn/escalations/'+d['id'],{'action':'deny'}); self.assertEqual(r.status,200)
        self.s.queue(d,request()); self.assertEqual(self.s.pending[d['id']]['status'],'denied'); self.assertNotIn('payload',self.s.pending[d['id']])
    async def test_expiry_on_state_access(self):
        d=self.s.decision(request('prove a theorem')); self.s.queue(d,request()); self.s.pending[d['id']]['created']=time.time()-1900
        self.s.state(); self.assertNotIn(d['id'],self.s.pending)
        self.s.queue(d,request()); self.assertNotIn(d['id'],self.s.pending)
    async def test_approval_queue_bounded(self):
        for i in range(80):
            d=self.s.decision(request('prove a theorem')); self.s.queue(d,request())
        self.assertLessEqual(len(self.s.pending),32)
    async def test_control_size_and_load_validation(self):
        r=await self.post('/loqalyn/models/load',{'model':'ollama/fixture-chat-0','context':True}); self.assertEqual(r.status,400)
    async def test_log_no_request_content(self):
        r=await self.post('/v1/chat/completions',request('PRIVATE-SENTINEL-927')); await r.read()
        self.assertNotIn('PRIVATE-SENTINEL-927',json.dumps(self.s.journal.recent()))
        self.assertNotIn(b'PRIVATE-SENTINEL-927',(self.path/'decisions.sqlite3').read_bytes())


if __name__=='__main__': unittest.main()
