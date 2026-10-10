#!/usr/bin/env python3
"""Apply the reviewed per-workspace PID policy to one quiesced Coder workspace."""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

POLICY_NORMALIZED_SHA256 = "9aa4ae776e0b6716218bbaad85c54a6002aa7679bc3e79b966f8683a06fd0edb"
RECORD_ROOT = Path("/opt/remote-dev/coder/pids-rollouts")
IMAGE_PATTERN = r'(resource "docker_container" "workspace"\s*\{\s*count\s*=\s*[^\n]+\n\s*image\s*=\s*)"[^"\n]+"'


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def source_from_archive(payload):
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        files = [m for m in archive.getmembers() if m.name in ("main.tf", "./main.tf")]
        if len(files) != 1 or not files[0].isfile() or files[0].size > 2_000_000:
            raise ValueError("Unsupported template archive")
        if any(m.isfile() and m.name.endswith((".tf", ".tf.json")) and m not in files
               for m in archive.getmembers()):
            raise ValueError("Multi-file Terraform template requires an adapter")
        return archive.extractfile(files[0]).read().decode()


def policy_for_image(source, image):
    if not re.fullmatch(r"remote-dev-workspace:[A-Za-z0-9_.-]+", image):
        raise ValueError("Workspace image tag is outside the Remote Dev registry")
    changed, count = re.subn(IMAGE_PATTERN, lambda m: m[1] + json.dumps(image), source)
    if count != 1 or "terraform_data\" \"workspace_pids\"" not in changed:
        raise ValueError("Reviewed PID policy or image reference is missing")
    return changed


class Rollout:
    def __init__(self, args):
        self.args = args
        self.origin = f"http://127.0.0.1:{args.port}/api/v2/"
        self.token = Path("/opt/remote-dev/coder/operator.session").read_text().strip()

    def api(self, path, data=None, raw=False):
        request = urllib.request.Request(
            self.origin + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Coder-Session-Token": self.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read() if raw else json.load(response)

    def wait(self, build):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            row = self.api("workspacebuilds/" + build["id"])
            if row["job"]["status"] == "succeeded":
                return row
            if row["job"]["status"] in ("failed", "canceled"):
                raise ValueError("Coder build failed: " + build["id"])
            time.sleep(2)
        raise ValueError("Coder build still pending: " + build["id"])

    def inspect_container(self, workspace_id):
        ids = run("docker", "ps", "-q", "--filter", "label=coder.workspace_id=" + workspace_id).split()
        if len(ids) != 1:
            raise ValueError("Expected exactly one running workspace container")
        return json.loads(run("docker", "inspect", ids[0]))[0]

    def quiescence(self, container, role):
        if role != "runner":
            return
        homes = [Path(m["Source"]) for m in container["Mounts"] if m["Destination"] == "/home/coder"]
        if len(homes) != 1:
            raise ValueError("Runner home volume is ambiguous")
        root = homes[0] / ".local/share/remote-dev"
        if not root.exists():
            return
        for control in (root / "local-agent").glob("*"):
            for subdir, pattern in (("processes/active", "*"),
                                    ("processes/recovery", "*"),
                                    ("executions", "*.running")):
                if any(p.is_file() for p in (control / subdir).glob(pattern)):
                    raise ValueError("Runner has active or unresolved execution records")

    def save(self, stage, **extra):
        self.receipt.update(stage=stage, **extra)
        temporary = self.record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.receipt, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.record_path)
        print("STAGE", stage, flush=True)

    def publish(self, source, name, template):
        with tempfile.TemporaryDirectory(prefix="remote-dev-pids-") as directory:
            file = Path(directory) / "main.tf"
            file.write_text(source)
            remote_dir = "/tmp/" + name
            run("docker", "exec", "remote-dev-coder-coder-1", "mkdir", "-p", remote_dir)
            run("docker", "cp", str(file), "remote-dev-coder-coder-1:" + remote_dir + "/main.tf")
            run("docker", "exec", "-e", "CODER_URL=http://127.0.0.1:7080",
                "-e", "CODER_SESSION_TOKEN=" + self.token, "remote-dev-coder-coder-1",
                "/opt/coder", "templates", "push", template["name"], "--name", name,
                "--directory", remote_dir, "--activate=false", "--ignore-lockfile", "--yes",
                "--variable", "remote_dev_coder_access_url=" + self.args.coder_url)
        matches = [v for v in self.api("templates/" + template["id"] + "/versions") if v["name"] == name]
        if len(matches) != 1 or matches[0]["job"]["status"] != "succeeded":
            raise ValueError("Published PID policy version could not be verified")
        version = matches[0]
        actual = source_from_archive(self.api("files/" + version["job"]["file_id"], raw=True))
        if actual != source:
            raise ValueError("Published PID policy source changed")
        return version["id"]

    def existing_version(self, template, name, source):
        matches = [v for v in self.api("templates/" + template["id"] + "/versions") if v["name"] == name]
        if not matches:
            return None
        if len(matches) != 1 or matches[0]["job"]["status"] != "succeeded":
            raise ValueError("Existing PID policy version is ambiguous")
        actual = source_from_archive(self.api("files/" + matches[0]["job"]["file_id"], raw=True))
        if actual != source:
            raise ValueError("Existing PID policy version has different source")
        return matches[0]["id"]

    def execute(self):
        a = self.args
        workspace = self.api("users/me/workspace/" + a.workspace)
        old = workspace["latest_build"]
        parameters = self.api("workspacebuilds/" + old["id"] + "/parameters")
        values = {p["name"]: p["value"] for p in parameters}
        role = values.get("workspace_role")
        capability = values.get("docker_capability")
        if old["status"] != "running" or role not in ("runner", "development") or capability not in ("core", "containers"):
            raise ValueError("Require a running Remote Dev workspace with known role and capability")
        if workspace.get("automatic_updates") != "never":
            raise ValueError("Automatic updates must be disabled for a scoped rollout")
        container = self.inspect_container(workspace["id"])
        image = container["Config"]["Image"]
        if run("docker", "image", "inspect", "--format", "{{.Id}}", image) != container["Image"]:
            raise ValueError("Running workspace image tag changed")
        self.quiescence(container, role)
        template = self.api("templates/" + workspace["template_id"])
        if template["name"] != "remote-dev-docker":
            raise ValueError("Unsupported Coder template")
        active = self.api("templateversions/" + template["active_version_id"])
        if active["job"]["status"] != "succeeded":
            raise ValueError("Active template version is not ready")
        base = source_from_archive(self.api("files/" + active["job"]["file_id"], raw=True))
        normalized = policy_for_image(base, "remote-dev-workspace:policy-reference")
        if hashlib.sha256(normalized.encode()).hexdigest() != POLICY_NORMALIZED_SHA256:
            raise ValueError("Active PID policy differs from the reviewed source")
        source = policy_for_image(base, image)
        digest = hashlib.sha256(source.encode()).hexdigest()
        name = "pids-" + digest[:12]
        cap = 2048 if capability == "containers" else 1024
        print(json.dumps({"workspace": a.workspace, "role": role, "capability": capability,
                          "image": image, "previous_build": old["id"],
                          "previous_version": old["template_version_id"],
                          "release": name, "pids_limit": cap, "apply": a.apply}), flush=True)
        if not a.apply:
            return
        if not a.maintenance_ready:
            raise ValueError("Apply requires --maintenance-ready after pausing the Local Agent or other client work")
        RECORD_ROOT.mkdir(mode=0o700, exist_ok=True)
        with (RECORD_ROOT / (workspace["id"] + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.record_path = RECORD_ROOT / (workspace["id"] + "-" + name + ".json")
            if self.record_path.exists():
                raise ValueError("Existing rollout receipt requires inspection: " + str(self.record_path))
            self.receipt = {"workspace": a.workspace, "workspace_id": workspace["id"],
                            "old_build": old["id"], "old_version": old["template_version_id"],
                            "image": image, "source_sha256": digest, "pids_limit": cap}
            self.save("prepared")
            if source == base:
                target = active["id"]
            else:
                target = self.existing_version(template, name, source)
                if target is None:
                    self.save("publishing")
                    target = self.publish(source, name, template)
            self.save("published", new_version=target)
            current = self.api("workspaces/" + workspace["id"])
            if current["latest_build"]["id"] != old["id"] or self.api("templates/" + template["id"])["active_version_id"] != active["id"]:
                raise ValueError("Workspace or active policy changed before restart")
            self.quiescence(self.inspect_container(workspace["id"]), role)
            self.save("stopping")
            stopped = self.api("workspaces/" + workspace["id"] + "/builds", {"transition": "stop"})
            self.save("stop_requested", stop_build=stopped["id"])
            self.wait(stopped)
            if self.api("workspaces/" + workspace["id"])["latest_build"]["id"] != stopped["id"]:
                raise ValueError("Concurrent build after stop; start refused")
            self.save("starting")
            started = self.api("workspaces/" + workspace["id"] + "/builds",
                               {"transition": "start", "template_version_id": target,
                                "rich_parameter_values": parameters})
            self.save("start_requested", start_build=started["id"])
            self.wait(started)
            current = self.api("workspaces/" + workspace["id"])
            if current["latest_build"]["id"] != started["id"] or current["latest_build"]["template_version_id"] != target:
                raise ValueError("Workspace build identity changed")
            updated = self.inspect_container(workspace["id"])
            if (updated["Config"]["Image"] != image or updated["HostConfig"]["PidsLimit"] != cap or
                    {"Name": "nproc", "Soft": -1, "Hard": -1} not in updated["HostConfig"]["Ulimits"]):
                raise ValueError("Updated container failed PID policy or image verification")
            actual = run("docker", "exec", updated["Id"], "sh", "-c",
                         "cat /sys/fs/cgroup/pids.max; grep 'Max processes' /proc/self/limits")
            if actual.splitlines()[0] != str(cap) or "unlimited" not in actual.splitlines()[1]:
                raise ValueError("Effective container PID policy differs")
            for _ in range(30):
                if self.api("workspaces/" + workspace["id"]).get("health", {}).get("healthy"):
                    self.save("verified", start_build=started["id"], container_id=updated["Id"])
                    print("READY", a.workspace, "PidsLimit=" + str(cap), flush=True)
                    return
                time.sleep(2)
            raise ValueError("Coder agent did not become healthy")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--coder-url", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--maintenance-ready", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.workspace):
        parser.error("invalid workspace name")
    try:
        Rollout(args).execute()
    except Exception as error:
        reason = str(error) if type(error) is ValueError else type(error).__name__
        print("PID_ROLLOUT_FAILED", reason, "Inspect the exact receipt and Coder build before recovery.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
