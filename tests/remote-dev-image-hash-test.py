#!/usr/bin/env python3
"""Image-pin maintenance must be safe, ordered, and check all consumers."""

import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
refresh = runpy.run_path(str(ROOT / "bin/remote-dev-image-hash.py"))["refresh_workspace_image_hash"]


class ImageHashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for rel in ("templates/docker", "deploy/install.sh", "tests/remote-dev-cli-smoke.sh", "tests/pids-rollout-test.py"):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if (ROOT / rel).is_dir():
                shutil.copytree(ROOT / rel, target)
            else:
                shutil.copy2(ROOT / rel, target)
        refresh(self.root)

    def snapshot(self):
        return {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_shell_contract_matches_and_check_is_read_only(self):
        install = (self.root / "deploy/install.sh").read_text()
        start = install.index("workspace_image_hash=$(cat")
        end = install.index("\nif [[", start)
        # Execute the actual shell hashing expression, rather than a second input list.
        command = install[start:end].replace("/opt/remote-dev/", str(self.root) + "/")
        command = command.replace("sha256sum", "shasum -a 256")
        result = subprocess.check_output(["bash", "-c", command + '\nprintf "%s" "$workspace_image_hash"'], text=True)
        self.assertIn("workspace_image_expected_hash=" + result, install)
        before = self.snapshot()
        self.assertIsNone(refresh(self.root, check=True))
        self.assertEqual(before, self.snapshot())

    def test_source_change_check_repair_and_idempotence(self):
        dockerfile = self.root / "templates/docker/Dockerfile"
        dockerfile.write_text(dockerfile.read_text() + "\n# changed input\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "Run pnpm image:hash"):
            refresh(self.root, check=True)
        self.assertEqual(before, self.snapshot())
        self.assertIsNotNone(refresh(self.root))
        self.assertIsNone(refresh(self.root, check=True))
        self.assertIsNone(refresh(self.root))

    def test_tag_drift_is_detected_even_with_current_source_hash(self):
        for rel in ("deploy/install.sh", "templates/docker/main.tf", "tests/remote-dev-cli-smoke.sh", "tests/pids-rollout-test.py"):
            with self.subTest(rel=rel):
                path = self.root / rel
                original = path.read_text()
                path.write_text(re.sub(r"(remote-dev-workspace:node24-\d{8}-)[0-9a-f]{11}", r"\g<1>00000000000", original))
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "stale workspace image pins"):
                    refresh(self.root, check=True)
                self.assertEqual(before, self.snapshot())
                refresh(self.root)

    def test_missing_input_or_pin_refuses_all_writes(self):
        for rel in ("templates/docker/start-docker.sh", "tests/pids-rollout-test.py"):
            with self.subTest(rel=rel):
                path = self.root / rel
                original = path.read_bytes()
                path.unlink()
                before = self.snapshot()
                with self.assertRaises((ValueError, OSError)):
                    refresh(self.root)
                self.assertEqual(before, self.snapshot())
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
