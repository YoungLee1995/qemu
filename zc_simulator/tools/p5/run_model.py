#!/usr/bin/env python3
"""Run a reproducible protocol/timing experiment without physical hardware."""
import argparse
import json
from pathlib import Path
from fabric import Fabric, LinkConfig


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',type=Path,required=True)
    ap.add_argument('--requests',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    config=json.loads(args.config.read_text())
    config['default']=LinkConfig(**config.get('default',{}))
    config['links']={name:LinkConfig(**value) for name,value in config.get('links',{}).items()}
    f=Fabric(**config)
    for request in json.loads(args.requests.read_text()):f.submit(**request)
    report=f.run()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2))
    if not report['credits_balanced'] or any(r['state']!='complete' for r in report['requests']):
        raise SystemExit('protocol model did not complete all requests')
    print(f'PASS requests={len(f.requests)} packets={len(f.packets)} virtual_ns={f.now}')

if __name__=='__main__':main()
