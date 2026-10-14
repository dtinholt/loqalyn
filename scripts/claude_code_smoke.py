#!/usr/bin/env python3
"""Real Claude Code client → Loqalyn → fixture or explicitly chosen local model.

Only a disposable project is editable. No cloud credentials, user settings,
permission bypasses, repository hooks or external MCP tools are inherited.
Published evidence contains outcomes and metadata, never prompts or credentials.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from loqalyn import __version__
from loqalyn.claude import client_info, find_claude, launch_arguments, launch_environment
from loqalyn.core import Config, atomic_yaml
from loqalyn.server import create_app
from tests.fixtures import Runtime

OLD='return price * percent / 100'
NEW='return price * (1 - percent / 100)'
SOURCE='def discounted(price, percent):\n    '+OLD+'\n'
TESTS='''import unittest
from prices import discounted
class Prices(unittest.TestCase):
    def test_twenty_percent(self): self.assertEqual(80, discounted(100, 20))
    def test_zero_percent(self): self.assertEqual(59, discounted(59, 0))
    def test_full_discount(self): self.assertEqual(0, discounted(81, 100))
    def test_fractional(self): self.assertAlmostEqual(8.991, discounted(9.99, 10))
'''


class CodingRuntime(Runtime):
    def __init__(self,project):
        super().__init__(); self.project=project; self.calls=[]
    async def chat(self,r):
        body=await r.json(); self.generations+=1
        results=[m for m in body['messages'] if m['role']=='tool']
        step=len(results)
        actions=[('Read',{'file_path':str(self.project/'prices.py')}),
                 ('Edit',{'file_path':str(self.project/'prices.py'),'old_string':OLD,'new_string':NEW}),
                 ('Bash',{'command':'./run_tests.sh','description':'Run the project tests'})]
        message={'role':'assistant','content':''}
        if step<len(actions):
            name,args=actions[step]; self.calls.append(name)
            message['tool_calls']=[{'function':{'name':name,'arguments':args}}]
        else: message['content']='Fixed the discount calculation. All four tests passed.'
        chunks=[{'message':message,'done':False},
                {'message':{'role':'assistant','content':''},'done':True,'done_reason':'stop','prompt_eval_count':1000,'eval_count':40}]
        return web.Response(text=''.join(json.dumps(c)+'\n' for c in chunks),content_type='application/x-ndjson')


async def run(args):
    info=client_info()
    if not info.get('compatible'): raise RuntimeError(info['message'])
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='loqalyn-claude-e2e-') as temporary:
        root=Path(temporary).resolve(); project=root/'project'; project.mkdir(); profile=root/'profile'
        (project/'prices.py').write_text(SOURCE)
        (project/'test_prices.py').write_text(TESTS)
        (project/'run_tests.sh').write_text('#!/bin/sh\nexec /usr/bin/python3 -m unittest -v\n'); (project/'run_tests.sh').chmod(0o700)
        runtime=await CodingRuntime(project).start() if args.mode=='fixture' else None
        config=Config(profile); config.save({**config.data,'runtimes':{'ollama':runtime.url if runtime else args.runtime},
            'budget_gb':24,'headroom_gb':4,'runtime_local_only_confirmed':True})
        app=await create_app(profile); server=TestServer(app,handler_cancellation=True); await server.start_server()
        service=app['service']; url=str(server.make_url('')).rstrip('/'); proc=None; started=time.monotonic()
        try:
            chosen='ollama/'+(runtime.names[1] if runtime else args.model)
            if chosen not in service.models: raise RuntimeError('Requested test model is not installed.')
            revision=service.models[chosen].revision
            # Keep other installed models out of test selection; preserve their
            # memory reservations and never change the user's production profile.
            overrides={id:{'tier':'on_demand' if id==chosen else 'archive'} for id in service.models}
            if runtime:
                overrides[chosen].update(tools_verified=True)
                service.task_pins.verify_model(chosen,revision)
            atomic_yaml(config.overrides,{'models':overrides}); await service.refresh()
            async with ClientSession(trust_env=False) as session:
                if not runtime:
                    async with session.post(url+'/loqalyn/claude/verify',headers={'Authorization':'Bearer '+service.token},json={'model':chosen}) as response:
                        verification=await response.json()
                        if response.status!=200: raise RuntimeError('Model tool verification failed: '+json.dumps(verification))
                env=launch_environment(profile,server.port)
                command=launch_arguments(find_claude())
                command += ['--restricted','--allowedTools','Read','Edit','Bash(./run_tests.sh)',
                    '--permission-prompts','none','--no-session-persistence','--output-format','json','-p',
                    'Fix the bug in prices.py. Read the file first. Only edit prices.py. Run ./run_tests.sh to verify all four existing tests. Do not change the tests or the test runner. Then briefly report the result.']
                proc=await asyncio.create_subprocess_exec(*command,cwd=project,env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await asyncio.wait_for(proc.communicate(),args.timeout)
                try: result=json.loads(out)
                except ValueError: result={'is_error':True,'parse_failure':out.decode()[-1000:]}
                check=await asyncio.create_subprocess_exec('/usr/bin/python3','-m','unittest','-v',cwd=project,
                    env={k:v for k,v in os.environ.items() if not k.startswith('PYTHON')},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                test_out,test_err=await check.communicate()
                tasks=service.task_pins.recent(); decisions=service.journal.recent()
                records=[d for d in decisions if d.get('client')=='claude_code']
                stable=bool(tasks) and all(len({d['model'] for d in records if d.get('task')==t['task']})==1 for t in tasks)
                report={'version':__version__,'mode':args.mode,'claude_code':info.get('version'),'model':chosen,'revision':revision,
                    'elapsed_seconds':round(time.monotonic()-started,2),'client_exit':proc.returncode,'client_error':result.get('is_error'),
                    'result':result.get('result',''),'permission_denials':len(result.get('permission_denials',[])),
                    'tool_permissions':'Read, Edit, and exactly ./run_tests.sh; no permission bypass',
                    'test_exit':check.returncode,'test_output':test_err.decode()[-2000:],
                    'tests_unchanged':(project/'test_prices.py').read_text()==TESTS,
                    'code_changed':(project/'prices.py').read_text()!=SOURCE,
                    'model_stable_per_task':stable,'tasks':tasks,'requests':len(records),
                    'request_outcomes':[{'task':d.get('task'),'model':d.get('model'),'status':d.get('status'),'context':d.get('context'),'error':d.get('error')} for d in reversed(records)],
                    'network_note':'Inference endpoint is loopback; cloud disabled checked by Loqalyn. This is not an OS-wide network sandbox.',
                    'scope':'Synthetic four-test Python repair; does not establish general coding quality.'}
                if runtime: report['fixture_tool_calls']=runtime.calls
                report['passed']=bool(proc.returncode==0 and not result.get('is_error') and check.returncode==0 and report['tests_unchanged'] and report['code_changed'] and stable and not report['permission_denials'] and len(records)>=3)
                if not report['passed']: report['client_diagnostic']=err.decode()[-2000:]
                output.write_text(json.dumps(report,indent=2)+'\n')
                print(json.dumps({k:report[k] for k in ('passed','mode','elapsed_seconds','client_exit','test_exit','requests','result')},indent=2))
                return report['passed']
        finally:
            if proc and proc.returncode is None: proc.terminate(); await proc.wait()
            # Only unload a model this isolated gateway loaded. Existing external
            # instances are never evicted or reconfigured by this harness.
            for m in list(service.models.values()):
                if m.managed and m.loaded and not m.active:
                    await service.ledger.unload(m.id)
            await server.close()
            if runtime: await runtime.stop()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--mode',choices=['fixture','live'],default='fixture')
    parser.add_argument('--runtime',default='http://127.0.0.1:11434'); parser.add_argument('--model',default='qwen3.5:4b')
    parser.add_argument('--timeout',type=int,default=900); parser.add_argument('--output',required=True)
    options=parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(options)) else 1)
