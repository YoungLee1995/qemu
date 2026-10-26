#!/usr/bin/env python3
"""Fetch pinned local build/guest assets without changing system packages."""
import concurrent.futures
import hashlib
from pathlib import Path
import subprocess
import time
import urllib.request

root = Path(__file__).resolve().parents[3]
base = root / 'experiments/qemu_stage1'
assets = base / 'assets'
assets.mkdir(parents=True, exist_ok=True)
downloads = [
    ('python312-parallel.tar.gz',
     'https://github.com/astral-sh/python-build-standalone/releases/download/20251014/'
     'cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-install_only.tar.gz',
     '1ab2b6594d1c3d76cbebea09d6bc3e6ba68d8eb3b6322080375c4cc3dd188f34'),
    ('vmlinuz-lts',
     'https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/netboot/vmlinuz-lts',
     '9d6eff6e7bcd5f5fba623f7a44c565ca6914da2b91110a8dc72a3a63cb088693'),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


for name, url, expected in downloads:
    dest = assets / name
    if dest.exists() and digest(dest) == expected:
        continue
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as f:
        total = int(f.headers['Content-Length'])
        resolved = f.url
    chunk_size = 1024 * 1024
    with dest.open('wb') as f:
        f.truncate(total)

    def fetch(start):
        end = min(start + chunk_size, total) - 1
        for attempt in range(4):
            try:
                request = urllib.request.Request(resolved,
                    headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(request, timeout=120) as reply:
                    data = reply.read()
                    if reply.status != 206 or len(data) != end - start + 1:
                        raise IOError('invalid ranged response')
                with dest.open('r+b') as out:
                    out.seek(start)
                    out.write(data)
                return
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(fetch, range(0, total, chunk_size)))
    if digest(dest) != expected:
        raise RuntimeError(f'{name}: hash mismatch; do not use a changed artifact')

python_dir = base / 'python-tool'
python_dir.mkdir(exist_ok=True)
if not (python_dir / 'python/bin/python3').exists():
    subprocess.run(['tar', '-xzf', str(assets / downloads[0][0]), '-C', str(python_dir)], check=True)
packages = list(assets.glob('busybox-static*.deb'))
if not packages:
    subprocess.run(['apt-get', 'download', 'busybox-static=1:1.30.1-7ubuntu3.1'],
                   cwd=assets, check=True)
    packages = list(assets.glob('busybox-static*.deb'))
expected_busybox = '833b81ad44c4e201059e1bbb1dab285d5d0488736db169adfaa677fd953baae6'
packages = [p for p in packages if digest(p) == expected_busybox]
if len(packages) != 1:
    raise RuntimeError('exact busybox-static package/hash not found')
subprocess.run(['dpkg-deb', '-x', str(packages[0]), str(base / 'rootfs')], check=True)
print('Pinned Python, Linux kernel, and BusyBox prepared:', base)
