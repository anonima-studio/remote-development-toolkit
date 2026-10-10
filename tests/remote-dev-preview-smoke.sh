#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
fixture=$(mktemp -d)
fake_bin=$fixture/bin
command_log=$fixture/commands.log
remote_service_pid=
cleanup() {
  if [[ -n ${remote_service_pid:-} ]] && kill -0 "$remote_service_pid" 2>/dev/null; then
    kill "$remote_service_pid" 2>/dev/null || true
    wait "$remote_service_pid" 2>/dev/null || true
  fi
  rm -rf -- "$fixture"
}
trap cleanup EXIT
mkdir -p "$fake_bin"

help_output=$("$repo_root/bin/remote-dev" --help)
if [[ $help_output != *'preview      Make one workspace HTTP port reachable from this Mac'* ]]; then
  printf 'remote-dev help omitted the workspace preview command:\n%s\n' \
    "$help_output" >&2
  exit 1
fi
if [[ $help_output != *'debug-forward Make one runner TCP/debug port reachable from this Mac'* ]]; then
  printf 'remote-dev help omitted the runner debug-forward command:\n%s\n' \
    "$help_output" >&2
  exit 1
fi

cat > "$fake_bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ssh %s\n' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
if [[ " $* " == *' remote-dev-debug-forward '* ]]; then
  if [[ ${REMOTE_DEV_TEST_REMOTE_TCP_STATUS:-0} != 0 ]]; then
    exit "$REMOTE_DEV_TEST_REMOTE_TCP_STATUS"
  fi
  remote_command=
  for remote_command in "$@"; do :; done
  exec /bin/bash -c "$remote_command"
fi
trap 'exit 0' INT TERM
sleep "${REMOTE_DEV_TEST_SSH_SECONDS:-0.75}"
EOF

cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
printf '%s' "${REMOTE_DEV_TEST_HTTP_STATUS:-200}"
exit "${REMOTE_DEV_TEST_CURL_STATUS:-0}"
EOF
cat > "$fake_bin/nc" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'nc %s\n' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
exit "${REMOTE_DEV_TEST_NC_STATUS:-0}"
EOF
chmod +x "$fake_bin/ssh" "$fake_bin/curl" "$fake_bin/nc"

preview_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview example-app 3000 --local-port 3300)

for expected in \
  'STARTING Secure preview tunnel to example-profile.example-app' \
  'READY    Remote workspace preview is reachable from this Mac.' \
  'Open     http://127.0.0.1:3300' \
  'HTTP     200' \
  'Stop     Press Ctrl+C in this Terminal.' \
  'Recovery If the tunnel drops'; do
  if [[ $preview_output != *"$expected"* ]]; then
    printf 'preview output omitted %q:\n%s\n' "$expected" "$preview_output" >&2
    exit 1
  fi
done

grep -Fq -- \
  '-L 127.0.0.1:3300:127.0.0.1:3000 example-profile.example-app' \
  "$command_log"
grep -Fq -- 'http://127.0.0.1:3300' "$command_log"

remote_port_file=$fixture/remote-port
python3 - "$remote_port_file" <<'PY' &
import socket
import sys
from pathlib import Path

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen()
Path(sys.argv[1]).write_text(str(listener.getsockname()[1]), encoding="utf-8")
while True:
    connection, _ = listener.accept()
    connection.close()
PY
remote_service_pid=$!
for _ in $(seq 1 50); do
  [[ ! -s $remote_port_file ]] || break
  sleep 0.05
done
[[ -s $remote_port_file ]] || { echo 'remote TCP fixture did not start' >&2; exit 1; }
remote_debug_port=$(<"$remote_port_file")

debug_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  debug-forward example-app-runner "$remote_debug_port" --local-port 19229)
for expected in \
  'STARTING Secure debug tunnel to example-profile.example-app-runner' \
  'READY    Verified runner debug service is reachable from this Mac.' \
  'Connect  127.0.0.1:19229' \
  'Stop     Press Ctrl+C in this Terminal.'; do
  if [[ $debug_output != *"$expected"* ]]; then
    printf 'debug-forward output omitted %q:\n%s\n' "$expected" "$debug_output" >&2
    exit 1
  fi
done
grep -Fq -- \
  'example-profile.example-app-runner /usr/local/bin/remote-dev-workspace-role require runner' \
  "$command_log"
grep -Fq -- "remote-dev-debug-forward $remote_debug_port" "$command_log"
grep -Fq -- \
  "-L 127.0.0.1:19229:127.0.0.1:$remote_debug_port example-profile.example-app-runner" \
  "$command_log"
grep -Fq -- '-z -w 1 127.0.0.1 19229' "$command_log"

assert_refused() {
  local expected=$1
  shift
  local output status
  set +e
  output=$("$@" 2>&1)
  status=$?
  set -e
  if (( status != 2 )) || [[ $output != *"$expected"* ]]; then
    printf 'expected refusal containing %q, got status %s:\n%s\n' \
      "$expected" "$status" "$output" >&2
    exit 1
  fi
}

assert_refused 'invalid Coder workspace name' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview '../example-app' 3000
assert_refused 'remote port must be a TCP port from 1 to 65535' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview example-app 70000
assert_refused 'local port must be a TCP port from 1 to 65535' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview example-app 3000 --local-port zero
assert_refused 'invalid Coder workspace name' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  debug-forward '../example-app' 9229
assert_refused 'remote port must be a TCP port from 1 to 65535' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  debug-forward example-app-runner 0

set +e
remote_debug_failure=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_REMOTE_TCP_STATUS=1 \
  "$repo_root/bin/remote-dev" --profile example-profile \
  debug-forward example-app-runner "$remote_debug_port" --local-port 19229 2>&1)
remote_debug_status=$?
set -e
if (( remote_debug_status != 1 )) || \
   [[ $remote_debug_failure != *'runner debug service is not reachable'* ]] || \
   [[ $remote_debug_failure != *'Start the debugger on runner loopback'* ]]; then
  printf 'closed remote debug port did not fail before forwarding (status %s):\n%s\n' \
    "$remote_debug_status" "$remote_debug_failure" >&2
  exit 1
fi

set +e
unreachable_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_HTTP_STATUS=000 \
  REMOTE_DEV_TEST_SSH_SECONDS=20 \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview example-app 3000 --local-port 3300 2>&1)
unreachable_status=$?
set -e
if (( unreachable_status != 1 )) || \
   [[ $unreachable_output != *'remote app did not return an HTTP response'* ]] || \
   [[ $unreachable_output != *'Keep the app running and rerun this preview command'* ]]; then
  printf 'unreachable preview did not produce actionable recovery (status %s):\n%s\n' \
    "$unreachable_status" "$unreachable_output" >&2
  exit 1
fi

set +e
tunnel_failure_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_HTTP_STATUS=000 \
  REMOTE_DEV_TEST_CURL_STATUS=7 \
  REMOTE_DEV_TEST_SSH_SECONDS=12 \
  "$repo_root/bin/remote-dev" --profile example-profile \
  preview example-app 3000 --local-port 3300 2>&1)
tunnel_failure_status=$?
set -e
if (( tunnel_failure_status != 1 )) || \
   [[ $tunnel_failure_output != *'Preview tunnel did not'* ]] || \
   [[ $tunnel_failure_output == *'remote app did not return an HTTP response'* ]]; then
  printf 'late SSH failure was misreported as an app failure (status %s):\n%s\n' \
    "$tunnel_failure_status" "$tunnel_failure_output" >&2
  exit 1
fi

printf 'PASS     Workspace preview reaches a verified Mac loopback URL\n'
