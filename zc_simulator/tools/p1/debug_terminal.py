"""PTY bridge for a single Spike debugger, independent of protocol heartbeats."""
import fcntl
import os
from pathlib import Path
import pty
import select
import socket
import termios
import threading


class DebugTerminal:
    def __init__(self, path, log):
        self.path, self.log = Path(path), log
        self.master, self.slave = pty.openpty()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.server.bind(str(path))
            self.server.listen(1)
        except OSError:
            self.server.close()
            os.close(self.master)
            os.close(self.slave)
            raise
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()

    def relay(self):
        client = None
        history = bytearray()
        try:
            while not self.stop.is_set():
                sources = [self.server, self.master] + ([client] if client else [])
                for source in select.select(sources, [], [], .1)[0]:
                    if source is self.server:
                        new, _ = self.server.accept()
                        if client:
                            new.close()
                        else:
                            client = new
                            client.settimeout(.2)
                            try:
                                client.sendall(history)
                            except OSError:
                                client.close()
                                client = None
                    elif source == self.master:
                        data = os.read(self.master, 65536)
                        if not data:
                            return
                        self.log.write(data)
                        self.log.flush()
                        history.extend(data)
                        del history[:-65536]
                        if client:
                            try:
                                client.sendall(data)
                            except OSError:
                                client.close()
                                client = None
                    else:
                        try:
                            data = client.recv(4096)
                        except OSError:
                            data = b''
                        if data:
                            while data:
                                data = data[os.write(self.master, data):]
                        else:
                            client.close()
                            client = None
        except OSError:
            pass
        finally:
            if client:
                client.close()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        self.server.close()
        os.close(self.master)
        os.close(self.slave)
        self.path.unlink(missing_ok=True)
        self.log.close()


if __name__ == '__main__':
    import sys
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv(sys.argv[1], sys.argv[1:])
