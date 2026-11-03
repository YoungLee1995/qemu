"""Reload is committed only by RESET after QUIESCE and validates new pins."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Session, sha
from protocol import ProtocolError, RESET, RESET_ACK


class ReloadTest(unittest.TestCase):
    def test_reset_commit_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            image = work / 'mcu.bin'
            image.write_bytes(b'new image')
            replacement = {'sha256': {str(image): sha(image)}, 'cfg': 'new.cfg'}
            session = Session(Mock(), {'digest': 'original identity'}, work, 0, 20, io.StringIO())
            session.session = 12
            session.state = 'QUIESCED'
            session.clear = Mock()
            session.reply = Mock()
            pending = work / 'reload-config.json'
            pending.write_text(json.dumps(replacement))
            session.handle((RESET, 0, 12, 1, 1, 0, b''), [])
            self.assertEqual(session.config['digest'], 'original identity')
            self.assertEqual(session.config['cfg'], 'new.cfg')
            self.assertFalse(pending.exists())
            self.assertTrue((work / 'applied-config.json').is_file())
            session.reply.assert_called_once_with(RESET_ACK, 1)
            session.state = 'QUIESCED'
            pending.write_text(json.dumps(replacement))
            image.write_bytes(b'tampered')
            session.clear.reset_mock()
            with self.assertRaises(ProtocolError):
                session.handle((RESET, 0, 12, 2, 2, 0, b''), [])
            session.clear.assert_not_called()


if __name__ == '__main__':
    unittest.main()
