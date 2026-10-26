#!/usr/bin/env python3
"""Local experiment: bounded socket adapter to the existing FIFO runtime."""
import argparse
import json
import os
from pathlib import Path
import select
import signal
import shutil
import socket
import struct
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument('--work', type=Path, required=True)
opts = parser.parse_args()
root = Path(__file__).resolve().parents[3]
work = opts.work.resolve()
work.mkdir(parents=True, exist_ok=True)
source = root / 'simulator/riscv-tests/packet_runtime_test'
subprocess.run(['make', '-C', str(source)], check=True)
for name in ('mcu.bin', 'npu.bin'):
    shutil.copy2(source / name, work / name)
shutil.copytree(root / 'moe_bf16_sint4_33x29x34_fix/data', work / 'data', dirs_exist_ok=True)
reference = sorted((root / 'experiments/single_die_packet').glob('mock-*/firmware.cfg'))[-1]
shutil.copy2(reference, work / 'firmware.cfg')
for name in ('inbox.fifo', 'outbox.fifo'):
    (work / name).unlink(missing_ok=True)
    os.mkfifo(work / name)
infd = os.open(work / 'inbox.fifo', os.O_RDWR | os.O_NONBLOCK)
outfd = os.open(work / 'outbox.fifo', os.O_RDWR | os.O_NONBLOCK)
sockpath = work / 'simu.sock'
sockpath.unlink(missing_ok=True)
server = socket.socket(socket.AF_UNIX)
server.bind(str(sockpath))
server.listen(1)
server.settimeout(120)
trace = []


def receive_exact(fd, n):
    data = bytearray()
    deadline = time.monotonic() + 30
    while len(data) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError('simu runtime reply timeout')
        chunk = os.read(fd, n - len(data))
        if not chunk:
            raise EOFError('FIFO EOF')
        data.extend(chunk)
    return bytes(data)


proc = None


def stop(signum, frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
try:
    conn, _ = server.accept()
    conn.settimeout(120)
    cmd = [str(root / 'simulator/riscv-isa-sim/build/spike'),
           '--isa=rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond',
           '--runtime-func', str(root / 'simulator/riscv-tests/firmware.riscv'),
           '-cfg', str(work / 'firmware.cfg')]
    with (work / 'spike.log').open('w') as log, conn:
        proc = subprocess.Popen(cmd, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        while True:
            packet = bytearray()
            while len(packet) < 160:
                chunk = conn.recv(160-len(packet))
                if not chunk:
                    raise EOFError('QEMU disconnected')
                packet.extend(chunk)
            ident = struct.unpack_from('<I', packet, 12)[0]
            (work / f'packet-{len(trace)}.bin').write_bytes(packet)
            if os.write(infd, packet) != 160:
                raise IOError('short FIFO packet write')
            header = receive_exact(outfd, 32)
            fields = struct.unpack('<8I', header)
            size = fields[5]
            if size not in (0, 33792):
                raise ValueError('invalid runtime payload length')
            payload = receive_exact(outfd, size)
            conn.sendall(header + payload)
            trace.append({'id': ident, 'status': fields[2], 'executions': fields[4],
                          'bytes': size})
            (work / 'trace.json').write_text(json.dumps(trace, indent=2))
            if struct.unpack_from('<I', packet, 20)[0] == 0 and fields[2] == 0:
                if proc.wait(timeout=10) != 0:
                    raise RuntimeError('simu failed at shutdown')
                # Leave the connection until QEMU closes, preserving final reply.
                while conn.recv(1024):
                    pass
                break
finally:
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait()
    os.close(infd)
    os.close(outfd)
    server.close()
