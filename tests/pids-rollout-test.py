#!/usr/bin/env python3
"""Focused checks for the Coder PID-policy release boundary."""
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pids_rollout", ROOT / "deploy/pids-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class PolicyTests(unittest.TestCase):
    def test_reviewed_source_digest_matches_platform_template(self):
        source = (ROOT / "templates/docker/main.tf").read_text()
        self.assertEqual(
            rollout.hashlib.sha256(
                rollout.policy_for_image(source, "remote-dev-workspace:policy-reference").encode()
            ).hexdigest(),
            rollout.POLICY_NORMALIZED_SHA256,
        )

    def test_image_derivation_changes_only_the_image_reference(self):
        source = (ROOT / "templates/docker/main.tf").read_text()
        changed = rollout.policy_for_image(source, "remote-dev-workspace:codex-0.154.0-abcdef")
        self.assertEqual(changed.count("remote-dev-workspace:codex-0.154.0-abcdef"), 1)
        self.assertEqual(rollout.policy_for_image(changed, "remote-dev-workspace:node24-20260906-cf2a26a364e"), source)
        with self.assertRaises(ValueError):
            rollout.policy_for_image(source, "alpine:latest")

    def test_runner_with_unresolved_execution_is_not_restarted(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            control = home / ".local/share/remote-dev/local-agent/project/processes/recovery"
            control.mkdir(parents=True)
            container = {"Mounts": [{"Destination": "/home/coder", "Source": directory}]}
            instance = rollout.Rollout.__new__(rollout.Rollout)
            instance.quiescence(container, "runner")
            (control / "orphan.identity.tsv").write_text("unresolved\n")
            with self.assertRaisesRegex(ValueError, "active or unresolved"):
                instance.quiescence(container, "runner")

    def test_multiple_terraform_files_are_refused(self):
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as archive:
            for name in ("main.tf", "other.tf"):
                data = b"{}"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "Multi-file"):
            rollout.source_from_archive(payload.getvalue())


if __name__ == "__main__":
    unittest.main()
