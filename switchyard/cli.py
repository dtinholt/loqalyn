"""Command-line control and newline-delimited MCP over stdio."""
import argparse, getpass, json, os, sys, time, urllib.request, urllib.error
from pathlib import Path
import yaml
from . import __version__
from .security import api_token, loads, MAX_BODY, MAX_RUNTIME

DEFAULT_DIR=Path(os.environ.get('SWITCHYARD_DIR',Path.home()/'.switchyard'))

def call(path,method='GET',body=None,port=8741,directory=DEFAULT_DIR):
    token=api_token(Path(directory)/'token')
    if not path.startswith('/') or path.startswith('//') or any(ord(c)<32 for c in path): raise ValueError('Invalid local API path.')
    req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=7200) as response: return loads(response.read(MAX_RUNTIME+1),limit=MAX_RUNTIME)

def mcp(port,directory):
    tools=[
        {'name':'list_models','description':'List discovered local models and their capabilities.','inputSchema':{'type':'object','properties':{}}},
        {'name':'route','description':'Explain a local routing decision without loading or generating.','inputSchema':{'type':'object','properties':{'prompt':{'type':'string'},'model':{'type':'string','default':'auto'}},'required':['prompt']}},
        {'name':'load','description':'Load a local model within the shared memory budget.','inputSchema':{'type':'object','properties':{'model':{'type':'string'},'context':{'type':'integer','default':8192}},'required':['model']}},
        {'name':'escalation_status','description':'List pending frontier suggestions. This does not approve or send anything.','inputSchema':{'type':'object','properties':{}}}
    ]
    while True:
        line=sys.stdin.readline(MAX_BODY+1)
        if not line: break
        req={}
        try:
            if len(line)>MAX_BODY:
                print(json.dumps({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Message exceeds the size limit.'}}),flush=True)
                return
            req=loads(line)
            if not isinstance(req,dict): raise ValueError('JSON-RPC request must be an object.')
            if 'id' not in req: continue
            method=req.get('method'); params=req.get('params',{})
            if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'switchyard','version':__version__}}
            elif method=='ping': result={}
            elif method=='tools/list': result={'tools':tools}
            elif method=='tools/call':
                name=params['name']; args=params.get('arguments',{})
                if name=='list_models': value=call('/switchyard/catalog',port=port,directory=directory)
                elif name=='route': value=call('/switchyard/route','POST',{'model':args.get('model','auto'),'messages':[{'role':'user','content':args['prompt']}]},port,directory)
                elif name=='load': value=call('/switchyard/models/load','POST',{'model':args['model'],'context':args.get('context',8192)},port,directory)
                elif name=='escalation_status': value=call('/switchyard/escalations',port=port,directory=directory)
                else: raise ValueError('Unknown tool')
                result={'content':[{'type':'text','text':json.dumps(value)}]}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True); continue
            print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}),flush=True)
        except Exception as e:
            print(json.dumps({'jsonrpc':'2.0','id':req.get('id') if isinstance(req,dict) else None,'error':{'code':-32603,'message':'Request failed; check the local gateway and input.'}}),flush=True)

def evaluate(path,port,directory):
    cases=yaml.safe_load(Path(path).read_text()); reports=[]; durations=[]
    for case in cases:
        begin=time.perf_counter()
        body=case.get('request',{'model':'auto','messages':case.get('messages',[])})
        result=call('/switchyard/route','POST',body,port,directory); durations.append((time.perf_counter()-begin)*1000)
        expected=case.get('expect',{}); failures=[]
        if 'route' in expected and result['route']!=expected['route']: failures.append('route')
        if expected.get('acceptable_models') and result['model'] not in expected['acceptable_models']: failures.append('model')
        if result['model'] and result['model'] in expected.get('must_not',[]): failures.append('forbidden_model')
        if 'escalate' in expected and bool(result['escalation'])!=(expected['escalate'] not in ('none',None,False)): failures.append('escalation')
        reports.append({'id':case['id'],'passed':not failures,'failures':failures,'route':result['route'],'model':result['model']})
    durations.sort(); passed=sum(r['passed'] for r in reports)
    print(json.dumps({'passed':passed,'total':len(reports),'p95_round_trip_ms':durations[min(len(durations)-1,int(len(durations)*.95))] if durations else None,'cases':reports,'note':'Routing fixture checks only; not the hardware E1–E6 release gate.'},indent=2))
    return 0 if passed==len(reports) else 1

def main():
    parser=argparse.ArgumentParser(prog='yard',description='One endpoint. The right local model.')
    parser.add_argument('--port',type=int,default=8741); parser.add_argument('--data-dir',type=Path,default=DEFAULT_DIR)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('serve',help='Start the local API gateway')
    for name in ('status','catalog','refresh','mcp','token'): sub.add_parser(name)
    route=sub.add_parser('route'); route.add_argument('prompt'); route.add_argument('--model',default='auto'); route.add_argument('--explain',action='store_true')
    for name in ('load','unload'):
        p=sub.add_parser(name); p.add_argument('model'); p.add_argument('--context',type=int,default=8192)
    pull=sub.add_parser('pull'); pull.add_argument('model'); pull.add_argument('--runtime',choices=['ollama','lmstudio'],default='ollama')
    bench=sub.add_parser('bench'); bench.add_argument('model'); bench.add_argument('--tokens',type=int,default=128)
    ev=sub.add_parser('eval'); ev.add_argument('fixtures',help='YAML synthetic routing fixtures')
    key=sub.add_parser('key'); key.add_argument('provider',choices=['openai','anthropic'])
    recovery=sub.add_parser('recover',help='Clear an uncertain reservation after restarting the affected runtime')
    recovery.add_argument('runtime',choices=['ollama','lmstudio']); recovery.add_argument('--confirmed-runtime-restarted',action='store_true',required=True)
    args=parser.parse_args()
    try:
        if args.command=='serve':
            from aiohttp import web
            from .server import create_app
            print(f'Switchyard is starting at http://127.0.0.1:{args.port}. Ctrl+C to stop.')
            web.run_app(create_app(args.data_dir),host='127.0.0.1',port=args.port,access_log=None,handler_cancellation=True); return
        if args.command=='token': print(api_token(args.data_dir/'token')); return
        if args.command=='mcp': mcp(args.port,args.data_dir); return
        if args.command=='key':
            if sys.platform!='darwin': raise ValueError('Provider credentials require the macOS Keychain.')
            from keyring.backends.macOS import Keyring
            key=getpass.getpass(f'{args.provider} API key (saved to macOS Keychain): ')
            if not key: raise ValueError('No key entered.')
            Keyring().set_password('switchyard',args.provider,key); print('Key saved. No network request was made.'); return
        if args.command=='eval': sys.exit(evaluate(args.fixtures,args.port,args.data_dir))
        request=lambda path,method='GET',body=None: call(path,method,body,args.port,args.data_dir)
        if args.command in ('status','catalog'): result=request('/switchyard/'+args.command)
        elif args.command=='recover': result=request('/switchyard/recover','POST',{'runtime':args.runtime,'confirmed_runtime_restarted':args.confirmed_runtime_restarted})
        elif args.command=='refresh': result=request('/switchyard/refresh','POST',{})
        elif args.command=='route': result=request('/switchyard/route','POST',{'model':args.model,'messages':[{'role':'user','content':args.prompt}]})
        elif args.command in ('load','unload'): result=request('/switchyard/models/'+args.command,'POST',{'model':args.model,'context':args.context})
        elif args.command=='pull': result=request('/switchyard/pull','POST',{'model':args.model,'runtime':args.runtime,'confirmed':True})
        elif args.command=='bench':
            start=time.perf_counter(); result=request('/v1/chat/completions','POST',{'model':args.model,'stream':False,'max_tokens':args.tokens,'messages':[{'role':'user','content':'Explain the benefits of local computing in concise sentences.'}]})
            elapsed=time.perf_counter()-start; count=result.get('usage',{}).get('completion_tokens')
            result={'model':result['model'],'seconds_including_load_and_prefill':round(elapsed,3),'output_tokens':count,'end_to_end_tokens_per_second':round(count/elapsed,2) if count else None,'note':'A synthetic inference benchmark. Not a calibrated quality score.'}
        print(json.dumps(result,indent=2))
    except urllib.error.HTTPError as e:
        try: print(json.loads(e.read()).get('error',{}).get('message',str(e)),file=sys.stderr)
        except Exception: print(str(e),file=sys.stderr)
        sys.exit(1)
    except (OSError,ValueError) as e: print(str(e),file=sys.stderr); sys.exit(1)

if __name__=='__main__': main()
