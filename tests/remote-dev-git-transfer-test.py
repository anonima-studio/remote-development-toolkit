#!/usr/bin/env python3
"""Exercise real Git state through the transfer CLI in disposable repositories."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


TOOL = Path(__file__).resolve().parents[1] / "bin/remote-dev-git-transfer.py"


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remote-dev-transfer-test-")
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Transfer Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("shared.txt", b"original\n")
        self.write("deleted.txt", b"delete this\n")
        self.write("executable", b"#!/bin/sh\nexit 0\n")
        self.write("dir/child", b"child\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial fixture")
        self.git("remote", "add", "origin", "https://example.invalid/fixture.git")
        self.git("config", "branch.main.remote", "origin")
        self.git("config", "branch.main.merge", "refs/heads/main")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args, root=None, data=None):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        result = subprocess.run(["git", "-C", str(root or self.source), *args], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def write(self, path, content):
        file = self.source / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)

    def invoke(self, *args, error=None):
        result = subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True)
        if error:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(error, result.stderr)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def roundtrip(self, allowed=()):
        flags = [part for path in allowed for part in ("--allow-untracked", path)]
        before = self.invoke("inspect", self.source, *flags)
        package = self.base / "transfer.zip"
        self.invoke("export", self.source, package, "--expected-snapshot", before["snapshot_id"], *flags)
        target = self.base / "restored"
        receipt = self.invoke("restore", package, target,
                              "--expected-package", hashlib.sha256(package.read_bytes()).hexdigest())
        self.assertEqual(receipt["snapshot_id"], before["snapshot_id"])
        self.assertEqual(before, self.invoke("inspect", target, *flags))
        self.assertEqual(before, self.invoke("inspect", self.source, *flags))
        for args in [("diff", "--cached", "--binary"), ("diff", "--binary")]:
            self.assertEqual(self.git(*args), self.git(*args, root=target))
        return target, package

    def test_clean_history_and_remote_metadata(self):
        self.git("branch", "local-only")
        self.git("tag", "-am", "Annotated tag", "fixture-tag")
        self.roundtrip()

    def test_staged_unstaged_binary_deletions_and_selected_untracked(self):
        self.write("shared.txt", b"staged\n")
        self.git("add", "shared.txt")
        self.write("shared.txt", b"unstaged after staging\n")
        self.write("binary file\nwith newline", b"\0\xffstaged")
        self.git("add", "binary file\nwith newline")
        self.write("binary file\nwith newline", b"\0\xfeunstaged")
        self.git("rm", "-q", "deleted.txt")
        (self.source / "dir/child").unlink()
        (self.source / "executable").chmod(0o755)
        os.symlink("shared.txt", self.source / "link")
        self.git("add", "link")
        self.write("new files/allowed", b"selected")
        self.write("keep-untracked", b"not selected")
        target, _ = self.roundtrip(["new files"])
        self.assertFalse((target / "keep-untracked").exists())
        self.assertTrue((self.source / "keep-untracked").exists())
        self.assertEqual(os.readlink(target / "link"), "shared.txt")

    def test_detached_head_without_named_ref(self):
        self.git("checkout", "--detach", "-q")
        self.write("detached", b"reachable only from HEAD")
        self.git("add", "detached")
        self.git("commit", "-qm", "Detached commit")
        self.roundtrip()

    def test_source_default_branch_and_other_master_ref(self):
        self.git("branch", "-m", "master")
        self.roundtrip()

    def test_noncurrent_master_ref_does_not_conflict_with_unborn_head(self):
        self.git("branch", "master")
        self.roundtrip()

    def test_core_filemode_semantics(self):
        self.git("config", "core.filemode", "false")
        (self.source / "shared.txt").chmod(0o755)
        self.assertEqual(self.git("diff"), b"")
        self.roundtrip()

    def test_core_symlinks_semantics(self):
        os.symlink("shared.txt", self.source / "link")
        self.git("add", "link")
        self.git("commit", "-qm", "Track link")
        (self.source / "link").unlink()
        self.write("link", b"shared.txt")
        self.git("config", "core.symlinks", "false")
        self.assertEqual(self.git("diff"), b"")
        self.roundtrip()

    def test_core_autocrlf_semantics(self):
        self.git("config", "core.autocrlf", "true")
        self.write("shared.txt", b"original\r\n")
        self.assertEqual(self.git("diff"), b"")
        self.roundtrip()

    def test_absent_platform_setting_is_preserved(self):
        subprocess.run(["git", "-C", str(self.source), "config", "--unset-all", "core.ignorecase"], capture_output=True)
        target, _ = self.roundtrip()
        result = subprocess.run(["git", "-C", str(target), "config", "--local", "--get-all", "core.ignorecase"], capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_multivalue_config_preserves_precedence(self):
        self.git("config", "core.filemode", "true")
        self.git("config", "--add", "core.filemode", "false")
        target, _ = self.roundtrip()
        self.assertEqual(self.git("config", "--get-all", "core.filemode"),
                         self.git("config", "--get-all", "core.filemode", root=target))
        for root in (self.source, target):
            (root / "shared.txt").chmod(0o755)
        self.assertEqual(self.git("diff"), self.git("diff", root=target))

    def test_index_object_restore_does_not_apply_autocrlf(self):
        self.git("config", "core.autocrlf", "false")
        self.write("committed-crlf", b"literal\r\nblob\r\n")
        self.git("add", "committed-crlf")
        self.git("commit", "-qm", "Raw CRLF object")
        self.git("config", "core.autocrlf", "true")
        self.roundtrip()

    def test_export_rejects_symlink_parent_and_dotdot(self):
        alias = self.base / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.invoke("export", self.source, alias / "package.zip", error="UNSAFE_PATH")
        self.assertFalse((self.source / "package.zip").exists())
        self.invoke("export", self.source, self.base / "source/../package.zip", error="UNSAFE_PATH")
        self.assertFalse((self.base / "package.zip").exists())

    def test_nested_directory_replaced_by_file(self):
        self.write("a/b/file", b"nested")
        self.git("add", "a")
        self.git("commit", "-qm", "Nested directory")
        self.git("rm", "-rq", "a")
        self.write("a", b"file now")
        self.git("add", "a")
        self.roundtrip()

    def test_rename_and_file_directory_transitions(self):
        self.git("mv", "shared.txt", "renamed.txt")
        self.git("rm", "-q", "dir/child")
        (self.source / "dir").rmdir() if (self.source / "dir").exists() else None
        self.write("dir", b"was a directory")
        self.git("add", "dir")
        self.roundtrip()

    def test_changed_source_rejects_reviewed_snapshot(self):
        before = self.invoke("inspect", self.source)
        self.write("shared.txt", b"new state")
        output = self.base / "stale.zip"
        self.invoke("export", self.source, output, "--expected-snapshot", before["snapshot_id"], error="SOURCE_CHANGED")
        self.assertFalse(output.exists())

    def test_nonempty_target_is_never_overwritten(self):
        output = self.base / "package.zip"
        self.invoke("export", self.source, output)
        target = self.base / "occupied"
        target.mkdir()
        (target / "precious").write_text("preserve")
        self.invoke("restore", output, target, error="TARGET_EXISTS")
        self.assertEqual((target / "precious").read_text(), "preserve")

    def test_intent_to_add_is_not_silently_staged(self):
        self.write("intent", b"intent")
        self.git("add", "--intent-to-add", "intent")
        self.invoke("inspect", self.source, error="UNSUPPORTED_GIT_STATE")

    def assert_unsupported_index_preserved(self):
        index = (self.source / ".git/index").read_bytes()
        status = self.git("status", "--porcelain=v1", "-z")
        files = {str(p.relative_to(self.source)): p.read_bytes()
                 for p in self.source.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(self.source).parts}
        self.invoke("inspect", self.source, error="UNSUPPORTED_GIT_STATE")
        self.assertEqual((self.source / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("status", "--porcelain=v1", "-z"), status)
        self.assertEqual({str(p.relative_to(self.source)): p.read_bytes()
                          for p in self.source.rglob("*")
                          if p.is_file() and ".git" not in p.relative_to(self.source).parts}, files)
        self.assertFalse((self.base / "restored").exists())

    def test_unmerged_index_is_rejected_without_altering_conflict(self):
        oid = self.git("rev-parse", "HEAD:shared.txt").decode().strip()
        entries = "0 " + "0" * len(oid) + "\tshared.txt\n"
        entries += "".join(f"100644 {oid} {stage}\tshared.txt\n" for stage in (1, 2, 3))
        self.git("update-index", "--index-info", data=entries.encode())
        self.assertTrue(self.git("ls-files", "--unmerged"))
        self.assert_unsupported_index_preserved()

    def test_sparse_checkout_is_rejected_without_expanding_worktree(self):
        self.git("sparse-checkout", "set", "--no-cone", "/shared.txt")
        self.assertFalse((self.source / "dir/child").exists())
        self.assert_unsupported_index_preserved()

    def test_split_index_is_rejected_without_rewriting_shared_index(self):
        self.git("update-index", "--split-index")
        shared = {p.name: p.read_bytes() for p in (self.source / ".git").glob("sharedindex.*")}
        self.assertTrue(shared)
        self.assert_unsupported_index_preserved()
        self.assertEqual({p.name: p.read_bytes() for p in (self.source / ".git").glob("sharedindex.*")}, shared)

    def test_secret_inventory_rejected_without_printing_contents(self):
        self.write(".env", b"SECRET=not-for-transfer")
        self.git("add", ".env")
        result = self.invoke("inspect", self.source, error="SECRET_PATH")
        self.assertNotIn("not-for-transfer", result.stderr + result.stdout)

    def test_outside_symlink_rejected(self):
        os.symlink("../outside", self.source / "escape")
        self.git("add", "escape")
        self.invoke("inspect", self.source, error="UNSAFE_PATH")

    def test_case_collision_rejected_from_git_index(self):
        oid = self.git("rev-parse", "HEAD:shared.txt").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"100644,{oid},SHARED.txt")
        self.invoke("inspect", self.source, error="PATH_COLLISION")

    def test_external_filter_and_credential_remote_rejected(self):
        self.git("config", "remote.origin.url", "https://secret@example.invalid/repo")
        self.invoke("inspect", self.source, error="SECRET_REMOTE")
        self.git("config", "remote.origin.url", "https://example.invalid/repo")
        self.write(".gitattributes", b"*.txt filter=external\n")
        self.git("add", ".gitattributes")
        self.invoke("inspect", self.source, error="UNSUPPORTED_GIT_STATE")

    def test_transport_corruption_rejected_before_destination_creation(self):
        output = self.base / "package.zip"
        self.invoke("export", self.source, output)
        target = self.base / "restored"
        self.invoke("restore", output, target, "--expected-package", "0" * 64, error="INVALID_PACKAGE")
        self.assertFalse(target.exists())
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("../../escape", b"bad")
        self.invoke("restore", output, target, error="INVALID_PACKAGE")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
