"""Package an app without extended attributes; verify a clean extraction."""
import argparse, hashlib, json, os, stat, subprocess, tempfile, zipfile
from pathlib import Path


def main(app,output):
    app=app.resolve(); output=output.resolve()
    subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise SystemExit('Refusing to replace an existing artifact')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in [app,*sorted(app.rglob('*'))]:
            relative=path.relative_to(app.parent).as_posix(); mode=path.lstat().st_mode
            info=zipfile.ZipInfo(relative+('/' if path.is_dir() and not path.is_symlink() else ''))
            info.create_system=3; info.external_attr=mode<<16; info.compress_type=zipfile.ZIP_DEFLATED
            if path.is_symlink():
                if not path.resolve().is_relative_to(app): raise ValueError('Bundle link escapes app')
                data=os.readlink(path).encode()
            else: data=b'' if path.is_dir() else path.read_bytes()
            archive.writestr(info,data)
    with tempfile.TemporaryDirectory(prefix='loqalyn-package-') as temp:
        subprocess.run(['ditto','-x','-k',str(output),temp],check=True)
        extracted=Path(temp)/app.name
        subprocess.run(['codesign','--verify','--deep','--strict',str(extracted)],check=True)
        help_result=subprocess.run([str(extracted/'Contents/MacOS/Loqalyn'),'--cli','--help'],capture_output=True,timeout=15,check=True)
        if b'loqalyn' not in help_result.stdout: raise ValueError('Packaged CLI did not start')
        subprocess.run(['codesign','--verify','--deep','--strict',str(extracted)],check=True)
    result={'file':output.name,'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'clean_extraction_signature':True,'bundled_cli_started':True,'signature_after_launch':True}
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('app',type=Path); parser.add_argument('output',type=Path); args=parser.parse_args(); main(args.app,args.output)
