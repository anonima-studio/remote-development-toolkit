#!/usr/bin/env python3
"""Real filesystem/Git transactions, including interrupted publication."""
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EP = load("endpoint", "bin/remote-dev-mode-endpoint.py")
EP.TRANSFER = load("transfer", "bin/remote-dev-git-transfer.py")
EP.CONTRACT = load("contract", "templates/docker/project-contract.py")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {"HOME": str(self.base), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        self.environment.start()
        self.source = self.base / "source"
        self.source.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.source / "file").write_bytes(b"original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        (self.source / "file").write_bytes(b"staged\n")
        self.git("add", ".")
        (self.source / "file").write_bytes(b"unstaged\n")
        self.target = self.base / "target"
        self.identity = {"operation_id": "a" * 64, "token": "b" * 64, "project_id": "fixture", "allowed": []}
        self.snapshot = EP.TRANSFER.inspect(self.source)["snapshot_id"]
        self.src = EP.Transaction({**self.identity, "root": str(self.source), "snapshot_id": self.snapshot})
        self.dst = EP.Transaction({**self.identity, "root": str(self.target), "snapshot_id": self.snapshot})

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_metadata_receipt_never_opens_payload_and_detects_restored_mtime(self):
        secret = self.source / ".env"
        secret.write_bytes(b"synthetic one")
        with patch.object(os, "open", side_effect=AssertionError("payload opened")):
            before = EP.tree_fingerprint(self.source)
        old = secret.stat()
        secret.write_bytes(b"synthetic two")
        os.utime(secret, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertNotEqual(before, EP.tree_fingerprint(self.source))
        before = EP.tree_fingerprint(self.source)
        moved = self.base / "renamed-source"
        self.source.rename(moved)
        self.assertEqual(before, EP.tree_fingerprint(moved))
        secret = moved / ".env"
        old = secret.stat()
        replacement = self.base / "replacement"
        replacement.write_bytes(b"synthetic two")
        replacement.chmod(old.st_mode & 0o777)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(secret)
        self.assertNotEqual(before, EP.tree_fingerprint(moved))

    def test_mode_inspection_does_not_read_ignored_secret(self):
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("mode_secret_probe", str(ROOT / "bin/remote-dev-mode"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mode = importlib.util.module_from_spec(spec)
        loader.exec_module(mode)
        secret = self.source / ".env"
        secret.write_bytes(b"synthetic fixture")
        (self.source / ".gitignore").write_text(".env\n")
        original_open, original_io_open = os.open, io.open
        def no_payload(original):
            def check(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path) == secret:
                    raise AssertionError("secret payload opened before grant")
                return original(path, *args, **kwargs)
            return check
        with patch.object(os, "open", no_payload(original_open)), patch.object(io, "open", no_payload(original_io_open)):
            result = mode.inspect_checkout(self.source, None, [])
        self.assertIn("snapshot", result)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True, capture_output=True).stdout

    def prepare(self):
        self.src.handle("claim")
        self.dst.handle("claim")
        self.dst.request["target_fingerprint"] = self.dst.handle("fingerprint")
        captured = self.src.handle("capture")
        buffer = io.BytesIO()
        self.src.handle("download", destination=buffer)
        self.dst.request["package_sha256"] = captured["sha256"]
        self.dst.handle("upload", source=io.BytesIO(buffer.getvalue()))
        self.dst.handle("restore")

    def test_capture_publish_abort_preserves_staged_and_unstaged(self):
        self.prepare()
        self.dst.handle("publish")
        self.assertEqual(EP.TRANSFER.inspect(self.target)["snapshot_id"], self.snapshot)
        self.dst.handle("rollback")
        self.assertFalse(self.target.exists())
        self.assertEqual(EP.TRANSFER.inspect(self.source)["snapshot_id"], self.snapshot)
        self.assertTrue((self.dst.area / "aborted-candidate/file").exists())
        self.dst.handle("release")
        self.dst.handle("release")

    def test_lifecycle_never_executes_changed_git_or_runner_contract(self):
        marker = self.base / "untrusted-command-executed"
        contract = self.source / ".remote-dev/project.env"
        contract.parent.mkdir()
        contract.write_text("version=2\nsetup_command=true\nstart_command=true\nquiesce_command=true\nstop_command=true\n"
                            "status_command=touch " + str(marker) + "\nhealth_url=http://127.0.0.1:3000/health\n"
                            "browser_port=3000\nresource_id=fixture\n")
        self.git("add", ".remote-dev/project.env")
        frozen = EP.TRANSFER.inspect(self.source)
        self.src.request.update(lifecycle_snapshot=frozen, lifecycle_phase="status", context={})
        self.src.handle("claim")
        contract.write_text(contract.read_text() + "\n")
        with self.assertRaisesRegex(EP.EndpointError, "LIFECYCLE_SOURCE_CHANGED"):
            self.src.handle("lifecycle")
        self.assertFalse(marker.exists())
        runner = self.base / "projects/runner"
        (runner / ".remote-dev").mkdir(parents=True)
        (runner / "file").write_bytes((self.source / "file").read_bytes())
        (runner / ".remote-dev/project.env").write_text(contract.read_text())
        role = self.base / ".local/share/remote-dev/workspace-role"
        role.write_text("runner\n")
        transaction = EP.Transaction({**self.identity, "root": str(runner), "role": "runner",
                                      "lifecycle_snapshot": frozen, "lifecycle_phase": "status", "context": {}})
        transaction.handle("claim")
        with self.assertRaisesRegex(EP.EndpointError, "PROJECTION_MISMATCH"):
            transaction.handle("lifecycle")
        self.assertFalse(marker.exists())

    def test_foreign_claim_cannot_be_taken_over(self):
        self.src.handle("claim")
        other = EP.Transaction({**self.src.request, "operation_id": "c" * 64})
        with self.assertRaisesRegex(EP.EndpointError, "OPERATION_BUSY"):
            other.handle("claim")
        with self.assertRaisesRegex(EP.EndpointError, "OPERATION_BUSY"):
            other.handle("release")

    def test_new_ignored_candidate_files_block_abort(self):
        self.prepare()
        self.dst.handle("publish")
        (self.target / "new-data").write_text("preserve me")
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            self.dst.handle("rollback")
        self.assertEqual((self.target / "new-data").read_text(), "preserve me")

    def test_target_change_after_inspect_blocks_publication(self):
        self.prepare()
        self.target.mkdir()
        (self.target / "foreign").write_text("owned elsewhere")
        with self.assertRaisesRegex(EP.EndpointError, "TARGET_CHANGED"):
            self.dst.handle("publish")
        self.assertTrue((self.target / "foreign").exists())

    def test_resume_after_candidate_rename_and_restore_previous(self):
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.target)], check=True)
        (self.target / "old-data").write_text("retain")
        self.prepare()
        real_save = self.dst.save
        def crash(state):
            if state["phase"] == "published-inactive":
                raise OSError("simulated crash after rename")
            real_save(state)
        with patch.object(self.dst, "save", crash):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self.dst.handle("publish")
        self.dst.handle("publish")
        self.dst.handle("rollback")
        self.assertEqual((self.target / "old-data").read_text(), "retain")

    def test_capture_package_without_receipt_requires_recovery_and_preserves_source(self):
        self.src.handle("claim")
        real_save = self.src.save
        def crash(state):
            if state.get("capture"):
                raise OSError("capture receipt not durable")
            return real_save(state)
        with patch.object(self.src, "save", crash):
            with self.assertRaisesRegex(OSError, "receipt not durable"):
                self.src.handle("capture")
        package = (self.src.area / "source.zip").read_bytes()
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            self.src.handle("capture")
        self.assertEqual((self.src.area / "source.zip").read_bytes(), package)
        self.assertEqual(EP.TRANSFER.inspect(self.source)["snapshot_id"], self.snapshot)
        self.src.handle("release")
        self.assertEqual((self.src.area / "source.zip").read_bytes(), package)

    def test_interrupted_upload_cleans_partial_bytes_and_can_retry(self):
        self.src.handle("claim")
        self.dst.handle("claim")
        captured = self.src.handle("capture")
        buffer = io.BytesIO()
        self.src.handle("download", destination=buffer)
        payload = buffer.getvalue()
        self.dst.request["package_sha256"] = captured["sha256"]
        class InterruptedStream:
            first = True
            def read(stream, size=-1):
                if stream.first:
                    stream.first = False
                    return payload[:64]
                raise OSError("transport interrupted after bytes")
        with self.assertRaisesRegex(OSError, "after bytes"):
            self.dst.handle("upload", source=InterruptedStream())
        self.assertFalse((self.dst.area / "incoming.zip").exists())
        self.assertEqual(list(self.dst.area.glob(".incoming-*")), [])
        self.dst.handle("upload", source=io.BytesIO(payload))
        self.dst.handle("restore")
        self.assertEqual(EP.TRANSFER.inspect(self.dst.area / "candidate")["snapshot_id"], self.snapshot)
        self.assertEqual(EP.TRANSFER.inspect(self.source)["snapshot_id"], self.snapshot)

    def test_resume_between_previous_and_candidate_renames(self):
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.target)], check=True)
        (self.target / "old-data").write_text("retain")
        self.prepare()
        original = os.rename
        def interrupted(source, destination, *args, **kwargs):
            result = original(source, destination, *args, **kwargs)
            if Path(source) == self.target and Path(destination) == self.dst.area / "previous":
                raise OSError("lost first rename response")
            return result
        with patch.object(EP.os, "rename", interrupted):
            with self.assertRaisesRegex(OSError, "first rename"):
                self.dst.handle("publish")
        self.assertFalse(self.target.exists())
        self.assertTrue((self.dst.area / "candidate").exists())
        self.assertEqual((self.dst.area / "previous/old-data").read_text(), "retain")
        self.dst.handle("publish")
        self.assertEqual(EP.TRANSFER.inspect(self.target)["snapshot_id"], self.snapshot)
        self.dst.handle("rollback")
        self.assertEqual((self.target / "old-data").read_text(), "retain")

    def test_backup_change_blocks_abort(self):
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.target)], check=True)
        self.prepare()
        self.dst.handle("publish")
        (self.dst.area / "previous/new-data").write_text("preserve")
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            self.dst.handle("rollback")
        self.assertTrue(self.target.exists())

    def test_identical_previous_and_candidate_abort_uses_directory_identity(self):
        self.prepare()
        EP.TRANSFER.restore(self.src.area / "source.zip", self.target)
        self.dst.request["target_fingerprint"] = self.dst.handle("fingerprint")
        previous_inode = self.target.stat().st_ino
        self.dst.handle("publish")
        self.assertNotEqual(self.target.stat().st_ino, previous_inode)
        self.dst.handle("rollback")
        self.assertEqual(self.target.stat().st_ino, previous_inode)

    def test_abort_resume_rechecks_candidate_after_rolling_back_checkpoint(self):
        self.prepare()
        self.dst.handle("publish")
        real_rename = EP.os.rename
        def crash(source, destination):
            if Path(destination).name == "aborted-candidate":
                raise OSError("injected rename interruption")
            return real_rename(source, destination)
        with patch.object(EP.os, "rename", crash):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.dst.handle("rollback")
        (self.target / "new-write").write_text("preserve in place")
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            self.dst.handle("rollback")
        self.assertEqual((self.target / "new-write").read_text(), "preserve in place")

    def test_claim_is_published_only_after_complete_json_is_durable(self):
        real_link = EP.os.link
        def interrupted(source, destination, **options):
            self.assertFalse(Path(destination).exists())
            self.assertEqual(EP.read_record(Path(source)), self.src.owner)
            raise OSError("interrupted before atomic claim publication")
        with patch.object(EP.os, "link", interrupted):
            with self.assertRaisesRegex(OSError, "atomic claim"):
                self.src.handle("claim")
        self.assertFalse(self.src.claim.exists())
        self.src.handle("claim")
        self.assertEqual(EP.read_record(self.src.claim), self.src.owner)

    def test_missing_leases_with_unknown_execution_is_not_down(self):
        self.dst.handle("claim")
        expected = {"project_id": "fixture", "profile": "fixture", "workspace": "runner", "root_hash": "d" * 64,
                    "remote_root": str(self.target), "owner_id": "owner"}
        self.dst.request["binding"] = expected
        control = self.base / ".local/share/remote-dev/local-agent/fixture"
        control.mkdir(parents=True, mode=0o700)
        binding = control / "binding.tsv"
        binding.write_text("\n".join(key + "\t" + value for key, value in expected.items()) + "\n")
        binding.chmod(0o600)
        (control / "executions").mkdir()
        pending = control / "executions/unknown.running"
        pending.write_text("unknown")
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            self.dst.handle("runner-binding")
        pending.unlink()
        self.assertEqual(self.dst.handle("runner-binding"), {"state": "down"})

    def test_discarding_packages_keeps_the_replaced_destination(self):
        self.target.mkdir()
        (self.target / "leftover").write_bytes(b"replaced\n")
        self.prepare()
        self.dst.handle("publish")
        self.assertTrue((self.dst.area / "incoming.zip").exists())
        self.assertEqual(self.dst.handle("discard-packages"), {"removed": ["incoming.zip"]})
        self.assertEqual(self.src.handle("discard-packages"), {"removed": ["source.zip"]})
        self.assertFalse((self.dst.area / "incoming.zip").exists())
        # The only copy of what publication moved aside must survive.
        self.assertEqual((self.dst.area / "previous/leftover").read_bytes(), b"replaced\n")
        self.assertEqual(self.dst.handle("discard-packages"), {"removed": []})

    def test_a_claimed_area_cannot_be_measured_or_discarded(self):
        self.prepare()
        self.dst.handle("publish")
        for action in ("area-usage", "discard-area"):
            with self.assertRaisesRegex(EP.EndpointError, "OPERATION_BUSY"):
                self.dst.handle(action)
        self.assertTrue(self.dst.area.exists())

    def test_released_area_is_measured_before_an_explicit_discard(self):
        self.target.mkdir()
        (self.target / "leftover").write_bytes(b"replaced\n")
        self.prepare()
        self.dst.handle("publish")
        self.dst.handle("release")
        usage = self.dst.handle("area-usage")
        self.assertEqual(usage["phase"], "published-inactive")
        self.assertIn("previous", usage["holds"])
        self.assertGreater(usage["bytes"], 0)
        self.assertTrue(self.dst.area.exists(), "measuring must not remove anything")
        # The area path carries the operation ID, so the reachable confusion is
        # a same-operation caller claiming a different project identity.
        foreign = EP.Transaction({**self.identity, "project_id": "other",
                                  "root": str(self.target), "snapshot_id": self.snapshot})
        self.assertEqual(foreign.area, self.dst.area)
        with self.assertRaisesRegex(EP.EndpointError, "RECOVERY_REQUIRED"):
            foreign.handle("discard-area")
        self.assertTrue(self.dst.area.exists())
        self.assertTrue(self.dst.handle("discard-area")["discarded"])
        self.assertFalse(self.dst.area.exists())
        self.assertEqual(EP.TRANSFER.inspect(self.target)["snapshot_id"], self.snapshot)
        self.assertEqual(self.dst.handle("area-usage"), {"area": str(self.dst.area), "exists": False})


if __name__ == "__main__":
    unittest.main()
