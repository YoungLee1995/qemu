"""Host supervisor for serialized, per-card firmware replacement."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import uuid

from paths import SOURCE, STATE


def atomic(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


class ReloadController:
    def __init__(self, platform, state):
        self.platform, self.state = platform, state
        self.root = STATE / 'share/reload'
        (self.root / 'requests').mkdir(parents=True, exist_ok=True)
        self.generation = uuid.uuid4().hex
        atomic(self.root / 'enabled.json', dict(cards=state['cards'], generation=self.generation))
        self.current = {i: json.loads(platform.cfg.read_text()) for i in range(state['cards'])}
        self.stopped = threading.Event()
        self.guest_proc = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def guest(self, spec):
        host = self.state['bind']
        if host == '0.0.0.0':
            host = '127.0.0.1'
        command = ['ssh', '-i', str(STATE / 'ssh_key'), '-p', str(self.state['ssh_port']),
                   '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                   '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
                   '-o', 'ConnectTimeout=10', 't100@' + host,
                   'sudo python3 - ' + shlex.quote(json.dumps(spec))]
        if self.stopped.is_set():
            raise RuntimeError('desktop is shutting down')
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.guest_proc = proc
        try:
            out, err = proc.communicate((SOURCE / 'reload_guest.py').read_text(), timeout=spec['timeout'] + 20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError('guest control timed out; operation may already have committed')
        finally:
            self.guest_proc = None
        if proc.returncode:
            raise RuntimeError(err[-3000:] or out[-3000:])
        return out.strip()

    def run(self):
        while not self.stopped.is_set():
            self.poll()
            self.stopped.wait(.5)

    def poll(self):
        for path in sorted((self.root / 'requests').glob('*/request.json')):
            if self.stopped.is_set():
                return
            result = path.parent / 'result.json'
            if result.exists():
                continue
            selected = False
            staged_pending = False
            pending = None
            try:
                spec = json.loads(path.read_text())
                if spec['generation'] != self.generation:
                    raise ValueError('stale request from an earlier VM session')
                card, timeout = spec['card'], spec['timeout']
                if type(card) is not int or card not in self.current or type(timeout) is not int or not 1 <= timeout <= 600:
                    raise ValueError('invalid card/timeout')
                ipc = spec.get('ipc')
                if ipc and (not 0 <= ipc['offset'] < 0x800000000 or ipc['offset'] % 4 or
                            not 0 < ipc['mask'] <= 0xffffffff or ipc['value'] & ~ipc['mask']):
                    raise ValueError('invalid IPC offset/mask/value')
                pending = self.platform.work / f'service{card}/reload-config.json'
                if pending.exists():
                    raise RuntimeError('previous reset did not consume pending configuration; inspect logs before retry')
                applied = pending.with_name('applied-config.json')
                original = json.loads(applied.read_text()) if applied.exists() else self.current[card]
                stage = self.platform.work / 'reloads' / path.parent.name
                stage.mkdir(parents=True, exist_ok=False)
                lines = Path(original['cfg']).read_text().splitlines()
                if 'pc_num:2' not in lines:
                    raise ValueError('reload requires the managed MCU+NPU desktop configuration')
                config = dict(original)
                from hashlib import sha256
                for core in ('mcu', 'npu'):
                    if core in spec:
                        source = path.parent / (core + '.bin')
                        if source.is_symlink() or source.resolve().parent != path.parent.resolve() or not source.is_file() or not 0 < source.stat().st_size <= 64 * 1024 * 1024:
                            raise ValueError('invalid or oversized BIN')
                        shutil.copyfile(source, stage / (core + '.bin'))
                    else:
                        shutil.copyfile(Path(original['cwd']) / (core + '.bin'), stage / (core + '.bin'))
                # Relative BINs stay local to this immutable input snapshot.
                (stage / 'firmware.cfg').write_text('\n'.join(lines) + '\n')
                config.update(cfg=str(stage / 'firmware.cfg'), cwd=str(stage))
                pins = [Path(config['spike']), Path(config['firmware']),
                        self.platform.bundle / 'bin/sm_system.cfg',
                        stage / 'firmware.cfg', stage / 'mcu.bin', stage / 'npu.bin']
                config['sha256'] = {str(p): sha256(p.read_bytes()).hexdigest() for p in pins}
                atomic(stage / 'backend.json', config)
                self.guest(dict(spec, action='preflight'))
                atomic(pending, config)
                staged_pending = True
                selected = True  # SSH reset can commit even if its reply is lost.
                self.guest(dict(spec, action='reset'))
                self.current[card] = config
                detail = json.loads(self.guest(dict(spec, action='wait')))
                if pending.exists():
                    raise RuntimeError('backend did not consume replacement configuration')
                atomic(result, dict(ok=True, card=card, detail=detail, inputs=str(stage),
                                    ipc_checked=bool(ipc), qemu_pid=self.state['qemu_pid']))
            except Exception as exc:
                # Never claim rollback: reset or IPC timeout may occur after commit.
                atomic(result, dict(ok=False, error=str(exc), replacement_may_be_active=selected,
                                    note='Inspect backend logs before submitting tasks; no automatic rollback.'))
                if pending is not None and pending.exists() and staged_pending and not selected:
                    pending.unlink()

    def close(self):
        self.stopped.set()
        if self.guest_proc is not None and self.guest_proc.poll() is None:
            self.guest_proc.kill()
        self.worker.join(timeout=5)
        (self.root / 'enabled.json').unlink(missing_ok=True)
