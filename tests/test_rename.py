"""Upgrade compatibility, with real profile persistence and HTTP boundaries."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from loqalyn.core import Config
from loqalyn.desktop import DesktopController, DesktopError
from loqalyn.identity import profile_directory, setting, keychain_password
from loqalyn.security import api_token
from loqalyn.server import create_app
from tests.fixtures import Runtime


class ProfileCompatibilityTests(unittest.TestCase):
    def test_new_profile_and_existing_profile_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)
            for native in (False, True):
                root=home/'Library'/'Application Support' if native else home
                current=root/('Loqalyn' if native else '.loqalyn')
                legacy=root/('Switchyard' if native else '.switchyard')
                kwargs={'native':native,'home':home,'environ':{}}
                self.assertEqual(profile_directory(**kwargs),current)
                legacy.mkdir(parents=True)
                self.assertEqual(profile_directory(**kwargs),legacy)
                current.mkdir()
                self.assertEqual(profile_directory(**kwargs),current)

    def test_environment_precedence_and_empty_override(self):
        self.assertEqual(setting('PORT',environ={'SWITCHYARD_PORT':'1234'}),'1234')
        self.assertEqual(setting('PORT',environ={'SWITCHYARD_PORT':'1234','LOQALYN_PORT':'5678'}),'5678')
        self.assertEqual(setting('LM_TOKEN',environ={'SWITCHYARD_LM_TOKEN':'old','LOQALYN_LM_TOKEN':''}),'')
        self.assertEqual(profile_directory(environ={'SWITCHYARD_DIR':'/legacy'}),Path('/legacy'))
        self.assertEqual(profile_directory(environ={'SWITCHYARD_DIR':'/legacy','LOQALYN_DIR':'/current'}),Path('/current'))
        with self.assertRaises(ValueError): profile_directory(environ={'LOQALYN_DIR':''})

    def test_existing_profile_token_and_settings_survive_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp); legacy=home/'Library'/'Application Support'/'Switchyard'
            config=Config(legacy)
            config.data.update(runtimes={'ollama':'http://127.0.0.1:1'},runtime_local_only_confirmed=True,escalation_mode='off',budget_gb=7)
            config.save(config.data); token=api_token(legacy/'token')
            policy_before=(legacy/'policy.yaml').read_bytes()
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            with patch.dict(os.environ,{},clear=True),patch('loqalyn.identity.Path.home',return_value=home):
                controller=DesktopController(port=port,use_keychain=False)
                try:
                    state=controller.start()
                    self.assertEqual(controller.data_dir,legacy)
                    self.assertEqual(controller.token,token)
                    self.assertEqual(state['policy']['budget_gb'],7)
                    self.assertEqual((legacy/'policy.yaml').read_bytes(),policy_before)
                    self.assertFalse((legacy.parent/'Loqalyn').exists())
                finally: controller.stop()

    def test_profile_symlink_cannot_silently_reset_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp); root=home/'Library'/'Application Support'; root.mkdir(parents=True)
            unsafe=root/'Loqalyn'; unsafe.symlink_to(home/'missing')
            (root/'Switchyard').mkdir()
            self.assertEqual(profile_directory(native=True,home=home,environ={}),unsafe)
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            controller=DesktopController(unsafe,port,use_keychain=False)
            with self.assertRaises(DesktopError): controller.start()

    def test_keychain_fallback_never_copies_or_overwrites(self):
        ring=Mock(); ring.get_password.side_effect=[None,'legacy-fixture']
        self.assertEqual(keychain_password(ring,'lmstudio'),'legacy-fixture')
        self.assertEqual([x.args for x in ring.get_password.call_args_list],[('loqalyn','lmstudio'),('switchyard','lmstudio')])
        ring.set_password.assert_not_called()
        ring=Mock(); ring.get_password.return_value='new-fixture'
        self.assertEqual(keychain_password(ring,'lmstudio'),'new-fixture')
        ring.get_password.assert_called_once_with('loqalyn','lmstudio')


class APICompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.runtime=await Runtime().start()
        path=Path(self.temp.name)/'profile'; config=Config(path)
        config.data.update(runtimes={'ollama':self.runtime.url},runtime_local_only_confirmed=True,budget_gb=12,headroom_gb=1)
        config.save(config.data); self.app=await create_app(path)
        self.client=TestClient(TestServer(self.app)); await self.client.start_server()
        self.auth={'Authorization':'Bearer '+self.app['service'].token}

    async def asyncTearDown(self):
        await self.client.close(); await self.runtime.stop(); self.temp.cleanup()

    async def test_both_names_share_state_and_enforce_privacy(self):
        for prefix in ('/loqalyn','/switchyard'):
            for path in ('/status','/hub/search','/hub/inspect','/hub/install','/jobs/fixture/cancel'):
                method='GET' if path=='/status' else 'POST'
                r=await self.client.request(method,prefix+path,json={})
                self.assertEqual(r.status,401)
            r=await self.client.get(prefix+'/status',headers={**self.auth,'Origin':'https://evil.invalid'})
            self.assertEqual(r.status,403)
            r=await self.client.post(prefix+'/hub/install',json={},headers=self.auth)
            self.assertEqual(r.status,403)
            self.assertEqual((await r.json())['error']['code'],'confirmation_required')
        r=await self.client.get('/switchyard/policy',headers=self.auth); policy=await r.json()
        policy['budget_gb']=9
        r=await self.client.put('/switchyard/policy',json=policy,headers=self.auth); self.assertEqual(r.status,200)
        r=await self.client.get('/loqalyn/policy',headers=self.auth); self.assertEqual((await r.json())['budget_gb'],9)

    async def test_generation_returns_both_receipts_and_legacy_lookup(self):
        r=await self.client.post('/v1/chat/completions',headers=self.auth,json={'model':'auto','messages':[{'role':'user','content':'Hello'}],'max_tokens':16})
        self.assertEqual(r.status,200); response=await r.json()
        request_id=r.headers['X-Loqalyn-Request-Id']
        self.assertEqual(r.headers['X-Switchyard-Request-Id'],request_id)
        self.assertEqual(r.headers['X-Switchyard-Model'],r.headers['X-Loqalyn-Model'])
        old=await self.client.get('/switchyard/decisions/'+request_id,headers=self.auth)
        new=await self.client.get('/loqalyn/decisions/'+request_id,headers=self.auth)
        self.assertEqual(await old.json(),await new.json())
        self.assertEqual((await new.json())['model'],response['model'])
