#!/usr/bin/env python3
"""Owner-side single-file publication for remote-dev-secrets; stdin is the payload."""
import os
from pathlib import Path
import stat
import sys
import secrets


def upload(root, relative, replace, source, guard):
    root = Path(root)
    parts = relative.split("/")
    if (not root.is_absolute() or (Path.home().resolve() / "projects") not in root.parents or
            any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts) or "\\" in relative or
            any(ord(char) < 32 for char in relative)):
        raise RuntimeError("SECRET_PATH_INVALID: require a canonical project file route")
    destination = root / relative
    parent = destination.parent

    def validate():
        if (root.resolve() != root or not root.is_dir() or parent.resolve() != parent or
                not parent.is_dir() or destination.is_symlink() or
                destination.exists() and not destination.is_file()):
            raise RuntimeError("SECRET_PATH_INVALID: destination must be a regular file in a canonical existing directory")
        if not replace and destination.exists():
            raise RuntimeError("SECRET_EXISTS: review the destination before using --replace")
        return parent.stat().st_dev, parent.stat().st_ino

    with guard.mutation_lock(root):
        parent_identity = validate()
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = None
        try:
            if (os.fstat(directory).st_dev, os.fstat(directory).st_ino) != parent_identity:
                raise RuntimeError("SECRET_PATH_CHANGED: destination directory moved")
            name = ".remote-dev-secret." + secrets.token_hex(16)
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            temporary = name
            with os.fdopen(descriptor, "wb") as output:
                size = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(chunk)
                    size += len(chunk)
                if not size:
                    raise RuntimeError("SECRET_EMPTY: refusing an empty payload")
                output.flush()
                os.fsync(output.fileno())
                info = os.fstat(output.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise RuntimeError("SECRET_PERMISSIONS: staging file must be owner-only")
            if validate() != parent_identity:
                raise RuntimeError("SECRET_PATH_CHANGED: destination directory moved")
            if replace:
                os.replace(name, destination.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                # Atomic no-clobber publication, including a concurrent creation.
                os.link(name, destination.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                os.unlink(name, dir_fd=directory)
            temporary = None
            os.fsync(directory)
        finally:
            if temporary is not None:
                try:
                    os.unlink(Path(temporary).name, dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.close(directory)


def main():
    import types
    guard = types.ModuleType("mode_guard")
    exec(compile(os.environ["REMOTE_DEV_MODE_GUARD_SOURCE"], "mode-guard.py", "exec"), guard.__dict__)
    replacement = os.environ["REMOTE_DEV_SECRET_REPLACE"]
    if replacement not in ("true", "false"):
        raise RuntimeError("SECRET_ARGUMENT_INVALID: replacement must be explicit")
    upload(os.environ["REMOTE_DEV_SECRET_ROOT"], os.environ["REMOTE_DEV_SECRET_REL"],
           replacement == "true", sys.stdin.buffer, guard)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(str(error) if isinstance(error, RuntimeError) else "SECRET_UPLOAD_FAILED: preserve the operation and inspect destination metadata", file=sys.stderr)
        sys.exit(1)
