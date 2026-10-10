import argparse
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'deploy/codex-update.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def archive(source=None, extra=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as bundle:
        sources = {'main.tf': source or (ROOT / 'templates/docker/main.tf').read_text()}
        if extra:
            sources['other.tf'] = 'locals {}'
        for name, content in sources.items():
            data = content.encode()
            item = tarfile.TarInfo(name)
            item.size = len(data)
            bundle.addfile(item, io.BytesIO(data))
    return stream.getvalue()


class Tests(unittest.TestCase):
    def test_template_changes_only_image(self):
        source = (ROOT / 'templates/docker/main.tf').read_text()
        updated = release.template_source(archive(), 'remote-dev-workspace:new')
        self.assertEqual(len([1 for a, b in zip(source.splitlines(), updated.splitlines()) if a != b]), 1)

    def test_reject_extra_terraform(self):
        with self.assertRaises(ValueError):
            release.template_source(archive(extra=True), 'image')

    def exercise(self, restart=True, role='development', drift=False, smoke=False, already=False):
        worker = object.__new__(release.Release)
        worker.args = argparse.Namespace(workspace='example-app', version='0.154.0',
                                         restart=restart, coder_url='https://coder.example')
        worker.token = 'test'
        current = {'id': 'w', 'template_id': 't', 'automatic_updates': 'never', 'latest_build':
                   {'id': 'old', 'status': 'running', 'template_version_id': 'v'}}
        requests, commands = [], []
        name = None
        def api(path, data=None, raw=False):
            requests.append((path, data))
            if path == 'users/me/workspace/example-app':
                return json.loads(json.dumps(current))
            if path == 'workspacebuilds/old/parameters':
                return [{'name': 'workspace_role', 'value': role}]
            if path == 'templateversions/v':
                return {'job': {'status': 'succeeded', 'file_id': 'f'}}
            if path == 'templates/t':
                return {'id': 't', 'name': 'remote-dev-docker', 'active_version_id': 'v'}
            if path == 'files/f':
                return archive()
            if path == 'templates/t/versions':
                return [{'name': name, 'id': 'new', 'job': {'status': 'succeeded'}}]
            if path == 'workspaces/w':
                if drift:
                    return {'latest_build': {'id': 'foreign'}}
                return current
            if path == 'workspaces/w/builds':
                current['latest_build'] = {'id': data['transition'],
                                          'template_version_id': data.get('template_version_id', 'v')}
                return current['latest_build']
            raise AssertionError(path)
        def run(args, **kwargs):
            nonlocal name
            commands.append(args)
            output = ''
            if args[1:3] == ['ps', '-q']:
                output = 'container'
            elif args[1] == 'inspect':
                output = ('sha256:' + 'b' * 64 if '--format' in args else
                          json.dumps([{'Image': 'sha256:' + 'a' * 64,
                                       'Config': {'Image': 'remote-dev-workspace:base'}}]))
            elif args[1:3] == ['image', 'inspect']:
                output = 'sha256:' + ('a' if args[-1] == 'remote-dev-workspace:base' else 'b') * 64
            elif args[1] == 'exec' and '/bin/grep' in args:
                output = "expected_codex_version='codex-cli 0.147.0'"
            elif args[1] == 'exec' and '/usr/local/bin/remote-dev-codex' in args:
                output = 'codex_version=codex-cli 0.154.0\n'
            elif '--version' in args:
                output = 'codex-cli ' + ('0.147.0' if smoke or
                    (args[1] == 'exec' and current['latest_build']['id'] == 'old' and not already)
                    else '0.154.0')
            if args[1] == 'build':
                dockerfile = (Path(args[-1]) / 'Dockerfile').read_text()
                self.assertIn('expected_codex_version', dockerfile)
                self.assertIn('codex-cli 0.154.0', dockerfile)
            if 'push' in args:
                name = args[args.index('--name') + 1]
                self.assertIn('--activate=false', args)
            return subprocess.CompletedProcess(args, 0, output)
        worker.api = api
        worker.wait = lambda build: build
        with tempfile.TemporaryDirectory() as tmp, patch.object(release, 'RECORD_ROOT', Path(tmp)), \
             patch.object(release, 'run', run), \
             patch.object(release.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            worker.execute()
        return requests, commands

    def test_check_never_mutates(self):
        requests, commands = self.exercise(restart=False)
        self.assertTrue(all(data is None for _, data in requests))
        self.assertFalse(any('build' in cmd or 'push' in cmd for cmd in commands))

    def test_complete_release_uses_exact_version(self):
        requests, commands = self.exercise()
        builds = [data for path, data in requests if path == 'workspaces/w/builds']
        self.assertEqual([data['transition'] for data in builds], ['stop', 'start'])
        self.assertEqual(builds[1]['template_version_id'], 'new')
        self.assertTrue(any('runc' in cmd for cmd in commands))
        self.assertTrue(any('sysbox-runc' in cmd for cmd in commands))

    def test_repair_outdated_launcher_at_same_codex_version(self):
        requests, commands = self.exercise(already=True)
        self.assertTrue(any(cmd[1] == 'build' for cmd in commands))
        self.assertEqual([data['transition'] for path, data in requests
                          if path == 'workspaces/w/builds'], ['stop', 'start'])

    def test_runner_refused(self):
        with self.assertRaisesRegex(ValueError, 'runners'):
            self.exercise(role='runner')

    def test_concurrent_change_refused(self):
        with self.assertRaisesRegex(ValueError, 'changed during build'):
            self.exercise(drift=True)

    def test_failed_smoke_refused(self):
        with self.assertRaisesRegex(ValueError, 'smoke failed'):
            self.exercise(smoke=True)


if __name__ == '__main__':
    unittest.main()
