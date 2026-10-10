#!/usr/bin/env python3
"""State machine tests with real Git/filesystem and simulated runtime/transport."""
import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("mode", str(ROOT / "bin/remote-dev-mode"))
spec = importlib.util.spec_from_loader(loader.name, loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)
O = M.module_from_file("operation", ROOT / "bin/remote-dev-mode-operation.py")
O.MODE = M
REAL_LOCAL_AGENT = O.Operation.local_agent
REAL_BINDING = O.Operation.binding

CONTRACT = """version=2
setup_command=true
start_command=true
quiesce_command=true
stop_command=true
status_command=true
health_url=http://127.0.0.1:3000/health
browser_port=3000
resource_id=fixture
"""


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.homes = {None: self.base / "mac", "development": self.base / "development", "runner": self.base / "runner"}
        for role, home in self.homes.items():
            (home / "projects").mkdir(parents=True)
            if role:
                marker = home / ".local/share/remote-dev/workspace-role"
                marker.parent.mkdir(parents=True)
                marker.write_text(role + "\n")
        self.environment = patch.dict(os.environ, {"HOME": str(self.homes[None]), "REMOTE_DEV_MODE_STATE_ROOT": str(self.base / "state"),
                                                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        self.environment.start()
        self.source = self.homes["development"] / "projects/fixture"
        self.local = self.homes[None] / "projects/fixture"
        self.runner_root = self.homes["runner"] / "projects/fixture"
        self.source.mkdir()
        self.git(self.source, "init", "-q", "-b", "main")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / ".remote-dev").mkdir()
        (self.source / ".remote-dev/project.env").write_text(CONTRACT)
        (self.source / "file").write_text("initial\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "fixture")
        (self.source / "file").write_text("staged\n")
        self.git(self.source, "add", "file")
        (self.source / "file").write_text("unstaged\n")
        self.runtime = {"development": True, "runner": False}
        self.identities = {}
        self.bound = False
        self.calls = []
        self.fail_action = None
        self.crash_phase = None
        self.stale_image = False
        fixture = self

        class Endpoint:
            def __init__(self, profile=None, origin=None, workspace=None, role=None):
                self.profile, self.origin, self.workspace, self.role = profile, origin, workspace, role

            def inspect(self, root, allowed=()):
                with patch.dict(os.environ, {"HOME": str(fixture.homes[self.role])}):
                    info = M.inspect_checkout(str(Path(root).expanduser()), self.role, allowed)
                    if fixture.stale_image and self.role == "runner":
                        info["image_features"] = {"guard": {"matches": False}}
                    return info

            def request(self, action, payload, input_path=None, output_path=None):
                fixture.calls.append((action, self.role, payload["root"]))
                if fixture.fail_action == action:
                    fixture.fail_action = None
                    raise M.ModeError("INJECTED_FAILURE: " + action)
                with patch.dict(os.environ, {"HOME": str(fixture.homes[self.role])}):
                    if action == "lifecycle":
                        # Keep the real central source gate even though these
                        # tests simulate the application runtime itself.
                        M.ENDPOINT.Transaction({**payload, "role": self.role}).verify_lifecycle_source()
                        phase = payload["lifecycle_phase"]
                        if phase in ("setup", "start"):
                            fixture.assertEqual(payload["root"], str(fixture.runner_root if self.role == "runner" else fixture.source))
                            fixture.identities[self.role] = payload["context"]
                        if phase == "start":
                            fixture.runtime[self.role] = True
                        elif phase in ("stop", "quiesce"):
                            fixture.runtime[self.role] = False
                        if phase == "status":
                            if payload.get("require_candidate") and not fixture.runtime[self.role]:
                                raise M.ModeError("RUNTIME_UNVERIFIED")
                            return {"state": "running" if fixture.runtime[self.role] else "stopped", **fixture.identities.get(self.role, {})}
                        return {"phase": phase}
                    transaction = M.ENDPOINT.Transaction({**payload, "role": self.role})
                    if input_path:
                        with open(input_path, "rb") as source:
                            return transaction.handle(action, source=source)
                    if output_path:
                        with open(output_path, "xb") as destination:
                            return transaction.handle(action, destination=destination)
                    return transaction.handle(action)

        def local_agent(operation, action):
            fixture.calls.append(("local-agent-" + action, "runner", operation.local_root))
            if action in ("up", "exec"):
                fixture.runner_root.mkdir(exist_ok=True)
                # Project from the binding's own anchor: an adopted plan moves
                # the Mac root, so the fixture must not assume a fixed path.
                anchor = Path(operation.local_root)
                snapshot = M.TRANSFER.inspect(anchor)
                for entry in snapshot["worktree"]:
                    path = entry["path"]
                    source, target = anchor / path, fixture.runner_root / path
                    if source.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                    elif not source.exists() and target.exists():
                        target.unlink()
                fixture.bound = True
            return {"action": action, "completed": True}

        real_phase = O.Operation.phase

        def phase(operation, value):
            # Crash with the journal already durable at this phase, the way a
            # process death between two phases leaves it behind.
            real_phase(operation, value)
            if fixture.crash_phase == value:
                fixture.crash_phase = None
                raise OSError("INJECTED_CRASH: " + value)

        self.patches = [patch.object(M, "Endpoint", Endpoint), patch.object(O.Operation, "local_agent", local_agent),
                        patch.object(O.Operation, "phase", phase),
                        patch.object(O.Operation, "binding", lambda operation: {"fixture": True} if fixture.bound else None),
                        patch.object(O.Operation, "preview", lambda operation: {"url": "http://127.0.0.1:3000", "verified_at": 1})]
        for item in self.patches:
            item.start()
        self.store = M.Store()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout

    def plan(self, to="local-agent", source_mode="remote", secrets_manifest=None, **options):
        args = argparse.Namespace(for_context=str(self.local) if self.store.for_context(str(self.local)) else None,
                                  project_id="fixture", profile="fixture", coder_url="https://coder.example.invalid",
                                  source_mode=source_mode, local_project=str(self.local), development_workspace="development",
                                  runner_workspace="runner", remote_project_dir="fixture", allow_untracked=[], to=to,
                                  data_policy="isolated", secrets_manifest=secrets_manifest)
        vars(args).update(options)
        result = M.plan(args, self.store)
        self.assertEqual(result["blockers"], [])
        return result["plan_id"]

    def execute(self, identity, command="apply", paused=True, **options):
        args = argparse.Namespace(command=command, identity=identity, writers_paused=paused)
        vars(args).update(options)
        return O.execute(args, self.store)

    def preparation_plan(self, identity, role="runner"):
        plan = self.store.read("plans", identity)
        plan.pop("plan_id")
        action = {"name": role, "role": role, "kind": "create" if role == "runner" else "start"}
        plan["new_workspace_actions"] = [action]
        key = "runner" if role == "runner" else "target"
        plan[key] = {"root": plan[key]["root"], "role": role, "deferred_workspace": role}
        plan["plan_id"] = M.checksum(M.encode(plan))
        self.store.create("plans", plan["plan_id"], plan)
        return plan["plan_id"]

    def test_preparation_finishes_before_source_stop_and_preserves_wip(self):
        identity = self.preparation_plan(self.plan())
        before = M.TRANSFER.inspect(self.source)["snapshot_id"]
        def prepare(action, receipt, save, disk):
            self.assertTrue(self.runtime["development"])
            self.assertFalse(any(call[0] == "claim" for call in self.calls))
            receipt.update(intent=True, ready=True)
            save()
        with patch.object(M.WORKSPACE.Owner, "prepare", side_effect=prepare):
            self.assertEqual(self.execute(identity)["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], before)
        record = self.store.read("operations", identity)
        self.assertIn("runner", record["prepared_endpoint_info"])
        for key, receipt in record["workspace_preparations"].items():
            self.assertTrue(self.store.read("workspace-reservations", key)["complete"])
            self.assertTrue(self.store.read("host-preparations", receipt["host_key"])["complete"])

    def test_unknown_preparation_abort_keeps_host_and_workspace_reservations(self):
        identity = self.preparation_plan(self.plan())
        before = M.TRANSFER.inspect(self.source)["snapshot_id"]
        def uncertain(action, receipt, save, disk):
            receipt["intent"] = True
            save()
            raise ValueError("WORKSPACE_OUTCOME_UNKNOWN: lost create response")
        with patch.object(M.WORKSPACE.Owner, "prepare", side_effect=uncertain):
            with self.assertRaisesRegex(M.ModeError, "WORKSPACE_OUTCOME_UNKNOWN"):
                self.execute(identity)
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")
        self.assertTrue(self.runtime["development"])
        self.assertFalse(self.calls)
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], before)
        record = self.store.read("operations", identity)
        for key, receipt in record["workspace_preparations"].items():
            self.assertFalse(self.store.read("workspace-reservations", key)["complete"])
            self.assertFalse(self.store.read("host-preparations", receipt["host_key"])["complete"])
        with patch.object(M.time, "time", return_value=M.time.time()+1):
            following = self.preparation_plan(self.plan())
        with patch.object(M.WORKSPACE.Owner, "prepare") as prepare:
            with self.assertRaisesRegex(M.ModeError, "HOST_PREPARATION_BUSY"):
                self.execute(following)
            prepare.assert_not_called()

    def test_foreign_workspace_preparation_on_same_host_blocks_before_allocation(self):
        identity = self.preparation_plan(self.plan())
        key = M.WORKSPACE.digest(["fixture", "https://coder.example.invalid"])
        self.store.write("host-preparations", key, {"operation_id": "another-project-operation", "complete": False})
        with patch.object(M.WORKSPACE.Owner, "prepare") as prepare:
            with self.assertRaisesRegex(M.ModeError, "HOST_PREPARATION_BUSY"):
                self.execute(identity)
            prepare.assert_not_called()
        self.assertTrue(self.runtime["development"])
        self.assertFalse(self.calls)

    def test_ordinary_apply_cannot_bypass_unresolved_workspace_preparation(self):
        identity = self.plan()
        key = M.WORKSPACE.digest(["fixture", "runner"])
        self.store.write("workspace-reservations", key, {"operation_id": "unknown-create", "complete": False})
        with self.assertRaisesRegex(M.ModeError, "WORKSPACE_BUSY"):
            self.execute(identity)
        self.assertFalse(self.calls)
        self.assertTrue(self.runtime["development"])

    def test_live_host_lock_survives_until_provisioning_outcome_is_known(self):
        identity = self.preparation_plan(self.plan())
        ready_read, ready_write = os.pipe()
        hold_read, hold_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(ready_read)
            os.close(hold_write)
            def hold(action, receipt, save, disk):
                receipt["intent"] = True
                save()
                os.write(ready_write, b"1")
                os.read(hold_read, 1)
            try:
                with patch.object(M.WORKSPACE.Owner, "prepare", side_effect=hold):
                    self.execute(identity)
            finally:
                os._exit(0)
        os.close(ready_write)
        os.close(hold_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 30)[0])
            self.assertEqual(os.read(ready_read, 1), b"1")
            key = M.WORKSPACE.digest(["fixture", "https://coder.example.invalid"])
            with self.assertRaisesRegex(M.ModeError, "OPERATION_BUSY"):
                with self.store.mutex("host-" + key):
                    self.fail("different workspace acquired host admission during allocation")
            self.assertFalse(self.store.read("host-preparations", key)["complete"])
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(ready_read)
            os.close(hold_write)
        self.execute(identity, "abort")
        self.assertFalse(self.store.read("host-preparations", key)["complete"])

    def test_plan_defers_missing_runner_without_contact_or_autostart(self):
        original = M.Endpoint.inspect
        def inspect(endpoint, *args, **kwargs):
            self.assertNotEqual(endpoint.role, "runner")
            return original(endpoint, *args, **kwargs)
        action = {"name": "runner", "role": "runner", "kind": "create", "limits": {"cpu": 1, "memory": 1024**3}}
        with patch.object(M.Endpoint, "inspect", inspect), patch.object(M.WORKSPACE.Owner, "freeze", return_value=action), \
                patch.object(M.WORKSPACE.Owner, "host_snapshot", return_value={}), patch.object(M.WORKSPACE.CAPACITY, "admit"):
            identity = self.plan(prepare_workspace=["runner"], workspace_size="small", workspace_template_version="a"*36)
        plan = self.store.read("plans", identity)
        self.assertEqual(plan["runner"], {"root": "/home/coder/projects/fixture", "role": "runner", "deferred_workspace": "runner"})
        self.assertEqual(plan["new_workspace_actions"][0]["kind"], "create")
        self.assertTrue(self.runtime["development"])
        self.assertFalse(self.calls)

    def test_started_target_drift_blocks_before_stopping_active_runner(self):
        self.execute(self.plan())
        identity = self.preparation_plan(self.plan("remote"), "development")
        before = M.TRANSFER.inspect(self.local)["snapshot_id"]
        def prepare(action, receipt, save, disk):
            (self.source / "file").write_text("foreign edit after workspace start\n")
            receipt.update(intent=True, ready=True)
            save()
        with patch.object(M.WORKSPACE.Owner, "prepare", side_effect=prepare):
            with self.assertRaisesRegex(M.ModeError, "TARGET_CHANGED"):
                self.execute(identity)
        self.assertTrue(self.runtime["runner"])
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], before)

    def test_round_trip_preserves_new_mac_staged_and_unstaged_wip(self):
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        first = self.plan()
        self.assertEqual(self.execute(first)["active_mode"], "local-agent")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], initial)
        (self.local / "file").write_text("new staged\n")
        self.git(self.local, "add", "file")
        (self.local / "file").write_text("new unstaged\n")
        current = M.TRANSFER.inspect(self.local)["snapshot_id"]
        second = self.plan("remote")
        self.assertEqual(self.execute(second)["active_mode"], "remote")
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], current)
        third = self.plan()
        self.assertEqual(self.execute(third)["active_mode"], "local-agent")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], current)

    def test_abort_after_publish_preserves_original_authority(self):
        identity = self.plan()
        with patch.object(O.Operation, "preview", side_effect=M.ModeError("PREVIEW_FAILED")):
            with self.assertRaisesRegex(M.ModeError, "PREVIEW_FAILED"):
                self.execute(identity)
        self.assertTrue(self.local.exists())
        result = self.execute(identity, "abort")
        self.assertEqual(result["state"], "aborted")
        self.assertFalse(self.local.exists())
        self.assertFalse(self.runtime["runner"])
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")

    def test_initial_local_agent_to_empty_remote_target_and_retry(self):
        self.source.rename(self.local)
        self.source.mkdir()
        self.bound = True
        self.runtime = {"development": False, "runner": True}
        expected = M.TRANSFER.inspect(self.local)["snapshot_id"]
        identity = self.plan("remote", source_mode="local-agent")
        self.fail_action = "upload"
        with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
            self.execute(identity)
        self.assertFalse(self.runtime["runner"])
        self.assertFalse(any(action == "lifecycle" and role == "development"
                             for action, role, _ in self.calls))
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertEqual(self.execute(identity, "resume")["active_mode"], "remote")
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], expected)
        self.assertTrue(self.runtime["development"])
        self.assertEqual(self.execute(identity)["operation_id"], identity)

    def test_abort_refuses_changed_runner_contract_before_any_hook(self):
        identity = self.plan()
        with patch.object(O.Operation, "preview", side_effect=M.ModeError("PREVIEW_FAILED")):
            with self.assertRaisesRegex(M.ModeError, "PREVIEW_FAILED"):
                self.execute(identity)
        marker = self.base / "untrusted-hook-ran"
        contract = self.runner_root / ".remote-dev/project.env"
        contract.write_text(CONTRACT.replace("status_command=true", "status_command=touch " + str(marker)))
        with self.assertRaisesRegex(M.ENDPOINT.EndpointError, "PROJECTION_MISMATCH"):
            self.execute(identity, "abort")
        self.assertFalse(marker.exists())
        self.assertTrue(self.runtime["runner"])
        self.assertTrue(self.local.exists())
        self.assertEqual(self.store.read("operations", identity)["state"], "failed")

    def test_partial_claim_abort_releases_only_own_claims_without_hooks(self):
        identity = self.plan()
        foreign = {"operation_id": "f" * 64, "token": "e" * 64, "project_id": "fixture",
                   "root": str(self.runner_root), "role": "runner"}
        with patch.dict(os.environ, {"HOME": str(self.homes["runner"])}):
            transaction = M.ENDPOINT.Transaction(foreign)
            transaction.handle("claim")
        with self.assertRaisesRegex(M.ENDPOINT.EndpointError, "OPERATION_BUSY"):
            self.execute(identity)
        self.assertEqual(self.store.read("operations", identity)["phase"], "planned")
        result = self.execute(identity, "abort")
        self.assertEqual(result["state"], "aborted")
        self.assertTrue(self.runtime["development"])
        self.assertFalse(any(call[0] == "lifecycle" for call in self.calls))
        self.assertEqual(M.ENDPOINT.read_record(transaction.claim), transaction.owner)
        for home in (self.homes[None], self.homes["development"]):
            self.assertEqual(list((home / ".local/share/remote-dev/mode-endpoints/claims").glob("*.json")), [])
        self.assertTrue(self.store.read("pending", "fixture")["complete"])

    def test_resume_transport_failure_uses_same_operation(self):
        identity = self.plan()
        self.fail_action = "upload"
        with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
            self.execute(identity)
        self.assertFalse(self.local.exists())
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(self.execute(identity)["operation_id"], identity)
        with self.assertRaisesRegex(M.ModeError, "REVERSE_PLAN_REQUIRED"):
            self.execute(identity, "abort")

    def test_source_drift_blocks_resume(self):
        identity = self.plan()
        self.fail_action = "restore"
        with self.assertRaises(M.ModeError):
            self.execute(identity)
        (self.source / "file").write_text("concurrent edit\n")
        with self.assertRaisesRegex(M.ModeError, "SOURCE_CHANGED"):
            self.execute(identity, "resume")
        self.assertEqual((self.source / "file").read_text(), "concurrent edit\n")
        self.assertIsNone(self.store.read("projects", "fixture"))

    def test_new_apply_requires_explicit_writer_pause(self):
        identity = self.plan()
        with self.assertRaisesRegex(M.ModeError, "WRITERS_PAUSE_REQUIRED"):
            self.execute(identity, paused=False)
        self.assertIsNone(self.store.read("operations", identity))
        self.assertTrue(self.runtime["development"])

    def secret_manifest(self, workspace="runner"):
        path = self.base / "secrets.tsv"
        fields = {"manifest_version": "2", "project_id": "fixture", "workspace": workspace, "workspace_role": workspace,
                  "local_root": str(self.local), "remote_root": str(self.runner_root if workspace == "runner" else self.source)}
        path.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()) + "file\t.env\t.env\n")
        path.chmod(0o600)
        return path

    def test_remote_source_grant_precedes_helper_and_survives_resume(self):
        path = self.secret_manifest()
        path.write_text(path.read_text().replace("manifest_version\t2", "manifest_version\t3") +
                        "source_workspace\tdevelopment\nsource_workspace_role\tdevelopment\n" +
                        "source_root\t/home/coder\nsource_context\t" + str(self.source) + "\n")
        identity = self.plan(secrets_manifest=str(path))
        self.assertIn("secrets_deferred_checks", self.store.read("plans", identity))
        with patch.object(O.Operation, "secret_command", return_value=True) as helper:
            with self.assertRaisesRegex(M.ModeError, "SECRETS_APPLY_REQUIRED"):
                self.execute(identity)
            helper.assert_not_called()
            arguments = self.secret_arguments(identity, "runner")
            arguments.confirm_source = "wrong"
            with self.assertRaisesRegex(M.ModeError, "SECRETS_CONFIRMATION_REQUIRED"):
                O.execute(arguments, self.store)
            helper.assert_not_called()
            self.assertNotIn("secrets_read_grant", self.store.read("operations", identity))
            arguments.confirm_source = "development"
            original = path.read_text()
            path.write_text(original.replace("source_root\t/home/coder\n", "source_root\t/home/coder/other\n"))
            with self.assertRaisesRegex(M.ModeError, "SECRETS_ROUTE_CHANGED"):
                O.execute(arguments, self.store)
            helper.assert_not_called()
            self.assertNotIn("secrets_read_grant", self.store.read("operations", identity))
            path.write_text(original)
            self.assertEqual(O.execute(arguments, self.store)["secrets"], "verified")
            journal = self.store.read("operations", identity)
            self.assertEqual(journal["secrets_read_grant"], self.store.read("plans", identity)["secrets_route"]["sha256"])
            self.assertEqual(self.execute(identity, "resume")["state"], "activated")

    def test_secrets_are_explicit_after_publication_and_before_setup(self):
        identity = self.plan(secrets_manifest=str(self.secret_manifest()))
        secret_calls = []
        installed = False
        def secret_command(operation, action, confirmations=()):
            nonlocal installed
            secret_calls.append(action)
            self.assertTrue(self.local.exists())
            self.assertTrue(self.bound)
            if action == "apply":
                installed = True
            return installed
        with patch.object(O.Operation, "secret_command", secret_command):
            with self.assertRaisesRegex(M.ModeError, "SECRETS_APPLY_REQUIRED"):
                self.execute(identity)
            journal = self.store.read("operations", identity)
            self.assertEqual(journal["phase"], "published-inactive")
            self.assertFalse(journal.get("setup_complete"))
            self.assertEqual(secret_calls, ["verify"])
            self.assertFalse(self.runtime["runner"])
            arguments = argparse.Namespace(command="secrets", identity=identity, confirm="wrong", confirm_profile="fixture",
                                           confirm_origin="https://coder.example.invalid", replace=False)
            with self.assertRaisesRegex(M.ModeError, "SECRETS_CONFIRMATION_REQUIRED"):
                O.execute(arguments, self.store)
            self.assertNotIn("apply", secret_calls)
            arguments.confirm = "runner"
            self.assertEqual(O.execute(arguments, self.store)["secrets"], "verified")
            self.assertFalse(self.runtime["runner"])
            self.assertEqual(self.execute(identity, "resume")["active_mode"], "local-agent")
            with self.assertRaisesRegex(M.ModeError, "SECRETS_CHECKPOINT_REQUIRED"):
                O.execute(arguments, self.store)

    def secret_arguments(self, identity, workspace="development"):
        return argparse.Namespace(command="secrets", identity=identity, confirm=workspace, confirm_profile="fixture",
                                  confirm_origin="https://coder.example.invalid", replace=False)

    def remote_secret_candidate(self):
        (self.source / ".gitignore").write_text(".env\nother-cache\n")
        self.git(self.source, "add", ".gitignore")
        self.source.rename(self.local)
        self.source.mkdir()
        self.bound = True
        self.runtime = {"development": False, "runner": True}
        identity = self.plan("remote", source_mode="local-agent", secrets_manifest=str(self.secret_manifest("development")))
        with patch.object(O.Operation, "secret_command", return_value=False):
            with self.assertRaisesRegex(M.ModeError, "SECRETS_APPLY_REQUIRED"):
                self.execute(identity)
        return identity

    def test_abort_retains_completed_secret_installation(self):
        identity = self.remote_secret_candidate()
        def install(operation, action, confirmations=()):
            if action == "apply":
                (self.source / ".env").write_bytes(b"synthetic completed secret")
                (self.source / ".env").chmod(0o600)
            return True
        with patch.object(O.Operation, "secret_command", install):
            self.assertEqual(O.execute(self.secret_arguments(identity), self.store)["secrets"], "verified")
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")
        retained = list(self.source.parent.glob(".remote-dev-mode-*/aborted-candidate/.env"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"synthetic completed secret")
        self.assertTrue(self.local.exists())
        self.assertEqual(list(self.source.iterdir()), [])

    def test_partial_secret_installation_can_abort_and_retains_data(self):
        identity = self.remote_secret_candidate()
        def install(operation, action, confirmations=()):
            (self.source / ".env").write_bytes(b"synthetic partial secret")
            return False
        with patch.object(O.Operation, "secret_command", install):
            with self.assertRaisesRegex(M.ModeError, "SECRETS_TRANSFER_INCOMPLETE"):
                O.execute(self.secret_arguments(identity), self.store)
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")
        retained = list(self.source.parent.glob(".remote-dev-mode-*/aborted-candidate/.env"))
        self.assertEqual(retained[0].read_bytes(), b"synthetic partial secret")

    def test_secret_installation_does_not_adopt_unrelated_ignored_drift(self):
        identity = self.remote_secret_candidate()
        def install(operation, action, confirmations=()):
            (self.source / ".env").write_bytes(b"synthetic secret")
            (self.source / "other-cache").write_bytes(b"unrelated write")
            return True
        with patch.object(O.Operation, "secret_command", install):
            with self.assertRaisesRegex(M.ENDPOINT.EndpointError, "outside the approved"):
                O.execute(self.secret_arguments(identity), self.store)
        with self.assertRaisesRegex(M.ENDPOINT.EndpointError, "outside the approved"):
            self.execute(identity, "abort")
        self.assertEqual((self.source / "other-cache").read_bytes(), b"unrelated write")
        self.assertEqual((self.source / ".env").read_bytes(), b"synthetic secret")

    def test_abort_recovers_secret_window_after_lost_finish_response(self):
        identity = self.remote_secret_candidate()
        def install(operation, action, confirmations=()):
            (self.source / ".env").write_bytes(b"synthetic secret")
            self.fail_action = "secrets-finish"
            return True
        with patch.object(O.Operation, "secret_command", install):
            with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
                O.execute(self.secret_arguments(identity), self.store)
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")

    def test_failed_setup_prevents_secret_replacement(self):
        identity = self.plan(secrets_manifest=str(self.secret_manifest()))
        real_lifecycle = O.Operation.lifecycle
        def failed_setup(operation, endpoint, root, phase, **kwargs):
            if phase == "setup":
                raise M.ModeError("SETUP_FAILED")
            return real_lifecycle(operation, endpoint, root, phase, **kwargs)
        with patch.object(O.Operation, "secret_command", return_value=True), patch.object(O.Operation, "lifecycle", failed_setup):
            with self.assertRaisesRegex(M.ModeError, "SETUP_FAILED"):
                self.execute(identity)
        journal = self.store.read("operations", identity)
        self.assertTrue(journal["runtime_attempted"])
        self.assertFalse(journal.get("setup_complete"))
        with patch.object(O.Operation, "secret_command") as helper:
            with self.assertRaisesRegex(M.ModeError, "SECRETS_CHECKPOINT_REQUIRED"):
                O.execute(self.secret_arguments(identity, "runner"), self.store)
            helper.assert_not_called()

    def test_late_secret_failure_offers_available_recovery(self):
        identity = self.plan(secrets_manifest=str(self.secret_manifest()))
        with patch.object(O.Operation, "secret_command", side_effect=[True, False]):
            with self.assertRaisesRegex(M.ModeError, "SECRETS_VERIFICATION_FAILED") as failure:
                self.execute(identity)
        self.assertEqual(self.store.read("operations", identity)["phase"], "runtime-ready")
        self.assertNotIn("mode secrets", str(failure.exception))
        self.assertIn("abort", str(failure.exception))
        with patch.object(O.Operation, "secret_command", return_value=True):
            self.assertEqual(self.execute(identity, "resume")["active_mode"], "local-agent")

    def test_changed_secrets_route_fails_before_helper_execution(self):
        path = self.secret_manifest()
        identity = self.plan(secrets_manifest=str(path))
        plan = self.store.read("plans", identity)
        operation = O.Operation(self.store, plan, {"operation_id": identity, "token": "e" * 64})
        path.write_text(path.read_text().replace("file\t.env\t.env", "file\tother.env\t.env"))
        with patch.object(O.subprocess, "run") as run:
            with self.assertRaisesRegex(M.ModeError, "SECRETS_ROUTE_CHANGED"):
                operation.secret_command("verify")
            run.assert_not_called()

    def test_secrets_helper_uses_private_frozen_route_and_expected_origin(self):
        path = self.secret_manifest()
        identity = self.plan(secrets_manifest=str(path))
        plan = self.store.read("plans", identity)
        operation = O.Operation(self.store, plan, {"operation_id": identity, "token": "e" * 64})
        with patch.object(O.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertTrue(operation.secret_command("verify"))
            args, options = run.call_args
            manifest = Path(args[0][2])
            self.assertNotEqual(manifest, path)
            self.assertEqual(sorted(manifest.read_text().splitlines()), sorted(path.read_text().splitlines()))
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(options["env"]["REMOTE_DEV_EXPECTED_CODER_ORIGIN"], "https://coder.example.invalid")
            self.assertEqual(options["env"]["REMOTE_DEV_MODE_TOKEN"], "e" * 64)

    def test_initial_secret_route_declares_runner_writable_paths(self):
        identity = self.plan(secrets_manifest=str(self.secret_manifest()))
        plan = self.store.read("plans", identity)
        self.assertEqual(plan["project"]["remote_writable"], [".env"])
        operation = O.Operation(self.store, plan, {"operation_id": identity, "token": "e" * 64})
        with patch.object(O.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            REAL_LOCAL_AGENT(operation, "up")
        arguments = run.call_args.args[0]
        index = arguments.index("--remote-writable")
        self.assertEqual(arguments[index + 1], ".env")

    def test_image_drift_after_plan_blocks_before_runtime_or_capture(self):
        identity = self.plan()
        self.stale_image = True
        with self.assertRaisesRegex(M.ModeError, "IMAGE_UPDATE_REQUIRED"):
            self.execute(identity)
        self.assertTrue(self.runtime["development"])
        self.assertFalse(self.local.exists())

    def test_additive_binding_policy_refuses_before_claims_or_runtime_stop(self):
        identity = self.plan()
        directory = self.homes[None] / "Library/Application Support/remote-dev/local-agent/bindings/by-root" / hashlib.sha256(os.fsencode(self.local)).hexdigest()
        directory.mkdir(parents=True)
        policy = directory / "policy.tsv"
        policy.write_text("allowed_untracked_file\tnew-file.ts\n")
        policy.chmod(0o600)
        values = {"schema": "1", "binding_kind": "single-root", "project_id": "fixture", "profile": "fixture",
                  "coder_origin": "https://coder.example.invalid", "workspace": "runner", "workspace_role": "runner",
                  "local_root": str(self.local), "remote_root": str(self.runner_root),
                  "projection_policy_path": str(policy), "git_metadata_mode": "explicit-input"}
        binding = directory / "binding.tsv"
        binding.write_text("".join(key + "\t" + value + "\n" for key, value in values.items()))
        binding.chmod(0o600)
        with patch.object(O.Operation, "binding", REAL_BINDING):
            with self.assertRaisesRegex(M.ModeError, "BINDING_POLICY_UNSUPPORTED"):
                self.execute(identity)
        self.assertTrue(self.runtime["development"])
        self.assertEqual(self.calls, [])
        self.assertEqual(policy.read_text(), "allowed_untracked_file\tnew-file.ts\n")
        # A supported existing binding remains reusable without a rebind.
        policy.write_text("")
        operation = O.Operation(self.store, self.store.read("plans", identity), {"operation_id": identity, "token": "e" * 64})
        self.assertEqual(REAL_BINDING(operation), values)
        self.assertFalse(any(call[0] in ("lifecycle", "capture") for call in self.calls))

    def test_crash_after_active_record_preserves_new_active_wip_on_resume(self):
        identity = self.plan()
        real_write = self.store.write
        def interrupted(group, key, value):
            real_write(group, key, value)
            if group == "projects":
                raise OSError("crash after CAS")
        with patch.object(self.store, "write", interrupted):
            with self.assertRaisesRegex(OSError, "CAS"):
                self.execute(identity)
        (self.local / "file").write_text("post-activation edit\n")
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual((self.local / "file").read_text(), "post-activation edit\n")

    def test_other_plan_cannot_take_pending_operation(self):
        first = self.plan()
        self.fail_action = "upload"
        with self.assertRaises(M.ModeError):
            self.execute(first)
        with patch.object(M.time, "time", return_value=self.store.read("plans", first)["created_at"] + 1):
            second = self.plan()
        self.assertNotEqual(second, first)
        with self.assertRaisesRegex(M.ModeError, "OPERATION_BUSY"):
            self.execute(second)
        self.assertEqual(self.store.read("pending", "fixture")["operation_id"], first)

    def resume_after_lost_call_response(self, action):
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        original = O.Operation.call
        fired = []
        def interrupted(operation, endpoint, root, current_action, **kwargs):
            result = original(operation, endpoint, root, current_action, **kwargs)
            if current_action == action and not fired:
                fired.append(True)
                raise OSError("lost " + action + " response")
            return result
        with patch.object(O.Operation, "call", interrupted):
            with self.assertRaisesRegex(OSError, "lost " + action):
                self.execute(identity)
        self.assertTrue(fired)
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], initial)
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(self.store.read("pending", "fixture"), {"operation_id": identity, "complete": True})
        for role, root, authority in (("development", self.source, "inactive"),
                                      (None, self.local, "active"),
                                      ("runner", self.runner_root, "active")):
            directory = self.homes[role] / ".local/share/remote-dev/mode-endpoints"
            key = hashlib.sha256(os.fsencode(root)).hexdigest() + ".json"
            self.assertFalse((directory / "claims" / key).exists(), "operation claim remained after recovery")
            self.assertEqual(json.loads((directory / "fences" / key).read_text()), {
                "root": str(root), "project_id": "fixture", "operation_id": identity, "state": authority})

    def test_resume_after_capture_response_loss(self):
        self.resume_after_lost_call_response("capture")

    def test_resume_after_partial_authority_publication(self):
        self.resume_after_lost_call_response("authority")

    def test_resume_after_partial_claim_release(self):
        self.resume_after_lost_call_response("release")

    def resume_after_phase_crash(self, phase):
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        self.crash_phase = phase
        with self.assertRaisesRegex(OSError, "INJECTED_CRASH: " + phase):
            self.execute(identity)
        self.assertIsNone(self.crash_phase)
        self.assertEqual(self.store.read("operations", identity)["phase"], phase)
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], initial)
        # Recovery continues the journalled operation instead of repeating the
        # capture that the crashed attempt had already taken.
        self.assertEqual([call[0] for call in self.calls].count("capture"), 1)
        self.assertEqual(self.store.read("pending", "fixture"), {"operation_id": identity, "complete": True})

    def test_resume_after_crash_before_source_quiesce(self):
        self.resume_after_phase_crash("preparing")

    def test_resume_after_crash_between_restore_and_publish(self):
        self.resume_after_phase_crash("verified")

    def test_resume_after_binding_up_response_loss(self):
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        original = O.Operation.local_agent
        def interrupted(operation, action):
            result = original(operation, action)
            if action == "up":
                raise OSError("lost binding up response")
            return result
        with patch.object(O.Operation, "local_agent", interrupted):
            with self.assertRaisesRegex(OSError, "binding up"):
                self.execute(identity)
        self.assertTrue(self.bound)
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], initial)

    def test_resume_after_binding_down_response_loss(self):
        self.execute(self.plan())
        identity = self.plan("remote")
        initial = M.TRANSFER.inspect(self.local)["snapshot_id"]
        original = O.Operation.local_agent
        def interrupted(operation, action):
            result = original(operation, action)
            if action == "down":
                raise OSError("lost binding down response")
            return result
        with patch.object(O.Operation, "local_agent", interrupted):
            with self.assertRaisesRegex(OSError, "binding down"):
                self.execute(identity)
        self.assertEqual(self.execute(identity, "resume")["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], initial)

    def test_abort_after_start_takes_effect_but_response_is_lost(self):
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        original = O.Operation.lifecycle
        def interrupted(operation, endpoint, root, phase, **kwargs):
            result = original(operation, endpoint, root, phase, **kwargs)
            if phase == "start":
                raise OSError("lost runtime start response")
            return result
        with patch.object(O.Operation, "lifecycle", interrupted):
            with self.assertRaisesRegex(OSError, "runtime start"):
                self.execute(identity)
        self.assertTrue(self.runtime["runner"])
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")
        self.assertFalse(self.runtime["runner"])
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], initial)
        self.assertEqual(self.execute(identity, "abort")["state"], "aborted")

    def test_generation_conflict_preserves_external_authority_and_wip(self):
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        changed = dict(self.store.read("plans", identity)["project"])
        changed.update(generation=17, active_mode="remote", operation_id="f" * 64)
        def concurrent_pointer(operation):
            self.store.write("projects", "fixture", changed)
            return {"url": "http://127.0.0.1:3000", "verified_at": 1}
        with patch.object(O.Operation, "preview", concurrent_pointer):
            with self.assertRaisesRegex(M.ModeError, "GENERATION_CONFLICT"):
                self.execute(identity)
        self.assertEqual(self.store.read("projects", "fixture"), changed)
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], initial)
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], initial)

    def test_mode_abort_stops_partial_detached_compose_and_records_preserved_volume(self):
        adapter_path = ROOT / "tests/fixtures/compose-adapter/lifecycle.py"
        adapter = M.module_from_file("partial_compose_adapter", adapter_path)
        adapter.ROOT = self.runner_root
        adapter.RESOURCE = "fixture"
        identity = self.plan()
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        volume = self.base / "detached-volume"
        volume.write_text("persisted application data")
        items, docker_calls = [], []
        def docker(argv, environment=None, capture=True):
            docker_calls.append(argv)
            if argv[:3] == ["docker", "ps", "-aq"]:
                return "\n".join(item["Id"] for item in items)
            if argv[:2] == ["docker", "inspect"]:
                return json.dumps(items)
            if argv[:2] == ["docker", "stop"]:
                self.assertEqual(argv[2:4], ["--time", "30"])
                self.assertEqual(argv[4:], [item["Id"] for item in items if item["State"]["Running"]])
                for item in items:
                    item["State"]["Running"] = False
                return ""
            self.fail("unexpected Docker mutation: " + argv[0])
        original = O.Operation.lifecycle
        def lifecycle(operation, endpoint, root, phase, **kwargs):
            result = original(operation, endpoint, root, phase, **kwargs)
            if endpoint.role != "runner":
                return result
            if phase == "start":
                items.append({"Id": "d" * 64, "Config": {"Labels": {
                    "com.docker.compose.project": "fixture",
                    "com.docker.compose.project.working_dir": str(self.runner_root),
                    "com.docker.compose.service": "postgres",
                    "io.remote-dev.operation-id": identity,
                    "io.remote-dev.source-snapshot": initial}},
                    "State": {"Running": True},
                    "Mounts": [{"Type": "volume", "Name": "detached-volume"}]})
                self.assertEqual(adapter.status()["state"], "partial")
                raise OSError("start interrupted with detached postgres")
            if phase in ("quiesce", "stop"):
                adapter.stop()
            return adapter.status() if phase == "status" else result
        with patch.object(adapter, "command", docker), patch.object(O.Operation, "lifecycle", lifecycle):
            with self.assertRaisesRegex(OSError, "detached postgres"):
                self.execute(identity)
            self.assertTrue(items[0]["State"]["Running"])
            self.assertEqual(self.execute(identity, "abort")["state"], "aborted")
            self.assertEqual(adapter.status()["state"], "stopped")
        receipt = self.store.read("operations", identity)["candidate_stopped"]
        self.assertEqual(receipt["state"], "stopped")
        self.assertEqual(receipt["preserved_volumes"], ["detached-volume"])
        self.assertFalse(receipt["resources"][0]["running"])
        self.assertEqual(volume.read_text(), "persisted application data")
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], initial)

    def test_concurrent_mode_commands_and_dead_owner_preserve_pending_operation(self):
        first = self.plan()
        with patch.object(M.time, "time", return_value=self.store.read("plans", first)["created_at"] + 1):
            second = self.plan()
        self.assertNotEqual(first, second)
        initial = M.TRANSFER.inspect(self.source)["snapshot_id"]
        ready_read, ready_write = os.pipe()
        hold_read, hold_write = os.pipe()
        original = O.Operation.call
        pid = os.fork()
        if pid == 0:
            os.close(ready_read)
            os.close(hold_write)
            def hold_capture(operation, endpoint, root, action, **kwargs):
                result = original(operation, endpoint, root, action, **kwargs)
                if action == "capture":
                    os.write(ready_write, b"R")
                    os.read(hold_read, 1)
                return result
            try:
                with patch.object(O.Operation, "call", hold_capture):
                    self.execute(first)
            except BaseException:
                os.write(ready_write, b"E")
                os._exit(1)
            os._exit(0)
        os.close(ready_write)
        os.close(hold_read)
        try:
            self.assertTrue(select.select([ready_read], [], [], 30)[0], "owner did not reach capture barrier")
            self.assertEqual(os.read(ready_read, 1), b"R")
            before = len(self.calls)
            for identity, command in ((first, "apply"), (second, "apply"), (first, "resume"), (first, "abort")):
                with self.subTest(command=command, same_plan=identity == first):
                    with self.assertRaisesRegex(M.ModeError, "OPERATION_BUSY"):
                        self.execute(identity, command)
            self.assertEqual(len(self.calls), before, "contenders must not reach endpoints")
        finally:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(ready_read)
            os.close(hold_write)
        # Kernel mutex release after death is not permission to steal durable claims.
        with self.assertRaisesRegex(M.ModeError, "OPERATION_BUSY"):
            self.execute(second)
        self.assertEqual(self.store.read("pending", "fixture")["operation_id"], first)
        self.assertEqual(self.execute(first, "abort")["state"], "aborted")
        self.assertEqual(M.TRANSFER.inspect(self.source)["snapshot_id"], initial)


    def equivalent_checkout(self, destination):
        """A Mac checkout that already carries the exact source state."""
        shutil.copytree(self.source, destination)
        (destination / "node_modules").mkdir()
        (destination / "node_modules/installed").write_text("expensive\n")
        return destination

    def operation_area(self, root, identity):
        key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]
        return root.parent / (".remote-dev-mode-" + identity + "-" + key)

    def test_adopted_destination_is_published_in_place_with_its_loss_preserved(self):
        self.equivalent_checkout(self.local)
        identity = self.plan(adopt_target=True, confirm_adopt_root=str(self.local),
                             accept_untracked_loss=True)
        self.assertEqual(self.execute(identity)["state"], "activated")
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"],
                         M.TRANSFER.inspect(self.source)["snapshot_id"])
        # Publication replaces the whole path, so the accepted loss must be
        # exactly where the adoption said it would be, not simply gone.
        self.assertFalse((self.local / "node_modules").exists())
        previous = self.operation_area(self.local, identity) / "previous"
        self.assertEqual((previous / "node_modules/installed").read_text(), "expensive\n")
        self.assertEqual(self.store.read("projects", "fixture")["local_root"], str(self.local))

    def test_adoption_moves_the_mac_root_and_records_the_superseded_checkout(self):
        main = self.homes[None] / "projects/main-checkout"
        self.equivalent_checkout(main)
        self.store.write("projects", "fixture",
                         {"project_id": "fixture", "profile": "fixture", "coder_url": "https://coder.example.invalid",
                          "local_root": str(self.local), "development_workspace": "development",
                          "runner_workspace": "runner", "remote_project_dir": "fixture",
                          "remote_root": str(self.source), "active_mode": "remote", "generation": 0,
                          "allowed_untracked": []})
        identity = self.plan(local_project=str(main), adopt_target=True,
                             confirm_adopt_root=str(main), accept_untracked_loss=True)
        self.assertEqual(self.execute(identity)["state"], "activated")
        record = self.store.read("projects", "fixture")
        self.assertEqual(record["active_mode"], "local-agent")
        self.assertEqual(record["local_root"], str(main))
        self.assertEqual(record["superseded_local_roots"], [str(self.local)])
        self.assertEqual(M.TRANSFER.inspect(main)["snapshot_id"], M.TRANSFER.inspect(self.source)["snapshot_id"])
        # No second Mac copy is created for the project that already had one.
        self.assertFalse(self.local.exists())
        self.assertEqual(self.store.superseded_owner(str(self.local))["project_id"], "fixture")

    def test_activation_discards_transport_archives_and_keeps_what_it_replaced(self):
        self.equivalent_checkout(self.local)
        identity = self.plan(adopt_target=True, confirm_adopt_root=str(self.local), accept_untracked_loss=True)
        self.assertEqual(self.execute(identity)["state"], "activated")
        for root in (self.local, self.source):
            area = self.operation_area(root, identity)
            self.assertEqual([name for name in ("source.zip", "incoming.zip") if (area / name).exists()], [])
        self.assertEqual((self.operation_area(self.local, identity) / "previous/node_modules/installed").read_text(),
                         "expensive\n")

    def test_forget_measures_a_retained_area_before_discarding_it(self):
        self.equivalent_checkout(self.local)
        identity = self.plan(adopt_target=True, confirm_adopt_root=str(self.local), accept_untracked_loss=True)
        self.assertEqual(self.execute(identity)["state"], "activated")
        areas = [self.operation_area(root, identity) for root in (self.local, self.source, self.runner_root)]
        measured = self.execute(identity, "forget")
        self.assertFalse(measured["discarded"])
        self.assertGreater(measured["retained_bytes"], 0)
        self.assertIn("previous", [hold for area in measured["areas"] for hold in area.get("holds", [])])
        self.assertIn("--confirm-discard-previous", measured["confirm_command"])
        self.assertTrue(all(area.exists() for area in areas), "measuring must not remove anything")
        discarded = self.execute(identity, "forget", confirm_discard_previous=True)
        self.assertTrue(discarded["discarded"])
        self.assertFalse(any(area.exists() for area in areas))
        self.assertTrue(self.store.read("operations", identity)["areas_discarded"])
        # The published source authority itself is never part of the discard.
        self.assertEqual(M.TRANSFER.inspect(self.local)["snapshot_id"], M.TRANSFER.inspect(self.source)["snapshot_id"])

    def test_forget_refuses_an_operation_that_has_not_finished(self):
        self.equivalent_checkout(self.local)
        identity = self.plan(adopt_target=True, confirm_adopt_root=str(self.local), accept_untracked_loss=True)
        self.fail_action = "capture"
        with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
            self.execute(identity)
        self.assertEqual(self.store.read("operations", identity)["state"], "failed")
        with self.assertRaisesRegex(M.ModeError, "OPERATION_UNFINISHED"):
            self.execute(identity, "forget")
        self.assertTrue(self.operation_area(self.source, identity).exists())


if __name__ == "__main__":
    unittest.main()
