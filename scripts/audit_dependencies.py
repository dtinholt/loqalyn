"""Query PyPI's published advisories for each locked wheel; no install or upload."""
import concurrent.futures, json, urllib.request
from pathlib import Path

def audit(item):
    result={'name':item['name'],'version':item['version']}
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{item['name']}/{item['version']}/json",timeout=20) as response:
            result['vulnerabilities']=json.load(response).get('vulnerabilities',[])
    except Exception as exc: result['error']=type(exc).__name__
    return result

if __name__=='__main__':
    lock=json.loads((Path(__file__).resolve().parents[1]/'macos/dependencies.lock.json').read_text())
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(audit,lock['wheels']))
    print(json.dumps(results,indent=2))
    raise SystemExit(1 if any(r.get('error') or r.get('vulnerabilities') for r in results) else 0)
