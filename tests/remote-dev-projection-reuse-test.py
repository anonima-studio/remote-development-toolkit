"""Validate that staging reuse depends on fresh content and stable sources."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / 'bin/remote-dev-local-agent-working-set.py'
spec = importlib.util.spec_from_file_location('working_set', HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class ProjectionReuseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / 'existing'
        self.source = self.stage / 'member' / 'source'
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'original')
        self.source.chmod(0o644)
        self.row = ('member/source', 'file', '0644', hashlib.sha256(b'original').hexdigest(), '-', 'present')

    def matches(self, rows=None):
        return helper.projection_matches(self.stage, rows or [self.row])

    def test_same_size_same_mtime_change_is_not_reused(self):
        self.assertTrue(self.matches())
        original = self.source.stat()
        self.source.write_bytes(b'modified')
        os.utime(self.source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertFalse(self.matches())

    def test_modes_missing_extra_paths_and_symlinks(self):
        self.source.chmod(0o755)
        self.assertFalse(self.matches())
        self.source.chmod(0o644)
        extra = self.stage / 'unexpected'
        extra.mkdir()
        self.assertFalse(self.matches())
        extra.rmdir()
        self.source.unlink()
        self.assertFalse(self.matches())
        self.source.symlink_to('/tmp/foreign')
        self.assertFalse(self.matches())
        row = ('member/source', 'symlink', '0777', '-', '/tmp/foreign', 'present')
        self.assertTrue(self.matches([row]))
        self.assertFalse(self.matches([(*row[:4], '/tmp/other', 'present')]))
        self.assertFalse(self.matches([(*row[:5], 'deleted')]))

    def test_stable_double_inventory_reuses_without_writing_stage(self):
        before = self.source.stat()
        inventory = ([self.row], [], {'member': 'digest'})
        with patch.object(helper, 'source_inventory', return_value=inventory) as scan:
            reused = helper.materialize({}, {}, self.root/'new', self.root/'managed',
                                        self.root/'excluded', self.root/'digests', self.stage)
        self.assertTrue(reused)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.source.stat().st_ino, before.st_ino)

    def test_changing_inventory_never_accepts_reuse(self):
        first = ([self.row], [], {'member': 'digest'})
        changed = ([self.row], [('private', 'observed_ignored')], {'member': 'digest'})
        with patch.object(helper, 'source_inventory', side_effect=[first, changed]*3):
            with self.assertRaisesRegex(helper.WorkingSetError, 'SOURCE_CHANGED'):
                helper.materialize({}, {}, self.root/'new', self.root/'managed',
                                   self.root/'excluded', self.root/'digests', self.stage)


if __name__ == '__main__':
    unittest.main()
