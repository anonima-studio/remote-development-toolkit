#!/usr/bin/env python3
"""Mac entry point for a complete, workspace-scoped Codex image release."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('workspace')
    parser.add_argument('--version', required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', args.workspace) or not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('require a workspace name and exact stable version')
    profile = subprocess.run([str(ROOT / 'bin/remote-dev'), '--profile', args.profile, 'profile'],
                             check=True, capture_output=True, text=True)
    values = dict(line.split('=', 1) for line in profile.stdout.splitlines() if '=' in line)
    if args.restart:
        subprocess.run([str(ROOT / 'bin/remote-dev'), '--profile', args.profile, 'doctor'], check=True)
    command = ['python3', '-B', '-', '--workspace', args.workspace, '--version', args.version,
               '--port', values['coder_host_port'], '--coder-url', values['coder_access_url']]
    if args.restart:
        command.append('--restart')
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                    '-o', 'ClearAllForwardings=yes', values['ssh_host'], shlex.join(command)],
                   input=(ROOT / 'deploy/codex-update.py').read_text(), text=True, check=True)
    if args.restart:
        # A fresh non-interactive SSH process verifies the path used by Desktop.
        config = Path(os.environ.get('REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT',
                      str(Path.home() / 'Library/Application Support/remote-dev/coder'))) / args.profile
        probe = ('/usr/local/bin/remote-dev-workspace-role require development >/dev/null && '
                 '/usr/local/bin/codex --version')
        result = subprocess.run(['coder', '--no-version-warning', '--global-config', str(config),
                                 '--url', values['coder_access_url'], 'ssh', '--disable-autostart=true',
                                 '--wait=yes', args.workspace, '--', probe],
                                check=True, capture_output=True, text=True, timeout=120)
        if result.stdout.strip() != 'codex-cli ' + args.version:
            raise ValueError('Codex version mismatch after release')
        print('READY Codex ' + args.version + ' verified over SSH. Reconnect ' +
              args.profile + '.' + args.workspace + ' in Codex Desktop and reopen the model picker.')
    else:
        print('CHECKED No changes made. Use --restart to build, publish and apply this release.')


if __name__ == '__main__':
    try:
        main()
    except (subprocess.SubprocessError, ValueError) as error:
        print('CODEX_UPDATE_UNVERIFIED: ' + type(error).__name__ +
              '. Inspect the host release receipt and build logs before retrying.', file=sys.stderr)
        sys.exit(1)
