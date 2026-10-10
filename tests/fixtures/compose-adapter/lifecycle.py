#!/usr/bin/env python3
"""Synthetic project-owned Docker Compose lifecycle adapter for mode tests.

It mirrors the status and stop contract of a real project adapter: it only
inspects and stops containers of its own Compose project and never removes
volumes or foreign resources. Tests replace ROOT, RESOURCE and command().
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
RESOURCE = "example-app"
APPLICATION_SERVICES = {"web", "api", "postgres", "mailpit", "object-store"}
INITIALIZER = "web-owner-init"
SERVICES = APPLICATION_SERVICES | {INITIALIZER}


class LifecycleError(Exception):
    pass


def fail(message):
    raise LifecycleError(message)


def command(argv, environment=None, capture=True):
    result = subprocess.run(argv, cwd=ROOT, env=environment, capture_output=capture, text=True)
    if result.returncode:
        fail("EXAMPLE_APP_COMMAND_FAILED: " + argv[0] + " exited " + str(result.returncode))
    return result.stdout if capture else None


def port(name="WEB_PORT", default="3000"):
    value = default
    path = ROOT / ".env"
    if path.exists():
        if path.is_symlink():
            fail("EXAMPLE_APP_ENV_UNTRUSTED: .env must be a regular project variable file")
        for line in path.read_text().splitlines():
            if re.match(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=", line):
                value = line.split("=", 1)[1].strip().strip("\"'")
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        fail("EXAMPLE_APP_PORT_INVALID: " + name + " must be a literal valid TCP port")
    return int(value)


def published_on_loopback(item, internal_port, host_port):
    bindings = item["NetworkSettings"]["Ports"].get(str(internal_port) + "/tcp") or []
    return any(binding.get("HostPort") == str(host_port) and binding.get("HostIp") in ("127.0.0.1", "0.0.0.0")
               for binding in bindings)


def containers():
    ids = command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + RESOURCE]).split()
    if not ids:
        return []
    result = json.loads(command(["docker", "inspect", *ids]))
    for item in result:
        labels = item["Config"].get("Labels") or {}
        if (labels.get("com.docker.compose.project") != RESOURCE or
                labels.get("com.docker.compose.project.working_dir") != str(ROOT) or
                labels.get("com.docker.compose.service") not in SERVICES):
            fail("EXAMPLE_APP_RESOURCE_CONFLICT: preserve a container with another working directory or unknown service")
    return result


def status():
    resources, volumes = [], set()
    all_items = containers()
    web = None
    web_verified = True
    for item in all_items:
        labels = item["Config"]["Labels"]
        identity = {"operation_id": labels.get("io.remote-dev.operation-id"),
                    "snapshot_id": labels.get("io.remote-dev.source-snapshot")}
        mounted = sorted(mount["Name"] for mount in item.get("Mounts", []) if mount["Type"] == "volume")
        volumes.update(mounted)
        resources.append({
            "kind": "docker-container", "id": item["Id"], "project_dir": str(ROOT), "resource_id": RESOURCE,
            "running": item["State"]["Running"],
            "labels": {k: v for k, v in labels.items() if k.startswith(("com.docker.compose.", "io.remote-dev."))},
            "volumes": mounted, "operation_id": identity.get("operation_id"),
            "snapshot_id": identity.get("snapshot_id")})
        if labels["com.docker.compose.service"] == "web" and item["State"]["Running"]:
            if web:
                fail("EXAMPLE_APP_RESOURCE_CONFLICT: more than one running web container")
            web = item
            if re.fullmatch(r"[a-f0-9]{64}", labels.get("io.remote-dev.operation-id", "")):
                expected_image = labels.get("io.remote-dev.prepared-image", "")
                web_verified = bool(re.fullmatch(r"sha256:[a-f0-9]{64}", expected_image)) and (
                    item.get("Image") == expected_image and
                    item["Config"].get("User") == str(os.getuid()) + ":" + str(os.getgid()))
            if not published_on_loopback(item, 3000, port()):
                fail("EXAMPLE_APP_HEALTH_MISMATCH: web does not publish WEB_PORT on the declared loopback target")
    running = [item for item in all_items if item["State"]["Running"]]
    healthy_services = {item["Config"]["Labels"]["com.docker.compose.service"] for item in running
                        if item["State"].get("Health", {}).get("Status", "healthy") == "healthy"}
    state = "stopped" if not running else "running" if web_verified and healthy_services == APPLICATION_SERVICES and not any(
        item["Config"]["Labels"]["com.docker.compose.service"] == INITIALIZER for item in running) else "partial"
    operation_ids = {r["operation_id"] for r in resources if r["running"]}
    snapshots = {r["snapshot_id"] for r in resources if r["running"]}
    return {"version": 1, "project_dir": str(ROOT), "resource_id": RESOURCE, "state": state,
            "operation_id": next(iter(operation_ids)) if len(operation_ids) == 1 else None,
            "snapshot_id": next(iter(snapshots)) if len(snapshots) == 1 else None,
            "resources": resources, "preserved_volumes": sorted(volumes),
            "health_url": f"http://127.0.0.1:{port()}/health", "health_resource": web["Id"] if web else None}


def stop():
    active = [item["Id"] for item in containers() if item["State"]["Running"]]
    if active:
        command(["docker", "stop", "--time", "30", *active])
    if status()["state"] != "stopped":
        fail("EXAMPLE_APP_STOP_FAILED: a project resource is still running")


def main():
    try:
        phase = sys.argv[1] if len(sys.argv) == 2 else None
        if phase == "status":
            print(json.dumps(status(), sort_keys=True))
        elif phase in ("stop", "quiesce"):
            stop()
        else:
            fail("Usage: python3 .remote-dev/lifecycle.py quiesce|stop|status")
    except (LifecycleError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, LifecycleError) else "EXAMPLE_APP_LIFECYCLE_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
