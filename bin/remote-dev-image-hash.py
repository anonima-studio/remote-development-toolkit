#!/usr/bin/env python3
"""Refresh or check the image pins using install.sh's ordered input contract."""

import argparse
import hashlib
import re
import sys
from pathlib import Path


def refresh_workspace_image_hash(root, check=False):
    root = Path(root)
    install = root / "deploy/install.sh"
    text = install.read_text(encoding="utf-8")
    block = re.search(r"workspace_image_hash=\$\(cat \\\n(.*?)\| sha256sum", text, re.S)
    expected = re.search(r"^workspace_image_expected_hash=([0-9a-f]{64})$", text, re.M)
    tag = re.search(r"^workspace_image_tag=(remote-dev-workspace:[\w.-]+-)([0-9a-f]{11})$", text, re.M)
    if not block or not expected or not tag:
        raise ValueError("cannot locate the workspace image hash contract in deploy/install.sh")
    paths = re.findall(r"/opt/remote-dev/(templates/docker/[^\s\\]+)", block.group(1))
    if not paths:
        raise ValueError("workspace image hash covers no files")
    digest = hashlib.sha256()
    for rel in paths:
        source = root / rel
        if not source.is_file():
            raise ValueError("workspace image hash input missing: " + rel)
        digest.update(source.read_bytes())
    old, new = expected.group(1), digest.hexdigest()
    image = tag.group(1) + new[:11]

    # Only current source pins are rewritten; historical docs and artifacts stay intact.
    # Read and validate every target before writing any of them.
    contracts = {
        "deploy/install.sh": [
            (r"(?m)^workspace_image_expected_hash=[0-9a-f]{64}$", "workspace_image_expected_hash=" + new),
            (r"(?m)^workspace_image_tag=remote-dev-workspace:[\w.-]+$", "workspace_image_tag=" + image),
        ],
        "templates/docker/main.tf": [
            (r'(?m)^  image    = "remote-dev-workspace:[\w.-]+"$', '  image    = "' + image + '"'),
        ],
        "tests/remote-dev-cli-smoke.sh": [
            (r"(?m)^workspace_image_hash=[0-9a-f]{64}$", "workspace_image_hash=" + new),
            (r"(?m)^workspace_image_tag=remote-dev-workspace:[\w.-]+$", "workspace_image_tag=" + image),
        ],
        "tests/pids-rollout-test.py": [
            (r"remote-dev-workspace:node24-\d{8}-[0-9a-f]{11}", image),
        ],
    }
    updates = {}
    for rel, rules in contracts.items():
        path = root / rel
        original = path.read_text(encoding="utf-8")
        updated = original
        for pattern, replacement in rules:
            updated, count = re.subn(pattern, replacement, updated)
            if count != 1:
                raise ValueError("expected one workspace image pin in " + rel)
        if updated != original:
            updates[path] = updated
    if check and updates:
        names = ", ".join(str(path.relative_to(root)) for path in updates)
        raise ValueError("stale workspace image pins in " + names + ". Run pnpm image:hash and commit the changed files; then rerun pnpm test.")
    for path, updated in updates.items():
        path.write_text(updated, encoding="utf-8")
    return (old, new) if updates else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on stale pins without writing")
    args = parser.parse_args()
    try:
        result = refresh_workspace_image_hash(Path(__file__).resolve().parents[1], args.check)
    except (OSError, ValueError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    print("Workspace image pins " + ("updated: " + result[0][:11] + " -> " + result[1][:11] if result else "are current."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
