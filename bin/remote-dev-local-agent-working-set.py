#!/usr/bin/env python3
"""Normalize, resolve, and materialize Local Agent multi-repo working sets."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


POLICY_KINDS = {"allow_untracked", "exclude_tracked", "remote_writable"}
SNAPSHOT_POLICY_KINDS = {
    "allow_untracked": "allowed_untracked",
    "exclude_tracked": "tracked_exclusion",
    "remote_writable": "remote_writable",
}
GIT_METADATA_MODES = {"not-required", "explicit-input", "required"}
STANDARD_EXCLUDES = (
    "node_modules",
    ".pnpm-store",
    ".yarn/cache",
    ".cache",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".output",
    "target",
)


class WorkingSetError(Exception):
    pass


def fail(message: str) -> None:
    raise WorkingSetError(message)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_token(value: str, label: str) -> str:
    if not value or any(character in value for character in "\t\r\n"):
        fail(f"WORKING_SET_INVALID: {label} must be non-empty single-line text")
    return value


def validate_relative(value: str, label: str, *, allow_dot: bool = False) -> str:
    validate_token(value, label)
    if value == "." and allow_dot:
        return value
    if value.startswith("/") or value.endswith("/") or "//" in value:
        fail(f"WORKING_SET_INVALID: {label} must be a normalized relative path: {value}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail(f"WORKING_SET_INVALID: {label} contains an unsafe component: {value}")
    if parts[0] == ".git":
        fail(f"WORKING_SET_INVALID: {label} must not select Git metadata: {value}")
    return value


def canonical_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        fail(f"WORKING_SET_INVALID: {label} must resolve to an absolute path: {path}")
    if path.is_symlink() or not path.is_dir():
        fail(f"WORKING_SET_INVALID: {label} must be a real directory: {path}")
    return path.resolve(strict=True)


def git(root: Path, *arguments: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        fail(f"WORKING_SET_GIT_FAILED: git -C {root} {' '.join(arguments)}: {detail}")
    return result.stdout if binary else result.stdout.decode("utf-8")


def exact_git_root(path: Path, label: str) -> Path:
    physical = canonical_directory(path, label)
    discovered = Path(str(git(physical, "rev-parse", "--show-toplevel")).strip()).resolve(strict=True)
    if physical != discovered:
        fail(f"WORKING_SET_INVALID: {label} must be the exact Git root: {discovered}")
    return physical


def intersects(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def within_standard_scope(path: str, scope: str) -> bool:
    framed_path = f"/{path}/"
    framed_scope = f"/{scope}/"
    return path == scope or path.startswith(f"{scope}/") or framed_scope in framed_path


def parse_manifest(manifest: Path) -> dict:
    if not manifest.is_absolute():
        fail("WORKING_SET_INVALID: manifest path must be absolute")
    if manifest.is_symlink() or not manifest.is_file():
        fail(f"WORKING_SET_INVALID: manifest must be a regular non-symlink file: {manifest}")
    manifest = manifest.resolve(strict=True)
    raw = manifest.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("WORKING_SET_INVALID: manifest must be UTF-8")

    schema = None
    anchor_value = None
    execution_member = None
    members: dict[str, str] = {}
    metadata: dict[str, str] = {}
    policies: list[tuple[str, str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        key = fields[0]
        if key == "schema" and len(fields) == 2 and schema is None:
            schema = fields[1]
        elif key == "anchor" and len(fields) == 2 and anchor_value is None:
            anchor_value = validate_token(fields[1], "anchor")
        elif key == "execution_member" and len(fields) == 2 and execution_member is None:
            execution_member = validate_token(fields[1], "execution_member")
        elif key == "member" and len(fields) == 3:
            member_id = validate_token(fields[1], "member ID")
            if member_id in members:
                fail(f"WORKING_SET_INVALID: duplicate member ID {member_id}")
            members[member_id] = validate_relative(fields[2], f"member {member_id} path")
        elif key == "git_metadata" and len(fields) == 3:
            member_id = validate_token(fields[1], "git_metadata member")
            if member_id in metadata:
                fail(f"WORKING_SET_INVALID: duplicate git_metadata for {member_id}")
            if fields[2] not in GIT_METADATA_MODES:
                fail(f"WORKING_SET_INVALID: invalid git_metadata mode for {member_id}: {fields[2]}")
            metadata[member_id] = fields[2]
        elif key in POLICY_KINDS and len(fields) == 3:
            member_id = validate_token(fields[1], f"{key} member")
            policies.append((key, member_id, validate_relative(fields[2], f"{key} path")))
        else:
            fail(f"WORKING_SET_INVALID: invalid row at line {number}")

    if schema != "1":
        fail("WORKING_SET_INVALID: schema must be 1")
    if anchor_value is None or execution_member is None or not members:
        fail("WORKING_SET_INVALID: anchor, execution_member, and at least one member are required")
    if execution_member not in members:
        fail(f"WORKING_SET_INVALID: execution_member is unknown: {execution_member}")
    unknown_metadata = set(metadata) - set(members)
    if unknown_metadata:
        fail(f"WORKING_SET_INVALID: git_metadata references unknown member: {sorted(unknown_metadata)[0]}")
    for _, member_id, _ in policies:
        if member_id not in members:
            fail(f"WORKING_SET_INVALID: policy references unknown member: {member_id}")

    anchor = canonical_directory((manifest.parent / anchor_value).resolve(), "working-set anchor")
    normalized_members = []
    for member_id, relative in members.items():
        root = exact_git_root(anchor / relative, f"member {member_id}")
        try:
            root.relative_to(anchor)
        except ValueError:
            fail(f"WORKING_SET_INVALID: member {member_id} escapes anchor {anchor}")
        mode = metadata.get(member_id, "not-required")
        if mode == "required":
            fail(f"UNSUPPORTED_GIT_METADATA: member {member_id} requires Git metadata")
        normalized_members.append((member_id, root, relative, mode))

    for index, first in enumerate(normalized_members):
        for second in normalized_members[index + 1 :]:
            first_root, second_root = first[1], second[1]
            if first_root == second_root or first_root in second_root.parents or second_root in first_root.parents:
                fail(
                    "WORKING_SET_MEMBER_OVERLAP: member roots must be disjoint: "
                    f"{first[0]}={first_root} <> {second[0]}={second_root}"
                )
            if intersects(first[2], second[2]):
                fail(
                    "WORKING_SET_REMOTE_OVERLAP: member remote paths must be disjoint: "
                    f"{first[0]}={first[2]} <> {second[0]}={second[2]}"
                )

    execution_root = next(item[1] for item in normalized_members if item[0] == execution_member)
    try:
        manifest_relative = manifest.relative_to(execution_root).as_posix()
    except ValueError:
        fail("WORKING_SET_INVALID: manifest must be owned by the execution-member repository")
    result = subprocess.run(
        ["git", "-C", str(execution_root), "ls-files", "--error-unmatch", "--", manifest_relative],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        fail("WORKING_SET_INVALID: manifest must be Git-tracked by the execution-member repository")

    semantic = [f"schema\t1", f"anchor\t{anchor}", f"execution_member\t{execution_member}"]
    for member_id, root, relative, mode in sorted(normalized_members, key=lambda item: item[0].encode()):
        semantic.append(f"member\t{member_id}\t{root}\t{relative}\t{mode}")
    for kind, member_id, relative in sorted(set(policies), key=lambda item: tuple(part.encode() for part in item)):
        semantic.append(f"policy\t{SNAPSHOT_POLICY_KINDS[kind]}\t{member_id}\t{relative}")
    topology_digest = digest_bytes(("\n".join(semantic) + "\n").encode())
    return {
        "manifest": manifest,
        "manifest_digest": digest_bytes(raw),
        "anchor": anchor,
        "execution_member": execution_member,
        "members": sorted(normalized_members, key=lambda item: item[0].encode()),
        "policies": sorted(set(policies), key=lambda item: tuple(part.encode() for part in item)),
        "topology_digest": topology_digest,
    }


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".working-set.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def snapshot_bytes(topology: dict) -> bytes:
    rows = [
        "schema\t1",
        f"manifest_path\t{topology['manifest']}",
        f"manifest_digest\t{topology['manifest_digest']}",
        f"topology_digest\t{topology['topology_digest']}",
        f"anchor\t{topology['anchor']}",
        f"execution_member\t{topology['execution_member']}",
    ]
    for member_id, root, relative, mode in topology["members"]:
        rows.append(f"member\t{member_id}\t{root}\t{relative}\t{mode}")
    return ("\n".join(rows) + "\n").encode()


def policy_bytes(topology: dict) -> bytes:
    rows = [
        f"{SNAPSHOT_POLICY_KINDS[kind]}\t{member_id}\t{relative}"
        for kind, member_id, relative in topology["policies"]
    ]
    return (("\n".join(rows) + "\n") if rows else "").encode()


def load_snapshot(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        fail(f"WORKING_SET_SNAPSHOT_INVALID: {path}")
    scalar: dict[str, str] = {}
    members = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("\t")
        if fields[0] == "member" and len(fields) == 5:
            members.append((fields[1], Path(fields[2]), fields[3], fields[4]))
        elif len(fields) == 2 and fields[0] not in scalar:
            scalar[fields[0]] = fields[1]
        else:
            fail(f"WORKING_SET_SNAPSHOT_INVALID: line {number}")
    required = {"schema", "manifest_path", "manifest_digest", "topology_digest", "anchor", "execution_member"}
    if scalar.get("schema") != "1" or not required.issubset(scalar) or not members:
        fail("WORKING_SET_SNAPSHOT_INVALID: missing required fields")
    return {**scalar, "members": members}


def verify_snapshot(snapshot: dict) -> None:
    current = parse_manifest(Path(snapshot["manifest_path"]))
    if current["topology_digest"] != snapshot["topology_digest"]:
        fail(
            "WORKING_SET_TOPOLOGY_DRIFT: normalized working-set topology changed; "
            "run exact local-agent down using the stored binding, then rebind"
        )


def decode_nul(data: bytes) -> list[str]:
    if data.endswith(b"\0"):
        data = data[:-1]
    if not data:
        return []
    try:
        return [item.decode("utf-8") for item in data.split(b"\0")]
    except UnicodeDecodeError:
        fail("PROJECTION_PATH_INVALID: non-UTF-8 Git path")


def load_policy(path: Path) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    if path.is_symlink() or not path.is_file():
        fail(f"PROJECTION_POLICY_INVALID: {path}")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("\t")
        if len(fields) != 3 or fields[0] not in SNAPSHOT_POLICY_KINDS.values():
            fail(f"PROJECTION_POLICY_INVALID: line {number}")
        validate_token(fields[1], "policy member")
        validate_relative(fields[2], "policy path")
        result.setdefault((fields[0], fields[1]), []).append(fields[2])
    return result


def load_single_root_policy(path: Path) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    if path.is_symlink() or not path.is_file():
        fail(f"PROJECTION_POLICY_INVALID: {path}")
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {*SNAPSHOT_POLICY_KINDS.values(), "allowed_untracked_file"}:
            fail(f"PROJECTION_POLICY_INVALID: line {number}")
        validate_relative(fields[1], "policy path")
        result.setdefault((fields[0], "root"), []).append(fields[1])
    return result


def exact_untracked_files(root: Path, paths: list[str]) -> set[str]:
    """Keep additive grants file-only across deletion, recreation and staging."""
    files = set()
    for relative in paths:
        validate_relative(relative, "exact untracked file")
        current = root
        parts = relative.split("/")
        for index, part in enumerate(parts):
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                # Absence removes source from the projection but not its
                # durable file-only grant. A later directory must still fail.
                break
            expected_type = stat.S_ISDIR if index < len(parts) - 1 else stat.S_ISREG
            if not expected_type(info.st_mode):
                fail(f"PROJECTION_EXACT_FILE_INVALID: {relative}")
        else:
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", "--", relative],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if ignored.returncode not in (0, 1):
                fail(f"PROJECTION_POLICY_INVALID: cannot check ignored path: {relative}")
            if ignored.returncode == 0:
                fail(f"PROJECTION_POLICY_INVALID: exact untracked file is ignored: {relative}")
            candidates = decode_nul(git(root, "--literal-pathspecs", "ls-files", "--others",
                                       "--exclude-standard", "-z", "--", relative, binary=True))
            if candidates not in ([], [relative]):
                fail(f"PROJECTION_EXACT_FILE_INVALID: {relative}")
            files.update(candidates)
    return files


def source_inventory(snapshot: dict, policy: dict) -> tuple[list[tuple[str, str, str, str, str, str]], list[tuple[str, str]], dict[str, str]]:
    managed_rows = []
    excluded_rows = set()
    member_rows: dict[str, list[tuple[str, str, str, str, str, str]]] = {}
    aggregate_case: dict[str, str] = {}
    for member_id, root, remote_relative, _ in snapshot["members"]:
        stage_rows = str(git(root, "ls-files", "--stage"))
        if any(line.startswith("160000 ") for line in stage_rows.splitlines()):
            fail(f"UNSUPPORTED_GIT_METADATA: member {member_id} contains a Git submodule")
        tracked = sorted(set(decode_nul(git(root, "ls-files", "-z", binary=True))), key=lambda value: value.encode())
        exact_files = policy.get(("allowed_untracked_file", member_id), [])
        untracked = exact_untracked_files(root, exact_files)
        for allowed in policy.get(("allowed_untracked", member_id), []):
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", "--", allowed],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if ignored.returncode == 0:
                fail(f"PROJECTION_POLICY_INVALID: allowed untracked path is ignored: {member_id}:{allowed}")
            untracked.update(decode_nul(git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", allowed, binary=True)))
        ignored = sorted(
            set(decode_nul(git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "--no-empty-directory", "-z", binary=True))),
            key=lambda value: value.encode(),
        )
        exclusions = policy.get(("tracked_exclusion", member_id), [])
        remote_writable = policy.get(("remote_writable", member_id), [])
        candidates = []
        for relative in tracked:
            validate_relative(relative, "tracked path")
            if not any(intersects(relative, excluded) for excluded in exclusions) and not any(
                within_standard_scope(relative, standard) for standard in STANDARD_EXCLUDES
            ):
                candidates.append((relative, "tracked"))
        for relative in sorted(untracked, key=lambda value: value.encode()):
            validate_relative(relative, "untracked path")
            if not any(within_standard_scope(relative, standard) for standard in STANDARD_EXCLUDES):
                candidates.append((relative, "untracked"))

        unique = []
        seen = set()
        for relative, source in sorted(set(candidates), key=lambda item: item[0].encode()):
            if relative in seen:
                continue
            seen.add(relative)
            for writable in remote_writable:
                if intersects(relative, writable):
                    fail(f"PROJECTION_POLICY_OVERLAP: {member_id}:{relative} <> {writable}")
            aggregate = f"{remote_relative}/{relative}"
            folded = aggregate.casefold()
            if folded in aggregate_case and aggregate_case[folded] != aggregate:
                fail(f"PROJECTION_CASE_COLLISION: {aggregate_case[folded]} <> {aggregate}")
            aggregate_case[folded] = aggregate
            unique.append((relative, source, aggregate))

        rows = []
        for relative, source, aggregate in unique:
            source_path = root / relative
            try:
                info = source_path.lstat()
            except FileNotFoundError:
                if source != "tracked":
                    fail(f"PROJECTION_SOURCE_MISSING: {member_id}:{relative}")
                row = (aggregate, "missing", "0000", "-", "-", "deleted")
            else:
                if relative in exact_files and not stat.S_ISREG(info.st_mode):
                    fail(f"PROJECTION_EXACT_FILE_INVALID: {relative}")
                if stat.S_ISREG(info.st_mode):
                    mode = "0755" if info.st_mode & stat.S_IXUSR else "0644"
                    row = (aggregate, "file", mode, digest_bytes(source_path.read_bytes()), "-", "present")
                elif stat.S_ISLNK(info.st_mode):
                    target = os.readlink(source_path)
                    if any(character in target for character in "\t\r\n"):
                        fail(f"PROJECTION_SYMLINK_INVALID: {member_id}:{relative}")
                    row = (aggregate, "symlink", "0777", digest_bytes(target.encode()), target, "present")
                else:
                    fail(f"PROJECTION_OBJECT_UNSUPPORTED: {member_id}:{relative}")
            rows.append(row)
            managed_rows.append(row)
        member_rows[member_id] = rows
        for standard in STANDARD_EXCLUDES:
            excluded_rows.add((f"{remote_relative}/{standard}", "standard"))
        for relative in exclusions:
            excluded_rows.add((f"{remote_relative}/{relative}", "tracked_exclusion"))
        for relative in remote_writable:
            excluded_rows.add((f"{remote_relative}/{relative}", "remote-writable"))
        for relative in ignored:
            validate_relative(relative.rstrip("/"), "ignored path")
            excluded_rows.add((f"{remote_relative}/{relative.rstrip('/')}", "observed_ignored"))

    member_digests = {}
    for member_id, rows in member_rows.items():
        encoded = "".join("\t".join(row) + "\n" for row in rows).encode()
        member_digests[member_id] = digest_bytes(encoded)
    return sorted(managed_rows, key=lambda row: row[0].encode()), sorted(excluded_rows, key=lambda row: (row[0].encode(), row[1])), member_digests


def load_retained(path: Path | None, snapshot: dict) -> list[str]:
    """Remote-owned areas a re-anchor left outside every current member."""
    if path is None or not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        fail(f"RETAINED_AREAS_INVALID: {path}")
    areas = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] != "retained":
            fail(f"RETAINED_AREAS_INVALID: line {number}")
        area = validate_relative(fields[1], "retained area")
        for member_id, _, remote_relative, _ in snapshot["members"]:
            if intersects(area, remote_relative):
                fail(f"RETAINED_AREAS_INVALID: {area} overlaps member {member_id}")
        areas.append(area)
    return areas


def projection_matches(stage: Path, rows: list) -> bool:
    """Re-read the entire staging tree; never trust mtimes or saved digests."""
    if stage.is_symlink() or not stage.is_dir():
        return False
    expected = {row[0]: row for row in rows if row[5] != "deleted"}
    parents = set()
    for relative in expected:
        parent = Path(relative).parent
        while str(parent) != ".":
            parents.add(parent.as_posix())
            parent = parent.parent
    seen = set()

    def visit(directory: Path) -> bool:
        for entry in directory.iterdir():
            relative = entry.relative_to(stage).as_posix()
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                if relative not in parents or not visit(entry):
                    return False
                continue
            row = expected.get(relative)
            if row is None:
                return False
            _, kind, mode, digest, target, _ = row
            if kind == "file":
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != int(mode, 8)
                        or digest_bytes(entry.read_bytes()) != digest):
                    return False
            elif kind == "symlink":
                if not stat.S_ISLNK(info.st_mode) or os.readlink(entry) != target:
                    return False
            else:
                return False
            seen.add(relative)
        return True

    try:
        return visit(stage) and seen == set(expected)
    except OSError:
        return False


def materialize(snapshot: dict, policy: dict, stage: Path, manifest: Path, excluded: Path, member_digests_path: Path,
                reuse_stage: Path | None = None, retained: list[str] = ()) -> bool:
    last_error = None
    for _ in range(3):
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        first, excluded_rows, member_digests = source_inventory(snapshot, policy)
        excluded_rows = sorted(set(excluded_rows) | {(area, "retained") for area in retained},
                               key=lambda row: (row[0].encode(), row[1]))
        mutation_path = os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_MUTATE_SOURCE")
        if mutation_path:
            with open(mutation_path, "ab") as mutation_stream:
                mutation_stream.write(b"x")
        reused = reuse_stage is not None and projection_matches(reuse_stage, first)
        try:
            for path, kind, mode, expected_digest, target, state in ([] if reused else first):
                if state == "deleted":
                    continue
                member_match = next(
                    (item for item in snapshot["members"] if path == item[2] or path.startswith(item[2] + "/")),
                    None,
                )
                if member_match is None:
                    fail(f"PROJECTION_PATH_INVALID: no member owns {path}")
                relative = path[len(member_match[2]) + 1 :]
                source_path = member_match[1] / relative
                destination = stage / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if kind == "file":
                    content = source_path.read_bytes()
                    if digest_bytes(content) != expected_digest:
                        raise RuntimeError(f"source changed while copying {path}")
                    destination.write_bytes(content)
                    destination.chmod(int(mode, 8))
                elif kind == "symlink":
                    actual_target = os.readlink(source_path)
                    if actual_target != target or digest_bytes(actual_target.encode()) != expected_digest:
                        raise RuntimeError(f"symlink changed while copying {path}")
                    destination.symlink_to(actual_target)
            second, second_excluded, second_member_digests = source_inventory(snapshot, policy)
            second_excluded = sorted(set(second_excluded) | {(area, "retained") for area in retained},
                                     key=lambda row: (row[0].encode(), row[1]))
            if first != second or excluded_rows != second_excluded or member_digests != second_member_digests:
                raise RuntimeError("source inventory changed during materialization")
        except (FileNotFoundError, RuntimeError) as error:
            last_error = error
            continue

        manifest_lines = ["path\ttype\tmode\tdigest\tsymlink_target\tstate"]
        manifest_lines.extend("\t".join(row) for row in first)
        excluded_lines = ["path\treason"]
        excluded_lines.extend("\t".join(row) for row in excluded_rows)
        digest_lines = ["member\tdigest"]
        digest_lines.extend(f"{member_id}\t{member_digests[member_id]}" for member_id in sorted(member_digests, key=lambda value: value.encode()))
        atomic_write(manifest, ("\n".join(manifest_lines) + "\n").encode())
        atomic_write(excluded, ("\n".join(excluded_lines) + "\n").encode())
        atomic_write(member_digests_path, ("\n".join(digest_lines) + "\n").encode())
        return reused
    fail(f"SOURCE_CHANGED: working-set sources did not stabilize after 3 attempts ({last_error})")


def command_normalize(arguments: argparse.Namespace) -> None:
    topology = parse_manifest(Path(arguments.manifest))
    atomic_write(Path(arguments.snapshot_out), snapshot_bytes(topology))
    atomic_write(Path(arguments.policy_out), policy_bytes(topology))
    print(topology["topology_digest"])


def command_verify(arguments: argparse.Namespace) -> None:
    snapshot = load_snapshot(Path(arguments.snapshot))
    verify_snapshot(snapshot)
    print(snapshot["topology_digest"])


def command_context(arguments: argparse.Namespace) -> None:
    snapshot = load_snapshot(Path(arguments.snapshot))
    requested = Path(arguments.path)
    if not requested.is_absolute():
        fail("WORKING_SET_CONTEXT_INVALID: context path must be absolute")
    if requested.is_file():
        requested = requested.parent
    requested = canonical_directory(requested, "context path")
    matches = []
    for member_id, root, remote_relative, _ in snapshot["members"]:
        try:
            relative = requested.relative_to(root)
        except ValueError:
            continue
        matches.append((member_id, root, remote_relative, relative))
    if len(matches) > 1:
        fail("WORKING_SET_CONTEXT_AMBIGUOUS: context belongs to multiple members")
    if matches:
        member_id, root, remote_relative, relative = matches[0]
    elif requested == Path(snapshot["anchor"]):
        member_id = snapshot["execution_member"]
        member_id, root, remote_relative, _ = next(item for item in snapshot["members"] if item[0] == member_id)
        relative = Path(".")
    else:
        fail("WORKING_SET_CONTEXT_OUTSIDE_MEMBERS: use the anchor or a configured member directory")
    remote_cwd = f"{arguments.remote_root}/{remote_relative}"
    if relative != Path("."):
        remote_cwd += "/" + relative.as_posix()
    print(f"member\t{member_id}")
    print(f"local_cwd\t{requested}")
    print(f"remote_cwd\t{remote_cwd}")


def command_materialize(arguments: argparse.Namespace) -> None:
    snapshot = load_snapshot(Path(arguments.snapshot))
    verify_snapshot(snapshot)
    policy = load_policy(Path(arguments.policy))
    reused = materialize(
        snapshot,
        policy,
        Path(arguments.stage),
        Path(arguments.managed),
        Path(arguments.excluded),
        Path(arguments.member_digests),
        Path(arguments.reuse_stage) if arguments.reuse_stage else None,
        load_retained(Path(arguments.retained) if arguments.retained else None, snapshot),
    )
    if arguments.reuse_stage:
        print("reused" if reused else "materialized")


def read_rows(path: Path, header: list[str]) -> list[list[str]]:
    if path.is_symlink() or not path.is_file():
        fail(f"REANCHOR_INPUT_INVALID: {path}")
    rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]
    if not rows or rows[0] != header or any(len(row) != len(header) for row in rows):
        fail(f"REANCHOR_INPUT_INVALID: {path}")
    return rows[1:]


def command_reanchor_plan(arguments: argparse.Namespace) -> None:
    """Decide where the old binding's remote-owned content ends up.

    The runner root keeps everything a projection does not own. An old member
    or retained area at exactly a new member path is inherited by that member;
    one disjoint from every new member stays where it is as a retained area; a
    partial overlap would mix old runtime content into new managed source.
    """
    old = load_snapshot(Path(arguments.old_snapshot))
    new = load_snapshot(Path(arguments.new_snapshot))
    policy = load_policy(Path(arguments.new_policy))
    excluded = read_rows(Path(arguments.old_excluded), ["path", "reason"])
    new_paths = {remote_relative: member_id for member_id, _, remote_relative, _ in new["members"]}
    candidates = {remote_relative for _, _, remote_relative, _ in old["members"]}
    candidates |= {path for path, reason in excluded if reason == "retained"}
    retained, inherited = set(), set()
    for area in sorted(candidates, key=lambda value: value.encode()):
        if area in new_paths:
            inherited.add(area)
            continue
        clash = sorted(path for path in new_paths if intersects(area, path))
        if clash:
            fail(f"REANCHOR_REMOTE_OVERLAP: previous remote area {area} overlaps new member path {clash[0]}")
        retained.add(area)
    for path, reason in excluded:
        if reason not in ("remote-writable", "tracked_exclusion"):
            continue
        owner = next((area for area in inherited if path.startswith(area + "/")), None)
        if owner is None:
            continue
        member_id, relative = new_paths[owner], path[len(owner) + 1:]
        scopes = policy.get(("remote_writable", member_id), []) + policy.get(("tracked_exclusion", member_id), [])
        if not any(relative == scope or relative.startswith(scope + "/") for scope in scopes) and not any(
                within_standard_scope(relative, standard) for standard in STANDARD_EXCLUDES):
            fail(f"REANCHOR_WRITABLE_UNCOVERED: remote-owned {path} would become unexpected under member "
                 f"{member_id}; declare {relative} in the new manifest")
    rows = [f"retained\t{area}" for area in sorted(retained, key=lambda value: value.encode())]
    atomic_write(Path(arguments.retained_out), (("\n".join(rows) + "\n") if rows else "").encode())
    for area in sorted(inherited, key=lambda value: value.encode()):
        print(f"inherited\t{area}")
    for area in sorted(retained, key=lambda value: value.encode()):
        print(f"retained\t{area}")


def command_reanchor_verify(arguments: argparse.Namespace) -> None:
    """Accept a runner tree whose old managed paths are exact or already gone.

    A previous topology retirement may have unlinked some or all of them; any
    other difference, and any path outside the old binding's scopes, is drift.
    """
    header = ["path", "type", "mode", "digest", "symlink_target", "state"]
    expected = read_rows(Path(arguments.expected), header)
    actual = read_rows(Path(arguments.actual), header)
    if len(actual) != len(expected):
        fail(f"REMOTE_DRIFT: {abs(len(actual) - len(expected))} runner paths differ from the previous managed manifest")
    present = 0
    for want, have in zip(expected, actual):
        if have == want and want[5] != "deleted":
            present += 1
        elif have != want and have != [want[0], "missing", "0000", "-", "-", "deleted"]:
            fail(f"REMOTE_DRIFT: {have[0]} differs from the previous managed manifest")
    print(f"present\t{present}")
    print(f"absent\t{len(expected) - present}")


def command_watch_token(arguments: argparse.Namespace) -> None:
    if arguments.snapshot:
        snapshot = load_snapshot(Path(arguments.snapshot))
        verify_snapshot(snapshot)
        policy = load_policy(Path(arguments.policy))
    else:
        root = Path(arguments.root).resolve(strict=True)
        if not root.is_dir() or (root / ".git").is_symlink():
            fail(f"PROJECTION_ROOT_INVALID: {root}")
        snapshot = {"members": [("root", root, ".", "not-required")]}
        policy = load_single_root_policy(Path(arguments.policy))
    managed, excluded, member_digests = source_inventory(snapshot, policy)
    digest = hashlib.sha256()
    for row in managed:
        digest.update(("managed\t" + "\t".join(row) + "\n").encode())
    for row in excluded:
        digest.update(("excluded\t" + "\t".join(row) + "\n").encode())
    for member_id in sorted(member_digests, key=lambda value: value.encode()):
        digest.update(f"member\t{member_id}\t{member_digests[member_id]}\n".encode())
    print(digest.hexdigest())


def command_exact_untracked(arguments: argparse.Namespace) -> None:
    policy = load_single_root_policy(Path(arguments.policy))
    paths = exact_untracked_files(Path(arguments.root), policy.get(("allowed_untracked_file", "root"), []))
    for path in sorted(paths):
        sys.stdout.buffer.write(path.encode() + b"\0")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subcommands = result.add_subparsers(dest="command", required=True)
    exact = subcommands.add_parser("exact-untracked")
    exact.add_argument("--root", required=True)
    exact.add_argument("--policy", required=True)
    exact.set_defaults(function=command_exact_untracked)
    normalize = subcommands.add_parser("normalize")
    normalize.add_argument("--manifest", required=True)
    normalize.add_argument("--snapshot-out", required=True)
    normalize.add_argument("--policy-out", required=True)
    normalize.set_defaults(function=command_normalize)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--snapshot", required=True)
    verify.set_defaults(function=command_verify)
    context = subcommands.add_parser("context")
    context.add_argument("--snapshot", required=True)
    context.add_argument("--path", required=True)
    context.add_argument("--remote-root", required=True)
    context.set_defaults(function=command_context)
    project = subcommands.add_parser("materialize")
    project.add_argument("--snapshot", required=True)
    project.add_argument("--policy", required=True)
    project.add_argument("--stage", required=True)
    project.add_argument("--managed", required=True)
    project.add_argument("--excluded", required=True)
    project.add_argument("--member-digests", required=True)
    project.add_argument("--reuse-stage")
    project.add_argument("--retained")
    project.set_defaults(function=command_materialize)
    plan = subcommands.add_parser("reanchor-plan")
    plan.add_argument("--old-snapshot", required=True)
    plan.add_argument("--old-excluded", required=True)
    plan.add_argument("--new-snapshot", required=True)
    plan.add_argument("--new-policy", required=True)
    plan.add_argument("--retained-out", required=True)
    plan.set_defaults(function=command_reanchor_plan)
    verify_previous = subcommands.add_parser("reanchor-verify")
    verify_previous.add_argument("--expected", required=True)
    verify_previous.add_argument("--actual", required=True)
    verify_previous.set_defaults(function=command_reanchor_verify)
    watch = subcommands.add_parser("watch-token")
    watch_source = watch.add_mutually_exclusive_group(required=True)
    watch_source.add_argument("--snapshot")
    watch_source.add_argument("--root")
    watch.add_argument("--policy", required=True)
    watch.set_defaults(function=command_watch_token)
    return result


def main() -> int:
    try:
        arguments = parser().parse_args()
        arguments.function(arguments)
        return 0
    except WorkingSetError as error:
        print(error, file=sys.stderr)
        if str(error).startswith("REMOTE_DRIFT"):
            return 74
        return 79 if str(error).startswith(("WORKING_SET_TOPOLOGY_DRIFT", "SOURCE_CHANGED")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
