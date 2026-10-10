#!/usr/bin/env python3
"""Frozen workspace preparation owned by the existing workspace entry point.

Mode supplies an operation journal and never handles Coder credentials or
implements creation itself. Unknown create outcomes are deliberately not retried.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


CAPACITY = module(ROOT / "deploy/workspace-capacity.py")


def private(path):
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("CODER_CONTEXT_UNTRUSTED: require an owner-only regular credential file")
    return path.read_text().strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("CODER_ORIGIN_CHANGED: redirects are forbidden")


class Owner:
    def __init__(self, profile, origin):
        parts = urllib.parse.urlsplit(origin)
        # HTTPS for a published Coder, or the Mac end of an SSH forward.
        forward = parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port is not None
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]*", profile) or not (parts.scheme == "https" or forward) or
                not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment):
            raise ValueError("WORKSPACE_IDENTITY_INVALID: explicit profile and HTTPS or SSH-forward origin required")
        self.profile, self.origin = profile, origin
        self.config = Path(os.environ.get("REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT",
                           str(Path.home() / "Library/Application Support/remote-dev/coder"))) / profile

    def api(self, path, data=None, raw=False):
        if private(self.config / "url").rstrip("/") != self.origin:
            raise ValueError("CODER_ORIGIN_CHANGED: authenticate the selected existing workspace context")
        token = private(self.config / "session")
        request = urllib.request.Request(self.origin + "/api/v2/" + path,
                    data=None if data is None else json.dumps(data).encode(),
                    headers={"Coder-Session-Token": token, "Content-Type": "application/json", "Accept": "application/json",
                             "User-Agent": "remote-dev-workspace/1.0"})
        with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
            return response.read() if raw else json.load(response)

    def inspect(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", name):
            raise ValueError("WORKSPACE_IDENTITY_INVALID: invalid workspace name")
        me = self.api("users/me")
        try:
            row = self.api("users/me/workspace/" + urllib.parse.quote(name, safe=""))
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            return {"name": name, "owner_id": me["id"], "exists": False}
        if row["owner_id"] != me["id"] or row["name"] != name:
            raise ValueError("WORKSPACE_IDENTITY_CHANGED: selected workspace owner/name differs")
        build = row["latest_build"]
        parameters = self.api("workspacebuilds/" + build["id"] + "/parameters")
        return {"name": name, "exists": True, "owner_id": me["id"], "workspace_id": row["id"],
                "organization_id": row["organization_id"], "template_id": row["template_id"],
                "automatic_updates": row["automatic_updates"], "build_id": build["id"],
                "build_number": build["build_number"], "status": build["status"],
                "transition": build["transition"], "initiator_id": build["initiator_id"],
                "version_id": build["template_version_id"],
                "parameters": {p["name"]: p["value"] for p in parameters
                               if p["name"] in ("workspace_role", "workspace_size", "docker_capability")}}

    def host(self, code):
        profile = subprocess.run([str(ROOT / "bin/remote-dev"), "--profile", self.profile, "profile"],
                                 capture_output=True, text=True, timeout=15)
        if profile.returncode:
            raise ValueError("HOST_PROFILE_UNAVAILABLE")
        values = dict(line.split("=", 1) for line in profile.stdout.splitlines() if "=" in line)
        if values.get("coder_access_url") != self.origin:
            raise ValueError("HOST_PROFILE_CHANGED")
        host = values["ssh_host"]
        if not host or host.startswith("-"):
            raise ValueError("HOST_PROFILE_INVALID")
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                                 "-o", "ClearAllForwardings=yes", host, "python3 -B -"],
                                input=code.replace("__CODER_HOST_PORT__", str(int(values["coder_host_port"]))),
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError("HOST_AUDIT_FAILED: preserve source; inspect the selected host audit")
        return json.loads(result.stdout)

    def host_snapshot(self):
        return self.host((ROOT / "deploy/workspace-capacity.py").read_text())

    def doctor(self):
        result = subprocess.run([str(ROOT / "bin/remote-dev"), "--profile", self.profile, "doctor"],
                                capture_output=True, timeout=240)
        if result.returncode:
            raise ValueError("HOST_CONTRACT_FAILED: run remote-dev doctor for the selected profile")

    def version(self, version_id):
        if not re.fullmatch(r"[0-9a-f-]{36}", version_id or ""):
            raise ValueError("WORKSPACE_VERSION_REQUIRED: supply one explicit template version UUID")
        version = self.api("templateversions/" + version_id)
        template = self.api("templates/" + version["template_id"])
        if template["name"] != "remote-dev-docker" or version["job"]["status"] != "succeeded":
            raise ValueError("WORKSPACE_TEMPLATE_UNSUPPORTED")
        archive = self.api("files/" + version["job"]["file_id"], raw=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            seen = set()
            for member in bundle.getmembers():
                name = member.name.removeprefix("./")
                if member.isdir() and name in ("", "."):
                    continue
                # macOS tar may attach AppleDouble metadata. Terraform's
                # configuration loader ignores dot-prefixed files; this exact
                # reviewed main.tf never reads it. Do not allow other dotfiles.
                if name == "._main.tf" and name not in seen and member.isfile() and member.size <= 65536:
                    if bundle.extractfile(member).read(8) == b"\x00\x05\x16\x07\x00\x02\x00\x00":
                        seen.add(name)
                        continue
                if name != "main.tf" or name in seen or not member.isfile() or member.size > 2_000_000:
                    raise ValueError("WORKSPACE_TEMPLATE_UNSUPPORTED: only reviewed main.tf and valid AppleDouble metadata are allowed")
                seen.add(name)
                if bundle.extractfile(member).read() != (ROOT / "templates/docker/main.tf").read_bytes():
                    raise ValueError("WORKSPACE_TEMPLATE_CHANGED: main.tf differs from the reviewed template")
            members = [m for m in bundle.getmembers() if m.name in ("main.tf", "./main.tf")]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 2_000_000:
                raise ValueError("WORKSPACE_TEMPLATE_UNSUPPORTED")
            main = bundle.extractfile(members[0]).read()
        # Admission limits are meaningful only for this reviewed infrastructure
        # contract. Other template layouts require their own owner adaptation.
        if main != (ROOT / "templates/docker/main.tf").read_bytes():
            raise ValueError("WORKSPACE_TEMPLATE_CHANGED: selected template differs from the reviewed infrastructure contract")
        pid_policy = re.search(
            r'workspace_pids_limit\s*=\s*data\.coder_parameter\.docker_capability\.value\s*==\s*"containers"\s*\?\s*(\d+)\s*:\s*(\d+)',
            main.decode())
        if not pid_policy:
            raise ValueError("WORKSPACE_PID_POLICY_UNKNOWN: selected template has no reviewed PID budget")
        containers_pids = int(pid_policy.group(1))
        limits = {name: {"cpu": int(cpu), "memory": int(memory) * 1024**2, "pids": containers_pids}
                  for name, cpu, memory in re.findall(
                      r"([a-z][a-z-]*)\s*=\s*\{\s*cpu_cores\s*=\s*(\d+)\s*memory_mb\s*=\s*(\d+)\s*\}", main.decode())}
        code = (ROOT / "deploy/workspace-images.py").read_text()
        image = self.host("import sys\nsys.argv = ['workspace-images', '--port', '__CODER_HOST_PORT__', '--version', "
                          + repr(version_id) + "]\n" + code)
        return {"version_id": version_id, "version_name": version["name"], "template_id": template["id"],
                "organization_id": template["organization_id"], "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "image": image, "limits_by_size": limits}

    def freeze(self, name, role, size=None, version_id=None):
        before = self.inspect(name)
        if before["exists"]:
            if before["status"] != "stopped" or before["automatic_updates"] != "never":
                raise ValueError("WORKSPACE_PREPARE_UNSUPPORTED: start requires a stopped workspace with automatic updates disabled")
            parameters = before["parameters"]
            if parameters.get("workspace_role") != role or parameters.get("docker_capability") != "containers":
                raise ValueError("WORKSPACE_ROLE_MISMATCH")
            if version_id and version_id != before["version_id"] or size and size != parameters["workspace_size"]:
                raise ValueError("WORKSPACE_PREPARE_UNSUPPORTED: start cannot resize or update a workspace")
            version_id, size = before["version_id"], parameters["workspace_size"]
        elif role != "runner":
            raise ValueError("WORKSPACE_PREPARE_UNSUPPORTED: only a missing runner can be created")
        version = self.version(version_id)
        if size not in version["limits_by_size"]:
            raise ValueError("WORKSPACE_SIZE_REQUIRED: supply an explicit reviewed runner size")
        if before["exists"] and before["template_id"] != version["template_id"]:
            raise ValueError("WORKSPACE_TEMPLATE_CHANGED")
        return {"kind": "start" if before["exists"] else "create", "name": name, "role": role,
                "size": size, "capability": "containers", "before": before, "version": version,
                "limits": version["limits_by_size"][size]}

    def prepare(self, action, receipt, save, disk_bytes):
        if receipt.get("ready"):
            current = self.inspect(action["name"])
            if current != receipt["after"]:
                raise ValueError("WORKSPACE_CHANGED: prepared workspace differs from its receipt")
            return current
        current = self.inspect(action["name"])
        version = self.version(action["version"]["version_id"])
        if version != action["version"]:
            raise ValueError("WORKSPACE_VERSION_CHANGED")
        if not receipt.get("intent"):
            if current != action["before"]:
                raise ValueError("WORKSPACE_CHANGED: destination differs from the frozen plan")
            self.doctor()
            facts = self.host_snapshot()
            CAPACITY.compare(action["host_at_plan"], facts, action["before"].get("workspace_id"))
            CAPACITY.admit(facts, action["limits"], disk_bytes, action.get("unbounded_budget"))
            receipt.update(intent=True, host_before=facts)
            save()
            if action["kind"] == "start":
                result = self.api("workspaces/" + current["workspace_id"] + "/builds",
                                  {"transition": "start", "template_version_id": version["version_id"]})
                receipt["build_id"] = result["id"]
                save()
            else:
                # Same API as the workspace CLI, but use the frozen UUID rather
                # than resolving the version name again after plan review.
                result = self.api("users/" + action["before"]["owner_id"] + "/workspaces",
                    {"name": action["name"], "template_version_id": version["version_id"],
                     "automatic_updates": "never", "rich_parameter_values": [
                         {"name": "workspace_role", "value": action["role"]},
                         {"name": "workspace_size", "value": action["size"]},
                         {"name": "docker_capability", "value": action["capability"]}]})
                receipt["build_id"], receipt["workspace_id"] = result["latest_build"]["id"], result["id"]
                save()
        current = self.inspect(action["name"])
        if action["kind"] == "create" and not receipt.get("workspace_id"):
            raise ValueError("WORKSPACE_OUTCOME_UNKNOWN: create result was not durably observed; inspect manually and preserve resources")
        expected_id = action["before"].get("workspace_id", receipt.get("workspace_id"))
        expected_number = action["before"].get("build_number", 0) + 1
        if (not current["exists"] or current["workspace_id"] != expected_id or
                current["owner_id"] != action["before"]["owner_id"] or
                current["version_id"] != version["version_id"] or current["template_id"] != version["template_id"] or
                current["organization_id"] != version["organization_id"] or current["automatic_updates"] != "never" or
                current["build_number"] != expected_number or current["transition"] != "start" or
                current["initiator_id"] != current["owner_id"] or
                receipt.get("build_id", current["build_id"]) != current["build_id"] or
                current["parameters"] != {"workspace_role": action["role"], "workspace_size": action["size"], "docker_capability": action["capability"]}):
            raise ValueError("WORKSPACE_OUTCOME_UNKNOWN: build identity differs; preserve resources for reconciliation")
        receipt["build_id"] = current["build_id"]
        save()
        if current["status"] != "running":
            raise ValueError("WORKSPACE_BUILD_PENDING: inspect the recorded build and resume this operation when running")
        self.doctor()
        after = self.host_snapshot()
        CAPACITY.compare(receipt["host_before"], after, current["workspace_id"])
        CAPACITY.admit(after, {"cpu": 0, "memory": 0}, disk_bytes, action.get("unbounded_budget"))
        owned = [row for row in after["containers"] if row["workspace_id"] == current["workspace_id"] and row["running"]]
        if (len(owned) != 1 or owned[0]["image"] != version["image"]["image_id"] or
                owned[0]["memory"] != action["limits"]["memory"] or owned[0]["cpu"] != action["limits"]["cpu"] or
                owned[0]["runtime"] != "sysbox-runc" or owned[0]["ports"] or
                owned[0]["pids_limit"] != action["limits"]["pids"] or
                {"Name": "nproc", "Soft": -1, "Hard": -1} not in (owned[0]["ulimits"] or [])):
            raise ValueError("WORKSPACE_LIMITS_CHANGED: actual runtime, image, ports or resource limits differ")
        receipt.update(ready=True, after=current, host_after=after)
        save()
        return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--coder-url", required=True)
    parser.add_argument("command", choices=("inspect",))
    parser.add_argument("workspace")
    args = parser.parse_args()
    try:
        print(json.dumps(Owner(args.profile, args.coder_url).inspect(args.workspace), sort_keys=True))
    except Exception as error:
        # Never echo HTTP bodies, CLI output or exception URLs from an auth path.
        print("WORKSPACE_INSPECT_FAILED: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
