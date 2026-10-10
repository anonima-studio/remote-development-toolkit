#!/usr/bin/env python3
"""CLI inspection with real Git and a Coder transport simulator, never a live host."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
# Synthetic host profiles; tests never read the deployment profiles.
os.environ["REMOTE_DEV_PROFILE_DIR"] = str(REPOSITORY / "tests/fixtures/host-profiles")
CLI = REPOSITORY / "bin/remote-dev"
MODE = REPOSITORY / "bin/remote-dev-mode"

CONTRACT = """version=2
setup_command=touch should-not-run
start_command=touch should-not-run
quiesce_command=touch should-not-run
stop_command=touch should-not-run
status_command=touch should-not-run
health_url=http://127.0.0.1:3000/health
browser_port=3000
resource_id=fixture
"""


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="remote-dev-mode-test-")
        self.base = Path(self.temporary.name).resolve()
        self.remote_home = self.base / "remote-home"
        self.source = self.remote_home / "projects/fixture"
        self.source.mkdir(parents=True)
        marker = self.remote_home / ".local/share/remote-dev/workspace-role"
        marker.parent.mkdir(parents=True)
        marker.write_text("development\n")
        self.runner_home = self.base / "runner-home"
        marker = self.runner_home / ".local/share/remote-dev/workspace-role"
        marker.parent.mkdir(parents=True)
        marker.write_text("runner\n")
        (self.runner_home / "projects").mkdir()
        for home in (self.remote_home, self.runner_home):
            (home / "image-bin").mkdir()
            for name, filename in (("remote-dev-mode-guard", "mode-guard.py"), ("remote-dev-project-contract", "project-contract.py"),
                                   ("remote-dev-codex", "codex-start.sh"), ("remote-dev-project-setup", "project-setup.sh")):
                (home / "image-bin" / name).write_bytes((REPOSITORY / "templates/docker" / filename).read_bytes())
        self.local = self.base / "local-project"
        self.state = self.base / "mode-state"
        self.fake = self.base / "bin"
        self.fake.mkdir()
        self.log = self.base / "commands.jsonl"
        script = self.fake / "coder"
        script.write_text("#!/usr/bin/env python3\n" + r'''
import json,os,subprocess,sys
from pathlib import Path
a=sys.argv[1:]
action='ssh' if 'ssh' in a else 'list' if 'list' in a else 'unknown'
with open(os.environ['MODE_TEST_LOG'],'a') as s:s.write(json.dumps({'action':action})+'\n')
if action=='ssh':
 i=a.index('ssh'); workspace=a[i+3]
 assert a[i+1:i+3]==['--disable-autostart','--wait=no']
 if os.environ.get('MODE_TEST_STOPPED')==workspace:
  print('workspace is stopped',file=sys.stderr);sys.exit(1)
 e=os.environ.copy();e['HOME']=os.environ['MODE_TEST_REMOTE_HOME' if workspace=='fixture' else 'MODE_TEST_RUNNER_HOME']
 payload=json.load(sys.stdin)
 payload['code']=payload['code'].replace('/usr/local/bin',str(Path(e['HOME'])/'image-bin'))
 sys.exit(subprocess.run(['bash','-c',a[i+5]],input=json.dumps(payload).encode(),env=e).returncode)
if action=='list':
 print(json.dumps([{'name':'fixture','latest_build':{'status':'stopped'}}]));sys.exit(0)
sys.exit(2)
''')
        script.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.fake) + os.pathsep + os.environ["PATH"],
                    "REMOTE_DEV_MODE_STATE_ROOT": str(self.state), "MODE_TEST_LOG": str(self.log),
                    "MODE_TEST_REMOTE_HOME": str(self.remote_home), "MODE_TEST_RUNNER_HOME": str(self.runner_home),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.source / ".remote-dev").mkdir()
        (self.source / ".remote-dev/project.env").write_text(CONTRACT)
        (self.source / "file").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.source), *args], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result.stdout

    def invoke(self, *args, error=None):
        result = subprocess.run([str(CLI), *args], env=self.env, capture_output=True, text=True)
        if error:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error, result.stderr)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def plan(self, *extra, error=None):
        return self.invoke("--profile", "example-profile", "mode", "plan", "--project-id", "fixture",
                           "--from", "remote", "--to", "local-agent", "--local-project", str(self.local),
                           "--development-workspace", "fixture", "--runner-workspace", "fixture-runner",
                           "--remote-project-dir", "fixture", *extra, error=error)

    def test_status_does_not_create_state_or_infer_source(self):
        status = self.invoke("mode", "status", "--for-context", str(self.local))
        self.assertEqual(status["state"], "unbound")
        self.assertFalse(self.state.exists())
        self.assertFalse(self.log.exists())

    def test_plan_freezes_final_secret_route_without_reading_secret_values(self):
        manifest = self.base / "secrets.tsv"
        fields = {"manifest_version": "2", "project_id": "fixture", "workspace": "fixture-runner", "workspace_role": "runner",
                  "local_root": str(self.local), "remote_root": str(self.runner_home / "projects/fixture")}
        manifest.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()) + "file\t.env\t.env\n")
        manifest.chmod(0o600)
        result = self.plan("--data-policy", "isolated", "--secrets-manifest", str(manifest))
        self.assertEqual(result["state"], "planned")
        reviewed = json.loads(Path(result["plan_file"]).read_text())
        self.assertEqual(reviewed["secrets_route"]["identity"], fields)
        self.assertEqual(reviewed["secrets_route"]["files"], [[".env", ".env"]])
        self.assertEqual(reviewed["project"]["remote_writable"], [".env"])
        self.assertFalse(self.local.exists())
        manifest.write_text(manifest.read_text().replace("workspace\tfixture-runner", "workspace\tforeign"))
        self.plan("--data-policy", "isolated", "--secrets-manifest", str(manifest), error="SECRETS_ROUTE_MISMATCH")
        projects = self.state / "projects"
        projects.mkdir(mode=0o700)
        record = projects / "fixture.json"
        record.write_text(json.dumps(reviewed["project"]))
        record.chmod(0o600)
        previous = record.read_bytes()
        manifest.write_text("".join(key + "\t" + value + "\n" for key, value in fields.items()) + "file\t.env.second\t.env.second\n")
        result = self.invoke("mode", "plan", "--for-context", str(self.local), "--to", "local-agent",
                             "--data-policy", "isolated", "--secrets-manifest", str(manifest))
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("RUNNER_POLICY_CHANGE_REQUIRED" in item for item in result["blockers"]))
        self.assertEqual(record.read_bytes(), previous)

    def test_secrets_owner_rejects_changed_profile_origin_before_remote_access(self):
        environment = {**self.env, "REMOTE_DEV_PROFILE": "example-profile", "REMOTE_DEV_EXPECTED_CODER_ORIGIN": "https://foreign.invalid"}
        result = subprocess.run([str(REPOSITORY / "bin/remote-dev-secrets"), "verify", str(self.base / "absent")],
                                env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Coder origin differs", result.stderr)
        self.assertFalse(self.log.exists())

    def test_plan_captures_dirty_source_without_running_project_or_creating_target(self):
        (self.source / "file").write_text("staged\n")
        self.git("add", "file")
        (self.source / "file").write_text("unstaged\n")
        (self.source / "new").write_text("untracked\n")
        before = self.git("status", "--porcelain=v1", "-z")
        result = self.plan("--allow-untracked", "new", "--data-policy", "isolated")
        self.assertEqual(result["state"], "planned")
        self.assertEqual(result["selected_untracked"], ["new"])
        self.assertEqual(self.git("status", "--porcelain=v1", "-z"), before)
        self.assertFalse(self.local.exists())
        self.assertFalse((self.source / "should-not-run").exists())
        stored = Path(result["plan_file"])
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual({json.loads(line)["action"] for line in self.log.read_text().splitlines()}, {"ssh"})

    def test_nonempty_unmanaged_target_is_a_plan_blocker(self):
        self.local.mkdir()
        # A second valid checkout is enough to show why presence is not authority.
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.local)], env=self.env, check=True)
        result = self.plan("--data-policy", "isolated")
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("TARGET_NOT_MANAGED" in item for item in result["blockers"]))

    def test_missing_lifecycle_does_not_silently_skip_runtime(self):
        (self.source / ".remote-dev/project.env").write_text(CONTRACT.replace("version=2", "version=1").split("quiesce_command")[0])
        result = self.plan()
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("CONTRACT_INVALID" in item for item in result["blockers"]))
        self.assertTrue(any("DATA_POLICY_REQUIRED" in item for item in result["blockers"]))

    def test_old_image_and_uncaptured_contract_block_before_any_hook(self):
        (self.runner_home / "image-bin/remote-dev-mode-guard").unlink()
        result = self.plan("--data-policy", "isolated")
        self.assertTrue(any("IMAGE_UPDATE_REQUIRED" in item for item in result["blockers"]))
        self.git("rm", "--cached", ".remote-dev/project.env")
        self.git("commit", "-qm", "remove tracked contract")
        result = self.plan("--data-policy", "isolated")
        self.assertTrue(any("CONTRACT_NOT_CAPTURED" in item for item in result["blockers"]))
        self.assertFalse((self.source / "should-not-run").exists())

    def test_reviewed_timeout_is_bounded_and_persisted(self):
        result = self.plan("--data-policy", "isolated", "--lifecycle-timeout-seconds", "1800")
        plan = json.loads(Path(result["plan_file"]).read_text())
        self.assertEqual(plan["project"]["lifecycle_timeout_seconds"], 1800)
        self.plan("--lifecycle-timeout-seconds", "10000", error="INVALID_TIMEOUT")

    def test_stopped_workspace_is_observed_without_start(self):
        self.env["MODE_TEST_STOPPED"] = "fixture"
        self.plan(error="WORKSPACE_STOPPED")
        self.assertEqual([json.loads(line)["action"] for line in self.log.read_text().splitlines()], ["ssh", "list"])
        self.assertFalse(self.local.exists())
        self.assertFalse(self.state.exists())

    def test_role_mismatch_does_not_convert_workspace(self):
        marker = self.remote_home / ".local/share/remote-dev/workspace-role"
        marker.write_text("runner\n")
        self.plan(error="WORKSPACE_ROLE_MISMATCH")
        self.assertEqual(marker.read_text(), "runner\n")
        self.assertFalse(self.state.exists())

    def equivalent_target(self, destination=None):
        """A destination that already holds the exact source commit and content.

        Removing `origin` is what makes it equivalent rather than a second
        clone: a clone's own remote is part of the checkout that publication
        would replace, so adoption must see it as a repository difference.
        """
        destination = destination or self.local
        subprocess.run(["git", "clone", "-q", str(self.source), str(destination)], env=self.env, check=True)
        for argument in (("remote", "remove", "origin"), ("config", "user.name", "Fixture"),
                         ("config", "user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(destination), *argument], env=self.env, check=True)
        return destination

    def adopt(self, *extra, error=None):
        return self.plan("--data-policy", "isolated", "--adopt-target",
                         "--confirm-adopt-root", str(self.local), *extra, error=error)

    def test_adoption_replaces_the_departure_receipt_for_an_equivalent_destination(self):
        self.equivalent_target()
        blocked = self.plan("--data-policy", "isolated")
        self.assertTrue(any("TARGET_NOT_MANAGED" in item for item in blocked["blockers"]))
        result = self.adopt()
        self.assertEqual(result["state"], "planned", result["blockers"])
        self.assertEqual(result["adoption"]["root"], str(self.local))
        self.assertEqual(result["adoption"]["dropped"], {"untracked": 0, "ignored": 0, "refs": 0})
        self.assertEqual(result["adoption"]["accepted"], [])

    def test_adoption_refuses_a_destination_that_is_not_the_same_repository(self):
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.local)], env=self.env, check=True)
        result = self.adopt()
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("ADOPT_REPOSITORY_MISMATCH" in item for item in result["blockers"]))

    def test_adoption_refuses_a_destination_holding_unique_uncommitted_work(self):
        self.equivalent_target()
        (self.local / "file").write_text("local work\n")
        result = self.adopt()
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("ADOPT_WORKTREE_DIVERGED" in item for item in result["blockers"]))
        # The refusal must not have touched the work it is protecting.
        self.assertEqual((self.local / "file").read_text(), "local work\n")

    def test_adoption_refuses_a_destination_on_a_different_commit(self):
        self.equivalent_target()
        (self.source / "file").write_text("advanced\n")
        self.git("commit", "-qam", "Advance")
        result = self.adopt()
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(any("ADOPT_REPOSITORY_MISMATCH" in item for item in result["blockers"]))

    def test_adoption_names_destination_only_data_before_it_can_be_accepted(self):
        (self.source / ".gitignore").write_text("ignored/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "Ignore")
        self.equivalent_target()
        (self.local / "stray.txt").write_text("not in the source\n")
        (self.local / "ignored").mkdir()
        (self.local / "ignored/data.bin").write_text("expensive\n")
        subprocess.run(["git", "-C", str(self.local), "branch", "extra"], env=self.env, check=True)
        result = self.adopt()
        self.assertEqual(result["state"], "blocked")
        codes = {item.split(":")[0] for item in result["blockers"]}
        self.assertEqual(codes & {"ADOPT_UNTRACKED_PRESENT", "ADOPT_IGNORED_PRESENT", "ADOPT_REFS_PRESENT"},
                         {"ADOPT_UNTRACKED_PRESENT", "ADOPT_IGNORED_PRESENT", "ADOPT_REFS_PRESENT"})
        accepted = self.adopt("--accept-untracked-loss", "--accept-ignored-loss", "--accept-ref-loss")
        self.assertEqual(accepted["state"], "planned", accepted["blockers"])
        self.assertEqual(accepted["adoption"]["dropped"], {"untracked": 1, "ignored": 1, "refs": 1})
        reviewed = json.loads(Path(accepted["plan_file"]).read_text())
        self.assertEqual(reviewed["adoption"]["dropped"],
                         {"untracked": ["stray.txt"], "ignored": ["ignored/"], "refs": ["refs/heads/extra"]})
        # Naming the loss is not performing it: the plan never writes there.
        self.assertEqual((self.local / "ignored/data.bin").read_text(), "expensive\n")

    def test_adoption_requires_the_exact_destination_in_its_confirmation(self):
        self.equivalent_target()
        self.plan("--data-policy", "isolated", "--adopt-target",
                  "--confirm-adopt-root", str(self.local) + "-other", error="ADOPT_CONFIRMATION_REQUIRED")
        self.plan("--data-policy", "isolated", "--adopt-target", error="ADOPT_CONFIRMATION_REQUIRED")

    def test_adoption_refuses_an_empty_destination_that_needs_no_proof(self):
        self.local.mkdir()
        self.adopt(error="ADOPT_TARGET_EMPTY")

    def bind(self, **overrides):
        record = {"project_id": "fixture", "profile": "example-profile", "coder_url": "https://coder.example.invalid",
                  "local_root": str(self.local), "development_workspace": "fixture", "runner_workspace": "fixture-runner",
                  "remote_project_dir": "fixture", "active_mode": "remote", "generation": 0, **overrides}
        projects = self.state / "projects"
        projects.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = projects / "fixture.json"
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return record

    def test_bound_project_moves_its_mac_root_only_through_an_adoption(self):
        main = self.base / "main-checkout"
        self.equivalent_target(main)
        self.local.mkdir()
        self.bind()
        self.invoke("mode", "plan", "--for-context", str(self.local), "--to", "local-agent",
                    "--data-policy", "isolated", "--local-project", str(main),
                    error="LOCAL_ROOT_IMMUTABLE")
        result = self.invoke("mode", "plan", "--for-context", str(self.local), "--to", "local-agent",
                             "--data-policy", "isolated", "--local-project", str(main),
                             "--adopt-target", "--confirm-adopt-root", str(main))
        self.assertEqual(result["state"], "planned", result["blockers"])
        self.assertEqual(result["target_root"], str(main))
        reviewed = json.loads(Path(result["plan_file"]).read_text())
        self.assertEqual(reviewed["project"]["local_root"], str(main))
        self.assertEqual(reviewed["project"]["superseded_local_roots"], [str(self.local)])

    def test_status_sends_a_superseded_mac_root_to_the_current_authority(self):
        main = self.base / "main-checkout"
        main.mkdir()
        self.bind(local_root=str(main), superseded_local_roots=[str(self.local)])
        self.local.mkdir()
        status = self.invoke("mode", "status", "--for-context", str(self.local))
        self.assertEqual(status["state"], "superseded")
        self.assertEqual(status["local_root"], str(main))


if __name__ == "__main__":
    unittest.main()
