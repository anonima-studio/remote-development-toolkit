#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
codex_version=$(sed -n 's/^ARG CODEX_VERSION=//p' "$repo_root/templates/docker/Dockerfile")
[[ -n $codex_version ]]
export REMOTE_DEV_CODEX_IMAGE_VERSION="codex-cli $codex_version"
grep -Fxq "expected_codex_version='$REMOTE_DEV_CODEX_IMAGE_VERSION'" \
  "$repo_root/templates/docker/codex-start.sh"
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT

test_home=$fixture/home
fake_bin=$fixture/bin
shadow_bin=$fixture/shadow-bin
auth_marker=$fixture/logged-in
launch_log=$fixture/launch.log
test_helper=$fixture/codex-start.sh
skill_source=$fixture/skill/worktree-branch-sync
mkdir -p "$test_home/projects/example-app" \
  "$test_home/projects/.codex-worktrees" \
  "$test_home/.agents/skills" \
  "$test_home/.local/share/remote-dev" \
  "$fake_bin" "$shadow_bin" "$skill_source"
test_home=$(cd "$test_home" && pwd -P)
role_marker=$test_home/.local/share/remote-dev/workspace-role
printf 'development\n' > "$role_marker"
printf '%s\n' '---' 'name: worktree-branch-sync' '---' \
  > "$skill_source/SKILL.md"
ln -s "$skill_source" \
  "$test_home/.agents/skills/worktree-branch-sync"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'case "${1:-}" in' \
  '  --version)' \
  '    echo "${REMOTE_DEV_CODEX_TEST_VERSION:-$REMOTE_DEV_CODEX_IMAGE_VERSION}"' \
  '    exit 0' \
  '    ;;' \
  '  login)' \
  '    if [[ "${2:-}" == status ]]; then' \
  '      [[ -f "$REMOTE_DEV_CODEX_TEST_AUTH_MARKER" ]]' \
  '      exit' \
  '    fi' \
  '    if [[ "${2:-}" == --device-auth ]]; then' \
  '      mkdir -p "$HOME/.codex"' \
  '      printf "{}\\n" > "$HOME/.codex/auth.json"' \
  '      : > "$REMOTE_DEV_CODEX_TEST_AUTH_MARKER"' \
  '      exit 0' \
  '    fi' \
  '    ;;' \
  'esac' \
  'printf "launch_dir=%s\\n" "$PWD" >> "$REMOTE_DEV_CODEX_TEST_LAUNCH_LOG"' \
  'printf "launch_arg=%s\\n" "$*" >> "$REMOTE_DEV_CODEX_TEST_LAUNCH_LOG"' \
  > "$fake_bin/codex"
chmod +x "$fake_bin/codex"

sed "s#codex_bin=/usr/local/bin/codex#codex_bin=$fake_bin/codex#" \
  "$repo_root/templates/docker/codex-start.sh" > "$test_helper"
chmod +x "$test_helper"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'echo "PATH shadow Codex must not run" >&2' \
  'exit 99' \
  > "$shadow_bin/codex"
chmod +x "$shadow_bin/codex"

common_env=(
  HOME="$test_home"
  PATH="$shadow_bin:$PATH"
  REMOTE_DEV_CODEX_TEST_AUTH_MARKER="$auth_marker"
  REMOTE_DEV_CODEX_TEST_LAUNCH_LOG="$launch_log"
  REMOTE_DEV_BRANCH_SYNC_SKILL_SOURCE="$skill_source"
  REMOTE_DEV_WORKSPACE_ROLE_HELPER="$repo_root/templates/docker/workspace-role.sh"
  REMOTE_DEV_MODE_GUARD_HELPER="$repo_root/templates/docker/mode-guard.py"
  REMOTE_DEV_WORKSPACE_ROLE_MARKER="$role_marker"
)

assert_contains() {
  local value=$1
  local expected=$2
  if [[ $value != *"$expected"* ]]; then
    printf 'Codex smoke output omitted %s:\n%s\n' "$expected" "$value" >&2
    exit 1
  fi
}

check_output=$(env "${common_env[@]}" \
  "$test_helper" \
  --check --project-dir example-app)
assert_contains "$check_output" "codex_path=$fake_bin/codex"
assert_contains "$check_output" "codex_version=$REMOTE_DEV_CODEX_IMAGE_VERSION"
assert_contains "$check_output" 'codex_auth=required'
assert_contains "$check_output" "project_dir=$test_home/projects/example-app"
assert_contains "$check_output" "worktree_root=$test_home/projects/.codex-worktrees"
assert_contains "$check_output" 'worktree_root_state=ready'
assert_contains "$check_output" "branch_sync_skill=$test_home/.agents/skills/worktree-branch-sync"
assert_contains "$check_output" 'branch_sync_skill_state=ready'
if [[ ! -L $test_home/.agents/skills/worktree-branch-sync ]]; then
  echo 'branch-sync skill is not installed as the image-owned symlink' >&2
  exit 1
fi

git -C "$test_home/projects/example-app" init -q
git -C "$test_home/projects/example-app" config user.name 'Remote Dev Test'
git -C "$test_home/projects/example-app" config user.email remote-dev@example.test
printf 'tracked\n' > "$test_home/projects/example-app/README.md"
git -C "$test_home/projects/example-app" add README.md
git -C "$test_home/projects/example-app" commit -qm initial
git -C "$test_home/projects/example-app" worktree add -qb feature \
  "$test_home/projects/.codex-worktrees/example-app-feature"
worktree_check=$(env "${common_env[@]}" \
  "$test_helper" --check --project-dir .codex-worktrees/example-app-feature)
assert_contains "$worktree_check" \
  "project_dir=$test_home/projects/.codex-worktrees/example-app-feature"
if [[ ! -f $test_home/projects/.codex-worktrees/example-app-feature/.git ]]; then
  echo 'linked worktree did not use a .git indirection file' >&2
  exit 1
fi

env "${common_env[@]}" \
  "$test_helper" \
  --project-dir example-app -- first-launch
[[ -f $auth_marker ]]
[[ -s $test_home/.codex/auth.json ]]
[[ $(<"$launch_log") == *"launch_dir=$test_home/projects/example-app"* ]]
[[ $(<"$launch_log") == *'launch_arg=first-launch'* ]]

check_output=$(env "${common_env[@]}" \
  "$test_helper" \
  --check --project-dir example-app)
[[ $check_output == *'codex_auth=ready'* ]]

set +e
outside_output=$(env "${common_env[@]}" \
  "$test_helper" \
  --project-dir "$fixture" -- second-launch 2>&1)
outside_status=$?
set -e
if (( outside_status != 2 )) || \
   [[ $outside_output != *'Codex project directory must stay below'* ]]; then
  printf 'outside project directory was not refused: %s\n' "$outside_output" >&2
  exit 1
fi

set +e
version_output=$(env "${common_env[@]}" \
  REMOTE_DEV_CODEX_TEST_VERSION='codex-cli 9.9.9' \
  "$test_helper" --check --project-dir example-app 2>&1)
version_status=$?
set -e
if (( version_status != 1 )) || \
   [[ $version_output != *'Unexpected image-owned Codex version'* ]]; then
  printf 'unexpected Codex version was not refused: %s\n' "$version_output" >&2
  exit 1
fi

grep -Fxq 'codex_bin=/usr/local/bin/codex' \
  "$repo_root/templates/docker/codex-start.sh"
grep -Fq 'npm install --global @openai/codex@${CODEX_VERSION}' \
  "$repo_root/templates/docker/Dockerfile"

echo 'remote-dev Codex smoke passed'
