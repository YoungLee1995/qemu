"""Exercise controlling PTY, detach/reconnect, transcript, and cleanup."""
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from debug_terminal import DebugTerminal
import debug_terminal


def receive_until(conn, marker):
    result = b''
    while marker not in result:
        chunk = conn.recv(65536)
        if not chunk:
            raise AssertionError('debug socket closed before expected output')
        result += chunk
    return result


class DebugTerminalTest(unittest.TestCase):
    def test_detach_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, log = Path(tmp) / 'debug.sock', Path(tmp) / 'spike.log'
            terminal = DebugTerminal(path, log.open('wb'))
            code = ('import os; '
                    'assert os.tcgetpgrp(0) == os.getpgrp(); '
                    'print("PROMPT", flush=True); '
                    'print("REPLY:" + input(), flush=True); '
                    'print("AGAIN:" + input(), flush=True)')
            proc = subprocess.Popen(
                [sys.executable, debug_terminal.__file__, sys.executable, '-c', code],
                stdin=terminal.slave, stdout=terminal.slave, stderr=terminal.slave)
            try:
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(3)
                    conn.connect(str(path))
                    receive_until(conn, b'PROMPT')
                    conn.sendall(b'first\n')
                    receive_until(conn, b'REPLY:first')
                    # Half-close confirms relay consumed EOF before reconnect.
                    conn.shutdown(socket.SHUT_WR)
                    while conn.recv(65536):
                        pass
                self.assertIsNone(proc.poll())
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(3)
                    conn.connect(str(path))
                    replay = receive_until(conn, b'REPLY:first')
                    self.assertIn(b'PROMPT', replay)
                    conn.sendall(b'second\n')
                    receive_until(conn, b'AGAIN:second')
                self.assertEqual(proc.wait(timeout=3), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                terminal.close()
            self.assertFalse(path.exists())
            self.assertFalse(terminal.thread.is_alive())
            self.assertIn(b'AGAIN:second', log.read_bytes())


if __name__ == '__main__':
    unittest.main()
