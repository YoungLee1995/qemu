#!/usr/bin/env python3
"""Submit a per-card runtime replacement from t100 via the 9p share."""
import argparse
import json
from pathlib import Path
import shutil
import time
import uuid


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--card', type=int, required=True)
    ap.add_argument('--mcu-bin', type=Path)
    ap.add_argument('--npu-bin', type=Path)
    ap.add_argument('--share', type=Path, default=Path('/mnt/hostshare'))
    ap.add_argument('--ipc-offset', type=lambda x: int(x, 0), help='IPC status offset within BAR4')
    ap.add_argument('--ipc-mask', type=lambda x: int(x, 0))
    ap.add_argument('--ipc-value', type=lambda x: int(x, 0))
    ap.add_argument('--timeout', type=int, default=120)
    args = ap.parse_args()
    if not (args.mcu_bin or args.npu_bin):
        ap.error('specify --mcu-bin and/or --npu-bin; omitted core retains its current image')
    ipc = (args.ipc_offset, args.ipc_mask, args.ipc_value)
    if any(x is not None for x in ipc) and any(x is None for x in ipc):
        ap.error('IPC offset/mask/value must be supplied together')
    if not 1 <= args.timeout <= 600:
        ap.error('--timeout must be 1..600 seconds')
    root = args.share / 'reload'
    if not (root / 'enabled.json').is_file():
        ap.error('desktop reload controller unavailable; restart with updated desktop.py/prepare.py')
    enabled = json.loads((root / 'enabled.json').read_text())
    if not 0 <= args.card < enabled['cards']:
        ap.error('card out of range')
    request = root / 'requests' / uuid.uuid4().hex
    request.mkdir(parents=True)
    spec = dict(card=args.card, timeout=args.timeout, generation=enabled['generation'])
    for core in ('mcu', 'npu'):
        source = getattr(args, core + '_bin')
        if source:
            shutil.copyfile(source, request / (core + '.bin'))
            spec[core] = core + '.bin'
    if args.ipc_offset is not None:
        spec['ipc'] = dict(offset=args.ipc_offset, mask=args.ipc_mask, value=args.ipc_value)
    (request / 'request.tmp').write_text(json.dumps(spec))
    (request / 'request.tmp').rename(request / 'request.json')
    print(f'Reload request {request.name}; keep all Host DMA/task programs stopped.', flush=True)
    end = time.monotonic() + args.timeout + 90
    while time.monotonic() < end:
        result = request / 'result.json'
        if result.exists():
            data = json.loads(result.read_text())
            print(json.dumps(data, indent=2, ensure_ascii=False))
            raise SystemExit(0 if data['ok'] else 1)
        time.sleep(.5)
    raise SystemExit(f'Controller timeout; inspect {request}; do not assume reload completed')


if __name__ == '__main__':
    main()
