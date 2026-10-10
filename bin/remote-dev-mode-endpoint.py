#!/usr/bin/env python3
"""Endpoint transaction primitives. Loaded with the exact transfer/contract readers.

Claims serialize managed operations; they cannot prevent arbitrary editor writes.
No claim expires and no foreign claim is recovered from a PID or timeout.
"""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time


class EndpointError(Exception):
    pass


def fail(code, detail):
    raise EndpointError(code + ": " + detail)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    if path.resolve() != path:
        fail("STATE_UNTRUSTED", "state path has a symbolic-link ancestor")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail("STATE_UNTRUSTED", "state directory must be owned by the current user with mode 0700")


def read_record(path):
    if path.parent.resolve() != path.parent:
        fail("STATE_UNTRUSTED", "record parent changed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail("STATE_UNTRUSTED", "record must be an owner-only regular file")
        try:
            return json.load(stream)
        except (ValueError, UnicodeError) as error:
            fail("RECOVERY_REQUIRED", "state record is incomplete or damaged; preserve its ownership")


def write_record(path, value, exclusive=False):
    private_directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def directory_identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        fail("RECOVERY_REQUIRED", "transaction directory was replaced by a different file type")
    return [info.st_dev, info.st_ino]


def read_tsv(path):
    if path.parent.resolve() != path.parent:
        fail("BINDING_UNTRUSTED", "runner control path changed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail("BINDING_UNTRUSTED", "runner record permissions differ")
        values = {}
        for line in stream.read().splitlines():
            if "\t" not in line:
                fail("BINDING_UNTRUSTED", "invalid runner record")
            key, value = line.split("\t", 1)
            if key in values:
                fail("BINDING_UNTRUSTED", "duplicate runner record field")
            values[key] = value
        return values


def tree_fingerprint(root, excluded_files=()):
    """Detect ALL target writes, including ignored data, without following links.

    This is an endpoint-local drift receipt, not portable content identity.
    Never open payloads here: ignored files may be secrets without a read grant.
    Git content parity is represented separately by the semantic Git snapshot.
    """
    root = Path(root)
    excluded_files = set(excluded_files)
    for relative in excluded_files:
        TRANSFER.safe_path(relative)
        path = root / relative
        if (path.parent.resolve() != path.parent or not path.parent.is_dir() or
                path.is_symlink() or path.exists() and not path.is_file()):
            fail("UNSAFE_PATH", "secret checkpoint can exclude only regular files in existing canonical directories")
    if not root.exists():
        return {"exists": False}
    if root.resolve() != root or not root.is_dir():
        fail("UNSAFE_PATH", "checkout must be a canonical directory")
    entries = []
    for parent, directories, files in os.walk(root, followlinks=False):
        if Path(parent) == root and ".git" in directories:
            directories.remove(".git")
        for name in sorted(directories + files):
            path = Path(parent) / name
            relative = str(path.relative_to(root))
            if relative in excluded_files:
                continue
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode):
                value = [relative, "link", os.readlink(path)]
            elif stat.S_ISDIR(before.st_mode):
                value = [relative, "directory", stat.S_IMODE(before.st_mode)]
            elif stat.S_ISREG(before.st_mode):
                value = [relative, "file-metadata", before.st_dev, before.st_ino,
                         before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                         before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)]
            else:
                fail("UNSUPPORTED_FILESYSTEM", "checkout contains a socket, device, or FIFO")
            after = path.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_uid, before.st_gid, before.st_mode) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_uid, after.st_gid, after.st_mode):
                fail("SOURCE_CHANGED", "checkout changed during fingerprint")
            entries.append(value)
    return {"exists": True, "tree_id": digest(encoded(sorted(entries))), "entries": len(entries)}


class Transaction:
    def __init__(self, request):
        self.request = request
        self.root = Path(request["root"]).expanduser()
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            fail("UNSAFE_PATH", "endpoint root must be canonical")
        role = request.get("role")
        if role:
            if role not in ("development", "runner") or (Path.home() / "projects").resolve() not in self.root.parents:
                fail("WORKSPACE_ROLE_MISMATCH", "invalid endpoint role or project root")
            marker = Path.home() / ".local/share/remote-dev/workspace-role"
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != role + "\n":
                fail("WORKSPACE_ROLE_MISMATCH", "immutable workspace role differs")
        self.operation = request["operation_id"]
        self.token = request["token"]
        if not re.fullmatch(r"[0-9a-f]{64}", self.operation) or not re.fullmatch(r"[0-9a-f]{64}", self.token):
            fail("INVALID_IDENTITY", "invalid operation identity")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", request.get("project_id", "")):
            fail("INVALID_IDENTITY", "invalid project identity")
        self.state_root = Path.home() / ".local/share/remote-dev/mode-endpoints"
        self.claim_key = digest(os.fsencode(self.root))
        self.claim = self.state_root / "claims" / (self.claim_key + ".json")
        self.owner = {"operation_id": self.operation, "token": self.token,
                      "project_id": request["project_id"], "root": str(self.root)}
        self.area = self.root.parent / (".remote-dev-mode-" + self.operation + "-" + self.claim_key[:16])
        self.record = self.area / "transaction.json"
        self.allowed = request.get("allowed", [])

    @contextlib.contextmanager
    def lock(self):
        private_directory(self.state_root)
        directory = self.state_root / "locks"
        private_directory(directory)
        fd = os.open(directory / self.claim_key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_mode & 0o077:
                fail("STATE_UNTRUSTED", "operation mutex permissions differ")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail("OPERATION_BUSY", "another process owns the endpoint mutex")
            yield
        finally:
            os.close(fd)

    def owned(self):
        if read_record(self.claim) != self.owner:
            fail("OPERATION_BUSY", "endpoint claim belongs to another operation or is absent")

    def state(self):
        state = read_record(self.record)
        if state is None or state.get("owner") != self.owner:
            fail("RECOVERY_REQUIRED", "transaction identity is absent or differs")
        return state

    def save(self, state):
        write_record(self.record, state)

    def snapshot(self, root=None):
        root = self.root if root is None else root
        return TRANSFER.inspect(root, self.allowed)["snapshot_id"]

    def matches(self, root, expected):
        return tree_fingerprint(root) == expected["tree"] and (not expected.get("snapshot") or self.snapshot(root) == expected["snapshot"])

    def finish_secret_checkpoint(self, state):
        window = state.get("secret_window")
        if window and window["pending"]:
            if (directory_identity(self.root) != state["candidate_identity"] or
                    self.snapshot() != state["candidate_snapshot"] or
                    tree_fingerprint(self.root, window["paths"]) != window["other_tree"]):
                fail("RECOVERY_REQUIRED", "candidate changed outside the approved secret file paths; preserve all paths")
            state["runtime_tree"] = tree_fingerprint(self.root)
            window["pending"] = False
            self.save(state)

    def verify_projection(self, snapshot):
        TRANSFER.validate_snapshot(snapshot)
        if (self.root / ".git").exists() or (self.root / ".git").is_symlink():
            fail("PROJECTION_MISMATCH", "expected a Git-less projection of the exact source")
        selected = {entry["path"] for entry in snapshot["index"]} | set(snapshot["selected_untracked"])
        tracked = {entry["path"] for entry in snapshot["index"]}
        existing = []
        for expected in snapshot["worktree"]:
            if expected["path"] not in selected:
                continue
            actual, _ = TRANSFER.read_entry(self.root, expected["path"])
            if actual != expected:
                fail("PROJECTION_MISMATCH", "runner managed files differ from the frozen source")
            if expected["path"] in tracked and expected["kind"] in ("file", "symlink"):
                existing.append(expected["path"])
        return {"snapshot_id": snapshot["snapshot_id"], "tracked_existing": len(existing)}

    def verify_lifecycle_source(self):
        snapshot = self.request["lifecycle_snapshot"]
        TRANSFER.validate_snapshot(snapshot)
        selected = {entry["path"] for entry in snapshot["index"]} | set(snapshot["selected_untracked"])
        if ".remote-dev/project.env" not in selected:
            fail("CONTRACT_NOT_CAPTURED", "lifecycle contract must belong to the frozen source")
        if self.request.get("role") == "runner":
            self.verify_projection(snapshot)
        elif self.snapshot() != snapshot["snapshot_id"]:
            fail("LIFECYCLE_SOURCE_CHANGED", "preserve the checkout; changed source cannot execute recovery hooks")

    def handle(self, action, source=None, destination=None):
        with self.lock():
            if action == "release-preparing":
                # A claim can have been persisted before its first journal
                # record. This path never acquires or removes a foreign claim.
                if read_record(self.claim) != self.owner:
                    return {"released": False, "owned": False}
                state = read_record(self.record)
                if state is not None and (state.get("owner") != self.owner or state.get("phase") != "claimed"):
                    fail("RECOVERY_REQUIRED", "endpoint progressed beyond claim-only preparation")
                self.claim.unlink()
                fsync_directory(self.claim.parent)
                return {"released": True, "owned": True}
            if action in ("area-usage", "discard-area"):
                # Reachable only after the operation released this endpoint:
                # a live claim still owns the area and its recovery copies.
                if read_record(self.claim) is not None:
                    fail("OPERATION_BUSY", "endpoint claim is held; finish or abort the operation first")
                state = read_record(self.record)
                if state is None:
                    return {"area": str(self.area), "exists": False}
                if state.get("owner") != self.owner:
                    fail("RECOVERY_REQUIRED", "operation area belongs to another operation")
                usage = {"area": str(self.area), "exists": True, "phase": state["phase"], "bytes": 0, "entries": 0,
                         "holds": sorted(name for name in ("previous", "aborted-candidate", "candidate",
                                                           "source.zip", "incoming.zip")
                                         if (self.area / name).exists())}
                for parent, directories, files in os.walk(self.area, followlinks=False):
                    usage["entries"] += len(directories) + len(files)
                    for name in files:
                        info = (Path(parent) / name).lstat()
                        if stat.S_ISREG(info.st_mode):
                            usage["bytes"] += info.st_size
                if action == "area-usage":
                    return usage
                info = self.area.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    fail("STATE_UNTRUSTED", "operation area is not an owner-only directory")
                shutil.rmtree(self.area)
                fsync_directory(self.area.parent)
                return {**usage, "discarded": True}
            if action == "release" and read_record(self.claim) is None:
                self.state()
                return {"released": True}
            if action == "claim":
                fence = read_record(self.state_root / "fences" / (self.claim_key + ".json"))
                if fence and (fence.get("root") != str(self.root) or fence.get("project_id") != self.request["project_id"]):
                    fail("ENDPOINT_BINDING_CONFLICT", "endpoint is already managed under another project identity")
                current = read_record(self.claim)
                if current is not None and current != self.owner:
                    fail("OPERATION_BUSY", "endpoint is claimed; use its existing operation ID")
                if current is None:
                    write_record(self.claim, self.owner, exclusive=True)
                self.root.parent.mkdir(parents=True, exist_ok=True)
                private_directory(self.area)
                if not self.record.exists():
                    self.save({"owner": self.owner, "phase": "claimed"})
                return self.state()
            self.owned()
            state = self.state()
            if action == "status":
                return state
            if action == "runner-binding":
                expected = self.request["binding"]
                control = Path.home() / ".local/share/remote-dev/local-agent" / self.request["project_id"]
                workspace = Path.home() / ".local/share/remote-dev/local-agent-workspace"
                binding = read_tsv(workspace / "binding.tsv")
                lease = read_tsv(workspace / "lease.tsv")
                project_lease = read_tsv(control / "lease.tsv")
                project_binding = read_tsv(control / "binding.tsv")
                fields = ("project_id", "profile", "workspace", "root_hash", "remote_root")
                if not project_binding or any(project_binding.get(key) != expected.get(key) for key in fields):
                    fail("BINDING_IDENTITY_MISMATCH", "persistent runner project identity differs or is absent")
                if (workspace / "claim.lock").exists():
                    fail("OPERATION_BUSY", "Local Agent is changing its workspace claim")
                if binding is None and lease is None and project_lease is None:
                    for directory in (control / "processes/active", control / "processes/recovery"):
                        if directory.is_symlink() or directory.exists() and any(directory.iterdir()):
                            fail("RECOVERY_REQUIRED", "runner retains active or recovery process records")
                    if any((control / "executions").glob("*.running")):
                        fail("RECOVERY_REQUIRED", "runner retains unknown execution records")
                    return {"state": "down"}
                if not binding or not lease or not project_lease:
                    fail("RECOVERY_REQUIRED", "runner release has an incomplete outcome")
                if (any(binding.get(key) != expected.get(key) or lease.get(key) != expected.get(key) for key in fields) or
                        lease.get("owner_id") != expected.get("owner_id") or project_lease.get("owner_id") != expected.get("owner_id")):
                    fail("BINDING_IDENTITY_MISMATCH", "runner lease belongs to another root or owner")
                return {"state": "owned"}
            if action == "runner-rebind":
                """Move the persistent runner binding onto another local root.

                The runner stores only the hash of the Mac root, so re-anchoring
                a project is exactly this one field. It is permitted only while
                the runner is released: no workspace binding, no lease, and no
                process or execution record may survive the change, and every
                other identity field must already match.
                """
                expected, replacement = self.request["binding"], self.request["root_hash"]
                if not re.fullmatch(r"[0-9a-f]{64}", replacement or ""):
                    fail("INVALID_IDENTITY", "replacement root hash is not a SHA-256 digest")
                control = Path.home() / ".local/share/remote-dev/local-agent" / self.request["project_id"]
                workspace = Path.home() / ".local/share/remote-dev/local-agent-workspace"
                if (workspace / "claim.lock").exists():
                    fail("OPERATION_BUSY", "Local Agent is changing its workspace claim")
                if read_tsv(workspace / "binding.tsv") or read_tsv(workspace / "lease.tsv") or read_tsv(control / "lease.tsv"):
                    fail("RUNNER_NOT_RELEASED", "run the exact Local Agent down before re-anchoring the runner")
                for directory in (control / "processes/active", control / "processes/recovery"):
                    if directory.is_symlink() or directory.exists() and any(directory.iterdir()):
                        fail("RECOVERY_REQUIRED", "runner retains active or recovery process records")
                if any((control / "executions").glob("*.running")):
                    fail("RECOVERY_REQUIRED", "runner retains unknown execution records")
                binding = read_tsv(control / "binding.tsv")
                fields = ("project_id", "profile", "workspace", "root_hash", "remote_root")
                if not binding:
                    fail("BINDING_IDENTITY_MISMATCH", "persistent runner project identity is absent")
                if binding["root_hash"] == replacement:
                    return {"state": "rebound", "root_hash": replacement}
                if any(binding.get(key) != expected.get(key) for key in fields):
                    fail("BINDING_IDENTITY_MISMATCH", "persistent runner project identity differs from the reviewed binding")
                updated = dict(binding, root_hash=replacement)
                fd, name = tempfile.mkstemp(prefix=".binding-", dir=control)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "w") as stream:
                        stream.write("".join(key + "\t" + value + "\n" for key, value in updated.items()))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, control / "binding.tsv")
                    fsync_directory(control)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                return {"state": "rebound", "root_hash": replacement}
            if action == "fingerprint":
                return {"tree": tree_fingerprint(self.root), "snapshot": self.snapshot() if (self.root / ".git").is_dir() else None}
            if action == "capture":
                expected = self.request["snapshot_id"]
                if self.snapshot() != expected:
                    fail("SOURCE_CHANGED", "source differs from the reviewed plan")
                package = self.area / "source.zip"
                if package.exists():
                    receipt = state.get("capture")
                    if not receipt or receipt["sha256"] != digest(package.read_bytes()):
                        fail("RECOVERY_REQUIRED", "incomplete capture exists; preserve it and abort or replan")
                else:
                    receipt = TRANSFER.export(self.root, package, self.allowed, expected)
                    state["capture"] = receipt
                    self.save(state)
                return receipt
            if action == "download":
                receipt = state.get("capture")
                package = self.area / "source.zip"
                if not receipt or digest(package.read_bytes()) != receipt["sha256"]:
                    fail("RECOVERY_REQUIRED", "capture checksum differs")
                with package.open("rb") as stream:
                    shutil.copyfileobj(stream, destination)
                return None
            if action == "upload":
                expected = self.request["package_sha256"]
                package = self.area / "incoming.zip"
                if package.exists():
                    if digest(package.read_bytes()) != expected:
                        fail("RECOVERY_REQUIRED", "existing transport payload differs")
                    return {"sha256": expected}
                fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.area)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        shutil.copyfileobj(source, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if digest(Path(temporary).read_bytes()) != expected:
                        fail("INVALID_PACKAGE", "transport checksum differs")
                    os.replace(temporary, package)
                    fsync_directory(self.area)
                finally:
                    if Path(temporary).exists():
                        Path(temporary).unlink()
                return {"sha256": expected}
            if action == "restore":
                candidate = self.area / "candidate"
                if candidate.exists():
                    if state.get("candidate_snapshot") != self.request["snapshot_id"] or self.snapshot(candidate) != self.request["snapshot_id"]:
                        fail("RECOVERY_REQUIRED", "candidate restore is incomplete or changed; preserve it")
                else:
                    TRANSFER.restore(self.area / "incoming.zip", candidate, self.request["package_sha256"])
                    if self.snapshot(candidate) != self.request["snapshot_id"]:
                        fail("SOURCE_CHANGED", "candidate differs from captured source")
                    state["candidate_snapshot"] = self.request["snapshot_id"]
                    state["candidate_tree"] = tree_fingerprint(candidate)
                    state["candidate_identity"] = directory_identity(candidate)
                    state["phase"] = "verified"
                    self.save(state)
                return {"snapshot_id": state["candidate_snapshot"]}
            if action == "publish":
                candidate, backup = self.area / "candidate", self.area / "previous"
                expected = self.request["target_fingerprint"]
                if state["phase"] == "verified":
                    if not self.matches(self.root, expected):
                        fail("TARGET_CHANGED", "destination changed after planning")
                    if self.snapshot(candidate) != state["candidate_snapshot"] or tree_fingerprint(candidate) != state["candidate_tree"]:
                        fail("SOURCE_CHANGED", "prepared candidate changed")
                    state.update(phase="publishing", previous=expected, previous_identity=directory_identity(self.root))
                    self.save(state)
                if state["phase"] == "publishing":
                    if expected["tree"]["exists"] and not backup.exists():
                        if directory_identity(self.root) != state["previous_identity"] or not self.matches(self.root, expected):
                            fail("RECOVERY_REQUIRED", "cannot identify previous destination before rename")
                        os.rename(self.root, backup)
                        fsync_directory(self.root.parent)
                        fsync_directory(self.area)
                    if backup.exists() and (directory_identity(backup) != state["previous_identity"] or not self.matches(backup, expected)):
                        fail("RECOVERY_REQUIRED", "previous destination changed")
                    if candidate.exists():
                        if directory_identity(candidate) != state["candidate_identity"] or self.snapshot(candidate) != state["candidate_snapshot"] or tree_fingerprint(candidate) != state["candidate_tree"]:
                            fail("RECOVERY_REQUIRED", "candidate identity changed before publication")
                        if self.root.exists():
                            fail("RECOVERY_REQUIRED", "final path is occupied during publish")
                        os.rename(candidate, self.root)
                        fsync_directory(self.root.parent)
                        fsync_directory(self.area)
                    if directory_identity(self.root) != state["candidate_identity"] or self.snapshot() != state["candidate_snapshot"] or tree_fingerprint(self.root) != state["candidate_tree"]:
                        fail("RECOVERY_REQUIRED", "published candidate does not match verified identity")
                    state["phase"] = "published-inactive"
                    self.save(state)
                if state["phase"] != "published-inactive":
                    fail("RECOVERY_REQUIRED", "publish cannot follow this endpoint phase")
                return {"root": str(self.root), "snapshot_id": state["candidate_snapshot"], "backup": str(backup) if backup.exists() else None}
            if action == "lifecycle":
                self.verify_lifecycle_source()
                context = dict(self.request["context"])
                context["operation_id"] = self.operation
                return CONTRACT.run_phase(self.root, self.request["lifecycle_phase"], context,
                                          self.request.get("require_candidate", False), timeout=self.request.get("lifecycle_timeout", 300))
            if action == "projection":
                snapshot = self.request["snapshot"]
                if snapshot["snapshot_id"] != self.request["snapshot_id"]:
                    fail("PROJECTION_MISMATCH", "expected a Git-less projection of the exact source")
                return self.verify_projection(snapshot)
            if action == "secrets-begin":
                if state["phase"] != "published-inactive":
                    fail("SECRETS_CHECKPOINT_REQUIRED", "secret checkpoint requires a published candidate")
                paths = self.request["secret_paths"]
                if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
                    fail("SECRETS_ROUTE_INVALID", "secret checkpoint requires unique file paths")
                for path in paths:
                    TRANSFER.safe_path(path)
                window = state.get("secret_window")
                if window and window["paths"] != paths:
                    fail("SECRETS_ROUTE_CHANGED", "secret checkpoint paths differ")
                self.finish_secret_checkpoint(state)
                if (directory_identity(self.root) != state["candidate_identity"] or
                        self.snapshot() != state["candidate_snapshot"] or
                        tree_fingerprint(self.root) != state.get("runtime_tree", state["candidate_tree"])):
                    fail("RECOVERY_REQUIRED", "candidate changed before secret installation")
                snapshot = TRANSFER.inspect(self.root, self.allowed)
                selected = {entry["path"] for entry in snapshot["index"]} | set(snapshot["selected_untracked"])
                if any(path == item or path.startswith(item + "/") or item.startswith(path + "/") for path in paths for item in selected):
                    fail("SECRETS_ROUTE_INVALID", "secret installation cannot overwrite transferred source paths")
                state["secret_window"] = {"paths": paths, "other_tree": tree_fingerprint(self.root, paths), "pending": True}
                self.save(state)
                return {"state": "prepared"}
            if action == "secrets-finish":
                self.finish_secret_checkpoint(state)
                return {"state": "recorded"}
            if action == "checkpoint":
                self.finish_secret_checkpoint(state)
                state["runtime_tree"] = tree_fingerprint(self.root)
                self.save(state)
                return {"tree": state["runtime_tree"]}
            if action == "rollback":
                self.finish_secret_checkpoint(state)
                backup, candidate = self.area / "previous", self.area / "candidate"
                if state["phase"] in ("publishing", "published-inactive", "rolling-back"):
                    expected = state["previous"]
                    if backup.exists() and (directory_identity(backup) != state["previous_identity"] or not self.matches(backup, expected)):
                        fail("RECOVERY_REQUIRED", "backup contains new writes; preserve all paths")
                    preserved = self.area / "aborted-candidate"
                    root_identity = directory_identity(self.root)
                    if root_identity == state["candidate_identity"]:
                        if self.snapshot() != state["candidate_snapshot"] or tree_fingerprint(self.root) != state.get("runtime_tree", state["candidate_tree"]):
                            fail("RECOVERY_REQUIRED", "candidate contains new writes; preserve all paths")
                    elif root_identity is not None:
                        if root_identity != state["previous_identity"] or not self.matches(self.root, expected):
                            fail("RECOVERY_REQUIRED", "final path occupant is not the unchanged previous checkout")
                    if preserved.exists() and (directory_identity(preserved) != state["candidate_identity"] or
                                               self.snapshot(preserved) != state["candidate_snapshot"] or
                                               tree_fingerprint(preserved) != state.get("runtime_tree", state["candidate_tree"])):
                        fail("RECOVERY_REQUIRED", "preserved candidate changed during abort")
                    state["phase"] = "rolling-back"
                    self.save(state)
                    if root_identity == state["candidate_identity"]:
                        if preserved.exists():
                            fail("RECOVERY_REQUIRED", "both candidate locations are occupied")
                        os.rename(self.root, preserved)
                        fsync_directory(self.root.parent)
                        fsync_directory(self.area)
                    if backup.exists():
                        if self.root.exists():
                            fail("RECOVERY_REQUIRED", "cannot overwrite final path during abort")
                        os.rename(backup, self.root)
                        fsync_directory(self.root.parent)
                        fsync_directory(self.area)
                    if not self.matches(self.root, expected):
                        fail("RECOVERY_REQUIRED", "abort destination differs from previous state")
                state["phase"] = "aborted"
                self.save(state)
                return {"state": "aborted", "preserved_area": str(self.area)}
            if action == "discard-packages":
                # Transport archives are redundant once the content is live and
                # verified at the final path. The previous destination is never
                # touched here: it is the only copy of what publication replaced.
                if state["phase"] == "publishing":
                    fail("RECOVERY_REQUIRED", "publication is incomplete; preserve every transport payload")
                removed = []
                for name in ("source.zip", "incoming.zip"):
                    package = self.area / name
                    if package.is_file() and not package.is_symlink():
                        package.unlink()
                        removed.append(name)
                if removed:
                    fsync_directory(self.area)
                state["packages_discarded"] = True
                self.save(state)
                return {"removed": removed}
            if action == "authority":
                value = self.request.get("authority")
                if value not in ("active", "inactive"):
                    fail("INVALID_ACTION", "authority must be active or inactive")
                fence = {"root": str(self.root), "project_id": self.request["project_id"],
                         "operation_id": self.operation, "state": value}
                write_record(self.state_root / "fences" / (self.claim_key + ".json"), fence)
                return fence
            if action == "release":
                self.claim.unlink()
                fsync_directory(self.claim.parent)
                return {"released": True}
            fail("INVALID_ACTION", "unknown endpoint operation")
