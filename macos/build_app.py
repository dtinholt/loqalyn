"""Build an ad-hoc-signed Apple silicon alpha using hash-locked dependencies.

Run with Python 3.12+ on an Apple silicon Mac with Apple's command line tools.
The optional --base-bundle path reuses a verified local 0.2.0 bundle's runtime
for development. Normal/release builds download all pinned inputs themselves.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def fetch(item,cache):
    path=cache/item['filename']
    if not path.exists():
        with urllib.request.urlopen(item['url'],timeout=120) as response:
            data=response.read(200*1024*1024)
        if hashlib.sha256(data).hexdigest()!=item['sha256']: raise ValueError('Download checksum mismatch: '+item['filename'])
        path.write_bytes(data)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']: raise ValueError('Cached checksum mismatch: '+item['filename'])
    return path


def unpack_wheel(path,destination):
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            p=Path(member.filename)
            if p.is_absolute() or '..' in p.parts or member.file_size>100*1024*1024: raise ValueError('Invalid wheel member')
        archive.extractall(destination)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,default=ROOT/'build')
    parser.add_argument('--cache',type=Path,default=ROOT/'build'/'downloads'); parser.add_argument('--base-bundle',type=Path)
    args=parser.parse_args()
    if sys.platform!='darwin' or platform.machine()!='arm64': raise SystemExit('Build on an Apple silicon Mac with the Apple command line tools.')
    lock=json.loads((ROOT/'macos/dependencies.lock.json').read_text())
    args.output.mkdir(parents=True,exist_ok=True); args.cache.mkdir(parents=True,exist_ok=True)
    app=args.output/'Switchyard.app'
    if app.exists(): raise SystemExit('Output bundle already exists; choose an empty build directory.')
    contents=app/'Contents'; resources=contents/'Resources'; binary=contents/'MacOS'
    resources.mkdir(parents=True); binary.mkdir()
    if args.base_bundle:
        base=args.base_bundle/'Contents'
        shutil.copytree(base/'Resources/runtime',resources/'runtime',symlinks=True)
        shutil.copy2(base/'MacOS/SwitchyardPython',binary/'SwitchyardPython')
    else:
        with tempfile.TemporaryDirectory(dir=args.output) as temp:
            with tarfile.open(fetch(lock['runtime'],args.cache)) as archive:
                archive.extractall(temp,filter='data')
            runtime=Path(temp)/'python'
            shutil.copytree(runtime,resources/'runtime',symlinks=True)
        exe=resources/'runtime/bin/python3.12'
        shutil.move(exe,binary/'SwitchyardPython')
        exe.symlink_to('../../../MacOS/SwitchyardPython')
    (contents/'lib').symlink_to('Resources/runtime/lib')
    # Packaging needs no installer in the runtime: dependencies are hash locked.
    shutil.rmtree(resources/'runtime/lib/python3.12/site-packages')
    shutil.copytree(ROOT/'macos/licenses',resources/'licenses')
    packages=resources/'python-packages'; packages.mkdir()
    for wheel in lock['wheels']:
        unpack_wheel(fetch(wheel,args.cache),packages)
    # Upstream PyObjC wheels include development bundles with Finder metadata.
    # They are not runtime dependencies and interfere with native code signing.
    for path in list(packages.rglob('*.dSYM')): shutil.rmtree(path)
    if (packages/'PyObjCTest').exists(): shutil.rmtree(packages/'PyObjCTest')
    # The journal uses patched APSW; bootstrap disables the old stdlib entry point.
    shutil.copytree(ROOT/'switchyard',resources/'app/switchyard',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copy2(ROOT/'macos/bootstrap.py',resources/'bootstrap.py')
    shutil.copy2(ROOT/'macos/Switchyard.icns',resources/'Switchyard.icns')
    shutil.copy2(ROOT/'LICENSE',resources/'LICENSE')
    shutil.copy2(ROOT/'macos/dependencies.lock.json',resources/'build-manifest.json')
    info={'CFBundleName':'Switchyard','CFBundleDisplayName':'Switchyard','CFBundleIdentifier':'dev.switchyard.desktop',
          'CFBundleExecutable':'Switchyard','CFBundleIconFile':'Switchyard.icns','CFBundlePackageType':'APPL',
          'CFBundleShortVersionString':'0.3.0','CFBundleVersion':'4','LSMinimumSystemVersion':'13.0',
          'NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication','LSApplicationCategoryType':'public.app-category.developer-tools'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(info)); (contents/'PkgInfo').write_text('APPL????')
    subprocess.run(['xcrun','clang','-arch','arm64','-mmacosx-version-min=13.0','-O2',str(ROOT/'macos/launcher.c'),'-o',str(binary/'Switchyard')],check=True)
    for attribute in ('com.apple.FinderInfo','com.apple.ResourceFork'):
        subprocess.run(['xattr','-dr',attribute,str(app)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for path in sorted(app.rglob('*'),key=lambda p:len(p.parts),reverse=True):
        if path.is_symlink(): continue
        if path==binary/'Switchyard': continue
        if path.is_file():
            with path.open('rb') as handle: magic=handle.read(4)
            if magic in (b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe'):
                subprocess.run(['codesign','--force','--sign','-',str(path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['xattr','-d','com.apple.FinderInfo',str(app)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['codesign','--force','--sign','-',str(app)],check=True)
    subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
    print(app)


if __name__=='__main__': main()
