#!/usr/bin/env bash
# Real Local Agent owner/watcher; disposable SSH transport and synthetic payload.
set -euo pipefail
if [[ ${REMOTE_DEV_TEST_SLICE:-} != secret-upload || -z ${tool:-} ]]; then
  exec env REMOTE_DEV_TEST_SLICE=secret-upload bash "$(dirname "$0")/remote-dev-local-agent-smoke.sh"
fi

: > "$fixture/mutagen-ghost-create.once"
common_env+=(REMOTE_DEV_LOCAL_AGENT_REMOTE_AUDIT_INTERVAL_SECONDS=1)
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src --remote-writable .env >/dev/null
binding_file=$(find "$state_root" -name binding.tsv -print -quit)
binding_dir=$(dirname "$binding_file")
watcher_pid=$(<"$binding_dir/watcher.pid")
slow_release_file=$remote_home/upload-release
upload_ready=$remote_home/upload-ready

# Pause the real atomic uploader precisely after creating its empty staging
# file. Production code has no test hook or relaxed path exclusions.
uploader_source=$(python3 - "$repo_root/bin/remote-dev-secret-upload.py" "$upload_ready" "$slow_release_file" <<'PY'
from pathlib import Path
import sys
s=Path(sys.argv[1]).read_text()
gate=('temporary = name\n            import time\n'
      f'            Path({sys.argv[2]!r}).write_text("ready")\n'
      f'            while not Path({sys.argv[3]!r}).exists(): time.sleep(0.05)')
assert s.count('temporary = name')==1
print(s.replace('temporary = name',gate))
PY
)
printf 'synthetic uploaded secret\n' > "$fixture/payload"
env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_SECRET_ROOT="$remote_home/projects/demo" REMOTE_DEV_SECRET_REL=.env \
  REMOTE_DEV_SECRET_REPLACE=false REMOTE_DEV_MODE_OPERATION_ID= REMOTE_DEV_MODE_TOKEN= \
  REMOTE_DEV_MODE_GUARD_SOURCE="$(<"$repo_root/templates/docker/mode-guard.py")" \
  REMOTE_DEV_SECRET_UPLOAD_SOURCE="$uploader_source" \
  python3 "$repo_root/bin/remote-dev-secret-runner-upload.py" "$tool" \
  example-profile https://coder.example.test "$project" \
  < "$fixture/payload" > "$fixture/upload.stdout" 2> "$fixture/upload.stderr" &
slow_exec_pid=$!
upload_deadline=$((SECONDS + 60))
while [[ ! -s $upload_ready ]] && (( SECONDS < upload_deadline )); do
  kill -0 "$slow_exec_pid" 2>/dev/null || break
  sleep 0.1
done
if [[ ! -s $upload_ready ]]; then
  cat "$fixture/upload.stderr" >&2
  if ! kill -0 "$slow_exec_pid" 2>/dev/null; then
    upload_status=0
    wait "$slow_exec_pid" || upload_status=$?
    slow_exec_pid=
    printf 'uploader exited before staging gate: %s\n' "$upload_status" >&2
  else
    echo 'uploader staging gate timed out while owner was still running' >&2
  fi
  exit 1
fi
[[ -d $binding_dir/operation.lock ]]
kill -0 "$watcher_pid"
before_audits=$(grep -c -- --compressed-match "$command_log" || true)
sleep 4
after_audits=$(grep -c -- --compressed-match "$command_log" || true)
[[ $before_audits == "$after_audits" && ! -e $binding_dir/REMOTE_DRIFT ]]
kill -0 "$watcher_pid"
: > "$slow_release_file"
wait "$slow_exec_pid"
slow_exec_pid=
cmp "$fixture/payload" "$remote_home/projects/demo/.env"
[[ $(find "$remote_home/projects/demo" -name '.remote-dev-secret.*' | wc -l | tr -d ' ') == 0 ]]
[[ ! -e $binding_dir/REMOTE_DRIFT ]]
run_tool exec --for-context "$project" -- /usr/bin/true >/dev/null
[[ ! -e $binding_dir/REMOTE_DRIFT ]]
if grep -R -q 'synthetic uploaded secret' "$binding_dir/receipts"; then
  echo 'payload entered an execution receipt' >&2; exit 1
fi

# The same watcher must still reject an unrelated unmanaged file.
printf 'unexpected source\n' > "$remote_home/projects/demo/foreign.txt"
wait_for_file "$binding_dir/REMOTE_DRIFT"
grep -q 'foreign.txt' "$binding_dir/REMOTE_DRIFT"
printf 'PASS runner secret upload holds watcher lock and preserves strict drift detection\n'
