#!/usr/bin/env python3
"""Controlled EP transport fault injection; does not run the NPU model."""
import argparse
from pathlib import Path
import socket
import struct
import time

p = argparse.ArgumentParser()
p.add_argument('--work', type=Path, required=True)
p.add_argument('--fault', choices=['disconnect', 'timeout', 'wrong-id'], required=True)
a = p.parse_args()
a.work.mkdir(parents=True)
server = socket.socket(socket.AF_UNIX)
server.bind(str(a.work / 'simu.sock'))
server.listen(1)
server.settimeout(120)
with server, server.accept()[0] as conn:
    conn.settimeout(120)
    request = bytearray()
    while len(request) < 160:
        data = conn.recv(160-len(request))
        if not data:
            raise EOFError('disconnected before packet')
        request.extend(data)
    if a.fault == 'wrong-id':
        # A valid-looking header for a different command must not complete ours.
        conn.sendall(struct.pack('<8I', 0x5041434b, 9999, 0, 1, 1, 0, 0, 0))
        while conn.recv(1024):
            pass
    elif a.fault == 'timeout':
        # The EP must enforce its 30-second deadline and close the socket.
        while conn.recv(1024):
            pass
