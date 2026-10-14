"""Second alpha review: concurrent discovery, privacy and download boundaries."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from loqalyn.adapters import Adapter
from loqalyn.core import Config, Fault, Journal, Model, route, DEFAULTS
from loqalyn.desktop_presenter import StreamBuffer
from loqalyn.hub import HubCatalogue, repository, gguf_file
from loqalyn.security import loads
from loqalyn.server import create_app, post_check, start_download
from tests.fixtures import Runtime


def body(text='hello', **kw):
    return {'messages':[{'role':'user','content':text}], 'max_tokens':16, **kw}


def hub_metadata():
    return {'id':'example/small-GGUF','sha':'a'*40,'tags':['license:mit'], 'siblings':[
        {'rfilename':'model-Q4_K_M.gguf','lfs':{'size':2*1024**2,'sha256':'b'*64}},
        {'rfilename':'model-Q8_0.gguf','lfs':{'size':4*1024**2,'sha256':'c'*64}},
        {'rfilename':'mmproj-F16.gguf','lfs':{'size':2*1024**2,'sha256':'d'*64}},
        {'rfilename':'model-00001-of-00002.gguf','lfs':{'size':2*1024**2,'sha256':'e'*64}},
        {'rfilename':'nested/model.gguf','lfs':{'size':2*1024**2,'sha256':'f'*64}}]}


class ReviewPureTests(unittest.TestCase):
    def test_utf8_byte_limit(self):
        with self.assertRaises(ValueError): loads('"'+'€'*10+'"',limit=15)
        self.assertEqual(loads('"hello"',limit=7),'hello')

    def test_zero_retention_disables_new_records(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Journal(Path(temp)/'j.sqlite')
            try:
                journal.prune(0); journal.event('test',{}); journal.decision({'id':'test','created':time.time()})
                self.assertEqual(journal.recent(),[])
                self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
            finally: journal.db.close()

    def test_retention_while_running_and_policy_change(self):
        with tempfile.TemporaryDirectory() as temp:
            journal=Journal(Path(temp)/'j.sqlite')
            try:
                journal.decision({'id':'old','created':time.time()-2*86400})
                journal.maintain(1)
                self.assertIsNone(journal.get('old'))
                journal.decision({'id':'old','created':time.time()-2*86400})
                journal.last_prune=time.monotonic()-61; journal.maintain(1)
                self.assertIsNone(journal.get('old'))
            finally: journal.db.close()

    def test_json_object_rejects_array_and_scalar(self):
        class Service:
            def queue(self,*args): pass
        for content in ('[]','null','42','"hi"'):
            with self.assertRaises(Fault): post_check(Service(),{}, {'response_format':{'type':'json_object'}}, {'choices':[{'message':{'content':content}}]})
        post_check(Service(),{}, {'response_format':{'type':'json_object'}}, {'choices':[{'message':{'content':'{}'}}]})

    def test_answer_first_reasoning_optional(self):
        stream=StreamBuffer(); stream.add({'choices':[{'delta':{'reasoning_content':'thinking'}}]})
        self.assertEqual(stream.text(),'')
        stream.add({'choices':[{'delta':{'content':'answer'}}]})
        self.assertEqual(stream.text(),'answer')
        self.assertEqual(stream.text(True),'answer\n\nMODEL REASONING\nthinking')

    def test_memory_explanation_and_protected_sum(self):
        models={'a':Model('a','a','ollama','a',size_gb=2,kv_gb_per_1k=.01,context_max=8192,loaded=True,memory_gb=3,loaded_context=8192),
                'b':Model('b','b','ollama','b',size_gb=8,context_max=8192)}
        policy={**DEFAULTS,'budget_gb':6,'headroom_gb':1}
        result=route(body(),models,policy)
        self.assertEqual(result['model'],'a')
        excluded=next(x for x in result['exclusions'] if x['id']=='b')
        self.assertEqual(excluded['available_gb'],2)
        self.assertIn('memory_budget',excluded['reasons'])

    def test_repository_and_file_boundaries(self):
        for value in ('https://evil.test/x','../model','owner/../model','owner/model?x','a/b/c','owner/..','a/b\n'):
            with self.assertRaises(Fault): repository(value)
        for value in ('../model.gguf','model.py','mmproj.gguf','x-00001-of-00002.gguf','x/y.gguf'):
            self.assertFalse(gguf_file(value))
        self.assertTrue(gguf_file('Model-Q4_K_M.gguf'))


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub=HubCatalogue(None); self.hub._get=AsyncMock(return_value=hub_metadata())

    async def test_preview_only_supported_files(self):
        result=await self.hub.inspect('example/small-GGUF')
        self.assertEqual(len(result['files']),2)
        self.assertEqual(result['files'][0]['quant'],'Q4_K_M')
        self.assertEqual(result['license'],'mit')
        file,repo=await self.hub.selection(result['id'],result['files'][0]['filename'])
        self.assertEqual(file['sha256'],'b'*64)
        self.assertEqual(repo,'example/small-GGUF')

    async def test_stale_or_tampered_preview(self):
        p=await self.hub.inspect('example/small-GGUF')
        with self.assertRaises(Fault): await self.hub.selection(p['id'],'arbitrary.gguf')
        self.hub.previews[p['id']]['expires']=0
        with self.assertRaises(Fault): await self.hub.selection(p['id'],p['files'][0]['filename'])

    async def test_revision_change_requires_new_review(self):
        p=await self.hub.inspect('example/small-GGUF'); data=hub_metadata(); data['sha']='f'*40
        self.hub._get.return_value=data
        with self.assertRaisesRegex(Fault,'changed'): await self.hub.selection(p['id'],p['files'][0]['filename'])

    async def test_missing_digest_gated_and_overlarge_files(self):
        for change in ('gated','digest','size'):
            data=hub_metadata()
            if change=='gated': data['gated']='auto'
            else:
                data['siblings']=data['siblings'][:1]
                if change=='digest': data['siblings'][0]['lfs'].pop('sha256')
                else: data['siblings'][0]['lfs']['size']=200*1024**3
            self.hub._get.return_value=data
            with self.assertRaises(Fault): await self.hub.inspect('example/small-GGUF')

    async def test_preview_cache_bounded(self):
        for _ in range(40): await self.hub.inspect('example/small-GGUF')
        self.assertEqual(len(self.hub.previews),32)

    async def test_search_only_fixed_host_and_query_parameters(self):
        self.hub._get.return_value=[{'id':'example/model','downloads':10,'tags':['license:mit']},{'id':'private/model','private':True}]
        result=await self.hub.search('hello & sort=evil')
        self.assertEqual(len(result['models']),1)
        args=self.hub._get.call_args.args
        self.assertEqual(args[0],'/api/models'); self.assertEqual(args[1]['search'],'hello & sort=evil')
        self.assertEqual(args[1]['limit'],'20')


class ReviewGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.peer=await Runtime().start()
        config=Config(Path(self.temp.name)/'profile'); config.data.update(runtimes={'ollama':self.peer.url},runtime_local_only_confirmed=True,budget_gb=12,headroom_gb=1)
        config.save(config.data); self.app=await create_app(config.path); self.s=self.app['service']
        self.client=TestClient(TestServer(self.app,handler_cancellation=True)); await self.client.start_server()
        self.auth={'Authorization':'Bearer '+self.s.token}

    async def asyncTearDown(self):
        await self.client.close(); await self.peer.stop(); self.temp.cleanup()

    async def test_cloud_enabled_blocks_before_model_load(self):
        self.peer.cloud_disabled=False
        r=await self.client.post('/v1/chat/completions',json=body(),headers=self.auth)
        self.assertEqual(r.status,403,await r.text()); self.assertEqual(self.peer.loads,0); self.assertEqual(self.peer.generations,0)

    async def test_cloud_reenabled_blocks_warm_model(self):
        r=await self.client.post('/v1/chat/completions',json=body(),headers=self.auth); self.assertEqual(r.status,200)
        self.peer.cloud_disabled=False
        r=await self.client.post('/v1/chat/completions',json=body(),headers=self.auth)
        self.assertEqual(r.status,403); self.assertEqual(self.peer.generations,1)

    async def test_refresh_does_not_block_release_or_overwrite_load(self):
        ident='ollama/fixture-chat-0'; entered=asyncio.Event(); finish=asyncio.Event()
        adapter=self.s.adapters['ollama']; stale=await adapter.discover()
        async def delayed(): entered.set(); await finish.wait(); return stale
        with patch.object(adapter,'discover',delayed):
            refresh=asyncio.create_task(self.s.refresh()); await entered.wait()
            await asyncio.wait_for(self.s.ledger.acquire(ident,1024),.5)
            await asyncio.wait_for(self.s.ledger.release(ident),.1)
            finish.set(); await refresh
        self.assertTrue(self.s.models[ident].loaded); self.assertTrue(self.s.models[ident].managed)
        self.assertEqual(self.s.models[ident].active,0)

    async def test_discovery_caches_metadata_not_privacy_checks(self):
        initial=self.peer.show_calls; await self.s.refresh(); await self.s.refresh()
        self.assertEqual(initial,self.peer.show_calls)
        self.peer.cloud_disabled=False; await self.s.refresh()
        self.assertFalse(self.s.health['ollama']['cloud_disabled'])

    async def test_explicit_attention_dimensions_set_kv_estimate(self):
        adapter=self.s.adapters['ollama']; original=adapter.json; adapter.metadata_cache.clear()
        async def metadata(method,path,*args,**kwargs):
            result=await original(method,path,*args,**kwargs)
            if path=='/api/show':
                result['model_info'].update({'fixture.attention.key_length':256,'fixture.attention.value_length':128})
            return result
        with patch.object(adapter,'json',metadata): models=await adapter._ollama_models()
        expected=16*4*(256+128)*2*1024/1024**3
        self.assertAlmostEqual(models[0].kv_gb_per_1k,expected)

    async def test_unverified_install_remains_blocked_across_reload(self):
        name='hf.co/example/model:model.gguf'
        self.peer.names.append(name)
        expected={'size_bytes':2*1024**2,'sha256':'b'*64}
        j=start_download(self.s,'ollama',name,expected,'example/model')
        await self.s.jobs[j['id']]['task']
        self.assertEqual(self.s.jobs[j['id']]['status'],'failed')
        await self.s.refresh()
        self.assertFalse(self.s.models['ollama/'+name].installation_verified)
        restored=Config(self.s.config.path)
        self.assertFalse(restored.catalog_data['models']['ollama/'+name]['installation_verified'])
        result=self.s.decision(body(model='ollama/'+name))
        self.assertIsNone(result['model'])
        with self.assertRaises(Fault): await self.s.ledger.acquire('ollama/'+name,1024)
        with patch.object(self.s.adapters['ollama'],'pull',AsyncMock(return_value={'status':'success'})):
            j=start_download(self.s,'ollama',name,expected,'example/model')
            await self.s.jobs[j['id']]['task']
        self.assertTrue(self.s.models['ollama/'+name].installation_verified)

    async def test_runtime_address_change_clears_old_inventory(self):
        policy=copy.deepcopy(self.s.config.data);policy['runtimes']['ollama']='http://127.0.0.1:1'
        r=await self.client.put('/loqalyn/policy',json=policy,headers=self.auth)
        self.assertEqual(r.status,200)
        self.assertEqual(self.s.models,{})
        self.assertIsNone(self.s.decision(body())['model'])

    async def test_stream_privacy_headers_sent_before_prepare(self):
        r=await self.client.post('/v1/chat/completions',json=body(stream=True),headers=self.auth)
        self.assertEqual(r.status,200)
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(r.headers['X-Content-Type-Options'],'nosniff')
        await r.read()

    async def test_hub_and_cancel_routes_need_auth(self):
        for path in ('/loqalyn/hub/search','/loqalyn/hub/inspect','/loqalyn/hub/install','/loqalyn/jobs/x/cancel'):
            r=await self.client.post(path,json={}); self.assertEqual(r.status,401)

    async def test_install_requires_confirmation_and_preview(self):
        for payload,expected in (({},403),({'confirmed':True,'preview_id':'madeup','filename':'x.gguf'},409)):
            r=await self.client.post('/loqalyn/hub/install',json=payload,headers=self.auth)
            self.assertEqual(r.status,expected); self.assertEqual(self.s.jobs,{})

    async def test_cancel_download_and_limit_duplicate_jobs(self):
        entered=asyncio.Event()
        async def blocked(*args,**kwargs): entered.set(); await asyncio.Future()
        with patch.object(self.s.adapters['ollama'],'pull',blocked):
            job=start_download(self.s,'ollama','test'); await entered.wait()
            with self.assertRaises(Fault): start_download(self.s,'ollama','test')
            r=await self.client.post('/loqalyn/jobs/'+job['id']+'/cancel',json={},headers=self.auth)
            self.assertEqual(r.status,200); self.assertEqual((await r.json())['status'],'cancelled')
            self.assertTrue(self.s.jobs[job['id']]['task'].done())

    async def test_stream_error_cannot_hold_lease_on_stalled_client(self):
        original=web.StreamResponse.write
        async def write(response,data):
            if b'Runtime stream failed' in data: await asyncio.Future()
            return await original(response,data)
        with patch.object(web.StreamResponse,'write',write):
            r=await self.client.post('/v1/chat/completions',json=body('[mid]',stream=True),headers=self.auth)
            try: await r.read()
            except aiohttp.ClientError: pass
            await asyncio.sleep(.05)
        self.assertEqual(sum(m.active for m in self.s.models.values()),0)
        self.assertEqual(self.s.inflight['inference'],0)
        r=await self.client.post('/v1/chat/completions',json=body(),headers=self.auth)
        self.assertEqual(r.status,200)


class PullTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, messages, expected, succeeds, blob_exists=True):
        async def handler(request):
            response=web.StreamResponse(); await response.prepare(request)
            for m in messages: await response.write(json.dumps(m).encode()+b'\n')
            return response
        app=web.Application(); app.router.add_post('/api/pull',handler)
        async def blob(request): return web.Response(status=200 if blob_exists else 404)
        app.router.add_head('/api/blobs/{digest}',blob)
        async with TestServer(app) as server, aiohttp.ClientSession() as session:
            adapter=Adapter('ollama',str(server.make_url('')).rstrip('/'),session)
            progress=[]
            if succeeds:
                result=await adapter.pull('hf.co/example/model:model.gguf',progress.append,expected)
                self.assertEqual(result['verified_sha256'],expected['sha256']); self.assertTrue(progress)
            else:
                with self.assertRaises(Fault): await adapter.pull('hf.co/example/model:model.gguf',progress.append,expected)

    async def test_success_requires_reviewed_digest_and_terminal_success(self):
        expected={'sha256':'b'*64,'size_bytes':2*1024**2}
        good={'status':'pulling','digest':'sha256:'+'b'*64,'total':2*1024**2,'completed':2*1024**2}
        await self.check([good,{'status':'success'}],expected,True)
        await self.check([good,{'status':'success'}],expected,False,blob_exists=False)
        await self.check([{**good,'completed':1024},{'status':'success'}],expected,True)
        await self.check([good],expected,False)
        await self.check([{'status':'success'}],expected,False)

    async def test_changed_digest_size_and_invalid_progress(self):
        expected={'sha256':'b'*64,'size_bytes':2*1024**2}
        good={'status':'pulling','digest':'sha256:'+'b'*64,'total':2*1024**2,'completed':0}
        for patch_value in ({'digest':'sha256:'+'a'*64},{'total':3*1024**2},{'completed':-1},{'completed':True}):
            await self.check([{**good,**patch_value},{'status':'success'}],expected,False)
