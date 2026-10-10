#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
tool=$repo_root/bin/remote-dev-local-agent
fixture=$(mktemp -d)

stop_test_pid() {
  local pid=${1:-} attempt
  [[ $pid =~ ^[0-9]+$ ]] || return 0
  (( pid > 1 && pid != $$ )) || return 0
  kill "$pid" 2>/dev/null || true
  for attempt in $(seq 1 50); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.02
  done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

wait_for_file() {
  local path=$1 attempt
  for attempt in $(seq 1 300); do
    [[ -s $path ]] && return 0
    sleep 0.05
  done
  return 1
}

tcp_port_open() {
  python3 - "$1" <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.2):
        pass
except OSError:
    raise SystemExit(1)
PY
}

cleanup() {
  local test_status=$?
  local pid_file record cleanup_pid cleanup_sid
  if (( test_status != 0 )) && [[ -f ${binding_dir:-}/watcher.log ]]; then
    printf 'Watcher diagnostics after test failure:\n' >&2
    tail -c 4000 "$binding_dir/watcher.log" >&2
  fi
  if [[ -n ${slow_release_file:-} ]]; then
    mkdir -p "$(dirname "$slow_release_file")" 2>/dev/null || true
    : > "$slow_release_file" 2>/dev/null || true
  fi
  if [[ -n ${heartbeat_release:-} ]]; then
    mkdir -p "$(dirname "$heartbeat_release")" 2>/dev/null || true
    : > "$heartbeat_release" 2>/dev/null || true
  fi
  if [[ -n ${activation_gate:-} ]]; then
    mkdir -p "$(dirname "$activation_gate")" 2>/dev/null || true
    : > "$activation_gate.release" 2>/dev/null || true
  fi
  if [[ -n ${watcher_lock_gate:-} ]]; then
    mkdir -p "$(dirname "$watcher_lock_gate")" 2>/dev/null || true
    : > "$watcher_lock_gate.release" 2>/dev/null || true
  fi
  if [[ -n ${intent_cancel_gate:-} ]]; then
    mkdir -p "$(dirname "$intent_cancel_gate")" 2>/dev/null || true
    : > "$intent_cancel_gate.release" 2>/dev/null || true
  fi
  stop_test_pid "${slow_exec_pid:-}"
  stop_test_pid "${heartbeat_exec_pid:-}"
  stop_test_pid "${activation_start_pid:-}"
  stop_test_pid "${activation_restart_pid:-}"
  stop_test_pid "${activation_down_pid:-}"
  stop_test_pid "${watcher_lock_test_pid:-}"
  stop_test_pid "${intent_cancel_start_pid:-}"
  if [[ -f $fixture/foreign.pid ]]; then
    stop_test_pid "$(<"$fixture/foreign.pid")"
  fi
  while IFS= read -r -d '' pid_file; do
    IFS= read -r cleanup_pid < "$pid_file" || true
    stop_test_pid "$cleanup_pid"
  done < <(find "$fixture" \( -name watcher.pid -o -path '*/exec-heartbeats/*.pid' \) -type f -print0 2>/dev/null)
  while IFS= read -r -d '' record; do
    cleanup_pid=$(awk -F '\t' '$1 == "pid" {print $2; exit}' "$record" 2>/dev/null || true)
    cleanup_sid=$(awk -F '\t' '$1 == "session_id" {print $2; exit}' "$record" 2>/dev/null || true)
    if [[ $cleanup_sid =~ ^[0-9]+$ && -x ${fake_bin:-}/pkill ]]; then
      "$fake_bin/pkill" -KILL -s "$cleanup_sid" 2>/dev/null || true
    fi
    stop_test_pid "$cleanup_pid"
  done < <(find "$fixture" \( -path '*/processes/active/*.tsv' -o \
    -path '*/processes/recovery/*.identity.tsv' -o -path '*/executions/*.running' \) -type f -print0 2>/dev/null)
  rm -rf -- "$fixture"
}
trap cleanup EXIT

fake_bin=$fixture/bin
fake_home=$fixture/home
remote_home=$fixture/remote-home
state_root=$fixture/state
mutagen_state=$fixture/mutagen
command_log=$fixture/commands.log
remote_syntax_marker=$fixture/remote-shell-syntax-error
project=$fixture/project
mkdir -p "$fake_bin" "$fake_home/.ssh" "$remote_home" "$mutagen_state" "$project"
remote_home=$(cd "$remote_home" && pwd -P)
project=$(cd "$project" && pwd -P)
ln -s "$repo_root/templates/docker/managed-unlink.py" "$fake_bin/remote-dev-managed-unlink"

cat > "$fake_bin/uname" <<'EOF'
#!/usr/bin/env bash
printf 'Darwin\n'
EOF

cat > "$fake_bin/ssh" <<'EOF'
#!/usr/bin/env bash
if [[ " $* " == *' -G '* ]]; then
  printf 'hostname synthetic\n'
  exit 0
fi
if [[ " $* " == *' -O exit '* ]]; then exit 0; fi
[[ " $* " == *' -F /dev/null '* && " $* " == *'ControlMaster=auto'* &&
   " $* " == *'--disable-autostart --wait=no'* ]] || {
  echo 'unexpected or unsafe multiplex SSH invocation' >&2; exit 1;
}
while (( $# > 2 )); do
  case "$1" in
    -F|-o|-S) shift 2 ;;
    -T) shift ;;
    *) echo 'unknown SSH option' >&2; exit 1 ;;
  esac
done
exec coder ssh --disable-autostart --wait=no "$1" -- "$2"
EOF

cat > "$fake_bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if (( $# == 0 )); then
  shasum -a 256
else
  shasum -a 256 "$@"
fi
EOF

cat > "$fake_bin/stat" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if (( $# == 3 )) && [[ $1 == -c && $2 == %a ]]; then
  /usr/bin/stat -f '%Lp' "$3"
elif (( $# == 3 )) && [[ $1 == -c && $2 == %U ]]; then
  printf 'coder\n'
else
  exec /usr/bin/stat "$@"
fi
EOF

cat > "$fake_bin/timeout" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
while [[ ${1:-} == --* ]]; do shift; done
[[ ${1:-} =~ ^[0-9]+s?$ ]] || exit 2
shift
exec "$@"
EOF

cat > "$fake_bin/realpath" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if (( $# == 2 )) && [[ $1 == -e && -e $2 ]]; then
  printf '%s\n' "$2"
else
  exit 1
fi
EOF

cat > "$fake_bin/mv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
destination=${!#}
if [[ ${REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_IDENTITY_PUBLISH:-false} == true && \
      $destination == */processes/recovery/proc-*.identity.tsv ]]; then
  echo 'PROCESS_TEST_IDENTITY_PUBLICATION_FAILURE' >&2
  exit 94
elif [[ ${REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_PUBLISH_PERSISTENT:-false} == true && \
      $destination == */processes/active/proc-*.tsv ]]; then
  echo 'PROCESS_TEST_PERSISTENT_PUBLICATION_FAILURE' >&2
  exit 97
elif [[ ${REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_PUBLISH:-false} == true && \
        $destination == */processes/active/proc-*.tsv ]]; then
  marker=${REMOTE_DEV_LOCAL_AGENT_TEST_PROCESS_PUBLISH_MARKER:-$REMOTE_DEV_TEST_REMOTE_HOME/.test-$(basename "$destination").publish-failed-once}
  if [[ ! -e $marker ]]; then
    : > "$marker"
    sleep 0.2
    echo 'PROCESS_TEST_PUBLICATION_FAILURE' >&2
    exit 97
  fi
fi
exec /bin/mv "$@"
EOF

cat > "$fake_bin/ps" <<'PY'
#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
pid = int(args[args.index("-p") + 1])
output = args[args.index("-o") + 1]
if output == "lstart=":
    print(f"synthetic-start-{pid}")
elif output == "command=":
    print(f"remote-dev-local-agent _watch synthetic-start-{pid}")
elif output == "sid=":
    print(os.getsid(pid))
elif output == "pgid=":
    print(os.getpgid(pid))
else:
    raise SystemExit(2)
PY

cat > "$fake_bin/setsid" <<'PY'
#!/usr/bin/env python3
import os
import sys

os.setsid()
pid_file = os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_SETSID_PID_FILE")
if pid_file:
    with open(pid_file, "w", encoding="ascii") as stream:
        stream.write(str(os.getpid()) + "\n")
os.execvp(sys.argv[1], sys.argv[1:])
PY

cat > "$fake_bin/pgrep" <<'PY'
#!/usr/bin/env python3
import os
import subprocess
import sys

args = sys.argv[1:]
if len(args) != 2 or args[0] != "-s":
    raise SystemExit(2)
sid = int(args[1])
found = []
# PID allocation can wrap or advance under unrelated host activity. Inspect
# actual process IDs instead of guessing a numeric window around the leader.
for value in subprocess.check_output(["/bin/ps", "-axo", "pid="], text=True).split():
    pid = int(value)
    if pid == os.getpid():
        continue
    try:
        if os.getsid(pid) == sid:
            found.append(pid)
    except (ProcessLookupError, PermissionError):
        pass
for pid in found:
    print(pid)
raise SystemExit(0 if found else 1)
PY

cat > "$fake_bin/pkill" <<'PY'
#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

args = sys.argv[1:]
if len(args) != 3 or args[1] != "-s":
    raise SystemExit(2)
if os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_SESSION_STOP") == "true":
    print("PROCESS_TEST_SESSION_STOP_FAILURE", file=sys.stderr)
    raise SystemExit(98)
signum = {"-TERM": signal.SIGTERM, "-KILL": signal.SIGKILL}[args[0]]
sid = int(args[2])
found = False
for value in subprocess.check_output(["/bin/ps", "-axo", "pid="], text=True).split():
    pid = int(value)
    if pid == os.getpid():
        continue
    try:
        if os.getsid(pid) == sid:
            os.kill(pid, signum)
            found = True
    except (ProcessLookupError, PermissionError):
        pass
raise SystemExit(0 if found else 1)
PY

cat > "$fake_bin/coder" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'coder' >> "$REMOTE_DEV_TEST_COMMAND_LOG"
printf ' %q' "$@" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
printf '\n' >> "$REMOTE_DEV_TEST_COMMAND_LOG"

if [[ " $* " == *' whoami '* ]]; then
  if [[ ${REMOTE_DEV_TEST_PREFLIGHT_FAILURE:-} == whoami-hang ]]; then
    sleep 20
    exit 1
  fi
  printf 'owner\n'
  exit 0
fi

if [[ " $* " == *' config-ssh '* ]]; then
  [[ " $* " == *' --disable-autostart '* && " $* " == *' --wait=no '* ]] || {
    echo 'generated Mutagen SSH transport must disable autostart and waiting' >&2
    exit 1
  }
  fragment=
  prefix=
  while (( $# > 0 )); do
    case "$1" in
      --ssh-config-file) fragment=$2; shift 2 ;;
      --ssh-host-prefix) prefix=$2; shift 2 ;;
      *) shift ;;
    esac
  done
  mkdir -p "$(dirname "$fragment")"
  {
    printf 'Host %sdemo-runner\n  ProxyCommand /synthetic/coder --global-config /synthetic/config ssh --stdio --disable-autostart --wait=no\n' "$prefix"
    printf 'Host %sanchored-runner\n  ProxyCommand /synthetic/coder --global-config /synthetic/config ssh --stdio --disable-autostart --wait=no\n' "$prefix"
    printf 'Host %smulti-runner\n  ProxyCommand /synthetic/coder --global-config /synthetic/config ssh --stdio --disable-autostart --wait=no\n' "$prefix"
    printf 'Host %smetadata-runner\n  ProxyCommand /synthetic/coder --global-config /synthetic/config ssh --stdio --disable-autostart --wait=no\n' "$prefix"
  } > "$fragment"
  exit 0
fi

# Match the subcommand positionally: remote scripts passed as arguments may
# contain the word "start" in their text.
subcommand=
index=1
while (( index <= $# )); do
  case "${!index}" in
    --global-config|--url) index=$((index + 2)) ;;
    --*) index=$((index + 1)) ;;
    *) subcommand=${!index}; break ;;
  esac
done
if [[ $subcommand == start ]]; then
  [[ -z ${REMOTE_DEV_TEST_WORKSPACE_STATE_FILE:-} ]] || rm -f "$REMOTE_DEV_TEST_WORKSPACE_STATE_FILE"
  printf 'workspace started\n'
  exit 0
fi

ssh_index=-1
for (( index = 1; index <= $#; index++ )); do
  if [[ ${!index} == ssh ]]; then ssh_index=$index; break; fi
done
(( ssh_index > 0 )) || { echo 'unexpected fake coder command' >&2; exit 1; }

if [[ -n ${REMOTE_DEV_TEST_WORKSPACE_STATE_FILE:-} && -e $REMOTE_DEV_TEST_WORKSPACE_STATE_FILE ]]; then
  printf 'workspace must be started\n' >&2
  exit 1
fi

separator=-1
for (( index = ssh_index + 1; index <= $#; index++ )); do
  if [[ ${!index} == -- ]]; then separator=$index; break; fi
done
(( separator > 0 )) || { echo 'fake coder ssh omitted --' >&2; exit 1; }

remote_env=(
  "PATH=$REMOTE_DEV_TEST_FAKE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"
  "HOME=$REMOTE_DEV_TEST_REMOTE_HOME"
)
for (( index = ssh_index + 1; index < separator; index++ )); do
  argument=${!index}
  if [[ $argument == -e ]]; then
    index=$((index + 1))
    # Real `coder ssh -e` parses each value as one CSV record
    # (key1=value1,key2=value2,...); quoted fields keep commas and quotes.
    while IFS= read -r -d '' assignment; do
      assignment=${assignment//\/home\/coder/$REMOTE_DEV_TEST_REMOTE_HOME}
      remote_env+=("$assignment")
    done < <(python3 -c 'import csv, sys
for field in next(csv.reader([sys.argv[1]])):
    sys.stdout.write(field + "\0")' "${!index}")
  fi
done

command=()
for (( index = separator + 1; index <= $#; index++ )); do
  argument=${!index}
  if [[ $argument == /home/coder* ]]; then
    argument=$REMOTE_DEV_TEST_REMOTE_HOME${argument#/home/coder}
  fi
  command+=("$argument")
done

(( ${#command[@]} == 1 )) || {
  echo 'fake coder ssh expected one shell-quoted remote command' >&2
  exit 1
}
remote_command=${command[0]//\/home\/coder/$REMOTE_DEV_TEST_REMOTE_HOME}
remote_command=${remote_command//\/usr\/bin\/timeout/$REMOTE_DEV_TEST_FAKE_BIN\/timeout}

if [[ ${REMOTE_DEV_TEST_DRAIN_PREFLIGHT:-} == true && $remote_command == *MODE_SOURCE_INACTIVE* ]]; then
  # Real SSH may drain stdin even when a preflight command never reads it.
  cat >/dev/null
fi

if [[ -n ${REMOTE_DEV_TEST_HANG_MANIFEST_FILE:-} && -e $REMOTE_DEV_TEST_HANG_MANIFEST_FILE ]]; then
  hang_phase=$(<"$REMOTE_DEV_TEST_HANG_MANIFEST_FILE")
  if [[ $hang_phase == recovery && $remote_command == *PROCESS_RECOVERY_GATE* ]] || \
     [[ $hang_phase == manifest && $remote_command == *remote_manifest_audit* ]]; then
    printf '%s\n' "$$" > "$REMOTE_DEV_TEST_HANG_MANIFEST_FILE.entered"
    while :; do sleep 1; done
  fi
fi

if ! /bin/bash -n -c "$remote_command"; then
  : > "$REMOTE_DEV_TEST_REMOTE_SYNTAX_MARKER"
  echo 'REMOTE_TEST_SHELL_SYNTAX_ERROR' >&2
  exit 98
fi

if [[ $remote_command == *'/usr/local/bin/remote-dev-workspace-role show'* ]]; then
  printf 'runner\n'
  exit 0
fi

if [[ $remote_command == *'/usr/local/bin/remote-dev-workspace-role require runner'* ]]; then
  case "${REMOTE_DEV_TEST_PREFLIGHT_FAILURE:-}" in
    ssh-hang) sleep 20; exit 255 ;;
    ssh-fail) echo 'connection reset by peer' >&2; exit 255 ;;
    role-invalid) echo 'WORKSPACE_ROLE_INVALID marker missing' >&2; exit 1 ;;
  esac
  if [[ ${REMOTE_DEV_TEST_WORKSPACE_ROLE:-runner} != runner ]]; then
    printf 'WORKSPACE_ROLE_MISMATCH expected=runner actual=%s\n' \
      "${REMOTE_DEV_TEST_WORKSPACE_ROLE:-development}" >&2
    exit 1
  fi
  printf 'workspace_role=runner\n'
  exit 0
fi

if [[ $remote_command == *SECRET_ENVELOPE_INVALID* ]]; then
  # The runner secret route carries its environment in a one-line stdin
  # envelope instead of -e values; map it the same way and pass the payload.
  exec env "${remote_env[@]}" /bin/bash -c "$remote_command" < <(
    IFS= read -r envelope
    printf '%s\n' "${envelope//\/home\/coder/$REMOTE_DEV_TEST_REMOTE_HOME}"
    cat
  )
fi

env "${remote_env[@]}" /bin/bash -c "$remote_command"
EOF

cat > "$fake_bin/mutagen" <<'PY'
#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import shutil
import sys

args = sys.argv[1:]
state_root = pathlib.Path(os.environ["REMOTE_DEV_TEST_MUTAGEN_STATE"])
remote_home = os.environ["REMOTE_DEV_TEST_REMOTE_HOME"]
log = pathlib.Path(os.environ["REMOTE_DEV_TEST_COMMAND_LOG"])
with log.open("a", encoding="utf-8") as stream:
    line = "mutagen " + " ".join(json.dumps(item) for item in args)
    if args == ["daemon", "start"]:
        # The daemon reads its SSH connect timeout only from the start environment.
        line += " MUTAGEN_SSH_CONNECT_TIMEOUT=" + os.environ.get("MUTAGEN_SSH_CONNECT_TIMEOUT", "")
    stream.write(line + "\n")

if args == ["version"]:
    print("0.18.1")
    raise SystemExit(0)
if args in (["daemon", "start"], ["daemon", "stop"]):
    raise SystemExit(0)
if len(args) < 2 or args[0] != "sync":
    raise SystemExit(2)
operation = args[1]
resume_failure_file = os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_RESUME_FILE")
pause_failure_file = os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_PAUSE_FILE")
if operation == "resume" and (os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_RESUME") == "true" or (resume_failure_file and pathlib.Path(resume_failure_file).exists())):
    print("PROCESS_TEST_MUTAGEN_RESUME_FAILURE", file=sys.stderr)
    raise SystemExit(96)
if operation == "pause" and (os.environ.get("REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_PAUSE") == "true" or (pause_failure_file and pathlib.Path(pause_failure_file).exists())):
    print("PROCESS_TEST_MUTAGEN_PAUSE_FAILURE", file=sys.stderr)
    raise SystemExit(95)

def session_dir(name):
    return state_root / name

def load_session(name):
    with (session_dir(name) / "session.json").open(encoding="utf-8") as stream:
        return json.load(stream)

def endpoint_path(endpoint):
    path = endpoint.split(":", 1)[1]
    if path.startswith("/home/coder"):
        path = remote_home + path[len("/home/coder"):]
    return path

def item_kind(path):
    if path.is_symlink():
        return "symlink"
    if path.is_file():
        return "file"
    return None

def items(root):
    result = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        kind = item_kind(path)
        if kind:
            result[path.relative_to(root).as_posix()] = kind
    return result

def same(left, right):
    if not left.exists() and not left.is_symlink():
        return not right.exists() and not right.is_symlink()
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes() and (left.stat().st_mode & 0o777) == (right.stat().st_mode & 0o777)

def copy_item(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)

def ignored(path, ignores):
    parts = path.split("/")
    for rule in ignores:
        rule = rule.rstrip("/")
        anchored = rule.startswith("/")
        if anchored:
            rule = rule[1:]
        if "/" not in rule:
            if (path == rule if anchored else rule in parts):
                return True
            continue
        if rule.startswith("**/"):
            suffix = rule[3:]
            if path == suffix or path.startswith(suffix + "/") or f"/{suffix}/" in f"/{path}/":
                return True
            continue
        if path == rule or path.startswith(rule + "/"):
            return True
    return False

def synchronize(name):
    session = load_session(name)
    alpha = pathlib.Path(session["alpha"])
    beta = pathlib.Path(session["beta"])
    baseline = session_dir(name) / "baseline"
    beta.mkdir(parents=True, exist_ok=True)
    baseline.mkdir(parents=True, exist_ok=True)
    alpha_items = {p: k for p, k in items(alpha).items() if not ignored(p, session["ignores"])}
    baseline_items = items(baseline)
    for relative in sorted(set(alpha_items) | set(baseline_items)):
        source = alpha / relative
        target = beta / relative
        previous = baseline / relative
        if relative in alpha_items:
            if relative not in baseline_items:
                if not target.exists() and not target.is_symlink():
                    copy_item(source, target)
                    copy_item(source, previous)
                elif same(source, target):
                    copy_item(source, previous)
            elif same(target, previous):
                copy_item(source, target)
                copy_item(source, previous)
        elif same(target, previous):
            if target.exists() or target.is_symlink():
                target.unlink()
            previous.unlink()

if operation == "create":
    name = args[args.index("--name") + 1]
    ignores = []
    for index, value in enumerate(args):
        if value == "--ignore":
            ignores.append(args[index + 1])
    alpha, beta = args[-2:]
    ghost_create = os.environ.get("REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE")
    if ghost_create:
        ghost_marker = pathlib.Path(ghost_create)
        if not ghost_marker.exists():
            ghost_marker.write_text("ghost create\n", encoding="utf-8")
            alpha_root = pathlib.Path(alpha)
            beta_root = pathlib.Path(endpoint_path(beta))
            for relative in items(alpha_root):
                source = alpha_root / relative
                target = beta_root / relative
                copy_item(source, target)
                if target.is_file() and not target.is_symlink():
                    target.chmod(0o600)
            raise SystemExit(0)
    directory = session_dir(name)
    directory.mkdir(parents=True, exist_ok=False)
    payload = {"alpha": alpha, "beta": endpoint_path(beta), "ignores": ignores, "status": "active"}
    (directory / "session.json").write_text(json.dumps(payload), encoding="utf-8")
    synchronize(name)
elif operation == "list":
    name = args[2]
    payload = load_session(name)
    print(f"Name: {name}\nStatus: {payload['status']}")
elif operation in ("pause", "resume"):
    name = args[2]
    payload = load_session(name)
    payload["status"] = "paused" if operation == "pause" else "active"
    (session_dir(name) / "session.json").write_text(json.dumps(payload), encoding="utf-8")
elif operation == "flush":
    if os.environ.get("REMOTE_DEV_TEST_MUTAGEN_FAIL_FLUSH") == "1":
        raise SystemExit(1)
    synchronize(args[2])
elif operation == "terminate":
    shutil.rmtree(session_dir(args[2]))
else:
    raise SystemExit(2)
PY

chmod +x "$fake_bin"/*

git -C "$project" init -q
git -C "$project" config user.name 'Remote Dev Test'
git -C "$project" config user.email remote-dev@example.test
cat > "$project/.gitignore" <<'EOF'
.env
.cache/
EOF
cat > "$project/argv.sh" <<'EOF'
#!/bin/sh
printf 'OUT<%s>|<%s>|<%s>\n' "$1" "$2" "$3"
printf 'ERR<%s>\n' "$2" >&2
exit 23
EOF
chmod 0755 "$project/argv.sh"
printf 'committed\n' > "$project/tracked.txt"
mkdir -p "$project/packages/legacy/node_modules"
printf 'tracked runtime residue\n' > "$project/packages/legacy/node_modules/tracked.txt"
git -C "$project" add .gitignore argv.sh tracked.txt
git -C "$project" add -f packages/legacy/node_modules/tracked.txt
git -C "$project" commit -qm initial
printf 'dirty tracked\n' > "$project/tracked.txt"
mkdir -p "$project/src" "$project/.cache"
printf 'allowed untracked\n' > "$project/src/generated.txt"
secret_value='IGNORED_SECRET_must_never_move_9482'
printf '%s\n' "$secret_value" > "$project/.env"
printf 'cache local\n' > "$project/.cache/local.txt"

common_env=(
  HOME="$fake_home"
  PATH="$fake_bin:$PATH"
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$state_root"
  REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config"
  REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=true
  REMOTE_DEV_LOCAL_AGENT_WATCHER_LAUNCH_MODE=process
  REMOTE_DEV_LOCAL_AGENT_HEARTBEAT_INTERVAL_SECONDS=0.2
  REMOTE_DEV_TEST_MUTAGEN_STATE="$mutagen_state"
  REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE="$fixture/mutagen-ghost-create.once"
  REMOTE_DEV_TEST_REMOTE_HOME="$remote_home"
  REMOTE_DEV_TEST_FAKE_BIN="$fake_bin"
  REMOTE_DEV_TEST_HANG_MANIFEST_FILE="$fixture/hang-manifest"
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log"
  REMOTE_DEV_TEST_REMOTE_SYNTAX_MARKER="$remote_syntax_marker"
)

run_tool() {
  env "${common_env[@]}" "$tool" "$@"
}

# A linked worktree created inside a bound directory is a separate Git root
# that the enclosing binding never projects; its commands must be refused, not
# routed to the enclosing runner.
assert_nested_worktree_refused() {
  local bound=$1 nested=$1/.claude/worktrees/nested output status
  git -C "$bound" worktree add -q --detach "$nested"
  set +e
  output=$(run_tool exec --for-context "$nested" -- /usr/bin/true 2>&1)
  status=$?
  set -e
  git -C "$bound" worktree remove --force "$nested"
  rm -rf -- "$bound/.claude"
  if (( status == 0 )) || [[ $output != NESTED_WORKTREE_UNBOUND:*"--local-project $nested"* ]]; then
    printf 'an unbound nested worktree was routed to the enclosing binding: %s\n' "$output" >&2
    exit 1
  fi
}

run_tool_watch() {
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false "$tool" "$@"
}

# A tracked exclusion is rooted at the projection root. It must not suppress
# an allowed untracked source whose nested directory has the same leaf name.
anchored_project=$fixture/anchored-project
mkdir -p "$anchored_project/scripts" "$anchored_project/src/scripts"
git -C "$anchored_project" init -q
git -C "$anchored_project" config user.name 'Remote Dev Test'
git -C "$anchored_project" config user.email remote-dev@example.test
printf 'root-only\n' > "$anchored_project/scripts/excluded.mjs"
git -C "$anchored_project" add scripts/excluded.mjs
git -C "$anchored_project" commit -qm initial
printf 'nested allowed\n' > "$anchored_project/src/scripts/allowed.mjs"
env "${common_env[@]}" REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE= "$tool" \
  --profile example-profile --coder-url https://coder.example.test \
  up anchored-runner --project-id anchored --local-project "$anchored_project" \
  --allow-untracked src --exclude-tracked scripts >/dev/null
anchored_remote=$remote_home/projects/anchored
[[ ! -e $anchored_remote/scripts/excluded.mjs ]] || {
  echo 'root tracked exclusion entered the remote projection' >&2; exit 1;
}
[[ $(<"$anchored_remote/src/scripts/allowed.mjs") == 'nested allowed' ]] || {
  echo 'nested allowed source was suppressed by a root tracked exclusion' >&2; exit 1;
}
grep -Fq -- '"--ignore" "/scripts"' "$command_log"
run_tool down --for-context "$anchored_project" >/dev/null

if [[ ${REMOTE_DEV_TEST_SLICE:-} == anchored-ignore ]]; then
  printf 'PASS     Root tracked exclusions preserve nested allowed source\n'
  exit
fi

# Retiring a down binding releases the runner claim so another local root can
# take the same runner workspace. Nothing about the project checkout changes.
if [[ ${REMOTE_DEV_TEST_SLICE:-} == retire ]]; then
  anchored_hash=$(cd "$anchored_project" && pwd -P | tr -d '\n' | shasum -a 256 | awk '{print $1}')
  runner_claim=$state_root/bindings/by-runner/example-profile/anchored-runner.claim
  context_claim=$state_root/bindings/by-context/$anchored_hash.claim
  binding_dir=$state_root/bindings/by-root/$anchored_hash
  [[ -f $runner_claim && -f $context_claim && -d $binding_dir ]] || {
    echo 'retire fixture expects a down binding that still holds its claims' >&2; exit 1;
  }
  env "${common_env[@]}" REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE= "$tool" \
    --profile example-profile --coder-url https://coder.example.test \
    up anchored-runner --project-id anchored --local-project "$anchored_project" \
    --allow-untracked src --exclude-tracked scripts >/dev/null
  if run_tool retire --for-context "$anchored_project" >/dev/null 2>"$fixture/retire-active.err"; then
    echo 'retire accepted a binding that still holds its runner lease' >&2; exit 1;
  fi
  grep -q 'RETIRE_BINDING_ACTIVE' "$fixture/retire-active.err" || {
    echo 'retire did not report the active lease' >&2; cat "$fixture/retire-active.err" >&2; exit 1;
  }
  [[ -d $binding_dir ]] || { echo 'a refused retire removed the binding directory' >&2; exit 1; }
  run_tool down --for-context "$anchored_project" >/dev/null
  run_tool retire --for-context "$anchored_project" >/dev/null
  [[ ! -e $runner_claim && ! -e $context_claim && ! -d $binding_dir ]] || {
    echo 'retire left binding claims or the binding directory in place' >&2; exit 1;
  }
  [[ -n $(find "$state_root/retired" -mindepth 1 -maxdepth 1 -name "*-${anchored_hash:0:16}" -print -quit) ]] || {
    echo 'retire did not preserve the retired binding directory' >&2; exit 1;
  }
  [[ $(<"$anchored_project/src/scripts/allowed.mjs") == 'nested allowed' ]] || {
    echo 'retire modified the project checkout' >&2; exit 1;
  }
  rebound_project=$fixture/rebound-project
  mkdir -p "$rebound_project"
  rebound_project=$(cd "$rebound_project" && pwd -P)
  git -C "$rebound_project" init -q
  git -C "$rebound_project" config user.name 'Remote Dev Test'
  git -C "$rebound_project" config user.email remote-dev@example.test
  printf 'rebound\n' > "$rebound_project/tracked"
  git -C "$rebound_project" add tracked
  git -C "$rebound_project" commit -qm initial
  env "${common_env[@]}" REMOTE_DEV_TEST_MUTAGEN_GHOST_CREATE_ONCE= "$tool" \
    --profile example-profile --coder-url https://coder.example.test \
    up anchored-runner --project-id rebound --local-project "$rebound_project" >/dev/null
  run_tool down --for-context "$rebound_project" >/dev/null
  printf 'PASS     Retiring a down binding releases its runner claim for another local root\n'
  exit
fi

if [[ ${REMOTE_DEV_TEST_SLICE:-} == reanchor ]]; then
  source "$repo_root/tests/remote-dev-local-agent-reanchor-test.sh"
  exit
fi

if [[ ${REMOTE_DEV_TEST_SLICE:-} == additive-policy ]]; then
  source "$repo_root/tests/remote-dev-local-agent-additive-policy.sh"
  exit
fi

if [[ ${REMOTE_DEV_TEST_SLICE:-} == secret-upload ]]; then
  source "$repo_root/tests/remote-dev-secret-upload-watcher-test.sh"
  exit
fi

if [[ ${REMOTE_DEV_TEST_SLICE:-} == tracked-inventory ]]; then
  metadata_project=$fixture/metadata-project
  mkdir -p "$metadata_project"
  metadata_project=$(cd "$metadata_project" && pwd -P)
  git -C "$metadata_project" init -q
  git -C "$metadata_project" config user.name 'Remote Dev Test'
  git -C "$metadata_project" config user.email remote-dev@example.test
  printf 'initial\n' > "$metadata_project/tracked"
  printf 'delete me\n' > "$metadata_project/deleted"
  git -C "$metadata_project" add .
  git -C "$metadata_project" commit -qm initial
  printf 'new bytes\n' > "$metadata_project/tracked"
  printf 'staged addition\n' > "$metadata_project/added"
  git -C "$metadata_project" add added
  rm "$metadata_project/deleted"
  printf 'untracked\n' > "$metadata_project/untracked"
  : > "$fixture/mutagen-ghost-create.once"
  set_runner_fence() {
    python3 - "$remote_home" "$1" <<'PY'
import hashlib,json,pathlib,sys
home=pathlib.Path(sys.argv[1]).resolve(); root=home/'projects/metadata'
key=hashlib.sha256(str(root).encode()).hexdigest()+'.json'
state=home/'.local/share/remote-dev/mode-endpoints'
for kind in ('claims','fences'):
    directory=state/kind; directory.mkdir(parents=True,exist_ok=True)
    (directory/key).unlink(missing_ok=True)
mode=sys.argv[2]
if mode!='clear':
    kind='claims' if mode=='claim' else 'fences'
    value={'root':str(root),'project_id':'metadata','operation_id':'a'*64}
    if mode=='claim': value['token']='b'*64
    else: value['state']=mode
    path=state/kind/key; path.write_text(json.dumps(value)); path.chmod(0o600)
PY
  }
  # No Mac-side fence exists: this also represents another client with the
  # same canonical local path. Runner authority must reject its up/exec.
  set_runner_fence inactive
  if fence_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
      up metadata-runner --project-id metadata --local-project "$metadata_project" --git-metadata explicit-input 2>&1); then
    echo 'inactive runner accepted up from an unfenced client' >&2; exit 1
  fi
  [[ $fence_output == *MODE_SOURCE_INACTIVE* ]]
  set_runner_fence clear
  run_tool --profile example-profile --coder-url https://coder.example.test \
    up metadata-runner --project-id metadata --local-project "$metadata_project" --git-metadata explicit-input >/dev/null
  set_runner_fence claim
  if fence_output=$(run_tool exec --for-context "$metadata_project" -- touch forbidden-execution 2>&1); then
    echo 'foreign runner claim accepted execution' >&2; exit 1
  fi
  [[ $fence_output == *MODE_OPERATION_IN_PROGRESS* && ! -e $remote_home/projects/metadata/forbidden-execution ]]
  env "${common_env[@]}" REMOTE_DEV_MODE_OPERATION_ID="$(printf 'a%.0s' {1..64})" \
    REMOTE_DEV_MODE_TOKEN="$(printf 'b%.0s' {1..64})" "$tool" exec --for-context "$metadata_project" -- /usr/bin/true
  set_runner_fence clear
  inventory_probe='import hashlib,json,os; p=os.environ["REMOTE_DEV_TRACKED_INVENTORY"]; raw=open(p,"rb").read(); data=json.loads(raw); assert hashlib.sha256(raw).hexdigest()==os.environ["REMOTE_DEV_TRACKED_INVENTORY_SHA256"]; assert data["receipt_id"]==os.environ["REMOTE_DEV_EXECUTION_RECEIPT_ID"]; assert data["projection_manifest_id"]==os.environ["REMOTE_DEV_PROJECTION_MANIFEST_ID"]; print(json.dumps({k:data[k] for k in ["files","source_index_id","projection_manifest_id","receipt_id"]}))'
  input_result=$(printf 'caller-input-survives' | env "${common_env[@]}" REMOTE_DEV_TEST_DRAIN_PREFLIGHT=true \
    "$tool" exec --for-context "$metadata_project" -- python3 -c 'import sys; print(sys.stdin.read())')
  [[ $input_result == caller-input-survives ]]
  first_inventory=$(run_tool exec --for-context "$metadata_project" -- python3 -c "$inventory_probe")
  git -C "$metadata_project" add tracked
  second_inventory=$(run_tool exec --for-context "$metadata_project" -- python3 -c "$inventory_probe")
  python3 - "$first_inventory" "$second_inventory" <<'PY'
import json,sys
first,second=map(json.loads,sys.argv[1:])
assert first['files']==second['files']==['added','tracked']
assert first['projection_manifest_id']==second['projection_manifest_id']
assert first['source_index_id']!=second['source_index_id']
assert first['receipt_id']!=second['receipt_id']
PY
  run_tool down --for-context "$metadata_project" >/dev/null
  printf 'PASS     Runner fences enforce authority; explicit tracked inventory follows execution receipts and Git index semantics\n'
  exit 0
fi

binding_records() {
  find "$state_root" \( -name binding.tsv -o -name '*.claim' \) | sort
}
# Bindings retired by an earlier fixture stay preserved by design, so compare
# against the exact records present before the refusal rather than requiring an
# empty state root.
role_records_before=$(binding_records)
set +e
role_output=$(env "${common_env[@]}" REMOTE_DEV_TEST_WORKSPACE_ROLE=development \
  "$tool" --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src 2>&1)
role_status=$?
set -e
if (( role_status != 1 )) || [[ $role_output != *WORKSPACE_ROLE_MISMATCH* ]]; then
  printf 'runner role mismatch was not refused: %s\n' "$role_output" >&2
  exit 1
fi
if [[ $(binding_records) != "$role_records_before" ]]; then
  echo 'role mismatch published a false local binding or runner claim' >&2
  binding_records >&2
  exit 1
fi

mkdir -p "$remote_home/projects/demo"
printf 'operator-owned preexisting file\n' > "$remote_home/projects/demo/preexisting.txt"
set +e
nonempty_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src 2>&1)
nonempty_status=$?
set -e
if (( nonempty_status != 1 )) || [[ $nonempty_output != *RUNNER_ROOT_NOT_EMPTY* ]]; then
  printf 'non-empty first runner binding was not refused: %s\n' "$nonempty_output" >&2
  exit 1
fi
if [[ $(binding_records) != "$role_records_before" ]]; then
  echo 'non-empty remote root failure published a false local binding or claim' >&2
  binding_records >&2
  exit 1
fi
rm -f "$remote_home/projects/demo/preexisting.txt"

set +e
first_up_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src 2>&1)
first_up_status=$?
set -e
if (( first_up_status != 76 )) || [[ $first_up_output != *SYNC_FAILED* ]]; then
  printf 'first incomplete Mutagen session did not fail closed: %s\n' "$first_up_output" >&2
  exit 1
fi
# Address this project's binding by its own root hash: an earlier fixture also
# leaves a preserved binding in this state root, so the first one found is not
# necessarily the one under test.
initial_binding_dir=$state_root/bindings/by-root/$(cd "$project" && pwd -P | tr -d '\n' | shasum -a 256 | awk '{print $1}')
[[ -f $initial_binding_dir/binding.tsv ]] || {
  echo "first incomplete binding was not recorded at $initial_binding_dir" >&2
  exit 1
}
if [[ -e $initial_binding_dir/REMOTE_DRIFT || ! -e $initial_binding_dir/SYNC_FAILED || \
      -e $initial_binding_dir/current-manifest.id ]]; then
  echo 'unaccepted initial projection was incorrectly classified as remote drift' >&2
  exit 1
fi

set +e
initial_drift_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src 2>&1)
initial_drift_status=$?
set -e
if (( initial_drift_status != 74 )) || [[ $initial_drift_output != *REMOTE_DRIFT* ]]; then
  printf 'partial initial projection was not detected as drift: %s\n' "$initial_drift_output" >&2
  exit 1
fi
up_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src --recover-drift)
for expected in \
  'READY          Local Agent / Remote Runner' \
  "Source         $project (authoritative)" \
  'Runner         example-profile/demo-runner (role=runner)' \
  'Remote workdir /home/coder/projects/demo (Git-less projection)'; do
  [[ $up_output == *"$expected"* ]] || {
    printf 'up output omitted %s:\n%s\n' "$expected" "$up_output" >&2
    exit 1
  }
done

ssh_fragment=$fake_home/.ssh/remote-dev-example-profile.conf
grep -Fxq '  ProxyCommand /synthetic/coder --no-version-warning --global-config /synthetic/config ssh --stdio --disable-autostart --wait=no' \
  "$ssh_fragment"

remote_project=$remote_home/projects/demo
remote_control=$remote_home/.local/share/remote-dev/local-agent/demo
[[ $(<"$remote_project/tracked.txt") == 'dirty tracked' ]]
[[ $(<"$remote_project/src/generated.txt") == 'allowed untracked' ]]
[[ ! -e $remote_project/.git && ! -e $remote_project/.env && ! -e $remote_project/.cache/local.txt && \
   ! -e $remote_project/packages/legacy/node_modules ]]

binding_dir=$initial_binding_dir
binding_file=$binding_dir/binding.tsv
managed_manifest=$binding_dir/managed_manifest.tsv
excluded_manifest=$binding_dir/excluded_manifest.tsv
grep -Fq $'argv.sh\tfile\t0755\t' "$managed_manifest"
grep -Fq $'tracked.txt\tfile\t0644\t' "$managed_manifest"
if grep -Fq 'packages/legacy/node_modules/tracked.txt' "$managed_manifest"; then
  echo 'tracked nested runtime path entered the single-root managed manifest' >&2
  exit 1
fi
grep -Fq $'.env\tobserved_ignored' "$excluded_manifest"
grep -Fq $'node_modules\tstandard' "$excluded_manifest"
grep -Fq -- 'mutagen "daemon" "start"' "$command_log"
grep -Fq -- 'mutagen "daemon" "start" MUTAGEN_SSH_CONNECT_TIMEOUT=45' "$command_log"
grep -Fq -- '"--sync-mode" "one-way-safe"' "$command_log"
grep -Fq -- '"--permissions-mode" "portable"' "$command_log"
grep -Fq -- '"--default-file-mode" "0644"' "$command_log"
grep -Fq -- '"--default-directory-mode" "0755"' "$command_log"
grep -Fq -- '"--ignore" "/.env"' "$command_log"

workspace_control=$remote_home/.local/share/remote-dev/local-agent-workspace
workspace_binding=$workspace_control/binding.tsv
[[ -f $workspace_binding && -f $workspace_control/lease.tsv ]]
grep -Fq $'project_id\tdemo' "$workspace_binding"
grep -Fq $'workspace\tdemo-runner' "$workspace_binding"
cp "$workspace_binding" "$fixture/workspace-binding.before"

workspace_state=$fixture/workspace-stopped
for preflight_failure in whoami-hang ssh-hang ssh-fail role-invalid; do
  case "$preflight_failure" in
    whoami-hang) expected_error=CODER_CONTEXT_UNAVAILABLE; expected_status=124 ;;
    ssh-hang) expected_error=RUNNER_UNREACHABLE; expected_status=124 ;;
    ssh-fail) expected_error=RUNNER_UNREACHABLE; expected_status=255 ;;
    role-invalid) expected_error=WORKSPACE_ROLE_INVALID; expected_status=1 ;;
  esac
  preflight_started=$SECONDS
  set +e
  preflight_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_PREFLIGHT_TIMEOUT_SECONDS=1 \
    REMOTE_DEV_TEST_PREFLIGHT_FAILURE="$preflight_failure" "$tool" status --for-context "$project" 2>&1)
  preflight_status=$?
  set -e
  if (( preflight_status != expected_status || SECONDS - preflight_started >= 15 )) || \
     [[ $preflight_output != *"$expected_error"* || $preflight_output == *'create a dedicated runner'* ]]; then
    printf 'preflight failure was misclassified or unbounded: %s %s\n' "$preflight_failure" "$preflight_output" >&2
    exit 1
  fi
done
: > "$workspace_state"
set +e
stopped_output=$(env "${common_env[@]}" REMOTE_DEV_TEST_WORKSPACE_STATE_FILE="$workspace_state" \
  "$tool" status --for-context "$project" 2>&1)
stopped_status=$?
set -e
if (( stopped_status == 0 )) || [[ $stopped_output != WORKSPACE_STOPPED:* ]] || \
   [[ $stopped_output == *'workspace must be started'* ]]; then
  printf 'stopped workspace did not return one normalized recovery: %s\n' "$stopped_output" >&2
  exit 1
fi
resume_output=$(env "${common_env[@]}" REMOTE_DEV_TEST_WORKSPACE_STATE_FILE="$workspace_state" \
  "$tool" resume --for-context "$project")
[[ ! -e $workspace_state ]]
for expected in \
  'READY      Coder workspace is running.' \
  'READY          Local Agent binding resumed' \
  'Synchronization restored' \
  'processes are not restarted automatically'; do
  [[ $resume_output == *"$expected"* ]] || {
    printf 'resume output omitted %s:\n%s\n' "$expected" "$resume_output" >&2
    exit 1
  }
done
run_tool exec --for-context "$project" -- /usr/bin/true >/dev/null
assert_nested_worktree_refused "$project"

for resume_gate in REMOTE_DRIFT UNKNOWN_EXECUTION PROCESS_ROLLBACK_FAILED; do
  printf 'preserve recovery evidence\n' > "$binding_dir/$resume_gate"
  set +e
  resume_blocked_output=$(run_tool resume --for-context "$project" 2>&1)
  resume_blocked_status=$?
  set -e
  if (( resume_blocked_status == 0 )) || [[ $resume_blocked_output != *"$resume_gate"* || \
        $resume_blocked_output == *'Local Agent binding resumed'* ]]; then
    printf 'resume bypassed recovery gate %s: %s\n' "$resume_gate" "$resume_blocked_output" >&2
    exit 1
  fi
  [[ $(<"$binding_dir/$resume_gate") == 'preserve recovery evidence' ]]
  rm "$binding_dir/$resume_gate"
done

second_project=$fixture/second-project
second_state_root=$fixture/second-state
mkdir -p "$second_project"
git -C "$second_project" init -q
git -C "$second_project" config user.name 'Remote Dev Test'
git -C "$second_project" config user.email remote-dev@example.test
printf 'second project\n' > "$second_project/source.txt"
git -C "$second_project" add source.txt
git -C "$second_project" commit -qm initial
set +e
second_binding_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$second_state_root" \
  "$tool" --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo-two --local-project "$second_project" 2>&1)
second_binding_status=$?
set -e
if (( second_binding_status != 1 )) || [[ $second_binding_output != *RUNNER_WORKSPACE_BINDING_CONFLICT* ]]; then
  printf 'second local state root claimed the same runner: %s\n' "$second_binding_output" >&2
  exit 1
fi
if find "$second_state_root" -name binding.tsv -o -name '*.claim' | grep -q .; then
  echo 'rejected second client published a false local binding or claim' >&2
  exit 1
fi
cmp -s "$fixture/workspace-binding.before" "$workspace_binding" || {
  echo 'rejected second client disturbed the first workspace-global binding' >&2
  exit 1
}
[[ ! -e $remote_home/projects/demo-two ]]
[[ ! -e $remote_home/.local/share/remote-dev/local-agent/demo-two ]]
run_tool exec --for-root "$project" -- /usr/bin/true

env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_REMOTE_AUDIT_INTERVAL_SECONDS=0 \
  REMOTE_DEV_LOCAL_AGENT_WATCHER_AUDIT_TIMEOUT_SECONDS=5 \
  "$tool" --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
bounded_audit_watcher_pid=$(<"$binding_dir/watcher.pid")
printf 'recovery\n' > "$fixture/hang-manifest"
bounded_audit_entered=false
for _ in $(seq 1 300); do
  if [[ -s $fixture/hang-manifest.entered || -e $fixture/hang-manifest.entered ]]; then
    bounded_audit_entered=true
    break
  fi
  sleep 0.05
done
[[ $bounded_audit_entered == true ]] || {
  echo 'bounded watcher audit did not enter the synthetic hung recovery transport' >&2
  [[ ! -f $binding_dir/watcher.log ]] || { echo 'watcher log:' >&2; tail -80 "$binding_dir/watcher.log" >&2; }
  echo 'recent command log:' >&2
  tail -40 "$command_log" >&2
  exit 1
}
bounded_audit_transport_pid=$(<"$fixture/hang-manifest.entered")
[[ $bounded_audit_transport_pid =~ ^[0-9]+$ ]] || {
  echo 'synthetic hung audit did not publish its transport PID' >&2
  exit 1
}
kill "$bounded_audit_watcher_pid"
bounded_audit_stopped=false
for _ in $(seq 1 100); do
  if ! kill -0 "$bounded_audit_watcher_pid" 2>/dev/null && \
     ! kill -0 "$bounded_audit_transport_pid" 2>/dev/null && \
     [[ ! -e $binding_dir/operation.lock && ! -e $binding_dir/watcher.pid && \
        ! -e $binding_dir/watcher.identity.tsv ]]; then
    bounded_audit_stopped=true
    break
  fi
  sleep 0.05
done
[[ $bounded_audit_stopped == true ]] || {
  echo 'TERM did not cancel the bounded watcher audit and release its lock' >&2
  exit 1
}
rm -f "$fixture/hang-manifest" "$fixture/hang-manifest.entered"
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
lease_watcher_pid=$(<"$binding_dir/watcher.pid")
cp "$workspace_control/lease.tsv" "$fixture/workspace-lease.before"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "owner_id" {$2="foreign-owner"} {print}' \
  "$workspace_control/lease.tsv" > "$fixture/workspace-lease.foreign"
mv "$fixture/workspace-lease.foreign" "$workspace_control/lease.tsv"
printf 'must not synchronize without lease\n' > "$project/tracked.txt"
watcher_failed_closed=false
for _ in $(seq 1 40); do
  if grep -Fq 'LEASE_CONFLICT' "$binding_dir/watcher.log" 2>/dev/null; then
    watcher_failed_closed=true
    break
  fi
  sleep 0.1
done
[[ $watcher_failed_closed == true ]] || { echo 'watcher did not report lease failure' >&2; exit 1; }
set +e
lease_failure_output=$(run_tool exec --for-root "$project" -- /usr/bin/true 2>&1)
lease_failure_status=$?
set -e
if (( lease_failure_status != 1 )) || [[ $lease_failure_output != *LEASE_CONFLICT* ]]; then
  printf 'lease failure did not fail closed: %s\n' "$lease_failure_output" >&2
  exit 1
fi
[[ $(<"$remote_project/tracked.txt") == 'dirty tracked' ]] || {
  echo 'source mutated remotely after workspace lease failure' >&2
  exit 1
}
lease_watcher_stopped=false
for _ in $(seq 1 100); do
  if ! kill -0 "$lease_watcher_pid" 2>/dev/null && [[ ! -e $binding_dir/operation.lock ]]; then
    lease_watcher_stopped=true
    break
  fi
  sleep 0.05
done
[[ $lease_watcher_stopped == true ]] || {
  echo 'lease-conflicted watcher did not exit and release its operation lock' >&2
  exit 1
}
rm -f "$binding_dir/watcher.pid" "$binding_dir/watcher.identity.tsv"
cp "$fixture/workspace-lease.before" "$workspace_control/lease.tsv"
printf 'dirty tracked\n' > "$project/tracked.txt"

mkdir -p "$remote_project/node_modules"
printf 'runtime cache survives\n' > "$remote_project/node_modules/cache.txt"
printf 'REMOTE_ONLY_SECRET=synthetic\n' > "$remote_project/.env"

set +e
policy_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" \
  --allow-untracked src --remote-writable custom-cache 2>&1)
policy_status=$?
set -e
if (( policy_status != 1 )) || [[ $policy_output != *BINDING_POLICY_MISMATCH* ]]; then
  printf 'existing binding policy mismatch was not refused: %s\n' "$policy_output" >&2
  exit 1
fi

stdout_file=$fixture/exec.stdout
stderr_file=$fixture/exec.stderr
set +e
run_tool exec --for-root "$project" -- ./argv.sh \
  'space value' '; touch SHOULD_NOT_EXIST' '$(touch ALSO_NOT)' \
  > "$stdout_file" 2> "$stderr_file"
exec_status=$?
set -e
if (( exec_status != 23 )); then
  printf 'remote exit status was not preserved: %s\n' "$exec_status" >&2
  cat "$stderr_file" >&2
  exit 1
fi
printf 'OUT<space value>|<; touch SHOULD_NOT_EXIST>|<$(touch ALSO_NOT)>\n' > "$fixture/expected.stdout"
printf 'ERR<; touch SHOULD_NOT_EXIST>\n' > "$fixture/expected.stderr"
cmp -s "$fixture/expected.stdout" "$stdout_file" || {
  echo 'remote stdout was not byte-exact' >&2; exit 1;
}
cmp -s "$fixture/expected.stderr" "$stderr_file" || {
  echo 'remote stderr was not byte-exact' >&2; cat "$stderr_file" >&2; exit 1;
}
[[ ! -e $remote_project/SHOULD_NOT_EXIST && ! -e $remote_project/ALSO_NOT ]]
[[ $(<"$remote_project/node_modules/cache.txt") == 'runtime cache survives' ]]
[[ $(<"$remote_project/.env") == 'REMOTE_ONLY_SECRET=synthetic' ]]
grep -Fq $'exit_code\t23' "$binding_dir/current_receipt.tsv"
grep -Fxq $'argc\t4' "$binding_dir/current_receipt.tsv"
grep -Fxq $'program\targv.sh' "$binding_dir/current_receipt.tsv"
if grep -Eq 'argv_|space value|SHOULD_NOT_EXIST' "$binding_dir/current_receipt.tsv"; then
  echo 'exec receipt retained argument values' >&2; exit 1
fi

mkdir -p "$remote_project/node_modules"
printf 'remote cache survives\n' > "$remote_project/node_modules/cache.txt"
printf '%s\n' "$secret_value" > "$remote_project/.env"
printf 'second dirty revision\n' > "$project/tracked.txt"
exec_output=$(run_tool exec --for-root "$project" -- /bin/sh -c \
  'printf "%s|%s\n" "$(cat tracked.txt)" "$(cat src/generated.txt)"')
[[ $exec_output == 'second dirty revision|allowed untracked' ]]
[[ $(<"$remote_project/node_modules/cache.txt") == 'remote cache survives' ]]
[[ $(<"$remote_project/.env") == "$secret_value" ]]

# A failed transfer must retain the accepted baseline, so an unchanged runner
# can retry without being misclassified as a foreign source edit.
cp "$binding_dir/managed_manifest.tsv" "$fixture/accepted-before-failed-sync.tsv"
printf 'revision after failed transfer\n' > "$project/tracked.txt"
set +e
failed_flush_output=$(env "${common_env[@]}" REMOTE_DEV_TEST_MUTAGEN_FAIL_FLUSH=1 \
  "$tool" exec --for-root "$project" -- /usr/bin/true 2>&1)
failed_flush_status=$?
set -e
if (( failed_flush_status != 76 )) || [[ $failed_flush_output != *SYNC_FAILED* ]]; then
  printf 'failed flush was not reported: %s\n' "$failed_flush_output" >&2
  exit 1
fi
cmp -s "$fixture/accepted-before-failed-sync.tsv" "$binding_dir/managed_manifest.tsv"
[[ ! -e $binding_dir/REMOTE_DRIFT && -e $binding_dir/SYNC_FAILED ]]
[[ $(<"$remote_project/tracked.txt") == 'second dirty revision' ]]
retry_output=$(run_tool exec --for-root "$project" -- /bin/cat tracked.txt)
[[ $retry_output == 'revision after failed transfer' ]]
[[ ! -e $binding_dir/REMOTE_DRIFT && ! -e $binding_dir/SYNC_FAILED ]]
printf 'second dirty revision\n' > "$project/tracked.txt"
run_tool exec --for-root "$project" -- /usr/bin/true

printf 'remote-only source injection\n' > "$remote_project/injected-source.txt"
set +e
injected_output=$(run_tool exec --for-root "$project" -- /usr/bin/true 2>&1)
injected_status=$?
set -e
if (( injected_status != 74 )) || [[ $injected_output != *REMOTE_DRIFT* ]] || \
   [[ ! -f $remote_project/injected-source.txt ]]; then
  printf 'remote-only source path was not detected and preserved: %s\n' "$injected_output" >&2
  exit 1
fi
grep -Fq $'injected-source.txt\tunexpected-file' "$binding_dir/REMOTE_DRIFT"
set +e
drift_status_output=$(run_tool status --for-root "$project" 2>&1)
drift_status_code=$?
set -e
if (( drift_status_code != 79 )) || [[ $drift_status_output != *'Projection     REMOTE_DRIFT'* ]]; then
  printf 'status did not expose projection drift: %s\n' "$drift_status_output" >&2
  exit 1
fi
rm -f "$remote_project/injected-source.txt"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src \
  --recover-drift >/dev/null
printf 'SYNC_FAILED\nstatus\t99\n' > "$binding_dir/SYNC_FAILED"
set +e
sync_status_output=$(run_tool status --for-root "$project" 2>&1)
sync_status_code=$?
set -e
if (( sync_status_code != 79 )) || [[ $sync_status_output != *'Synchronization SYNC_FAILED'* ]]; then
  printf 'status did not expose synchronization failure: %s\n' "$sync_status_output" >&2
  exit 1
fi
rm -f "$binding_dir/SYNC_FAILED"

printf 'REMOTE EDIT MUST SURVIVE\n' > "$remote_project/tracked.txt"
set +e
drift_output=$(run_tool exec --for-root "$project" -- /usr/bin/true 2>&1)
drift_status=$?
set -e
if (( drift_status != 74 )) || [[ $drift_output != *REMOTE_DRIFT* ]]; then
  printf 'remote drift did not fail closed: %s\n' "$drift_output" >&2
  exit 1
fi
[[ $(<"$remote_project/tracked.txt") == 'REMOTE EDIT MUST SURVIVE' ]]
[[ -f $binding_dir/REMOTE_DRIFT ]]

cp "$project/tracked.txt" "$remote_project/tracked.txt"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --recover-drift >/dev/null
[[ ! -e $binding_dir/REMOTE_DRIFT ]]

set +e
post_drift_output=$(run_tool exec --for-root "$project" -- /bin/sh -c \
  'printf "remote command edit\n" > tracked.txt' 2>&1)
post_drift_status=$?
set -e
if (( post_drift_status != 74 )) || [[ $post_drift_output != *REMOTE_DRIFT* ]] || \
   [[ $(<"$remote_project/tracked.txt") != 'remote command edit' ]]; then
  printf 'managed command source drift was not preserved and blocked: %s\n' \
    "$post_drift_output" >&2
  exit 1
fi
cp "$project/tracked.txt" "$remote_project/tracked.txt"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --recover-drift >/dev/null

stale_lock=$binding_dir/operation.lock
mkdir "$stale_lock"
printf '99999999\n' > "$stale_lock/pid"
printf 'stale-start-token\n' > "$stale_lock/start_token"
stale_one_output=$fixture/stale-lock-one.output
stale_two_output=$fixture/stale-lock-two.output
stale_one_status=$fixture/stale-lock-one.status
stale_two_status=$fixture/stale-lock-two.status
(
  set +e
  run_tool exec --for-root "$project" -- /bin/true > "$stale_one_output" 2>&1
  printf '%s\n' "$?" > "$stale_one_status"
) &
stale_one_pid=$!
(
  set +e
  run_tool exec --for-root "$project" -- /bin/true > "$stale_two_output" 2>&1
  printf '%s\n' "$?" > "$stale_two_status"
) &
stale_two_pid=$!
wait "$stale_one_pid"
wait "$stale_two_pid"
if [[ $(<"$stale_one_status") != 77 || $(<"$stale_two_status") != 77 ]] ||
   ! grep -Fq 'OPERATION_LOCK_STALE' "$stale_one_output" ||
   ! grep -Fq 'OPERATION_LOCK_STALE' "$stale_two_output" ||
   [[ $(<"$stale_lock/pid") != 99999999 ]] ||
   [[ $(<"$stale_lock/start_token") != stale-start-token ]]; then
  echo 'concurrent stale local lock contenders did not fail closed without reclaiming the lock' >&2
  exit 1
fi
rm -f "$stale_lock/pid" "$stale_lock/start_token"
rmdir "$stale_lock"

for partial_lock_state in empty pid-only token-only; do
  mkdir "$stale_lock"
  case "$partial_lock_state" in
    pid-only) printf '99999999\n' > "$stale_lock/pid" ;;
    token-only) printf 'partial-start-token\n' > "$stale_lock/start_token" ;;
  esac
  set +e
  partial_lock_output=$(run_tool exec --for-root "$project" -- /bin/true 2>&1)
  partial_lock_status=$?
  set -e
  if (( partial_lock_status != 77 )) || \
     [[ $partial_lock_output != *OPERATION_LOCK_INCOMPLETE* ]] || \
     [[ $partial_lock_output != *'Recovery: inspect '* ]]; then
    printf 'partial operation lock %s did not fail closed with exact recovery: %s\n' \
      "$partial_lock_state" "$partial_lock_output" >&2
    exit 1
  fi
  case "$partial_lock_state" in
    empty) [[ ! -e $stale_lock/pid && ! -e $stale_lock/start_token ]] ;;
    pid-only) [[ $(<"$stale_lock/pid") == 99999999 && ! -e $stale_lock/start_token ]] ;;
    token-only) [[ ! -e $stale_lock/pid && $(<"$stale_lock/start_token") == partial-start-token ]] ;;
  esac
  rm -f "$stale_lock/pid" "$stale_lock/start_token"
  rmdir "$stale_lock"
done

env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_LOCK_WAIT_ATTEMPTS=5 "$tool" \
  --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
slow_stdout=$fixture/slow-exec.stdout
slow_stderr=$fixture/slow-exec.stderr
slow_release_file=$remote_project/node_modules/slow-exec.release
rm -f "$slow_release_file"
env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_LOCK_WAIT_ATTEMPTS=300 "$tool" exec --for-root "$project" -- /bin/sh -c \
  'while [ ! -f node_modules/slow-exec.release ]; do sleep 0.05; done; cat tracked.txt' \
  > "$slow_stdout" 2> "$slow_stderr" &
slow_exec_pid=$!
remote_execution_seen=false
running_deadline=$((SECONDS + 20))
while (( SECONDS < running_deadline )); do
  if find "$remote_control/executions" -maxdepth 1 -name '*.running' -print -quit | grep -q .; then
    remote_execution_seen=true
    break
  fi
  kill -0 "$slow_exec_pid" 2>/dev/null || break
  sleep 0.05
done
if [[ $remote_execution_seen != true ]]; then
  : > "$slow_release_file"
  wait "$slow_exec_pid" 2>/dev/null || true
  slow_exec_pid=
  printf 'slow exec did not publish remote running state before its bounded deadline\n' >&2
  cat "$slow_stderr" >&2 || true
  exit 1
fi
watcher_pid=$(<"$binding_dir/watcher.pid")
kill -0 "$watcher_pid" 2>/dev/null || { echo 'watcher exited before concurrent exec check' >&2; exit 1; }
[[ -f $binding_dir/operation.lock/pid ]] || { echo 'exec did not retain the operation lock' >&2; exit 1; }
heartbeat_seen=false
heartbeat_deadline=$((SECONDS + 10))
while (( SECONDS < heartbeat_deadline )); do
  if grep -Fq $'operation\texec-heartbeat' "$workspace_control/lease.tsv"; then
    heartbeat_seen=true
    break
  fi
  kill -0 "$slow_exec_pid" 2>/dev/null || break
  sleep 0.05
done
if [[ $heartbeat_seen != true ]]; then
  : > "$slow_release_file"
  wait "$slow_exec_pid" 2>/dev/null || true
  slow_exec_pid=
  echo 'slow exec did not refresh its independent lease heartbeat' >&2
  exit 1
fi
former_lock_deadline=$((SECONDS + 2))
while (( SECONDS < former_lock_deadline )); do
  kill -0 "$slow_exec_pid" 2>/dev/null || {
    echo 'slow exec ended before exceeding the former watcher lock timeout' >&2
    exit 1
  }
  kill -0 "$watcher_pid" 2>/dev/null || {
    echo 'watcher exited while a live whole-exec lock was held' >&2
    exit 1
  }
  sleep 0.05
done
printf 'edit during terminating exec\n' > "$project/tracked.txt"
[[ $(<"$remote_project/tracked.txt") == 'second dirty revision' ]] || {
  echo 'watcher synchronized while exec still owned the operation lock' >&2
  exit 1
}
: > "$slow_release_file"
wait "$slow_exec_pid"
slow_exec_pid=
rm -f "$slow_release_file"
slow_release_file=
[[ $(<"$slow_stdout") == 'second dirty revision' && ! -s $slow_stderr ]]
post_exec_synced=false
sync_deadline=$((SECONDS + 20))
while (( SECONDS < sync_deadline )); do
  if [[ $(<"$remote_project/tracked.txt") == 'edit during terminating exec' ]]; then
    post_exec_synced=true
    break
  fi
  kill -0 "$watcher_pid" 2>/dev/null || break
  sleep 0.1
done
[[ $post_exec_synced == true ]] || { echo 'watcher did not resume after exec released its lock' >&2; exit 1; }
healthy_status_output=$(run_tool status --for-root "$project")
if [[ $healthy_status_output != *'Watcher        running'* || $healthy_status_output != *'Health         ready'* ]]; then
  printf 'status did not report a healthy live watcher after long exec: %s\n' "$healthy_status_output" >&2
  exit 1
fi
[[ $healthy_status_output == *'Transfer       managed'* ]]
[[ $healthy_status_output == *'Activity       '* ]]
[[ $healthy_status_output != *'Identifier:'* && $healthy_status_output != *'Paused'* ]]

heartbeat_release=$remote_project/node_modules/heartbeat-identity.release
rm -f "$heartbeat_release"
env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false "$tool" \
  exec --for-root "$project" -- /bin/sh -c \
  'while [ ! -f node_modules/heartbeat-identity.release ]; do sleep 0.05; done' &
heartbeat_exec_pid=$!
heartbeat_identity=
# This wait includes the entire execution barrier before heartbeat startup.
# Keep it distinct from the five-second heartbeat publication check in core.
for _ in $(seq 1 600); do
  heartbeat_identity=$(find "$binding_dir/execution-heartbeats" -name '*.identity.tsv' -print -quit 2>/dev/null || true)
  [[ -n $heartbeat_identity ]] && break
  kill -0 "$heartbeat_exec_pid" 2>/dev/null || break
  sleep 0.05
done
[[ -n $heartbeat_identity ]] || { echo 'execution heartbeat identity was not published' >&2; exit 1; }
heartbeat_remote_running=false
for _ in $(seq 1 600); do
  if find "$remote_control/executions" -maxdepth 1 -name '*.running' -print -quit | grep -q .; then
    heartbeat_remote_running=true
    break
  fi
  kill -0 "$heartbeat_exec_pid" 2>/dev/null || break
  sleep 0.05
done
[[ $heartbeat_remote_running == true ]] || { echo 'heartbeat test exec did not enter remote running state' >&2; exit 1; }
cp "$heartbeat_identity" "$fixture/heartbeat.identity.valid"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "start_token" {$2="synthetic-reused-heartbeat-token"} {print}' \
  "$heartbeat_identity" > "$fixture/heartbeat.identity.reused"
mv "$fixture/heartbeat.identity.reused" "$heartbeat_identity"
set +e
reused_heartbeat_status=$(run_tool status --for-root "$project" 2>&1)
reused_heartbeat_code=$?
set -e
if (( reused_heartbeat_code != 79 )) || \
   [[ $reused_heartbeat_status != *'pid-reused-or-parent-mismatch'* ]]; then
  printf 'status trusted a reused heartbeat PID identity: %s\n' "$reused_heartbeat_status" >&2
  exit 1
fi
heartbeat_failed=false
for _ in $(seq 1 100); do
  if [[ -f $binding_dir/LEASE_HEARTBEAT_FAILED ]]; then heartbeat_failed=true; break; fi
  sleep 0.05
done
[[ $heartbeat_failed == true ]] || { echo 'reused heartbeat identity did not fail its lease' >&2; exit 1; }
cp "$fixture/heartbeat.identity.valid" "$heartbeat_identity"
: > "$heartbeat_release"
set +e
wait "$heartbeat_exec_pid"
heartbeat_exec_code=$?
set -e
if (( heartbeat_exec_code != 78 )); then
  printf 'execution with reused heartbeat identity returned %s, expected 78\n' "$heartbeat_exec_code" >&2
  exit 1
fi
rm -f "$heartbeat_release"
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src \
  --recover-unknown >/dev/null

# Unknown-execution recovery can replace the watcher that failed closed on the
# marker. Stop the currently published identity, not the pre-recovery PID.
watcher_pid=$(<"$binding_dir/watcher.pid")
kill "$watcher_pid" 2>/dev/null || true
wait "$watcher_pid" 2>/dev/null || true
watcher_deadline=$((SECONDS + 5))
while kill -0 "$watcher_pid" 2>/dev/null && (( SECONDS < watcher_deadline )); do sleep 0.05; done
set +e
dead_watcher_status=$(run_tool status --for-root "$project" 2>&1)
dead_watcher_code=$?
set -e
if (( dead_watcher_code != 79 )) || [[ $dead_watcher_status != *'Watcher        stopped'* ]] || \
   [[ $dead_watcher_status != *'Health         blocked'* ]]; then
  printf 'status did not block an expected-active binding with a dead watcher: %s\n' "$dead_watcher_status" >&2
  exit 1
fi
for _ in $(seq 1 100); do
  [[ ! -e $binding_dir/operation.lock ]] && break
  sleep 0.05
done
[[ ! -e $binding_dir/operation.lock ]] || {
  echo 'TERM-stopped watcher left an operation lock behind' >&2
  exit 1
}
rm -f "$binding_dir/watcher.pid" "$binding_dir/watcher.identity.tsv"

# TERM/INT cannot run EXIT cleanup after mkdir succeeds but before the watcher
# publishes ownership of the local operation lock. Exercise that exact window
# repeatedly and require both identity and lock cleanup without false stale or
# incomplete-lock diagnostics.
for watcher_lock_attempt in $(seq 1 5); do
  printf 'watcher lock race %s\n' "$watcher_lock_attempt" > "$project/tracked.txt"
  watcher_lock_gate=$fixture/watcher-lock-term-$watcher_lock_attempt
  watcher_lock_stdout=$watcher_lock_gate.stdout
  watcher_lock_stderr=$watcher_lock_gate.stderr
  watcher_lock_status=$watcher_lock_gate.status
  (
    set +e
    env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
      REMOTE_DEV_LOCAL_AGENT_TEST_WATCHER_LOCK_GATE="$watcher_lock_gate" \
      "$tool" _watch "$binding_dir" > "$watcher_lock_stdout" 2> "$watcher_lock_stderr"
    printf '%s\n' "$?" > "$watcher_lock_status"
  ) &
  watcher_lock_test_pid=$!
  wait_for_file "$watcher_lock_gate.ready" || {
    echo "watcher lock TERM gate $watcher_lock_attempt was not reached" >&2; exit 1;
  }
  watcher_lock_inner_pid=$(<"$watcher_lock_gate.ready")
  kill -TERM "$watcher_lock_inner_pid"
  : > "$watcher_lock_gate.release"
  wait "$watcher_lock_test_pid"
  watcher_lock_test_pid=
  watcher_lock_gate=
  if [[ $(<"$watcher_lock_status") != 143 ]] || [[ -e $binding_dir/operation.lock ]] || \
     [[ -e $binding_dir/watcher.pid || -e $binding_dir/watcher.identity.tsv ]] || \
     grep -Eq 'OPERATION_LOCK_(INCOMPLETE|STALE|CONFLICT|PUBLICATION_FAILED)' "$watcher_lock_stderr"; then
    printf 'watcher lock TERM race %s leaked or misdiagnosed: status=%s lock=%s pid=%s identity=%s stderr=%s\n' \
      "$watcher_lock_attempt" "$(<"$watcher_lock_status")" \
      "$([[ -e $binding_dir/operation.lock ]] && printf yes || printf no)" \
      "$([[ -e $binding_dir/watcher.pid ]] && printf yes || printf no)" \
      "$([[ -e $binding_dir/watcher.identity.tsv ]] && printf yes || printf no)" \
      "$(<"$watcher_lock_stderr")" >&2
    exit 1
  fi
done

cat > "$project/server.sh" <<'EOF'
#!/bin/sh
mkdir -p node_modules
python3 - "$PWD/node_modules/managed-child.pid" "$PWD/node_modules/managed-port" <<'PY' &
import os
import socket
import sys

pid_path, port_path = sys.argv[1:]
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 0))
server.listen()
server.settimeout(0.2)
with open(pid_path, "w", encoding="ascii") as stream:
    stream.write(str(os.getpid()) + "\n")
with open(port_path, "w", encoding="ascii") as stream:
    stream.write(str(server.getsockname()[1]) + "\n")
print("managed-ready", flush=True)
while True:
    try:
        connection, _ = server.accept()
    except socket.timeout:
        continue
    connection.close()
PY
# The top-level wrapper deliberately exits. The managed supervisor must stay
# alive while the descendant still owns the session and TCP listener.
exit 0
EOF
chmod 0755 "$project/server.sh"
git -C "$project" add server.sh

# The remote workspace mutex orders cancellation against a delayed launch.
# Stop from an independent Mac-local state root while the original CLI is
# paused after durable intent publication but before fork. The later child must
# acquire the same mutex, observe the archived intent, and exit before argv.
rm -f "$remote_project/node_modules/managed-child.pid" "$remote_project/node_modules/managed-port"
intent_cancel_gate=$fixture/intent-cancel-before-fork
intent_cancel_stdout=$intent_cancel_gate.stdout
intent_cancel_stderr=$intent_cancel_gate.stderr
intent_cancel_status=$intent_cancel_gate.status
(
  set +e
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_PAUSE_AFTER_RECOVERY_INTENT="$intent_cancel_gate" \
    "$tool" start --for-root "$project" -- ./server.sh \
    > "$intent_cancel_stdout" 2> "$intent_cancel_stderr"
  printf '%s\n' "$?" > "$intent_cancel_status"
) &
intent_cancel_start_pid=$!
wait_for_file "$intent_cancel_gate.ready" || {
  echo 'pre-fork intent cancellation gate was not reached' >&2
  cat "$intent_cancel_stderr" >&2
  exit 1
}
intent_cancel_handle=$(<"$intent_cancel_gate.ready")
[[ $intent_cancel_handle =~ ^proc- ]] || {
  printf 'pre-fork gate did not expose an exact handle: %s\n' "$intent_cancel_handle" >&2; exit 1;
}
intent_cancel_recovery=$remote_control/processes/recovery/$intent_cancel_handle
if [[ ! -f $intent_cancel_recovery.tsv ]] || \
   ! grep -Fq $'recovery_phase\tidentity-pending' "$intent_cancel_recovery.tsv" || \
   ! grep -Fq $'supervisor_state\tnot-started' "$intent_cancel_recovery.tsv"; then
  printf 'pre-fork gate did not publish a durable not-started intent: handle=%s record=%s contents=%s\n' \
    "$intent_cancel_handle" "$intent_cancel_recovery.tsv" \
    "$([[ -f $intent_cancel_recovery.tsv ]] && tr '\n' '|' < "$intent_cancel_recovery.tsv" || printf missing)" >&2
  exit 1
fi
intent_cancel_state=$fixture/intent-cancel-state
mkdir -p "$intent_cancel_state"
cp -R "$state_root/." "$intent_cancel_state/"
intent_cancel_binding_dir=$intent_cancel_state/${binding_dir#"$state_root"/}
while IFS= read -r intent_cancel_claim; do
  if [[ $(<"$intent_cancel_claim") == "$binding_dir" ]]; then
    printf '%s\n' "$intent_cancel_binding_dir" > "$intent_cancel_claim"
  fi
done < <(find "$intent_cancel_state/bindings" -name '*.claim' -type f)
rm -rf -- "$intent_cancel_binding_dir/operation.lock"
rm -f "$intent_cancel_binding_dir/watcher.pid" "$intent_cancel_binding_dir/watcher.identity.tsv"
intent_cancel_stop=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$intent_cancel_state" \
  "$tool" stop --for-root "$project" "$intent_cancel_handle")
[[ $intent_cancel_stop == "STOPPED  $intent_cancel_handle" ]]
if [[ -e $intent_cancel_recovery.tsv ]] || \
   [[ ! -f $remote_control/processes/stopped/$intent_cancel_handle.tsv ]] || \
   ! grep -Fq $'recovery_state\tintent-cleared-no-launch' "$remote_control/processes/stopped/$intent_cancel_handle.tsv"; then
  echo 'concurrent exact stop did not archive and remove the pre-fork intent' >&2
  exit 1
fi
: > "$intent_cancel_gate.release"
wait "$intent_cancel_start_pid"
intent_cancel_start_pid=
intent_cancel_gate=
if [[ $(<"$intent_cancel_status") == 0 ]]; then
  printf 'cancelled pre-fork start unexpectedly succeeded: %s\n%s\n' \
    "$(<"$intent_cancel_stdout")" "$(<"$intent_cancel_stderr")" >&2
  exit 1
fi
sleep 0.2
if [[ -e $remote_project/node_modules/managed-child.pid || -e $remote_project/node_modules/managed-port ]] || \
   find "$remote_control/processes/active" -name "*$intent_cancel_handle*" -print -quit | grep -q . || \
   find "$remote_control/processes/recovery" -name "$intent_cancel_handle.*" -print -quit | grep -q .; then
  printf 'cancelled delayed child produced argv/identity/ownership residue: stderr=%s\n' \
    "$(<"$intent_cancel_stderr")" >&2
  exit 1
fi

start_output=$(run_tool_watch start --for-root "$project" -- ./server.sh)
handle=$(sed -n 's/^STARTED  //p' <<<"$start_output")
[[ $handle =~ ^proc- ]]
managed_child_file=$remote_project/node_modules/managed-child.pid
managed_port_file=$remote_project/node_modules/managed-port
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'managed descendant did not publish its PID/port' >&2
  [[ ! -f $binding_dir/watcher.log ]] || { echo 'watcher log:' >&2; cat "$binding_dir/watcher.log" >&2; }
  [[ ! -f $binding_dir/REMOTE_DRIFT ]] || { echo 'remote drift:' >&2; cat "$binding_dir/REMOTE_DRIFT" >&2; }
  [[ ! -f $remote_control/logs/$handle.log ]] || { echo 'process log:' >&2; cat "$remote_control/logs/$handle.log" >&2; }
  exit 1
}
managed_child_pid=$(<"$managed_child_file")
managed_port=$(<"$managed_port_file")
kill -0 "$managed_child_pid"
tcp_port_open "$managed_port" || { echo 'managed descendant TCP port is not open' >&2; exit 1; }
logs_output=$(run_tool logs --for-root "$project" "$handle")
[[ $logs_output == *managed-ready* ]]
managed_status_output=$(run_tool status --for-root "$project")
[[ $managed_status_output == *$'active_process\t'"$handle"$'\tlive'* ]]
[[ $managed_status_output == *'Health         ready'* ]]
cp "$binding_dir/watcher.identity.tsv" "$fixture/watcher.identity.valid"
managed_watcher_pid=$(<"$binding_dir/watcher.pid")
kill -STOP "$managed_watcher_pid"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "start_token" {$2="synthetic-reused-watcher-token"} {print}' \
  "$binding_dir/watcher.identity.tsv" > "$fixture/watcher.identity.reused"
mv "$fixture/watcher.identity.reused" "$binding_dir/watcher.identity.tsv"
set +e
reused_watcher_status=$(run_tool status --for-root "$project" 2>&1)
reused_watcher_code=$?
reused_watcher_down=$(run_tool down --for-root "$project" 2>&1)
reused_watcher_down_code=$?
set -e
if (( reused_watcher_code != 79 || reused_watcher_down_code == 0 )) || \
   [[ $reused_watcher_status != *'Watcher        pid-reused-or-foreign'* ]] || \
   [[ $reused_watcher_down != *WATCHER_PID_REUSED* ]] || \
   ! kill -0 "$managed_child_pid" 2>/dev/null || ! tcp_port_open "$managed_port"; then
  printf 'reused watcher PID identity did not fail closed:\n%s\n%s\n' \
    "$reused_watcher_status" "$reused_watcher_down" >&2
  exit 1
fi
cp "$fixture/watcher.identity.valid" "$binding_dir/watcher.identity.tsv"
kill -CONT "$managed_watcher_pid"
printf 'watcher revision\n' > "$project/tracked.txt"
watcher_updated=false
for _ in $(seq 1 80); do
  if [[ $(<"$remote_project/tracked.txt") == 'watcher revision' ]] && \
     [[ -f $remote_control/processes/active/$handle.revisions ]] && \
     (( $(wc -l < "$remote_control/processes/active/$handle.revisions") >= 2 )); then
    watcher_updated=true
    break
  fi
  sleep 0.1
done
[[ $watcher_updated == true ]] || {
  echo 'managed watcher did not project a local edit into the live runner' >&2
  cat "$binding_dir/watcher.log" >&2 || true
  exit 1
}
current_manifest_id=$(shasum -a 256 "$managed_manifest" | awk '{print $1}')
grep -Fq "$current_manifest_id" \
  "$remote_control/processes/active/$handle.revisions" || {
  echo 'managed process did not record the subsequent sync revision' >&2
  exit 1
}
cp "$remote_control/processes/active/$handle.tsv" "$fixture/process.before-reused-pid"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "start_token" {$2="synthetic-reused-token"} {print}' \
  "$remote_control/processes/active/$handle.tsv" > "$fixture/process.reused-pid"
mv "$fixture/process.reused-pid" "$remote_control/processes/active/$handle.tsv"
set +e
reused_stop_output=$(run_tool stop --for-root "$project" "$handle" 2>&1)
reused_stop_code=$?
set -e
if (( reused_stop_code != 79 )) || [[ $reused_stop_output != *PROCESS_PID_REUSED_OR_SESSION_MISMATCH* ]] || \
   [[ $reused_stop_output != *PROCESS_ROLLBACK_FAILED* ]] || \
   ! kill -0 "$managed_child_pid" 2>/dev/null || ! tcp_port_open "$managed_port"; then
  printf 'reused managed PID identity did not fail closed (status=%s child=%s port=%s): %s\n' \
    "$reused_stop_code" "$(kill -0 "$managed_child_pid" 2>/dev/null && printf live || printf dead)" \
    "$(tcp_port_open "$managed_port" && printf open || printf closed)" "$reused_stop_output" >&2
  exit 1
fi
cp "$fixture/process.before-reused-pid" "$remote_control/processes/active/$handle.tsv"
reused_recovery_output=$(run_tool stop --for-root "$project" "$handle")
[[ $reused_recovery_output == "STOPPED  $handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || kill -0 "$managed_child_pid" 2>/dev/null || \
   tcp_port_open "$managed_port"; then
  echo 'explicit stop did not recover the reused-PID stop failure marker' >&2
  exit 1
fi

rm -f "$managed_child_file" "$managed_port_file"
start_output=$(run_tool_watch start --for-root "$project" -- ./server.sh)
handle=$(sed -n 's/^STARTED  //p' <<<"$start_output")
[[ $handle =~ ^proc- ]]
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'restart seed descendant did not publish its PID/port' >&2; exit 1;
}
managed_child_pid=$(<"$managed_child_file")
managed_port=$(<"$managed_port_file")
kill -0 "$managed_child_pid"
tcp_port_open "$managed_port"

rm -f "$managed_child_file" "$managed_port_file"
restart_output=$(run_tool restart --for-root "$project" "$handle")
[[ $restart_output == "RESTARTED $handle" ]]
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'restarted descendant did not publish its PID/port' >&2; exit 1;
}
restarted_child_pid=$(<"$managed_child_file")
restarted_port=$(<"$managed_port_file")
if kill -0 "$managed_child_pid" 2>/dev/null || tcp_port_open "$managed_port" || \
   [[ $restarted_child_pid == "$managed_child_pid" ]] || ! tcp_port_open "$restarted_port"; then
  echo 'restart did not replace the complete managed session and TCP listener' >&2
  exit 1
fi

foreign_handle=proc-20260821T120000-999-999
sleep 60 &
foreign_pid=$!
printf '%s\n' "$foreign_pid" > "$fixture/foreign.pid"
foreign_token="synthetic-start-$foreign_pid"
cat > "$remote_control/processes/active/$foreign_handle.tsv" <<EOF
schema	2
owner_id	foreign-owner
project_id	demo
profile	example-profile
workspace	demo-runner
root_hash	$(awk -F '\t' '$1 == "root_hash" {print $2; exit}' "$binding_file")
remote_root	$remote_project
handle	$foreign_handle
pid	$foreign_pid
start_token	$foreign_token
session_id	$foreign_pid
process_group_id	$foreign_pid
EOF
set +e
foreign_stop_output=$(run_tool stop --for-root "$project" "$foreign_handle" 2>&1)
foreign_stop_status=$?
set -e
if (( foreign_stop_status != 1 )) || [[ $foreign_stop_output != *PROCESS_BINDING_IDENTITY_MISMATCH* ]] || \
   ! kill -0 "$foreign_pid" 2>/dev/null; then
  printf 'foreign managed process was not protected: %s\n' "$foreign_stop_output" >&2
  exit 1
fi
rm -f "$remote_control/processes/active/$foreign_handle.tsv"
kill "$foreign_pid"
wait "$foreign_pid" 2>/dev/null || true
rm -f "$fixture/foreign.pid"

stop_output=$(run_tool stop --for-root "$project" "$handle")
[[ $stop_output == "STOPPED  $handle" ]]
if kill -0 "$restarted_child_pid" 2>/dev/null || tcp_port_open "$restarted_port"; then
  echo 'stop left a managed descendant or TCP listener alive' >&2
  exit 1
fi

# Atomic recovery identity publication is a hard prerequisite for user argv.
# Repeat the focused failure five times to catch supervisor/session races.
for identity_attempt in $(seq 1 5); do
  rm -f "$managed_child_file" "$managed_port_file"
  identity_setsid_pid_file=$fixture/identity-failure-$identity_attempt.setsid.pid
  set +e
  identity_failure_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_IDENTITY_PUBLISH=true \
    REMOTE_DEV_LOCAL_AGENT_TEST_SETSID_PID_FILE="$identity_setsid_pid_file" \
    "$tool" start --for-root "$project" -- ./server.sh 2>&1)
  identity_failure_code=$?
  set -e
  if (( identity_failure_code == 0 )) || \
     [[ $identity_failure_output != *PROCESS_SESSION_IDENTITY_PUBLISH_FAILED* ]]; then
    printf 'identity publication failure %s was not surfaced: %s\n' \
      "$identity_attempt" "$identity_failure_output" >&2
    exit 1
  fi
  wait_for_file "$identity_setsid_pid_file" || {
    echo "identity failure $identity_attempt omitted setsid lifecycle evidence" >&2; exit 1;
  }
  identity_setsid_pid=$(<"$identity_setsid_pid_file")
  identity_supervisor_stopped=false
  for _ in $(seq 1 100); do
    if ! kill -0 "$identity_setsid_pid" 2>/dev/null && \
       ! "$fake_bin/pgrep" -s "$identity_setsid_pid" >/dev/null 2>&1; then
      identity_supervisor_stopped=true
      break
    fi
    sleep 0.02
  done
  if [[ $identity_supervisor_stopped != true || -e $managed_child_file || -e $managed_port_file || \
        -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || \
     find "$remote_control/processes/active" -name 'proc-*.tsv' -print -quit | grep -q . || \
     find "$remote_control/processes/recovery" -name 'proc-*' -print -quit | grep -q .; then
    printf 'identity failure %s leaked supervisor=%s child_file=%s port_file=%s marker=%s active=%s recovery=%s\n' \
      "$identity_attempt" "$identity_supervisor_stopped" "$([[ -e $managed_child_file ]] && printf yes || printf no)" \
      "$([[ -e $managed_port_file ]] && printf yes || printf no)" \
      "$([[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] && printf yes || printf no)" \
      "$(find "$remote_control/processes/active" -name 'proc-*.tsv' -print -quit)" \
      "$(find "$remote_control/processes/recovery" -name 'proc-*' -print -quit)" >&2
    exit 1
  fi
done

# A launch intent interrupted before setsid is a durable, explicitly
# recoverable phase. Exact stop must prove the unchanged not-started intent,
# archive it, clear the workspace-global gate, and never signal a guessed PID.
rm -f "$managed_child_file" "$managed_port_file"
set +e
pending_intent_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_TEST_INTERRUPT_AFTER_RECOVERY_INTENT=true \
  "$tool" start --for-root "$project" -- ./server.sh 2>&1)
pending_intent_code=$?
set -e
if (( pending_intent_code != 79 )) || \
   [[ $pending_intent_output != *PROCESS_TEST_INTERRUPT_AFTER_RECOVERY_INTENT* ]] || \
   [[ $pending_intent_output != *PROCESS_ROLLBACK_FAILED* ]]; then
  printf 'identity-pending interruption was not preserved: %s\n' "$pending_intent_output" >&2
  exit 1
fi
pending_intent_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
pending_intent_recovery=$remote_control/processes/recovery/$pending_intent_handle
if [[ ! -f $pending_intent_recovery.tsv || ! -f $pending_intent_recovery.argv || \
      ! -f $pending_intent_recovery.revisions || -e $pending_intent_recovery.identity.tsv ]] || \
   find "$remote_control/processes/recovery" -name "$pending_intent_handle.identity.tsv.tmp.*" -print -quit | grep -q . || \
   ! grep -Fq $'recovery_phase\tidentity-pending' "$pending_intent_recovery.tsv" || \
   ! grep -Fq $'supervisor_state\tnot-started' "$pending_intent_recovery.tsv" || \
   [[ -e $managed_child_file || -e $managed_port_file ]]; then
  echo 'identity-pending interruption omitted stable no-launch recovery evidence' >&2
  exit 1
fi
pending_intent_stop=$(run_tool stop --for-root "$project" "$pending_intent_handle")
[[ $pending_intent_stop == "STOPPED  $pending_intent_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || \
   find "$remote_control/processes/recovery" -name "$pending_intent_handle.*" -print -quit | grep -q . || \
   [[ ! -f $remote_control/processes/stopped/$pending_intent_handle.tsv ]] || \
   ! grep -Fq $'recovery_state\tintent-cleared-no-launch' "$remote_control/processes/stopped/$pending_intent_handle.tsv"; then
  echo 'exact stop did not reconcile stable identity-pending launch intent' >&2
  exit 1
fi
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null

# If the launcher dies immediately after fork, the child still has to acquire
# the workspace mutex and atomically promote the exact intent/identity before
# argv. Exact stop then recovers that child-owned transition.
rm -f "$managed_child_file" "$managed_port_file"
set +e
post_fork_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_TEST_INTERRUPT_AFTER_SUPERVISOR_FORK=true \
  "$tool" start --for-root "$project" -- ./server.sh 2>&1)
post_fork_code=$?
set -e
if (( post_fork_code != 79 )) || \
   [[ $post_fork_output != *PROCESS_TEST_INTERRUPT_AFTER_SUPERVISOR_FORK* ]] || \
   [[ $post_fork_output != *PROCESS_ROLLBACK_FAILED* ]]; then
  printf 'post-fork interruption was not preserved: %s\n' "$post_fork_output" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'post-fork interruption did not expose real session/TCP evidence' >&2; exit 1;
}
post_fork_child=$(<"$managed_child_file")
post_fork_port=$(<"$managed_port_file")
post_fork_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
post_fork_recovery=$remote_control/processes/recovery/$post_fork_handle
if [[ ! -f $post_fork_recovery.tsv || ! -f $post_fork_recovery.identity.tsv ]] || \
   ! grep -Fq $'recovery_phase\tactive-transition' "$post_fork_recovery.tsv" || \
   ! grep -Fq $'supervisor_state\tidentity-ready' "$post_fork_recovery.tsv" || \
   ! kill -0 "$post_fork_child" 2>/dev/null || ! tcp_port_open "$post_fork_port"; then
  echo 'post-fork interruption did not retain mutex-promoted journal plus verified identity' >&2
  exit 1
fi
post_fork_stop=$(run_tool stop --for-root "$project" "$post_fork_handle")
[[ $post_fork_stop == "STOPPED  $post_fork_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || \
   find "$remote_control/processes/recovery" -name "$post_fork_handle.*" -print -quit | grep -q . || \
   kill -0 "$post_fork_child" 2>/dev/null || tcp_port_open "$post_fork_port"; then
  echo 'exact stop did not recover post-fork/pre-transition interruption' >&2
  exit 1
fi
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null

for publication_attempt in $(seq 1 5); do
  rm -f "$managed_child_file" "$managed_port_file"
  set +e
  publication_failure_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_PUBLISH=true "$tool" \
    start --for-root "$project" -- ./server.sh 2>&1)
  publication_failure_code=$?
  set -e
  if (( publication_failure_code == 0 )) || [[ $publication_failure_output != *PROCESS_TEST_PUBLICATION_FAILURE* ]]; then
    printf 'synthetic process publication failure %s did not reach cleanup: %s\n' \
      "$publication_attempt" "$publication_failure_output" >&2
    exit 1
  fi
  wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
    echo "publication-failure child $publication_attempt did not expose lifecycle evidence" >&2; exit 1;
  }
  failed_child_pid=$(<"$managed_child_file")
  failed_child_port=$(<"$managed_port_file")
  publication_cleanup_closed=false
  for _ in $(seq 1 100); do
    if ! kill -0 "$failed_child_pid" 2>/dev/null && ! tcp_port_open "$failed_child_port"; then
      publication_cleanup_closed=true
      break
    fi
    sleep 0.02
  done
  publication_active_residue=$(find "$remote_control/processes/active" -name 'proc-*' -print -quit)
  publication_recovery_residue=$(find "$remote_control/processes/recovery" -name 'proc-*' -print -quit)
  if [[ $publication_cleanup_closed != true || -n $publication_active_residue || \
        -n $publication_recovery_residue || -e $binding_dir/PROCESS_ROLLBACK_FAILED ]]; then
    printf 'one-shot publication cleanup %s failed: child=%s port=%s active=%s recovery=%s marker=%s\n' \
      "$publication_attempt" "$(kill -0 "$failed_child_pid" 2>/dev/null && printf live || printf stopped)" \
      "$(tcp_port_open "$failed_child_port" && printf open || printf closed)" \
      "${publication_active_residue:-none}" "${publication_recovery_residue:-none}" \
      "$([[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] && printf yes || printf no)" >&2
    exit 1
  fi
done

# A failed activation rollback must retain both the remote ownership record and
# a local fail-closed marker until an explicit, verified stop succeeds.
rm -f "$managed_child_file" "$managed_port_file"
activation_gate=$fixture/rollback-remote-stop
rollback_stdout=$fixture/rollback-remote-stop.stdout
rollback_stderr=$fixture/rollback-remote-stop.stderr
rollback_status=$fixture/rollback-remote-stop.status
(
  set +e
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_ACTIVATION_GATE="$activation_gate" \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_RESUME_FILE="$activation_gate.fail-resume" \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_SESSION_STOP=true \
    REMOTE_DEV_LOCAL_AGENT_TEST_PROCESS_STOP_ATTEMPTS=2 \
    "$tool" start --for-root "$project" -- ./server.sh \
    > "$rollback_stdout" 2> "$rollback_stderr"
  printf '%s\n' "$?" > "$rollback_status"
) &
activation_start_pid=$!
wait_for_file "$activation_gate.ready" || { echo 'remote-stop rollback gate was not reached' >&2; exit 1; }
: > "$activation_gate.fail-resume"
: > "$activation_gate.release"
wait "$activation_start_pid"
activation_start_pid=
if [[ $(<"$rollback_status") != 79 ]] || ! grep -Fq PROCESS_ROLLBACK_FAILED "$rollback_stderr"; then
  printf 'remote-stop rollback failure was not surfaced: %s\n%s\n' \
    "$(<"$rollback_stdout")" "$(<"$rollback_stderr")" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'remote-stop rollback child did not expose lifecycle evidence' >&2; exit 1;
}
rollback_child_pid=$(<"$managed_child_file")
rollback_child_port=$(<"$managed_port_file")
rollback_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
[[ -f $remote_control/processes/active/$rollback_handle.tsv ]]
kill -0 "$rollback_child_pid"
tcp_port_open "$rollback_child_port"
set +e
rollback_status_output=$(run_tool status --for-root "$project" 2>&1)
rollback_status_code=$?
rollback_exec_output=$(run_tool exec --for-root "$project" -- /usr/bin/true 2>&1)
rollback_exec_code=$?
rollback_down_output=$(run_tool down --for-root "$project" 2>&1)
rollback_down_code=$?
set -e
if (( rollback_status_code != 79 || rollback_exec_code != 79 || rollback_down_code != 79 )) || \
   [[ $rollback_status_output != *'Process rollback PROCESS_ROLLBACK_FAILED'* ]] || \
   [[ $rollback_exec_output != *PROCESS_ROLLBACK_FAILED* ]] || \
   [[ $rollback_down_output != *RUNNER_UNRESOLVED_STATE* ]]; then
  printf 'rollback marker did not block status/sync/down:\n%s\n%s\n%s\n' \
    "$rollback_status_output" "$rollback_exec_output" "$rollback_down_output" >&2
  exit 1
fi
rollback_stop_output=$(run_tool stop --for-root "$project" "$rollback_handle")
[[ $rollback_stop_output == "STOPPED  $rollback_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || kill -0 "$rollback_child_pid" 2>/dev/null || \
   tcp_port_open "$rollback_child_port" || [[ -e $remote_control/processes/active/$rollback_handle.tsv ]]; then
  echo 'explicit stop did not recover remote-stop rollback failure' >&2
  exit 1
fi

# If process termination succeeds but Mutagen pause fails, keep the rollback
# marker and recover through the same idempotent explicit-stop command.
rm -f "$managed_child_file" "$managed_port_file"
activation_gate=$fixture/rollback-mutagen-pause
rollback_stdout=$fixture/rollback-mutagen-pause.stdout
rollback_stderr=$fixture/rollback-mutagen-pause.stderr
rollback_status=$fixture/rollback-mutagen-pause.status
(
  set +e
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_ACTIVATION_GATE="$activation_gate" \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_RESUME_FILE="$activation_gate.fail-resume" \
    REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_MUTAGEN_PAUSE_FILE="$activation_gate.fail-pause" \
    "$tool" start --for-root "$project" -- ./server.sh \
    > "$rollback_stdout" 2> "$rollback_stderr"
  printf '%s\n' "$?" > "$rollback_status"
) &
activation_start_pid=$!
wait_for_file "$activation_gate.ready" || { echo 'Mutagen-pause rollback gate was not reached' >&2; exit 1; }
: > "$activation_gate.fail-resume"
: > "$activation_gate.fail-pause"
: > "$activation_gate.release"
wait "$activation_start_pid"
activation_start_pid=
if [[ $(<"$rollback_status") != 79 ]] || ! grep -Fq 'synchronization could not be paused' "$rollback_stderr"; then
  printf 'Mutagen-pause rollback failure was not surfaced: %s\n%s\n' \
    "$(<"$rollback_stdout")" "$(<"$rollback_stderr")" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'Mutagen-pause rollback child did not expose lifecycle evidence' >&2; exit 1;
}
rollback_child_pid=$(<"$managed_child_file")
rollback_child_port=$(<"$managed_port_file")
rollback_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
if kill -0 "$rollback_child_pid" 2>/dev/null || tcp_port_open "$rollback_child_port" || \
   [[ -e $remote_control/processes/active/$rollback_handle.tsv ]] || \
   [[ ! -f $remote_control/processes/stopped/$rollback_handle.tsv ]] || \
   [[ ! -e $binding_dir/SYNC_FAILED ]]; then
  echo 'Mutagen-pause failure did not preserve verified stopped ownership evidence' >&2
  exit 1
fi
rollback_stop_output=$(run_tool stop --for-root "$project" "$rollback_handle")
[[ $rollback_stop_output == "STOPPED  $rollback_handle" ]]
[[ ! -e $binding_dir/PROCESS_ROLLBACK_FAILED && ! -e $binding_dir/SYNC_FAILED ]]

# If prepublication cleanup cannot terminate the real session, promote the
# launch identity to a complete active record before returning to the caller.
rm -f "$managed_child_file" "$managed_port_file"
set +e
prepublish_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_PUBLISH=true \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_SESSION_STOP=true \
  REMOTE_DEV_LOCAL_AGENT_TEST_PROCESS_STOP_ATTEMPTS=2 \
  "$tool" start --for-root "$project" -- ./server.sh 2>&1)
prepublish_code=$?
set -e
if (( prepublish_code != 79 )) || [[ $prepublish_output != *PROCESS_PREPUBLICATION_ROLLBACK_FAILED* ]] || \
   [[ $prepublish_output != *PROCESS_ROLLBACK_FAILED* ]]; then
  printf 'prepublication terminate failure was not preserved: %s\n' "$prepublish_output" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'prepublication rollback child did not expose lifecycle evidence' >&2; exit 1;
}
prepublish_child_pid=$(<"$managed_child_file")
prepublish_child_port=$(<"$managed_port_file")
prepublish_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
prepublish_record=$remote_control/processes/active/$prepublish_handle.tsv
if [[ ! -f $prepublish_record ]] || ! grep -Fq $'activation_state\tprepublication-rollback-failed' "$prepublish_record" || \
   [[ ! -f $remote_control/processes/active/$prepublish_handle.argv ]] || \
   [[ ! -f $remote_control/processes/active/$prepublish_handle.revisions ]] || \
   ! kill -0 "$prepublish_child_pid" 2>/dev/null || ! tcp_port_open "$prepublish_child_port"; then
  echo 'prepublication terminate failure omitted recoverable active ownership or live TCP evidence' >&2
  exit 1
fi
prepublish_stop_output=$(run_tool stop --for-root "$project" "$prepublish_handle")
[[ $prepublish_stop_output == "STOPPED  $prepublish_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || kill -0 "$prepublish_child_pid" 2>/dev/null || \
   tcp_port_open "$prepublish_child_port" || [[ -e $remote_control/processes/active/$prepublish_handle.tsv ]]; then
  echo 'explicit stop did not recover prepublication termination failure' >&2
  exit 1
fi

# Active publication precedes recovery deletion. If that final deletion fails,
# both records remain a gated duplicate until exact stop validates their shared
# identity, stops the real session, archives active ownership, then removes the
# recovery journal.
rm -f "$managed_child_file" "$managed_port_file"
set +e
recovery_delete_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_RECOVERY_DELETE=true \
  "$tool" start --for-root "$project" -- ./server.sh 2>&1)
recovery_delete_code=$?
set -e
if (( recovery_delete_code != 79 )) || \
   [[ $recovery_delete_output != *PROCESS_TEST_RECOVERY_DELETE_FAILURE* ]] || \
   [[ $recovery_delete_output != *PROCESS_ROLLBACK_FAILED* ]]; then
  printf 'active/recovery duplicate failure was not preserved: %s\n' "$recovery_delete_output" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'active/recovery duplicate did not expose live TCP evidence' >&2; exit 1;
}
recovery_delete_child=$(<"$managed_child_file")
recovery_delete_port=$(<"$managed_port_file")
recovery_delete_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
recovery_delete_active=$remote_control/processes/active/$recovery_delete_handle.tsv
recovery_delete_recovery=$remote_control/processes/recovery/$recovery_delete_handle
if [[ ! -f $recovery_delete_active || ! -f $recovery_delete_recovery.tsv || \
      ! -f $recovery_delete_recovery.identity.tsv || ! -f $recovery_delete_recovery.argv || \
      ! -f $recovery_delete_recovery.revisions ]] || \
   ! grep -Fq $'recovery_phase\tactive-transition' "$recovery_delete_recovery.tsv" || \
   ! grep -Fq $'supervisor_state\tidentity-ready' "$recovery_delete_recovery.tsv" || \
   [[ $(awk -F '\t' '$1 == "pid" {print $2; exit}' "$recovery_delete_active") != \
      $(awk -F '\t' '$1 == "pid" {print $2; exit}' "$recovery_delete_recovery.identity.tsv") ]] || \
   ! kill -0 "$recovery_delete_child" 2>/dev/null || ! tcp_port_open "$recovery_delete_port"; then
  echo 'recovery-delete failure omitted matching active/recovery ownership or live TCP evidence' >&2
  exit 1
fi
set +e
recovery_delete_status=$(run_tool status --for-root "$project" 2>&1)
recovery_delete_status_code=$?
set -e
if (( recovery_delete_status_code != 79 )) || \
   [[ $recovery_delete_status != *$'recovery_process\t'"$recovery_delete_handle"$'\tlive'* ]] || \
   [[ $recovery_delete_status != *'Health         blocked'* ]]; then
  printf 'active/recovery duplicate did not gate status: %s\n' "$recovery_delete_status" >&2
  exit 1
fi
recovery_delete_stop=$(run_tool stop --for-root "$project" "$recovery_delete_handle")
[[ $recovery_delete_stop == "STOPPED  $recovery_delete_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || \
   [[ -e $recovery_delete_active ]] || \
   find "$remote_control/processes/recovery" -name "$recovery_delete_handle.*" -print -quit | grep -q . || \
   [[ ! -f $remote_control/processes/stopped/$recovery_delete_handle.tsv ]] || \
   kill -0 "$recovery_delete_child" 2>/dev/null || tcp_port_open "$recovery_delete_port"; then
  echo 'exact stop did not reconcile active/recovery duplicate state' >&2
  exit 1
fi
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
recovery_delete_ready=$(run_tool status --for-root "$project")
[[ $recovery_delete_ready == *'Health         ready'* ]] || {
  printf 'workspace did not become ready after recovery-delete reconciliation: %s\n' "$recovery_delete_ready" >&2
  exit 1
}

# Persistent active-record publication failure must still leave deterministic
# full binding ownership and session identity that explicit stop can recover.
rm -f "$managed_child_file" "$managed_port_file"
set +e
persistent_publish_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_PROCESS_PUBLISH_PERSISTENT=true \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_SESSION_STOP=true \
  REMOTE_DEV_LOCAL_AGENT_TEST_PROCESS_STOP_ATTEMPTS=2 \
  "$tool" start --for-root "$project" -- ./server.sh 2>&1)
persistent_publish_code=$?
set -e
if (( persistent_publish_code != 79 )) || \
   [[ $persistent_publish_output != *PROCESS_TEST_PERSISTENT_PUBLICATION_FAILURE* ]] || \
   [[ $persistent_publish_output != *'recovery ownership remains'* ]] || \
   [[ $persistent_publish_output != *PROCESS_ROLLBACK_FAILED* ]]; then
  printf 'persistent publication/terminate failure was not preserved: %s\n' "$persistent_publish_output" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'persistent publication rollback child did not expose lifecycle evidence' >&2; exit 1;
}
persistent_child_pid=$(<"$managed_child_file")
persistent_child_port=$(<"$managed_port_file")
persistent_handle=$(awk -F '\t' '$1 == "handle" {print $2; exit}' "$binding_dir/PROCESS_ROLLBACK_FAILED")
persistent_recovery=$remote_control/processes/recovery/$persistent_handle
if [[ -e $remote_control/processes/active/$persistent_handle.tsv ]] || \
   [[ ! -f $persistent_recovery.tsv || ! -f $persistent_recovery.identity.tsv || \
      ! -f $persistent_recovery.argv || ! -f $persistent_recovery.revisions ]] || \
   ! grep -Fq $'activation_state\tlaunch-intent' "$persistent_recovery.tsv" || \
   ! grep -Fq $'owner_id\t' "$persistent_recovery.tsv" || \
   ! grep -Fq $'session_id\t' "$persistent_recovery.identity.tsv" || \
   ! kill -0 "$persistent_child_pid" 2>/dev/null || ! tcp_port_open "$persistent_child_port"; then
  echo 'persistent publication failure omitted recovery ownership or live TCP evidence' >&2
  exit 1
fi
# The remote journal is authoritative even if local marker state is lost.
rm -f "$binding_dir/PROCESS_ROLLBACK_FAILED"
set +e
persistent_status_output=$(run_tool status --for-root "$project" 2>&1)
persistent_status_code=$?
persistent_exec_output=$(run_tool exec --for-root "$project" -- /usr/bin/true 2>&1)
persistent_exec_code=$?
persistent_down_output=$(run_tool down --for-root "$project" 2>&1)
persistent_down_code=$?
set -e
if (( persistent_status_code != 79 || persistent_exec_code != 79 || persistent_down_code != 79 )) || \
   [[ $persistent_status_output != *$'recovery_process\t'"$persistent_handle"$'\tlive'* ]] || \
   [[ $persistent_status_output != *'Process rollback clean'* ]] || \
   [[ $persistent_exec_output != *PROCESS_RECOVERY_GATE* ]] || \
   [[ $persistent_down_output != *PROCESS_RECOVERY_GATE* ]]; then
  printf 'persistent recovery did not block status/sync/down:\n%s\n%s\n%s\n' \
    "$persistent_status_output" "$persistent_exec_output" "$persistent_down_output" >&2
  exit 1
fi
recovery_takeover_project=$fixture/recovery-takeover-project
recovery_takeover_state=$fixture/recovery-takeover-state
mkdir -p "$recovery_takeover_project"
git -C "$recovery_takeover_project" init -q
git -C "$recovery_takeover_project" config user.name 'Remote Dev Test'
git -C "$recovery_takeover_project" config user.email remote-dev@example.test
printf 'recovery takeover probe\n' > "$recovery_takeover_project/source.txt"
git -C "$recovery_takeover_project" add source.txt
git -C "$recovery_takeover_project" commit -qm initial
set +e
recovery_takeover_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$recovery_takeover_state" \
  "$tool" --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id recovery-takeover --local-project "$recovery_takeover_project" \
  --take-over-stale 2>&1)
recovery_takeover_code=$?
set -e
if (( recovery_takeover_code != 79 )) || [[ $recovery_takeover_output != *PROCESS_RECOVERY_GATE* ]] || \
   [[ $recovery_takeover_output != *"handle=$persistent_handle"* ]]; then
  printf 'fresh binding/takeover bypassed workspace recovery gate: %s\n' "$recovery_takeover_output" >&2
  exit 1
fi
persistent_stop_output=$(run_tool stop --for-root "$project" "$persistent_handle")
[[ $persistent_stop_output == "STOPPED  $persistent_handle" ]]
if [[ -e $binding_dir/PROCESS_ROLLBACK_FAILED ]] || \
   find "$remote_control/processes/recovery" -name "$persistent_handle.*" -print -quit | grep -q . || \
   [[ ! -f $remote_control/processes/stopped/$persistent_handle.tsv ]] || \
   ! grep -Fq $'recovery_state\tstopped' "$remote_control/processes/stopped/$persistent_handle.tsv" || \
   kill -0 "$persistent_child_pid" 2>/dev/null || tcp_port_open "$persistent_child_port"; then
  echo 'explicit stop did not recover persistent publication ownership' >&2
  exit 1
fi
run_tool_watch --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null

sleep 60 &
execution_pid=$!
printf '%s\n' "$execution_pid" > "$fixture/foreign.pid"
execution_token="synthetic-start-$execution_pid"
owner_id=$(awk -F '\t' '$1 == "owner_id" {print $2; exit}' "$binding_file")
cat > "$remote_control/executions/synthetic-running.running" <<EOF
schema	2
pid	$execution_pid
start_token	$execution_token
owner_id	$owner_id
project_id	demo
profile	example-profile
workspace	demo-runner
root_hash	$(awk -F '\t' '$1 == "root_hash" {print $2; exit}' "$binding_file")
remote_root	$remote_project
EOF

# Freeze the owning heartbeat before making its lease stale. The live execution
# remains independent, while the watcher exits through its fail-closed path.
stale_watcher_pid=$(<"$binding_dir/watcher.pid")
printf 'UNKNOWN_EXECUTION\nreceipt_id\tstale-takeover-heartbeat-freeze\n' > "$binding_dir/UNKNOWN_EXECUTION"
watcher_stopped=false
watcher_stop_deadline=$((SECONDS + 10))
while (( SECONDS < watcher_stop_deadline )); do
  if ! kill -0 "$stale_watcher_pid" 2>/dev/null && [[ ! -e $binding_dir/operation.lock ]]; then
    watcher_stopped=true
    break
  fi
  sleep 0.05
done
if [[ $watcher_stopped != true ]]; then
  stop_test_pid "$stale_watcher_pid"
  printf 'owning watcher did not stop before stale takeover check\n' >&2
  exit 1
fi
rm -f "$binding_dir/watcher.pid" "$binding_dir/watcher.identity.tsv" "$binding_dir/UNKNOWN_EXECUTION"

cp "$workspace_control/lease.tsv" "$fixture/workspace-lease.live-exec"
cp "$remote_control/lease.tsv" "$fixture/project-lease.live-exec"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "heartbeat" {$2="1"} {print}' \
  "$workspace_control/lease.tsv" > "$fixture/workspace-lease.stale"
awk -F '\t' 'BEGIN {OFS="\t"} $1 == "heartbeat" {$2="1"} {print}' \
  "$remote_control/lease.tsv" > "$fixture/project-lease.stale"
mv "$fixture/workspace-lease.stale" "$workspace_control/lease.tsv"
mv "$fixture/project-lease.stale" "$remote_control/lease.tsv"
set +e
stale_takeover_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$second_state_root" \
  "$tool" --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --take-over-stale 2>&1)
stale_takeover_code=$?
set -e
if (( stale_takeover_code != 1 )) || [[ $stale_takeover_output != *STALE_LEASE_LIVE_EXECUTION* ]]; then
  printf 'stale takeover ignored a live remote execution: %s\n' "$stale_takeover_output" >&2
  exit 1
fi
[[ $(awk -F '\t' '$1 == "owner_id" {print $2; exit}' "$workspace_control/lease.tsv") == "$owner_id" ]]
cp "$fixture/workspace-lease.live-exec" "$workspace_control/lease.tsv"
cp "$fixture/project-lease.live-exec" "$remote_control/lease.tsv"
set +e
running_status_output=$(run_tool status --for-root "$project" 2>&1)
running_status_code=$?
set -e
if (( running_status_code != 79 )) || [[ $running_status_output != *$'remote_execution\tsynthetic-running\tlive'* ]] || \
   [[ $running_status_output != *$'workspace_claim\towned'* ]] || \
   [[ $running_status_output != *$'owner_id\t'* ]] || \
   [[ $running_status_output != *'Health         blocked'* ]]; then
  printf 'status did not expose the unresolved remote execution: %s\n' "$running_status_output" >&2
  exit 1
fi
set +e
running_down_output=$(run_tool down --for-root "$project" 2>&1)
running_down_code=$?
set -e
if (( running_down_code != 79 )) || [[ $running_down_output != *UNRESOLVED_REMOTE_EXECUTION* ]] || \
   ! kill -0 "$execution_pid" 2>/dev/null || [[ ! -f $workspace_binding ]]; then
  printf 'down did not preserve an unresolved remote execution and global claim: %s\n' "$running_down_output" >&2
  exit 1
fi
printf 'UNKNOWN_EXECUTION\nreceipt_id\tsynthetic-running\n' > "$binding_dir/UNKNOWN_EXECUTION"
set +e
unknown_status_output=$(run_tool status --for-root "$project" 2>&1)
unknown_status_code=$?
set -e
if (( unknown_status_code != 79 )) || [[ $unknown_status_output != *'Unknown exec   UNKNOWN_EXECUTION'* ]]; then
  printf 'status did not expose local unknown-execution evidence: %s\n' "$unknown_status_output" >&2
  exit 1
fi
kill "$execution_pid"
wait "$execution_pid" 2>/dev/null || true
rm -f "$fixture/foreign.pid"
run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --recover-unknown >/dev/null
[[ ! -e $binding_dir/UNKNOWN_EXECUTION ]]
[[ -f $remote_control/executions/recovered/synthetic-running.running ]]

rm -f "$managed_child_file" "$managed_port_file"
down_start_output=$(run_tool_watch start --for-root "$project" -- ./server.sh)
down_handle=$(sed -n 's/^STARTED  //p' <<<"$down_start_output")
[[ $down_handle =~ ^proc- ]]
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'down-test descendant did not publish its PID/port' >&2; exit 1;
}
down_child_pid=$(<"$managed_child_file")
down_child_port=$(<"$managed_port_file")
kill -0 "$down_child_pid"
tcp_port_open "$down_child_port"

sed 's/managed-child/managed-child-two/g; s/managed-port/managed-port-two/g' \
  "$project/server.sh" > "$project/server-two.sh"
chmod 0755 "$project/server-two.sh"
git -C "$project" add server-two.sh
second_child_file=$remote_project/node_modules/managed-child-two.pid
second_port_file=$remote_project/node_modules/managed-port-two
rm -f "$second_child_file" "$second_port_file"
activation_gate=$fixture/start-vs-down
activation_start_stdout=$fixture/start-vs-down.stdout
activation_start_stderr=$fixture/start-vs-down.stderr
activation_start_status=$fixture/start-vs-down.status
(
  set +e
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_ACTIVATION_GATE="$activation_gate" "$tool" \
    start --for-root "$project" -- ./server-two.sh \
    > "$activation_start_stdout" 2> "$activation_start_stderr"
  printf '%s\n' "$?" > "$activation_start_status"
) &
activation_start_pid=$!
wait_for_file "$activation_gate.ready" || { echo 'start activation gate was not reached' >&2; exit 1; }
activation_down_stdout=$fixture/start-vs-down.down.stdout
activation_down_stderr=$fixture/start-vs-down.down.stderr
activation_down_status=$fixture/start-vs-down.down.status
(
  set +e
  run_tool down --for-root "$project" > "$activation_down_stdout" 2> "$activation_down_stderr"
  printf '%s\n' "$?" > "$activation_down_status"
) &
activation_down_pid=$!
sleep 0.2
kill -0 "$activation_down_pid" 2>/dev/null || {
  echo 'down did not wait behind the complete start activation transaction' >&2
  exit 1
}
: > "$activation_gate.release"
wait "$activation_start_pid"
activation_start_pid=
wait "$activation_down_pid"
activation_down_pid=
if [[ $(<"$activation_start_status") != 0 || $(<"$activation_down_status") != 0 ]] || \
   ! grep -Fq 'STARTED  proc-' "$activation_start_stdout" || \
   ! grep -Fq 'bridge-owned synchronization and managed processes stopped' "$activation_down_stdout"; then
  printf 'start-vs-down did not serialize successfully:\n%s\n%s\n%s\n%s\n' \
    "$(<"$activation_start_stdout")" "$(<"$activation_start_stderr")" \
    "$(<"$activation_down_stdout")" "$(<"$activation_down_stderr")" >&2
  exit 1
fi
wait_for_file "$second_child_file" && wait_for_file "$second_port_file" || {
  echo 'serialized start did not publish child lifecycle evidence before down' >&2; exit 1;
}
second_child_pid=$(<"$second_child_file")
second_child_port=$(<"$second_port_file")
if kill -0 "$down_child_pid" 2>/dev/null || tcp_port_open "$down_child_port" || \
   kill -0 "$second_child_pid" 2>/dev/null || tcp_port_open "$second_child_port" || \
   find "$remote_control/processes/active" -name '*.tsv' -print -quit | grep -q .; then
  echo 'start-vs-down left a managed descendant, listener, or active record' >&2
  exit 1
fi
[[ -d $remote_project && -d $project && -f $binding_file ]]
[[ ! -e $workspace_control/binding.tsv && ! -e $workspace_control/lease.tsv ]]

run_tool --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
rm -f "$managed_child_file" "$managed_port_file"
restart_seed_output=$(run_tool_watch start --for-root "$project" -- ./server.sh)
restart_handle=$(sed -n 's/^STARTED  //p' <<<"$restart_seed_output")
[[ $restart_handle =~ ^proc- ]]
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'restart-vs-down seed did not publish lifecycle evidence' >&2; exit 1;
}
restart_old_pid=$(<"$managed_child_file")
restart_old_port=$(<"$managed_port_file")
activation_gate=$fixture/restart-vs-down
activation_restart_stdout=$fixture/restart-vs-down.stdout
activation_restart_stderr=$fixture/restart-vs-down.stderr
activation_restart_status=$fixture/restart-vs-down.status
(
  set +e
  env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
    REMOTE_DEV_LOCAL_AGENT_TEST_ACTIVATION_GATE="$activation_gate" "$tool" \
    restart --for-root "$project" "$restart_handle" \
    > "$activation_restart_stdout" 2> "$activation_restart_stderr"
  printf '%s\n' "$?" > "$activation_restart_status"
) &
activation_restart_pid=$!
wait_for_file "$activation_gate.ready" || { echo 'restart activation gate was not reached' >&2; exit 1; }
rm -f "$managed_child_file" "$managed_port_file"
activation_down_stdout=$fixture/restart-vs-down.down.stdout
activation_down_stderr=$fixture/restart-vs-down.down.stderr
activation_down_status=$fixture/restart-vs-down.down.status
(
  set +e
  run_tool down --for-root "$project" > "$activation_down_stdout" 2> "$activation_down_stderr"
  printf '%s\n' "$?" > "$activation_down_status"
) &
activation_down_pid=$!
sleep 0.2
kill -0 "$activation_down_pid" 2>/dev/null || {
  echo 'down did not wait behind the complete restart activation transaction' >&2
  exit 1
}
: > "$activation_gate.release"
wait "$activation_restart_pid"
activation_restart_pid=
wait "$activation_down_pid"
activation_down_pid=
if [[ $(<"$activation_restart_status") != 0 || $(<"$activation_down_status") != 0 ]] || \
   ! grep -Fq "RESTARTED $restart_handle" "$activation_restart_stdout" || \
   ! grep -Fq 'bridge-owned synchronization and managed processes stopped' "$activation_down_stdout"; then
  printf 'restart-vs-down did not serialize successfully:\n%s\n%s\n%s\n%s\n' \
    "$(<"$activation_restart_stdout")" "$(<"$activation_restart_stderr")" \
    "$(<"$activation_down_stdout")" "$(<"$activation_down_stderr")" >&2
  exit 1
fi
wait_for_file "$managed_child_file" && wait_for_file "$managed_port_file" || {
  echo 'serialized restart did not publish lifecycle evidence before down' >&2; exit 1;
}
restart_new_pid=$(<"$managed_child_file")
restart_new_port=$(<"$managed_port_file")
if kill -0 "$restart_old_pid" 2>/dev/null || tcp_port_open "$restart_old_port" || \
   kill -0 "$restart_new_pid" 2>/dev/null || tcp_port_open "$restart_new_port" || \
   find "$remote_control/processes/active" -name '*.tsv' -print -quit | grep -q .; then
  echo 'restart-vs-down left an old/new descendant, listener, or active record' >&2
  exit 1
fi
[[ ! -e $workspace_control/binding.tsv && ! -e $workspace_control/lease.tsv ]]
inactive_status_output=$(run_tool status --for-root "$project")
[[ $inactive_status_output == *$'workspace_claim\treleased'* ]]
[[ $inactive_status_output == *$'lease\tmissing'* ]]
[[ $inactive_status_output == *'Health         inactive'* ]]

env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_WATCHER_LAUNCH_MODE=daemon "$tool" \
  --profile example-profile --coder-url https://coder.example.test \
  up demo-runner --project-id demo --local-project "$project" --allow-untracked src >/dev/null
daemon_watcher_pid=$(<"$binding_dir/watcher.pid")
sleep 0.2
if ! kill -0 "$daemon_watcher_pid" 2>/dev/null; then
  printf 'durable watcher did not survive completion of up: pid=%s\n' \
    "$daemon_watcher_pid" >&2
  exit 1
fi
env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_DISABLE_WATCHER=false \
  REMOTE_DEV_LOCAL_AGENT_WATCHER_LAUNCH_MODE=daemon "$tool" \
  down --for-root "$project" >/dev/null

# A multi-repo working set is one Local Agent binding and one remote projection,
# while every member remains an independent local Git authority.
working_anchor=$fixture/working-set
mkdir -p "$working_anchor"
working_anchor=$(cd "$working_anchor" && pwd -P)
working_wrapper=$working_anchor/wrapper.git
working_app=$working_anchor/app.git
working_api=$working_anchor/api.git
mkdir -p "$working_wrapper/.remote-dev" "$working_app/src/(frontend)" "$working_api/src"
for repository in "$working_wrapper" "$working_app" "$working_api"; do
  git -C "$repository" init -q
  git -C "$repository" config user.name remote-dev-smoke
  git -C "$repository" config user.email remote-dev-smoke@example.invalid
done
printf 'services: {}\n' > "$working_wrapper/compose.yaml"
printf 'app tracked\n' > "$working_app/src/app.txt"
printf 'route tracked\n' > "$working_app/src/(frontend)/page [draft].tsx"
printf 'api tracked\n' > "$working_api/src/api.txt"
printf '.env\nnode_modules/\n' > "$working_app/.gitignore"
git -C "$working_app" add .gitignore src/app.txt 'src/(frontend)/page [draft].tsx'
git -C "$working_app" commit -qm initial
git -C "$working_api" add src/api.txt
git -C "$working_api" commit -qm initial
working_manifest=$working_wrapper/.remote-dev/local-agent-working-set.tsv
printf '%s\n' \
  $'schema\t1' \
  $'anchor\t../..' \
  $'execution_member\twrapper' \
  $'member\twrapper\twrapper.git' \
  $'member\tapp\tapp.git' \
  $'member\tapi\tapi.git' \
  $'allow_untracked\tapp\tsrc/generated.txt' \
  $'remote_writable\tapp\t.env' \
  > "$working_manifest"
git -C "$working_wrapper" add compose.yaml .remote-dev/local-agent-working-set.tsv
git -C "$working_wrapper" commit -qm initial
printf 'dirty generated\n' > "$working_app/src/generated.txt"
working_secret='WORKING_SET_SECRET_must_stay_local_7261'
printf '%s\n' "$working_secret" > "$working_app/.env"
working_coder_origin=$(REMOTE_DEV_PROFILE=example-profile /bin/bash -c \
  '. "$1/deploy/host-profiles/load.sh"; remote_dev_load_profile "$1" "$REMOTE_DEV_PROFILE"; printf %s "$REMOTE_DEV_CODER_ACCESS_URL"' \
  _ "$repo_root")

partial_state=$fixture/partial-claim-state
set +e
partial_output=$(env "${common_env[@]}" REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$partial_state" \
  REMOTE_DEV_LOCAL_AGENT_TEST_FAIL_CLAIM_AFTER=2 "$tool" \
  --profile example-profile --coder-url https://coder.example.test \
  up partial-runner --project-id partial --working-set "$working_manifest" 2>&1)
partial_status=$?
set -e
if (( partial_status != 97 )) || [[ $partial_output != *LOCAL_BINDING_CLAIM_TEST_FAILURE* ]] || \
   find "$partial_state/bindings" -name '*.claim' -print -quit 2>/dev/null | grep -q .; then
  printf 'partial working-set claims were not rolled back atomically: %s\n' "$partial_output" >&2
  exit 1
fi

working_up=$(run_tool --profile example-profile --coder-url "$working_coder_origin" \
  up multi-runner --project-id multi --working-set "$working_manifest")
[[ $working_up == *'Binding kind   working-set'* ]]
[[ $working_up == *'Member         wrapper'* ]]
working_hash=$(printf '%s' "$working_anchor" | shasum -a 256 | awk '{print $1}')
working_binding=$state_root/bindings/by-root/$working_hash
[[ $(awk -F '\t' '$1 == "binding_kind" {print $2; exit}' "$working_binding/binding.tsv") == working-set ]]
[[ $(find "$state_root/bindings/by-context" -name '*.claim' -type f -exec grep -Flx "$working_binding" {} + | wc -l | tr -d ' ') == 4 ]]
assert_working_projection_clean() {
  local checkpoint=$1
  if [[ -e $working_binding/REMOTE_DRIFT ]]; then
    printf 'working-set projection drifted at %s:\n' "$checkpoint" >&2
    cat "$working_binding/REMOTE_DRIFT" >&2
    exit 1
  fi
}
assert_working_projection_clean up
remote_working=$remote_home/projects/multi
[[ $(<"$remote_working/wrapper.git/compose.yaml") == 'services: {}' ]]
[[ $(<"$remote_working/app.git/src/app.txt") == 'app tracked' ]]
[[ $(<"$remote_working/app.git/src/(frontend)/page [draft].tsx") == 'route tracked' ]]
[[ $(<"$remote_working/app.git/src/generated.txt") == 'dirty generated' ]]
[[ $(<"$remote_working/api.git/src/api.txt") == 'api tracked' ]]
[[ ! -e $remote_working/app.git/.env ]]

working_topology=$(awk -F '\t' '$1 == "topology_digest" {print $2; exit}' "$working_binding/binding.tsv")
working_secret_manifest=$fixture/working-set-secrets.tsv
chmod 0600 "$working_app/.env"
printf 'manifest_version\t2\nproject_id\tmulti\nworkspace\tmulti-runner\nworkspace_role\trunner\nlocal_root\t%s\nremote_root\t/home/coder/projects/multi\ntopology_digest\t%s\nfile\tapp.git/.env\tapp.git/.env\n' \
  "$working_anchor" "$working_topology" > "$working_secret_manifest"
mkdir -p "$remote_home/.local/share/remote-dev"
printf 'runner\n' > "$remote_home/.local/share/remote-dev/workspace-role"
env "${common_env[@]}" REMOTE_DEV_PROFILE=example-profile \
  "$repo_root/bin/remote-dev-secrets" plan "$working_secret_manifest" >/dev/null
env "${common_env[@]}" REMOTE_DEV_PROFILE=example-profile \
  "$repo_root/bin/remote-dev-secrets" apply "$working_secret_manifest" \
  --confirm multi-runner --confirm-profile example-profile --confirm-origin "$working_coder_origin" >/dev/null
env "${common_env[@]}" REMOTE_DEV_PROFILE=example-profile \
  "$repo_root/bin/remote-dev-secrets" verify "$working_secret_manifest" >/dev/null
[[ $(<"$remote_working/app.git/.env") == "$working_secret" ]]
[[ $(/usr/bin/stat -f '%Lp' "$remote_working/app.git/.env") == 600 ]]
assert_working_projection_clean secrets

working_pwd=$(run_tool exec --for-context "$working_app/src" -- /bin/pwd)
[[ $working_pwd == "$remote_working/app.git/src" ]]
assert_working_projection_clean exec

set +e
conflict_output=$(run_tool --profile example-profile --coder-url https://coder.example.test \
  up another-runner --project-id conflict --local-project "$working_app" 2>&1)
conflict_status=$?
set -e
if (( conflict_status == 0 )) || [[ $conflict_output != *LOCAL_CONTEXT_ALREADY_BOUND* ]]; then
  printf 'a working-set member was accepted by a second binding: %s\n' "$conflict_output" >&2
  exit 1
fi

process_cwd_file=$remote_working/app.git/node_modules/process-cwd
working_start=$(run_tool start --for-context "$working_app/src" -- \
  /bin/sh -c 'mkdir -p ../node_modules; pwd > ../node_modules/process-cwd; sleep 60')
working_handle=$(sed -n 's/^STARTED  //p' <<<"$working_start")
[[ $working_handle =~ ^proc- ]]
wait_for_file "$process_cwd_file" || { echo 'working-set process did not publish cwd' >&2; exit 1; }
[[ $(<"$process_cwd_file") == "$remote_working/app.git/src" ]]
assert_working_projection_clean start
rm -f "$process_cwd_file"
run_tool restart --for-context "$working_wrapper" "$working_handle" >/dev/null
wait_for_file "$process_cwd_file" || { echo 'working-set restart did not reuse stored cwd' >&2; exit 1; }
[[ $(<"$process_cwd_file") == "$remote_working/app.git/src" ]]
assert_working_projection_clean restart
run_tool stop --for-context "$working_wrapper" "$working_handle" >/dev/null
assert_working_projection_clean stop

# Dependency managers can create runtime trees below any workspace member and
# package after the local projection snapshot was built. These paths remain
# remote-owned and must neither drift nor be deleted by a later barrier.
run_tool exec --for-context "$working_app" -- /bin/sh -c \
  'mkdir -p packages/classified-lab/node_modules/.bin; printf runtime > packages/classified-lab/node_modules/state; ln -s ../tool packages/classified-lab/node_modules/.bin/tool' \
  >/dev/null
[[ $(<"$remote_working/app.git/packages/classified-lab/node_modules/state") == runtime ]]
[[ -L $remote_working/app.git/packages/classified-lab/node_modules/.bin/tool ]]
assert_working_projection_clean nested-runtime
run_tool exec --for-context "$working_app" -- /usr/bin/true >/dev/null
[[ $(<"$remote_working/app.git/packages/classified-lab/node_modules/state") == runtime ]]
[[ -L $remote_working/app.git/packages/classified-lab/node_modules/.bin/tool ]]
[[ ! -e $working_binding/REMOTE_DRIFT ]]
assert_nested_worktree_refused "$working_app"

printf '%s\n' $'remote_writable\tapi\t.env' >> "$working_manifest"
set +e
working_status_output=$(run_tool status --for-context "$working_anchor" 2>&1)
working_status_code=$?
working_exec_output=$(run_tool exec --for-context "$working_app" -- /bin/true 2>&1)
working_exec_code=$?
set -e
if (( working_status_code != 79 )) || [[ $working_status_output != *'Topology       WORKING_SET_TOPOLOGY_DRIFT'* ]] || \
   (( working_exec_code != 79 )) || [[ $working_exec_output != WORKING_SET_TOPOLOGY_DRIFT:* ]]; then
  printf 'working-set topology drift did not block status/exec correctly:\n%s\n%s\n' \
    "$working_status_output" "$working_exec_output" >&2
  exit 1
fi

retirement_outside=$fixture/retirement-outside
mkdir -p "$retirement_outside"
cp -pR "$remote_working/app.git/src/." "$retirement_outside/"
printf 'outside sentinel\n' > "$retirement_outside/sentinel.txt"
rm -rf "$remote_working/app.git/src"
ln -s "$retirement_outside" "$remote_working/app.git/src"
set +e
unsafe_retirement_output=$(run_tool down --for-context "$working_anchor" 2>&1)
unsafe_retirement_status=$?
set -e
if (( unsafe_retirement_status == 0 )) || \
   [[ $unsafe_retirement_output != *MANAGED_UNLINK_REFUSED* ]]; then
  printf 'topology retirement followed an intermediate symlink: %s\n' \
    "$unsafe_retirement_output" >&2
  exit 1
fi
if [[ ! -f $retirement_outside/app.txt || ! -f $retirement_outside/generated.txt || \
      $(<"$retirement_outside/sentinel.txt") != 'outside sentinel' ]]; then
  echo 'refused topology retirement changed a file outside the runner root' >&2
  exit 1
fi
rm "$remote_working/app.git/src"
mkdir -p "$remote_working/app.git/src"
find "$retirement_outside" -mindepth 1 -maxdepth 1 ! -name sentinel.txt \
  -exec cp -pR {} "$remote_working/app.git/src/" \;
working_down=$(run_tool down --for-context "$working_anchor" 2>/dev/null)
[[ $working_down == *'Retired       '* ]]
[[ ! -e $working_binding ]]
if [[ -e $remote_working/wrapper.git/compose.yaml || \
      -e $remote_working/wrapper.git/.remote-dev/local-agent-working-set.tsv || \
      $(<"$remote_working/app.git/.env") != "$working_secret" ]]; then
  echo 'topology retirement did not clear only verified managed source while preserving remote-writable data' >&2
  exit 1
fi
if find "$state_root/bindings/by-context" -name '*.claim' -type f -exec grep -Flx "$working_binding" {} + | grep -q .; then
  echo 'retired working-set binding left a member claim' >&2
  exit 1
fi
set +e
working_reup_output=$(run_tool --profile example-profile --coder-url "$working_coder_origin" \
  up multi-runner --project-id multi --working-set "$working_manifest" 2>&1)
working_reup_status=$?
set -e
if (( working_reup_status != 0 )); then
  printf 'changed working-set topology did not rebind cleanly: %s\n' "$working_reup_output" >&2
  [[ ! -e $working_binding/REMOTE_DRIFT ]] || cat "$working_binding/REMOTE_DRIFT" >&2
  exit 1
fi
run_tool down --for-context "$working_anchor" >/dev/null

[[ ! -e $remote_syntax_marker ]] || {
  echo 'a remote Local Agent command emitted a shell syntax/EOF error' >&2
  exit 1
}

if grep -R -Fq "$secret_value" "$state_root" "$mutagen_state" "$command_log"; then
  echo 'ignored secret leaked into projection state, Mutagen state, logs, or receipts' >&2
  exit 1
fi
if grep -R -Fq "$working_secret" "$state_root" "$mutagen_state" "$command_log"; then
  echo 'working-set ignored secret leaked into projection state, Mutagen state, logs, or receipts' >&2
  exit 1
fi

printf 'PASS     Local Agent projects dirty source into an isolated remote runner safely\n'
