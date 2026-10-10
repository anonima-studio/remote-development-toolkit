#!/usr/bin/env python3
"""Capture Git cached-existing semantics against an accepted runner projection.

The Local Agent execution owner supplies the manifest and receipt identity.
The consumer receives explicit metadata; no Git database is projected.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class InventoryError(Exception):
    pass


def fail(message):
    raise InventoryError("TRACKED_INVENTORY_INVALID: " + message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *args):
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    result = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", *args], env=env, capture_output=True)
    if result.returncode:
        fail("cannot read source Git index")
    return result.stdout


def capture(root, manifest_path, remote_root, receipt_id):
    root = Path(root)
    manifest_path = Path(manifest_path)
    if manifest_path.is_symlink():
        fail("accepted projection manifest must not be a symbolic link")
    manifest_path = manifest_path.resolve(strict=True)
    if root.resolve() != root:
        fail("source and accepted manifest paths must be canonical")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", receipt_id):
        fail("execution receipt identity is invalid")
    if not Path(remote_root).is_absolute():
        fail("remote project path must be absolute")
    raw_manifest = manifest_path.read_bytes()
    rows = raw_manifest.decode().splitlines()
    if not rows or rows[0] != "path\ttype\tmode\tdigest\tsymlink_target\tstate":
        fail("accepted projection header differs")
    managed = {}
    for line in rows[1:]:
        row = line.split("\t")
        if len(row) != 6 or row[0] in managed:
            fail("invalid accepted projection row")
        managed[row[0]] = row
    before_index = git(root, "ls-files", "--stage", "-z")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    if git(root, "ls-files", "--unmerged", "-z"):
        fail("unmerged index requires resolution before execution")
    files = []
    entries = []
    for relative_bytes in git(root, "ls-files", "--cached", "-z").split(b"\0"):
        if not relative_bytes:
            continue
        relative = os.fsdecode(relative_bytes)
        if any(c in relative for c in "\t\r\n") or any(part in ("", ".", "..", ".git") for part in relative.split("/")):
            fail("tracked path is not supported by the existing projection transport")
        path = root / relative
        if not path.exists():
            # Matches the project's existing Git-backed existsSync semantics.
            continue
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                fail("tracked path has a symbolic-link ancestor")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            content = os.fsencode(os.readlink(path))
            kind, mode = "symlink", "0777"
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                if os.fstat(stream.fileno()).st_ino != info.st_ino:
                    fail("tracked file identity changed")
                content = stream.read()
            kind, mode = "file", "%04o" % stat.S_IMODE(info.st_mode)
        else:
            fail("cached-existing path is not a projected file or symlink")
        row = managed.get(relative)
        sha = digest(content)
        if not row or row[1] != kind or row[2] != mode or row[3] != sha or row[5] != "present":
            fail("tracked content is excluded or differs from the accepted projection")
        files.append(relative)
        entries.append({"path": relative, "kind": kind, "sha256": sha})
    if before_index != git(root, "ls-files", "--stage", "-z") or head != git(root, "rev-parse", "HEAD").decode().strip():
        fail("Git index or HEAD changed during metadata capture")
    if manifest_path.read_bytes() != raw_manifest:
        fail("accepted projection changed during metadata capture")
    return {"version": 1, "kind": "git-cached-existing", "project_dir": remote_root,
            "local_root": str(root), "source_head": head, "source_index_id": digest(before_index),
            "projection_manifest_id": digest(raw_manifest), "receipt_id": receipt_id,
            "files": sorted(set(files)), "entries": sorted(entries, key=lambda row: row["path"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("manifest")
    parser.add_argument("remote_root")
    parser.add_argument("receipt_id")
    args = parser.parse_args()
    try:
        print(json.dumps(capture(args.root, args.manifest, args.remote_root, args.receipt_id), sort_keys=True, separators=(",", ":")))
    except (InventoryError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, InventoryError) else "TRACKED_INVENTORY_INVALID: cannot verify source metadata", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
