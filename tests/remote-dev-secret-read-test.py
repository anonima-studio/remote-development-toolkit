#!/usr/bin/env python3
"""Real owner reader and SSH pipe; synthetic payloads and disposable homes only."""
import hashlib
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
# Synthetic host profiles; tests never read the deployment profiles.
os.environ["REMOTE_DEV_PROFILE_DIR"] = str(ROOT / "tests/fixtures/host-profiles")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("reader", "bin/remote-dev-secret-read.py")
G = load("guard", "templates/docker/mode-guard.py")
E = load("endpoint", "bin/remote-dev-mode-endpoint.py")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "projects/fixture"
        self.root.mkdir(parents=True)
        marker = self.home / ".local/share/remote-dev/workspace-role"
        marker.parent.mkdir(parents=True)
        marker.write_text("development\n")
        self.path = self.root / ".env"
        self.payload = b"synthetic fixture only\n" * 100000
        self.path.write_bytes(self.payload)
        self.environment = patch.dict(os.environ, {"HOME": str(self.home), "REMOTE_DEV_MODE_OPERATION_ID": "", "REMOTE_DEV_MODE_TOKEN": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def read(self, action="stream", output=None, relative=".env", role="development"):
        output = output or io.BytesIO()
        R.read(self.root, self.root, relative, role, action, output, G)
        return output

    def test_metadata_does_not_open_payload_or_create_state(self):
        with patch.object(os, "open", wraps=os.open) as opened:
            self.assertEqual(self.read("metadata").getvalue(), b"")
        self.assertFalse(any(Path(call.args[0]) == self.path for call in opened.call_args_list))
        self.assertFalse((self.home / ".local/share/remote-dev/mode-endpoints").exists())

    def test_stream_hash_and_path_role_refusals(self):
        self.assertEqual(self.read().getvalue(), self.payload)
        self.assertEqual(self.read("hash").getvalue(), hashlib.sha256(self.payload).hexdigest().encode())
        for relative in ("../.env", ".git/config", "link", "missing"):
            if relative == "link":
                (self.root / "link").symlink_to(self.path)
            with self.assertRaises((RuntimeError, OSError)):
                self.read(relative=relative)
        with self.assertRaisesRegex(RuntimeError, "ROLE"):
            self.read(role="runner")

    def test_source_mutation_is_detected(self):
        fixture = self
        class Mutator(io.BytesIO):
            def write(self, chunk):
                fixture.path.write_bytes(b"changed")
                return super().write(chunk)
        with self.assertRaisesRegex(RuntimeError, "CHANGED"):
            self.read(output=Mutator())

    def test_foreign_claim_inactive_and_endpoint_mutex(self):
        transaction = E.Transaction({"root": str(self.root), "operation_id": "a" * 64, "token": "b" * 64, "project_id": "fixture"})
        with transaction.lock():
            with self.assertRaisesRegex(RuntimeError, "IN_PROGRESS"):
                self.read()
        transaction.handle("claim")
        with self.assertRaisesRegex(RuntimeError, "IN_PROGRESS"):
            self.read()
        with patch.dict(os.environ, {"REMOTE_DEV_MODE_OPERATION_ID": "a" * 64, "REMOTE_DEV_MODE_TOKEN": "b" * 64}):
            self.assertEqual(self.read().getvalue(), self.payload)
        transaction.claim.unlink()
        E.write_record(transaction.state_root / "fences" / (transaction.claim_key + ".json"), {"root": str(self.root), "state": "inactive"})
        with self.assertRaisesRegex(RuntimeError, "SOURCE_INACTIVE"):
            self.read()

    def test_standalone_owner_confirmation_precedes_hash_or_stream(self):
        destination_home = self.home / "destination"
        destination = destination_home / "projects/fixture"
        destination.mkdir(parents=True)
        marker = destination_home / ".local/share/remote-dev/workspace-role"
        marker.parent.mkdir(parents=True)
        marker.write_text("development\n")
        fake = self.home / "coder-owner"
        trace = self.home / "trace"
        fake.write_text('''#!/usr/bin/env python3
import csv, io, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
assert "--disable-autostart=true" in args
env = dict(os.environ)
separator = args.index("--")
workspace = args[separator - 1]
home = os.environ["FIXTURE_" + workspace.upper() + "_HOME"]
env["HOME"] = home
for index, value in enumerate(args):
    if value == "-e":
        fields = next(csv.reader(io.StringIO(args[index + 1]), strict=True))
        assert len(fields) == 1
        key, data = fields[0].split("=", 1)
        if key.endswith("ROOT") or key.endswith("CONTEXT"):
            data = data.replace("/home/coder", home)
        env[key] = data
with open(os.environ["FIXTURE_TRACE"], "a") as output:
    output.write(workspace + ":" + env.get("REMOTE_DEV_SECRET_READ_ACTION", "other") + "\\n")
command = args[separator + 1].replace("/home/coder", home)
# GNU stat is not installed on the Mac fixture; production verifies coder UID.
command = command.replace('mode=$(stat -c %a "$destination")', 'mode=600')
command = command.replace('owner=$(stat -c %U "$destination")', 'owner=coder')
command = command.replace('realpath -e', 'realpath')
if os.environ.get("FIXTURE_NOOP_UPLOAD") and "REMOTE_DEV_SECRET_UPLOAD_SOURCE" in env:
    sys.exit(0)
sys.exit(subprocess.run(["bash", "-c", command], env=env).returncode)
''')
        fake.chmod(0o700)
        manifest = self.home / "route.tsv"
        fields = {"manifest_version": "3", "project_id": "fixture", "workspace": "target", "workspace_role": "development",
                  "local_root": str(self.home), "remote_root": "/home/coder/projects/fixture", "source_workspace": "source",
                  "source_workspace_role": "development", "source_root": "/home/coder", "source_context": "/home/coder/projects/fixture"}
        manifest.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()) + "file\tprojects/fixture/.env\t.env\n")
        confirmations = ["--confirm", "target", "--confirm-profile", "example-profile", "--confirm-origin", "https://coder.example.invalid"]
        env = {"REMOTE_DEV_PROFILE": "example-profile", "REMOTE_DEV_CODER_BIN": str(fake), "FIXTURE_SOURCE_HOME": str(self.home),
               "FIXTURE_TARGET_HOME": str(destination_home), "FIXTURE_TRACE": str(trace), "REMOTE_DEV_EXPECTED_CODER_ORIGIN": ""}
        with patch.dict(os.environ, env):
            def invoke(action, options=()):
                return subprocess.run([str(ROOT / "bin/remote-dev-secrets"), action, str(manifest), *options], capture_output=True)
            for action in ("apply", "verify"):
                result = invoke(action, confirmations)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(trace.exists(), result.stderr)
            result = invoke("plan")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(":hash", trace.read_text())
            self.assertNotIn(":stream", trace.read_text())
            with patch.dict(os.environ, {"FIXTURE_NOOP_UPLOAD": "true"}):
                result = invoke("apply", [*confirmations, "--confirm-source", "source"])
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"COPIED", result.stdout)
            self.assertFalse((destination / ".env").exists())
            result = invoke("apply", [*confirmations, "--confirm-source", "source"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / ".env").read_bytes(), self.payload)
            self.assertIn("source:stream", trace.read_text())
            self.assertIn("source:hash", trace.read_text())
            self.assertNotIn(self.payload[:80], result.stdout + result.stderr)
            # Reverse after down: the local binding/policy remains; remote lease
            # is deliberately absent. Exercise the real binding owner parser.
            anchor = self.home / "anchor"
            anchor.mkdir()
            subprocess.run(["git", "init", "-q", str(anchor)], check=True)
            state = self.home / "binding-state"
            binding = state / "bindings/by-root" / hashlib.sha256(os.fsencode(anchor)).hexdigest()
            binding.mkdir(parents=True)
            managed, excluded = binding / "managed.tsv", binding / "excluded.tsv"
            managed.write_text("path\ttype\tmode\tdigest\ttarget\tstate\n")
            excluded.write_text("path\treason\n.env\tremote-writable\n")
            identity = {"schema": "1", "project_id": "fixture", "workspace": "target", "workspace_role": "runner",
                        "profile": "example-profile", "coder_origin": "https://coder.example.invalid", "local_root": str(anchor),
                        "remote_root": "/home/coder/projects/fixture", "managed_manifest_path": str(managed), "excluded_manifest_path": str(excluded)}
            (binding / "binding.tsv").write_text("".join(key + "\t" + value + "\n" for key, value in identity.items()))
            marker.write_text("runner\n")
            fields.update(workspace="source", source_workspace="target", source_workspace_role="runner",
                          local_root=str(anchor), source_root="/home/coder/projects/fixture")
            manifest.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()) + "file\t.env\t.env\n")
            reverse = ["--confirm", "source", *confirmations[2:], "--confirm-source", "target", "--replace"]
            with patch.dict(os.environ, {"REMOTE_DEV_LOCAL_AGENT_STATE_ROOT": str(state)}):
                result = invoke("apply", reverse)
                self.assertEqual(result.returncode, 0, result.stderr)
                excluded.write_text("path\treason\n.env\tsecret\n")
                before = trace.read_text()
                result = invoke("apply", reverse)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"not declared remote-writable", result.stderr)
                self.assertEqual(trace.read_text(), before)

    def test_real_owner_pipe_and_either_endpoint_failure(self):
        destination_home = self.home / "destination"
        destination = destination_home / "projects/fixture"
        destination.mkdir(parents=True)
        fake = self.home / "coder"
        fake.write_text('''#!/usr/bin/env python3
import csv, io, os, subprocess, sys
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
workspace = args[separator - 1]
assert workspace in ("source", "target")
env["HOME"] = os.environ["FIXTURE_" + workspace.upper() + "_HOME"]
if workspace == os.environ.get("FIXTURE_FAIL"):
    sys.exit(17)
sys.exit(subprocess.run(["bash", "-c", args[separator + 1]], env=env).returncode)
''')
        fake.chmod(0o700)
        owner = (ROOT / "bin/remote-dev-secrets").read_text()
        functions = owner[owner.index("coder_env_argument() {"):owner.index("\nverify_one() {")]
        command = '''set -euo pipefail
repo_root=$1
source_root=$2
source_context=$2
remote_root=$3
coder_command=("$4")
source_workspace=source
source_workspace_role=development
workspace=target
workspace_role=development
coder_url=https://fixture.invalid
manifest_version=3
''' + functions + '\nupload_one .env .env false\n'
        argv = ["bash", "-c", command, "fixture", str(ROOT), str(self.root), str(destination), str(fake)]
        with patch.dict(os.environ, {"FIXTURE_SOURCE_HOME": str(self.home), "FIXTURE_TARGET_HOME": str(destination_home)}):
            result = subprocess.run(argv, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / ".env").read_bytes(), self.payload)
            self.assertNotIn(self.payload[:80], result.stdout + result.stderr)
            (destination / ".env").unlink()
            for endpoint in ("source", "target"):
                with patch.dict(os.environ, {"FIXTURE_FAIL": endpoint}):
                    result = subprocess.run(argv, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((destination / ".env").exists())


if __name__ == "__main__":
    unittest.main()
