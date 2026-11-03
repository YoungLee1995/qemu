"""Transport startup must never depend on firmware-private DDR ready flags."""
import importlib.util
from pathlib import Path
import struct
import sys
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools/p1'))
from protocol import ProtocolError, SOFTWARE_READY


class UnreadableDDR:
    def __getitem__(self, key):
        raise AssertionError('transport must not read firmware-private DDR')


class TransportStartTest(unittest.TestCase):
    def test_source_and_published_services(self):
        for path in (ROOT / 'tools/p1/service.py', ROOT / 'simu/current/service/service.py'):
            with self.subTest(path=path):
                spec = importlib.util.spec_from_file_location('tested_service', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                session = module.Session.__new__(module.Session)
                session.proc = Mock()
                session.proc.poll.return_value = None
                session.state = 'STARTING'
                session.maps = [UnreadableDDR(), UnreadableDDR(), bytearray(4096)]
                session.epoch, session.pending = 7, 42
                session.reply = Mock()
                session.deadline = time.monotonic() + 1
                session.tick()
                self.assertEqual(session.state, 'READY')
                session.reply.assert_called_once_with(SOFTWARE_READY, 42)
                self.assertEqual(struct.unpack_from('<II', session.maps[2], 0x404), (7, 1))
                session.state, session.pending = 'STARTING', 43
                session.reply.reset_mock()
                session.proc.poll.return_value = 1
                with self.assertRaises(ProtocolError):
                    session.tick()
                session.reply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
