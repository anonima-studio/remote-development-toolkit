#!/usr/bin/env python3
"""Exercise binary framing and receipt privacy with a subprocess execution owner."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transport', ROOT / 'bin/remote-dev-secret-runner-upload.py')
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)


class TransportTests(unittest.TestCase):
    def test_buffered_binary_tail_and_capability_never_enter_receipt_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = root / 'owner'
            receipt, output = root / 'argv.json', root / 'received'
            owner.write_text('''#!/usr/bin/env python3
import json,os,subprocess,sys
from pathlib import Path
Path(os.environ['FIXTURE_RECEIPT']).write_text(json.dumps(sys.argv[1:]))
sys.exit(subprocess.run(sys.argv[sys.argv.index('--')+1:]).returncode)
''')
            owner.chmod(0o700)
            values = {key: '' for key in T.KEYS}
            values.update(REMOTE_DEV_MODE_TOKEN='synthetic-private-capability', REMOTE_DEV_SECRET_REL='.env',
                          REMOTE_DEV_SECRET_ROOT=str(root), FIXTURE_RECEIPT=str(receipt),
                          REMOTE_DEV_SECRET_UPLOAD_SOURCE='from pathlib import Path\nimport os,sys\nPath(os.environ["REMOTE_DEV_SECRET_ROOT"],"received").write_bytes(sys.stdin.buffer.read())\n')
            payload = b'\x00\xff\n{"binary":"tail"}\n' * 100000
            with patch.dict(os.environ, values):
                self.assertEqual(T.transfer(owner, 'fixture', 'https://coder.example.invalid', str(root), io.BytesIO(payload)), 0)
            self.assertEqual(output.read_bytes(), payload)
            argv = json.loads(receipt.read_text())
            self.assertIn('exec', argv)
            self.assertEqual(argv[argv.index('--for-context') + 1], str(root))
            self.assertNotIn(values['REMOTE_DEV_MODE_TOKEN'], receipt.read_text())
            self.assertNotIn(values['REMOTE_DEV_SECRET_UPLOAD_SOURCE'], receipt.read_text())

    def test_early_owner_refusal_preserves_its_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / 'owner'
            owner.write_text('#!/bin/sh\nexit 79\n')
            owner.chmod(0o700)
            with patch.dict(os.environ, {'REMOTE_DEV_SECRET_UPLOAD_SOURCE': 'pass'}):
                self.assertEqual(T.transfer(owner, 'fixture', 'https://coder.example.invalid', temporary,
                                            io.BytesIO(b'x' * (2 * T.LIMIT))), 79)

    def test_bounded_header_refuses_before_owner_launch(self):
        with patch.dict(os.environ, {'REMOTE_DEV_SECRET_UPLOAD_SOURCE': 'x' * T.LIMIT}), patch.object(T.subprocess, 'Popen') as start:
            with self.assertRaisesRegex(RuntimeError, 'SECRET_ENVELOPE_TOO_LARGE'):
                T.transfer(Path('/unused'), 'fixture', 'https://coder.example.invalid', '/unused', io.BytesIO(b'payload'))
            start.assert_not_called()

    def test_receiver_rejects_unbounded_or_unframed_metadata(self):
        for header in (b'x' * (T.LIMIT + 1), b'{}', b'{"version":1}\n'):
            result = subprocess.run([sys.executable, '-c', T.BOOTSTRAP], input=header, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b'')
            self.assertIn(b'SECRET_ENVELOPE_INVALID', result.stderr)


if __name__ == '__main__':
    unittest.main()
