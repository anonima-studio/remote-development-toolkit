#!/usr/bin/env python3
"""Offline Mac control-plane checks: real binding writer and secrets CLI."""

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
# Synthetic host profiles; tests never read the deployment profiles.
os.environ["REMOTE_DEV_PROFILE_DIR"] = str(REPO / "tests/fixtures/host-profiles")


class RunnerSecretBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remote-dev-binding-test-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve()
        self.project = self.fixture / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.state = self.fixture / "state"
        root_hash = hashlib.sha256(str(self.project).encode()).hexdigest()
        self.binding = self.state / "bindings/by-root" / root_hash / "binding.tsv"
        self.binding.parent.mkdir(parents=True)
        self.managed = self.fixture / "managed.tsv"
        self.managed.write_text("path\ttype\tmode\tdigest\tsymlink_target\tstate\n")
        self.excluded = self.fixture / "excluded.tsv"
        self.excluded.write_text("path\treason\nsynthetic.secret\tremote-writable\n")
        source = self.project / "synthetic.secret"
        source.write_text("synthetic-fixture-only\n")
        source.chmod(0o600)
        self.manifest = self.fixture / "secrets.manifest.tsv"
        self.manifest.write_text(
            "manifest_version\t2\nproject_id\ttest-project\n"
            "workspace\ttest-runner\nworkspace_role\trunner\n"
            f"local_root\t{self.project}\n"
            "remote_root\t/home/coder/projects/test-project\n"
            "file\tsynthetic.secret\tsynthetic.secret\n"
        )
        self.calls = self.fixture / "coder-calls"
        coder = self.fixture / "coder"
        coder.write_text('''#!/usr/bin/env bash
set -euo pipefail
[[ "$*" == *' ssh --disable-autostart=true '* ]] || exit 91
[[ "$*" == *'--url https://coder.example.invalid '* ]] || exit 92
[[ "$*" == *' test-runner -- '* ]] || exit 93
if [[ "$*" == *'.local/share/remote-dev/workspace-role'* ]]; then
  printf 'role\n' >> "$REMOTE_DEV_BINDING_TEST_CALLS"
  printf '%s' "${REMOTE_DEV_BINDING_TEST_ROLE:-runner}"
elif [[ "$*" == *'printf ABSENT'* ]]; then
  printf 'destination\n' >> "$REMOTE_DEV_BINDING_TEST_CALLS"
  printf ABSENT
else
  exit 94
fi
''')
        coder.chmod(0o700)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("REMOTE_DEV_")
        }
        self.environment.update({
            "REMOTE_DEV_CODER_BIN": str(coder),
            "REMOTE_DEV_PROFILE_DIR": os.environ["REMOTE_DEV_PROFILE_DIR"],
            "REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT": str(self.fixture / "coder-config"),
            "REMOTE_DEV_LOCAL_AGENT_STATE_ROOT": str(self.state),
            "REMOTE_DEV_BINDING_TEST_CALLS": str(self.calls),
        })
        # Exercise the current canonical writer, without binding a real runner.
        writer_source = (REPO / "bin/remote-dev-local-agent").read_text()
        writer = "write_binding() {" + writer_source.split("write_binding() {", 1)[1].split("\n}\n", 1)[0] + "\n}"
        writer_environment = dict(self.environment, **{
            "binding_file": str(self.binding), "binding_kind": "single-root",
            "project_id": "test-project", "workspace": "test-runner",
            "profile": "example-profile", "coder_origin": "https://coder.example.invalid",
            "ssh_alias": "synthetic-runner", "local_root": str(self.project),
            "remote_root": "/home/coder/projects/test-project", "root_hash": root_hash,
            "mutagen_session": "synthetic", "owner_id": "synthetic",
            "remote_control": "/home/coder/.local/share/remote-dev/test",
            "policy_file": str(self.fixture / "policy.tsv"),
            "managed_manifest": str(self.managed), "excluded_manifest": str(self.excluded),
            "current_receipt": str(self.fixture / "receipt.tsv"),
            "topology_file": "", "topology_digest": "", "execution_member": "root",
            "member_digests": str(self.fixture / "members.tsv"), "git_metadata_mode": "none",
        })
        subprocess.run(
            ["bash", "-c", 'set -euo pipefail\nwrite_atomic() { cat > "$1"; }\n' + writer + "\nwrite_binding"],
            env=writer_environment, check=True,
        )
        self.original = self.binding.read_text()

    def replace_field(self, key, value):
        self.binding.write_text("".join(
            f"{key}\t{value}\n" if row.startswith(key + "\t") else row
            for row in self.original.splitlines(keepends=True)
        ))

    def plan(self):
        return subprocess.run(
            [str(REPO / "bin/remote-dev"), "--profile", "example-profile", "secrets", "plan", str(self.manifest)],
            env=self.environment, text=True, capture_output=True,
        )

    def assert_blocked_before_transport(self, expected):
        self.calls.unlink(missing_ok=True)
        result = self.plan()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)
        self.assertFalse(self.calls.exists(), "invalid binding reached Coder transport")

    def test_current_single_root_writer_output_is_accepted(self):
        self.assertIn("topology_path\t\ntopology_digest\t\n", self.original)
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("READY    plan is valid; no secret content was transferred", result.stdout)
        self.assertEqual(self.calls.read_text(), "role\ndestination\n")

    def test_legacy_single_root_without_optional_rows_is_accepted(self):
        self.binding.write_text("".join(
            row for row in self.original.splitlines(keepends=True)
            if not row.startswith(("binding_kind\t", "topology_path\t", "topology_digest\t"))
        ))
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_required_fields_still_fail_closed(self):
        required = (
            "schema", "project_id", "workspace", "workspace_role", "profile", "coder_origin",
            "local_root", "remote_root", "managed_manifest_path", "excluded_manifest_path",
        )
        for key in required:
            with self.subTest(key=key, mutation="empty"):
                self.replace_field(key, "")
                self.assert_blocked_before_transport("invalid local-agent binding line")
            with self.subTest(key=key, mutation="missing"):
                self.binding.write_text("".join(
                    row for row in self.original.splitlines(keepends=True)
                    if not row.startswith(key + "\t")
                ))
                self.assert_blocked_before_transport("ERROR")
            with self.subTest(key=key, mutation="mismatch"):
                self.replace_field(key, "wrong")
                self.assert_blocked_before_transport("ERROR")

    def test_malformed_and_duplicate_rows_fail_closed(self):
        for row in ("topology_path", "\tvalue", "topology_path\t\textra", "topology_path\t\t", "\n"):
            with self.subTest(row=row):
                self.binding.write_text(self.original + row + "\n")
                self.assert_blocked_before_transport("invalid local-agent binding line")
        for key in ("project_id", "binding_kind", "topology_path", "topology_digest"):
            with self.subTest(duplicate=key):
                row = next(row for row in self.original.splitlines() if row.startswith(key + "\t"))
                self.binding.write_text(self.original + row + "\n")
                self.assert_blocked_before_transport("duplicate binding " + key)
        self.replace_field("binding_kind", "")
        self.assert_blocked_before_transport("invalid local-agent binding line")
        self.binding.write_text(self.original + "future_metadata\tpreserved\n")
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_working_set_topology_is_required_and_validated(self):
        snapshot = self.fixture / "invalid-snapshot.tsv"
        snapshot.write_text("not a topology snapshot\n")
        digest = "a" * 64
        self.original = self.original.replace("binding_kind\tsingle-root", "binding_kind\tworking-set")
        self.original = self.original.replace("topology_path\t\n", f"topology_path\t{snapshot}\n")
        self.original = self.original.replace("topology_digest\t\n", f"topology_digest\t{digest}\n")
        for key in ("topology_path", "topology_digest"):
            for value in ("", "invalid"):
                with self.subTest(key=key, value=value):
                    self.replace_field(key, value)
                    self.assert_blocked_before_transport("active working-set topology snapshot is missing or invalid")
            with self.subTest(key=key, mutation="missing"):
                self.binding.write_text("".join(
                    row for row in self.original.splitlines(keepends=True)
                    if not row.startswith(key + "\t")
                ))
                self.assert_blocked_before_transport("active working-set topology snapshot is missing or invalid")
        self.replace_field("topology_digest", digest)
        self.assert_blocked_before_transport("topology_digest does not match")
        self.manifest.write_text(self.manifest.read_text() + f"topology_digest\t{digest}\n")
        self.assert_blocked_before_transport("active working-set topology has drifted")

    def test_valid_working_set_and_subsequent_topology_drift(self):
        topology_manifest = self.project / "working-set.tsv"
        topology_manifest.write_text(
            "schema\t1\nanchor\t..\nexecution_member\tproject\n"
            "member\tproject\tproject\nremote_writable\tproject\tsynthetic.secret\n"
        )
        subprocess.run(["git", "-C", str(self.project), "add", "working-set.tsv"], check=True)
        snapshot = self.fixture / "topology.tsv"
        normalized = subprocess.run(
            ["python3", str(REPO / "bin/remote-dev-local-agent-working-set.py"), "normalize",
             "--manifest", str(topology_manifest), "--snapshot-out", str(snapshot),
             "--policy-out", str(self.fixture / "policy.tsv")],
            text=True, capture_output=True, check=True,
        )
        digest = normalized.stdout.strip()
        root_hash = hashlib.sha256(str(self.fixture).encode()).hexdigest()
        self.binding = self.state / "bindings/by-root" / root_hash / "binding.tsv"
        self.binding.parent.mkdir(parents=True)
        binding = self.original.replace("binding_kind\tsingle-root", "binding_kind\tworking-set")
        binding = binding.replace(f"local_root\t{self.project}\n", f"local_root\t{self.fixture}\n")
        binding = binding.replace("topology_path\t\n", f"topology_path\t{snapshot}\n")
        binding = binding.replace("topology_digest\t\n", f"topology_digest\t{digest}\n")
        self.binding.write_text(binding)
        manifest = self.manifest.read_text().replace(f"local_root\t{self.project}\n", f"local_root\t{self.fixture}\n")
        manifest = manifest.replace("file\tsynthetic.secret\tsynthetic.secret", "file\tproject/synthetic.secret\tproject/synthetic.secret")
        self.manifest.write_text(manifest + f"topology_digest\t{digest}\n")
        self.excluded.write_text("project/synthetic.secret\tremote-writable\n")
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        topology_manifest.write_text(topology_manifest.read_text() + "remote_writable\tproject\tdata\n")
        self.assert_blocked_before_transport("active working-set topology has drifted")
        # Replacing the snapshot cannot silently replace the bound identity.
        subprocess.run(
            ["python3", str(REPO / "bin/remote-dev-local-agent-working-set.py"), "normalize",
             "--manifest", str(topology_manifest), "--snapshot-out", str(snapshot),
             "--policy-out", str(self.fixture / "policy.tsv")],
            text=True, capture_output=True, check=True,
        )
        self.assert_blocked_before_transport("active working-set snapshot digest does not match the binding")

    def test_projection_and_remote_role_checks_remain_active(self):
        self.managed.write_text("synthetic.secret\tfile\t0600\tplaceholder\t-\tpresent\n")
        self.assert_blocked_before_transport("runner secret destination overlaps managed source")
        self.managed.write_text("")
        self.excluded.write_text("synthetic.secret\tobserved-ignored\n")
        self.assert_blocked_before_transport("runner secret destination is not declared remote-writable")
        self.excluded.write_text("synthetic.secret\tremote-writable\n")
        self.environment["REMOTE_DEV_BINDING_TEST_ROLE"] = "development"
        result = self.plan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("workspace role mismatch", result.stderr)
        self.assertEqual(self.calls.read_text(), "role\n")


if __name__ == "__main__":
    unittest.main()
