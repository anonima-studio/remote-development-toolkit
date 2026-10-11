#!/usr/bin/env python3
"""Focused checks for the Coder PID-policy release boundary."""
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pids_rollout", ROOT / "deploy/pids-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)


class PolicyTests(unittest.TestCase):
    def command_fixture(self, directory, capability="core", role="development", apply=False, source=None):
        """Run execute against isolated Coder/Docker responses, with real receipts."""
        if source is None:
            source = (ROOT / "templates/docker/main.tf").read_text()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as output:
            data = source.encode()
            entry = tarfile.TarInfo("main.tf")
            entry.size = len(data)
            output.addfile(entry, io.BytesIO(data))
        parameters = [{"name": "workspace_role", "value": role},
                      {"name": "docker_capability", "value": capability},
                      {"name": "workspace_size", "value": "standard"}]
        old = {"id": "old-build", "status": "running", "template_version_id": "old-version"}
        state = {"latest_build": old}
        workspace = {"id": "workspace-id", "template_id": "template-id", "automatic_updates": "never"}
        template = {"id": "template-id", "name": "remote-dev-docker", "active_version_id": "active-version"}

        def api(path, data=None, raw=False):
            if path in ("users/me/workspace/example", "workspaces/workspace-id"):
                return dict(workspace, latest_build=state["latest_build"], health={"healthy": True})
            if path == "workspacebuilds/old-build/parameters":
                return parameters
            if path == "templates/template-id":
                return template
            if path == "templateversions/active-version":
                return {"id": "active-version", "job": {"status": "succeeded", "file_id": "source-file"}}
            if path == "files/source-file" and raw:
                return archive.getvalue()
            if path == "workspaces/workspace-id/builds" and data:
                build = {"id": data["transition"] + "-build", "template_version_id": data.get("template_version_id")}
                state["latest_build"] = build
                return build
            raise AssertionError("Unexpected API call: " + path)

        cap = 2048 if capability == "containers" else 1024
        # An existing workspace may use an older image than the active template.
        image = "remote-dev-workspace:retained-image"
        container = {"Id": "container-id", "Image": "sha256:running-image",
                     "Config": {"Image": image},
                     "Mounts": [{"Destination": "/home/coder", "Source": directory}],
                     "HostConfig": {"PidsLimit": cap, "Ulimits": [{"Name": "nproc", "Soft": -1, "Hard": -1}]}}
        instance = rollout.Rollout.__new__(rollout.Rollout)
        instance.args = SimpleNamespace(workspace="example", apply=apply, maintenance_ready=True)
        instance.api = Mock(side_effect=api)
        instance.inspect_container = Mock(return_value=container)
        instance.existing_version = Mock(return_value=None)
        instance.publish = Mock(return_value="derived-version")
        instance.wait = Mock()

        def docker(*args):
            if args == ("docker", "image", "inspect", "--format", "{{.Id}}", image):
                return container["Image"]
            if args[:3] == ("docker", "exec", container["Id"]):
                return str(cap) + "\nMax processes  unlimited  unlimited  processes"
            raise AssertionError("Unexpected Docker call: " + repr(args))

        return instance, docker, parameters, cap, image

    def test_reviewed_source_digest_matches_platform_template(self):
        source = (ROOT / "templates/docker/main.tf").read_text()
        self.assertIn(
            rollout.hashlib.sha256(
                rollout.policy_for_image(source, "remote-dev-workspace:policy-reference").encode()
            ).hexdigest(),
            rollout.REVIEWED_POLICY_SHA256,
        )

    def test_image_derivation_changes_only_the_image_reference(self):
        source = (ROOT / "templates/docker/main.tf").read_text()
        changed = rollout.policy_for_image(source, "remote-dev-workspace:codex-0.154.0-abcdef")
        self.assertEqual(changed.count("remote-dev-workspace:codex-0.154.0-abcdef"), 1)
        self.assertEqual(rollout.policy_for_image(changed, "remote-dev-workspace:node24-20260906-44772a817ce"), source)
        with self.assertRaises(ValueError):
            rollout.policy_for_image(source, "alpine:latest")

    def test_command_checks_current_policy_without_changing_workspace(self):
        for capability in ("core", "containers"):
            for role in ("development", "runner"):
                with self.subTest(capability=capability, role=role), tempfile.TemporaryDirectory() as directory:
                    instance, docker, _, cap, image = self.command_fixture(directory, capability, role)
                    output = io.StringIO()
                    with patch.object(rollout, "run", side_effect=docker), redirect_stdout(output):
                        instance.execute()
                    receipt = json.loads(output.getvalue())
                    self.assertEqual(receipt["pids_limit"], cap)
                    self.assertEqual(receipt["image"], image)
                    self.assertFalse(receipt["apply"])
                    self.assertTrue(all(len(call.args) == 1 for call in instance.api.call_args_list))
                    instance.publish.assert_not_called()
                    instance.wait.assert_not_called()

    def test_command_applies_policy_preserving_image_and_parameters(self):
        for capability in ("core", "containers"):
            for role in ("development", "runner"):
                with self.subTest(capability=capability, role=role), tempfile.TemporaryDirectory() as directory:
                    instance, docker, parameters, cap, image = self.command_fixture(directory, capability, role, apply=True)
                    records = Path(directory) / "receipts"
                    output = io.StringIO()
                    with patch.object(rollout, "run", side_effect=docker), patch.object(rollout, "RECORD_ROOT", records), redirect_stdout(output):
                        instance.execute()
                    source = instance.publish.call_args.args[0]
                    self.assertEqual(source, rollout.policy_for_image((ROOT / "templates/docker/main.tf").read_text(), image))
                    mutations = [call.args for call in instance.api.call_args_list if len(call.args) > 1]
                    self.assertEqual(mutations, [
                        ("workspaces/workspace-id/builds", {"transition": "stop"}),
                        ("workspaces/workspace-id/builds", {"transition": "start", "template_version_id": "derived-version",
                                                          "rich_parameter_values": parameters}),
                    ])
                    receipt = json.loads(next(records.glob("*.json")).read_text())
                    self.assertEqual(receipt["stage"], "verified")
                    self.assertEqual(receipt["pids_limit"], cap)
                    self.assertEqual(receipt["image"], image)
                    self.assertIn("READY example PidsLimit=" + str(cap), output.getvalue())

    def test_command_refuses_unreviewed_template_before_any_changes(self):
        current = (ROOT / "templates/docker/main.tf").read_text()
        for source in (current.replace("? 2048 : 1024", "? 2048 : 9999"), current + "\n# unreviewed change\n"):
            with self.subTest(source=source[-40:]), tempfile.TemporaryDirectory() as directory:
                instance, docker, _, _, _ = self.command_fixture(directory, apply=True, source=source)
                records = Path(directory) / "receipts"
                with patch.object(rollout, "run", side_effect=docker), patch.object(rollout, "RECORD_ROOT", records):
                    with self.assertRaisesRegex(ValueError, "differs from the reviewed source"):
                        instance.execute()
                self.assertFalse(records.exists())
                self.assertTrue(all(len(call.args) == 1 for call in instance.api.call_args_list))
                instance.publish.assert_not_called()
                instance.wait.assert_not_called()

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
