"""Root-only guest half of reload: inspect users/DMA, FLR, transport and IPC."""
import json
import mmap
from pathlib import Path
import struct
import sys
import time


def main():
    spec = json.loads(sys.argv[1])
    devices = sorted(p for p in Path('/sys/bus/pci/devices').iterdir()
                     if (p / 'vendor').read_text().strip() == '0x16c3'
                     and (p / 'device').read_text().strip() == '0xabcd')
    target = devices[spec['card']]
    action = spec['action']
    if action == 'preflight':
        # Current reference driver does not arbitrate concurrent mmap owners.
        # Reject users of any card because peer DMA can target the reset card.
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                maps = (proc / 'maps').read_text()
                links = [str(p.readlink()) for p in (proc / 'fd').iterdir()]
            except (FileNotFoundError, ProcessLookupError):
                continue
            if '/dev/test_dma_' in maps or '/resource' in maps and '/sys/' in maps or any(
                    '/dev/test_dma_' in x or '/sys/' in x and '/resource' in x for x in links):
                raise RuntimeError(f'Host mapping/file still open in PID {proc.name}; stop all card clients first')
        for dev in devices:
            with (dev / 'resource0').open('r+b', buffering=0) as f:
                with mmap.mmap(f.fileno(), 0x400000) as regs:
                    # Sixteen model HDMA channels; state 1 is in flight.
                    if any(struct.unpack_from('<I', regs, 0x380080 + i * 0x100)[0] == 1
                           for i in range(16)):
                        raise RuntimeError(f'DMA still active on {dev.name}; wait for completion')
            with (dev / 'resource4').open('r+b', buffering=0) as f:
                with mmap.mmap(f.fileno(), 4096, offset=0x10000000) as service:
                    issued = struct.unpack_from('<I', service, 0x100)[0]
                    done = struct.unpack_from('<I', service, 0x2c)[0]
                    if issued != done:
                        raise RuntimeError(f'reference task still pending on {dev.name}')
        if not (target / 'reset').exists():
            raise RuntimeError('PCI function reset unavailable')
        print(json.dumps(dict(bdf=target.name)))
    elif action == 'reset':
        # Kernel saves/restores PCI config around FLR, keeping the driver bound.
        (target / 'reset').write_text('1')
    elif action == 'wait':
        end = time.monotonic() + spec['timeout']
        with (target / 'resource0').open('r+b', buffering=0) as f:
            with mmap.mmap(f.fileno(), 4096) as regs:
                while struct.unpack_from('<I', regs, 8)[0] != 1:
                    if time.monotonic() > end:
                        raise TimeoutError('backend transport not ready')
                    time.sleep(.05)
        ipc = spec.get('ipc')
        if ipc:
            offset = ipc['offset']
            base = offset & ~(mmap.PAGESIZE - 1)
            with (target / 'resource4').open('r+b', buffering=0) as f:
                with mmap.mmap(f.fileno(), mmap.PAGESIZE, offset=base) as regs:
                    while struct.unpack_from('<I', regs, offset - base)[0] & ipc['mask'] != ipc['value']:
                        if time.monotonic() > end:
                            raise TimeoutError('SCU/IPC device-ready timeout (new backend remains selected)')
                        time.sleep(.01)
        print(json.dumps(dict(bdf=target.name, transport_ready=True, ipc_ready=bool(ipc))))
    else:
        raise ValueError(action)


if __name__ == '__main__':
    main()
