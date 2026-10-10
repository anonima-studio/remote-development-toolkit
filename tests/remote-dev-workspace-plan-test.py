#!/usr/bin/env python3
"""Workspace owner failure/recovery and admission tests; no live mutations."""
import copy
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workspace_plan", ROOT / "bin/remote-dev-workspace-plan.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
GIB = 1024**3


def facts():
    return {"hostname": "host", "daemon_id": "daemon", "memory_total": 32*GIB, "memory_available": 20*GIB,
            "cpu_count": 12, "disk_free": 100*GIB, "pids": 100, "pid_max": 10000,
            "threads": 400, "threads_max": 10000,
            "pressure": {"cpu": {"some": 1}, "memory": {"full": 0}, "io": {"full": 0}},
            "containers": [], "networks": [], "volumes": ["existing-data"], "firewall": "unchanged",
            "egress_firewall": "unchanged", "rules": "unchanged", "routes": "unchanged"}


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.owner = W.Owner("fixture", "https://coder.example.invalid")
        self.before = {"name": "runner", "exists": True, "owner_id": "owner", "workspace_id": "workspace",
                       "organization_id": "org", "template_id": "template", "automatic_updates": "never",
                       "build_id": "old", "build_number": 3, "status": "stopped", "transition": "stop",
                       "initiator_id": "owner", "version_id": "version", "parameters": {
                           "workspace_role": "runner", "workspace_size": "small", "docker_capability": "containers"}}
        self.current = copy.deepcopy(self.before)
        self.version = {"version_id": "version", "template_id": "template", "organization_id": "org",
                        "version_name": "pinned", "image": {"image_id": "image"},
                        "limits_by_size": {"small": {"cpu": 1, "memory": 2*GIB, "pids": 2048}}}
        self.action = {"name": "runner", "kind": "start", "role": "runner", "capability": "containers",
                       "size": "small", "before": self.before, "version": self.version,
                       "limits": self.version["limits_by_size"]["small"], "host_at_plan": facts()}
        self.receipt = {}
        self.saved = []
        self.requests = []
        self.lose_response = False
        self.owner.inspect = lambda name: copy.deepcopy(self.current)
        self.owner.version = lambda identity: copy.deepcopy(self.version)
        self.owner.doctor = lambda: None
        self.owner.host_snapshot = self.host_snapshot
        self.owner.api = self.api

    def host_snapshot(self):
        result = facts()
        if self.current.get("status") == "running":
            result["containers"] = [{"workspace_id": "workspace", "running": True, "image": "image",
                "memory": 2*GIB, "cpu": 1, "runtime": "sysbox-runc", "ports": {}, "mounts": [],
                "pids_limit": 2048, "ulimits": [{"Name": "nproc", "Soft": -1, "Hard": -1}]}]
        return result

    def save(self):
        self.saved.append(copy.deepcopy(self.receipt))

    def api(self, path, data=None):
        self.assertTrue(self.saved[-1]["intent"])
        self.requests.append((path, data))
        self.current.update(build_id="new", build_number=4, status="running", transition="start")
        if self.lose_response:
            self.lose_response = False
            raise OSError("lost response")
        return {"id": "new"}

    def prepare(self):
        return self.owner.prepare(self.action, self.receipt, self.save, GIB)

    def test_start_freezes_version_and_records_actual_limits(self):
        result = self.prepare()
        self.assertEqual(self.requests, [("workspaces/workspace/builds", {"transition": "start", "template_version_id": "version"})])
        self.assertEqual(result["build_id"], "new")
        self.assertTrue(self.receipt["ready"])
        self.prepare()
        self.assertEqual(len(self.requests), 1)

    def test_lost_start_response_reconciles_exact_next_build_without_resubmit(self):
        self.lose_response = True
        with self.assertRaises(OSError):
            self.prepare()
        self.prepare()
        self.assertTrue(self.receipt["ready"])
        self.assertEqual(len(self.requests), 1)

    def test_unknown_before_post_gap_never_retries(self):
        self.receipt.update(intent=True, host_before=facts())
        with self.assertRaisesRegex(ValueError, "WORKSPACE_OUTCOME_UNKNOWN"):
            self.prepare()
        self.assertEqual(self.requests, [])

    def test_different_build_after_lost_response_is_not_adopted(self):
        self.lose_response = True
        with self.assertRaises(OSError):
            self.prepare()
        self.current["build_number"] = 5
        with self.assertRaisesRegex(ValueError, "WORKSPACE_OUTCOME_UNKNOWN"):
            self.prepare()
        self.assertFalse(self.receipt.get("ready", False))

    def test_admission_rejects_thread_exhaustion_even_with_few_processes(self):
        current = facts()
        current["threads"] = 7500
        current["pids"] = 100
        with self.assertRaisesRegex(ValueError, "HOST_CAPACITY_REQUIRED: pids"):
            W.CAPACITY.admit(current, {"memory": GIB, "cpu": 1, "pids": 2048}, GIB)

    def test_post_start_rejects_missing_container_pid_cap(self):
        self.current.update(build_id="new", build_number=4, status="running", transition="start")
        self.receipt.update(intent=True, build_id="new", host_before=facts())
        original = self.host_snapshot

        def uncapped_snapshot():
            result = original()
            result["containers"][0]["pids_limit"] = None
            return result

        self.owner.host_snapshot = uncapped_snapshot
        with self.assertRaisesRegex(ValueError, "WORKSPACE_LIMITS_CHANGED"):
            self.prepare()

    def test_creation_uses_frozen_owner_and_version_uuid(self):
        self.action["kind"] = "create"
        self.action["before"] = {"exists": False, "name": "runner", "owner_id": "owner"}
        self.current = copy.deepcopy(self.action["before"])
        def create(path, data):
            self.assertTrue(self.saved[-1]["intent"])
            self.assertEqual(path, "users/owner/workspaces")
            self.assertEqual(data["template_version_id"], "version")
            self.assertEqual(data["automatic_updates"], "never")
            self.assertNotIn("template_id", data)
            self.current = {**self.before, "build_id": "new", "build_number": 1, "status": "running", "transition": "start"}
            return {"id": "workspace", "latest_build": {"id": "new"}}
        with patch.object(self.owner, "api", side_effect=create) as run:
            self.prepare()
            self.prepare()
            self.assertEqual(run.call_count, 1)

    def test_creation_response_loss_cannot_adopt_matching_workspace(self):
        self.action["kind"] = "create"
        self.receipt.update(intent=True, host_before=facts())
        self.current.update(build_id="new", build_number=1, status="running", transition="start")
        with patch.object(self.owner, "api") as run:
            with self.assertRaisesRegex(ValueError, "WORKSPACE_OUTCOME_UNKNOWN"):
                self.prepare()
            run.assert_not_called()

    def test_capacity_failure_precedes_intent_and_request(self):
        self.owner.host_snapshot = lambda: {**facts(), "memory_available": GIB}
        with self.assertRaisesRegex(ValueError, "HOST_CAPACITY_REQUIRED"):
            self.prepare()
        self.assertFalse(self.receipt.get("intent", False))
        self.assertEqual(self.requests, [])

    def test_neighbor_mutation_blocks_ready_receipt(self):
        original = self.owner.host_snapshot
        def changed():
            result = original()
            if self.current["status"] == "running":
                result["firewall"] = "foreign change"
            return result
        self.owner.host_snapshot = changed
        with self.assertRaisesRegex(ValueError, "HOST_CHANGED"):
            self.prepare()
        self.assertFalse(self.receipt.get("ready", False))


class CapacityTests(unittest.TestCase):
    def test_uncapped_neighbors_require_explicit_shared_budget(self):
        current = facts()
        current["containers"] = [{"running": True, "memory": 0, "cpu": 0}]
        with self.assertRaisesRegex(ValueError, "HOST_UNBOUNDED_BUDGET_REQUIRED"):
            W.CAPACITY.admit(current, {"memory": GIB, "cpu": 1}, GIB)
        W.CAPACITY.admit(current, {"memory": GIB, "cpu": 1}, GIB, {"memory": 4*GIB, "cpu": 2})
        with self.assertRaisesRegex(ValueError, "HOST_CAPACITY_REQUIRED"):
            W.CAPACITY.admit(current, {"memory": GIB, "cpu": 1}, GIB, {"memory": 30*GIB, "cpu": 2})

    def test_source_allocation_stays_in_admission(self):
        current = facts()
        current["containers"] = [{"running": True, "memory": 28*GIB, "cpu": 10}]
        with self.assertRaisesRegex(ValueError, "HOST_CAPACITY_REQUIRED"):
            W.CAPACITY.admit(current, {"memory": 4*GIB, "cpu": 2}, GIB)

    def test_missing_target_does_not_exclude_unowned_neighbors(self):
        before, after = facts(), facts()
        after["containers"] = [{"workspace_id": None, "name": "foreign"}]
        with self.assertRaisesRegex(ValueError, "neighboring"):
            W.CAPACITY.compare(before, after, None)

    def test_new_foreign_volumes_are_detected(self):
        before, after = facts(), facts()
        after["volumes"].append("foreign")
        with self.assertRaisesRegex(ValueError, "unrelated volume"):
            W.CAPACITY.compare(before, after, "workspace")

    def test_io_pressure_blocks_allocation(self):
        current = facts()
        current["pressure"]["io"]["full"] = 2
        with self.assertRaisesRegex(ValueError, "pressure"):
            W.CAPACITY.admit(current, {"memory": GIB, "cpu": 1}, GIB)


class TemplateTests(unittest.TestCase):
    def test_matching_local_extra_terraform_is_still_not_reviewed_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"templates/docker").mkdir(parents=True)
            main = (ROOT/"templates/docker/main.tf").read_bytes()
            (root/"templates/docker/main.tf").write_bytes(main)
            (root/"templates/docker/extra.tf").write_bytes(b"{}")
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                for path, data in (("main.tf", main), ("extra.tf", b"{}")):
                    info = tarfile.TarInfo(path)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            owner = W.Owner("fixture", "https://coder.example.invalid")
            def api(path, raw=False):
                if path.startswith("templateversions/"):
                    return {"template_id": "template", "name": "version", "job": {"status": "succeeded", "file_id": "file"}}
                if path.startswith("templates/"):
                    return {"name": "remote-dev-docker", "id": "template", "organization_id": "org"}
                return output.getvalue()
            owner.api = api
            with patch.object(W, "ROOT", root), patch.object(owner, "host") as host:
                with self.assertRaisesRegex(ValueError, "WORKSPACE_TEMPLATE_UNSUPPORTED"):
                    owner.version("a"*36)
                host.assert_not_called()

    def test_only_regular_unique_appledouble_metadata_is_accepted(self):
        for metadata, duplicate, allowed in ((b"\x00\x05\x16\x07\x00\x02\x00\x00"+b"\0"*155, False, True),
                                             (b"not AppleDouble", False, False),
                                             (b"\x00\x05\x16\x07\x00\x02\x00\x00", True, False)):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                entries = [("main.tf", (ROOT/"templates/docker/main.tf").read_bytes()), ("._main.tf", metadata)]
                if duplicate:
                    entries.append(("._main.tf", metadata))
                for path, data in entries:
                    info = tarfile.TarInfo(path)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            owner = W.Owner("fixture", "https://coder.example.invalid")
            def api(path, raw=False):
                if path.startswith("templateversions/"):
                    return {"template_id": "template", "name": "version", "job": {"status": "succeeded", "file_id": "file"}}
                if path.startswith("templates/"):
                    return {"name": "remote-dev-docker", "id": "template", "organization_id": "org"}
                return output.getvalue()
            owner.api = api
            with patch.object(owner, "host", return_value={"image_id": "image"}) as host:
                if allowed:
                    self.assertEqual(owner.version("a"*36)["image"]["image_id"], "image")
                else:
                    with self.assertRaisesRegex(ValueError, "WORKSPACE_TEMPLATE_UNSUPPORTED"):
                        owner.version("a"*36)
                    host.assert_not_called()

    def test_extra_terraform_or_variable_input_refused_before_host_or_create(self):
        for name in ("extra.tf", "override.tf.json", "terraform.tfvars", "extra.auto.tfvars.json"):
            with self.subTest(name=name):
                output = io.BytesIO()
                with tarfile.open(fileobj=output, mode="w") as archive:
                    for path, data in (("main.tf", (ROOT/"templates/docker/main.tf").read_bytes()), (name, b"{}")):
                        info = tarfile.TarInfo(path)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                owner = W.Owner("fixture", "https://coder.example.invalid")
                def api(path, raw=False):
                    if path.startswith("templateversions/"):
                        return {"template_id": "template", "name": "version", "job": {"status": "succeeded", "file_id": "file"}}
                    if path.startswith("templates/"):
                        return {"name": "remote-dev-docker", "id": "template", "organization_id": "org"}
                    return output.getvalue()
                owner.api = api
                with patch.object(owner, "host") as host:
                    with self.assertRaisesRegex(ValueError, "WORKSPACE_TEMPLATE_UNSUPPORTED"):
                        owner.version("a"*36)
                    host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
