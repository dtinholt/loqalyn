"""Native bridge imports, lifecycle isolation, and presentation boundaries."""
import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from switchyard import cli
from switchyard.core import Config
from switchyard.desktop import DesktopController, DesktopError
from switchyard.desktop_presenter import ApprovalReview, request_payload, load_blocker, unload_blocker, StreamBuffer


class NativeBridgeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='darwin','Cocoa bridge requires macOS')
    def test_real_appkit_protocol_registration(self):
        # Import actual PyObjC, not mocks: catches signatures that abort launch.
        from switchyard.desktop_ui import DesktopApplication, TableSource, FlippedView
        self.assertIsNotNone(DesktopApplication)
        self.assertIsNotNone(TableSource)
        self.assertIsNotNone(FlippedView)


class DesktopTests(unittest.TestCase):
    def test_lifecycle_and_port_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'profile'; config=Config(path)
            # Unreachable local peer makes startup fast without touching real runtimes.
            config.data['runtimes']={'ollama':'http://127.0.0.1:1'}; config.save(config.data)
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            first=DesktopController(path,port,use_keychain=False)
            try:
                self.assertEqual(first.start()['mode'],'local')
                self.assertEqual(first.start()['active'],0)
                with self.assertRaises(DesktopError): DesktopController(Path(temp)/'other',port,use_keychain=False).start()
                self.assertEqual(first.call('/health')['ok'],True)
                for value in ('//evil.test','http://evil.test','/bad\n'):
                    with self.assertRaises(DesktopError): first.call(value)
            finally: first.stop()
            first.stop()
            with socket.socket() as sock: sock.bind(('127.0.0.1',port))

    def test_invalid_port(self):
        for value in (True,0,65536,'8741'):
            with self.assertRaises(ValueError): DesktopController(port=value)

    def test_approval_must_match_current_inputs(self):
        current={'request_id':'fixture','provider':'openai','model':'fixture','payload':{'messages':[{'role':'user','content':'hello'}]}}
        review=ApprovalReview(copy.deepcopy(current),{'hash':'fixture-hash','tags':[],'provider':'openai','model':'fixture'})
        self.assertTrue(review.approval_body(current,True)['confirmed'])
        with self.assertRaises(ValueError): review.approval_body(current,False)
        for key in ('request_id','provider','model','payload'):
            changed=copy.deepcopy(current); changed[key]='changed'
            with self.assertRaises(ValueError): review.approval_body(changed,True)
        sensitive=ApprovalReview(current,{'hash':'fixture-hash','tags':['email'],'provider':'openai','model':'fixture'})
        with self.assertRaises(ValueError): sensitive.approval_body(current,True)

    def test_playground_validation(self):
        for prompt,tokens in (('', '10'),('hello','nan'),('hello','-1'),('hello','131073')):
            with self.assertRaises(ValueError): request_payload(prompt,'auto',tokens)
        self.assertEqual(request_payload('hello','auto','32')['max_tokens'],32)

    def test_load_unload_explanations(self):
        model={'loaded':True,'active':1,'managed':True}
        self.assertIsNotNone(load_blocker(model,{'policy':{}}))
        self.assertIsNotNone(unload_blocker(model))
        model['active']=0; model['managed']=False
        self.assertIn('outside',unload_blocker(model))

    def test_stream_error_is_not_success(self):
        stream=StreamBuffer(); stream.add({'choices':[{'delta':{'content':'hello'}}]})
        self.assertEqual(stream.text(),'hello')
        with self.assertRaises(ValueError): stream.add({'error':{'message':'fixture error'}})

    def test_mcp_recovers_after_malformed_inputs(self):
        payloads=['[]','{"x":NaN}','{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}','{"jsonrpc":"2.0","id":2,"method":"ping"}','{"jsonrpc":"2.0","id":3,"method":"tools/list"}']
        out=io.StringIO()
        with patch('sys.stdin',io.StringIO('\n'.join(payloads)+'\n')),patch('sys.stdout',out): cli.mcp(1,Path('/unused'))
        result=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(all('error' in r for r in result[:3]))
        self.assertEqual(result[3]['result'],{})
        self.assertEqual(len(result[4]['result']['tools']),4)
        self.assertNotIn('approve',[x['name'] for x in result[4]['result']['tools']])

    def test_mcp_stops_on_oversized_message(self):
        out=io.StringIO()
        with patch('sys.stdin',io.StringIO('x'*(cli.MAX_BODY+2))),patch('sys.stdout',out): cli.mcp(1,Path('/unused'))
        self.assertEqual(json.loads(out.getvalue())['error']['code'],-32700)
