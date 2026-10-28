#!/usr/bin/env python3
"""Pinned guest module build in evidence/, without installing host packages.

Internal debug build: GCC11 substitutes alignment flag and omits GCC14-only
initialization/plugins. Matching kernel headers/symvers/objtool are retained.
This is not a release hardening/toolchain equivalence claim.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
ASSETS=HERE/'evidence/assets'
KVER='6.12.114-0-lts'
BASE='https://dl-cdn.alpinelinux.org/alpine/v3.22/main/x86_64/'
# Fetched package content hashes, fixed after inspection of the supplied version.
PACKAGES={
 'libelf-0.193-r0.apk':(43625,'b8df03bd86f172aa1741790592056e6d6588247ddf6473920fa32cd7cc681f6a'),
 'musl-1.2.5-r13.apk':(412387,'359890bc7c4dc5fee7a85192de60a471c987e4664767288578beca60a44db2c8'),
 'zstd-libs-1.5.7-r0.apk':(377569,'1bdd6e57cfbfbfd6e8481cad37ddd5d199950715bec1879b3afb600272dbb09e'),
 'zlib-1.3.2-r1.apk':(55457,'af24e502645e2b392cae833856d776691ac21655362e5db69d7b89f147f76216'),
 'linux-lts-dev-6.12.114-r0.apk':(19540778,'ee98dea7868032ed36a66af3ed2296e7fb80823d02f4cdc21991964d0d480af0'),
}


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def fetch(name,size,expected):
    p=ASSETS/name
    if p.exists() and sha(p)==expected:return p
    with p.open('wb') as f:f.truncate(size)
    def part(start):
        end=min(start+512*1024,size)-1
        for attempt in range(3):
            try:
                req=urllib.request.Request(BASE+name,headers={'Range':f'bytes={start}-{end}'})
                with urllib.request.urlopen(req,timeout=30) as r:
                    data=r.read()
                    if r.status!=206 or len(data)!=end-start+1:raise RuntimeError('short range')
                with p.open('r+b') as f:f.seek(start);f.write(data)
                return
            except Exception:
                if attempt==2:raise
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(part,range(0,size,512*1024)))
    if sha(p)!=expected:raise RuntimeError('package hash mismatch: '+name)
    return p


def extract(package,dest):
    dest.mkdir(parents=True,exist_ok=True)
    with tarfile.open(package) as t:
        for m in t.getmembers():
            if m.name.startswith('.') and '/' not in m.name:continue
            if m.name.startswith('lib/modules/'):continue
            # Packages are pinned and contain normal Linux tree links.
            target=(dest/m.name).resolve()
            if not target.is_relative_to(dest.resolve()):raise RuntimeError('archive traversal')
            t.extract(m,dest)


def main():
    ASSETS.mkdir(parents=True,exist_ok=True)
    packages={name:fetch(name,*args) for name,args in PACKAGES.items()}
    headers=ASSETS/'headers'
    h=headers/'usr/src'/('linux-headers-'+KVER)
    # Re-extract exact package each build; wrappers never modify source archive.
    extract(packages['linux-lts-dev-6.12.114-r0.apk'],headers)
    musl=ASSETS/'musl-root'
    for name,p in packages.items():
        if not name.startswith('linux-'):extract(p,musl)
    module=HERE/'evidence/host-module'
    module.mkdir(exist_ok=True)
    for name in ('ssv_test_driver.c','ssv_test_app.c'):
        shutil.copy2(ROOT/'simulator/host-demo'/name,module/name)
    subprocess.run(['patch','-p1','-i',str(HERE/'host-demo.patch')],cwd=module,check=True)
    (module/'Makefile').write_text('obj-m += ssv_test_driver.o\n')
    subprocess.run(['gcc','-O2','-I'+str(h/'scripts/include'),'-o',str(h/'scripts/basic/fixdep'),str(h/'scripts/basic/fixdep.c')],check=True)
    subprocess.run(['gcc','-O2','-I'+str(h/'scripts/include'),'-I'+str(h/'scripts/mod'),'-o',str(h/'scripts/mod/modpost')]+
                   [str(h/'scripts/mod'/n) for n in ('modpost.c','file2alias.c','sumversion.c','symsearch.c')],check=True)
    obj=h/'tools/objtool/objtool';obj.rename(obj.with_name('objtool.musl'))
    obj.write_text('#!/bin/sh\nexec "'+str(musl/'lib/ld-musl-x86_64.so.1')+'" --library-path "'+str(musl/'lib')+':'+str(musl/'usr/lib')+'" "'+str(obj.with_name('objtool.musl'))+'" "$@"\n');obj.chmod(0o755)
    cc=module/'gcc-compat'
    cc.write_text('''#!/usr/bin/env python3
import os,sys
args=[]
for arg in sys.argv[1:]:
    if arg == '-ftrivial-auto-var-init=zero' or arg.startswith('-fplugin') or arg == '-DLATENT_ENTROPY_PLUGIN': continue
    arg=arg.replace('-fmin-function-alignment=','-falign-functions=')
    args.append(arg)
os.execvp('gcc',['gcc']+args)
''');cc.chmod(0o755)
    subprocess.run(['make','-C',str(h),'M='+str(module),'CC='+str(cc),'modules'],check=True)
    subprocess.run(['gcc','-static','-O2','-o',str(module/'ssv_test_app'),str(module/'ssv_test_app.c')],check=True)
    (module/'manifest.json').write_text(json.dumps({
        'kernel_version':KVER,'mode':'internal GCC11 debug module; plugins/auto-var-init omitted',
        'packages':{name:{'url':BASE+name,'sha256':sha(p)} for name,p in packages.items()},
        'inputs':{str(p):sha(p) for p in [HERE/'host-demo.patch',ROOT/'simulator/host-demo/ssv_test_driver.c',ROOT/'simulator/host-demo/ssv_test_app.c']},
        'outputs':{str(p):sha(p) for p in [module/'ssv_test_driver.ko',module/'ssv_test_app']}},indent=2))
    print(module,flush=True)

if __name__=='__main__':main()
