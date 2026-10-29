"""Internal P1 v1, explicit little-endian seqpacket framing (not a driver ABI)."""
import array
import os
import socket
import struct

MAGIC = 0x5a435030
VERSION = 1
MAX_PAYLOAD = 65536
HEADER = struct.Struct('<IHHIIQQQII')
HELLO, HELLO_ACK, MEM_SETUP, MEM_READY, START, SOFTWARE_READY = range(1, 7)
QUIESCE, QUIESCED, RESET, RESET_ACK, STOP, STOPPED, ERROR, PING, PONG = range(7, 16)
CAPS = 15  # shared DDR, shared mailbox, reset, stop (no DMA/REG RPC capability)
REGIONS = [(0x20000000, 1 << 30), (0x4000000000, 1 << 38), (0x18019000, 4096)]
CONTROL = 0x100100000


class ProtocolError(Exception):
    pass


def encode(kind, card, session, epoch, request, payload=b'', flags=0):
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError('payload too large')
    return HEADER.pack(MAGIC, VERSION, kind, len(payload), card, session,
                       epoch, request, flags, 0) + payload


def send(sock, kind, card, session, epoch, request, payload=b'', fds=(), flags=0):
    data = encode(kind, card, session, epoch, request, payload, flags)
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', fds))] if fds else []
    n = sock.sendmsg([data], ancillary, socket.MSG_NOSIGNAL)
    if n != len(data):
        raise ProtocolError('short seqpacket send')


def receive(sock):
    data, ancillary, flags, _ = sock.recvmsg(HEADER.size + MAX_PAYLOAD,
                                          socket.CMSG_SPACE(16 * 4), socket.MSG_CMSG_CLOEXEC)
    fds = []
    try:
        for level, kind, value in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise ProtocolError('unexpected ancillary data')
            values = array.array('i')
            values.frombytes(value[:len(value) - len(value) % values.itemsize])
            fds.extend(values)
        if not data:
            raise EOFError('peer disconnected')
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(data) < HEADER.size:
            raise ProtocolError('truncated frame')
        magic, version, kind, length, card, session, epoch, request, bits, reserved = HEADER.unpack_from(data)
        if (magic != MAGIC or version != VERSION or length > MAX_PAYLOAD or
                len(data) != HEADER.size + length or reserved or bits not in (0, 1) or
                not session):
            raise ProtocolError('invalid header/version/length/identity')
        return (kind, card, session, epoch, request, bits, data[HEADER.size:]), fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
