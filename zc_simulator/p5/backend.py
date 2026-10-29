"""Live P4 QEMU/Spike adapter for the P5 transaction scheduler."""
from pathlib import Path
import struct
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'p4'))
from cluster_platform import Platform, ROOT
sys.path.insert(0, str(ROOT / 'simulator/tools/p5'))
from fabric import Fabric, LinkConfig
from communication import Communication


class DMAAdapter:
    def __init__(self, platform, host_iova=None, host_gpas=None):
        self.p = platform
        self.host_iova, self.host_gpas = host_iova, host_gpas
        self.buffers = {}
        self.transfers = []
        self.reset_count = [0] * platform.count
        self._slot = 0

    def prepare(self, t, data):
        # A source buffer belongs to the ticket until wait completes. Bounded
        # local allocations are independent of its remote receive offset.
        self._slot += 1
        local = 0x20080000 + self._slot * 0x20000
        if local + t.length > 0x21000000:
            raise BufferError('adapter local buffer capacity')
        self.buffers[t.sequence] = local
        with self.p.backing(t.source, 1 << 30) as m:
            a = local - 0x20000000
            m[a:a+t.length] = data if data is not None else bytes(t.length)

    def address(self, t):
        if t.target == -1:
            if self.host_iova is None:
                raise ValueError('Host IOVA mapping not configured')
            return self.host_iova + t.offset
        return self.p.peer(t.target) + t.offset

    def transfer(self, t, packet):
        if not self.p.rd(self.p.cards[t.source]['bar0'] + 8):
            raise RuntimeError('source not ready')
        local = self.buffers[t.sequence] + packet.address - self.address(t)
        remote = packet.address
        read = not t.write
        # Pair 1 leaves runtime's pair 0 independent; direct PCIe addresses use
        # the supplied demo ATU_BYPASS control bit.
        a = self.p.cards[t.source]['bar0'] + 0x380200 + (0x100 if read else 0)
        src, dst = (remote, local) if read else (local, remote)
        for off, value in [(0, 1), (0x8c, 0xffffffff), (0x88, 0),
                           (0x34, 0x202), (0x1c, packet.length),
                           (0x20, src & 0xffffffff), (0x24, src >> 32),
                           (0x28, dst & 0xffffffff), (0x2c, dst >> 32), (4, 1)]:
            self.p.wr(a + off, value)
        if not self.p.wait(a + 0x80, 3, 10):
            self.p.wr(a, 0)
            raise TimeoutError('HDMA completion')
        status = self.p.rd(a + 0x84)
        self.transfers.append(dict(ticket=t.sequence, packet=packet.id, kind=packet.kind,
            source=t.source, target=t.target, address=packet.address, bytes=packet.length,
            status=status))
        if status & 5 != 1:
            raise RuntimeError(f'HDMA abort {status}')

    def result(self, t):
        if t.write:
            address = self.host_gpas[t.source] + t.offset if t.target == -1 else self.address(t)
            return self.p.read(address, t.length)
        with self.p.backing(t.source, 1 << 30) as m:
            a = self.buffers[t.sequence] - 0x20000000
            return bytes(m[a:a+t.length])

    def reset(self, card):
        # A communication queue reset stops the owned channel pair; it is not
        # advertised as PCIe FLR or a hardware hart-reset register.
        for off in (0, 0x100):
            a = self.p.cards[card]['bar0'] + 0x380200 + off
            self.p.wr(a, 0)
            self.p.wr(a + 0x8c, 0xffffffff)
        self.reset_count[card] += 1
