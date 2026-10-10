#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
helper=$repo_root/templates/docker/project-setup.sh
fixture=$(mktemp -d)
fake_bin=$fixture/bin
command_log=$fixture/commands.log
projects_root=$fixture/projects
project_path=$projects_root/demo
contract_dir=$project_path/.remote-dev
contract=$contract_dir/project.env
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT
role_marker=$fixture/workspace-role
printf 'development\n' > "$role_marker"
export REMOTE_DEV_WORKSPACE_ROLE_HELPER=$repo_root/templates/docker/workspace-role.sh
export REMOTE_DEV_WORKSPACE_ROLE_MARKER=$role_marker
export REMOTE_DEV_PROJECT_CONTRACT_HELPER=$repo_root/templates/docker/project-contract.py
export REMOTE_DEV_MODE_GUARD_HELPER=$repo_root/templates/docker/mode-guard.py
mkdir -p "$fake_bin" "$contract_dir"
project_path=$(cd "$project_path" && pwd -P)
contract_dir=$project_path/.remote-dev
contract=$contract_dir/project.env

cat > "$fake_bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'ssh %s\n' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"
if [[ "$*" == *'remote-dev-project-setup'*'--help'* && \
      "${REMOTE_DEV_TEST_PROJECT_HELPER_ABSENT:-false}" == true ]]; then
  echo '/usr/local/bin/remote-dev-project-setup: No such file or directory' >&2
  exit 127
fi
if [[ "$*" == *'remote-dev-project-setup'*'--help'* && \
      "${REMOTE_DEV_TEST_WORKSPACE_UNREACHABLE:-false}" == true ]]; then
  echo 'Workspace is stopped' >&2
  exit 1
fi
exit 0
EOF
cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s' "${REMOTE_DEV_TEST_HTTP_STATUS:-204}"
EOF
chmod +x "$fake_bin/ssh" "$fake_bin/curl"

help_output=$($repo_root/bin/remote-dev --help)
if [[ $help_output != *'project-setup Run one project-owned setup/start/health contract'* ]]; then
  echo 'root help omitted project-setup' >&2
  exit 1
fi

set +e
owner_workspace_output=$(env PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup owner/example-app --project-dir demo 2>&1)
owner_workspace_status=$?
set -e
if (( owner_workspace_status != 2 )) || \
   [[ $owner_workspace_output != *'invalid Coder workspace name'* ]]; then
  printf 'owner-qualified workspace did not fail before printing fallback: %s\n' \
    "$owner_workspace_output" >&2
  exit 1
fi

set +e
invalid_output=$(env PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup example-app --project-dir ../escape 2>&1)
invalid_status=$?
set -e
if (( invalid_status != 2 )) || [[ $invalid_output != *'invalid project directory'* ]]; then
  printf 'unsafe project path was not rejected: %s\n' "$invalid_output" >&2
  exit 1
fi

: > "$command_log"
env PATH="$fake_bin:$PATH" REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup example-app --project-dir projects/demo
env PATH="$fake_bin:$PATH" REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup example-app \
  --project-dir .codex-worktrees/example-app-feature
for expected in \
  '/usr/local/bin/remote-dev-workspace-role\ require\ development' \
  '/usr/local/bin/remote-dev-project-setup\ --help' \
  '/usr/local/bin/remote-dev-project-setup --profile example-profile --workspace example-app --project-dir projects/demo'; do
  grep -Fq "$expected" "$command_log" || {
    printf 'project-setup CLI omitted %s:\n' "$expected" >&2
    cat "$command_log" >&2
    exit 1
  }
done
grep -Fq -- '--project-dir .codex-worktrees/example-app-feature' "$command_log" || {
  echo 'project-setup rejected the hidden Codex worktree root' >&2
  exit 1
}

set +e
missing_helper_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_PROJECT_HELPER_ABSENT=true \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup example-app --project-dir projects/demo 2>&1)
missing_helper_status=$?
set -e
if (( missing_helper_status != 1 )) || \
   [[ $missing_helper_output != *'does not contain the current project setup helper'* ]] || \
   [[ $missing_helper_output != *'choose Update'* ]]; then
  printf 'missing helper recovery was not actionable: %s\n' \
    "$missing_helper_output" >&2
  exit 1
fi

set +e
unreachable_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_WORKSPACE_UNREACHABLE=true \
  "$repo_root/bin/remote-dev" --profile example-profile \
  project-setup example-app --project-dir projects/demo 2>&1)
unreachable_status=$?
set -e
if (( unreachable_status != 1 )) || \
   [[ $unreachable_output != *'Start the workspace in Coder'* ]] || \
   [[ $unreachable_output != *'Workspace is stopped'* ]]; then
  printf 'stopped workspace recovery was not actionable: %s\n' \
    "$unreachable_output" >&2
  exit 1
fi

write_contract() {
  cat > "$contract" <<'EOF'
version=1
setup_command=printf setup > "$REMOTE_DEV_PROJECT_DIR/setup.marker"
start_command=printf start > "$REMOTE_DEV_PROJECT_DIR/start.marker"
health_url=http://127.0.0.1:3000/health/ready
browser_port=3000
coder_hostname=demo.coder
EOF
}

write_contract
setup_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --profile example-profile --workspace demo --project-dir demo)
for expected in \
  'PHASE    setup' \
  'PHASE    start' \
  'READY    Project setup completed and health is HTTP 204.' \
  "Project  $project_path" \
  'Open     http://demo.coder:3000' \
  'Fallback bin/remote-dev --profile example-profile preview demo 3000'; do
  [[ $setup_output == *"$expected"* ]] || {
    printf 'successful setup omitted %s:\n%s\n' "$expected" "$setup_output" >&2
    exit 1
  }
done
if [[ $(<"$project_path/setup.marker") != setup ]] || \
   [[ $(<"$project_path/start.marker") != start ]]; then
  echo 'project setup phase markers do not match expected side effects' >&2
  exit 1
fi

write_contract
printf '%s\n' 'browser_port=3300' >> "$contract"
set +e
duplicate_output=$(env PATH="$fake_bin:$PATH" \
  REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --profile example-profile --workspace demo --project-dir demo 2>&1)
duplicate_status=$?
set -e
if (( duplicate_status != 1 )) || [[ $duplicate_output != *'duplicate project contract key'* ]]; then
  printf 'duplicate contract key was not rejected: %s\n' "$duplicate_output" >&2
  exit 1
fi

write_contract
sed -i.bak 's/setup_command=.*/setup_command=exit 23/' "$contract"
set +e
phase_output=$(env PATH="$fake_bin:$PATH" \
  REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --profile example-profile --workspace demo --project-dir demo 2>&1)
phase_status=$?
set -e
if (( phase_status != 23 )) || \
   [[ $phase_output != *'Project setup phase failed'* ]] || \
   [[ $phase_output != *'rerun project-setup'* ]]; then
  printf 'setup phase failure lost its recovery: %s\n' "$phase_output" >&2
  exit 1
fi

write_contract
set +e
health_output=$(env \
  PATH="$fake_bin:$PATH" \
  REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  REMOTE_DEV_PROJECT_HEALTH_TIMEOUT_SECONDS=1 \
  REMOTE_DEV_TEST_HTTP_STATUS=503 \
  "$helper" --profile example-profile --workspace demo --project-dir demo 2>&1)
health_status=$?
set -e
if (( health_status != 1 )) || \
   [[ $health_output != *'Project health did not become ready'* ]] || \
   [[ $health_output != *'inspect the project-owned start command'* ]]; then
  printf 'health failure lost its recovery: %s\n' "$health_output" >&2
  exit 1
fi

write_contract
mv "$contract" "$contract.real"
ln -s project.env.real "$contract"
set +e
symlink_output=$(env PATH="$fake_bin:$PATH" \
  REMOTE_DEV_PROJECTS_ROOT="$projects_root" \
  "$helper" --profile example-profile --workspace demo --project-dir demo 2>&1)
symlink_status=$?
set -e
if (( symlink_status != 1 )) || [[ $symlink_output != *'regular non-symlink file'* ]]; then
  printf 'symlink contract was not rejected: %s\n' "$symlink_output" >&2
  exit 1
fi

printf 'PASS     Universal project setup validates contract, phases, health, and recovery\n'
