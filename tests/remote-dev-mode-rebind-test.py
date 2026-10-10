#!/usr/bin/env python3
"""Re-anchoring an active binding, with real Git, state files and endpoints.

The Local Agent CLI itself is simulated, but every state change it would make
is performed, so the orchestration, the runner rebind and the refusals are
exercised against the real records rather than against assertions about them.
"""
import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("mode", str(ROOT / "bin/remote-dev-mode"))
M = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(M)
O = M.module_from_file("operation", ROOT / "bin/remote-dev-mode-operation.py")
O.MODE = M

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

PROFILE, WORKSPACE, PROJECT = "fixture", "runner", "fixture"


class RebindTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.mac, self.runner_home = self.base / "mac", self.base / "runner"
        (self.mac / "projects").mkdir(parents=True)
        (self.runner_home / "projects").mkdir(parents=True)
        marker = self.runner_home / ".local/share/remote-dev/workspace-role"
        marker.parent.mkdir(parents=True)
        marker.write_text("runner\n")
        self.agent_state = self.base / "local-agent"
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.mac), "REMOTE_DEV_MODE_STATE_ROOT": str(self.base / "state"),
            "REMOTE_DEV_LOCAL_AGENT_STATE_ROOT": str(self.agent_state),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        self.environment.start()

        self.previous = self.checkout(self.mac / "projects/duplicate")
        self.requested = self.mac / "projects/main"
        shutil.copytree(self.previous, self.requested)
        (self.requested / "node_modules").mkdir()
        (self.requested / "node_modules/installed").write_text("expensive\n")

        self.runner_root = "/home/coder/projects/" + PROJECT
        self.remote_root = self.runner_home / "projects" / PROJECT
        self.control = self.runner_home / ".local/share/remote-dev/local-agent" / PROJECT
        fixture = self

        class Endpoint:
            def __init__(self, profile=None, origin=None, workspace=None, role=None):
                self.profile, self.origin, self.workspace, self.role = profile, origin, workspace, role

            def resolve(self, root):
                return str(fixture.remote_root) if self.role == "runner" else root

            def inspect(self, root, allowed=()):
                with patch.dict(os.environ, {"HOME": str(fixture.runner_home if self.role else fixture.mac)}):
                    return M.inspect_checkout(self.resolve(root), self.role, allowed)

            def request(self, action, payload, input_path=None, output_path=None):
                fixture.calls.append((action, self.role))
                if fixture.fail_action == action:
                    fixture.fail_action = None
                    raise M.ModeError("INJECTED_FAILURE: " + action)
                home = fixture.runner_home if self.role else fixture.mac
                with patch.dict(os.environ, {"HOME": str(home)}):
                    request = {**payload, "root": self.resolve(payload["root"]), "role": self.role}
                    return M.ENDPOINT.Transaction(request).handle(action)

        self.calls, self.fail_action = [], None
        self.patches = [patch.object(M, "Endpoint", Endpoint),
                        patch.object(O, "run_local_agent", self.local_agent)]
        for item in self.patches:
            item.start()
        self.store = M.Store()
        self.bind_runner(self.previous)
        self.bind_mac(self.previous)
        self.project_record()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.environment.stop()
        self.temporary.cleanup()

    # Fixtures -------------------------------------------------------------

    def checkout(self, root, origin="https://example.invalid/fixture.git"):
        root.mkdir(parents=True)
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        (root / ".remote-dev").mkdir()
        (root / ".remote-dev/project.env").write_text(CONTRACT)
        (root / "file").write_text("initial\n")
        self.git(root, "remote", "add", "origin", origin)
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "fixture")
        return root

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout

    def digest(self, root):
        return hashlib.sha256(os.fsencode(str(root))).hexdigest()

    def binding_dir(self, root):
        return self.agent_state / "bindings/by-root" / self.digest(root)

    def bind_mac(self, root):
        # `up` is repeatable against an existing binding, as it is on resume.
        directory = self.binding_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        policy = directory / "policy.tsv"
        policy.write_text("")
        policy.chmod(0o600)
        fields = {"schema": "1", "binding_kind": "single-root", "project_id": PROJECT, "workspace": WORKSPACE,
                  "workspace_role": "runner", "profile": PROFILE, "coder_origin": "https://coder.example.invalid",
                  "ssh_alias": PROFILE + "." + WORKSPACE, "local_root": str(root), "remote_root": self.runner_root,
                  "root_hash": self.digest(root), "mutagen_session": "session", "owner_id": "owner",
                  "git_metadata_mode": "explicit-input", "projection_policy_path": str(policy),
                  "current_receipt_path": str(directory / "current_receipt.tsv")}
        binding = directory / "binding.tsv"
        binding.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()))
        binding.chmod(0o600)
        for claim in (self.agent_state / "bindings/by-context" / (self.digest(root) + ".claim"),
                      self.agent_state / "bindings/by-runner" / PROFILE / (WORKSPACE + ".claim")):
            claim.parent.mkdir(parents=True, exist_ok=True)
            claim.write_text(str(directory) + "\n")
            claim.chmod(0o600)
        self.project_runner(root)

    def project_runner(self, root):
        """Mirror the projection the runner would hold for this local root."""
        if self.remote_root.exists():
            shutil.rmtree(self.remote_root)
        self.remote_root.mkdir(parents=True)
        for entry in M.TRANSFER.inspect(root)["worktree"]:
            source = Path(root) / entry["path"]
            if source.is_file():
                target = self.remote_root / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def bind_runner(self, root):
        self.control.mkdir(parents=True, exist_ok=True)
        fields = {"schema": "1", "project_id": PROJECT, "profile": PROFILE, "workspace": WORKSPACE,
                  "workspace_role": "runner", "root_hash": self.digest(root), "remote_root": self.runner_root}
        binding = self.control / "binding.tsv"
        binding.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()))
        binding.chmod(0o600)

    def project_record(self, **overrides):
        record = {"project_id": PROJECT, "profile": PROFILE, "coder_url": "https://coder.example.invalid",
                  "local_root": str(self.previous), "development_workspace": "development",
                  "runner_workspace": WORKSPACE, "remote_project_dir": PROJECT,
                  "remote_root": "/home/coder/projects/" + PROJECT, "active_mode": "local-agent",
                  "generation": 2, "allowed_untracked": [],
                  "departures": {"local-agent": {"tree": {"exists": True}, "snapshot": "stale"}}, **overrides}
        self.store.write("projects", PROJECT, record)
        return record

    def local_agent(self, project, action, local_root, operation_id, token):
        """Perform the state changes the Local Agent CLI would make."""
        self.calls.append(("local-agent-" + action, local_root))
        if action == "retire":
            directory = self.binding_dir(local_root)
            for claim in (self.agent_state / "bindings/by-context" / (self.digest(local_root) + ".claim"),
                          self.agent_state / "bindings/by-runner" / PROFILE / (WORKSPACE + ".claim")):
                claim.unlink(missing_ok=True)
            retired = self.agent_state / "retired" / self.digest(local_root)[:16]
            retired.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(directory), str(retired))
        elif action == "up":
            self.bind_mac(Path(local_root))
        return None

    def rebind(self, to_root=None, **options):
        args = argparse.Namespace(for_context=str(self.previous), to_root=str(to_root or self.requested),
                                  confirm_root=str(to_root or self.requested), accept_source_change=False,
                                  writers_paused=True)
        vars(args).update(options)
        return M.rebind(args, self.store, O)

    def runner_hash(self):
        return dict(line.split("\t", 1) for line in
                    (self.control / "binding.tsv").read_text().splitlines())["root_hash"]

    def fence(self, root):
        key = hashlib.sha256(os.fsencode(str(root))).hexdigest()
        path = self.mac / ".local/share/remote-dev/mode-endpoints/fences" / (key + ".json")
        return json.loads(path.read_text())["state"] if path.exists() else None

    # Tests ----------------------------------------------------------------

    def test_rebind_moves_the_anchor_without_transferring_anything(self):
        result = self.rebind()
        self.assertEqual(result["state"], "activated")
        self.assertEqual(result["local_root"], str(self.requested))
        self.assertEqual(result["superseded_local_root"], str(self.previous))
        record = self.store.read("projects", PROJECT)
        self.assertEqual(record["local_root"], str(self.requested))
        self.assertEqual(record["generation"], 3)
        self.assertEqual(record["superseded_local_roots"], [str(self.previous)])
        # The receipt described the previous root and cannot authorize the new one.
        self.assertNotIn("local-agent", record["departures"])
        self.assertEqual(self.runner_hash(), self.digest(self.requested))
        self.assertTrue(self.binding_dir(self.requested).is_dir())
        self.assertFalse(self.binding_dir(self.previous).exists())
        self.assertEqual(self.fence(self.previous), "inactive")
        self.assertEqual(self.fence(self.requested), "active")
        self.assertTrue(self.store.read("pending", PROJECT)["complete"])
        # Nothing was captured, published or removed at either checkout.
        self.assertEqual((self.requested / "node_modules/installed").read_text(), "expensive\n")
        self.assertTrue((self.previous / "file").exists())
        self.assertEqual([call for call in self.calls if call[0].startswith("local-agent")],
                         [("local-agent-retire", str(self.previous)), ("local-agent-up", str(self.requested))])

    def test_rebind_leaves_no_endpoint_area_and_keeps_status_readable(self):
        self.rebind()
        for root in (self.previous, self.requested, self.remote_root):
            key = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]
            areas = list(Path(root).parent.glob(".remote-dev-mode-*-" + key))
            self.assertEqual(areas, [], "re-anchoring publishes nothing and keeps no recovery copy")
        # A re-anchor journal has no reviewed plan; status must still read it.
        result = subprocess.run([str(ROOT / "bin/remote-dev"), "mode", "status",
                                 "--for-context", str(self.requested)],
                                env={**os.environ, "HOME": str(self.mac)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["local_root"], str(self.requested))
        self.assertEqual(status["retained_operation_areas"], [])

    def test_a_rebind_journal_is_not_resumed_through_the_transfer_commands(self):
        self.fail_action = "projection"
        with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
            self.rebind()
        identity = next(path.stem for path in (self.store.root / "operations").glob("*.json"))
        for command in ("resume", "abort", "apply"):
            args = argparse.Namespace(command=command, identity=identity, writers_paused=True)
            with self.assertRaisesRegex(M.ModeError, "REBIND_OPERATION|PLAN_MISSING"):
                O.execute(args, self.store)
        self.assertEqual(self.runner_hash(), self.digest(self.requested))

    def test_rebind_accepts_the_same_repository_reached_over_another_transport(self):
        self.git(self.requested, "remote", "set-url", "origin", "git@example.invalid:fixture.git")
        result = self.rebind()
        self.assertEqual(result["state"], "activated")
        self.assertEqual(self.runner_hash(), self.digest(self.requested))

    def test_rebind_refuses_a_different_repository(self):
        other = self.checkout(self.mac / "projects/other", origin="https://example.invalid/other.git")
        with self.assertRaisesRegex(M.ModeError, "REBIND_REPOSITORY_MISMATCH"):
            self.rebind(to_root=other)
        self.assertEqual(self.runner_hash(), self.digest(self.previous))
        self.assertEqual(self.store.read("projects", PROJECT)["local_root"], str(self.previous))

    def test_rebind_refuses_a_changed_source_until_it_is_accepted(self):
        (self.requested / "file").write_text("local work\n")
        with self.assertRaisesRegex(M.ModeError, "REBIND_SOURCE_CHANGED"):
            self.rebind()
        self.assertEqual(self.runner_hash(), self.digest(self.previous))
        self.assertEqual((self.requested / "file").read_text(), "local work\n")
        result = self.rebind(accept_source_change=True)
        self.assertEqual(result["state"], "activated")
        self.assertEqual(self.runner_hash(), self.digest(self.requested))
        self.assertEqual((self.requested / "file").read_text(), "local work\n")

    def test_rebind_requires_the_exact_confirmation_and_paused_writers(self):
        with self.assertRaisesRegex(M.ModeError, "REBIND_CONFIRMATION_REQUIRED"):
            self.rebind(confirm_root=str(self.requested) + "-other")
        with self.assertRaisesRegex(M.ModeError, "WRITERS_PAUSE_REQUIRED"):
            self.rebind(writers_paused=False)
        with self.assertRaisesRegex(M.ModeError, "ALREADY_ACTIVE"):
            self.rebind(to_root=self.previous, for_context=str(self.previous))
        self.assertEqual(self.runner_hash(), self.digest(self.previous))

    def test_rebind_is_not_offered_for_a_remote_source_authority(self):
        self.project_record(active_mode="remote")
        with self.assertRaisesRegex(M.ModeError, "REBIND_MODE_UNSUPPORTED"):
            self.rebind()
        self.assertEqual(self.runner_hash(), self.digest(self.previous))

    def test_rebind_resumes_the_same_operation_after_an_interrupted_step(self):
        self.fail_action = "projection"
        with self.assertRaisesRegex(M.ModeError, "INJECTED_FAILURE"):
            self.rebind()
        journal = [self.store.read("operations", path.stem) for path in
                   (self.store.root / "operations").glob("*.json")]
        self.assertEqual([item["state"] for item in journal], ["failed"])
        self.assertEqual(journal[0]["phase"], "anchored")
        self.assertEqual(self.runner_hash(), self.digest(self.requested))
        result = self.rebind()
        self.assertEqual(result["state"], "activated")
        self.assertEqual(result["operation_id"], journal[0]["operation_id"])
        # The runner rebind is idempotent; resuming does not rewrite it twice.
        self.assertEqual(self.runner_hash(), self.digest(self.requested))
        self.assertEqual(self.store.read("projects", PROJECT)["generation"], 3)

    def test_runner_rebind_refuses_a_runner_that_still_holds_its_lease(self):
        lease = self.control / "lease.tsv"
        lease.write_text("schema\t1\nowner_id\towner\n")
        lease.chmod(0o600)
        request = {"root": str(self.remote_root), "operation_id": "a" * 64, "token": "b" * 64,
                   "project_id": PROJECT, "role": "runner", "allowed": [], "root_hash": self.digest(self.requested),
                   "binding": {"project_id": PROJECT, "profile": PROFILE, "workspace": WORKSPACE,
                               "root_hash": self.digest(self.previous), "remote_root": self.runner_root}}
        with patch.dict(os.environ, {"HOME": str(self.runner_home)}):
            transaction = M.ENDPOINT.Transaction(request)
            transaction.handle("claim")
            with self.assertRaisesRegex(M.ENDPOINT.EndpointError, "RUNNER_NOT_RELEASED"):
                transaction.handle("runner-rebind")
        self.assertEqual(self.runner_hash(), self.digest(self.previous))


if __name__ == "__main__":
    unittest.main()
