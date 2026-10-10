#!/usr/bin/env python3
"""Actual endpoint wire format over simulated SSH, including large inventories."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("mode_transport", str(ROOT / "bin/remote-dev-mode"))
spec = importlib.util.spec_from_loader(loader.name, loader)
M = importlib.util.module_from_spec(spec)
loader.exec_module(M)


class TransportTests(unittest.TestCase):
    def test_large_projection_and_binary_package_do_not_enter_ssh_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home = base / "home"
            (home / "projects").mkdir(parents=True)
            marker = home / ".local/share/remote-dev/workspace-role"
            marker.parent.mkdir(parents=True)
            marker.write_text("runner\n")
            source, runner = base / "source", home / "projects/fixture"
            source.mkdir(); runner.mkdir()
            fake = base / "bin"
            fake.mkdir()
            coder = fake / "coder"
            coder.write_text("#!/usr/bin/env python3\n" +
                "import os,subprocess,sys\n"
                "from pathlib import Path\n"
                "a=sys.argv[1:]; assert sum(len(x) for x in a)<8192\n"
                "assert '--disable-autostart' in a and '--wait=no' in a\n"
                "command=a[a.index('--')+1]; sys.exit(subprocess.run(['bash','-c',command]).returncode)\n")
            coder.chmod(0o755)
            with patch.dict(os.environ, {"HOME": str(home), "PATH": str(fake) + os.pathsep + os.environ["PATH"],
                                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
                def git(*args):
                    subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
                git("init", "-q", "-b", "main")
                git("config", "user.name", "Fixture")
                git("config", "user.email", "fixture@example.invalid")
                for index in range(700):
                    name = str(index).zfill(4) + "-" + "path" * 20
                    (source / name).write_bytes(b"\x00binary\xff\n")
                    shutil.copy2(source / name, runner / name)
                git("add", "."); git("commit", "-qm", "large inventory")
                snapshot = M.TRANSFER.inspect(source)
                self.assertGreater(len(M.encode(snapshot)), 128 * 1024)
                endpoint = M.Endpoint("fixture", "https://coder.example.invalid", "runner", "runner")
                identity = {"root": str(runner), "project_id": "fixture", "operation_id": "a" * 64,
                            "token": "b" * 64, "snapshot_id": snapshot["snapshot_id"], "allowed": []}
                endpoint.request("claim", identity)
                result = endpoint.request("projection", {**identity, "snapshot": snapshot})
                self.assertEqual(result["tracked_existing"], 700)
                package = base / "package.zip"
                captured = M.TRANSFER.export(source, package, [])
                endpoint.request("upload", {**identity, "package_sha256": captured["sha256"]}, input_path=package)
                result = endpoint.request("restore", {**identity, "package_sha256": captured["sha256"]})
                self.assertEqual(result["snapshot_id"], snapshot["snapshot_id"])
                endpoint.request("rollback", identity)
                endpoint.request("release", identity)


if __name__ == "__main__":
    unittest.main()
