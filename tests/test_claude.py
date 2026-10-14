import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from loqalyn.adapters import anthropic_request, Adapter
from loqalyn.claude import TaskPins, task_identity, coding_status, launch_environment, launch_arguments, write_launcher, CONTEXT
from loqalyn.core import Config, Journal, Model, Fault, DEFAULTS, atomic_yaml, inspect_payload
from loqalyn.security import api_token, tool_schema
from loqalyn.server import create_app
from tests.fixtures import Runtime


def body(**kwargs):
    return {'model':'auto-code','max_tokens':128,'messages':[{'role':'user','content':'Fix this Python function.'}],**kwargs}


def identity(prompt='one',**kwargs):
    return task_identity({'x-claude-code-session-id':'session-one','x-claude-code-prompt-id':prompt,**kwargs})


class TaskPinTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)
        self.journal=Journal(self.path/'decisions.sqlite3'); self.pins=TaskPins(self.journal.db)
        self.policy=copy.deepcopy(DEFAULTS); self.policy.update(budget_gb=24,headroom_gb=4,runtime_local_only_confirmed=True)
        self.models={}
        for name,size in [('code-a',1),('code-b',2)]:
            self.models[name]=Model(id=name,name=name,handle=name,runtime='ollama',size_gb=size,context_max=131072,
                capabilities=['chat','code','tools'],kv_gb_per_1k=.01,tools_verified=True,revision='a'*64,tools_verified_revision='a'*64)
        self.adapters={'ollama':SimpleNamespace(url='http://127.0.0.1:11434')}
    def tearDown(self): self.journal.db.close(); self.temp.cleanup()
    def decide(self,id=None,**kwargs): return self.pins.decide(id or identity(),body(**kwargs),self.models,self.policy,self.adapters)

    def test_stable_across_warmth_and_preference_changes_then_new_task_reselects(self):
        first=self.decide(); chosen=first['model']; other=next(m for m in self.models.values() if m.id!=chosen)
        other.loaded=True; other.managed=True; other.loaded_context=CONTEXT; other.memory_gb=3
        for r in self.policy['routes']: r['prefer']=[other.id]
        again=self.decide(model='auto-fast')
        self.assertEqual(chosen,again['model']); self.assertTrue(again['task_reused'])
        self.assertEqual(other.id,self.decide(identity('two'))['model'])

    def test_persisted_on_restart_without_prompt_logging(self):
        selected=self.decide(); self.journal.db.close(); self.journal=Journal(self.path/'decisions.sqlite3'); self.pins=TaskPins(self.journal.db)
        self.assertEqual(selected['model'],self.decide()['model'])
        data=(self.path/'decisions.sqlite3').read_bytes()
        self.assertNotIn(b'Fix this Python',data); self.assertNotIn(b'session-one',data)

    def test_changed_removed_offline_or_unverified_never_falls_back(self):
        chosen=self.decide()['model']; model=self.models[chosen]
        for attribute,value in [('revision','b'*64),('available',False),('tools_verified',False),('tools_verified_revision','b'*64),('tier','archive')]:
            old=getattr(model,attribute); setattr(model,attribute,value)
            with self.assertRaises(Fault): self.decide()
            setattr(model,attribute,old)
        del self.models[chosen]
        with self.assertRaises(Fault): self.decide()

    def test_runtime_address_change_refused(self):
        self.decide(); self.adapters['ollama'].url='http://127.0.0.1:12434'
        with self.assertRaises(Fault): self.decide()

    def test_compaction_without_prompt_hint_uses_current_agent_task(self):
        first=self.decide(); other=self.decide(identity('child',**{'x-claude-code-agent-id':'child'}))
        compact=task_identity({'x-claude-code-session-id':'session-one','x-claude-code-request-class':'compaction'})
        result=self.decide(compact)
        self.assertEqual(first['task'],result['task']); self.assertEqual(first['model'],result['model'])
        missing=task_identity({'x-claude-code-session-id':'different','x-claude-code-request-class':'compaction'})
        with self.assertRaises(Fault): self.decide(missing)

    def test_separate_task_agent_session_auxiliary_and_fallback_keys(self):
        base=identity()
        keys=[base['key'],identity('two')['key'],identity(**{'x-claude-code-agent-id':'child'})['key'],
            identity(**{'x-claude-code-session-id':'session-two'})['key'],identity(**{'x-claude-code-request-class':'auxiliary'})['key']]
        self.assertEqual(5,len(set(keys)))
        self.assertEqual(base['key'],identity(**{'x-claude-code-request-class':'compaction'})['key'])
        self.assertEqual('session',task_identity({'x-claude-code-session-id':'one'})['mode'])
        with self.assertRaises(Fault): task_identity({},required=True)
        with self.assertRaises(Fault): identity(**{'x-claude-code-prompt-id':'x'*129})
        with self.assertRaises(Fault): identity(**{'x-claude-code-session-id':'bad\r\nheader'})

    def test_capacity_keeps_existing_task_and_refuses_new(self):
        self.decide()
        with patch('loqalyn.claude.MAX_TASKS',1):
            self.assertTrue(self.decide()['task_reused'])
            with self.assertRaises(Fault): self.decide(identity('new'))

    def test_external_models_remain_in_memory_accounting(self):
        self.models['external']=Model(id='external',name='external',runtime='lmstudio',handle='external',loaded=True,memory_gb=19,managed=False)
        with self.assertRaises(Fault): self.decide()
        self.assertFalse(coding_status(self.models,self.policy)['ready'])

    def test_context_limit_is_explicit_and_never_truncated(self):
        with self.assertRaisesRegex(Fault,'prompt is too long'):
            self.decide(messages=[{'role':'user','content':'z'*CONTEXT}])
        self.assertEqual([],self.pins.recent())

    def test_unverified_revision_not_eligible(self):
        for m in self.models.values(): m.tools_verified_revision=None
        with self.assertRaises(Fault): self.decide()

    def test_coding_profile_does_not_queue_cloud_suggestions(self):
        self.policy['escalation_mode']='suggest'
        for r in self.policy['routes']: r['escalate']='suggest'
        self.assertIsNone(self.decide()['escalation'])


class CompatibilityTests(unittest.TestCase):
    def test_cache_hints_and_actual_claude_schemas(self):
        schema={'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','properties':{'limit':{'type':'integer','exclusiveMinimum':0},'value':{'anyOf':[{'type':'string'},{'type':'null'}]}},'required':['value']}
        value=anthropic_request({'model':'auto-code','max_tokens':256,'stream':True,
            'system':[{'type':'text','text':'System instructions','cache_control':{'type':'ephemeral','ttl':'1h'}}],
            'messages':[{'role':'user','content':[{'type':'text','text':'Read a file.','cache_control':{'type':'ephemeral'}}]}],
            'tools':[{'name':'Read','description':'Read files','input_schema':schema,'cache_control':{'type':'ephemeral'}}],
            'output_config':{'effort':'high'}},local_coding=True)
        self.assertFalse(value['thinking']); inspect_payload(value,DEFAULTS)
        model=Model(id='test',name='test',runtime='ollama',handle='test',loaded_context=65536)
        native=Adapter('ollama','http://127.0.0.1:11434',None).native_body(model,value)
        self.assertFalse(native['think']); self.assertEqual(schema,native['tools'][0]['function']['parameters'])

    def test_tool_history_round_trip_and_errors(self):
        b={'model':'auto-code','max_tokens':16,'messages':[
            {'role':'user','content':'Read.'},
            {'role':'assistant','content':[{'type':'tool_use','id':'a','name':'Read','input':{'path':'x'},'cache_control':{'type':'ephemeral'}}]},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'a','content':[{'type':'text','text':'denied'}],'is_error':True,'cache_control':{'type':'ephemeral'}},{'type':'text','text':'Try something else.'}]}]}
        value=anthropic_request(b,local_coding=True)
        self.assertEqual('Tool error: denied',value['messages'][2]['content'])
        self.assertEqual('Try something else.',value['messages'][3]['content'])

    def test_unsupported_features_fail_explicitly(self):
        for extra in ({'thinking':{'type':'enabled','budget_tokens':1024}},{'output_config':{'format':{'type':'json_schema'}}},{'context_management':{}},{'tool_choice':{'type':'tool','name':'Read'}}):
            with self.assertRaises(Fault): anthropic_request(body(**extra),local_coding=True)
        with self.assertRaises(Fault): anthropic_request(body(messages=[{'role':'user','content':[{'type':'image','source':{'type':'url','url':'https://example.com'}}]}]),local_coding=True)

    def test_tool_schemas_cannot_fetch_references_or_expand_at_gateway(self):
        for schema in ({'type':'object','$ref':'https://example.com/a'},{'type':'object','$ref':'file:///etc/passwd'},{'type':'object','description':'x'*150000}):
            with self.assertRaises(ValueError): tool_schema(schema)
        tool_schema({'type':'object','$defs':{'a':{'type':'object','properties':{'again':{'$ref':'#/$defs/a'}}}},'properties':{'x':{'$ref':'#/$defs/a'}}})

    def test_profile_isolated_without_credentials_or_permission_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=launch_environment(tmp,8741,environ={'HOME':'/home/user','PATH':'/bin','ANTHROPIC_API_KEY':'secret-cloud','OPENAI_API_KEY':'other-secret','CLAUDE_CODE_USE_BEDROCK':'1','ANTHROPIC_BASE_URL':'https://bad.example','HTTPS_PROXY':'http://proxy','PYTHONPATH':'/tmp/poison'})
            self.assertEqual('http://127.0.0.1:8741',env['ANTHROPIC_BASE_URL'])
            self.assertNotIn('secret-cloud',env.values()); self.assertNotIn('OPENAI_API_KEY',env)
            for name in ('CLAUDE_CODE_USE_BEDROCK','HTTPS_PROXY','PYTHONPATH'): self.assertNotIn(name,env)
            args=launch_arguments('/path/claude'); self.assertIn('manual',args); self.assertNotIn('bypassPermissions',args)
            self.assertEqual(0o600,Path(tmp,'claude-token').stat().st_mode&0o777)

    def test_launcher_quotes_project_paths_without_embedding_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)/'project $(touch nope)'; project.mkdir()
            launcher=write_launcher(tmp,8741,project,'/Users/example/Applications/Loqalyn.app/Contents/MacOS/Loqalyn')
            import shlex
            args=shlex.split(launcher.read_text().splitlines()[1])
            self.assertEqual(str(project.resolve()),args[-1]); self.assertNotIn('ANTHROPIC',launcher.read_text())
            self.assertEqual(0o700,launcher.stat().st_mode&0o777)


class CodingHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)
        self.runtime=await Runtime().start()
        config=Config(self.path); config.save({**config.data,'runtimes':{'ollama':self.runtime.url},'budget_gb':24,'headroom_gb':4,'runtime_local_only_confirmed':True})
        self.client=TestClient(TestServer(await create_app(self.path),handler_cancellation=True)); await self.client.start_server()
        self.s=self.client.server.app['service']
        self.admin={'Authorization':'Bearer '+self.s.token}
        self.headers={'Authorization':'Bearer '+self.s.claude_token,'x-claude-code-session-id':'test-session','x-claude-code-prompt-id':'first'}
        data={'models':{m.id:{'tools_verified':True} for m in self.s.models.values()}}
        for m in self.s.models.values(): self.s.task_pins.verify_model(m.id,m.revision)
        atomic_yaml(config.overrides,data); await self.s.refresh()
    async def asyncTearDown(self):
        await self.client.close(); await self.runtime.stop(); self.temp.cleanup()

    async def test_coding_credential_is_inference_only(self):
        for method,path in [('GET','/loqalyn/status'),('PUT','/loqalyn/policy'),('POST','/loqalyn/claude/reset'),('POST','/loqalyn/claude/verify'),('POST','/v1/chat/completions'),('GET','/switchyard/escalations')]:
            response=await self.client.request(method,path,headers=self.headers,json={})
            self.assertEqual(403,response.status,(path,await response.text()))
        response=await self.client.get('/v1/models',headers=self.headers)
        self.assertEqual(['auto-code','auto-fast'],[x['id'] for x in (await response.json())['data']])

    async def test_missing_identity_rejected_and_count_does_not_pin(self):
        response=await self.client.post('/v1/messages',headers={'x-api-key':self.s.claude_token},json=body())
        self.assertEqual(400,response.status)
        response=await self.client.post('/v1/messages/count_tokens',headers=self.headers,json={'model':'auto-code','messages':body()['messages']})
        self.assertEqual(200,response.status); self.assertGreater((await response.json())['input_tokens'],1)
        self.assertEqual([],self.s.task_pins.recent())

    async def test_concurrent_requests_pin_atomically(self):
        async def one():
            response=await self.client.post('/v1/messages',headers=self.headers,json=body(stream=True))
            text=await response.text(); self.assertEqual(200,response.status,text); self.assertIn('message_stop',text)
            return response.headers['X-Loqalyn-Model']
        models=await asyncio.gather(*(one() for _ in range(12)))
        self.assertEqual(1,len(set(models))); self.assertEqual(1,len(self.s.task_pins.recent()))
        self.assertEqual(12,self.s.task_pins.recent()[0]['requests'])
        self.assertEqual(0,self.runtime.evicted_active)

    async def test_reset_revokes_old_client_and_drops_pins(self):
        response=await self.client.post('/v1/messages',headers=self.headers,json=body()); self.assertEqual(200,response.status); await response.read()
        response=await self.client.post('/loqalyn/claude/reset',headers=self.admin,json={'confirmed':True}); self.assertEqual(200,response.status)
        self.assertEqual([],self.s.task_pins.recent())
        response=await self.client.post('/v1/messages',headers=self.headers,json=body()); self.assertEqual(401,response.status)

    async def test_burst_existing_tasks_stay_fixed_while_new_tasks_follow_policy(self):
        model_a,model_b=list(self.s.models)[:2]
        policy=copy.deepcopy(self.s.config.data)
        for r in policy['routes']:
            if r['name']=='code_edit': r['prefer']=[model_a]
        self.s.config.save(policy)
        gate=asyncio.Semaphore(8)
        async def run_task(number):
            async with gate:
                response=await self.client.post('/v1/messages',headers={**self.headers,'x-claude-code-prompt-id':str(number)},json=body(stream=bool(number%2)))
                await response.read(); self.assertEqual(200,response.status)
                return response.headers['X-Loqalyn-Model']
        first=await asyncio.gather(*(run_task(i) for i in range(32)))
        self.assertEqual({model_a},set(first))
        for r in policy['routes']:
            if r['name']=='code_edit': r['prefer']=[model_b]
        self.s.config.save(policy)
        second=await asyncio.gather(*(run_task(i) for i in list(range(32))*4+list(range(32,64))))
        self.assertEqual({model_a},set(second[:128])); self.assertEqual({model_b},set(second[128:]))
        self.assertEqual(64,len(list(self.s.journal.db.execute('SELECT key FROM claude_tasks'))))
        self.assertFalse(any(m.active for m in self.s.models.values())); self.assertEqual(0,self.runtime.evicted_active)

    async def test_context_error_has_anthropic_shape(self):
        response=await self.client.post('/v1/messages',headers=self.headers,json=body(messages=[{'role':'user','content':'x'*65536}]))
        self.assertEqual(400,response.status); error=await response.json()
        self.assertEqual('invalid_request_error',error['error']['type']); self.assertIn('prompt is too long',error['error']['message'])

    async def test_failed_tool_check_revokes_previous_verification(self):
        id=next(iter(self.s.models))
        response=await self.client.post('/loqalyn/claude/verify',headers=self.admin,json={'model':id})
        self.assertEqual(422,response.status)
        self.assertFalse(self.s.models[id].tools_verified)
        self.assertFalse(self.s.config.catalog_data['models'][id]['tools_verified'])

    async def test_successful_probe_records_revision_without_changing_catalogue_format(self):
        id=next(iter(self.s.models)); revision=self.s.models[id].revision
        self.s.task_pins.verify_model(id,None)
        async def chunks(model,payload,**kwargs):
            results=[m for m in payload['messages'] if m['role']=='tool']
            if results:
                delta={'content':results[-1]['content'].split(': ',1)[1]}; reason='stop'
            else:
                delta={'tool_calls':[{'index':0,'id':'probe','type':'function','function':{'name':'loqalyn_probe','arguments':'{"value":314159}'}}]}; reason='tool_calls'
            yield {'choices':[{'index':0,'delta':delta,'finish_reason':reason}]}
        with patch.object(self.s.adapters['ollama'],'chunks',chunks):
            response=await self.client.post('/loqalyn/claude/verify',headers=self.admin,json={'model':id})
            self.assertEqual(200,response.status,await response.text())
        self.assertEqual(revision,self.s.task_pins.verified_models()[id])
        self.assertTrue(self.s.models[id].tools_verified)
        self.assertEqual({'tools_verified':True},self.s.config.catalog_data['models'][id])

    async def test_gateway_restart_restores_same_client_credential_and_task(self):
        response=await self.client.post('/v1/messages',headers=self.headers,json=body()); await response.read()
        first=response.headers['X-Loqalyn-Model']; original_token=self.s.claude_token
        await self.client.close()
        self.client=TestClient(TestServer(await create_app(self.path),handler_cancellation=True)); await self.client.start_server()
        self.s=self.client.server.app['service']
        self.assertEqual(original_token,self.s.claude_token)
        response=await self.client.post('/v1/messages',headers=self.headers,json=body()); await response.read()
        self.assertEqual(200,response.status); self.assertEqual(first,response.headers['X-Loqalyn-Model'])
        self.assertEqual(2,self.s.task_pins.recent()[0]['requests'])

    async def test_client_cannot_enable_thinking_cloud_or_explicit_models(self):
        for extra in ({'thinking':{'type':'adaptive'}},{'model':'qwen-cloud'},{'model':'ollama/fixture-code-1'}):
            response=await self.client.post('/v1/messages',headers=self.headers,json=body(**extra))
            self.assertEqual(400,response.status,await response.text())
        self.assertFalse(self.s.pending)

    async def test_multiple_tool_blocks_are_sequential_and_invalid_calls_are_not_emitted(self):
        name='test_tool'
        async def chunks(model,payload,**kwargs):
            for index in range(2):
                yield {'choices':[{'index':0,'delta':{'tool_calls':[{'index':index,'id':'call'+str(index),'type':'function',
                    'function':{'name':name,'arguments':'{"value":1}'}}]},'finish_reason':None}]}
            yield {'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}]}
        request=body(stream=True,tools=[{'name':'test_tool','description':'Test','input_schema':{'type':'object','properties':{'value':{'type':'integer'}}}}])
        with patch.object(self.s.adapters['ollama'],'chunks',chunks):
            response=await self.client.post('/v1/messages',headers=self.headers,json=request)
            events=[json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
            blocks=[e for e in events if e['type'].startswith('content_block')]
            self.assertEqual(['content_block_start','content_block_delta','content_block_stop']*2,[b['type'] for b in blocks])
            self.assertEqual([0,0,0,1,1,1],[b['index'] for b in blocks])
            name='unrequested_tool'
            response=await self.client.post('/v1/messages',headers=self.headers,json=request)
            content=await response.text()
            self.assertIn('event: error',content); self.assertNotIn('content_block_start',content); self.assertNotIn('message_stop',content)

    async def test_failed_and_interrupted_requests_keep_pin(self):
        response=await self.client.post('/v1/messages',headers=self.headers,json=body(messages=[{'role':'user','content':'[before]'}]))
        self.assertEqual(502,response.status)
        first=self.s.task_pins.recent()[0]['model']
        response=await self.client.post('/v1/messages',headers=self.headers,json=body(stream=True,messages=[{'role':'user','content':'[mid]'}]))
        self.assertIn('event: error',await response.text())
        self.assertEqual(first,self.s.task_pins.recent()[0]['model'])
        self.assertFalse(any(m.active for m in self.s.models.values()))
