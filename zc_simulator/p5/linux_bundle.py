"""Build and validate a relocatable P5 Linux runtime, without model sources."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'p4'))
from cluster_platform import Platform, ROOT, digest, stop
from run_p1 import ISA, entry

HERE = Path(__file__).resolve().parent
BUNDLE = HERE.parent / 'simu'
SIM = ROOT / 'simulator'
REQUIRED = (
    'bin/spike', 'bin/sm_system.cfg', 'images/firmware.riscv',
    'images/mcu.bin', 'images/npu.bin', 'service/service.py',
    'service/protocol.py', 'fabric/fabric_service.py', 'fabric/fabric.py',
    'fabric/gen4-eight.json', 'guest/vmlinuz-lts',
    'guest/initramfs-2.cpio.gz', 'guest/initramfs-8.cpio.gz',
)


def has_sources():
    # A partial source checkout is a build error, not a reason to hide failures
    # by selecting an old runtime. Runtime-only directories lack this marker.
    return (SIM / 'riscv-isa-sim/spike_main/spike.cc').is_file()


def validate_bundle(path):
    path = Path(path).resolve()
    try:
        manifest = json.loads((path / 'manifest.json').read_text())
        files = manifest['files']
        if manifest['schema'] != 1 or not set(REQUIRED).issubset(files):
            raise ValueError('incomplete bundle manifest')
        for name, expected in files.items():
            target = (path / name).resolve()
            if not target.is_relative_to(path) or digest(target) != expected:
                raise ValueError('invalid bundle member: ' + name)
        if not os.access(path / 'bin/spike', os.X_OK):
            raise ValueError('spike is not executable')
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError(f'P5 runtime bundle invalid: {path}: {exc}. '
                           'Rebuild it in a workspace containing simulator sources.') from exc
    return manifest


def guest_image(stage, cards, module, work):
    fs = work / f'rootfs-{cards}'
    shutil.copytree(ROOT / 'experiments/qemu_stage1/rootfs', fs)
    for name in ('dev', 'sys', 'proc', 'tmp'):
        (fs / name).mkdir(exist_ok=True)
    shutil.copy2(module / 'ssv_test_driver.ko', fs)
    base = (HERE.parent / 'p4/guest-cluster.c').read_text().replace(
        '#define CARDS 8', f'#define CARDS {cards}')
    base = base.replace(
        'puts("P4_LINUX_PASS cards=8 host_tasks=32 peer_tasks=224 directed_pairs=56 modes=4 file_ipc=8");',
        'printf("P5_BASE_PASS cards=%u host_tasks=%u peer_tasks=%u file_ipc=%u\\n",CARDS,CARDS*4,CARDS*(CARDS-1)*4,CARDS);')
    (work / 'p4-base.c').write_text(base)
    with (work / 'bundle-build.log').open('a') as log:
        subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra',
                        '-I' + str(work), '-I' + str(SIM / 'tools/p5'),
                        str(HERE / 'guest-communication.c'), '-o', str(fs / 'guest-cluster')],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    (fs / 'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox insmod /ssv_test_driver.ko || exit 1
/guest-cluster
echo P4_GUEST_EXIT=$?
/bin/busybox poweroff -f
''')
    (fs / 'init').chmod(0o755)
    archive = b''.join(entry(str(f.relative_to(fs)), b'' if f.is_dir() else f.read_bytes(),
                             f.stat().st_mode, i)
                       for i, f in enumerate(sorted(fs.rglob('*')), 1) if not f.is_symlink())
    (stage / f'guest/initramfs-{cards}.cpio.gz').write_bytes(
        gzip.compress(archive + entry('TRAILER!!!', b'', 0, 999999), mtime=0))


def build_bundle(work):
    work = Path(work)
    BUNDLE.mkdir(exist_ok=True)
    # The existing published payload survives a failed compilation.
    with (work / 'bundle-build.log').open('w') as log:
        commands = [
            ['make', '-C', str(SIM / 'riscv-isa-sim/build'), '-j8', 'spike'],
            ['make', '-C', str(SIM / 'riscv-tests'), 'firmware.riscv'],
            ['make', '-C', str(SIM / 'tools/p5/runtime')],
            [sys.executable, str(HERE.parent / 'p1/build_host_demo.py')],
        ]
        for command in commands:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=BUNDLE) as tmp:
        stage = Path(tmp) / 'payload'
        copies = {
            'bin/spike': SIM / 'riscv-isa-sim/build/spike',
            'bin/sm_system.cfg': SIM / 'riscv-isa-sim/smart/sm_system.cfg',
            'images/firmware.riscv': SIM / 'riscv-tests/firmware.riscv',
            'images/mcu.bin': SIM / 'tools/p5/runtime/mcu.bin',
            'images/npu.bin': SIM / 'tools/p5/runtime/npu.bin',
            'guest/vmlinuz-lts': ROOT / 'experiments/qemu_stage1/assets/vmlinuz-lts',
        }
        for name in ('service.py', 'protocol.py'):
            copies['service/' + name] = SIM / 'tools/p1' / name
        for name in ('fabric_service.py', 'fabric.py'):
            copies['fabric/' + name] = SIM / 'tools/p5' / name
        copies['fabric/gen4-eight.json'] = SIM / 'tools/p5/profiles/gen4-eight.json'
        for name, source in copies.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # Keep the distributed simulator reasonably sized; the original remains intact.
        subprocess.run(['strip', '--strip-debug', str(stage / 'bin/spike')], check=True)
        module = HERE.parent / 'p1/evidence/host-module'
        for cards in (2, 8):
            guest_image(stage, cards, module, work)
        manifest = dict(schema=1, scope='P5-Linux-2-and-8-card-runtime',
                        created_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        files={str(p.relative_to(stage)): digest(p)
                               for p in sorted(stage.rglob('*')) if p.is_file()})
        (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        dependencies = subprocess.run(['ldd', str(stage / 'bin/spike')],
                                      capture_output=True, text=True, check=True).stdout
        (stage / 'host-dependencies.txt').write_text(dependencies)
        validate_bundle(stage)
        # Versioned payloads plus an atomic pointer switch avoid partial packages.
        target = BUNDLE / f'runtime-{time.time_ns()}'
        stage.rename(target)
        link = BUNDLE / f'.current-{os.getpid()}'
        link.symlink_to(target.name, target_is_directory=True)
        link.replace(BUNDLE / 'current')
    return target


def select_bundle(work, mode='auto'):
    source = mode == 'source' or (mode == 'auto' and has_sources())
    if source:
        if not has_sources():
            raise RuntimeError('Simulator source tree is missing')
        print('P5 runtime: source build and publish', flush=True)
        path = build_bundle(work)
    else:
        print('P5 runtime: prebuilt bundle', flush=True)
        path = (BUNDLE / 'current').resolve()
    manifest = validate_bundle(path)
    (Path(work) / 'bundle.json').write_text(json.dumps(
        dict(mode='source' if source else 'prebuilt', path=str(path), manifest=manifest), indent=2))
    return path


class BundlePlatform(Platform):
    """Same PCIe layout, with precompiled images and relocated services."""
    def __init__(self, work, count, bundle):
        super().__init__(work, count)
        self.bundle = Path(bundle)
        self.fabric_process = None
        self.fabric_socket = Path(f'/tmp/zc-p5-fabric-{os.getpid()}-{time.time_ns()%1000000}.sock')

    def prepare(self):
        inputs = self.work / 'inputs'
        inputs.mkdir()
        for name in ('mcu.bin', 'npu.bin'):
            shutil.copy2(self.bundle / 'images' / name, inputs / name)
        fw = inputs / 'firmware.cfg'
        fw.write_text('runtime_func:1\npc_num:2\n[1, 0x40000000, ./mcu.bin]\n[16, 0x4000000000, ./npu.bin]\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
        spike = self.bundle / 'bin/spike'
        firmware = self.bundle / 'images/firmware.riscv'
        files = [spike, firmware, self.bundle / 'bin/sm_system.cfg', fw,
                 inputs / 'mcu.bin', inputs / 'npu.bin']
        self.cfg = self.work / 'backend.json'
        self.cfg.write_text(json.dumps(dict(spike=str(spike), firmware=str(firmware), isa=ISA,
            cfg=str(fw), cwd=str(inputs), p2=True,
            sha256={str(p): digest(p) for p in files}), indent=2))
        for i in range(self.count):
            self.sockets.append(Path(f'/tmp/zc-p4-{os.getpid()}-{i}.sock'))
            self.start_service(i)
        config = json.loads((self.bundle / 'fabric/gen4-eight.json').read_text())
        config['cards'] = self.count
        (self.work / 'fabric-config.json').write_text(json.dumps(config, indent=2))
        with (self.work / 'fabric-service.log').open('w') as log:
            self.fabric_process = subprocess.Popen([sys.executable,
                str(self.bundle / 'fabric/fabric_service.py'), '--socket', str(self.fabric_socket),
                '--config', str(self.work / 'fabric-config.json'),
                '--out', str(self.work / 'live-fabric.json')], stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.wait_socket(self.fabric_socket, self.fabric_process)

    @staticmethod
    def wait_socket(path, process):
        end = time.monotonic() + 10
        while not path.exists():
            if process.poll() is not None:
                raise RuntimeError('service exited: ' + str(path))
            if time.monotonic() >= end:
                raise TimeoutError(str(path))
            time.sleep(.005)

    def start_service(self, i):
        with (self.work / f'service{i}.log').open('a') as log:
            self.services[i] = subprocess.Popen([sys.executable,
                str(self.bundle / 'service/service.py'), '--socket', str(self.sockets[i]),
                '--config', str(self.cfg), '--work', str(self.work / f'service{i}'),
                '--card', str(i)], stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.wait_socket(self.sockets[i], self.services[i])

    def device_args(self):
        return [a.replace('p4-isolation=on,',
                f'p4-isolation=on,p5-flr=on,p5-fabric-socket={self.fabric_socket},')
                for a in super().device_args()]

    def close(self):
        try:
            super().close()
        finally:
            stop(self.fabric_process)
            self.fabric_socket.unlink(missing_ok=True)
