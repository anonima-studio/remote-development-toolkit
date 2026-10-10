#!/usr/bin/env python3
"""Measure real stdio MCP calls from an explicit trusted project context."""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--context', required=True)
parser.add_argument('--server', default=str(Path(__file__).resolve().parents[1] / 'bin/remote-dev-local-agent-mcp.mjs'))
parser.add_argument('--tool', default='runner_exec')
parser.add_argument('--repeat', type=int, default=1)
parser.add_argument('--timeout-seconds', type=int)
parser.add_argument('--handle')
parser.add_argument('argv', nargs='*', default=['/usr/bin/printf', 'latency-probe-ok\\n'])
args = parser.parse_args()
process = subprocess.Popen(['node', args.server], cwd=args.context, env=os.environ,
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
request_id = 0
failed = False

def request(method, params):
    global request_id
    request_id += 1
    started = time.monotonic()
    process.stdin.write(json.dumps(dict(jsonrpc='2.0', id=request_id, method=method, params=params))+'\n')
    process.stdin.flush()
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError('MCP server closed without response')
        response = json.loads(line)
        if response.get('id') == request_id:
            return time.monotonic()-started, response

try:
    duration, result = request('initialize', dict(protocolVersion='2025-06-18', capabilities={}, clientInfo=dict(name='latency-probe', version='1')))
    if 'error' in result:
        raise RuntimeError(result['error'])
    print(json.dumps(dict(phase='initialize', seconds=duration)), flush=True)
    process.stdin.write(json.dumps(dict(jsonrpc='2.0', method='notifications/initialized'))+'\n')
    process.stdin.flush()
    for index in range(args.repeat):
        arguments = dict(argv=args.argv) if args.tool in ('runner_exec', 'runner_start') else {}
        if args.tool in ('runner_logs', 'runner_stop', 'runner_restart'):
            if not args.handle:
                raise ValueError('--handle is required for process lifecycle calls')
            arguments = dict(handle=args.handle)
        if args.tool == 'runner_exec' and args.timeout_seconds is not None:
            arguments['timeout_seconds'] = args.timeout_seconds
        duration, result = request('tools/call', dict(name=args.tool, arguments=arguments))
        print(json.dumps(dict(phase=args.tool, repeat=index+1, seconds=duration, response=result)), flush=True)
        failed |= 'error' in result or bool(result.get('result', {}).get('isError'))
finally:
    process.stdin.close()
    process.wait(timeout=10)
raise SystemExit(1 if failed else 0)
