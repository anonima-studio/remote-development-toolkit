#!/usr/bin/env python3
"""Managed-entrypoint fence and bounded writer lock; external editors are separate."""
import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys


def record(path):
    if path.parent.resolve() != path.parent:
        raise RuntimeError("MODE_STATE_UNTRUSTED: fence parent is not canonical")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("MODE_STATE_UNTRUSTED: fence must be an owner-only regular file")
        return json.load(stream)


def check(context):
    context = Path(context).expanduser()
    if not context.is_absolute() or context.resolve() != context:
        raise RuntimeError("MODE_PATH_INVALID: managed context must be canonical")
    state = Path.home().resolve() / ".local/share/remote-dev/mode-endpoints"
    for root in (context, *context.parents):
        key = hashlib.sha256(os.fsencode(root)).hexdigest() + ".json"
        claim = record(state / "claims" / key)
        if claim:
            if (claim.get("root") != str(root) or claim.get("operation_id") != os.environ.get("REMOTE_DEV_MODE_OPERATION_ID") or
                    claim.get("token") != os.environ.get("REMOTE_DEV_MODE_TOKEN")):
                raise RuntimeError("MODE_OPERATION_IN_PROGRESS: resume or abort the operation before running managed commands")
            continue
        fence = record(state / "fences" / key)
        if fence:
            if fence.get("root") != str(root) or fence.get("state") not in ("active", "inactive"):
                raise RuntimeError("MODE_STATE_UNTRUSTED: invalid authority fence")
            if fence["state"] == "inactive":
                raise RuntimeError("MODE_SOURCE_INACTIVE: this checkout is a preserved previous source; use mode status and the active path")


@contextmanager
def mutation_lock(context):
    """Serialize a bounded writer against endpoint claims/publication.

    Lock ancestors too: a secret route may name a subdirectory of the checkout
    whose claim is owned by mode. Check authority after every lock is held.
    """
    context = Path(context).expanduser()
    if not context.is_absolute() or context.resolve() != context:
        raise RuntimeError("MODE_PATH_INVALID: managed context must be canonical")
    state = Path.home().resolve() / ".local/share/remote-dev/mode-endpoints"
    directory = state / "locks"
    for path in (state, directory):
        if path.resolve() != path:
            raise RuntimeError("MODE_STATE_UNTRUSTED: mutex parent is not canonical")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError("MODE_STATE_UNTRUSTED: mutex directory must be owner-only")
    descriptors = []
    try:
        keys = sorted(hashlib.sha256(os.fsencode(root)).hexdigest() for root in (context, *context.parents))
        for key in keys:
            descriptor = os.open(directory / key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeError("MODE_STATE_UNTRUSTED: mutex must be an owner-only regular file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("MODE_OPERATION_IN_PROGRESS: another endpoint operation owns the mutex")
        check(context)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print("Usage: remote-dev-mode-guard <canonical-project-context>")
        sys.exit(0)
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("MODE_PATH_REQUIRED: provide one project context")
        check(sys.argv[1])
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, RuntimeError) else "MODE_STATE_UNTRUSTED: cannot verify mode fence", file=sys.stderr)
        sys.exit(1)
