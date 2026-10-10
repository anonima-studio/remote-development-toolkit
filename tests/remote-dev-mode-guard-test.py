#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("guard", Path(__file__).resolve().parents[1] / "templates/docker/mode-guard.py")
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / "project"
        self.root.mkdir()
        self.environment = patch.dict(os.environ, {"HOME": str(self.home)})
        self.environment.start()
        self.key = G.hashlib.sha256(os.fsencode(self.root)).hexdigest() + ".json"

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def write(self, group, value):
        path = self.home / ".local/share/remote-dev/mode-endpoints" / group / self.key
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_unbound_context_is_read_only(self):
        G.check(str(self.root))
        self.assertFalse((self.home / ".local").exists())

    def test_claim_requires_matching_operation_and_token(self):
        self.write("claims", {"root": str(self.root), "operation_id": "a" * 64, "token": "b" * 64})
        with self.assertRaisesRegex(RuntimeError, "IN_PROGRESS"):
            G.check(str(self.root / "child"))
        with patch.dict(os.environ, {"REMOTE_DEV_MODE_OPERATION_ID": "a" * 64, "REMOTE_DEV_MODE_TOKEN": "b" * 64}):
            G.check(str(self.root))

    def test_inactive_fence_cannot_be_bypassed_with_old_environment(self):
        self.write("fences", {"root": str(self.root), "state": "inactive", "operation_id": "a" * 64})
        with patch.dict(os.environ, {"REMOTE_DEV_MODE_OPERATION_ID": "a" * 64, "REMOTE_DEV_MODE_TOKEN": "b" * 64}):
            with self.assertRaisesRegex(RuntimeError, "SOURCE_INACTIVE"):
                G.check(str(self.root))

    def test_symlink_fence_is_rejected(self):
        path = self.write("fences", {"root": str(self.root), "state": "active"})
        other = path.with_suffix(".copy")
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(OSError):
            G.check(str(self.root))


if __name__ == "__main__":
    unittest.main()
