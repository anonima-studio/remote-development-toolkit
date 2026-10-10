#!/usr/bin/env python3
"""Synthetic payloads only; real filesystem and endpoint mutex boundaries."""
import importlib.util
import csv
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


G = load("guard", "templates/docker/mode-guard.py")
U = load("upload", "bin/remote-dev-secret-upload.py")
E = load("endpoint", "bin/remote-dev-mode-endpoint.py")


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "projects/fixture"
        self.root.mkdir(parents=True)
        self.environment = patch.dict(os.environ, {"HOME": str(self.home), "REMOTE_DEV_MODE_OPERATION_ID": "", "REMOTE_DEV_MODE_TOKEN": ""})
        self.environment.start()
        self.payload = b"synthetic test payload\n" * 70000

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def upload(self, payload=None, replace=False, relative=".env"):
        U.upload(self.root, relative, replace, io.BytesIO(self.payload if payload is None else payload), G)

    def transaction(self):
        return E.Transaction({"root": str(self.root), "operation_id": "a" * 64, "token": "b" * 64, "project_id": "fixture"})

    def test_coder_environment_csv_roundtrip(self):
        owner = (ROOT / "bin/remote-dev-secrets").read_text()
        function = owner[owner.index("coder_env_argument() {"):owner.index("\nread_remote_source() {")]
        for value in ("plain", "comma,value", 'quoted,"value"', '# comment\nprint("marker")\n', ""):
            with self.subTest(value=value):
                assignment = "SYNTHETIC_PROBE=" + value
                result = subprocess.run(
                    ["bash", "-c", function + '\ncoder_env_argument "$1"', "fixture", assignment],
                    text=True, capture_output=True, check=True,
                )
                rows = list(csv.reader(io.StringIO(result.stdout), strict=True))
                self.assertEqual(rows, [[assignment]])

    def test_atomic_publication_and_explicit_replacement(self):
        self.upload()
        destination = self.root / ".env"
        self.assertEqual(destination.read_bytes(), self.payload)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, "SECRET_EXISTS"):
            self.upload(b"replacement")
        self.assertEqual(destination.read_bytes(), self.payload)
        self.upload(b"replacement", replace=True)
        self.assertEqual(destination.read_bytes(), b"replacement")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [".env"])
        # A first standalone upload must leave state usable by the endpoint.
        with self.transaction().lock():
            pass

    def test_no_write_for_foreign_claim_or_inactive_checkout(self):
        transaction = self.transaction()
        transaction.handle("claim")
        with self.assertRaisesRegex(RuntimeError, "IN_PROGRESS"):
            self.upload()
        self.assertEqual(list(self.root.iterdir()), [])
        with patch.dict(os.environ, {"REMOTE_DEV_MODE_OPERATION_ID": "a" * 64, "REMOTE_DEV_MODE_TOKEN": "b" * 64}):
            self.upload()
        transaction.claim.unlink()
        E.write_record(transaction.state_root / "fences" / (transaction.claim_key + ".json"),
                       {"root": str(self.root), "state": "inactive"})
        with self.assertRaisesRegex(RuntimeError, "SOURCE_INACTIVE"):
            self.upload(b"must not publish", replace=True)
        self.assertEqual((self.root / ".env").read_bytes(), self.payload)

    def test_endpoint_and_upload_share_mutex_for_nested_roots(self):
        transaction = self.transaction()
        nested = self.root / "nested"
        nested.mkdir()
        with transaction.lock():
            with self.assertRaisesRegex(RuntimeError, "IN_PROGRESS"):
                U.upload(nested, ".env", False, io.BytesIO(self.payload), G)
        # In the reverse direction, a claim cannot start during payload read.
        class Stream(io.BytesIO):
            def read(stream, size):
                with self.assertRaisesRegex(E.EndpointError, "OPERATION_BUSY"):
                    transaction.handle("claim")
                return super().read(size)
        U.upload(nested, ".env", False, Stream(self.payload), G)
        self.assertFalse(transaction.claim.exists())

    def test_empty_stream_failure_and_symlinks_leave_no_payload(self):
        with self.assertRaisesRegex(RuntimeError, "SECRET_EMPTY"):
            self.upload(b"")
        class Broken(io.BytesIO):
            def read(stream, size):
                raise OSError("synthetic read failure")
        with self.assertRaises(OSError):
            U.upload(self.root, ".env", False, Broken(), G)
        self.assertEqual(list(self.root.iterdir()), [])
        outside = self.home / "outside"
        outside.write_bytes(b"keep")
        (self.root / ".env").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "SECRET_PATH_INVALID"):
            self.upload(replace=True)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_upload_entrypoint_uses_environment_guard_and_stdin(self):
        environment = {**os.environ, "REMOTE_DEV_MODE_GUARD_SOURCE": (ROOT / "templates/docker/mode-guard.py").read_text(),
                       "REMOTE_DEV_SECRET_ROOT": str(self.root), "REMOTE_DEV_SECRET_REL": ".env", "REMOTE_DEV_SECRET_REPLACE": "false"}
        result = subprocess.run([sys.executable, str(ROOT / "bin/remote-dev-secret-upload.py")], env=environment,
                                input=self.payload, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual((self.root / ".env").read_bytes(), self.payload)

    def test_secrets_owner_forwards_identity_through_coder_transport(self):
        fake = self.home / "coder"
        fake.write_text("#!" + sys.executable + "\n" + '''import csv, io, os, subprocess, sys
args = sys.argv[1:]
assert "--disable-autostart=true" in args
env = dict(os.environ)
for index, value in enumerate(args):
    if value == "-e":
        fields = next(csv.reader(io.StringIO(args[index + 1]), strict=True))
        assert len(fields) == 1
        key, data = fields[0].split("=", 1)
        env[key] = data
separator = args.index("--")
assert args[separator - 1] == "fixture-workspace"
sys.exit(subprocess.run(["bash", "-c", args[separator + 1]], env=env).returncode)
''')
        fake.chmod(0o700)
        source = self.home / "source"
        source.mkdir()
        (source / ".env").write_bytes(self.payload)
        # Exercise the exact production owner function and its SSH command.
        owner = (ROOT / "bin/remote-dev-secrets").read_text()
        function = owner[owner.index("coder_env_argument() {"):owner.index("\nverify_one() {")]
        command = '''set -euo pipefail
repo_root=$1
local_root=$2
remote_root=$3
coder_command=("$4")
coder_url=https://coder.example.invalid
workspace=fixture-workspace
workspace_role=development
manifest_version=2
''' + function + '\nupload_one .env .env false\n'
        transaction = self.transaction()
        transaction.handle("claim")
        argv = ["bash", "-c", command, "secret-owner-fixture", str(ROOT), str(source), str(self.root), str(fake)]
        result = subprocess.run(argv, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"MODE_OPERATION_IN_PROGRESS", result.stderr)
        self.assertFalse((self.root / ".env").exists())
        with patch.dict(os.environ, {"REMOTE_DEV_MODE_OPERATION_ID": "a" * 64, "REMOTE_DEV_MODE_TOKEN": "b" * 64}):
            result = subprocess.run(argv, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / ".env").read_bytes(), self.payload)
        self.assertNotIn(self.payload[:80], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
