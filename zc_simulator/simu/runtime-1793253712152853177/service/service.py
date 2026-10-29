#!/usr/bin/env python3
"""Persistent single-card backend supervisor. Never mutates a hart or ECLIC.

One QEMU seqpacket connection owns one session. QEMU supplies sealed memfds;
Spike inherits validated descriptors only. All model state stays on its thread.
"""
import argparse
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import time

from protocol import (CAPS, CONTROL, ERROR, HELLO, HELLO_ACK, MAX_PAYLOAD,
                      MEM_READY, MEM_SETUP, PING, PONG, ProtocolError,
                      QUIESCE, QUIESCED, REGIONS, RESET, RESET_ACK, SOFTWARE_READY,
                      START, STOP, STOPPED, receive, send)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Session:
    def __init__(self, sock, config, work, card, ready_timeout, log):
        self.sock, self.config, self.work, self.card = sock, config, work, card
        self.ready_timeout, self.log = ready_timeout, log
        self.session = None
        self.epoch = 0
        self.last_request = 0
        self.fds, self.maps = [], []
        self.proc = None
        self.state = 'HELLO'
        self.pending = None
        self.deadline = time.monotonic() + 5
        self.run_number = 0

    def record(self, event, **values):
        self.log.write(json.dumps(dict(time_ns=time.time_ns(), card=self.card,
            session=self.session, epoch=self.epoch, state=self.state, event=event, **values))+'\n')
        self.log.flush()

    def reply(self, kind, request, payload=b''):
        send(self.sock, kind, self.card, self.session, self.epoch, request, payload)
        self.record('reply', kind=kind, request=request)

    def alive(self, value):
        if self.maps:
            struct.pack_into('<I', self.maps[2], 0x408, value)

    def reap(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
            else:
                self.proc.wait()
            self.record('reaped', pid=self.proc.pid, exit_code=self.proc.returncode)
            self.proc = None
        self.alive(0)

    def clear(self):
        # Called only after process reaping. Old writes cannot race new state.
        self.maps[2][:] = bytes(4096)
        struct.pack_into('<III', self.maps[2], 0x400, self.epoch, self.epoch, 0)
        self.maps[1][CONTROL:CONTROL+65536] = bytes(65536)

    def map_regions(self, payload, fds):
        if len(payload) != 3*24 or len(fds) != 3:
            raise ProtocolError('MEM_SETUP needs three region descriptors/FDs')
        mappings = []
        try:
            for i, (base, size) in enumerate(REGIONS):
                actual = struct.unpack_from('<QQII', payload, i*24)
                if actual != (base, size, i, 0):
                    raise ProtocolError('region base/size/index/flags mismatch')
                if os.fstat(fds[i]).st_size != size:
                    raise ProtocolError('FD size mismatch')
                seals = fcntl.fcntl(fds[i], fcntl.F_GET_SEALS)
                required = fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL
                if seals & required != required:
                    raise ProtocolError('FD not sealed against resize')
                mappings.append(mmap.mmap(fds[i], size))
            # Different logical regions may not alias the same underlying file.
            identities = [(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in fds]
            if len(set(identities)) != 3:
                raise ProtocolError('aliased region FDs')
        except BaseException:
            for m in mappings:
                m.close()
            raise
        self.fds, self.maps = fds[:], mappings
        fds.clear()  # Session owns these now.
        self.clear()

    def start(self, request):
        # Recheck content pins on every start/reset, not only at service boot.
        for name, expected in self.config['sha256'].items():
            if sha(name) != expected:
                raise ProtocolError('configured image/input changed: '+name)
        self.run_number += 1
        log = (self.work/f'spike-{self.session}-{self.run_number}.log').open('w')
        cmd = [self.config['spike'], f'--p1-ddr0-fd={self.fds[0]}',
               f'--p1-ddr1-fd={self.fds[1]}', '--isa='+self.config['isa'],
               '--runtime-func', self.config['firmware'], '-cfg', self.config['cfg']]
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.config['cwd'], pass_fds=self.fds,
                env=dict(os.environ, ZC_P1_MAILBOX_FD=str(self.fds[2]),
                         ZC_P1_PARENT_PID=str(os.getpid()),
                         ZC_P2_PCIE="1" if self.config.get("p2", False) else "0"),
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self.record('spawn', pid=self.proc.pid)
        self.state, self.pending = 'STARTING', request
        self.deadline = time.monotonic() + self.ready_timeout

    def handle(self, frame, fds):
        kind, card, session, epoch, request, flags, payload = frame
        if (card != self.card or flags or not request or request <= self.last_request or
                (self.session is not None and session != self.session)):
            raise ProtocolError('card/session/request mismatch or duplicate')
        if self.session is None:
            self.session = session
            self.epoch = epoch
        if kind == RESET:
            if self.state != 'QUIESCED' or epoch <= self.epoch:
                raise ProtocolError('RESET needs quiesce and increasing epoch')
        elif epoch != self.epoch:
            raise ProtocolError('stale epoch')
        self.last_request = request
        self.record('request', kind=kind, request=request)
        if kind != MEM_SETUP and fds:
            raise ProtocolError('unexpected FDs')
        if kind == HELLO and self.state == 'HELLO':
            expected = struct.pack('<QII', CAPS | (16 if self.config.get('p2', False) else 0), MAX_PAYLOAD, 0) + bytes.fromhex(self.config['digest'])
            if payload != expected:
                raise ProtocolError('capabilities/max payload/configuration mismatch')
            self.reply(HELLO_ACK, request, expected)
            self.state = 'MEMORY'
            self.deadline = time.monotonic()+5
        elif kind == MEM_SETUP and self.state == 'MEMORY':
            self.map_regions(payload, fds)
            self.state = 'MEMORY_READY'
            self.reply(MEM_READY, request)
            self.deadline = time.monotonic()+30
        elif kind == START and self.state == 'MEMORY_READY' and not payload:
            self.start(request)
        elif kind in (QUIESCE, STOP) and self.state in ('READY', 'STOPPED', 'MEMORY_READY', 'QUIESCED') and not payload:
            self.reap()
            self.state = 'QUIESCED' if kind == QUIESCE else 'STOPPED'
            self.reply(QUIESCED if kind == QUIESCE else STOPPED, request, struct.pack('<I', 0))
            self.deadline = time.monotonic()+30
        elif kind == RESET and not payload:
            self.reap()
            self.epoch = epoch
            self.clear()
            self.state = 'MEMORY_READY'
            self.reply(RESET_ACK, request)
            self.deadline = time.monotonic()+30
        elif kind == PING and self.state in ('READY', 'STOPPED') and not payload:
            self.reply(PONG, request)
            self.deadline = time.monotonic()+30
        else:
            raise ProtocolError('unsupported request/state/payload')

    def tick(self):
        if self.state == 'STARTING':
            if self.proc.poll() is not None:
                raise ProtocolError('backend exited before software ready')
            if struct.unpack_from('<II', self.maps[1], CONTROL+16) == (1, 1):
                struct.pack_into('<II', self.maps[2], 0x404, self.epoch, 1)
                self.state = 'READY'
                self.reply(SOFTWARE_READY, self.pending)
                self.pending = None
                self.deadline = time.monotonic()+30
        elif self.state == 'READY' and self.proc.poll() is not None:
            code = self.proc.returncode
            self.reap()
            self.state = 'STOPPED' if code == 0 else 'FAILED'
            if code == 0:
                send(self.sock, STOPPED, self.card, self.session,
                     self.epoch, 0, struct.pack('<i', code), flags=1)
            self.record('exit_event', exit_code=code)
            self.deadline = time.monotonic()+30
            if code:
                raise ProtocolError('backend abnormal exit')
        if time.monotonic() > self.deadline:
            raise ProtocolError('state/ready/heartbeat timeout')

    def close(self):
        self.reap()
        for m in self.maps:
            m.close()
        for fd in self.fds:
            os.close(fd)
        self.maps, self.fds = [], []
        self.sock.close()
        self.record('session_closed')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--socket', required=True, type=Path)
    ap.add_argument('--config', required=True, type=Path)
    ap.add_argument('--work', required=True, type=Path)
    ap.add_argument('--card', type=int, default=0)
    ap.add_argument('--ready-timeout', type=float, default=20)
    args = ap.parse_args()
    config = json.loads(args.config.read_text())
    if 'p2' in config and not isinstance(config['p2'], bool):
        raise ValueError('p2 must be a JSON boolean')
    config['digest'] = sha(args.config)
    args.work.mkdir(parents=True, exist_ok=True)
    # Do not replace an existing listening service or unlink another socket.
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.bind(str(args.socket));server.listen(4);server.setblocking(False)
    running = True
    def shutdown(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, shutdown);signal.signal(signal.SIGINT, shutdown)
    selector = selectors.DefaultSelector();selector.register(server, selectors.EVENT_READ)
    current = None
    with (args.work/'service.jsonl').open('a') as log:
        print('P1_SERVICE_READY '+config['digest'], flush=True)
        try:
            while running:
                for key, _ in selector.select(.005):
                    if key.fileobj is server:
                        client, _ = server.accept();client.settimeout(.1)
                        if current is not None:
                            client.close()  # Only one owner per configured card.
                        else:
                            current = Session(client, config, args.work, args.card, args.ready_timeout, log)
                            selector.register(client, selectors.EVENT_READ)
                    elif current:
                        fds = []
                        try:
                            frame, fds = receive(current.sock)
                            current.handle(frame, fds)
                        except (EOFError, OSError, ProtocolError, ValueError) as e:
                            current.record('failure', detail=str(e))
                            try:
                                if current.session:
                                    current.reply(ERROR, current.last_request, str(e).encode()[:1024])
                            except OSError:
                                pass
                            selector.unregister(current.sock);current.close();current=None
                        finally:
                            for fd in fds:
                                os.close(fd)
                if current:
                    try:
                        current.tick()
                    except (OSError, ProtocolError) as e:
                        current.record('failure', detail=str(e))
                        try:
                            current.reply(ERROR, current.pending or 0, str(e).encode()[:1024])
                        except OSError:
                            pass
                        selector.unregister(current.sock);current.close();current=None
        finally:
            if current:
                current.close()
            selector.close();server.close();args.socket.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
