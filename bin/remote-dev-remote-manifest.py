#!/usr/bin/env python3
"""remote_manifest_audit: hash a runner projection in one process.

Sent over the existing authenticated transport; no workspace image update is
required. stdin contains the expected TSV manifest, argv contains trusted scopes.
"""
import hashlib
import contextlib
import gzip
import io
import os
import stat
import sys


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fields(path, kind, info):
    if kind == "symlink":
        target = os.readlink(path)
        if any(char in target for char in "\t\r\n"):
            raise ValueError("REMOTE_MANIFEST_INVALID_SYMLINK")
        return "0777", hashlib.sha256(os.fsencode(target)).hexdigest(), target
    return "%04o" % stat.S_IMODE(info.st_mode), digest_file(path), "-"


def emit(row):
    print("\t".join(row))


def audit():
    root = sys.argv[1]
    count = int(sys.argv[2])
    standard = sys.argv[3:3 + count]
    allowed = sys.argv[3 + count:]
    rows = [line.rstrip("\n").split("\t") for line in sys.stdin]
    header = ["path", "type", "mode", "digest", "symlink_target", "state"]
    if not rows or rows[0] != header:
        raise ValueError("REMOTE_MANIFEST_INVALID_HEADER")
    managed = set()
    parents = set()
    emit(header)
    for row in rows[1:]:
        if len(row) != 6:
            raise ValueError("REMOTE_MANIFEST_INVALID_ROW")
        relative, expected_type, _, _, _, state = row
        if (relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/"))
                or any(char in relative for char in "\t\r\n")):
            raise ValueError("REMOTE_MANIFEST_INVALID_PATH")
        managed.add(relative)
        parent = os.path.dirname(relative)
        while parent:
            parents.add(parent)
            parent = os.path.dirname(parent)
        candidate = os.path.join(root, relative)
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            info = None
        if state == "deleted" and info is not None:
            emit([relative, "unexpected", "0000", "-", "-", "present"])
        elif state != "deleted" and info is not None and (
            (expected_type == "file" and stat.S_ISREG(info.st_mode))
            or (expected_type == "symlink" and stat.S_ISLNK(info.st_mode))
        ):
            emit([relative, expected_type, *fields(candidate, expected_type, info), "present"])
        else:
            emit([relative, "missing", "0000", "-", "-", "deleted"])

    def standard_scope(relative):
        return any("/" + scope + "/" in "/" + relative + "/" for scope in standard)

    def visit(directory):
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            relative = os.path.relpath(entry.path, root)
            if any(char in relative for char in "\t\r\n"):
                emit(["__INVALID_REMOTE_PATH__", "unexpected", "0000", "-", "-", "present"])
                continue
            if standard_scope(relative):
                continue
            is_directory = entry.is_dir(follow_symlinks=False)
            if is_directory:
                with os.scandir(entry.path) as scan:
                    empty = next(scan, None) is None
                if not empty:
                    visit(entry.path)
                    continue
            if relative in managed or relative in parents:
                continue
            if any(relative == scope or relative.startswith(scope + "/") for scope in allowed):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                emit([relative, "unexpected-symlink", *fields(entry.path, "symlink", info), "present"])
            elif stat.S_ISREG(info.st_mode):
                emit([relative, "unexpected-file", *fields(entry.path, "file", info), "present"])
            elif is_directory:
                emit([relative, "unexpected-directory", "0000", "-", "-", "present"])
    visit(root)


def main():
    if sys.argv[1:2] != ["--compressed-match"]:
        audit()
        return
    del sys.argv[1]
    expected = gzip.decompress(sys.stdin.buffer.read()).decode("utf-8")
    sys.stdin = io.StringIO(expected)
    actual = io.StringIO()
    with contextlib.redirect_stdout(actual):
        audit()
    # This acknowledgement follows a complete fresh audit, not cached metadata.
    # Return the full mismatch for the existing local drift evidence path.
    output = actual.getvalue()
    sys.stdout.write("REMOTE_MANIFEST_MATCH\n" if output == expected else output)


if __name__ == "__main__":
    main()
