#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remote-dev-worktree-vars --source-tree <path> --worktree <path> [--replace]

Synchronize the exact files declared by the source tree's
.remote-dev/worktree-vars.tsv into another linked Git worktree. The manifest is
non-secret and contains one tab-separated source and destination path per line.
File contents are never printed.

Options:
  --source-tree <path>  Existing source Git worktree below ~/projects
  --worktree <path>     Target linked Git worktree below ~/projects
  --replace             Replace a differing regular target file
  -h, --help            Show this help
EOF
}

source_tree=
worktree=
replace=false
while (( $# > 0 )); do
  case "$1" in
    --source-tree)
      (( $# >= 2 )) || { echo '--source-tree requires a path' >&2; exit 2; }
      source_tree=$2
      shift 2
      ;;
    --worktree)
      (( $# >= 2 )) || { echo '--worktree requires a path' >&2; exit 2; }
      worktree=$2
      shift 2
      ;;
    --replace)
      replace=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown remote-dev-worktree-vars option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

role_helper=${REMOTE_DEV_WORKSPACE_ROLE_HELPER:-/usr/local/bin/remote-dev-workspace-role}
"$role_helper" require development >/dev/null

[[ -n $source_tree ]] || { echo '--source-tree is required' >&2; exit 2; }
[[ -n $worktree ]] || { echo '--worktree is required' >&2; exit 2; }

projects_root=${REMOTE_DEV_PROJECTS_ROOT:-$HOME/projects}
mkdir -p "$projects_root"
projects_real=$(realpath "$projects_root")

resolve_tree() {
  local value=$1
  local label=$2
  local requested resolved
  if [[ $value = /* ]]; then
    requested=$value
  else
    requested=$projects_real/$value
  fi
  if [[ ! -d $requested ]]; then
    printf '%s does not exist: %s\n' "$label" "$requested" >&2
    return 2
  fi
  resolved=$(realpath "$requested")
  case "$resolved/" in
    "$projects_real/"*) ;;
    *)
      printf '%s must stay below %s: %s\n' "$label" "$projects_real" "$resolved" >&2
      return 2
      ;;
  esac
  printf '%s\n' "$resolved"
}

source_real=$(resolve_tree "$source_tree" 'source tree')
worktree_real=$(resolve_tree "$worktree" 'target worktree')
if [[ $source_real == "$worktree_real" ]]; then
  echo 'source tree and target worktree must be different' >&2
  exit 2
fi

git_top() {
  local tree=$1
  local label=$2
  local top
  if ! top=$(git -C "$tree" rev-parse --show-toplevel 2>/dev/null); then
    printf '%s is not a Git worktree: %s\n' "$label" "$tree" >&2
    return 2
  fi
  realpath "$top"
}

git_common_dir() {
  local tree=$1
  local common
  common=$(git -C "$tree" rev-parse --git-common-dir)
  if [[ $common = /* ]]; then
    realpath "$common"
  else
    realpath "$tree/$common"
  fi
}

source_top=$(git_top "$source_real" 'source tree')
worktree_top=$(git_top "$worktree_real" 'target worktree')
if [[ $source_top != "$source_real" || $worktree_top != "$worktree_real" ]]; then
  cat >&2 <<EOF
Variable synchronization requires Git worktree roots.
Source root: $source_top
Target root: $worktree_top
EOF
  exit 2
fi
source_common=$(git_common_dir "$source_real")
worktree_common=$(git_common_dir "$worktree_real")
if [[ $source_common != "$worktree_common" ]]; then
  cat >&2 <<EOF
Source and target do not belong to the same linked-worktree repository.
Source: $source_real
Target: $worktree_real
EOF
  exit 2
fi

manifest=$source_real/.remote-dev/worktree-vars.tsv
if [[ ! -f $manifest || -L $manifest ]]; then
  cat >&2 <<EOF
Missing regular non-symlink worktree variable manifest: $manifest
Recovery add exact source<TAB>destination routes to the source tree, then rerun.
EOF
  exit 2
fi

valid_relative_path() {
  local value=$1
  local segment
  local -a segments=()
  [[ -n $value && $value != /* && $value != */ && $value != *//* ]] || return 1
  IFS=/ read -r -a segments <<<"$value"
  for segment in "${segments[@]}"; do
    [[ $segment != . && $segment != .. && $segment =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  done
}

declare -a source_paths=()
declare -a destination_paths=()
destinations_seen=$'\n'
line_number=0
while IFS=$'\t' read -r source_path destination_path extra || \
      [[ -n ${source_path:-}${destination_path:-}${extra:-} ]]; do
  line_number=$((line_number + 1))
  source_path=${source_path%$'\r'}
  destination_path=${destination_path%$'\r'}
  extra=${extra%$'\r'}
  [[ -n $source_path ]] || continue
  [[ $source_path == \#* ]] && continue
  if [[ -z $destination_path || -n $extra ]] || \
     ! valid_relative_path "$source_path" || \
     ! valid_relative_path "$destination_path"; then
    printf 'invalid worktree variable route at %s:%d\n' "$manifest" "$line_number" >&2
    exit 2
  fi
  if [[ $destinations_seen == *$'\n'$destination_path$'\n'* ]]; then
    printf 'duplicate worktree variable destination at %s:%d: %s\n' \
      "$manifest" "$line_number" "$destination_path" >&2
    exit 2
  fi
  destinations_seen+=$destination_path$'\n'
  source_paths+=("$source_path")
  destination_paths+=("$destination_path")
done < "$manifest"

if (( ${#source_paths[@]} == 0 )); then
  printf 'worktree variable manifest has no routes: %s\n' "$manifest" >&2
  exit 2
fi

for left_index in "${!destination_paths[@]}"; do
  for right_index in "${!destination_paths[@]}"; do
    (( left_index == right_index )) && continue
    if [[ ${destination_paths[$right_index]} == "${destination_paths[$left_index]}/"* ]]; then
      printf 'worktree variable destinations overlap: %s and %s\n' \
        "${destination_paths[$left_index]}" "${destination_paths[$right_index]}" >&2
      exit 2
    fi
  done
done

for source_path in "${source_paths[@]}"; do
  source_candidate=$source_real/$source_path
  if [[ ! -f $source_candidate || -L $source_candidate ]]; then
    printf 'worktree variable source must be a regular non-symlink file: %s\n' \
      "$source_candidate" >&2
    exit 2
  fi
  source_resolved=$(realpath "$source_candidate")
  case "$source_resolved/" in
    "$source_real/"*) ;;
    *)
      printf 'worktree variable source escapes its Git worktree: %s\n' \
        "$source_candidate" >&2
      exit 2
      ;;
  esac
done

validate_destination_path() {
  local relative=$1
  local current=$worktree_real
  local component
  local component_index
  local -a components=()
  IFS=/ read -r -a components <<<"$relative"
  for ((component_index = 0; component_index < ${#components[@]} - 1; component_index++)); do
    component=${components[$component_index]}
    current=$current/$component
    if [[ -L $current ]]; then
      printf 'refusing symlinked worktree variable destination parent: %s\n' \
        "$current" >&2
      return 2
    fi
    if [[ -e $current && ! -d $current ]]; then
      printf 'worktree variable destination parent is not a directory: %s\n' \
        "$current" >&2
      return 2
    fi
  done
  local destination=$worktree_real/$relative
  if [[ -L $destination || ( -e $destination && ! -f $destination ) ]]; then
    printf 'refusing non-regular worktree variable destination: %s\n' \
      "$destination" >&2
    return 2
  fi
}

declare -a destination_states=()
for index in "${!source_paths[@]}"; do
  source_path=$source_real/${source_paths[$index]}
  destination_path=$worktree_real/${destination_paths[$index]}
  validate_destination_path "${destination_paths[$index]}" || exit $?
  if [[ -f $destination_path ]] && cmp -s "$source_path" "$destination_path"; then
    destination_states+=(unchanged)
  elif [[ -f $destination_path && $replace == false ]]; then
    cat >&2 <<EOF
Refusing to replace a differing worktree variable file: $destination_path
Recovery inspect the target, then rerun with --replace if the source must win.
EOF
    exit 1
  else
    destination_states+=(copy)
  fi
done

copied=0
unchanged=0
temp_path=
cleanup() {
  [[ -z $temp_path ]] || rm -f -- "$temp_path"
}
trap cleanup EXIT

for index in "${!source_paths[@]}"; do
  source_path=$source_real/${source_paths[$index]}
  destination_path=$worktree_real/${destination_paths[$index]}
  if [[ ${destination_states[$index]} == unchanged ]]; then
    chmod 0600 "$destination_path"
    unchanged=$((unchanged + 1))
    continue
  fi
  destination_parent=$(dirname "$destination_path")
  mkdir -p "$destination_parent"
  parent_real=$(realpath "$destination_parent")
  case "$parent_real/" in
    "$worktree_real/"*) ;;
    *)
      printf 'worktree variable destination escapes its Git worktree: %s\n' \
        "$destination_path" >&2
      exit 2
      ;;
  esac
  temp_path=$(mktemp "$destination_parent/.remote-dev-worktree-vars.XXXXXX")
  install -m 0600 "$source_path" "$temp_path"
  mv -f "$temp_path" "$destination_path"
  temp_path=
  copied=$((copied + 1))
done

cat <<EOF
READY    Worktree variables are synchronized without printing their values.
Source   $source_real
Target   $worktree_real
Files    copied=$copied unchanged=$unchanged
Recovery rerun with --replace only after reviewing a differing target file.
EOF
