#!/usr/bin/env python3
"""Regression coverage for cleanup of stopped-workspace image dependencies."""

import contextlib
import importlib.util
import io
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "workspace_images", Path(__file__).resolve().parents[1] / "deploy/workspace-images.py"
)
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)
HASH = "a" * 64
IMAGE = "remote-dev-workspace:node24-20260905-" + HASH[:11]
OLD = "remote-dev-workspace:node24-20260817-" + "b" * 12
IMAGE_ID = "sha256:" + "c" * 64


def archive(source):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        info = tarfile.TarInfo("main.tf")
        data = source.encode()
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return output.getvalue()


def workspace(name, version, status):
    return dict(name=name, owner_name="owner", template_name="remote-dev-docker",
                template_id="template", latest_build=dict(template_version_id=version, status=status))


class InventoryTests(unittest.TestCase):
    def test_explicit_inactive_version_is_verified_without_retention(self):
        def get(path, raw=False):
            if path == "templateversions/inactive":
                return {"id": "inactive", "template_id": "template", "job": {"status": "succeeded", "file_id": "file"}}
            self.assertEqual(path, "files/file")
            self.assertTrue(raw)
            return archive('image = "'+IMAGE+'"\n')
        with patch.object(images, "verify_image", return_value=IMAGE_ID) as verify:
            result = images.inspect_version(get, "inactive")
            verify.assert_called_once_with(IMAGE, retain=False)
            self.assertEqual(result["version_id"], "inactive")
            self.assertEqual(result["image_id"], IMAGE_ID)

    def get(self, path, raw=False):
        if path == "workspaces?limit=100&offset=0":
            return {"workspaces": [workspace("legacy", "old", "failed")], "count": 2}
        if path == "workspaces?limit=100&offset=1":
            return {"workspaces": [workspace("runner", "new", "stopped")], "count": 2}
        if path == "templates/template":
            return {"active_version_id": "new"}
        if path.startswith("templateversions/"):
            return {"job": {"file_id": path.split("/")[1]}}
        if path.startswith("files/"):
            self.assertTrue(raw)
            return archive('  image = "' + (OLD if path.endswith("old") else IMAGE) + '"\n')
        self.fail(path)

    def test_current_image_cannot_hide_missing_failed_workspace_image(self):
        inventory = images.dependencies(self.get)
        self.assertEqual(inventory[OLD], {"workspace:owner/legacy(failed)"})
        self.assertEqual(inventory[IMAGE], {"workspace:owner/runner(stopped)", "active-template:remote-dev-docker"})

        def verify(image, retain):
            if image == OLD:
                raise ValueError("missing image")
            return IMAGE_ID

        with patch.object(images, "verify_image", side_effect=verify), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(images.audit(inventory), 1)
        self.assertIn("owner/legacy(failed)", output.getvalue())
        self.assertIn("Preserve their volumes", output.getvalue())

    def test_incomplete_pagination_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            images.dependencies(lambda _: {"workspaces": [], "count": 1})

    def test_nonliteral_or_ambiguous_template_fails_closed(self):
        for source in ['image = var.image', f'image = "{IMAGE}"\nimage = "{OLD}"']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                images.template_image(archive(source))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.image = {"Id": IMAGE_ID, "RepoTags": [IMAGE], "Config": {"Labels": {"id0.remote-dev.workspace-source-sha256": HASH}}}
        self.reference = {"Image": IMAGE_ID, "State": {"Status": "created"},
                          "Config": {"Labels": {"id0.remote-dev.purpose": "image-retention", "id0.remote-dev.owner": "remote-dev"}},
                          "Mounts": [], "HostConfig": {"NetworkMode": "none", "PortBindings": {}}}

    def test_read_only_audit_and_explicit_retention(self):
        for retain in (False, True):
            with self.subTest(retain=retain), patch.object(images, "docker", side_effect=[self.image, self.reference]), patch.object(images.subprocess, "run") as run:
                self.assertEqual(images.verify_image(IMAGE, retain), IMAGE_ID)
                self.assertEqual(run.call_count, int(retain))
                if retain:
                    self.assertEqual(run.call_args.args[0][-2:], [IMAGE, HASH])

    def test_source_mismatch_and_aliases_refuse_mutation(self):
        for field in ("source", "alias"):
            if field == "source":
                self.image["Config"]["Labels"]["id0.remote-dev.workspace-source-sha256"] = "d" * 64
            else:
                self.image["Config"]["Labels"]["id0.remote-dev.workspace-source-sha256"] = HASH
                self.image["RepoTags"].append("another:alias")
            with self.subTest(field=field), patch.object(images, "docker", return_value=self.image), patch.object(images.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    images.verify_image(IMAGE, retain=True)
                run.assert_not_called()

    def test_running_or_coolify_owned_reference_is_rejected(self):
        self.reference["State"]["Status"] = "running"
        with patch.object(images, "docker", side_effect=[self.image, self.reference]), self.assertRaises(ValueError):
            images.verify_image(IMAGE)
        self.reference["State"]["Status"] = "created"
        self.reference["Config"]["Labels"]["coolify.managed"] = "true"
        with patch.object(images, "docker", side_effect=[self.image, self.reference]), self.assertRaises(ValueError):
            images.verify_image(IMAGE)


if __name__ == "__main__":
    unittest.main()
