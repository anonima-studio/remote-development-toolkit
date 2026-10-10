#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
fixture=$(mktemp -d)
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT

fake_bin=$fixture/bin
command_log=$fixture/commands.log
mkdir -p "$fake_bin" "$fixture/home"
: > "$command_log"

cat > "$fake_bin/uname" <<'EOF'
#!/usr/bin/env bash
printf 'Darwin\n'
EOF

cat > "$fake_bin/coder" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'coder' >> "$REMOTE_DEV_TEST_COMMAND_LOG"
printf ' %q' "$@" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
printf '\n' >> "$REMOTE_DEV_TEST_COMMAND_LOG"
case " $* " in
  *' whoami '*)
    [[ ${REMOTE_DEV_TEST_WHOAMI_FAIL:-false} != true ]]
    ;;
  *' create '*)
    [[ ${REMOTE_DEV_TEST_CREATE_FAIL:-false} != true ]]
    ;;
  *' start '*)
    [[ ${REMOTE_DEV_TEST_START_FAIL:-false} != true ]]
    ;;
  *' update '*)
    [[ ${REMOTE_DEV_TEST_UPDATE_FAIL:-false} != true ]] || exit 1
    touch "$REMOTE_DEV_TEST_COMMAND_LOG.updated"
    ;;
  *' ssh '*)
    separator=0
    for (( index = 1; index <= $#; index++ )); do
      [[ ${!index} == -- ]] && separator=$index
    done
    (( separator > 0 && separator == $# - 1 )) || {
      printf 'fake coder expected one shell-quoted remote command\n' >&2
      exit 1
    }
    remote_index=$((separator + 1))
    remote_command=${!remote_index}
    if [[ $remote_command == *'/usr/local/bin/codex --version'* ]]; then
      [[ ${REMOTE_DEV_TEST_REMOTE_ROLE:-development} == development ]] || exit 1
      [[ ${REMOTE_DEV_TEST_PROBE_FAIL:-false} != true ]] || exit 1
      if [[ -f $REMOTE_DEV_TEST_COMMAND_LOG.updated ]]; then
        printf 'codex-cli %s\n' "${REMOTE_DEV_TEST_UPDATED_CODEX:-0.154.0}"
      else
        printf 'codex-cli 0.147.0\n'
      fi
      exit 0
    fi
    [[ $remote_command == *'/usr/local/bin/remote-dev-workspace-role'* ]] || {
      printf 'unexpected remote role command: %s\n' "$remote_command" >&2
      exit 1
    }
    if [[ $remote_command == *' show' ]]; then
      printf 'workspace startup progress\n' >&2
      printf '%s\n' "${REMOTE_DEV_TEST_REMOTE_ROLE:-runner}"
    else
      printf 'workspace_role=%s\n' "${REMOTE_DEV_TEST_REMOTE_ROLE:-runner}"
    fi
    ;;
esac
EOF
chmod 0755 "$fake_bin/uname" "$fake_bin/coder"

output=$(env \
  HOME="$fixture/home" \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability containers --size standard)

for expected in \
  'READY      Coder workspace is running.' \
  'Profile    example-profile' \
  'Coder URL  https://coder.example.invalid' \
  'Workspace  example-app-runner' \
  'Role       runner' \
  'Capability containers' \
  'Size       standard'; do
  [[ $output == *"$expected"* ]] || {
    printf 'workspace output omitted %q:\n%s\n' "$expected" "$output" >&2
    exit 1
  }
done

grep -Fq -- '--global-config' "$command_log"
grep -Fq -- '--no-version-warning' "$command_log"
grep -Fq -- "$fixture/coder-config/example-profile" "$command_log"
grep -Fq -- '--url https://coder.example.invalid create' "$command_log"
grep -Fq -- '--template remote-dev-docker' "$command_log"
grep -Fq -- '--parameter workspace_role=runner' "$command_log"
grep -Fq -- '--parameter docker_capability=containers' "$command_log"
grep -Fq -- '--parameter workspace_size=standard' "$command_log"
grep -Fq -- '--yes example-app-runner' "$command_log"
grep -Fq -- '--url https://coder.example.invalid ssh --wait=yes example-app-runner' \
  "$command_log"

: > "$command_log"
pinned_output=$(env \
  HOME="$fixture/home" \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability containers --size standard \
  --template-version mode-pilot-cf2a26a364e)
[[ $pinned_output == *'Template   mode-pilot-cf2a26a364e (automatic updates disabled)'* ]]
grep -Fq -- '--template-version mode-pilot-cf2a26a364e --automatic-updates never' "$command_log"

: > "$command_log"
start_output=$(env \
  HOME="$fixture/home" \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace start example-app-runner)
[[ $start_output == *'READY      Coder workspace is running.'* ]]
[[ $start_output == *'Workspace  example-app-runner'* ]]
[[ $start_output == *'Role       runner'* ]]
grep -Fq -- '--url https://coder.example.invalid start --yes example-app-runner' "$command_log"
grep -Fq -- '--url https://coder.example.invalid ssh --wait=yes example-app-runner' "$command_log"

url_file=$fixture/coder-config/example-profile/url
[[ $(<"$url_file") == 'https://coder.example.invalid' ]]
url_mode=$(stat -f '%Lp' "$url_file" 2>/dev/null || stat -c '%a' "$url_file")
[[ $url_mode == 600 ]]

assert_refused() {
  local expected=$1
  shift
  local output status
  set +e
  output=$(env \
    HOME="$fixture/home" \
    PATH="$fake_bin:$PATH" \
    REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config" \
    REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
    "$@" 2>&1)
  status=$?
  set -e
  if (( status == 0 )) || [[ $output != *"$expected"* ]]; then
    printf 'expected refusal containing %q, got status %s:\n%s\n' \
      "$expected" "$status" "$output" >&2
    exit 1
  fi
}

assert_refused '--role must be development or runner' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role mutable --capability core --size small

assert_refused '--template-version must be one explicit version name' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability containers --size standard --template-version '../unexpected'

assert_refused 'invalid Coder workspace name' \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create '../example-app' \
  --role runner --capability core --size small

assert_refused 'WORKSPACE_ROLE_MISMATCH' \
  env REMOTE_DEV_TEST_REMOTE_ROLE=development \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability core --size small

assert_refused 'Workspace creation failed.' \
  env REMOTE_DEV_TEST_CREATE_FAIL=true \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability core --size small

assert_refused 'Workspace start failed.' \
  env REMOTE_DEV_TEST_START_FAIL=true \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace start example-app-runner

assert_refused 'WORKSPACE_ROLE_INVALID' \
  env REMOTE_DEV_TEST_REMOTE_ROLE=invalid \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace start example-app-runner

assert_refused 'Coder login is required' \
  env REMOTE_DEV_TEST_WHOAMI_FAIL=true \
  "$repo_root/bin/remote-dev" --profile example-profile \
  workspace create example-app-runner \
  --role runner --capability core --size small

printf 'PASS     profile-aware workspace create/start verifies immutable role\n'

update_workspace() {
  env HOME="$fixture/home" PATH="$fake_bin:$PATH" \
    REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT="$fixture/coder-config" \
    REMOTE_DEV_TEST_COMMAND_LOG="$command_log" "$@" \
    "$repo_root/bin/remote-dev" --profile example-profile workspace update \
    example-app --expect-codex 0.154.0 "${update_flag:---restart}"
}
: > "$command_log"
update_flag=--check
checked=$(update_workspace env)
[[ $checked == *'CHECKED'* && ! -f $command_log.updated ]]
! grep -q ' update ' "$command_log"
update_flag=--restart
updated=$(update_workspace env)
[[ $updated == *'READY'* && $updated == *'0.154.0'* ]]
grep -Fq -- 'update example-app' "$command_log"
grep -Fq -- 'ssh --disable-autostart=true --wait=no' "$command_log"
grep -Fq -- 'ssh --disable-autostart=true --wait=yes' "$command_log"
for scenario in runner probe update mismatch; do
  rm -f "$command_log.updated"
  : > "$command_log"
  case "$scenario" in
    runner) failure_env=REMOTE_DEV_TEST_REMOTE_ROLE=runner ;;
    probe) failure_env=REMOTE_DEV_TEST_PROBE_FAIL=true ;;
    update) failure_env=REMOTE_DEV_TEST_UPDATE_FAIL=true ;;
    mismatch) failure_env=REMOTE_DEV_TEST_UPDATED_CODEX=0.147.0 ;;
  esac
  if failed=$(update_workspace env "$failure_env" 2>&1); then
    printf 'unexpected update success: %s\n' "$scenario" >&2; exit 1
  fi
  [[ $failed != *'READY'* ]]
  if [[ $scenario == runner || $scenario == probe ]]; then
    ! grep -q ' update ' "$command_log"
  fi
done
assert_refused 'update requires' \
  "$repo_root/bin/remote-dev" --profile example-profile workspace update example-app
assert_refused 'choose exactly one' \
  "$repo_root/bin/remote-dev" --profile example-profile workspace update example-app \
  --expect-codex 0.154.0 --check --restart
printf 'PASS     workspace update preflight, interruption opt-in, and version verification\n'
