#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remote-dev-project-setup --profile <name> --workspace <name> \
  --project-dir <relative-path>

Validate and run the project-owned .remote-dev/project.env contract, wait for
its loopback health URL, and print the Mac-visible Coder Connect and preview
entry points.
EOF
}

profile=
workspace=
project_dir=
while (( $# > 0 )); do
  case "$1" in
    --profile)
      (( $# >= 2 )) || { echo '--profile requires a name' >&2; exit 2; }
      profile=$2
      shift 2
      ;;
    --workspace)
      (( $# >= 2 )) || { echo '--workspace requires a name' >&2; exit 2; }
      workspace=$2
      shift 2
      ;;
    --project-dir)
      (( $# >= 2 )) || { echo '--project-dir requires a relative path' >&2; exit 2; }
      project_dir=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unexpected project setup argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

role_helper=${REMOTE_DEV_WORKSPACE_ROLE_HELPER:-/usr/local/bin/remote-dev-workspace-role}
"$role_helper" require development >/dev/null

[[ $profile =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  printf 'invalid Remote Dev profile name: %s\n' "$profile" >&2
  exit 2
}
[[ $workspace =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  printf 'invalid Coder workspace name: %s\n' "$workspace" >&2
  exit 2
}
[[ -n $project_dir && $project_dir != /* ]] || {
  printf 'project directory must be relative to ~/projects: %s\n' "$project_dir" >&2
  exit 2
}
IFS=/ read -r -a project_segments <<< "$project_dir"
for segment in "${project_segments[@]}"; do
  [[ $segment =~ ^[A-Za-z0-9._-]+$ && $segment != . && $segment != .. ]] || {
    printf 'project directory contains an unsafe segment: %s\n' "$project_dir" >&2
    exit 2
  }
done

projects_root=${REMOTE_DEV_PROJECTS_ROOT:-$HOME/projects}
[[ -d $projects_root ]] || {
  printf 'projects root does not exist: %s\n' "$projects_root" >&2
  exit 1
}
projects_root=$(realpath "$projects_root")
project_path=$projects_root/$project_dir
[[ -d $project_path ]] || {
  printf 'project directory does not exist: %s\n' "$project_path" >&2
  exit 1
}
project_path=$(realpath "$project_path")
mode_guard=${REMOTE_DEV_MODE_GUARD_HELPER:-/usr/local/bin/remote-dev-mode-guard}
python3 "$mode_guard" "$project_path"
case "$project_path/" in
  "$projects_root/"*) ;;
  *)
    printf 'project directory escapes the projects root: %s\n' "$project_path" >&2
    exit 1
    ;;
esac

version=
setup_command=
start_command=
health_url=
browser_port=
coder_hostname=
contract_helper=${REMOTE_DEV_PROJECT_CONTRACT_HELPER:-/usr/local/bin/remote-dev-project-contract}
if [[ ! -f $contract_helper || -L $contract_helper ]]; then
  printf 'Current project contract helper is missing. Update the Remote Dev workspace image.\n' >&2
  exit 1
fi
contract_values=$(python3 "$contract_helper" "$project_path" --format env)
while IFS= read -r line; do
  [[ -n $line ]] || continue
  # The shared parser validates the complete key allowlist. No shell evaluation.
  printf -v "${line%%=*}" '%s' "${line#*=}"
done <<< "$contract_values"
browser_port=$((10#$browser_port))

export REMOTE_DEV_PROFILE_NAME=$profile
export REMOTE_DEV_WORKSPACE_NAME=$workspace
export REMOTE_DEV_PROJECT_DIR=$project_path
export REMOTE_DEV_CODER_HOSTNAME=$coder_hostname

run_phase() {
  local phase=$1
  local command=$2
  printf 'PHASE    %s\n' "$phase"
  local status=0
  if (cd "$project_path" && bash -lc "$command"); then
    return 0
  else
    status=$?
  fi
  printf 'Project %s phase failed in %s (exit %s).\n' \
    "$phase" "$project_path" "$status" >&2
  printf 'Recovery Fix the project-owned command, then rerun project-setup.\n' >&2
  return "$status"
}

run_phase setup "$setup_command"
run_phase start "$start_command"

health_timeout=${REMOTE_DEV_PROJECT_HEALTH_TIMEOUT_SECONDS:-120}
[[ $health_timeout =~ ^[0-9]+$ ]] && (( health_timeout >= 1 )) || {
  printf 'invalid REMOTE_DEV_PROJECT_HEALTH_TIMEOUT_SECONDS: %s\n' \
    "$health_timeout" >&2
  exit 2
}
deadline=$((SECONDS + health_timeout))
http_status=000
while (( SECONDS < deadline )); do
  http_status=$(curl \
    --noproxy 127.0.0.1,localhost \
    --silent \
    --output /dev/null \
    --max-time 2 \
    --write-out '%{http_code}' \
    "$health_url" 2>/dev/null || true)
  if [[ $http_status =~ ^2[0-9][0-9]$ ]]; then
    break
  fi
  sleep 1
done
if [[ ! $http_status =~ ^2[0-9][0-9]$ ]]; then
  cat >&2 <<EOF
Project health did not become ready within ${health_timeout}s: $health_url
Recovery:
  1. In $project_path, inspect the project-owned start command and service logs.
  2. Verify the declared loopback health URL, then rerun project-setup.
EOF
  exit 1
fi

cat <<EOF

READY    Project setup completed and health is HTTP $http_status.
Project  $project_path
Health   $health_url
Port     $browser_port
EOF
if [[ -n $coder_hostname ]]; then
  printf 'Open     http://%s:%s\n' "$coder_hostname" "$browser_port"
  printf 'Recovery If the hostname does not resolve, toggle Coder Connect off and on.\n'
else
  cat <<EOF
Open     UNAVAILABLE: project contract does not declare coder_hostname.
Recovery Add a verified .coder hostname or configure the SSH/Desktop route:
         bin/remote-dev --profile $profile codex-desktop $workspace --project-dir $project_dir
EOF
fi
cat <<EOF
Fallback bin/remote-dev --profile $profile preview $workspace $browser_port
Restart  After a workspace restart, refresh the same URL. If project health did
         not return, rerun this project-setup command.
EOF
