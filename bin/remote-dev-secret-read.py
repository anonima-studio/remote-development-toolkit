#!/usr/bin/env python3
"""Secret-owner reader. Stream/hash output is private transport, never diagnostics."""
import hashlib
import os
from pathlib import Path
import stat
import sys


def read(root, context, relative, role, action, output, guard, owner_only=False):
    home = Path.home().resolve()
    root, context = Path(root), Path(context)
    parts = relative.split("/")
    if (action not in ("metadata", "hash", "stream") or role not in ("runner", "development") or
            not root.is_absolute() or root != home and home not in root.parents or
            (home / "projects") not in context.parents or
            any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts) or
            "\\" in relative or any(ord(char) < 32 for char in relative)):
        raise RuntimeError("SECRET_SOURCE_INVALID: invalid source route")
    path = root / relative

    def validate():
        marker = home / ".local/share/remote-dev/workspace-role"
        if (marker.is_symlink() or not marker.is_file() or marker.read_bytes() != (role + "\n").encode()):
            raise RuntimeError("SECRET_SOURCE_ROLE: immutable source role differs")
        if any(not directory.is_dir() or directory.resolve() != directory for directory in (root, context, path.parent)):
            raise RuntimeError("SECRET_SOURCE_INVALID: source directories must be canonical")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_uid != os.getuid():
            raise RuntimeError("SECRET_SOURCE_INVALID: require a nonempty owned regular file")
        if owner_only and stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("SECRET_SOURCE_MODE: require mode 600")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)

    # Metadata preflight must neither open payloads nor create mutex state.
    if action == "metadata":
        guard.check(context)
        validate()
        return
    with guard.mutation_lock(context):
        before = validate()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_mode)
            if identity(info) != before:
                raise RuntimeError("SECRET_SOURCE_CHANGED: source changed before read")
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                if action == "stream":
                    output.write(chunk)
                else:
                    digest.update(chunk)
            if identity(os.fstat(source.fileno())) != before or validate() != before:
                raise RuntimeError("SECRET_SOURCE_CHANGED: source changed during read")
            if action == "hash":
                output.write(digest.hexdigest().encode())


def main():
    import types
    guard = types.ModuleType("mode_guard")
    exec(compile(os.environ["REMOTE_DEV_MODE_GUARD_SOURCE"], "mode-guard.py", "exec"), guard.__dict__)
    read(os.environ["REMOTE_DEV_SECRET_SOURCE_ROOT"], os.environ["REMOTE_DEV_SECRET_SOURCE_CONTEXT"],
         os.environ["REMOTE_DEV_SECRET_REL"], os.environ["REMOTE_DEV_SECRET_SOURCE_ROLE"],
         os.environ["REMOTE_DEV_SECRET_READ_ACTION"], sys.stdout.buffer, guard,
         os.environ.get("REMOTE_DEV_SECRET_OWNER_ONLY", "false") == "true")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError):
        # OSError names and unexpected exceptions must not expose input bytes.
        print("SECRET_READ_FAILED: source metadata, authority or stream changed", file=sys.stderr)
        sys.exit(1)
