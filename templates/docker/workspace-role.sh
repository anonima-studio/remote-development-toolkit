#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remote-dev-workspace-role show
       remote-dev-workspace-role require <development|runner>

Read the immutable workspace-role marker created by the Coder template and
fail closed when a managed command is routed to the wrong workspace role.
EOF
}

role_marker=${REMOTE_DEV_WORKSPACE_ROLE_MARKER:-$HOME/.local/share/remote-dev/workspace-role}

read_role() {
  local value bytes expected_bytes
  if [[ -L $role_marker || ! -f $role_marker ]]; then
    printf 'WORKSPACE_ROLE_INVALID marker must be a regular non-symlink file: %s\n' \
      "$role_marker" >&2
    return 1
  fi
  IFS= read -r value < "$role_marker" || {
    printf 'WORKSPACE_ROLE_INVALID marker must end with exactly one newline: %s\n' \
      "$role_marker" >&2
    return 1
  }
  bytes=$(wc -c < "$role_marker")
  bytes=${bytes//[[:space:]]/}
  expected_bytes=$((${#value} + 1))
  if [[ $bytes != "$expected_bytes" || \
        ( $value != development && $value != runner ) ]]; then
    printf 'WORKSPACE_ROLE_INVALID marker has an unsupported or non-exact value: %s\n' \
      "$role_marker" >&2
    return 1
  fi
  printf '%s\n' "$value"
}

command=${1:-}
case "$command" in
  show)
    (( $# == 1 )) || { usage >&2; exit 2; }
    read_role
    ;;
  require)
    (( $# == 2 )) || { usage >&2; exit 2; }
    expected=$2
    [[ $expected == development || $expected == runner ]] || {
      printf 'invalid required workspace role: %s\n' "$expected" >&2
      exit 2
    }
    actual=$(read_role)
    if [[ $actual != "$expected" ]]; then
      printf 'WORKSPACE_ROLE_MISMATCH expected=%s actual=%s\n' \
        "$expected" "$actual" >&2
      printf 'Recovery Use a dedicated %s workspace; workspace roles cannot be changed in place.\n' \
        "$expected" >&2
      exit 1
    fi
    printf 'workspace_role=%s\n' "$actual"
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
