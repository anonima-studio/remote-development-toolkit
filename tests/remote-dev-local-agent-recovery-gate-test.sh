#!/usr/bin/env bash
# The remote recovery gate runs before the local operation lock, so a start or
# stop transaction may promote or remove a handle while the gate inspects it.
# These cases pin the settle behaviour: a handle whose files disappear during
# the settle is not recovery ownership; persistent files still block.
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
tool=$repo_root/bin/remote-dev-local-agent
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT

# Extract the single-quoted remote script from remote_recovery_gate exactly as
# bash would after quote removal.
awk '
  /^remote_recovery_gate\(\) \{/ { in_function = 1; next }
  in_function && /^  "\$transport" '\''$/ { in_script = 1; next }
  in_script && /^'\'' "\$\{remote_control%\/\*\}"/ { exit }
  in_script { print }
' "$tool" | sed "s/'\"'\"'/'/g" > "$fixture/gate.sh"
grep -q 'PROCESS_RECOVERY_GATE' "$fixture/gate.sh" || {
  echo 'could not extract the recovery gate script from remote-dev-local-agent' >&2
  exit 1
}

controls=$fixture/controls
recovery=$controls/demo/processes/recovery
mkdir -p "$recovery"
handle=proc-20260911T000000-1-1
write_record() {
  printf 'schema\t2\nowner_id\towner\nproject_id\tdemo\nprofile\tp\nworkspace\tw\nroot_hash\tr\nremote_root\t/x\nhandle\t%s\nidentity_file\t%s\nrecovery_phase\tidentity-pending\nsupervisor_state\tnot-started\n' \
    "$handle" "$recovery/$handle.identity.tsv" > "$recovery/$handle.tsv"
  : > "$recovery/$handle.argv"
  : > "$recovery/$handle.revisions"
}
run_gate() {
  set +e
  output=$(bash "$fixture/gate.sh" "$controls" p w command 2>&1)
  status=$?
  set -e
}
expect() {
  local wanted_status=$1 wanted_output=$2 name=$3
  if [[ $status != "$wanted_status" || $output != *"$wanted_output"* ]]; then
    printf 'FAIL %s: status=%s output=%s\n' "$name" "$status" "$output" >&2
    exit 1
  fi
  printf 'PASS %s\n' "$name"
}

# A launch that publishes its active record while the gate inspects the
# recovery record must not report recovery ownership.
write_record
( sleep 0.15; rm -f "$recovery/$handle".* ) &
run_gate; wait
expect 0 '' 'record promoted during the settle is ignored'

write_record
run_gate
expect 79 "PROCESS_RECOVERY_GATE: handle=$handle project=demo state=" 'persistent recovery record still gates'
rm -f "$recovery/$handle".*

# Cleanup removes the record before the identity; that window is not an
# orphaned identity.
: > "$recovery/$handle.identity.tsv"
( sleep 0.15; rm -f "$recovery/$handle.identity.tsv" ) &
run_gate; wait
expect 0 '' 'identity removed during the settle is ignored'

: > "$recovery/$handle.identity.tsv"
run_gate
expect 79 'state=orphan-identity' 'persistent orphaned identity still gates'
rm -f "$recovery/$handle.identity.tsv"

started=$(date +%s)
run_gate
expect 0 '' 'empty recovery directory passes'
(( $(date +%s) - started < 2 )) || { echo 'FAIL empty recovery directory must not settle' >&2; exit 1; }
echo 'PASS     recovery gate tolerates handles that finish their transition mid-scan'
