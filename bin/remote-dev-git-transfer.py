#!/usr/bin/env python3
"""Lossless, bounded Git checkout transfer. Never write to the source repository."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
import zipfile


class TransferError(Exception):
    pass

SEMANTIC_CONFIG = {"core.filemode", "core.symlinks", "core.autocrlf", "core.eol", "core.ignorecase"}

def fail(code, detail):
    raise TransferError(f"{code}: {detail}")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def git(root, *args, data=None, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    command = ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
               "-C", str(root), *args]
    result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if check and result.returncode:
        # Git diagnostics may contain remote URLs or configuration values. Do not echo them.
        fail("GIT_FAILED", f"{args[0]} exited {result.returncode}")
    return result.stdout if check else result


def text(root, *args):
    return git(root, *args).decode("utf-8", "surrogateescape").rstrip("\n")


def nul_paths(data):
    return [os.fsdecode(value) for value in data.split(b"\0") if value]


def read_blobs(root, oids):
    """Read binary blobs in one Git process, independent of file count."""
    ordered = sorted(set(oids))
    raw = git(root, "cat-file", "--batch", data="".join(oid + "\n" for oid in ordered).encode())
    result, position = {}, 0
    for expected in ordered:
        end = raw.find(b"\n", position)
        header = raw[position:end].decode().split()
        if len(header) != 3 or header[:2] != [expected, "blob"]:
            fail("GIT_FAILED", "invalid object response during blob capture")
        size = int(header[2])
        position = end + 1
        result[expected] = raw[position:position + size]
        position += size + 1
    if position != len(raw):
        fail("GIT_FAILED", "incomplete blob capture")
    return result


def safe_path(value):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        fail("UNSAFE_PATH", "invalid relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.casefold() == ".git" for part in parts):
        fail("UNSAFE_PATH", "path escapes checkout or addresses Git metadata")
    if str(PurePosixPath(value)) != value:
        fail("UNSAFE_PATH", "non-canonical path")
    return value


def secret_path(value):
    for component in value.lower().split("/"):
        if component == ".env" or (component.startswith(".env.") and
                                  not component.endswith((".example", ".sample", ".template"))):
            return True
        if component in (".ssh", "credentials.json", "auth.json", "id_rsa", "id_ed25519"):
            return True
        if component.endswith((".key", ".pem", ".p12", ".pfx")):
            return True
    return False


def validate_paths(paths):
    names = {}
    for value in paths:
        safe_path(value)
        if secret_path(value):
            fail("SECRET_PATH", "source inventory contains a secret path; use the separate secrets flow")
        # Include directories: a/Foo and A/Bar also collide on a Mac filesystem.
        parts = value.split("/")
        for size in range(1, len(parts) + 1):
            name = "/".join(parts[:size])
            key = unicodedata.normalize("NFC", name).casefold()
            if key in names and names[key] != name:
                fail("PATH_COLLISION", "case or Unicode collision in source inventory")
            names[key] = name


def checked_file(root, relative):
    safe_path(relative)
    path = Path(root)
    for component in relative.split("/")[:-1]:
        path /= component
        if path.is_symlink():
            fail("UNSAFE_PATH", "ancestor is a symbolic link")
    return path / relative.split("/")[-1]


def read_entry(root, relative):
    path = checked_file(root, relative)
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return {"path": relative, "kind": "missing"}, None
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        if os.path.isabs(target) or "\\" in target:
            fail("UNSAFE_SYMLINK", "absolute or non-portable symbolic link")
        resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
        safe_path(resolved)
        payload = os.fsencode(target)
        kind, mode = "symlink", 0o777
    elif stat.S_ISREG(info.st_mode):
        # Open without following a concurrently substituted link.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                fail("SOURCE_CHANGED", "file identity changed during capture")
            payload = stream.read()
            final = os.fstat(stream.fileno())
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            fail("SOURCE_CHANGED", "file changed during capture")
        kind, mode = "file", 0o755 if info.st_mode & 0o111 else 0o644
    elif stat.S_ISDIR(info.st_mode):
        return {"path": relative, "kind": "directory"}, None
    else:
        fail("UNSUPPORTED_FILE", "special filesystem entry in source inventory")
    return {"path": relative, "kind": kind, "mode": mode, "sha256": digest(payload),
            "size": len(payload)}, payload


def repository_root(value):
    root = Path(value).resolve(strict=True)
    if not (root / ".git").is_dir() or (root / ".git").is_symlink():
        fail("UNSUPPORTED_GIT_STATE", "expected an ordinary checkout; linked worktrees are not supported")
    actual = Path(text(root, "rev-parse", "--show-toplevel")).resolve()
    if root != actual or Path(text(root, "rev-parse", "--absolute-git-dir")).resolve() != root / ".git":
        fail("UNSUPPORTED_GIT_STATE", "Git root or common directory differs")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge",
                   "rebase-apply", "sequencer", "index.lock", "shallow", "info/grafts"):
        if (root / ".git" / marker).exists():
            fail("UNSUPPORTED_GIT_STATE", f"unfinished or partial Git state ({marker})")
    if list((root / ".git").glob("sharedindex.*")):
        fail("UNSUPPORTED_GIT_STATE", "split index requires a separate supported import")
    if (root / ".git/objects/info/alternates").exists():
        fail("UNSUPPORTED_GIT_STATE", "external object storage is not supported")
    config = git(root, "config", "--local", "--list", "-z")
    pairs = [entry.split(b"\n", 1) for entry in config.split(b"\0") if entry]
    for key, *values in pairs:
        name = key.decode().lower()
        val = values[0].decode("utf-8", "surrogateescape") if values else ""
        if name.startswith(("filter.", "include.", "includeif.", "extensions.partialclone")):
            fail("UNSUPPORTED_GIT_STATE", "external filters, includes or partial clone configuration")
        if name in ("core.sparsecheckout", "core.splitindex") and val.lower() in ("true", "1", "yes"):
            fail("UNSUPPORTED_GIT_STATE", "sparse or split index")
    return root, pairs


def index_entries(root):
    entries = []
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        if stage != "0" or mode not in ("100644", "100755", "120000"):
            fail("UNSUPPORTED_GIT_STATE", "unmerged index or submodule")
        entries.append({"path": os.fsdecode(path), "mode": mode, "oid": oid})
    debug = git(root, "ls-files", "--debug", "-z")
    offset = 0
    metadata = re.compile(rb"  ctime: .*\n  mtime: .*\n  dev: .*\n  uid: .*\n  size: .*flags: ([0-9a-fA-F]+)\n")
    for entry in entries:
        prefix = os.fsencode(entry["path"]) + b"\0"
        if not debug.startswith(prefix, offset):
            fail("UNSUPPORTED_GIT_STATE", "cannot verify index flags")
        offset += len(prefix)
        match = metadata.match(debug, offset)
        if not match:
            fail("UNSUPPORTED_GIT_STATE", "cannot decode index flags")
        if int(match.group(1), 16) != 0:
            fail("UNSUPPORTED_GIT_STATE", "intent-to-add, assume-unchanged or extended index flags")
        offset = match.end()
    if offset != len(debug):
        fail("SOURCE_CHANGED", "index inventory changed during inspection")
    return entries


def configuration(pairs):
    result = []
    for key, *values in pairs:
        name = key.decode("utf-8", "surrogateescape")
        value = values[0].decode("utf-8", "surrogateescape") if values else ""
        if name in SEMANTIC_CONFIG:
            result.append([name, value])
            continue
        if name.startswith(("remote.", "branch.")):
            if name.endswith((".url", ".pushurl")):
                if "://" in value:
                    parsed = urlsplit(value)
                    if parsed.password is not None or (parsed.username and parsed.scheme != "ssh") or parsed.query:
                        fail("SECRET_REMOTE", "remote URL contains credentials or query parameters")
                if "\n" in value or "\r" in value:
                    fail("UNSAFE_REMOTE", "remote URL is not a single line")
            if name.endswith((".url", ".pushurl", ".fetch", ".remote", ".merge", ".rebase", ".pushremote")):
                result.append([name, value])
    # Stable sorting groups keys without changing each key's value precedence.
    return sorted(result, key=lambda pair: pair[0])


def inspect(value, allowed=()):
    root, pairs = repository_root(value)
    head = text(root, "rev-parse", "--verify", "HEAD")
    branch_result = git(root, "symbolic-ref", "--quiet", "HEAD", check=False)
    branch = os.fsdecode(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    index = index_entries(root)
    tree = []
    for entry in git(root, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            if kind != "blob":
                fail("UNSUPPORTED_GIT_STATE", "submodule in HEAD")
            tree.append({"path": os.fsdecode(path), "mode": mode, "oid": oid})
    untracked = set(nul_paths(git(root, "ls-files", "--others", "--exclude-standard", "-z")))
    allowed = sorted(set(safe_path(path) for path in allowed))
    selected = sorted(path for path in untracked if any(path == scope or path.startswith(scope + "/") for scope in allowed))
    all_paths = sorted({entry["path"] for entry in index + tree} | set(selected))
    if untracked.intersection(all_paths).difference(selected):
        fail("UNTRACKED_REVIEW_REQUIRED", "a former tracked path remains untracked; explicitly include it before transfer")
    validate_paths(all_paths)
    if all_paths:
        attrs = git(root, "check-attr", "-z", "--stdin", "filter", "working-tree-encoding",
                    data=b"".join(os.fsencode(path) + b"\0" for path in all_paths)).split(b"\0")
        if any(attrs[i] not in (b"unspecified", b"unset") for i in range(2, len(attrs) - 1, 3)):
            fail("UNSUPPORTED_GIT_STATE", "filter or working-tree encoding requires an explicit import adapter")
    refs = []
    for row in git(root, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)").splitlines():
        name, oid, symbolic = row.decode().split("\0")
        if name.startswith("refs/replace/"):
            fail("UNSUPPORTED_GIT_STATE", "replace refs are not supported")
        refs.append({"name": name, "oid": oid, "symbolic": symbolic or None})
    worktree = [read_entry(root, path)[0] for path in all_paths]
    result = {"version": 1, "head": head, "branch": branch, "refs": refs,
              "object_format": text(root, "rev-parse", "--show-object-format"),
              "config": configuration(pairs), "index": index, "head_tree": tree,
              "worktree": worktree, "selected_untracked": selected,
              "staged_raw_sha256": digest(git(root, "diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv", "-z")),
              "unstaged_raw_sha256": digest(git(root, "diff", "--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv", "-z"))}
    result["snapshot_id"] = digest(encoded(result))
    return result


@contextmanager
def export_file(root, output):
    # Verify the physical parent, then create relative to a pinned directory FD.
    # A substituted ancestor cannot redirect the file creation through a symlink.
    parent = output.parent
    if parent.resolve(strict=True) != parent or output.is_relative_to(root):
        fail("UNSAFE_PATH", "package destination must be canonical and outside the source checkout")
    parent_info = parent.stat()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pinned = os.fstat(parent_fd)
        if (pinned.st_dev, pinned.st_ino) != (parent_info.st_dev, parent_info.st_ino):
            fail("SOURCE_CHANGED", "package parent changed before creation")
        descriptor = os.open(output.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, "w+b") as stream:
            yield stream
            final = output.lstat()
            if parent.resolve(strict=True) != parent or (final.st_dev, final.st_ino) != (
                    os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino):
                fail("SOURCE_CHANGED", "package path changed during export")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(parent_fd)


def export(value, output, allowed=(), expected=None):
    root = Path(value).resolve(strict=True)
    before = inspect(root, allowed)
    if expected and before["snapshot_id"] != expected:
        fail("SOURCE_CHANGED", "source no longer matches the reviewed plan")
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        fail("TARGET_EXISTS", "export destination must be absent")
    if output.parent.resolve(strict=True) != output.parent or output.is_relative_to(root):
        fail("UNSAFE_PATH", "transfer package must be outside source checkout")
    with tempfile.TemporaryDirectory(prefix="remote-dev-git-") as temporary, export_file(root, output) as package_stream:
        bundle = Path(temporary) / "history.bundle"
        git(root, "bundle", "create", str(bundle), "--all", "HEAD")
        git(root, "bundle", "verify", str(bundle))
        captured_blobs = read_blobs(root, [entry["oid"] for entry in before["index"] + before["head_tree"]])
        with zipfile.ZipFile(package_stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.write(bundle, "history.bundle")
            blobs = {}
            for entry in before["index"]:
                if entry["oid"] not in blobs:
                    payload = captured_blobs[entry["oid"]]
                    blobs[entry["oid"]] = digest(payload)
                    archive.writestr("index/" + entry["oid"], payload)
            head_files = {entry["path"]: entry for entry in before["head_tree"]}
            overlay = []
            written = set()
            for entry in before["worktree"]:
                current, payload = read_entry(root, entry["path"])
                if current != entry:
                    fail("SOURCE_CHANGED", "working tree changed during export")
                if payload is None:
                    continue
                baseline = head_files.get(entry["path"])
                if baseline and payload == captured_blobs[baseline["oid"]]:
                    continue
                overlay.append(entry["path"])
                member = "worktree/" + entry["sha256"]
                if member not in written:
                    archive.writestr(member, payload)
                    written.add(member)
            package = {"snapshot": before, "overlay": overlay, "index_blobs": blobs,
                       "bundle_sha256": digest(bundle.read_bytes())}
            archive.writestr("manifest.json", encoded(package))
        after = inspect(root, allowed)
        if before != after:
            fail("SOURCE_CHANGED", "source changed during export; no package accepted")
    return {"snapshot_id": before["snapshot_id"], "package": str(output),
            "sha256": digest(output.read_bytes()), "selected_untracked": before["selected_untracked"]}


def validate_snapshot(snapshot):
    if snapshot.get("version") != 1 or snapshot.get("object_format") not in ("sha1", "sha256"):
        fail("INVALID_PACKAGE", "unsupported snapshot format")
    identity = dict(snapshot)
    expected = identity.pop("snapshot_id", None)
    if digest(encoded(identity)) != expected:
        fail("INVALID_PACKAGE", "snapshot checksum differs")
    paths = [entry["path"] for entry in snapshot["worktree"]]
    validate_paths(paths)
    if len(paths) != len(set(paths)):
        fail("INVALID_PACKAGE", "duplicate working-tree entries")
    for entry in snapshot["index"] + snapshot["head_tree"]:
        safe_path(entry["path"])
        if entry["mode"] not in ("100644", "100755", "120000") or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", entry["oid"]):
            fail("INVALID_PACKAGE", "invalid index or tree entry")
    for entry in snapshot["worktree"]:
        if entry["kind"] not in ("file", "symlink", "missing", "directory"):
            fail("INVALID_PACKAGE", "unsupported filesystem kind")
        if entry["kind"] in ("file", "symlink") and (entry["mode"] not in (0o644, 0o755, 0o777) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            fail("INVALID_PACKAGE", "invalid file metadata")
    # Only allow the exact inert Git config inventory accepted at capture.
    pairs = [[os.fsencode(k), os.fsencode(v)] for k, v in snapshot["config"]]
    if configuration(pairs) != snapshot["config"]:
        fail("INVALID_PACKAGE", "unsafe Git configuration")


def restore(package_path, destination, expected_package=None):
    package_path = Path(package_path)
    if expected_package and digest(package_path.read_bytes()) != expected_package:
        fail("INVALID_PACKAGE", "transport checksum differs")
    destination = Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        fail("TARGET_EXISTS", "restore requires an empty destination")
    if destination.parent.resolve() != destination.parent:
        fail("UNSAFE_PATH", "destination parent must be canonical")
    with zipfile.ZipFile(package_path) as archive, tempfile.TemporaryDirectory(prefix="remote-dev-restore-") as temp:
        names = archive.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            fail("INVALID_PACKAGE", "duplicate or missing archive entry")
        manifest = json.loads(archive.read("manifest.json"))
        snapshot = manifest["snapshot"]
        validate_snapshot(snapshot)
        if set(manifest["index_blobs"]) != {entry["oid"] for entry in snapshot["index"]}:
            fail("INVALID_PACKAGE", "index payload inventory differs")
        if any(not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in manifest["index_blobs"].values()):
            fail("INVALID_PACKAGE", "invalid blob digest")
        if len(manifest["overlay"]) != len(set(manifest["overlay"])):
            fail("INVALID_PACKAGE", "duplicate overlay path")
        expected_names = {"history.bundle", "manifest.json"}
        expected_names.update("index/" + oid for oid in manifest["index_blobs"])
        worktree = {entry["path"]: entry for entry in snapshot["worktree"]}
        for path in manifest["overlay"]:
            safe_path(path)
            if path not in worktree or worktree[path]["kind"] not in ("file", "symlink"):
                fail("INVALID_PACKAGE", "overlay does not match inventory")
            expected_names.add("worktree/" + worktree[path]["sha256"])
        if set(names) != expected_names:
            fail("INVALID_PACKAGE", "unexpected archive members")
        bundle_data = archive.read("history.bundle")
        if digest(bundle_data) != manifest["bundle_sha256"]:
            fail("INVALID_PACKAGE", "bundle checksum differs")
        for oid, sha in manifest["index_blobs"].items():
            if digest(archive.read("index/" + oid)) != sha:
                fail("INVALID_PACKAGE", "index blob checksum differs")
        for path in manifest["overlay"]:
            entry = worktree[path]
            if digest(archive.read("worktree/" + entry["sha256"])) != entry["sha256"]:
                fail("INVALID_PACKAGE", "working-tree checksum differs")
        destination.mkdir(mode=0o700, exist_ok=True)
        git(destination, "init", "--quiet", "--object-format=" + snapshot["object_format"])
        import_ref = "refs/heads/remote-dev-import-" + snapshot["snapshot_id"][:16]
        occupied = {entry["name"] for entry in snapshot["refs"]}
        while import_ref in occupied:
            import_ref += "-unused"
        git(destination, "symbolic-ref", "HEAD", import_ref)
        bundle = Path(temp) / "history.bundle"
        bundle.write_bytes(bundle_data)
        git(destination, "bundle", "verify", str(bundle))
        # Detached HEAD may not be reachable from any named ref.
        git(destination, "fetch", "--quiet", "--no-tags", str(bundle), "+refs/*:refs/*", "HEAD")
        git(destination, "checkout", "--quiet", "--detach", snapshot["head"])
        for entry in snapshot["refs"]:
            if entry["symbolic"]:
                git(destination, "symbolic-ref", entry["name"], entry["symbolic"])
        if snapshot["branch"]:
            git(destination, "symbolic-ref", "HEAD", snapshot["branch"])
        for key in SEMANTIC_CONFIG | {key for key, _ in snapshot["config"]}:
            git(destination, "config", "--local", "--unset-all", key, check=False)
        for key, value in snapshot["config"]:
            git(destination, "config", "--local", "--add", key, value)
        object_paths = []
        for oid in manifest["index_blobs"]:
            object_path = Path(temp) / oid
            object_path.write_bytes(archive.read("index/" + oid))
            object_paths.append(object_path)
        actual_objects = git(destination, "hash-object", "-w", "--no-filters", "--stdin-paths",
                             data=b"".join(os.fsencode(path) + b"\n" for path in object_paths)).decode().splitlines()
        if actual_objects != list(manifest["index_blobs"]):
            fail("INVALID_PACKAGE", "Git index object hash differs")
        git(destination, "read-tree", "--empty")
        index_input = b"".join(f'{entry["mode"]} {entry["oid"]}\t'.encode() + os.fsencode(entry["path"]) + b"\0"
                               for entry in snapshot["index"])
        git(destination, "update-index", "-z", "--index-info", data=index_input)
        # Remove only enumerated HEAD files from this new, private checkout, before
        # rebuilding file/directory transitions. Never recurse through a symlink.
        for entry in sorted(snapshot["head_tree"], key=lambda item: item["path"].count("/"), reverse=True):
            wanted = worktree.get(entry["path"])
            if wanted and wanted["kind"] == "file" and entry["path"] not in manifest["overlay"]:
                continue
            path = checked_file(destination, entry["path"])
            if path.is_file() or path.is_symlink():
                path.unlink()
        directories = {str(parent) for entry in snapshot["head_tree"]
                       for parent in PurePosixPath(entry["path"]).parents if str(parent) != "."}
        for relative in sorted(directories, key=lambda value: value.count("/"), reverse=True):
            path = checked_file(destination, relative)
            if path.is_dir() and not path.is_symlink():
                try:
                    path.rmdir()
                except OSError:
                    pass  # Keep all nonempty and unrecognized content.
        head_files = {entry["path"]: entry for entry in snapshot["head_tree"]}
        baseline_blobs = read_blobs(destination, [entry["oid"] for entry in snapshot["head_tree"]])
        for entry in sorted(snapshot["worktree"], key=lambda item: item["path"].count("/")):
            path = checked_file(destination, entry["path"])
            if entry["kind"] in ("missing", "directory"):
                if entry["kind"] == "directory":
                    path.mkdir(parents=True, exist_ok=True)
                continue
            if path.is_dir() and not path.is_symlink():
                try:
                    path.rmdir()
                except OSError:
                    fail("INVALID_PACKAGE", "file collides with a nonempty directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            if entry["path"] in manifest["overlay"]:
                payload = archive.read("worktree/" + entry["sha256"])
            else:
                baseline = head_files.get(entry["path"])
                if baseline is None:
                    fail("INVALID_PACKAGE", "missing file payload")
                payload = baseline_blobs[baseline["oid"]]
            if path.exists() or path.is_symlink():
                path.unlink()
            if entry["kind"] == "symlink":
                target = os.fsdecode(payload)
                if os.path.isabs(target):
                    fail("UNSAFE_SYMLINK", "absolute symbolic link")
                safe_path(os.path.normpath(os.path.join(os.path.dirname(entry["path"]), target)))
                os.symlink(target, path)
            else:
                with path.open("xb") as stream:
                    stream.write(payload)
                path.chmod(entry["mode"])
        actual = inspect(destination, snapshot["selected_untracked"])
        if actual != snapshot:
            fail("PARITY_FAILED", "restored checkout does not match source snapshot; keep source and candidate")
    return {"snapshot_id": snapshot["snapshot_id"], "root": str(destination), "parity": "verified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "export"):
        item = sub.add_parser(name)
        item.add_argument("root")
        item.add_argument("--allow-untracked", action="append", default=[])
        if name == "export":
            item.add_argument("output")
            item.add_argument("--expected-snapshot")
    item = sub.add_parser("restore")
    item.add_argument("package")
    item.add_argument("destination")
    item.add_argument("--expected-package")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect(args.root, args.allow_untracked)
        elif args.command == "export":
            result = export(args.root, args.output, args.allow_untracked, args.expected_snapshot)
        else:
            result = restore(args.package, args.destination, args.expected_package)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    except (TransferError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        if isinstance(error, TransferError):
            print(str(error), file=sys.stderr)
        else:
            print(f"TRANSFER_FAILED: {type(error).__name__}; source remains authoritative", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
