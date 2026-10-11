#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remote-dev-codex [--check] [--project-dir <path>] [-- <codex arguments>]

Verify the image-provided Codex CLI, initialize persistent user state, perform
device-code authentication when required, and launch Codex.

Options:
  --check               Report installation and authentication state only
  --project-dir <path>  Start below /home/coder/projects (default: its root)
  -h, --help            Show this help
EOF
}

check_only=false
project_dir=.
codex_bin=/usr/local/bin/codex
# renovate: datasource=npm depName=@openai/codex
expected_codex_version='codex-cli 0.147.0'
while (( $# > 0 )); do
  case "$1" in
    --check)
      check_only=true
      shift
      ;;
    --project-dir)
      (( $# >= 2 )) || { echo '--project-dir requires a path' >&2; exit 2; }
      project_dir=$2
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown remote-dev-codex option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

role_helper=${REMOTE_DEV_WORKSPACE_ROLE_HELPER:-/usr/local/bin/remote-dev-workspace-role}
"$role_helper" require development >/dev/null

if [[ ! -x $codex_bin ]]; then
  cat >&2 <<'EOF'
Codex is absent from this workspace image. Reconcile Remote Dev, update the
workspace to the current template, and retry; do not install a floating version
during workspace startup.
EOF
  exit 1
fi

actual_codex_version=$("$codex_bin" --version)
if [[ $actual_codex_version != "$expected_codex_version" ]]; then
  printf 'Unexpected image-owned Codex version: expected %s, got %s\n' \
    "$expected_codex_version" "$actual_codex_version" >&2
  exit 1
fi

projects_root=$HOME/projects
worktree_root=$projects_root/.codex-worktrees
skills_root=$HOME/.agents/skills
branch_sync_skill=$skills_root/worktree-branch-sync
branch_sync_skill_source=${REMOTE_DEV_BRANCH_SYNC_SKILL_SOURCE:-/usr/local/share/remote-dev/skills/worktree-branch-sync}
mkdir -p "$projects_root" "$HOME/.codex"
chmod 0700 "$HOME/.codex"
if [[ -d $worktree_root && -w $worktree_root ]]; then
  worktree_root_state=ready
else
  worktree_root_state=missing
fi
branch_sync_skill_state=missing
if [[ -e $branch_sync_skill && ! -L $branch_sync_skill ]]; then
  branch_sync_skill_state=conflict
elif [[ -L $branch_sync_skill && -f $branch_sync_skill/SKILL.md ]]; then
  branch_sync_skill_state=ready
elif [[ ! -f $branch_sync_skill_source/SKILL.md ]]; then
  branch_sync_skill_state=image-missing
fi

if [[ $project_dir = /* ]]; then
  requested_dir=$project_dir
else
  requested_dir=$projects_root/$project_dir
fi
if [[ ! -d $requested_dir ]]; then
  printf 'Codex project directory does not exist: %s\n' "$requested_dir" >&2
  exit 2
fi

projects_real=$(realpath "$projects_root")
requested_real=$(realpath "$requested_dir")
if [[ $check_only != true ]]; then
  mode_guard=${REMOTE_DEV_MODE_GUARD_HELPER:-/usr/local/bin/remote-dev-mode-guard}
  python3 "$mode_guard" "$requested_real"
fi
case "$requested_real/" in
  "$projects_real/"|"$projects_real/"*) ;;
  *)
    printf 'Codex project directory must stay below %s: %s\n' \
      "$projects_real" "$requested_real" >&2
    exit 2
    ;;
esac

if "$codex_bin" login status >/dev/null 2>&1; then
  auth_state=ready
else
  auth_state=required
fi

if [[ $check_only == true ]]; then
  printf 'codex_path=%s\n' "$codex_bin"
  printf 'codex_version=%s\n' "$actual_codex_version"
  printf 'codex_auth=%s\n' "$auth_state"
  printf 'project_dir=%s\n' "$requested_real"
  printf 'worktree_root=%s\n' "$worktree_root"
  printf 'worktree_root_state=%s\n' "$worktree_root_state"
  printf 'branch_sync_skill=%s\n' "$branch_sync_skill"
  printf 'branch_sync_skill_state=%s\n' "$branch_sync_skill_state"
  exit 0
fi

if [[ $auth_state == required ]]; then
  echo 'Codex authentication is required; starting device-code login.'
  "$codex_bin" login --device-auth
  if [[ -f $HOME/.codex/auth.json ]]; then
    chmod 0600 "$HOME/.codex/auth.json"
  fi
fi

cd "$requested_real"
exec "$codex_bin" "$@"
