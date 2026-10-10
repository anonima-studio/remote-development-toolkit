#!/usr/bin/env bash
set -euo pipefail

# Live integration test: it writes a synthetic file into a real Coder
# workspace. There are no defaults; the operator names both targets.
usage() {
  printf 'Usage: %s <profile> <workspace>\n' "${0##*/}" >&2
  printf '       REMOTE_DEV_PROFILE=<profile> REMOTE_DEV_SECRET_TEST_WORKSPACE=<workspace> %s\n' "${0##*/}" >&2
}
if (( $# > 2 )); then
  usage
  exit 2
fi
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
profile=${1:-${REMOTE_DEV_PROFILE:-}}
workspace=${2:-${REMOTE_DEV_SECRET_TEST_WORKSPACE:-}}
if [[ -z $profile || -z $workspace ]]; then
  printf 'ERROR    the live secrets smoke needs an explicit host profile and Coder workspace\n' >&2
  usage
  exit 2
fi
coder_bin="${REMOTE_DEV_CODER_BIN:-coder}"
coder_config_root=${REMOTE_DEV_CODEX_DESKTOP_CONFIG_ROOT:-$HOME/Library/Application Support/remote-dev/coder}
coder_command=("$coder_bin" --global-config "$coder_config_root/$profile")
# shellcheck disable=SC1091
. "$repo_root/deploy/host-profiles/load.sh"
remote_dev_load_profile "$repo_root" "$profile"
remote_dev_validate_profile_values
coder_url=$REMOTE_DEV_CODER_ACCESS_URL
unset REMOTE_DEV_CODER_URL REMOTE_DEV_CODER_ACCESS_URL
test_id="remote-dev-secret-smoke-$$"
remote_root="/home/coder/projects/$test_id"
local_fixture=$(mktemp -d)
manifest="$local_fixture/secrets.manifest.tsv"
source_file="$local_fixture/repo/smoke.env"

cleanup() {
  "${coder_command[@]}" --url "$coder_url" ssh --disable-autostart=true \
    -e "REMOTE_DEV_SECRET_TEST_ROOT=$remote_root" \
    "$workspace" -- '
      set -euo pipefail
      root=$REMOTE_DEV_SECRET_TEST_ROOT
      [[ "$root" == /home/coder/projects/remote-dev-secret-smoke-* ]]
      rm -rf -- "$root"
    ' >/dev/null 2>&1 || true
  rm -rf -- "$local_fixture"
}
trap cleanup EXIT

mkdir -p "$local_fixture/repo"
printf '%s\n' 'REMOTE_DEV_SMOKE=synthetic-v1' > "$source_file"
chmod 0600 "$source_file"
printf 'workspace\t%s\nlocal_root\t%s\nremote_root\t%s\nfile\t%s\t%s\n' \
  "$workspace" \
  "$local_fixture" \
  "$remote_root" \
  'repo/smoke.env' \
  'repo/smoke.env' > "$manifest"

"${coder_command[@]}" --url "$coder_url" ssh --disable-autostart=true \
  -e "REMOTE_DEV_SECRET_TEST_ROOT=$remote_root" \
  "$workspace" -- '
    set -euo pipefail
    root=$REMOTE_DEV_SECRET_TEST_ROOT
    [[ "$root" == /home/coder/projects/remote-dev-secret-smoke-* ]]
    mkdir -p "$root/repo"
  '

if "$repo_root/bin/remote-dev" --profile "$profile" secrets apply "$manifest" --confirm wrong-workspace >/dev/null 2>&1; then
  echo 'expected exact workspace confirmation to be required' >&2
  exit 1
fi

"$repo_root/bin/remote-dev" --profile "$profile" secrets plan "$manifest"
"$repo_root/bin/remote-dev" --profile "$profile" secrets apply "$manifest" \
  --confirm "$workspace" \
  --confirm-profile "$profile" \
  --confirm-origin "$coder_url"
"$repo_root/bin/remote-dev" --profile "$profile" secrets verify "$manifest"

if "$repo_root/bin/remote-dev" --profile "$profile" secrets apply "$manifest" \
  --confirm "$workspace" \
  --confirm-profile "$profile" \
  --confirm-origin "$coder_url" >/dev/null 2>&1; then
  echo 'expected an existing destination to be refused without --replace' >&2
  exit 1
fi

printf '%s\n' 'REMOTE_DEV_SMOKE=synthetic-v2' > "$source_file"
"$repo_root/bin/remote-dev" --profile "$profile" secrets apply "$manifest" \
  --confirm "$workspace" \
  --confirm-profile "$profile" \
  --confirm-origin "$coder_url" \
  --replace
"$repo_root/bin/remote-dev" --profile "$profile" secrets verify "$manifest"

printf 'PASS     secret transfer smoke completed without real secret material\n'
