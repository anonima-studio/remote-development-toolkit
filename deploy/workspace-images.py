#!/usr/bin/env python3
"""Check image dependencies of existing Coder workspaces, including stopped ones.

Run on the selected Coder host. Never start/update workspaces or remove images.
--retain may create only the existing source-verified retention references.
"""

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path


def template_image(archive):
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        files = [m for m in bundle.getmembers() if m.name in ("main.tf", "./main.tf")]
        if len(files) != 1 or not files[0].isfile() or files[0].size > 2_000_000:
            raise ValueError("template must contain one regular root main.tf")
        source = bundle.extractfile(files[0]).read().decode()
    images = re.findall(r'^\s*image\s*=\s*"(remote-dev-workspace:[a-zA-Z0-9_.-]+)"\s*$', source, re.M)
    if len(images) != 1:
        raise ValueError("expected one literal Remote Dev workspace image; inspect template manually")
    return images[0]


def dependencies(get):
    versions = {}
    templates = set()
    offset = 0
    while True:
        page = get(f"workspaces?limit=100&offset={offset}")
        rows = page["workspaces"]
        for workspace in rows:
            if workspace["template_name"] != "remote-dev-docker":
                continue
            build = workspace["latest_build"]
            owner = f"workspace:{workspace['owner_name']}/{workspace['name']}({build['status']})"
            versions.setdefault(build["template_version_id"], set()).add(owner)
            templates.add(workspace["template_id"])
        offset += len(rows)
        if offset >= page["count"]:
            break
        if not rows:
            raise ValueError("incomplete Coder workspace inventory")
    for template_id in sorted(templates):
        template = get(f"templates/{template_id}")
        versions.setdefault(template["active_version_id"], set()).add("active-template:remote-dev-docker")
    images = {}
    for version_id, owners in sorted(versions.items()):
        version = get(f"templateversions/{version_id}")
        image = template_image(get(f"files/{version['job']['file_id']}", raw=True))
        images.setdefault(image, set()).update(owners)
    return images


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"docker {' '.join(args[:2])} failed; resource missing or Docker unavailable")
    return json.loads(result.stdout)[0]


def verify_image(image, retain=False):
    metadata = docker("image", "inspect", image)
    source_hash = (metadata.get("Config", {}).get("Labels") or {}).get(
        "id0.remote-dev.workspace-source-sha256", ""
    )
    suffix = image.rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash) or not re.fullmatch(r"[0-9a-f]{11,64}", suffix) or not source_hash.startswith(suffix):
        raise ValueError("image source label does not match content-derived tag")
    # Docker can untag a referenced image when it has another tag. A retention
    # container alone cannot protect that name against Coolify's `docker rmi`.
    if metadata.get("RepoTags") != [image]:
        raise ValueError("image has tag aliases; retention cannot protect individual tags from docker rmi")
    image_id = metadata["Id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("invalid Docker image identity")
    if retain:
        subprocess.run(
            ["bash", str(Path(__file__).with_name("retain-workspace-image.sh")), image, source_hash],
            check=True, timeout=30,
        )
    reference = docker("container", "inspect", f"remote-dev-image-retention-{image_id[7:]}")
    labels = reference.get("Config", {}).get("Labels") or {}
    host = reference["HostConfig"]
    if not (
        reference["Image"] == image_id
        and reference["State"]["Status"] == "created"
        and labels.get("id0.remote-dev.purpose") == "image-retention"
        and labels.get("id0.remote-dev.owner") == "remote-dev"
        and not labels.get("coolify.managed")
        and not reference.get("Mounts")
        and not host.get("PortBindings")
        and host["NetworkMode"] == "none"
    ):
        raise ValueError("retention reference has unsafe or conflicting configuration")
    return image_id


def inspect_version(get, version_id):
    """Inspect an explicitly selected inactive version without retaining it."""
    version = get(f"templateversions/{version_id}")
    if version.get("id") != version_id or version.get("job", {}).get("status") != "succeeded":
        raise ValueError("selected template version is not ready")
    image = template_image(get(f"files/{version['job']['file_id']}", raw=True))
    return {"version_id": version_id, "template_id": version["template_id"],
            "image": image, "image_id": verify_image(image, retain=False)}


def audit(images, retain=False):
    failures = 0
    for image, owners in sorted(images.items()):
        try:
            image_id = verify_image(image, retain)
            print(f"OK   {image} {image_id}")
        except (ValueError, subprocess.SubprocessError) as error:
            failures += 1
            print(f"FAIL {image}: {error}")
        print("     required by " + ", ".join(sorted(owners)))
    if failures:
        print("Recovery: restore the exact verified image or explicitly migrate the listed workspaces to a verified retained template. Preserve their volumes; never retag a different image as the missing version.")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--retain", action="store_true")
    parser.add_argument("--version", help="inspect this exact template version, including an inactive candidate")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("invalid Coder loopback port")
    if args.version and (args.retain or not re.fullmatch(r"[0-9a-f-]{36}", args.version)):
        parser.error("--version requires one UUID and cannot retain resources")
    token = Path("/opt/remote-dev/coder/operator.session").read_text().strip()

    def get(path, raw=False):
        request = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/api/v2/{path}",
            headers={"Coder-Session-Token": token},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read() if raw else json.load(response)

    try:
        if args.version:
            print(json.dumps(inspect_version(get, args.version), sort_keys=True))
            return 0
        return int(audit(dependencies(get), args.retain) > 0)
    except (OSError, ValueError, KeyError, tarfile.TarError) as error:
        print(f"FAIL workspace image inventory: {type(error).__name__}; cannot prove image coverage", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
