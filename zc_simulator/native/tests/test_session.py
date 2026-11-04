"""Exercise native Spike protocol, malformed peers, reset, and reap with real ELF."""
import hashlib
import os
import re
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/p1'))
from protocol import (CAPS, MAX_PAYLOAD, REGIONS, HELLO, HELLO_ACK, MEM_SETUP,
                      MEM_READY, START, SOFTWARE_READY, PING, PONG, QUIESCE,
                      QUIESCED, RESET, RESET_ACK, STOP, STOPPED, ERROR, send, receive)
import fcntl

BUNDLE = ROOT / 'simu/current'
SPIKE = Path(os.environ.get('ZC_NATIVE_SPIKE', str(BUNDLE / 'bin/spike'))).resolve()


class NativeSessionTest(unittest.TestCase):
    def test_protocol_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='zc-native-test-') as tmp:
            work = Path(tmp)
            cfg = work / 'firmware.cfg'
            cfg.write_text('runtime_func:1\npc_num:1\n[1, 0x40000000, ' +
                           str((BUNDLE / 'images/mcu.bin').resolve()) +
                           ']\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
            identity = hashlib.sha256(cfg.read_bytes()).hexdigest()
            path = work / 'card.sock'
            command = [str(SPIKE), '--pcie-socket', str(path), '--pcie-config-sha256', identity,
                       '--pcie-log', str(work / 'interaction.log'),
                       '--isa=rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond',
                       '--runtime-func', str((BUNDLE / 'images/firmware.riscv').resolve()), '-cfg', str(cfg)]
            with (work / 'console.log').open('wb') as out:
                proc = subprocess.Popen(command, stdout=out, stderr=out)
                try:
                    end = time.monotonic() + 5
                    while not path.exists():
                        self.assertIsNone(proc.poll())
                        self.assertLess(time.monotonic(), end)
                        time.sleep(.02)
                    # A second listener cannot delete or replace the first socket.
                    self.assertNotEqual(subprocess.run(command, stdout=subprocess.DEVNULL,
                                                       stderr=subprocess.DEVNULL).returncode, 0)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as bad:
                        bad.settimeout(2)
                        bad.connect(str(path))
                        bad.send(b'bad header')
                        self.assertEqual(bad.recv(1024), b'')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as bad:
                        bad.settimeout(2)
                        bad.connect(str(path))
                        payload = struct.pack('<QII', CAPS | 16, MAX_PAYLOAD, 0) + bytes(32)
                        send(bad, HELLO, 0, 1200, 1, 1, payload)
                        frame, fds = receive(bad)
                        self.assertFalse(fds)
                        self.assertEqual(frame[0], ERROR)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
                        conn.settimeout(3)
                        conn.connect(str(path))
                        request, epoch = 0, 1
                        def exchange(kind, expected, payload=b'', fds=()):
                            nonlocal request
                            request += 1
                            send(conn, kind, 0, 1234, epoch, request, payload, fds)
                            frame, extra = receive(conn)
                            for fd in extra:
                                os.close(fd)
                            self.assertFalse(extra)
                            self.assertEqual(frame[:6], (expected, 0, 1234, epoch, request, 0))
                            return frame[6]
                        hello = struct.pack('<QII', CAPS | 16, MAX_PAYLOAD, 0) + bytes.fromhex(identity)
                        self.assertEqual(exchange(HELLO, HELLO_ACK, hello), hello)
                        fds = []
                        try:
                            for _, size in REGIONS:
                                fd = os.memfd_create('native-test', os.MFD_ALLOW_SEALING)
                                fds.append(fd)
                                os.ftruncate(fd, size)
                                fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                                            fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
                            regions = b''.join(struct.pack('<QQII', base, size, i, 0)
                                               for i, (base, size) in enumerate(REGIONS))
                            exchange(MEM_SETUP, MEM_READY, regions, fds)
                            exchange(START, SOFTWARE_READY)
                            time.sleep(.2)
                            for _ in range(3):
                                exchange(PING, PONG)
                            exchange(QUIESCE, QUIESCED)
                            epoch += 1
                            exchange(RESET, RESET_ACK)
                            exchange(START, SOFTWARE_READY)
                            time.sleep(.2)
                            exchange(PING, PONG)
                            exchange(STOP, STOPPED)
                        finally:
                            for fd in fds:
                                os.close(fd)
                    proc.send_signal(signal.SIGINT)
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertFalse(path.exists())
                    text = (work / 'interaction.log').read_text()
                    self.assertEqual(text.count('event=spawn '), 2)
                    self.assertEqual(text.count('event=reaped '), 2)
                    for pid in re.findall(r'event=spawn .*pid=(\d+)', text):
                        self.assertFalse(Path('/proc', pid).exists())
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
