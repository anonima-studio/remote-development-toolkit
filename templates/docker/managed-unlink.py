#!/usr/bin/env python3
"""Unlink managed projection leaves without following path-component symlinks."""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys


HEADER = "path\ttype\tmode\tdigest\tsymlink_target\tstate"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class UnsafeManagedPath(Exception):
    pass


def components(path: str, *, absolute: bool) -> list[str]:
    if absolute != path.startswith("/"):
        raise UnsafeManagedPath("path has the wrong root form")
    parts = path.split("/")
    if absolute:
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafeManagedPath("path contains an empty or traversal component")
    return parts


def open_root(path: str) -> int:
    current = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in components(path, absolute=True):
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def open_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = components(relative, absolute=False)
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, parts[-1]
    except BaseException:
        os.close(parent)
        raise


def validate_leaf(root_fd: int, relative: str) -> None:
    parent, leaf = open_parent(root_fd, relative)
    try:
        try:
            metadata = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            raise UnsafeManagedPath("managed leaf became a directory")
    finally:
        os.close(parent)


def unlink_leaf(root_fd: int, relative: str) -> None:
    parent, leaf = open_parent(root_fd, relative)
    try:
        try:
            metadata = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            raise UnsafeManagedPath("managed leaf became a directory")
        os.unlink(leaf, dir_fd=parent)
    finally:
        os.close(parent)


def parse_manifest() -> list[str]:
    lines = [line.rstrip("\n") for line in sys.stdin]
    if not lines or lines[0] != HEADER:
        raise UnsafeManagedPath("managed manifest header is invalid")
    paths: list[str] = []
    for line in lines[1:]:
        if "\r" in line:
            raise UnsafeManagedPath("managed manifest contains a carriage return")
        fields = line.split("\t")
        if len(fields) != 6:
            raise UnsafeManagedPath("managed manifest row is invalid")
        paths.append(fields[0])
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", help="absolute managed projection root")
    args = parser.parse_args()
    try:
        paths = parse_manifest()
        root_fd = open_root(args.root)
        try:
            for path in paths:
                validate_leaf(root_fd, path)
            for path in paths:
                unlink_leaf(root_fd, path)
        finally:
            os.close(root_fd)
    except (OSError, UnsafeManagedPath) as error:
        if isinstance(error, OSError):
            detail = errno.errorcode.get(error.errno or 0, "OS_ERROR")
        else:
            detail = str(error)
        print(f"MANAGED_UNLINK_REFUSED: {detail}", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
