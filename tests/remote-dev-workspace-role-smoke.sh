#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
template=$repo_root/templates/docker/main.tf
fixture=$(mktemp -d)
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT

require_template_text() {
  local expected=$1
  grep -Fq -- "$expected" "$template" || {
    printf 'workspace role template omitted: %s\n' "$expected" >&2
    exit 1
  }
}

for expected in \
  'data "coder_parameter" "workspace_role" {' \
  'default      = "development"' \
  'mutable      = false' \
  'value = "development"' \
  'value = "runner"' \
  'startup_script_behavior = "blocking"' \
  'vscode                 = data.coder_parameter.workspace_role.value == "development"' \
  'role_marker="$HOME/.local/share/remote-dev/workspace-role"' \
  'label = "remote-dev.workspace-role"' \
  'value = data.coder_parameter.workspace_role.value' \
  'display_name = "Workspace role"' \
  'REMOTE_DEV_WORKSPACE_ROLE = data.coder_parameter.workspace_role.value' \
  'count        = data.coder_parameter.workspace_role.value == "development" ? data.coder_workspace.me.start_count : 0'; do
  require_template_text "$expected"
done

for isolated_volume in \
  'name = "coder-${data.coder_workspace.me.id}-home"' \
  'name = "coder-${data.coder_workspace.me.id}-docker"' \
  'volume_name    = docker_volume.home.name' \
  'volume_name    = docker_volume.docker_data.name'; do
  require_template_text "$isolated_volume"
done

development_only_block=$(awk '
  /# remote-dev-development-only:start/ { capture = 1; next }
  /# remote-dev-development-only:end/ { capture = 0; exit }
  capture { print }
' "$template")
for expected in \
  'if [ "$requested_workspace_role" = '\''development'\'' ]; then' \
  '$HOME/projects/.codex-worktrees' \
  'worktree-branch-sync'; do
  [[ $development_only_block == *"$expected"* ]] || {
    printf 'development-only startup block omitted: %s\n' "$expected" >&2
    exit 1
  }
done

render_role_script() {
  local role=$1 destination=$2
  {
    printf '#!/bin/sh\nset -eu\n'
    awk -v role="$role" '
      /# remote-dev-workspace-role:start/ { capture = 1; next }
      /# remote-dev-workspace-role:end/ { capture = 0; exit }
      capture {
        gsub(/[$][{]data[.]coder_parameter[.]workspace_role[.]value[}]/, role)
        print
      }
    ' "$template"
  } > "$destination"
  chmod 0755 "$destination"
}

development_script=$fixture/development-role.sh
runner_script=$fixture/runner-role.sh
render_role_script development "$development_script"
render_role_script runner "$runner_script"

development_home=$fixture/development-home
runner_home=$fixture/runner-home
mkdir -p "$development_home" "$runner_home"

HOME=$development_home "$development_script"
printf 'development\n' > "$fixture/expected-development"
cmp -s "$fixture/expected-development" \
  "$development_home/.local/share/remote-dev/workspace-role" || {
  echo 'development role marker does not contain the exact persistent role' >&2
  exit 1
}
development_marker_mode=$(stat -f '%Lp' \
  "$development_home/.local/share/remote-dev/workspace-role" 2>/dev/null || \
  stat -c '%a' "$development_home/.local/share/remote-dev/workspace-role")
if [[ $development_marker_mode != 600 ]]; then
  printf 'development role marker mode is %s, expected 600\n' \
    "$development_marker_mode" >&2
  exit 1
fi
HOME=$development_home "$development_script"

HOME=$runner_home "$runner_script"
printf 'runner\n' > "$fixture/expected-runner"
cmp -s "$fixture/expected-runner" \
  "$runner_home/.local/share/remote-dev/workspace-role" || {
  echo 'runner role marker does not contain the exact persistent role' >&2
  exit 1
}

set +e
mismatch_output=$(HOME=$development_home "$runner_script" 2>&1)
mismatch_status=$?
set -e
if (( mismatch_status != 1 )) || \
   [[ $mismatch_output != *'WORKSPACE_ROLE_MISMATCH'* ]] || \
   [[ $mismatch_output != *'create a new workspace'* ]]; then
  printf 'persistent role mismatch did not fail closed with recovery: %s\n' \
    "$mismatch_output" >&2
  exit 1
fi
cmp -s "$fixture/expected-development" \
  "$development_home/.local/share/remote-dev/workspace-role" || {
  echo 'role mismatch changed the persistent development marker' >&2
  exit 1
}

malformed_home=$fixture/malformed-home
mkdir -p "$malformed_home/.local/share/remote-dev"
printf 'development' > "$malformed_home/.local/share/remote-dev/workspace-role"
set +e
malformed_output=$(HOME=$malformed_home "$development_script" 2>&1)
malformed_status=$?
set -e
if (( malformed_status != 1 )) || \
   [[ $malformed_output != *'WORKSPACE_ROLE_MISMATCH'* ]]; then
  printf 'non-exact role marker did not fail closed: %s\n' \
    "$malformed_output" >&2
  exit 1
fi

role_helper=$repo_root/templates/docker/workspace-role.sh
runner_marker=$runner_home/.local/share/remote-dev/workspace-role
if [[ $(REMOTE_DEV_WORKSPACE_ROLE_MARKER=$runner_marker \
  "$role_helper" show) != runner ]]; then
  echo 'workspace role helper did not report the exact runner role' >&2
  exit 1
fi

for managed_helper in \
  "$repo_root/templates/docker/codex-start.sh" \
  "$repo_root/templates/docker/project-setup.sh" \
  "$repo_root/templates/docker/worktree-vars.sh"; do
  grep -Fq '"$role_helper" require development' "$managed_helper" || {
    printf 'development helper omitted shared workspace role preflight: %s\n' \
      "$managed_helper" >&2
    exit 1
  }
  set +e
  role_output=$(env \
    REMOTE_DEV_WORKSPACE_ROLE_HELPER="$role_helper" \
    REMOTE_DEV_WORKSPACE_ROLE_MARKER="$runner_marker" \
    "$managed_helper" 2>&1)
  role_status=$?
  set -e
  if (( role_status != 1 )) || \
     [[ $role_output != *'WORKSPACE_ROLE_MISMATCH expected=development actual=runner'* ]]; then
    printf 'development helper accepted a runner workspace: %s\n%s\n' \
      "$managed_helper" "$role_output" >&2
    exit 1
  fi
done

printf 'PASS     immutable development and runner workspace roles stay isolated\n'
