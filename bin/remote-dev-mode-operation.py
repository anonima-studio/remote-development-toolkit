#!/usr/bin/env python3
"""Durable, explicitly addressed mode handoff. Dependencies supplied by CLI."""

import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


PHASES = ["planned", "preparing", "quiesced", "captured", "transferred", "verified",
          "published-inactive", "runtime-ready", "activated"]


def fail(code, text):
    raise MODE.ModeError(code + ": " + text)


def fingerprint(info):
    return {"tree": info["tree"], "snapshot": info.get("snapshot", {}).get("snapshot_id")}


def root_hash(local_root):
    return hashlib.sha256(os.fsencode(local_root)).hexdigest()


def binding_directory(local_root):
    directory = Path(os.environ.get("REMOTE_DEV_LOCAL_AGENT_STATE_ROOT",
                                    str(Path.home() / "Library/Application Support/remote-dev/local-agent")))
    return directory / "bindings/by-root" / root_hash(local_root)


def read_binding(project, runner_root, local_root):
    directory = Path(os.environ.get("REMOTE_DEV_LOCAL_AGENT_STATE_ROOT", str(Path.home() / "Library/Application Support/remote-dev/local-agent")))
    file = directory / "bindings/by-root" / hashlib.sha256(os.fsencode(local_root)).hexdigest() / "binding.tsv"
    if not file.exists():
        return None
    if file.resolve() != file or file.stat().st_uid != os.getuid() or file.stat().st_mode & 0o077:
        fail("BINDING_UNTRUSTED", "Local Agent binding file identity or permissions differ")
    values = {}
    for line in file.read_text().splitlines():
        key, value = line.split("\t", 1)
        if key in values:
            fail("BINDING_UNTRUSTED", "duplicate Local Agent binding field")
        values[key] = value
    expected = {"schema": "1", "binding_kind": "single-root", "project_id": project["project_id"],
                "profile": project["profile"], "coder_origin": project["coder_url"],
                "workspace": project["runner_workspace"], "workspace_role": "runner",
                "local_root": local_root, "remote_root": runner_root}
    if any(values.get(key) != value for key, value in expected.items()):
        fail("BINDING_IDENTITY_MISMATCH", "existing binding does not match the stable reviewed endpoints")
    policy = Path(values.get("projection_policy_path", ""))
    if policy != file.parent / "policy.tsv" or policy.resolve() != policy or not policy.is_file():
        fail("BINDING_UNTRUSTED", "projection policy is not the binding-owned regular file")
    info = policy.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail("BINDING_UNTRUSTED", "projection policy ownership or permissions differ")
    expected_policy = sorted({"allowed_untracked\t" + path for path in project["allowed_untracked"]} |
                             {"remote_writable\t" + path for path in project.get("remote_writable", [])})
    actual_policy = policy.read_text().splitlines()
    if actual_policy != expected_policy or values.get("git_metadata_mode") != "explicit-input":
        fail("BINDING_POLICY_UNSUPPORTED", "existing Local Agent policy differs from this mode plan; preserve the binding and source runtime. Exact-file additive grants and tracked exclusions require mode policy integration before switching; do not remove grants or rebind to bypass this check")
    return values


def run_local_agent(project, action, local_root, operation_id, token):
    """Drive the Local Agent CLI for one exact local root under this operation.

    The root is explicit because re-anchoring runs the same verbs against two
    different checkouts within one operation.
    """
    command = [str(MODE.REPOSITORY / "bin/remote-dev-local-agent"), "--profile", project["profile"],
               "--coder-url", project["coder_url"], action]
    if action == "up":
        command += [project["runner_workspace"], "--project-id", project["project_id"],
                    "--local-project", local_root, "--git-metadata", "explicit-input"]
        for path in project["allowed_untracked"]:
            command += ["--allow-untracked", path]
        for path in project.get("remote_writable", []):
            command += ["--remote-writable", path]
    else:
        command += ["--for-context", local_root]
    if action == "exec":
        command += ["--", "/usr/bin/true"]
    environment = {**os.environ, "REMOTE_DEV_MODE_OPERATION_ID": operation_id, "REMOTE_DEV_MODE_TOKEN": token}
    result = subprocess.run(command, env=environment, capture_output=True, timeout=420)
    if result.returncode:
        fail("LOCAL_AGENT_FAILED", action + " did not complete for " + local_root +
             "; inspect local-agent status for that exact context; do not re-anchor the binding by hand")
    return result


class Operation:
    def __init__(self, store, plan, journal):
        plan = copy.deepcopy(plan)
        for key, info in journal.get("prepared_endpoint_info", {}).items():
            plan[key] = info
        self.store, self.plan, self.journal = store, plan, journal
        self.project = plan["project"]
        self.project_id = self.project["project_id"]
        self.id = journal["operation_id"]
        self.snapshot = plan["source"]["snapshot"]["snapshot_id"]
        self.local = MODE.Endpoint()
        self.remote = MODE.Endpoint(self.project["profile"], self.project["coder_url"], self.project["development_workspace"], "development")
        self.runner = MODE.Endpoint(self.project["profile"], self.project["coder_url"], self.project["runner_workspace"], "runner")
        self.local_root, self.remote_root = self.project["local_root"], self.project["remote_root"]
        self.runner_root = plan["runner"]["root"]
        self.runner.local_context = self.local_root
        self.source, self.target = (self.remote, self.local) if plan["to"] == "local-agent" else (self.local, self.remote)
        self.source_root, self.target_root = plan["source"]["root"], plan["target"]["root"]
        self.runtime = self.runner if plan["to"] == "local-agent" else self.remote
        self.runtime_root = self.runner_root if plan["to"] == "local-agent" else self.remote_root
        self.source_runtime = self.remote if plan["from"] == "remote" else self.runner
        self.source_runtime_root = self.remote_root if plan["from"] == "remote" else self.runner_root
        self.endpoints = [(self.local, self.local_root), (self.remote, self.remote_root), (self.runner, self.runner_root)]
        self.endpoints.sort(key=lambda pair: ((pair[0].workspace or ""), pair[1]))

    def save(self):
        self.journal["updated_at"] = int(time.time())
        self.store.write("operations", self.id, self.journal)

    def phase(self, value):
        self.journal["phase"] = value
        self.journal["state"] = value
        self.journal.pop("error", None)
        self.save()

    def call(self, endpoint, root, action, **options):
        return endpoint.request(action, {"root": root, "operation_id": self.id, "token": self.journal["token"],
                                         "project_id": self.project_id, "allowed": self.project["allowed_untracked"],
                                         "snapshot_id": self.snapshot, **options})

    def claim_all(self):
        for endpoint, root in self.endpoints:
            self.call(endpoint, root, "claim")

    def release_all(self):
        for endpoint, root in self.endpoints:
            self.call(endpoint, root, "release")

    def assert_prerequisites(self):
        # Refuse incompatible projection policy before claims or runtime stop.
        # In particular, never reinterpret exact-file additive grants as a
        # recursive legacy --allow-untracked grant.
        for endpoint, root in self.endpoints:
            if endpoint.workspace:
                key = MODE.WORKSPACE.digest([self.project["profile"], endpoint.workspace])
                reservation = self.store.read("workspace-reservations", key)
                if reservation and not reservation.get("complete") and reservation["operation_id"] != self.id:
                    fail("WORKSPACE_BUSY", "destination has an unresolved preparation; inspect its original operation")
        self.binding()
        for endpoint, root in self.endpoints:
            # Image/daemon and filesystem checks do not need to hash runtime
            # caches in a checkout that may already have been published.
            probe_root = str(Path(root).parent / ".remote-dev-mode-preflight")
            info = endpoint.inspect(probe_root)
            if any(not feature["matches"] for feature in info.get("image_features", {}).values()):
                fail("IMAGE_UPDATE_REQUIRED", "endpoint image changed or lacks this reviewed toolkit; preserve the operation")
            if endpoint.role and self.plan["source"].get("contract", {}).get("docker_capability") == "containers" and not info.get("docker_ready"):
                fail("DOCKER_CAPABILITY_REQUIRED", "the declared internal Docker daemon is unavailable")
            if info["free_bytes"] < self.plan.get("transfer_free_bytes_required", 0):
                fail("DISK_CAPACITY_REQUIRED", "endpoint no longer has the reviewed transfer and recovery headroom")

    def assert_source(self):
        info = self.source.inspect(self.source_root, self.project["allowed_untracked"])
        if info.get("snapshot", {}).get("snapshot_id") != self.snapshot or info.get("untracked_inventory") != self.plan["source"]["untracked_inventory"]:
            fail("SOURCE_CHANGED", "source index, worktree, refs, or untracked inventory changed; preserve claims and abort/replan")

    def prepare_workspaces(self):
        actions = self.plan.get("new_workspace_actions", [])
        if not actions:
            return
        self.binding()
        self.assert_source()
        owner = MODE.WORKSPACE.Owner(self.project["profile"], self.project["coder_url"])
        host_key = MODE.WORKSPACE.digest([self.project["profile"], self.project["coder_url"]])
        for action in actions:
            key = MODE.WORKSPACE.digest([self.project["profile"], action["name"]])
            # Admission covers the whole host while its source is still live.
            # Keep a durable host reservation across crashes/unknown outcomes.
            with self.store.mutex("host-" + host_key), self.store.mutex("workspace-" + key):
                host_reservation = self.store.read("host-preparations", host_key)
                if host_reservation and not host_reservation.get("complete") and host_reservation["operation_id"] != self.id:
                    fail("HOST_PREPARATION_BUSY", "another workspace preparation has an unresolved host allocation")
                reservation = self.store.read("workspace-reservations", key)
                if reservation and not reservation.get("complete") and reservation["operation_id"] != self.id:
                    fail("WORKSPACE_BUSY", "another mode operation owns preparation of this destination")
                self.store.write("workspace-reservations", key, {"operation_id": self.id, "complete": False})
                self.store.write("host-preparations", host_key, {"operation_id": self.id, "complete": False})
                receipt = self.journal.setdefault("workspace_preparations", {}).setdefault(key, {})
                receipt["host_key"] = host_key
                self.save()
                try:
                    owner.prepare(action, receipt, self.save, self.plan.get("transfer_free_bytes_required", 0))
                except ValueError as error:
                    fail(str(error).split(":", 1)[0], str(error).split(":", 1)[-1])
                finally:
                    if receipt.get("ready") or not receipt.get("intent"):
                        self.store.write("host-preparations", host_key, {"operation_id": self.id, "complete": True})
            endpoint = self.runner if action["role"] == "runner" else self.remote
            info_key = "runner" if action["role"] == "runner" else "target"
            root = self.runner_root if info_key == "runner" else self.remote_root
            info = endpoint.inspect(root, self.project["allowed_untracked"] if info_key == "target" else ())
            if info["root"] != root:
                fail("WORKSPACE_ROOT_CHANGED", "prepared destination uses a different canonical root")
            if info_key == "target":
                expected = self.project.get("departures", {}).get(self.plan["to"])
                if fingerprint(info) != expected:
                    fail("TARGET_CHANGED", "started target differs from its frozen managed departure")
            elif action["kind"] == "create" and info.get("exists") and not info.get("empty"):
                fail("RUNNER_OWNERSHIP_REQUIRED", "new runner project path is unexpectedly nonempty")
            self.journal.setdefault("prepared_endpoint_info", {})[info_key] = info
            self.plan[info_key] = info
            self.save()
        self.assert_source()

    def release_workspace_reservations(self):
        for key, receipt in self.journal.get("workspace_preparations", {}).items():
            if receipt.get("intent") and not receipt.get("ready"):
                # Aborting source transfer cannot prove a remote provisioning
                # request absent. Preserve both reservations for reconciliation.
                continue
            with self.store.mutex("workspace-" + key):
                reservation = self.store.read("workspace-reservations", key)
                if reservation and reservation["operation_id"] == self.id:
                    self.store.write("workspace-reservations", key, {"operation_id": self.id, "complete": True})

    def assert_candidate(self):
        info = self.target.inspect(self.target_root, self.project["allowed_untracked"])
        if info.get("snapshot", {}).get("snapshot_id") != self.snapshot:
            fail("CANDIDATE_CHANGED", "runtime modified managed source; activation is forbidden")

    def secret_command(self, action, confirmations=()):
        route = self.plan.get("secrets_route")
        if not route:
            fail("SECRETS_ROUTE_MISSING", "create a plan with an explicit separately reviewed secrets manifest")
        actual = MODE.secrets_route(route["path"], self.project, self.plan["to"], self.runtime_root,
                                    self.source_root if self.plan["to"] == "local-agent" else self.runner_root)
        if actual != route:
            fail("SECRETS_ROUTE_CHANGED", "restore the reviewed manifest before continuing this operation")
        if route["identity"]["manifest_version"] == "3":
            if self.journal.get("secrets_read_grant") != route["sha256"]:
                fail("SECRETS_APPLY_REQUIRED", "explicit mode secrets source confirmation is required before any remote secret read")
            if action == "verify":
                confirmations = ["--confirm", self.runtime.workspace, "--confirm-profile", self.project["profile"],
                                 "--confirm-origin", self.project["coder_url"], "--confirm-source", route["identity"]["source_workspace"]]
        directory = self.store.root / "transport" / self.id
        MODE.ENDPOINT.private_directory(directory)
        manifest = directory / "secrets-route.tsv"
        content = "".join(key + "\t" + value + "\n" for key, value in route["identity"].items())
        content += "".join("file\t" + source + "\t" + target + "\n" for source, target in route["files"])
        try:
            descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_uid != os.getuid() or manifest.stat().st_mode & 0o077 or manifest.read_text() != content:
                fail("SECRETS_ROUTE_CHANGED", "operation-owned route differs; preserve it for recovery")
        else:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        environment = {**os.environ, "REMOTE_DEV_PROFILE": self.project["profile"],
                       "REMOTE_DEV_EXPECTED_CODER_ORIGIN": self.project["coder_url"],
                       "REMOTE_DEV_MODE_OPERATION_ID": self.id, "REMOTE_DEV_MODE_TOKEN": self.journal["token"]}
        command = [str(MODE.REPOSITORY / "bin/remote-dev-secrets"), action, str(manifest), *confirmations]
        result = subprocess.run(command, env=environment, capture_output=True, timeout=420)
        # Keep diagnostics and secret hashes out of the mode response/journal.
        return result.returncode == 0

    def verify_secrets(self):
        route = self.plan.get("secrets_route")
        needs_grant = route and route["identity"]["manifest_version"] == "3" and self.journal.get("secrets_read_grant") != route["sha256"]
        if route and (needs_grant or not self.secret_command("verify")):
            if self.journal.get("runtime_attempted") or self.journal["phase"] != "published-inactive":
                fail("SECRETS_VERIFICATION_FAILED", "runtime setup has already been attempted; check the original local inputs and endpoint connectivity, then resume when verification succeeds; if destination secrets need replacement, abort and create a new plan")
            recovery = [str(MODE.REPOSITORY / "bin/remote-dev"), "mode", "secrets", self.id,
                        "--confirm", self.runtime.workspace, "--confirm-profile", self.project["profile"],
                        "--confirm-origin", self.project["coder_url"]]
            if route["identity"]["manifest_version"] == "3":
                recovery += ["--confirm-source", route["identity"]["source_workspace"]]
            fail("SECRETS_APPLY_REQUIRED", "review and explicitly run " + shlex.join(recovery) + "; then resume this operation")

    def install_secrets(self, args):
        if self.journal["phase"] != "published-inactive" or self.journal.get("runtime_attempted") or self.journal.get("setup_complete"):
            fail("SECRETS_CHECKPOINT_REQUIRED", "secret installation is allowed only after publication/binding and before setup")
        if (args.confirm, args.confirm_profile, args.confirm_origin) != (
                self.runtime.workspace, self.project["profile"], self.project["coder_url"]):
            fail("SECRETS_CONFIRMATION_REQUIRED", "confirm the exact reviewed workspace, profile and Coder origin")
        route = self.plan.get("secrets_route")
        if not route:
            fail("SECRETS_ROUTE_MISSING", "create a plan with an explicit secrets route")
        if route["identity"]["manifest_version"] == "3":
            if getattr(args, "confirm_source", None) != route["identity"]["source_workspace"]:
                fail("SECRETS_CONFIRMATION_REQUIRED", "confirm the exact reviewed source workspace")
            actual = MODE.secrets_route(route["path"], self.project, self.plan["to"], self.runtime_root,
                                        self.source_root if self.plan["to"] == "local-agent" else self.runner_root)
            if actual != route:
                fail("SECRETS_ROUTE_CHANGED", "restore the reviewed manifest before granting read access")
            self.journal["secrets_read_grant"] = route["sha256"]
            self.save()
        self.assert_prerequisites()
        self.claim_all()
        self.assert_source()
        self.assert_candidate()
        if self.plan["to"] == "local-agent":
            self.local_agent("status")
            self.call(self.runner, self.runner_root, "projection", snapshot=self.plan["source"]["snapshot"])
        self.journal["secrets_transfer_attempted"] = True
        self.save()
        confirmations = ["--confirm", args.confirm, "--confirm-profile", args.confirm_profile,
                         "--confirm-origin", args.confirm_origin]
        if route["identity"]["manifest_version"] == "3":
            confirmations += ["--confirm-source", args.confirm_source]
        if args.replace:
            confirmations.append("--replace")
        if self.plan["to"] == "remote":
            self.call(self.target, self.target_root, "secrets-begin", secret_paths=[entry[1] for entry in self.plan["secrets_route"]["files"]])
        try:
            installed = self.secret_command("apply", confirmations)
        finally:
            if self.plan["to"] == "remote":
                # Record even partial approved writes. A process crash leaves
                # the persisted window for rollback/resume to check instead.
                self.call(self.target, self.target_root, "secrets-finish")
        if not installed:
            fail("SECRETS_TRANSFER_INCOMPLETE", "inspect the existing secrets workflow and retry mode secrets at this checkpoint, or abort; do not recreate the binding")
        self.verify_secrets()
        self.assert_candidate()
        self.journal["secrets_verified_at"] = int(time.time())
        self.save()
        return {**self.result(), "secrets": "verified", "next_action": "mode resume " + self.id}

    def lifecycle(self, endpoint, root, phase, candidate=False, snapshot=None):
        return self.call(endpoint, root, "lifecycle", lifecycle_phase=phase, require_candidate=candidate,
                         lifecycle_snapshot=snapshot or self.plan["source"]["snapshot"],
                         lifecycle_timeout=self.project.get("lifecycle_timeout_seconds", 300),
                         context={"operation_id": self.id, "snapshot_id": self.snapshot,
                                  "profile": self.project["profile"], "workspace": endpoint.workspace or "",
                                  "data_policy": self.plan["data_policy"]})

    def stop_runtime(self, endpoint, root, candidate=False, snapshot=None):
        # Status is the first recovery action. A failed hook never establishes
        # quiescence and does not release any operation claim.
        receipt = self.lifecycle(endpoint, root, "status", snapshot=snapshot)
        if candidate:
            for resource in receipt.get("resources", []):
                if resource["running"] and (resource.get("operation_id") != self.id or resource.get("snapshot_id") != self.snapshot):
                    fail("RUNTIME_OWNERSHIP_MISMATCH", "candidate stop cannot affect an unverified or independently restarted resource")
        if receipt["state"] != "stopped":
            self.lifecycle(endpoint, root, "quiesce", snapshot=snapshot)
            self.lifecycle(endpoint, root, "stop", snapshot=snapshot)
            receipt = self.lifecycle(endpoint, root, "status", snapshot=snapshot)
        if receipt["state"] != "stopped":
            fail("RUNTIME_NOT_STOPPED", "project resource inventory still reports live resources")
        return receipt

    def binding(self):
        return read_binding(self.project, self.runner_root, self.local_root)

    def local_agent(self, action):
        binding = self.binding()
        if action != "up" and not binding:
            fail("BINDING_REQUIRED", "source Local Agent binding is missing")
        if action == "down":
            state = self.call(self.runner, self.runner_root, "runner-binding", binding=binding)
            if state["state"] == "down":
                directory = Path(binding["current_receipt_path"]).parent
                if (directory / "watcher.pid").exists() or (directory / "watcher.identity.tsv").exists():
                    fail("RECOVERY_REQUIRED", "runner lease is released but watcher identity remains; inspect Local Agent")
                return {"action": action, "completed": True}
        run_local_agent(self.project, action, self.local_root, self.id, self.journal["token"])
        if action == "up":
            self.binding()
        if action == "down" and self.call(self.runner, self.runner_root, "runner-binding", binding=binding)["state"] != "down":
            fail("RECOVERY_REQUIRED", "Local Agent down did not release the exact runner lease")
        return {"action": action, "completed": True}

    def preview(self):
        contract = self.plan["source"]["contract"]
        port = int(contract["browser_port"])
        health = urlsplit(contract["health_url"])
        ports = sorted({port, health.port} | {int(value) for value in contract.get("preview_ports", "").split(",") if value})
        # Dedicated Coder context, explicit workspace, and no autostart. Bind
        # the contract port so auth origins do not silently change.
        command = self.runtime.coder + ["port-forward", "--disable-autostart", self.runtime.workspace]
        for value in ports:
            command += ["--tcp", f"127.0.0.1:{value}:{value}"]
        opener = build_opener(ProxyHandler({}))
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            url = f"http://127.0.0.1:{port}"
            last = None
            for _ in range(40):
                if process.poll() is not None:
                    fail("PREVIEW_FAILED", "Coder tunnel exited or the contract port is occupied")
                owned = all(subprocess.run(["lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP:" + str(value),
                                            "-sTCP:LISTEN"], capture_output=True).returncode == 0 for value in ports)
                if not owned:
                    time.sleep(0.25)
                    continue
                try:
                    health_url = f"http://127.0.0.1:{health.port}" + (health.path or "/") + ("?" + health.query if health.query else "")
                    with opener.open(health_url, timeout=2) as response:
                        if not 200 <= response.status < 300:
                            fail("HEALTH_FAILED", "candidate health endpoint is not successful")
                    with opener.open(url, timeout=2) as response:
                        status = response.status
                    if process.poll() is None:
                        return {"url": url, "http_status": status, "verified_at": int(time.time()),
                                "state": "probe-complete", "reopen_command": shlex.join(command),
                                "instruction": "Run reopen_command on the Mac and keep it running, then open url."}
                except OSError as error:
                    last = type(error).__name__
                    time.sleep(0.25)
            fail("PREVIEW_FAILED", "no Mac-visible HTTP response: " + str(last))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def run(self):
        if self.journal["state"] == "aborted":
            fail("OPERATION_ABORTED", "create a new reviewed plan to switch modes")
        current = self.store.read("projects", self.project_id)
        if current and current.get("operation_id") == self.id:
            self.phase("activated")
        if self.journal["phase"] == "activated":
            self.finish_activation()
            return self.result()
        if self.journal["phase"] == "planned":
            self.prepare_workspaces()
        self.assert_prerequisites()
        self.journal["claims_started"] = True
        self.save()
        self.claim_all()
        self.assert_source()
        if self.journal["phase"] == "planned":
            self.phase("preparing")
        if self.journal["phase"] == "preparing":
            if fingerprint(self.target.inspect(self.target_root, self.project["allowed_untracked"])) != fingerprint(self.plan["target"]):
                fail("TARGET_CHANGED", "destination differs from the reviewed plan")
            if self.plan["from"] == "local-agent":
                # Flush the source's existing binding before reading the
                # Git-less contract. Status after down remains read-only SSH.
                self.local_agent("exec")
                self.assert_source()
            self.journal["source_stopped"] = self.stop_runtime(self.source_runtime, self.source_runtime_root)
            binding = self.binding()
            if binding:
                self.journal["previous_binding"] = binding
                self.save()
                self.local_agent("down")
            elif self.plan["from"] == "local-agent":
                fail("BINDING_REQUIRED", "active Local Agent source requires its verified binding")
            # The previous target runtime must be stopped before replacing its
            # path; for the runner its lease is still governed by local-agent.
            if self.plan["to"] == "remote" and self.plan["target"].get("snapshot"):
                self.journal["target_stopped"] = self.stop_runtime(self.remote, self.remote_root,
                                                                  snapshot=self.plan["target"]["snapshot"])
            self.assert_source()
            self.phase("quiesced")
        if self.journal["phase"] == "quiesced":
            self.journal["capture"] = self.call(self.source, self.source_root, "capture")
            self.phase("captured")
        if self.journal["phase"] == "captured":
            directory = self.store.root / "transport" / self.id
            MODE.ENDPOINT.private_directory(directory)
            fd, temporary = tempfile.mkstemp(dir=directory)
            os.close(fd)
            Path(temporary).unlink()
            try:
                payload = {"root": self.source_root, "operation_id": self.id, "token": self.journal["token"],
                           "project_id": self.project_id, "allowed": self.project["allowed_untracked"]}
                self.source.request("download", payload, output_path=temporary)
                if hashlib.sha256(Path(temporary).read_bytes()).hexdigest() != self.journal["capture"]["sha256"]:
                    fail("INVALID_PACKAGE", "download checksum differs")
                self.target.request("upload", {**payload, "root": self.target_root, "package_sha256": self.journal["capture"]["sha256"]}, input_path=temporary)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.phase("transferred")
        if self.journal["phase"] == "transferred":
            self.call(self.target, self.target_root, "restore", package_sha256=self.journal["capture"]["sha256"])
            self.phase("verified")
        if self.journal["phase"] == "verified":
            self.assert_source()
            self.journal["published"] = self.call(self.target, self.target_root, "publish", target_fingerprint=fingerprint(self.plan["target"]))
            self.phase("published-inactive")
        if self.journal["phase"] == "published-inactive":
            self.assert_candidate()
            if self.plan["to"] == "remote" and self.journal.get("secrets_transfer_attempted"):
                self.call(self.target, self.target_root, "secrets-finish")
            if self.plan["to"] == "local-agent":
                self.local_agent("up")
                self.local_agent("status")
                self.call(self.runner, self.runner_root, "projection", snapshot=self.plan["source"]["snapshot"])
            self.verify_secrets()
            # Every lifecycle action is journalled before it can create runtime
            # resources. Repeating hooks is permitted only by the v2 contract.
            if not self.journal.get("setup_complete"):
                self.journal["runtime_attempted"] = True
                self.save()
                self.lifecycle(self.runtime, self.runtime_root, "setup")
                self.journal["setup_complete"] = True
                self.save()
            receipt = self.lifecycle(self.runtime, self.runtime_root, "status")
            if receipt["state"] == "stopped":
                self.lifecycle(self.runtime, self.runtime_root, "start")
            self.journal["runtime_receipt"] = self.lifecycle(self.runtime, self.runtime_root, "status", candidate=True)
            self.assert_candidate()
            self.call(self.target, self.target_root, "checkpoint")
            self.journal["preview"] = self.preview()
            self.phase("runtime-ready")
        if self.journal["phase"] == "runtime-ready":
            self.verify_secrets()
            self.assert_source()
            self.assert_candidate()
            self.journal["runtime_receipt"] = self.lifecycle(self.runtime, self.runtime_root, "status", candidate=True)
            if self.lifecycle(self.source_runtime, self.source_runtime_root, "status")["state"] != "stopped":
                fail("SOURCE_RUNTIME_RUNNING", "previous runtime restarted; activation forbidden")
            current = self.store.read("projects", self.project_id)
            if current and current.get("operation_id") == self.id:
                self.phase("activated")
            else:
                if (current or {}).get("generation", 0) != self.project["generation"] or current and current["active_mode"] != self.plan["from"]:
                    fail("GENERATION_CONFLICT", "active record changed after planning")
                updated = dict(self.project)
                departures = dict(updated.get("departures", {}))
                departures[self.plan["from"]] = fingerprint(self.source.inspect(self.source_root, self.project["allowed_untracked"]))
                updated.update(active_mode=self.plan["to"], generation=self.project["generation"] + 1,
                               operation_id=self.id, departures=departures, preview=self.journal["preview"])
                self.store.write("projects", self.project_id, updated)
                self.phase("activated")
        self.finish_activation()
        return self.result()

    def finish_activation(self):
        # CAS may have committed immediately before a process crash. Reconcile
        # the same operation, never create a second transfer or roll back WIP.
        current = self.store.read("projects", self.project_id)
        if not current or current.get("operation_id") != self.id:
            fail("GENERATION_CONFLICT", "activated operation is no longer the current authority")
        if not self.journal.get("authority_marked"):
            for endpoint, root in self.endpoints:
                active = endpoint == self.target or endpoint == self.runner and self.plan["to"] == "local-agent"
                self.call(endpoint, root, "authority", authority="active" if active else "inactive")
            self.journal["authority_marked"] = True
            self.save()
        if not self.journal.get("packages_discarded"):
            # The committed content is live and verified at its final path, so
            # the transport archives are redundant. What publication replaced
            # stays untouched until `mode forget` discards it explicitly.
            for endpoint, root in self.endpoints:
                self.call(endpoint, root, "discard-packages")
            self.journal["packages_discarded"] = True
            self.save()
        if not self.journal.get("released"):
            self.release_all()
            self.journal["released"] = True
        self.journal["state"] = "activated"
        self.release_workspace_reservations()
        self.save()
        self.store.write("pending", self.project_id, {"operation_id": self.id, "complete": True})

    def abort(self):
        current = self.store.read("projects", self.project_id)
        if self.journal["phase"] == "activated" or current and current.get("operation_id") == self.id:
            fail("REVERSE_PLAN_REQUIRED", "activated work can only return through a new reverse plan of the current source")
        if self.journal["state"] == "aborted":
            self.release_all()
            self.journal["released"] = True
            self.save()
            self.store.write("pending", self.project_id, {"operation_id": self.id, "complete": True})
            return self.result()
        if self.journal["phase"] == "planned":
            # No runtime or transfer action can precede phase=preparing.
            # Release only this operation's partial claims; never require
            # ownership of the endpoint that rejected the original claim.
            if self.journal.get("claims_started") or not self.plan.get("new_workspace_actions"):
                for endpoint, root in self.endpoints:
                    self.call(endpoint, root, "release-preparing")
            self.release_workspace_reservations()
            self.journal.update(state="aborted", released=True)
            self.save()
            self.store.write("pending", self.project_id, {"operation_id": self.id, "complete": True})
            return self.result()
        self.claim_all()
        if self.journal.get("runtime_attempted"):
            self.journal["candidate_stopped"] = self.stop_runtime(self.runtime, self.runtime_root, candidate=True)
            self.save()
        if self.plan["to"] == "local-agent" and self.binding():
            self.local_agent("down")
        self.call(self.target, self.target_root, "rollback")
        self.journal["state"] = "aborted"
        self.release_workspace_reservations()
        self.save()
        self.release_all()
        self.journal["released"] = True
        self.save()
        self.store.write("pending", self.project_id, {"operation_id": self.id, "complete": True})
        return self.result()

    def forget(self, confirmed):
        """Measure, then explicitly discard, the areas a finished operation kept.

        Activation already removed the redundant transport archives. What
        remains is the destination that publication replaced and any candidate
        an abort preserved: the operator's only copies of that state, including
        ignored data no snapshot carries. So measuring is the default and
        discarding needs the exact confirmation.
        """
        areas = []
        for endpoint, root in self.endpoints:
            usage = self.call(endpoint, root, "discard-area" if confirmed else "area-usage")
            areas.append({**usage, "workspace": endpoint.workspace, "root": root})
        if confirmed:
            self.journal["areas_discarded"] = True
            self.save()
        return {"operation_id": self.id, "state": self.journal["state"], "discarded": bool(confirmed),
                "areas": areas, "retained_bytes": sum(area.get("bytes", 0) for area in areas),
                "confirm_command": None if confirmed else
                "bin/remote-dev mode forget " + self.id + " --confirm-discard-previous"}

    def result(self):
        return {"operation_id": self.id, "state": self.journal["state"], "phase": self.journal["phase"],
                "active_mode": (self.store.read("projects", self.project_id) or self.project)["active_mode"],
                "source_root": self.source_root, "target_root": self.target_root,
                "preview": self.journal.get("preview"), "journal_file": str(self.store.root / "operations" / (self.id + ".json")),
                "retained_workspace_preparations": [key for key, receipt in self.journal.get("workspace_preparations", {}).items()
                                                     if receipt.get("intent") and not receipt.get("ready")],
                "recovery": ("Source runtime remains stopped after abort; use its project start command at the original source path."
                             if self.journal.get("source_stopped") else "No source stop was confirmed; inspect its runtime before starting anything.")
                            if self.journal["state"] == "aborted" else None}


REBIND_PHASES = ["planned", "quiesced", "rebound", "anchored", "projected"]


class Rebind:
    """Re-anchor an active Local Agent binding onto another local Git root.

    Nothing is captured, transferred or published: the requested root keeps its
    own content, including the ignored data no snapshot carries. What moves is
    the anchor -- the Mac binding directory, the one root hash the runner
    stores, the authority fences and the project record -- which is what a
    project needs when the checkout it should use already exists.

    There is no rollback once the runner has been re-anchored, because the
    previous root can no longer claim it. Recovery is to run the same command
    again: every step is idempotent and the journal records how far it got.
    """

    def __init__(self, store, journal):
        self.store, self.journal = store, journal
        self.project = journal["project"]
        self.project_id = self.project["project_id"]
        self.id = journal["operation_id"]
        self.previous_root, self.local_root = journal["previous_root"], journal["local_root"]
        self.runner_root = journal["runner_root"]
        self.snapshot = journal["snapshot_id"]
        self.local = MODE.Endpoint()
        self.runner = MODE.Endpoint(self.project["profile"], self.project["coder_url"],
                                    self.project["runner_workspace"], "runner")
        self.endpoints = [(self.local, self.previous_root), (self.local, self.local_root),
                          (self.runner, self.runner_root)]

    save = Operation.save
    phase = Operation.phase
    call = Operation.call
    claim_all = Operation.claim_all
    release_all = Operation.release_all
    forget = Operation.forget

    def assert_source(self):
        """The requested root must stay exactly what the review approved."""
        actual = MODE.TRANSFER.inspect(self.local_root, self.project["allowed_untracked"])
        if actual["snapshot_id"] != self.snapshot:
            fail("SOURCE_CHANGED", "the requested Mac root changed after review; pause writers and review it again")
        return actual

    def run(self):
        self.claim_all()
        if self.journal["phase"] == "planned":
            self.assert_source()
            binding = read_binding(self.project, self.runner_root, self.previous_root)
            if not binding:
                fail("BINDING_REQUIRED", "the current Mac root has no verified Local Agent binding")
            self.journal["previous_binding"] = binding
            self.save()
            if self.call(self.runner, self.runner_root, "runner-binding", binding=binding)["state"] != "down":
                run_local_agent(self.project, "down", self.previous_root, self.id, self.journal["token"])
                if self.call(self.runner, self.runner_root, "runner-binding", binding=binding)["state"] != "down":
                    fail("RECOVERY_REQUIRED", "Local Agent down did not release the exact runner lease")
            self.phase("quiesced")
        binding = self.journal["previous_binding"]
        if self.journal["phase"] == "quiesced":
            # The runner claim names one binding directory, so the previous one
            # releases it before the requested root can take the same runner.
            if binding_directory(self.previous_root).exists():
                run_local_agent(self.project, "retire", self.previous_root, self.id, self.journal["token"])
            self.call(self.runner, self.runner_root, "runner-rebind", binding=binding,
                      root_hash=root_hash(self.local_root))
            self.phase("rebound")
        if self.journal["phase"] == "rebound":
            current = self.store.read("projects", self.project_id)
            if not current or current.get("generation") != self.project["generation"] or current["active_mode"] != "local-agent":
                fail("GENERATION_CONFLICT", "active record changed after review")
            self.project = dict(current, local_root=self.local_root, generation=current["generation"] + 1,
                                operation_id=self.id,
                                # The receipt described the previous root, so it
                                # cannot authorize publishing over the new one.
                                departures={key: value for key, value in current.get("departures", {}).items()
                                            if key != "local-agent"},
                                superseded_local_roots=sorted(
                                    (set(current.get("superseded_local_roots", [])) | {self.previous_root})
                                    - {self.local_root}))
            self.store.write("projects", self.project_id, self.project)
            self.journal["project"] = self.project
            self.phase("anchored")
        if self.journal["phase"] == "anchored":
            self.call(self.local, self.previous_root, "authority", authority="inactive")
            self.call(self.local, self.local_root, "authority", authority="active")
            run_local_agent(self.project, "up", self.local_root, self.id, self.journal["token"])
            if not read_binding(self.project, self.runner_root, self.local_root):
                fail("RECOVERY_REQUIRED", "the re-anchored Local Agent binding is absent after up")
            self.call(self.runner, self.runner_root, "projection", snapshot=self.assert_source())
            self.phase("projected")
        if not self.journal.get("released"):
            self.release_all()
            self.journal["released"] = True
        self.journal["state"] = "activated"
        if not self.journal.get("areas_discarded"):
            # Re-anchoring publishes nothing, so its endpoint areas hold only
            # transaction records; there is no recovery copy to preserve.
            for endpoint, root in self.endpoints:
                self.call(endpoint, root, "discard-area")
            self.journal["areas_discarded"] = True
        self.save()
        self.store.write("pending", self.project_id, {"operation_id": self.id, "complete": True})
        return self.result()

    def result(self):
        return {"operation_id": self.id, "state": self.journal["state"], "phase": self.journal["phase"],
                "active_mode": "local-agent", "local_root": self.local_root,
                "superseded_local_root": self.previous_root, "runner_root": self.runner_root,
                "journal_file": str(self.store.root / "operations" / (self.id + ".json")),
                "recovery": None if self.journal["state"] == "activated" else
                "Re-anchoring cannot roll back once the runner moved; repeat the same mode rebind command."}


def execute_rebind(journal, store):
    with store.mutex(journal["project"]["project_id"]):
        existing = store.read("operations", journal["operation_id"])
        if existing:
            journal = existing
        pending = store.read("pending", journal["project"]["project_id"])
        if pending and not pending.get("complete") and pending["operation_id"] != journal["operation_id"]:
            fail("OPERATION_BUSY", "resume or abort the existing project operation before re-anchoring")
        if journal["state"] == "activated" and journal.get("released"):
            return Rebind(store, journal).result()
        if not existing:
            store.create("operations", journal["operation_id"], journal)
        store.write("pending", journal["project"]["project_id"],
                    {"operation_id": journal["operation_id"], "complete": False})
        operation = Rebind(store, journal)
        try:
            return operation.run()
        except Exception as error:
            journal["state"] = "failed"
            journal["error"] = str(error).split(":", 1)[0] if isinstance(
                error, (MODE.ModeError, MODE.ENDPOINT.EndpointError, MODE.CONTRACT.ContractError,
                        MODE.TRANSFER.TransferError)) else type(error).__name__
            operation.save()
            raise


def execute(args, store):
    if args.command == "apply":
        plan = store.read("plans", args.identity)
        if plan is None:
            fail("PLAN_MISSING", "create and review a mode plan first")
        operation_id = plan["plan_id"]
    else:
        journal = store.read("operations", args.identity)
        if journal is None:
            fail("OPERATION_MISSING", "no trusted journal exists for this operation ID")
        if journal.get("kind") == "rebind":
            # A re-anchor has no reviewed plan to verify against, and no
            # rollback: it is resumed by repeating `mode rebind`, not here.
            if args.command != "forget":
                fail("REBIND_OPERATION", "re-anchoring is resumed by repeating the same mode rebind command")
            with store.mutex(journal["project"]["project_id"]):
                pending = store.read("pending", journal["project"]["project_id"])
                if pending and not pending.get("complete") and pending["operation_id"] == journal["operation_id"]:
                    fail("OPERATION_UNFINISHED", "finish the re-anchor before discarding its retained areas")
                return Rebind(store, journal).forget(getattr(args, "confirm_discard_previous", False))
        operation_id = journal["operation_id"]
        plan = store.read("plans", journal["plan_id"])
    identity = dict(plan)
    if identity.pop("plan_id", None) != MODE.checksum(MODE.encode(identity)):
        fail("PLAN_CHANGED", "immutable plan identity differs")
    if plan["toolkit_id"] != MODE.toolkit_id():
        fail("TOOLKIT_CHANGED", "use the toolkit version that created this plan; no mixed-version recovery")
    with store.mutex(plan["project"]["project_id"]):
        journal = store.read("operations", operation_id)
        if journal is None:
            if plan["blockers"]:
                fail("PLAN_BLOCKED", "; ".join(plan["blockers"]))
            if plan["expires_at"] < time.time():
                fail("PLAN_EXPIRED", "create a fresh plan without modifying source or target")
            if not args.writers_paused:
                fail("WRITERS_PAUSE_REQUIRED", "pause source/target IDEs and agents, then apply this plan with --writers-paused")
            pending = store.read("pending", plan["project"]["project_id"])
            if pending and not pending.get("complete") and pending["operation_id"] != operation_id:
                fail("OPERATION_BUSY", "resume or abort the existing project operation")
            journal = {"version": 1, "operation_id": operation_id, "plan_id": plan["plan_id"],
                       "token": secrets.token_hex(32), "phase": "planned", "state": "planned",
                       "created_at": int(time.time()), "writers_paused": True}
            store.create("operations", operation_id, journal)
        pending = store.read("pending", plan["project"]["project_id"])
        if pending and not pending.get("complete") and pending["operation_id"] != operation_id:
            fail("OPERATION_BUSY", "another operation is pending for this project")
        current = store.read("projects", plan["project"]["project_id"])
        if args.command == "secrets" and journal["phase"] != "published-inactive":
            fail("SECRETS_CHECKPOINT_REQUIRED", "secret installation is allowed only at the published-inactive checkpoint")
        if args.command == "abort" and (journal["phase"] == "activated" or current and current.get("operation_id") == operation_id):
            fail("REVERSE_PLAN_REQUIRED", "create a reverse plan from the current source")
        if args.command == "forget":
            if journal["state"] not in ("activated", "aborted") or not journal.get("released"):
                fail("OPERATION_UNFINISHED", "only an activated or aborted operation can discard its retained areas")
            if pending and not pending.get("complete"):
                fail("OPERATION_BUSY", "finish the pending project operation before discarding retained areas")
            return Operation(store, plan, journal).forget(getattr(args, "confirm_discard_previous", False))
        if journal["state"] in ("activated", "aborted") and journal.get("released"):
            operation = Operation(store, plan, journal)
            if args.command == "abort" and journal["state"] == "activated":
                fail("REVERSE_PLAN_REQUIRED", "create a reverse plan from the current source")
            return operation.result()
        store.write("pending", plan["project"]["project_id"], {"operation_id": operation_id, "complete": False})
        operation = Operation(store, plan, journal)
        try:
            if args.command == "secrets":
                return operation.install_secrets(args)
            return operation.abort() if args.command == "abort" else operation.run()
        except Exception as error:
            journal["state"] = "failed"
            journal["error"] = str(error).split(":", 1)[0] if isinstance(error, (MODE.ModeError, MODE.ENDPOINT.EndpointError, MODE.CONTRACT.ContractError, MODE.TRANSFER.TransferError)) else type(error).__name__
            operation.save()
            raise
