#!/usr/bin/env python3
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inventory", REPOSITORY / "bin/remote-dev-tracked-inventory.py")
I = importlib.util.module_from_spec(spec)
spec.loader.exec_module(I)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        for name in ("tracked", "deleted"):
            (self.root / name).write_text(name)
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.manifest = self.base / "manifest.tsv"

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True).stdout

    def manifest_for_existing(self):
        rows = ["path\ttype\tmode\tdigest\tsymlink_target\tstate"]
        for path in sorted(self.root.iterdir()):
            if path.is_file():
                rows.append("\t".join([path.name, "file", "%04o" % (path.stat().st_mode & 0o777),
                                       hashlib.sha256(path.read_bytes()).hexdigest(), "-", "present"]))
        self.manifest.write_text("\n".join(rows) + "\n")

    def capture(self, receipt="execution-1"):
        return I.capture(self.root, self.manifest, "/home/coder/projects/fixture", receipt)

    def test_cached_existing_semantics_include_staged_additions_ignore_untracked_and_deleted(self):
        (self.root / "added").write_text("staged addition")
        self.git("add", "added")
        (self.root / "untracked").write_text("ignored by knowledge check")
        (self.root / "deleted").unlink()
        self.manifest_for_existing()
        captured = self.capture()
        self.assertEqual(captured["files"], ["added", "tracked"])
        git_paths = self.git("ls-files", "--cached", "-z").decode().split("\0")
        self.assertEqual(captured["files"], sorted(p for p in git_paths if p and (self.root / p).exists()))

    def test_same_worktree_with_changed_index_has_new_metadata_identity(self):
        self.manifest_for_existing()
        first = self.capture()
        (self.root / "tracked").write_text("unstaged bytes")
        self.manifest_for_existing()
        before_stage = self.capture()
        self.git("add", "tracked")
        after_stage = self.capture("execution-2")
        self.assertEqual(before_stage["projection_manifest_id"], after_stage["projection_manifest_id"])
        self.assertNotEqual(before_stage["source_index_id"], after_stage["source_index_id"])

    def test_excluded_tracked_content_is_a_failure_not_silently_omitted(self):
        self.manifest.write_text("path\ttype\tmode\tdigest\tsymlink_target\tstate\n")
        with self.assertRaisesRegex(I.InventoryError, "excluded or differs"):
            self.capture()

    def test_source_changed_after_projection_is_rejected(self):
        self.manifest_for_existing()
        (self.root / "tracked").write_text("new bytes")
        with self.assertRaisesRegex(I.InventoryError, "differs"):
            self.capture()


if __name__ == "__main__":
    unittest.main()
