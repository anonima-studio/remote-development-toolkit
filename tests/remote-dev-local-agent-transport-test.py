"""Exercise the operation-scoped SSH transport without a Coder deployment."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SOURCE = (Path(__file__).resolve().parents[1] / 'bin/remote-dev-local-agent').read_text()
FUNCTIONS = SOURCE[SOURCE.index('ssh_transport_dir=\n'):SOURCE.index('\nensure_ssh_alias()')]
FUNCTIONS += '\n' + SOURCE[SOURCE.index('coder_ssh() {'):SOURCE.index('\nrequire_runner_role()')]


class TransportTest(unittest.TestCase):
    def test_private_reuse_stdin_quoting_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'ssh'
            log = root / 'calls.jsonl'
            fake.write_text('''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['SSH_TEST_LOG'], 'a') as output:
    output.write(json.dumps(sys.argv[1:])+'\\n')
if '-O' not in sys.argv:
    sys.stdout.write(sys.stdin.read())
''')
            fake.chmod(0o755)
            script = '''set -e
workspace=demo-runner
coder_origin=https://coder.example.test
coder_command=(coder --global-config '/tmp/config with spaces%literal')
fail() { printf '%s\\n' "$*" >&2; }
''' + FUNCTIONS + '''
prepare_ssh_transport
transport_before=$ssh_transport_dir
[[ $(stat -f '%Lp' "$transport_before" 2>/dev/null || stat -c '%a' "$transport_before") == 700 ]]
printf 'manifest\\tinput\\n' | coder_ssh printf '%s' 'space quote " and $literal'
coder_ssh_bounded 3 /usr/bin/true </dev/null
cleanup_ssh_transport
[[ ! -e $transport_before && -z $ssh_transport_dir ]]
'''
            environment = {**os.environ, 'PATH': str(root)+os.pathsep+os.environ['PATH'],
                           'SSH_TEST_LOG': str(log), 'REMOTE_DEV_LOCAL_AGENT_TRANSPORT': 'multiplex'}
            result = subprocess.run(['bash', '-c', script], input='', text=True,
                                    capture_output=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'manifest\tinput\n')
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 3)
            sockets = [call[call.index('-S')+1] for call in calls]
            self.assertEqual(len(set(sockets)), 1)
            for call in calls[:2]:
                self.assertEqual(call[:3], ['-F', '/dev/null', '-T'])
                self.assertIn('ControlMaster=auto', call)
                self.assertIn('ControlPersist=60', call)
                proxy = next(value for value in call if value.startswith('ProxyCommand='))
                proxy_argv = shlex.split(proxy.removeprefix('ProxyCommand=').replace('%%', '%'))
                self.assertIn('/tmp/config with spaces%literal', proxy_argv)
                self.assertEqual(proxy_argv[-5:], ['ssh', '--stdio', '--disable-autostart', '--wait=no', 'demo-runner'])
            self.assertEqual(shlex.split(calls[0][-1]), ['printf', '%s', 'space quote " and $literal'])
            self.assertIn('-O', calls[2])

    def test_unknown_transport_fails_closed(self):
        result = subprocess.run(['bash', '-c', 'fail() { echo "$*" >&2; };\n'+FUNCTIONS+'\nprepare_ssh_transport'],
                                env={**os.environ, 'REMOTE_DEV_LOCAL_AGENT_TRANSPORT': 'other'},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('TRANSPORT_INVALID', result.stderr)

    def test_watcher_yields_to_verified_waiter_before_lock(self):
        function = SOURCE[SOURCE.index('acquire_lock() {'):SOURCE.index('\noperation_lock=\n')]
        with tempfile.TemporaryDirectory() as directory:
            for waiter_status, expected in [(0, 82), (77, 77)]:
                script = 'binding_dir=$1\nforeground_waiter_exists() { return '+str(waiter_status)+'; }\n'
                result = subprocess.run(['bash', '-c', script+function+'\nacquire_lock watcher', 'test', directory])
                self.assertEqual(result.returncode, expected)
                self.assertFalse((Path(directory)/'operation.lock').exists())

    def test_stopped_classification_requires_fresh_exact_workspace(self):
        function = SOURCE[SOURCE.index('coder_workspace_is_stopped() {'):SOURCE.index('\nrequire_runner_role()')]
        with tempfile.TemporaryDirectory() as directory:
            for rows, expected in [
                ([{'name': 'demo-runner', 'latest_build': {'status': 'stopped'}}], 0),
                ([{'name': 'demo-runner', 'latest_build': {'status': 'running'}}], 1),
                ([{'name': 'foreign', 'latest_build': {'status': 'stopped'}}], 1),
                ([{'name': 'demo-runner', 'latest_build': {'status': 'stopped'}}]*2, 1),
            ]:
                script = '''binding_dir=$1
workspace=demo-runner
PREFLIGHT_TIMEOUT_SECONDS=3
bounded_command() { printf '%s' "$WORKSPACE_TEST_ROWS"; }
''' + function + '\ncoder_workspace_is_stopped'
                result = subprocess.run(['bash', '-c', script, 'test', directory],
                                        env={**os.environ, 'WORKSPACE_TEST_ROWS': json.dumps(rows)})
                self.assertEqual(result.returncode, expected)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
