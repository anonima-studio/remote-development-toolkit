import hashlib
import gzip
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / "bin/remote-dev-remote-manifest.py"
HEADER = "path\ttype\tmode\tdigest\tsymlink_target\tstate\n"


class RemoteManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.txt").write_text("source\n")
        (self.root / "source.txt").chmod(0o644)
        self.row = "source.txt\tfile\t0644\t" + hashlib.sha256(b"source\n").hexdigest() + "\t-\tpresent\n"

    def audit(self, manifest=None, allowed=()):
        return subprocess.run(
            [sys.executable, str(HELPER), str(self.root), "2", "node_modules", ".yarn/cache", *allowed],
            input=HEADER + (self.row if manifest is None else manifest), text=True, capture_output=True,
        )

    def test_clean_and_ignored_nested_runtime(self):
        for relative in ["pkg/node_modules/large", "pkg/.yarn/cache/large", "private/ignored"]:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not source")
        result = self.audit(allowed=["private"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, HEADER + self.row)

    def test_content_and_mode_drift(self):
        (self.root / "source.txt").write_text("remote edit\n")
        (self.root / "source.txt").chmod(0o755)
        result = self.audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source.txt\tfile\t0755\t", result.stdout)
        self.assertNotIn(self.row.strip(), result.stdout)

    def test_compressed_match_audits_fresh_content_and_returns_drift(self):
        def audit():
            return subprocess.run(
                [sys.executable, str(HELPER), '--compressed-match', str(self.root), '0'],
                input=gzip.compress((HEADER+self.row).encode()), capture_output=True,
            )
        self.assertEqual(audit().stdout, b'REMOTE_MANIFEST_MATCH\n')
        source = self.root / 'source.txt'
        original = source.stat()
        source.write_text('changed')
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        mismatch = audit()
        self.assertEqual(mismatch.returncode, 0, mismatch.stderr)
        self.assertTrue(mismatch.stdout.startswith(HEADER.encode()))
        self.assertNotIn(self.row.encode(), mismatch.stdout)

    def test_deleted_path_and_reappearance(self):
        row = "gone\tmissing\t0000\t-\t-\tdeleted\n"
        self.assertEqual(self.audit(self.row + row).stdout, HEADER + self.row + row)
        (self.root / "gone").mkdir()
        self.assertIn("gone\tunexpected\t0000\t-\t-\tpresent", self.audit(self.row + row).stdout)

    def test_symlink_and_unexpected_paths(self):
        os.symlink("source.txt", self.root / "link")
        (self.root / "empty").mkdir()
        result = self.audit()
        self.assertIn("link\tunexpected-symlink\t0777\t", result.stdout)
        self.assertIn("empty\tunexpected-directory\t", result.stdout)
        row = "link\tsymlink\t0777\t" + hashlib.sha256(b"source.txt").hexdigest() + "\tsource.txt\tpresent\n"
        (self.root / "empty").rmdir()
        self.assertEqual(self.audit(self.row + row).stdout, HEADER + self.row + row)

    def test_invalid_remote_name_and_manifest(self):
        (self.root / "bad\tname").write_text("bad")
        self.assertIn("__INVALID_REMOTE_PATH__", self.audit().stdout)
        result = self.audit("../escape\tfile\t0644\t-\t-\tpresent\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REMOTE_MANIFEST_INVALID_PATH", result.stderr)

    def transport(self, command, payload=""):
        source = (HELPER.parent / "remote-dev-local-agent").read_text()
        functions = source[source.index("bounded_transport_pid=\n"):source.index("\nrequire_runner_role()")]
        return subprocess.run(["bash", "-c", functions + "\n" + command],
                              input=payload, text=True, capture_output=True, timeout=10)

    def test_bounded_transport_preserves_manifest_stdin(self):
        command = "bounded_command 3 " + shlex.quote(sys.executable) + " -c " + shlex.quote(
            "import sys; sys.stdout.write(sys.stdin.read())")
        payload = HEADER + self.row
        result = self.transport(command, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    def test_bounded_transport_timeout(self):
        command = "bounded_command 1 " + shlex.quote(sys.executable) + " -c " + shlex.quote(
            "import time; time.sleep(20)")
        result = self.transport(command)
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_foreground_waiter_removed_during_identity_read(self):
        source = (HELPER.parent / "remote-dev-local-agent").read_text()
        function = source[source.index("foreground_waiter_exists() {"):source.index("\nacquire_lock() {")]
        waiters = self.root / "operation.waiters"
        waiters.mkdir()
        waiter = waiters / "waiter-123.tsv"
        waiter.write_text("pid\t123\n")
        script = """
binding_dir=$1
binding_value() { printf '123\\n'; }
live_identity_matches() { rm "$1"; return 1; }
identity_matches_binding() { return 1; }
fail() { printf '%s\\n' "$*" >&2; }
""" + function + "\nforeground_waiter_exists\n"
        result = subprocess.run(["bash", "-c", script, "test", str(self.root)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stderr, "")
        # A still-present foreign record must continue to fail closed.
        waiter.write_text("pid\t123\n")
        result = subprocess.run(["bash", "-c", script.replace('rm "$1"; ', ''), "test", str(self.root)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertIn("OPERATION_WAITER_INVALID", result.stderr)

    def test_smoke_session_helpers_handle_wrapped_descendant_pids(self):
        source = (HELPER.parent.parent / "tests/remote-dev-local-agent-smoke.sh").read_text()
        for name in ("pgrep", "pkill"):
            marker = 'cat > "$fake_bin/' + name + '" <<\'PY\'\n'
            helper = source.split(marker, 1)[1].split("\nPY\n", 1)[0]
            harness = """
import os, signal, subprocess, sys
from unittest.mock import patch
name = sys.argv[1]
source = sys.stdin.read()
sys.argv = [name] + (["-TERM"] if name == "pkill" else []) + ["-s", "99990"]
def session(pid):
    return 99990
def kill(pid, sig):
    assert sig == signal.SIGTERM
    print(pid)
with patch.object(subprocess, "check_output", return_value="12 500 99990\\n"), \\
     patch.object(os, "getsid", side_effect=session), \\
     patch.object(os, "getpid", return_value=500), \\
     patch.object(os, "kill", side_effect=kill):
    exec(compile(source, name, "exec"))
"""
            result = subprocess.run([sys.executable, "-c", harness, name],
                                    input=helper, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "12\n99990\n")


if __name__ == "__main__":
    unittest.main()
