#!/usr/bin/env python3
"""Parse the project-owned runtime contract shared by setup and mode switching."""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from urllib.parse import urlsplit


class ContractError(Exception):
    pass


BASE_KEYS = {"version", "setup_command", "start_command", "health_url", "browser_port", "coder_hostname"}
MODE_KEYS = {"quiesce_command", "stop_command", "status_command", "resource_id"}
MODE_OPTIONAL_KEYS = {"preview_ports", "docker_capability"}
REQUIRED = BASE_KEYS - {"coder_hostname"}


def read_contract(root, switching=False):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ContractError("PROJECT_PATH_INVALID: expected an existing canonical project directory")
    path = root / ".remote-dev/project.env"
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        raise ContractError("CONTRACT_MISSING: project setup contract must be a regular non-symlink file: .remote-dev/project.env")
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line or "\0" in line:
            raise ContractError(f"CONTRACT_INVALID: line {number} must be literal key=value")
        key, value = line.split("=", 1)
        if key not in BASE_KEYS | MODE_KEYS | MODE_OPTIONAL_KEYS:
            raise ContractError(f"CONTRACT_INVALID: unknown key at line {number}")
        if key in values:
            raise ContractError(f"CONTRACT_INVALID: duplicate project contract key: {key}")
        values[key] = value
    version = values.get("version")
    if version not in ("1", "2"):
        raise ContractError("CONTRACT_INVALID: supported versions are 1 and 2")
    if version == "1" and (MODE_KEYS | MODE_OPTIONAL_KEYS).intersection(values):
        raise ContractError("CONTRACT_INVALID: lifecycle fields require version=2")
    missing = {key for key in REQUIRED | (MODE_KEYS if version == "2" else set()) if not values.get(key)}
    if missing:
        raise ContractError("CONTRACT_INVALID: missing " + ", ".join(sorted(missing)))
    if switching and version != "2":
        raise ContractError("LIFECYCLE_REQUIRED: mode switching requires version=2 with quiesce/stop/status and resource_id")
    if not re.fullmatch(r"[0-9]{1,5}", values["browser_port"]) or not 1 <= int(values["browser_port"]) <= 65535:
        raise ContractError("CONTRACT_INVALID: browser_port must be from 1 to 65535")
    url = urlsplit(values["health_url"])
    try:
        port = url.port
    except ValueError:
        port = None
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or not port or
            url.username or url.password or url.fragment or any(c.isspace() for c in values["health_url"])):
        raise ContractError("CONTRACT_INVALID: health_url must be loopback HTTP with an explicit valid port")
    hostname = values.get("coder_hostname")
    if hostname and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.coder", hostname):
        raise ContractError("CONTRACT_INVALID: coder_hostname must be a .coder hostname")
    if version == "2" and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", values["resource_id"]):
        raise ContractError("CONTRACT_INVALID: resource_id must identify one project-owned runtime")
    if "preview_ports" in values:
        ports = values["preview_ports"].split(",")
        if (len(ports) > 8 or len(set(ports)) != len(ports) or
                any(not re.fullmatch(r"[0-9]{1,5}", p) or not 1 <= int(p) <= 65535 for p in ports)):
            raise ContractError("CONTRACT_INVALID: preview_ports must list up to eight distinct TCP port numbers")
    if values.get("docker_capability", "core") not in ("core", "containers"):
        raise ContractError("CONTRACT_INVALID: docker_capability must be core or containers")
    return values


def validate_receipt(receipt, root, contract, context, require_candidate=False):
    if not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != 1:
        raise ContractError("RUNTIME_UNVERIFIED: project status must return a version=1 JSON object")
    if receipt.get("project_dir") != str(Path(root).resolve()) or receipt.get("resource_id") != contract["resource_id"]:
        raise ContractError("RUNTIME_OWNERSHIP_MISMATCH: status belongs to another checkout or resource group")
    if receipt.get("state") not in ("stopped", "running", "partial") or not isinstance(receipt.get("resources"), list):
        raise ContractError("RUNTIME_UNVERIFIED: invalid resource status")
    identities = set()
    if require_candidate:
        for key in ("operation_id", "snapshot_id"):
            if not isinstance(context.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", context[key]):
                raise ContractError("RUNTIME_UNVERIFIED: candidate context requires exact operation and snapshot IDs")
    if receipt.get("health_url") != contract["health_url"]:
        raise ContractError("RUNTIME_UNVERIFIED: health endpoint differs from the project contract")
    if not isinstance(receipt.get("preserved_volumes"), list) or any(not isinstance(v, str) or not v for v in receipt["preserved_volumes"]):
        raise ContractError("RUNTIME_UNVERIFIED: explicit preserved volume inventory is required")
    for resource in receipt["resources"]:
        if not isinstance(resource, dict) or resource.get("kind") not in ("docker-container", "process"):
            raise ContractError("RUNTIME_UNVERIFIED: unknown resource kind")
        identity = resource.get("id")
        if not isinstance(identity, str) or not identity or "\n" in identity:
            raise ContractError("RUNTIME_UNVERIFIED: missing resource identity")
        key = (resource["kind"], identity)
        if key in identities:
            raise ContractError("RUNTIME_UNVERIFIED: duplicate resource identity")
        identities.add(key)
        if resource.get("project_dir") != receipt["project_dir"] or resource.get("resource_id") != receipt["resource_id"]:
            raise ContractError("RUNTIME_OWNERSHIP_MISMATCH: resource belongs to another project")
        if type(resource.get("running")) is not bool:
            raise ContractError("RUNTIME_UNVERIFIED: resource running state is missing")
        if resource["kind"] == "process":
            if (not re.fullmatch(r"[1-9][0-9]*", identity) or not isinstance(resource.get("start_identity"), str) or
                    not resource["start_identity"] or type(resource.get("process_group_id")) is not int or resource["process_group_id"] < 1):
                raise ContractError("RUNTIME_UNVERIFIED: process requires PID, start identity, and process group")
        else:
            if not re.fullmatch(r"[0-9a-f]{64}", identity):
                raise ContractError("RUNTIME_UNVERIFIED: Docker resource requires its full container ID")
            labels = resource.get("labels")
            required_labels = {"com.docker.compose.project": contract["resource_id"],
                               "com.docker.compose.project.working_dir": receipt["project_dir"]}
            if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in required_labels.items()) or not labels.get("com.docker.compose.service"):
                raise ContractError("RUNTIME_OWNERSHIP_MISMATCH: Docker Compose labels do not identify the final project path")
            if not isinstance(resource.get("volumes"), list) or any(v not in receipt["preserved_volumes"] for v in resource["volumes"]):
                raise ContractError("RUNTIME_UNVERIFIED: Docker volumes are absent from the preserved inventory")
        if require_candidate and resource["running"]:
            if resource.get("operation_id") != context.get("operation_id") or resource.get("snapshot_id") != context.get("snapshot_id"):
                raise ContractError("RUNTIME_OWNERSHIP_MISMATCH: running resource is not the verified candidate")
    running = any(resource["running"] for resource in receipt["resources"])
    if receipt["state"] == "stopped" and running or receipt["state"] == "running" and not running:
        raise ContractError("RUNTIME_UNVERIFIED: state contradicts resource inventory")
    if require_candidate and (receipt["state"] != "running" or receipt.get("operation_id") != context.get("operation_id") or
                              receipt.get("snapshot_id") != context.get("snapshot_id")):
        raise ContractError("RUNTIME_UNVERIFIED: candidate identity or running state differs")
    if running and (receipt["state"] == "running" or receipt.get("health_resource") is not None):
        health_resource = receipt.get("health_resource")
        if not any(r["id"] == health_resource and r["running"] for r in receipt["resources"]):
            raise ContractError("RUNTIME_UNVERIFIED: health endpoint must name a running resource")
    return receipt


def run_phase(root, phase, context, require_candidate=False, timeout=300):
    contract = read_contract(root, switching=True)
    if phase not in ("setup", "start", "quiesce", "stop", "status"):
        raise ContractError("CONTRACT_INVALID: unknown lifecycle phase")
    env = os.environ.copy()
    env.update(REMOTE_DEV_PROJECT_DIR=str(Path(root).resolve()), REMOTE_DEV_RESOURCE_ID=contract["resource_id"])
    for key, variable in {"operation_id": "REMOTE_DEV_OPERATION_ID", "snapshot_id": "REMOTE_DEV_SOURCE_SNAPSHOT",
                          "profile": "REMOTE_DEV_PROFILE_NAME", "workspace": "REMOTE_DEV_WORKSPACE_NAME",
                          "tracked_inventory": "REMOTE_DEV_TRACKED_INVENTORY", "data_policy": "REMOTE_DEV_DATA_POLICY"}.items():
        value = context.get(key, env.get(variable, "") if key == "tracked_inventory" else "")
        if not isinstance(value, str) or "\0" in value or "\n" in value:
            raise ContractError("CONTRACT_INVALID: invalid lifecycle context")
        env[variable] = value
    # Commands are project-owned and were part of the reviewed source snapshot.
    # Never echo expanded commands, environment variables, or arbitrary app output.
    process = subprocess.Popen(["bash", "-lc", contract[phase + "_command"]], cwd=root, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        # Only this newly created session is ours. Detached services are covered
        # separately by the project's status inventory, never guessed from PIDs.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=10)
        raise ContractError(f"PROJECT_PHASE_TIMEOUT: {phase}; hook group terminated, runtime state remains unverified; retain claims and run status before recovery") from error
    result = process
    if result.returncode:
        raise ContractError(f"PROJECT_PHASE_FAILED: {phase} exited {result.returncode}; inspect the project's owned runtime")
    if phase == "status":
        try:
            return validate_receipt(json.loads(stdout), root, contract, context, require_candidate)
        except ValueError as error:
            raise ContractError("RUNTIME_UNVERIFIED: status did not return JSON") from error
    return {"phase": phase, "exit_code": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--switching", action="store_true")
    parser.add_argument("--format", choices=("json", "env"), default="json")
    args = parser.parse_args()
    try:
        values = read_contract(args.root, args.switching)
        if args.format == "env":
            print("\n".join(key + "=" + value for key, value in values.items()))
        else:
            print(json.dumps(values, sort_keys=True))
    except (ContractError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, ContractError) else "CONTRACT_INVALID: cannot read project contract", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
