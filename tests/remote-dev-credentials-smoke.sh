#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
fixture=$(mktemp -d)
fake_bin="$fixture/bin"
command_log="$fixture/commands.log"
remote_root="$fixture/remote"
real_python=$(command -v python3)
test_receipt=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export REMOTE_DEV_TEST_RECEIPT=$test_receipt

cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT
mkdir -p "$fake_bin" "$remote_root/opt/remote-dev/coder" "$remote_root/root"
: > "$command_log"

cat > "$fake_bin/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
command_text=$*
printf 'ssh %s\n' "$command_text" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
if [[ "$command_text" == *remote-dev-bootstrap-state* ]]; then
  if [[ "$command_text" == *bootstrap-owner.env* ]]; then
    path="$REMOTE_DEV_TEST_ROOT/opt/remote-dev/coder/bootstrap-owner.env"
  else
    path="$REMOTE_DEV_TEST_ROOT/root/.coolify-bootstrap-credentials"
  fi
  [[ -e "$path" ]] && printf 'present\n' || printf 'absent\n'
elif [[ "$command_text" == *remote-dev-bootstrap-read* ]]; then
  if [[ "$command_text" == *bootstrap-owner.env* ]]; then
    cat "$REMOTE_DEV_TEST_ROOT/opt/remote-dev/coder/bootstrap-owner.env"
  else
    cat "$REMOTE_DEV_TEST_ROOT/root/.coolify-bootstrap-credentials"
  fi
elif [[ "$command_text" == *remote-dev-bootstrap-write* ]]; then
  cat > "$REMOTE_DEV_TEST_ROOT/root/.coolify-bootstrap-credentials"
elif [[ "$command_text" == *remote-dev-bootstrap-delete-if-equal* ]]; then
  if [[ "$command_text" == *bootstrap-owner.env* ]]; then
    path="$REMOTE_DEV_TEST_ROOT/opt/remote-dev/coder/bootstrap-owner.env"
  else
    path="$REMOTE_DEV_TEST_ROOT/root/.coolify-bootstrap-credentials"
  fi
  expected=$(cat)
  actual=$(cat "$path")
  [[ "$actual" == "$expected" ]]
  rm -f -- "$path"
else
  printf 'unexpected fake ssh command: %s\n' "$command_text" >&2
  exit 1
fi
SH

cat > "$fake_bin/python3" <<SH
#!/usr/bin/env bash
set -euo pipefail
if [[ "\${1:-}" != "$repo_root/bin/remote-dev-credential-helper.py" ]]; then
  exec "$real_python" "\$@"
fi
shift
command=\$1
shift
printf 'helper %s %s\n' "\$command" "\$*" >> "\$REMOTE_DEV_TEST_COMMAND_LOG"
case "\$command" in
  coder-login|coolify-login)
    cat >/dev/null
    if [[ "\${REMOTE_DEV_TEST_LOGIN_FAIL:-false}" == true ]]; then
      echo 'ERROR    synthetic login failure' >&2
      exit 1
    fi
    ;;
  coolify-status)
    if [[ -e "\$REMOTE_DEV_TEST_ROOT/coolify-account-present" ]]; then
      printf 'present\n'
    else
      printf 'absent\n'
    fi
    ;;
  coolify-register)
    cat >/dev/null
    if [[ "\${REMOTE_DEV_TEST_REGISTER_FAIL:-false}" == true ]]; then
      echo 'ERROR    synthetic registration failure' >&2
      exit 1
    fi
    touch "\$REMOTE_DEV_TEST_ROOT/coolify-account-present"
    ;;
  reveal)
    cat >/dev/null
    if [[ " \$* " == *' --consumer app '* ]]; then
      printf '{"version":1,"handoff_url":"http://127.0.0.1:1/one-time","single_use":true,"receipt":"%s"}\n' "\$REMOTE_DEV_TEST_RECEIPT"
    fi
    ;;
  receipt)
    cat >/dev/null
    printf '%s\n' "\$REMOTE_DEV_TEST_RECEIPT"
    ;;
  *)
    printf 'unexpected fake helper command: %s\n' "\$command" >&2
    exit 1
    ;;
esac
SH

chmod +x "$fake_bin/ssh" "$fake_bin/python3"

run_cli() {
  env \
    REMOTE_DEV_PROFILE=example-profile \
    REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
    REMOTE_DEV_TEST_ROOT="$remote_root" \
    REMOTE_DEV_TEST_LOGIN_FAIL="${REMOTE_DEV_TEST_LOGIN_FAIL:-false}" \
    REMOTE_DEV_TEST_REGISTER_FAIL="${REMOTE_DEV_TEST_REGISTER_FAIL:-false}" \
    PATH="$fake_bin:$PATH" \
    "$repo_root/bin/remote-dev" credentials "$@"
}

assert_refused() {
  local expected=$1
  shift
  local output status
  set +e
  output=$(run_cli "$@" 2>&1)
  status=$?
  set -e
  if (( status == 0 )) || [[ "$output" != *"$expected"* ]]; then
    printf 'expected refusal containing %q, got status %s: %s\n' \
      "$expected" "$status" "$output" >&2
    exit 1
  fi
}

assert_refused 'operation requires --confirm-profile example-profile' \
  reveal coder --consumer app --confirm-profile wrong
[[ ! -s "$command_log" ]]

printf 'CODER_EMAIL=owner@remote-dev.local\nCODER_PASSWORD=Synthetic-Coder-Password-1!\n' \
  > "$remote_root/opt/remote-dev/coder/bootstrap-owner.env"
descriptor=$(run_cli reveal coder --consumer app --confirm-profile example-profile 2>/dev/null)
[[ "$descriptor" == *'"single_use":true'* ]]
[[ "$descriptor" != *'Synthetic-Coder-Password'* ]]
[[ -f "$remote_root/opt/remote-dev/coder/bootstrap-owner.env" ]]
login_line=$(grep -n 'helper coder-login' "$command_log" | cut -d: -f1)
reveal_line=$(grep -n 'helper reveal' "$command_log" | cut -d: -f1)
(( login_line < reveal_line ))
! grep -Fq 'Synthetic-Coder-Password' "$command_log"

: > "$command_log"
assert_refused 'finalize requires --confirm-stored and --receipt' \
  finalize coder --confirm-profile example-profile
[[ ! -s "$command_log" ]]

: > "$command_log"
assert_refused 'receipt does not match the current coder credential' \
  finalize coder --confirm-profile example-profile --confirm-stored \
  --receipt sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
! grep -Fq 'helper coder-login' "$command_log"
! grep -Fq 'remote-dev-bootstrap-delete-if-equal' "$command_log"
[[ -f "$remote_root/opt/remote-dev/coder/bootstrap-owner.env" ]]

: > "$command_log"
run_cli finalize coder --confirm-profile example-profile --confirm-stored \
  --receipt "$test_receipt" >/dev/null
[[ ! -e "$remote_root/opt/remote-dev/coder/bootstrap-owner.env" ]]
login_line=$(grep -n 'helper coder-login' "$command_log" | cut -d: -f1)
delete_line=$(grep -n 'remote-dev-bootstrap-delete-if-equal' "$command_log" | cut -d: -f1)
(( login_line < delete_line ))

: > "$command_log"
status_output=$(run_cli status)
[[ "$status_output" == *'coder bootstrap=absent account=unknown'* ]]
[[ "$status_output" == *'coolify bootstrap=absent account=absent'* ]]
[[ "$status_output" != *claimed* ]]

: > "$command_log"
REMOTE_DEV_TEST_REGISTER_FAIL=true assert_refused 'synthetic registration failure' \
  provision coolify --consumer app --confirm-profile example-profile
[[ -f "$remote_root/root/.coolify-bootstrap-credentials" ]]
[[ ! -e "$remote_root/coolify-account-present" ]]
cp "$remote_root/root/.coolify-bootstrap-credentials" "$fixture/interrupted-bootstrap"

: > "$command_log"
descriptor=$(run_cli provision coolify --consumer app --confirm-profile example-profile 2>/dev/null)
[[ "$descriptor" == *'"single_use":true'* ]]
[[ -f "$remote_root/root/.coolify-bootstrap-credentials" ]]
[[ -f "$remote_root/coolify-account-present" ]]
cmp "$fixture/interrupted-bootstrap" "$remote_root/root/.coolify-bootstrap-credentials"
grep -Fq 'remote-dev-bootstrap-read' "$command_log"
! grep -Fq 'remote-dev-bootstrap-write' "$command_log"
register_line=$(grep -n 'helper coolify-register' "$command_log" | cut -d: -f1)
login_line=$(grep -n 'helper coolify-login' "$command_log" | cut -d: -f1)
reveal_line=$(grep -n 'helper reveal' "$command_log" | cut -d: -f1)
(( register_line < login_line && login_line < reveal_line ))

: > "$command_log"
REMOTE_DEV_TEST_LOGIN_FAIL=true assert_refused 'synthetic login failure' \
  reveal coolify --confirm-profile example-profile
[[ -f "$remote_root/root/.coolify-bootstrap-credentials" ]]

run_cli finalize coolify --confirm-profile example-profile --confirm-stored \
  --receipt "$test_receipt" >/dev/null
[[ ! -e "$remote_root/root/.coolify-bootstrap-credentials" ]]

python3 - "$repo_root/bin/remote-dev-credential-helper.py" <<'PY'
import concurrent.futures
import json
import subprocess
import sys
import urllib.error
import urllib.request

helper = sys.argv[1]

def receipt(profile, service, url, account, password):
    result = subprocess.run(
        [
            sys.executable,
            helper,
            "receipt",
            "--profile",
            profile,
            "--service",
            service,
            "--url",
            url,
        ],
        input=f"{account}\n{password}\n",
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()

receipt_args = (
    "synthetic-profile",
    "Synthetic",
    "https://example.test",
    "owner@example.test",
    "Synthetic-Reveal-Password-1!",
)
baseline_receipt = receipt(*receipt_args)
assert baseline_receipt == receipt(*receipt_args)
for changed_args in (
    ("other-profile", *receipt_args[1:]),
    (receipt_args[0], "Other service", *receipt_args[2:]),
    (*receipt_args[:2], "https://other.example.test", *receipt_args[3:]),
    (*receipt_args[:3], "other@example.test", receipt_args[4]),
    (*receipt_args[:4], "Synthetic-Reveal-Password-2!"),
):
    assert receipt(*changed_args) != baseline_receipt

process = subprocess.Popen(
    [sys.executable, helper, "reveal", "--profile", "synthetic-profile", "--service", "Synthetic", "--url", "https://example.test", "--consumer", "app", "--open-timeout", "10"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
assert process.stdin is not None
process.stdin.write("owner@example.test\nSynthetic-Reveal-Password-1!\n")
process.stdin.close()
assert process.stdout is not None
descriptor_line = process.stdout.readline()
assert "Synthetic-Reveal-Password" not in descriptor_line
descriptor = json.loads(descriptor_line)
def consume_once(_index):
    request = urllib.request.Request(
        descriptor["handoff_url"], headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.load(response) if response.status == 200 else None
            return response.status, response.headers["Cache-Control"], body
    except (urllib.error.HTTPError, urllib.error.URLError, ConnectionError):
        return 0, "", None

with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
    results = list(executor.map(consume_once, range(8)))
successes = [result for result in results if result[0] == 200]
assert len(successes) == 1
_, cache_control, credential = successes[0]
assert cache_control.startswith("no-store")
assert credential["account"] == "owner@example.test"
assert credential["password"] == "Synthetic-Reveal-Password-1!"
assert credential["receipt"] == descriptor["receipt"]
assert process.wait(timeout=5) == 0
PY

echo 'remote-dev credential smoke passed'
