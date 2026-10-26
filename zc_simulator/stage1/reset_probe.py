#!/usr/bin/env python3
"""Check EP reset while a transport request is incomplete using qtest/QMP."""
import json
from pathlib import Path
import socket
import subprocess
import time

root = Path(__file__).resolve().parents[3]
work = root / 'experiments/qemu_stage1' / f'reset-{time.time_ns()}'
work.mkdir()
backend = socket.socket(socket.AF_UNIX)
backend.bind(str(work / 'simu.sock'))
backend.listen(1)
backend.settimeout(10)
cmd = [str(root / 'qemu/build/qemu-system-x86_64'), '-machine', 'q35',
       '-accel', 'qtest', '-display', 'none', '-nodefaults',
       '-qtest', f'unix:{work}/qtest.sock,server=on,wait=off',
       '-qtest-log', '/dev/null',
       '-qmp', f'unix:{work}/qmp.sock,server=on,wait=off',
       '-chardev', f'socket,id=sim,path={work}/simu.sock',
       '-device', 'zc-pcie-sim,chardev=sim,addr=2']
with (work / 'qemu.log').open('w') as log:
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    try:
        peer, _ = backend.accept()
        peer.settimeout(10)
        channels = []
        for name in ('qtest', 'qmp'):
            s = socket.socket(socket.AF_UNIX)
            deadline = time.monotonic() + 10
            while True:
                try:
                    s.connect(str(work / f'{name}.sock'))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            s.settimeout(10)
            channels.append(s)
        qt = channels[0].makefile('rwb', buffering=0)
        qm = channels[1].makefile('rwb', buffering=0)
        qm.readline()

        def qtest(text):
            qt.write(text.encode() + b'\n')
            reply = qt.readline().decode().strip()
            assert reply.startswith('OK'), reply
            return reply.split()[1:]

        def qmp(name):
            qm.write(json.dumps({'execute': name}).encode() + b'\n')
            while True:
                msg = json.loads(qm.readline())
                if 'return' in msg:
                    return msg['return']
                if 'error' in msg:
                    raise RuntimeError(msg)

        qmp('qmp_capabilities')
        # Root-bus slot 2. Map BAR0 and enable memory decoding.
        qtest('outl 0xcf8 0x80001010')
        qtest('outl 0xcfc 0xe0000000')
        qtest('outl 0xcf8 0x80001004')
        qtest('outw 0xcfc 2')
        assert int(qtest('readl 0xe0000000')[0], 16) == 0x5a435031
        qtest('writel 0xe000010c 1')
        qtest('writel 0xe0000010 1')
        assert int(qtest('readl 0xe0000008')[0], 16) == 1
        data = bytearray()
        while len(data) < 160:
            data.extend(peer.recv(160-len(data)))
        qmp('system_reset')
        # Reset must close transport, preventing a late reply from completing
        # a new request. Restore config after PCI reset before reading status.
        assert peer.recv(1) == b''
        qtest('outl 0xcf8 0x80001010')
        qtest('outl 0xcfc 0xe0000000')
        qtest('outl 0xcf8 0x80001004')
        qtest('outw 0xcfc 2')
        assert int(qtest('readl 0xe0000008')[0], 16) == 0
        assert int(qtest('readl 0xe0000014')[0], 16) == 0
        assert int(qtest('readl 0xe0000020')[0], 16) == 0
        (work / 'results.json').write_text(json.dumps({'pass': True,
            'reset_inflight': True, 'transport_closed': True,
            'status_irq_cleared': True}, indent=2))
        print(work, 'RESET_PASS', flush=True)
        qmp('quit')
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        backend.close()
