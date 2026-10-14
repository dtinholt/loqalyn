"""Explicit opt-in actual inference; requires an already installed small model.

Only contacts the runtime URL provided. Creates a disposable gateway profile;
never downloads models, changes the user's runtime, or contacts cloud providers.
"""
import argparse, asyncio, json, platform, tempfile, time
from pathlib import Path
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from switchyard.core import Config
from switchyard.server import create_app

async def main(args):
    report={'kind':'actual local model inference','model':args.model,'platform':platform.platform(),'checks':{},'cases':[]}
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'profile'; c=Config(path)
        c.data.update(runtimes={'ollama':args.runtime},runtime_local_only_confirmed=True,budget_gb=6,headroom_gb=1,request_timeout=90,escalation_mode='off')
        c.save(c.data)
        async with aiohttp.ClientSession(trust_env=False) as runtime:
            async with runtime.get(args.runtime+'/api/ps') as r:
                report['initially_loaded']=any(m.get('name')==args.model for m in (await r.json())['models'])
            async with runtime.get(args.runtime+'/api/version') as r: report['runtime_version']=await r.json()
            async with runtime.get(args.runtime+'/api/tags') as r:
                tag=next(m for m in (await r.json())['models'] if m['name']==args.model)
                report['model_bytes']=tag['size']; report['model_manifest_digest']=tag.get('digest')
                if tag['size']>1_000_000_000: raise ValueError('Smoke model exceeds the 1 GB safety limit')
        app=await create_app(path); s=app['service']; client=TestClient(TestServer(app,handler_cancellation=True)); await client.start_server()
        auth={'Authorization':'Bearer '+s.token}; model='ollama/'+args.model
        async def run_case(name,prompt,**options):
            body={'model':model,'messages':[{'role':'user','content':prompt+' /no_think'}],'max_tokens':512,**options}
            start=time.perf_counter()
            async with client.post('/v1/chat/completions',json=body,headers=auth) as r:
                text=await r.text(); case={'name':name,'status':r.status,'seconds':round(time.perf_counter()-start,3)}
                if r.status==200:
                    if options.get('stream'): case['completed']='[DONE]' in text; case['contains_content']='"content"' in text
                    else:
                        value=json.loads(text); case['model_receipt']=value.get('model'); case['content']=value['choices'][0]['message'].get('content'); case['usage']=value.get('usage'); case['completed']=bool(case['content'])
                    decision=s.journal.get(r.headers['X-Switchyard-Request-Id']); case['journal_status']=decision['status']; case['receipt_matches']=decision['model']==model
                else: case['error']=json.loads(text).get('error',{}).get('code')
                report['cases'].append(case)
                report['checks'][name]=r.status==200 and case.get('completed',False) and case.get('receipt_matches',False) and case.get('journal_status')=='completed'
                return case
        try:
            await run_case('first_text' if report['initially_loaded'] else 'cold_text','Reply with the single word SWITCHYARD.')
            await run_case('warm_stream','What is two plus two? Reply briefly.',stream=True)
            await run_case('json_output','Return exactly a JSON object with one key "ok" and boolean true.',response_format={'type':'json_object'})
            for i in range(3): await run_case('repeat_'+str(i),'Reply with the single word READY.')
            if report['initially_loaded']:
                report['checks']['existing_loaded_model_preserved']=s.models[model].loaded and not s.models[model].managed
            else:
                async with client.post('/switchyard/models/unload',json={'model':model},headers=auth) as r: report['checks']['managed_unload']=r.status==200; await r.read()
            report['checks']['active_released']=sum(m.active for m in s.models.values())==0
        finally: await client.close()
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report,indent=2))
    return 0 if all(report['checks'].values()) else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--runtime',required=True); p.add_argument('--model',default='qwen3:0.6b'); p.add_argument('--output',type=Path,default=Path('validation-output/live-ollama.json'))
    p.add_argument('--confirmed-local-only',action='store_true',required=True)
    raise SystemExit(asyncio.run(main(p.parse_args())))
