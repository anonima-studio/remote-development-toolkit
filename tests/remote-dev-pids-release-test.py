#!/usr/bin/env python3
"""Exercise the template's actual exec completion gate with Docker API fixtures."""
import pathlib, subprocess, tempfile, sys, json
source=pathlib.Path(sys.argv[1]).read_text()
start=source.index('      exec_response=$(request')
code=source[start:source.index('    EOT',start)]
cases=[
 ('already-complete',[{'Running':False,'ExitCode':0}],0,''),
 ('delayed-success',[{'Running':True,'ExitCode':None},{'Running':True,'ExitCode':0},{'Running':False,'ExitCode':0}],0,''),
 ('command-failure',[{'Running':False,'ExitCode':1}],1,'PID_LIMIT_RELEASE_FAILED'),
 ('timeout',[{'Running':True,'ExitCode':0}],1,'PID_LIMIT_RELEASE_TIMEOUT'),
 ('missing-exit',[{'Running':False}],1,'PID_LIMIT_RELEASE_FAILED'),
 ('invalid-id',[{'Running':False,'ExitCode':0}],1,'PID_LIMIT_EXEC_ID_INVALID'),
 ('api-failure',[{'Running':False,'ExitCode':0}],22,''),
]
for name,states,status,error in cases:
 with tempfile.TemporaryDirectory() as d:
  p=pathlib.Path(d)
  for i,state in enumerate(states): (p/str(i)).write_text(json.dumps(state))
  script='''set -eu
api=http://docker
container_id=abc
exec_payload=unused
count_file="$FIXTURES/count"
printf 0 > "$count_file"
sleep() { :; }
request() {
 case "$*" in
  *containers/abc/exec*) printf '%s' "$EXEC_RESPONSE" ;;
  *exec/abcd/start*) : ;;
  *exec/abcd/json*)
   [ "$API_FAILURE" = no ] || return 22
   n=$(cat "$count_file")
   cat "$FIXTURES/$n"
   if [ "$n" -lt "$LAST" ]; then printf '%s' "$((n+1))" > "$count_file"; fi ;;
  *) echo UNEXPECTED_REQUEST >&2; return 90 ;;
 esac
}
'''+code
  import os
  env=dict(os.environ,FIXTURES=d,LAST=str(len(states)-1),EXEC_RESPONSE=json.dumps({'Id':'wrong' if name=='invalid-id' else 'abcd'}),API_FAILURE='yes' if name=='api-failure' else 'no')
  r=subprocess.run(['/bin/sh'],input=script,text=True,capture_output=True,env=env,timeout=10)
  assert r.returncode==status and error in r.stderr,(name,r.returncode,r.stderr)
  if name=='delayed-success': assert (p/'count').read_text()=='2'
  print('PASS',name)
