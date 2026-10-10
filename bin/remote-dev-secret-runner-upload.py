#!/usr/bin/env python3
"""Stream one secret through the existing Local Agent execution owner."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

LIMIT = 1024 * 1024
KEYS = ('REMOTE_DEV_SECRET_ROOT', 'REMOTE_DEV_SECRET_REL', 'REMOTE_DEV_SECRET_REPLACE',
        'REMOTE_DEV_MODE_OPERATION_ID', 'REMOTE_DEV_MODE_TOKEN', 'REMOTE_DEV_MODE_GUARD_SOURCE')

# argv is visible in process listings on both hosts. Keep both payload and
# capability token in stdin, including the metadata needed by the uploader.
BOOTSTRAP = '''import json,os,sys
stream=sys.stdin.buffer
line=stream.readline(1048577)
if len(line)>1048576 or not line.endswith(b"\\n"):
 raise RuntimeError("SECRET_ENVELOPE_INVALID")
envelope=json.loads(line)
keys={"REMOTE_DEV_SECRET_ROOT","REMOTE_DEV_SECRET_REL","REMOTE_DEV_SECRET_REPLACE","REMOTE_DEV_MODE_OPERATION_ID","REMOTE_DEV_MODE_TOKEN","REMOTE_DEV_MODE_GUARD_SOURCE"}
if set(envelope)!={"version","environment","uploader"} or envelope["version"]!=1 or set(envelope["environment"])!=keys:
 raise RuntimeError("SECRET_ENVELOPE_INVALID")
if any(not isinstance(value,str) or "\\0" in value for value in envelope["environment"].values()) or not isinstance(envelope["uploader"],str):
 raise RuntimeError("SECRET_ENVELOPE_INVALID")
os.environ.update(envelope["environment"])
exec(compile(envelope["uploader"],"remote-dev-secret-upload.py","exec"),{"__name__":"__main__"})
'''


def transfer(owner, profile, origin, context, source):
    envelope = {'version': 1, 'environment': {key: os.environ.get(key, '') for key in KEYS},
                'uploader': os.environ['REMOTE_DEV_SECRET_UPLOAD_SOURCE']}
    header = json.dumps(envelope, separators=(',', ':')).encode() + b'\n'
    if len(header) > LIMIT:
        raise RuntimeError('SECRET_ENVELOPE_TOO_LARGE')
    command = [str(owner), '--profile', profile, '--coder-url', origin, 'exec', '--for-context', context,
               '--timeout-seconds', '300', '--', 'python3', '-c', BOOTSTRAP]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        process.stdin.write(header)
        shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
        process.stdin.close()
    except BrokenPipeError:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        code = process.wait()
        return code or 1
    except BaseException:
        # EOF lets the execution owner settle its receipt and post-command
        # audit. Never kill that owner and leave an unobserved remote writer.
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.wait()
        raise
    return process.wait()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 5:
            raise RuntimeError('Use owner profile origin context; secret data is stdin only')
        sys.exit(transfer(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.stdin.buffer))
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'SECRET_RUNNER_TRANSFER_FAILED: preserve the execution receipt', file=sys.stderr)
        sys.exit(1)
