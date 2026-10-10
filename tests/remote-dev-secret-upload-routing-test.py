#!/usr/bin/env python3
"""Exercise the real secrets upload caller with disposable transport owners."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Synthetic host profiles; tests never read the deployment profiles.
os.environ["REMOTE_DEV_PROFILE_DIR"] = str(ROOT / "tests/fixtures/host-profiles")


class RoutingTests(unittest.TestCase):
    def invoke(self, role, status=0):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            (fixture / 'bin').mkdir()
            (fixture / 'templates/docker').mkdir(parents=True)
            for name in ('remote-dev-secret-runner-upload.py', 'remote-dev-secret-upload.py'):
                (fixture / 'bin' / name).symlink_to(ROOT / 'bin' / name)
            (fixture / 'templates/docker/mode-guard.py').symlink_to(ROOT / 'templates/docker/mode-guard.py')
            record, payload = fixture / 'record.json', fixture / 'payload'
            owner = fixture / 'bin/remote-dev-local-agent'
            owner.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
envelope=json.loads(sys.stdin.buffer.readline())
Path(os.environ['FIXTURE_RECORD']).write_text(json.dumps({'owner':'local-agent','argv':sys.argv[1:],'environment':envelope['environment']}))
Path(os.environ['FIXTURE_PAYLOAD']).write_bytes(sys.stdin.buffer.read())
sys.exit(int(os.environ['FIXTURE_STATUS']))
''')
            owner.chmod(0o700)
            coder = fixture / 'coder'
            coder.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['FIXTURE_RECORD']).write_text(json.dumps({'owner':'coder','argv':sys.argv[1:]}))
Path(os.environ['FIXTURE_PAYLOAD']).write_bytes(sys.stdin.buffer.read())
sys.exit(int(os.environ['FIXTURE_STATUS']))
''')
            coder.chmod(0o700)
            source = (ROOT / 'bin/remote-dev-secrets').read_text()
            functions = source[source.index('coder_env_argument() {'):source.index('\nupload_one() {')]
            (fixture / 'functions.sh').write_text(functions)
            environment = dict(os.environ, repo_root=str(fixture), workspace_role=role,
                               local_root='/synthetic/local project', remote_root='/home/coder/projects/demo',
                               workspace='demo', REMOTE_DEV_PROFILE='example-profile',
                               coder_url='https://coder.example.test', REMOTE_DEV_MODE_OPERATION_ID='a' * 64,
                               REMOTE_DEV_MODE_TOKEN='synthetic-capability', FIXTURE_CODER=str(coder),
                               FIXTURE_RECORD=str(record), FIXTURE_PAYLOAD=str(payload), FIXTURE_STATUS=str(status))
            body = b'synthetic\x00binary\xff\n'
            result = subprocess.run(['bash', '-c', '''set -euo pipefail
source "$repo_root/functions.sh"
coder_command=("$FIXTURE_CODER")
upload_stdin .env true
'''], env=environment, input=body, capture_output=True)
            return result, json.loads(record.read_text()), payload.read_bytes(), body

    def test_runner_uses_exact_existing_execution_owner_and_private_stdin(self):
        result, record, payload, body = self.invoke('runner')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record['owner'], 'local-agent')
        argv = record['argv']
        self.assertEqual(argv[argv.index('--for-context') + 1], '/synthetic/local project')
        self.assertEqual(argv[argv.index('--profile') + 1], 'example-profile')
        self.assertEqual(argv[argv.index('--coder-url') + 1], 'https://coder.example.test')
        self.assertNotIn('synthetic-capability', json.dumps(argv))
        self.assertEqual(record['environment']['REMOTE_DEV_SECRET_ROOT'], '/home/coder/projects/demo')
        self.assertEqual(record['environment']['REMOTE_DEV_SECRET_REL'], '.env')
        self.assertEqual(record['environment']['REMOTE_DEV_SECRET_REPLACE'], 'true')
        self.assertEqual(record['environment']['REMOTE_DEV_MODE_TOKEN'], 'synthetic-capability')
        self.assertEqual(payload, body)

    def test_development_keeps_direct_coder_route(self):
        result, record, payload, body = self.invoke('development')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(record['owner'], 'coder')
        self.assertIn('ssh', record['argv'])
        self.assertIn('--disable-autostart=true', record['argv'])
        self.assertEqual(payload, body)

    def test_owner_refusal_propagates_for_both_routes(self):
        for role in ('runner', 'development'):
            result, _, _, _ = self.invoke(role, 79)
            self.assertEqual(result.returncode, 79, result.stderr)


if __name__ == '__main__':
    unittest.main()
