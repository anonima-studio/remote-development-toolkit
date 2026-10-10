#!/usr/bin/env python3
"""Read-only workspace admission facts on the selected infrastructure host.

The workspace owner also runs the existing doctor for the network/platform
contract. This script never creates resources and never returns container Env.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("host inventory unavailable: " + args[0])
    return result.stdout


def pressure(name):
    values = {}
    for line in Path("/proc/pressure/" + name).read_text().splitlines():
        fields = line.split()
        values[fields[0]] = float(dict(field.split("=") for field in fields[1:])["avg10"])
    return values


def snapshot():
    memory = {line.split(":")[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines()}
    ids = command("docker", "ps", "-aq").split()
    rows = json.loads(command("docker", "inspect", *ids)) if ids else []
    containers = []
    for row in rows:
        config, host = row["Config"], row["HostConfig"]
        labels = config.get("Labels") or {}
        containers.append({
            "id": row["Id"], "name": row["Name"], "image": row["Image"],
            "workspace_id": labels.get("coder.workspace_id"),
            "running": row["State"]["Running"],
            "health": row["State"].get("Health", {}).get("Status"),
            "runtime": host["Runtime"], "memory": host["Memory"],
            "cpu": host["NanoCpus"] / 1e9 if host["NanoCpus"] else
                   (host["CpuQuota"] / host["CpuPeriod"] if host["CpuQuota"] > 0 and host["CpuPeriod"] > 0 else 0),
            "pids_limit": host.get("PidsLimit"), "ulimits": host.get("Ulimits"),
            "ports": host.get("PortBindings"),
            "mounts": sorted([{key: mount.get(key) for key in ("Type", "Name", "Source", "Destination", "RW")}
                              for mount in row["Mounts"]], key=lambda mount: mount["Destination"]),
            "networks": sorted(row["NetworkSettings"]["Networks"]),
        })
    ids = command("docker", "network", "ls", "-q").split()
    networks = json.loads(command("docker", "network", "inspect", *ids)) if ids else []
    daemon = json.loads(command("docker", "info", "--format", "{{json .}}"))
    processes = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    threads = 0
    for process in processes:
        try:
            for line in (process / "status").read_text().splitlines():
                if line.startswith("Threads:"):
                    threads += int(line.split()[1])
                    break
        except FileNotFoundError:
            continue  # The process exited during this read-only snapshot.
    return {"hostname": command("hostname").strip(), "daemon_id": daemon["ID"],
            "memory_total": memory["MemTotal"], "memory_available": memory["MemAvailable"],
            "cpu_count": os.cpu_count(), "disk_free": shutil.disk_usage(daemon["DockerRootDir"]).free,
            "pids": len(processes), "threads": threads,
            "pid_max": int(Path("/proc/sys/kernel/pid_max").read_text()),
            "threads_max": int(Path("/proc/sys/kernel/threads-max").read_text()),
            "pressure": {name: pressure(name) for name in ("cpu", "memory", "io")},
            "containers": sorted(containers, key=lambda row: row["id"]),
            "networks": sorted([{key: row.get(key) for key in ("Id", "Name", "Driver", "Internal", "IPAM", "Options")}
                                for row in networks], key=lambda row: row["Id"]),
            "volumes": sorted(command("docker", "volume", "ls", "-q").split()),
            "firewall": command("sudo", "-n", "iptables", "-S", "DOCKER-USER"),
            **egress_facts()}


def runtime_profile():
    """Non-secret runtime identity written by install on the host."""
    values = {}
    path = Path("/etc/remote-dev/host-profile.env")
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value
    return values


def egress_facts():
    profile = runtime_profile()
    if profile.get("REMOTE_DEV_EGRESS", "wireguard") == "host":
        # Host egress installs no policy routing or egress filter chain.
        return {"egress_firewall": "", "rules": command("ip", "-4", "rule", "show"), "routes": ""}
    table = profile.get("REMOTE_DEV_POLICY_TABLE", "")
    if not table.isdigit():
        raise ValueError("host inventory unavailable: REMOTE_DEV_POLICY_TABLE")
    return {"egress_firewall": command("sudo", "-n", "iptables", "-S", "REMOTE-DEV-EGRESS"),
            "rules": command("ip", "-4", "rule", "show"),
            "routes": command("ip", "-4", "route", "show", "table", table)}


def admit(facts, limits, disk_bytes, unbounded_budget=None):
    """Admission with an explicit shared budget for uncapped neighbors."""
    running = [row for row in facts["containers"] if row["running"]]
    margin = 2 * 1024**3
    reasons = []
    unbounded = any(not row["memory"] or not row["cpu"] for row in running)
    if unbounded and (not unbounded_budget or unbounded_budget.get("memory", 0) <= 0 or unbounded_budget.get("cpu", 0) <= 0):
        raise ValueError("HOST_UNBOUNDED_BUDGET_REQUIRED: declare reviewed shared memory and CPU budgets for uncapped neighbors")
    budget = unbounded_budget or {"memory": 0, "cpu": 0}
    if (facts["memory_available"] < limits["memory"] + margin or
            sum(row["memory"] for row in running) + budget["memory"] + limits["memory"] + margin > facts["memory_total"]):
        reasons.append("memory")
    if sum(row["cpu"] for row in running) + budget["cpu"] + limits["cpu"] + 1 > facts["cpu_count"]:
        reasons.append("cpu")
    if facts["disk_free"] < disk_bytes + margin:
        reasons.append("disk")
    if (facts["pids"] + 1024 >= facts["pid_max"] or
            facts["threads"] + limits.get("pids", 0) + 1024 >= facts["threads_max"]):
        reasons.append("pids")
    if (facts["pressure"]["cpu"].get("some", 100) >= 50 or
            facts["pressure"]["memory"].get("full", 100) >= 1 or
            facts["pressure"]["io"].get("full", 100) >= 1):
        reasons.append("pressure")
    if reasons:
        raise ValueError("HOST_CAPACITY_REQUIRED: " + ", ".join(reasons))


def compare(before, after, workspace_id):
    for key in ("hostname", "daemon_id", "networks", "firewall", "egress_firewall", "rules", "routes"):
        if before[key] != after[key]:
            raise ValueError("HOST_CHANGED: " + key)
    def neighbors(facts):
        return [row for row in facts["containers"] if workspace_id is None or row["workspace_id"] != workspace_id]
    if neighbors(before) != neighbors(after):
        raise ValueError("HOST_CHANGED: neighboring containers")
    if not set(before["volumes"]).issubset(after["volumes"]):
        raise ValueError("HOST_CHANGED: a pre-existing volume disappeared")
    owned_volumes = {mount["Name"] for row in after["containers"]
                     if workspace_id is not None and row["workspace_id"] == workspace_id for mount in row["mounts"]
                     if mount["Type"] == "volume"}
    if set(after["volumes"]) - set(before["volumes"]) - owned_volumes:
        raise ValueError("HOST_CHANGED: an unrelated volume appeared")


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
