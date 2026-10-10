#!/usr/bin/env python3
"""Build, publish and apply a Codex-only release on the selected Coder host."""
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

RECORD_ROOT = Path('/opt/remote-dev/coder/codex-releases')


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def template_source(archive, image):
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        files = [m for m in bundle.getmembers() if m.name in ('main.tf', './main.tf')]
        if len(files) != 1 or not files[0].isfile() or files[0].size > 2_000_000:
            raise ValueError('Unsupported template archive')
        source = bundle.extractfile(files[0]).read().decode()
        # Extra Terraform configuration would be lost by publishing main.tf alone.
        if any(m.isfile() and m.name.endswith(('.tf', '.tf.json')) and m not in files
               and not (m.name in ('._main.tf', './._main.tf') and m.size <= 65536
                        and bundle.extractfile(m).read(8) == b'\x00\x05\x16\x07\x00\x02\x00\x00')
               for m in bundle.getmembers()):
            raise ValueError('Multi-file Terraform template requires a release adapter')
    pattern = r'(resource "docker_container" "workspace"\s*\{\s*count\s*=\s*[^\n]+\n\s*image\s*=\s*)"[^"\n]+"'
    source, count = re.subn(pattern, lambda m: m[1] + json.dumps(image), source)
    if count != 1:
        raise ValueError('Expected one literal workspace image in the template')
    # This platform template is self-contained. Do not silently drop file dependencies.
    if re.search(r'\b(?:file|filebase64|templatefile|fileset)\s*\(', source):
        raise ValueError('Template file dependencies require a release adapter')
    return source


class Release:
    def __init__(self, args):
        self.args = args
        self.origin = 'http://127.0.0.1:' + str(args.port)
        self.token = Path('/opt/remote-dev/coder/operator.session').read_text().strip()

    def api(self, path, data=None, raw=False):
        req = urllib.request.Request(self.origin + '/api/v2/' + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Coder-Session-Token': self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read() if raw else json.load(response)

    def wait(self, build):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            row = self.api('workspacebuilds/' + build['id'])
            if row['job']['status'] == 'succeeded':
                return row
            if row['job']['status'] in ('failed', 'canceled'):
                raise ValueError('Build failed: ' + build['id'])
            time.sleep(2)
        raise ValueError('Build still pending; inspect ' + build['id'] + ' before retrying')

    def execute(self):
        with contextlib.ExitStack() as resources:
            self.resources = resources
            return self._execute()

    def _execute(self):
        args = self.args
        workspace = self.api('users/me/workspace/' + args.workspace)
        old = workspace['latest_build']
        parameters = self.api('workspacebuilds/' + old['id'] + '/parameters')
        values = {p['name']: p['value'] for p in parameters}
        if old['status'] != 'running' or values.get('workspace_role') != 'development':
            raise ValueError('Require a running development workspace; runners are refused')
        if workspace.get('automatic_updates') != 'never':
            raise ValueError('Disable automatic template updates for this workspace before a scoped release')
        containers = run(['docker', 'ps', '-q', '--filter',
                          'label=coder.workspace_id=' + workspace['id']], capture_output=True).stdout.split()
        if len(containers) != 1:
            raise ValueError('Expected exactly one running workspace container')
        container = json.loads(run(['docker', 'inspect', containers[0]], capture_output=True).stdout)[0]
        base = container['Image']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', base):
            raise ValueError('Invalid base image identity')
        base_tag = container['Config']['Image']
        if not re.fullmatch(r'remote-dev-workspace:[A-Za-z0-9_.-]+', base_tag):
            raise ValueError('Require a retained Remote Dev base image tag')
        if run(['docker', 'image', 'inspect', '--format', '{{.Id}}', base_tag], capture_output=True).stdout.strip() != base:
            raise ValueError('Base image tag no longer identifies the running image')
        version = self.api('templateversions/' + old['template_version_id'])
        template = self.api('templates/' + workspace['template_id'])
        if template['name'] != 'remote-dev-docker' or version['job']['status'] != 'succeeded':
            raise ValueError('Unsupported template')
        archive = self.api('files/' + version['job']['file_id'], raw=True)
        dockerfile = (f'FROM {base_tag}\n# Verified base image: {base}\nUSER root\n'
            f'RUN npm install --global @openai/codex@{args.version} && npm cache clean --force\n'
            f'RUN sed -i "s/^expected_codex_version=.*/expected_codex_version=\'codex-cli {args.version}\'/" '
            '/usr/local/bin/remote-dev-codex '
            f'&& grep -Fxq "expected_codex_version=\'codex-cli {args.version}\'" '
            '/usr/local/bin/remote-dev-codex '
            '&& bash -n /usr/local/bin/remote-dev-codex\n'
            f'RUN test "$(codex --version)" = "codex-cli {args.version}"\nUSER coder\n')
        digest = hashlib.sha256(dockerfile.encode()).hexdigest()
        tag = 'remote-dev-workspace:codex-' + args.version + '-' + digest[:12]
        source = template_source(archive, tag)
        release_name = 'codex-' + args.version + '-' + hashlib.sha256(source.encode()).hexdigest()[:12]
        before_version = run(['docker', 'exec', containers[0], '/usr/local/bin/codex', '--version'],
                             capture_output=True).stdout.strip()
        before_wrapper = run(['docker', 'exec', containers[0], '/bin/grep', '-E',
                              '^expected_codex_version=', '/usr/local/bin/remote-dev-codex'],
                             capture_output=True).stdout.strip()
        if not re.fullmatch(r"expected_codex_version='codex-cli \d+\.\d+\.\d+'", before_wrapper):
            raise ValueError('Unexpected Codex launcher version contract')
        target_wrapper = f"expected_codex_version='codex-cli {args.version}'"
        print(json.dumps({'workspace': args.workspace, 'installed': before_version,
              'target': args.version, 'base_image': base, 'image': tag,
              'previous_template_version': old['template_version_id'],
              'release': release_name, 'apply': args.restart}), flush=True)
        if not args.restart:
            return
        if before_version == 'codex-cli ' + args.version and before_wrapper == target_wrapper:
            print('UNCHANGED Requested Codex version is already installed.', flush=True)
            return
        record_dir = RECORD_ROOT
        record_dir.mkdir(mode=0o700, exist_ok=True)
        lock = self.resources.enter_context((record_dir / (workspace['id'] + '.lock')).open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record_path = record_dir / (workspace['id'] + '-' + release_name + '.json')
        if record_path.exists():
            raise ValueError('Release receipt exists; inspect it before another mutation: ' + str(record_path))
        receipt = {'workspace_id': workspace['id'], 'workspace': args.workspace,
                   'old_build': old['id'], 'old_version': old['template_version_id'],
                   'base_image': base, 'image': tag, 'codex': args.version}
        def save(stage, **extra):
            receipt.update(stage=stage, **extra)
            temporary = record_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(receipt, indent=2) + '\n')
            temporary.chmod(0o600)
            temporary.replace(record_path)
            print('STAGE ' + stage, flush=True)
        save('building')
        with tempfile.TemporaryDirectory(prefix='remote-dev-codex-') as directory:
            root = Path(directory)
            (root / 'Dockerfile').write_text(dockerfile)
            run(['docker', 'build', '--label', 'id0.remote-dev.workspace-source-sha256=' + digest,
                 '--tag', tag, directory])
            image_id = run(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag], capture_output=True).stdout.strip()
            for runtime in ('runc', 'sysbox-runc'):
                actual = run(['docker', 'run', '--rm', '--network', 'none', '--runtime', runtime,
                              '--entrypoint', '/usr/local/bin/codex', image_id, '--version'],
                             capture_output=True).stdout.strip()
                if actual != 'codex-cli ' + args.version:
                    raise ValueError('Image smoke failed: ' + runtime)
            retention = 'remote-dev-image-retention-' + image_id.removeprefix('sha256:')
            existing = subprocess.run(['docker', 'container', 'inspect', retention], capture_output=True)
            if existing.returncode:
                run(['docker', 'create', '--name', retention, '--network', 'none', '--entrypoint', '/bin/true',
                     '--label', 'id0.remote-dev.purpose=image-retention',
                     '--label', 'id0.remote-dev.owner=remote-dev', image_id], capture_output=True)
            save('image_verified', image_id=image_id)
            # Use a unique publication directory; never overwrite the installed template.
            (root / 'main.tf').write_text(source)
            remote_dir = '/tmp/' + release_name
            run(['docker', 'exec', 'remote-dev-coder-coder-1', 'mkdir', '-p', remote_dir])
            run(['docker', 'cp', str(root / 'main.tf'), 'remote-dev-coder-coder-1:' + remote_dir + '/main.tf'])
            save('publishing')
            run(['docker', 'exec', '-e', 'CODER_URL=http://127.0.0.1:7080',
                 '-e', 'CODER_SESSION_TOKEN=' + self.token, 'remote-dev-coder-coder-1',
                 '/opt/coder', 'templates', 'push', template['name'], '--name', release_name,
                 '--directory', remote_dir, '--activate=false', '--ignore-lockfile', '--yes',
                 '--variable', 'remote_dev_coder_access_url=' + args.coder_url])
        versions = self.api('templates/' + template['id'] + '/versions')
        matches = [v for v in versions if v['name'] == release_name]
        if len(matches) != 1 or matches[0]['job']['status'] != 'succeeded':
            raise ValueError('Published version could not be verified')
        target = matches[0]['id']
        save('published', new_version=target)
        current = self.api('workspaces/' + workspace['id'])
        if current['latest_build']['id'] != old['id']:
            raise ValueError('Workspace changed during build; no restart attempted')
        if self.api('templates/' + template['id'])['active_version_id'] != template['active_version_id']:
            raise ValueError('Active template changed during release; no restart attempted')
        save('stopping')
        stopped = self.api('workspaces/' + workspace['id'] + '/builds', {'transition': 'stop'})
        save('stop_requested', stop_build=stopped['id'])
        self.wait(stopped)
        if self.api('workspaces/' + workspace['id'])['latest_build']['id'] != stopped['id']:
            raise ValueError('Concurrent workspace build detected; no start attempted')
        save('starting')
        started = self.api('workspaces/' + workspace['id'] + '/builds',
                          {'transition': 'start', 'template_version_id': target,
                           'rich_parameter_values': parameters})
        save('start_requested', start_build=started['id'])
        self.wait(started)
        current = self.api('workspaces/' + workspace['id'])
        if current['latest_build']['id'] != started['id'] or current['latest_build']['template_version_id'] != target:
            raise ValueError('Workspace build identity changed')
        ids = run(['docker', 'ps', '-q', '--filter', 'label=coder.workspace_id=' + workspace['id']],
                  capture_output=True).stdout.split()
        if len(ids) != 1:
            raise ValueError('Updated workspace container missing')
        installed_image = run(['docker', 'inspect', '--format', '{{.Image}}', ids[0]],
                              capture_output=True).stdout.strip()
        if installed_image != image_id:
            raise ValueError('Running workspace image differs from verified release')
        run(['docker', 'exec', ids[0], '/usr/local/bin/remote-dev-workspace-role', 'require', 'development'],
            capture_output=True)
        actual = run(['docker', 'exec', ids[0], '/usr/local/bin/codex', '--version'],
                     capture_output=True).stdout.strip()
        if actual != 'codex-cli ' + args.version:
            raise ValueError('Running Codex differs from requested version')
        launcher = run(['docker', 'exec', ids[0], '/usr/local/bin/remote-dev-codex', '--check'],
                       capture_output=True).stdout
        if 'codex_version=codex-cli ' + args.version + '\n' not in launcher:
            raise ValueError('Codex launcher differs from requested version')
        save('workspace_verified')
        print('RECEIPT ' + str(record_path), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--coder-url', required=True)
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.workspace) or not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('require a workspace name and an exact stable Codex version')
    try:
        Release(args).execute()
    except Exception as error:
        # HTTP/CLI exceptions may contain credentials; report type and recovery only.
        reason = str(error) if type(error) is ValueError else type(error).__name__
        print('RELEASE_FAILED ' + reason + ': inspect the release receipt and Coder build logs; do not retry blindly.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
