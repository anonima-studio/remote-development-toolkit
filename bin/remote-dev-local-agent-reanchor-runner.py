#!/usr/bin/env python3
"""Move a released runner project binding onto another Local Agent anchor.

Sent over the authenticated runner transport by `local-agent reanchor`; no
workspace image update is required. stdin is the gzip-compressed managed
manifest of the previous binding. Under the workspace claim lock this checks
that the runner is released, re-verifies every previous managed leaf that is
still present, unlinks only those leaves, removes directories they leave empty
and moves the one root hash the runner stores. Remote-writable content,
dependencies, caches, secrets and data are never touched.
"""

import errno
import gzip
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

HEADER = ["path", "type", "mode", "digest", "symlink_target", "state"]
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
FIELDS = ("project_id", "profile", "workspace", "remote_root")


class Refused(Exception):
    pass


def read_tsv(path):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise Refused("RUNNER_STATE_INVALID: " + str(path))
    values = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("\t")
        values[key] = value
    return values


def components(relative):
    parts = relative.split("/")
    if relative.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise Refused("REMOTE_MANIFEST_INVALID_PATH: " + relative)
    return parts


def open_directory(root_fd, parts):
    """Open a directory below the root without following any symlink."""
    current = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def leaf(root_fd, relative):
    """Return (parent_fd, name) or None when an ancestor is already absent."""
    parts = components(relative)
    try:
        return open_directory(root_fd, parts[:-1]), parts[-1]
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused("REMOTE_DRIFT: a parent of " + relative + " is no longer a real directory")
        raise


def matches(parent, name, row, info):
    _, kind, mode, digest, target, _ = row
    if kind == "symlink":
        actual = os.readlink(name, dir_fd=parent)
        return stat.S_ISLNK(info.st_mode) and actual == target and \
            hashlib.sha256(os.fsencode(actual)).hexdigest() == digest
    if kind != "file" or not stat.S_ISREG(info.st_mode) or "%04o" % stat.S_IMODE(info.st_mode) != mode:
        return False
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest() == digest


def remove_previous_source(remote_root, rows):
    root_fd = os.open(remote_root, DIRECTORY_FLAGS)
    try:
        verified = []
        for row in rows:
            found = leaf(root_fd, row[0])
            if found is None:
                continue
            parent, name = found
            try:
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if row[5] == "deleted" or not matches(parent, name, row, info):
                    raise Refused("REMOTE_DRIFT: " + row[0] + " differs from the previous managed manifest")
            finally:
                os.close(parent)
            verified.append(row[0])
        for relative in verified:
            parent, name = leaf(root_fd, relative)
            try:
                os.unlink(name, dir_fd=parent)
            finally:
                os.close(parent)
        parents = set()
        for row in rows:
            parts = components(row[0])
            for depth in range(1, len(parts)):
                parents.add(tuple(parts[:depth]))
        pruned = 0
        for parts in sorted(parents, key=len, reverse=True):
            try:
                parent = open_directory(root_fd, list(parts[:-1]))
            except OSError:
                continue
            try:
                os.rmdir(parts[-1], dir_fd=parent)
                pruned += 1
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                    raise
            finally:
                os.close(parent)
        return len(verified), pruned
    finally:
        os.close(root_fd)


def start_token(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip()


def main():
    if len(sys.argv) != 9:
        raise Refused("REANCHOR_ARGUMENTS_INVALID")
    workspace_control, control = Path(sys.argv[1]), Path(sys.argv[2])
    project, profile, workspace, old_hash, new_hash, remote_root = sys.argv[3:9]
    for value in (old_hash, new_hash):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise Refused("INVALID_IDENTITY: root hash is not a SHA-256 digest")
    rows = [line.split("\t") for line in gzip.decompress(sys.stdin.buffer.read()).decode("utf-8").splitlines()]
    if not rows or rows[0] != HEADER or any(len(row) != len(HEADER) for row in rows):
        raise Refused("REMOTE_MANIFEST_INVALID_HEADER")
    rows = rows[1:]
    lock = workspace_control / "claim.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise Refused("OPERATION_BUSY: Local Agent is changing its workspace claim; retry after it finishes")
    try:
        token = start_token(os.getpid())
        (lock / "owner.tsv").write_text("pid\t%d\nstart_token\t%s\n" % (os.getpid(), token))
        if read_tsv(workspace_control / "binding.tsv") or read_tsv(workspace_control / "lease.tsv") or \
                read_tsv(control / "lease.tsv"):
            raise Refused("RUNNER_NOT_RELEASED: the runner is bound; run the exact Local Agent down first")
        for directory in (control / "processes/active", control / "processes/recovery"):
            if directory.is_symlink() or directory.exists() and any(directory.iterdir()):
                raise Refused("RECOVERY_REQUIRED: runner retains active or recovery process records")
        if any((control / "executions").glob("*.running")):
            raise Refused("RECOVERY_REQUIRED: runner retains unknown execution records; recover them with the previous binding")
        binding = read_tsv(control / "binding.tsv")
        expected = {"project_id": project, "profile": profile, "workspace": workspace, "remote_root": remote_root}
        if not binding or any(binding.get(key) != expected[key] for key in FIELDS):
            raise Refused("BINDING_IDENTITY_MISMATCH: persistent runner project identity differs or is absent")
        if binding.get("root_hash") == new_hash:
            print("REANCHOR_RUNNER_MOVED\talready")
            return
        if binding.get("root_hash") != old_hash:
            raise Refused("BINDING_IDENTITY_MISMATCH: the runner is anchored to another local root")
        removed, pruned = remove_previous_source(remote_root, rows)
        updated = dict(binding, root_hash=new_hash)
        descriptor, name = tempfile.mkstemp(prefix=".binding-", dir=control)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write("".join(key + "\t" + value + "\n" for key, value in updated.items()))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, control / "binding.tsv")
            directory = os.open(control, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        print("REANCHOR_RUNNER_MOVED\tremoved=%d\tpruned=%d" % (removed, pruned))
    finally:
        try:
            (lock / "owner.tsv").unlink()
        except FileNotFoundError:
            pass
        lock.rmdir()


if __name__ == "__main__":
    try:
        main()
    except Refused as error:
        print(error, file=sys.stderr)
        raise SystemExit(74 if str(error).startswith("REMOTE_DRIFT") else 79)
