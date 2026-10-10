#!/usr/bin/env python3
"""Runtime identity and timeout regressions for the project lifecycle contract."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("contract", ROOT / "templates/docker/project-contract.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".remote-dev").mkdir()
        self.contract = {"version": "2", "setup_command": "true", "start_command": "true", "quiesce_command": "true",
                         "stop_command": "true", "status_command": "true", "resource_id": "fixture",
                         "health_url": "http://127.0.0.1:3000/health", "browser_port": "3000"}
        self.context = {"operation_id": "a" * 64, "snapshot_id": "b" * 64}
        self.receipt = {"version": 1, "project_dir": str(self.root), "resource_id": "fixture", "state": "running",
                        "health_url": self.contract["health_url"], "health_resource": "1234", "preserved_volumes": [],
                        **self.context,
                        "resources": [{"kind": "process", "id": "1234", "project_dir": str(self.root), "resource_id": "fixture",
                                       "start_identity": "Mon Sep 1 10:00:00 2026", "process_group_id": 1234,
                                       "running": True, **self.context}]}

    def tearDown(self):
        self.temporary.cleanup()

    def validate(self, receipt=None, context=None):
        return C.validate_receipt(self.receipt if receipt is None else receipt, self.root, self.contract,
                                  self.context if context is None else context, require_candidate=True)

    def test_complete_candidate_receipt(self):
        self.assertEqual(self.validate(), self.receipt)

    def test_preview_ports_and_docker_capability_are_explicit_and_validated(self):
        path = self.root / ".remote-dev/project.env"
        self.contract.update(preview_ports="9000,9001", docker_capability="containers")
        for ports, valid in (("9000,9001", True), ("9000,9000", False), ("0", False), ("65536", False), ("9000:9000", False)):
            self.contract["preview_ports"] = ports
            path.write_text("\n".join(k + "=" + v for k, v in self.contract.items()))
            if valid:
                self.assertEqual(C.read_contract(self.root, switching=True)["preview_ports"], ports)
            else:
                with self.assertRaisesRegex(C.ContractError, "preview_ports"):
                    C.read_contract(self.root, switching=True)

    def test_missing_candidate_context_never_matches_missing_receipt(self):
        for key in self.context:
            self.receipt.pop(key)
            self.receipt["resources"][0].pop(key)
        with self.assertRaisesRegex(C.ContractError, "exact operation and snapshot"):
            self.validate(context={})

    def test_old_snapshot_and_unrelated_health_are_rejected(self):
        for field, wrong in (("snapshot_id", "c" * 64), ("health_url", "http://127.0.0.1:4000/health"), ("health_resource", "9999")):
            receipt = copy.deepcopy(self.receipt)
            receipt[field] = wrong
            with self.assertRaises(C.ContractError):
                self.validate(receipt)

    def test_process_pid_without_start_identity_rejected(self):
        self.receipt["resources"][0].pop("start_identity")
        with self.assertRaisesRegex(C.ContractError, "start identity"):
            self.validate()

    def test_numeric_booleans_and_empty_running_inventory_rejected(self):
        self.receipt["resources"][0]["running"] = 1
        with self.assertRaises(C.ContractError):
            self.validate()
        self.receipt["resources"] = []
        with self.assertRaises(C.ContractError):
            self.validate()

    def test_foreign_resource_path_and_duplicate_resource_rejected(self):
        self.receipt["resources"].append(copy.deepcopy(self.receipt["resources"][0]))
        with self.assertRaisesRegex(C.ContractError, "duplicate"):
            self.validate()
        self.receipt["resources"].pop()
        self.receipt["resources"][0]["project_dir"] = "/other"
        with self.assertRaisesRegex(C.ContractError, "OWNERSHIP_MISMATCH"):
            self.validate()

    def test_compose_requires_final_path_labels_and_preserved_volumes(self):
        resource = self.receipt["resources"][0]
        resource.update(kind="docker-container", id="c" * 64, volumes=["fixture-data"],
                        labels={"com.docker.compose.project": "fixture", "com.docker.compose.project.working_dir": str(self.root),
                                "com.docker.compose.service": "web"})
        self.receipt.update(health_resource=resource["id"], preserved_volumes=["fixture-data"])
        self.validate()
        resource["labels"]["com.docker.compose.project.working_dir"] = str(self.root / "temporary")
        with self.assertRaisesRegex(C.ContractError, "final project path"):
            self.validate()

    def test_timeout_terminates_child_writer_and_remains_unverified(self):
        self.contract["quiesce_command"] = "(sleep 0.4; touch escaped-writer) & wait"
        (self.root / ".remote-dev/project.env").write_text("\n".join(k + "=" + v for k, v in self.contract.items()) + "\n")
        with self.assertRaisesRegex(C.ContractError, "PROJECT_PHASE_TIMEOUT"):
            C.run_phase(self.root, "quiesce", self.context, timeout=0.1)
        time.sleep(0.5)
        self.assertFalse((self.root / "escaped-writer").exists())


if __name__ == "__main__":
    unittest.main()
