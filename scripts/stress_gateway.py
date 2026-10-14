"""Separate gateway-process stress against six synthetic loopback models."""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import platform
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import time

import aiohttp
from loqalyn.core import Config
from tests.fixtures import Runtime


def percentile(values,p):
    return round(sorted(values)[min(len(values)-1,int(len(values)*p))],3) if values else None


async def run(output,total=6000,soak_seconds=60):
    output.mkdir(parents=True,exist_ok=True)
    peers=[await Runtime('ollama').start(),await Runtime('lmstudio').start()]
    process=None; running=True; rss=[]; active=[]; budget=[]
    report={'kind':'synthetic loopback HTTP fixtures; separate gateway process','platform':platform.platform(),
            'python':platform.python_version(),'attempts':0,'unexpected':[],'phases':[]}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            profile=Path(tmp)/'profile'; c=Config(profile)
            p=c.data; p.update(runtimes={r.name:r.url for r in peers},runtime_local_only_confirmed=True,
                              budget_gb=6,headroom_gb=1,queue_timeout=2,request_timeout=5,escalation_mode='off')
            c.save(p)
            with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            env=os.environ.copy(); env.pop('LOQALYN_LM_TOKEN',None); env.pop('SWITCHYARD_LM_TOKEN',None)
            with (output/'gateway.log').open('wb') as log:
                process=subprocess.Popen([sys.executable,'-m','loqalyn.cli','--port',str(port),'--data-dir',str(profile),'serve'],env=env,stdout=log,stderr=log)
                base=f'http://127.0.0.1:{port}'
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),connector=aiohttp.TCPConnector(limit=160),trust_env=False) as client:
                    for _ in range(100):
                        try:
                            async with client.get(base+'/health') as r:
                                if r.status==200: break
                        except aiohttp.ClientError: pass
                        if process.poll() is not None: raise RuntimeError('Gateway exited during startup')
                        await asyncio.sleep(.1)
                    else: raise RuntimeError('Gateway startup timed out')
                    auth={'Authorization':'Bearer '+(profile/'token').read_text().strip()}
                    async def monitor():
                        while running:
                            try:
                                async with client.get(base+'/loqalyn/status',headers=auth) as r:
                                    if r.status==200:
                                        state=await r.json(); active.append(state['active']); budget.append(state['used_gb']); rss.append(state['process_peak_rss_mib'])
                            except Exception: pass
                            await asyncio.sleep(.1)
                    sampler=asyncio.create_task(monitor())
                    try:
                        latencies=[]
                        for _ in range(1000):
                            start=time.perf_counter()
                            async with client.post(base+'/loqalyn/route',headers=auth,json={'model':'auto','messages':[{'role':'user','content':'Hello'}],'max_tokens':16}) as r:
                                if r.status!=200: report['unexpected'].append({'phase':'routing','status':r.status})
                                await r.read()
                            latencies.append((time.perf_counter()-start)*1000)
                        report['warm_routing']={'probes':1000,'p50_ms':percentile(latencies,.5),'p95_ms':percentile(latencies,.95)}
                        models=[f'{p.name}/{name}' for p in peers for name in p.names]
                        serial=0
                        for concurrency in (1,8,32,128):
                            gate=asyncio.Semaphore(concurrency); codes=Counter(); errors=Counter(); latency=[]
                            async def attempt(i):
                                nonlocal serial
                                async with gate:
                                    serial+=1; marker='[before]' if i%101==0 else '[mid]' if i%137==0 else '[malformed]' if i%199==0 else ''
                                    stream=i%3==0
                                    start=time.perf_counter()
                                    try:
                                        async with client.post(base+'/v1/chat/completions',headers=auth,json={
                                            'model':models[i%len(models)],'messages':[{'role':'user','content':'Synthetic test '+marker}],
                                            'max_tokens':16,'stream':stream}) as r:
                                            text=await r.text(); codes[str(r.status)]+=1
                                            if r.status>=400:
                                                code=json.loads(text).get('error',{}).get('code'); errors[str(code)]+=1
                                                if code not in ('runtime_error','truncated_response','memory_budget','memory_pressure','no_eligible_model','context_exceeds_local','overloaded','queue_timeout'):
                                                    report['unexpected'].append({'phase':concurrency,'error_code':code})
                                                if r.status==502 and not marker:
                                                    report['unexpected'].append({'phase':concurrency,'unplanned_runtime_error':code})
                                            if r.status not in (200,422,502,503): report['unexpected'].append({'phase':concurrency,'status':r.status})
                                            elif r.status==200 and not marker and ('[DONE]' not in text if stream else 'fixture reply' not in text):
                                                report['unexpected'].append({'phase':concurrency,'error':'missing_completion'})
                                            elif r.status==200 and marker and (not stream or marker!='[mid]' or '[DONE]' in text or 'error' not in text):
                                                report['unexpected'].append({'phase':concurrency,'error':'injected_fault_reported_success'})
                                            elif r.status==503 and 'Retry-After' not in r.headers: report['unexpected'].append({'error':'missing_retry_after'})
                                    except Exception as exc:
                                        report['unexpected'].append({'phase':concurrency,'exception':type(exc).__name__})
                                    latency.append((time.perf_counter()-start)*1000)
                            start=time.perf_counter(); await asyncio.gather(*(attempt(i) for i in range(total//4)))
                            report['phases'].append({'concurrency':concurrency,'attempts':len(latency),'statuses':dict(codes),'error_codes':dict(errors),
                                'seconds':round(time.perf_counter()-start,3),'p95_ms':percentile(latency,.95)})
                            report['attempts']+=len(latency)
                        # Exercise journal retention and long-lived request cleanup.
                        concurrency=8; gate=asyncio.Semaphore(concurrency); codes=Counter(); errors=Counter(); latency=[]
                        soak_start=time.perf_counter(); soak_index=0
                        while time.perf_counter()-soak_start<soak_seconds:
                            await asyncio.gather(*(attempt(soak_index+i) for i in range(8)))
                            soak_index+=8
                        report['soak']={'attempts':len(latency),'statuses':dict(codes),'error_codes':dict(errors),'seconds':round(time.perf_counter()-soak_start,3),'p95_ms':percentile(latency,.95)}
                        report['attempts']+=len(latency)
                        # Disconnect after receiving a token, then demand recovery.
                        for _ in range(40):
                            async with client.post(base+'/v1/chat/completions',headers=auth,json={
                                'model':models[0],'messages':[{'role':'user','content':'[slow]'}],'max_tokens':16,'stream':True}) as r:
                                await r.content.read(20)
                        await asyncio.sleep(.3)
                        async with client.post(base+'/v1/chat/completions',headers=auth,json={
                            'model':models[0],'messages':[{'role':'user','content':'Recovery'}],'max_tokens':16}) as r:
                            report['recovery_status']=r.status; await r.read()
                        async with client.get(base+'/loqalyn/status',headers=auth) as r: final=await r.json()
                        report['final_active']=final['active']; report['final_inflight']=final['inflight']
                        report['gateway_metrics']=final['metrics']
                        report['fixture_active_evictions']=sum(p.evicted_active for p in peers)
                        report['fixture_loads']=sum(p.loads for p in peers); report['fixture_unloads']=sum(p.unloads for p in peers)
                        report['disconnect_cases']=40
                    finally:
                        running=False; await sampler
                process.terminate()
                await asyncio.to_thread(process.wait,10)
    finally:
        if process and process.poll() is None: process.kill(); process.wait()
        for peer in peers: await peer.stop()
    report['sampled_peak_rss_mib']=round(max(rss),2) if rss else None
    report['rss_method']='gateway resource.getrusage(RUSAGE_SELF); OS process peak, excludes runtime peers'
    report['sampled_peak_model_reservation_gib']=max(budget) if budget else None
    report['sampled_peak_active']=max(active) if active else None
    report['checks']={'no_unexpected_outcomes':not report['unexpected'],'no_active_eviction':report['fixture_active_evictions']==0,
                      'recovered':report['recovery_status']==200,'active_released':report['final_active']==0,
                      'budget_respected':bool(budget) and max(budget)<=5,'rss_under_500_mib':bool(rss) and max(rss)<500}
    (output/'stress-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 0 if all(report['checks'].values()) else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,default=Path('validation-output/stress')); parser.add_argument('--attempts',type=int,default=6000)
    parser.add_argument('--soak-seconds',type=int,default=60)
    args=parser.parse_args(); raise SystemExit(asyncio.run(run(args.output,args.attempts,args.soak_seconds)))
