#!/usr/bin/env bash
# Host profile topology modes: ingress (cloudflare-tunnel | ssh-forward),
# egress (wireguard | host), `remote-dev init`, and the SSH dashboard forward.
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fixture_profiles=$repo_root/tests/fixtures/host-profiles
export REMOTE_DEV_PROFILE_DIR=$fixture_profiles
unset REMOTE_DEV_PROFILE

fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
fake_bin=$fixture/bin
command_log=$fixture/commands.log
mkdir -p "$fake_bin" "$fixture/home"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

assert_refused() {
  local expected=$1
  shift
  local output status
  set +e
  output=$("$@" 2>&1 </dev/null)
  status=$?
  set -e
  if (( status == 0 )) || [[ "$output" != *"$expected"* ]]; then
    fail "expected refusal containing '$expected', got status $status: $output"
  fi
}

# Load and fully validate one profile from a profile directory.
validate_profile() {
  local directory=$1 name=$2
  HOME=$fixture/home REMOTE_DEV_PROFILE_DIR=$directory bash -c '
    set -euo pipefail
    . "$1/deploy/host-profiles/load.sh"
    remote_dev_load_profile "$1" "$2"
    remote_dev_validate_profile_values
  ' _ "$repo_root" "$name"
}

# Write a variant of a fixture profile under a new name into $fixture/<dir>.
# Arguments after the name are sed expressions applied to the copy.
variant() {
  local directory=$fixture/$1 source=$2 name=$3
  shift 3
  local -a expressions=(-e "s/^REMOTE_DEV_PROFILE_NAME=.*/REMOTE_DEV_PROFILE_NAME=$name/")
  local expression
  for expression in "$@"; do
    expressions+=(-e "$expression")
  done
  mkdir -p "$directory"
  sed "${expressions[@]}" "$fixture_profiles/$source.env" > "$directory/$name.env"
}

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf '\''ssh %s\n'\'' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  'cat >/dev/null' \
  'exit 0' > "$fake_bin/ssh"
chmod +x "$fake_bin/ssh"
fake_env=(HOME="$fixture/home" REMOTE_DEV_TEST_COMMAND_LOG="$command_log" PATH="$fake_bin:$PATH")

# Both fixture topologies load and validate.
validate_profile "$fixture_profiles" example-profile || fail 'example-profile does not validate'
validate_profile "$fixture_profiles" example-direct || fail 'example-direct does not validate'

direct_profile=$("$repo_root/bin/remote-dev" --profile example-direct profile)
for marker in \
  'profile=example-direct' \
  'ingress=ssh-forward' \
  'egress=host' \
  'ssh_host=example-direct' \
  'coder_access_url=http://127.0.0.1:3080' \
  'coder_host_port=3080' \
  'cloudflare_tunnel_id=<unset>' \
  'wireguard_address=<unset>' \
  'egress_subnet=10.232.10.0/24'; do
  [[ $direct_profile == *"$marker"* ]] || fail "example-direct profile output is missing: $marker"
done

# Missing mode fields keep the original topology.
variant modes example-profile legacy-modes '/^REMOTE_DEV_PROFILE_INGRESS=/d' '/^REMOTE_DEV_PROFILE_EGRESS=/d'
legacy_profile=$(REMOTE_DEV_PROFILE_DIR=$fixture/modes "$repo_root/bin/remote-dev" --profile legacy-modes profile)
[[ $legacy_profile == *'ingress=cloudflare-tunnel'* && $legacy_profile == *'egress=wireguard'* ]] || \
  fail "profile without mode fields did not default to cloudflare-tunnel/wireguard: $legacy_profile"

# ssh-forward publishes nothing: the access URL must be plain HTTP on loopback.
variant ingress example-direct direct-https \
  's#^REMOTE_DEV_PROFILE_CODER_ACCESS_URL=.*#REMOTE_DEV_PROFILE_CODER_ACCESS_URL=https://127.0.0.1:3080#'
assert_refused 'ssh-forward requires REMOTE_DEV_CODER_ACCESS_URL=http://127.0.0.1:<local-port>' \
  validate_profile "$fixture/ingress" direct-https
variant ingress example-direct direct-public \
  's#^REMOTE_DEV_PROFILE_CODER_ACCESS_URL=.*#REMOTE_DEV_PROFILE_CODER_ACCESS_URL=http://coder.example.invalid:3080#'
assert_refused 'ssh-forward requires REMOTE_DEV_CODER_ACCESS_URL=http://127.0.0.1:<local-port>' \
  validate_profile "$fixture/ingress" direct-public
variant ingress example-direct direct-no-port \
  's#^REMOTE_DEV_PROFILE_CODER_ACCESS_URL=.*#REMOTE_DEV_PROFILE_CODER_ACCESS_URL=http://127.0.0.1#'
assert_refused 'ssh-forward requires REMOTE_DEV_CODER_ACCESS_URL=http://127.0.0.1:<local-port>' \
  validate_profile "$fixture/ingress" direct-no-port

# Unknown modes are refused when the profile is loaded.
variant modes example-direct unknown-ingress 's/^REMOTE_DEV_PROFILE_INGRESS=.*/REMOTE_DEV_PROFILE_INGRESS=public-port/'
assert_refused 'unsupported ingress' \
  env REMOTE_DEV_PROFILE_DIR="$fixture/modes" "$repo_root/bin/remote-dev" --profile unknown-ingress profile
variant modes example-direct unknown-egress 's/^REMOTE_DEV_PROFILE_EGRESS=.*/REMOTE_DEV_PROFILE_EGRESS=vpn/'
assert_refused 'unsupported egress' \
  env REMOTE_DEV_PROFILE_DIR="$fixture/modes" "$repo_root/bin/remote-dev" --profile unknown-egress profile

# Host egress needs no WireGuard or policy-table fields; WireGuard egress does.
for field in WIREGUARD_ADDRESS WIREGUARD_PUBLIC_KEY WIREGUARD_INTERFACE POLICY_TABLE; do
  ! grep -q "^REMOTE_DEV_PROFILE_$field=" "$fixture_profiles/example-direct.env" || \
    fail "example-direct unexpectedly sets $field"
done
: > "$command_log"
env "${fake_env[@]}" "$repo_root/bin/remote-dev" --profile example-direct doctor </dev/null >/dev/null || \
  fail 'doctor refused a host-egress profile without WireGuard fields'
grep -Fq 'ssh ' "$command_log" || fail 'doctor did not reach the host-egress profile host'
variant egress example-direct direct-wireguard 's/^REMOTE_DEV_PROFILE_EGRESS=.*/REMOTE_DEV_PROFILE_EGRESS=wireguard/'
: > "$command_log"
assert_refused 'profile direct-wireguard is incomplete for doctor; set: REMOTE_DEV_WIREGUARD_ADDRESS REMOTE_DEV_WIREGUARD_PUBLIC_KEY REMOTE_DEV_WIREGUARD_INTERFACE' \
  env "${fake_env[@]}" REMOTE_DEV_PROFILE_DIR="$fixture/egress" \
  "$repo_root/bin/remote-dev" --profile direct-wireguard doctor
[[ ! -s $command_log ]] || fail 'doctor contacted the host before refusing an incomplete WireGuard profile'
assert_refused 'REMOTE_DEV_POLICY_TABLE' \
  env "${fake_env[@]}" REMOTE_DEV_PROFILE_DIR="$fixture/egress" \
  "$repo_root/bin/remote-dev" --profile direct-wireguard doctor

# forward with ssh-forward ingress keeps a loopback-only dashboard tunnel.
: > "$command_log"
forward_output=$(env "${fake_env[@]}" "$repo_root/bin/remote-dev" --profile example-direct forward </dev/null)
[[ $forward_output == *'Forwarding http://127.0.0.1:3080 to Coder on example-direct'* ]] || \
  fail "forward did not announce the local dashboard URL: $forward_output"
expected_forward='ssh -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127.0.0.1:3080:127.0.0.1:3080 example-direct'
[[ $(<"$command_log") == "$expected_forward" ]] || \
  fail "unexpected forward command: $(<"$command_log")"
variant forward example-direct split-ports \
  's#^REMOTE_DEV_PROFILE_CODER_ACCESS_URL=.*#REMOTE_DEV_PROFILE_CODER_ACCESS_URL=http://127.0.0.1:13080#' \
  's#^REMOTE_DEV_PROFILE_CODER_HOST_PORT=.*#REMOTE_DEV_PROFILE_CODER_HOST_PORT=3081#'
: > "$command_log"
env "${fake_env[@]}" REMOTE_DEV_PROFILE_DIR="$fixture/forward" \
  "$repo_root/bin/remote-dev" --profile split-ports forward </dev/null >/dev/null
grep -Fxq 'ssh -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes -N -L 127.0.0.1:13080:127.0.0.1:3081 example-direct' \
  "$command_log" || fail "forward did not map the local port to the host port: $(<"$command_log")"
assert_refused 'select a host profile with --profile <name> or REMOTE_DEV_PROFILE' \
  env "${fake_env[@]}" "$repo_root/bin/remote-dev" forward

# init writes a minimal ssh-forward/host-egress profile and never overwrites.
init_dir=$fixture/init
init_output=$(REMOTE_DEV_PROFILE_DIR=$init_dir "$repo_root/bin/remote-dev" init new-server --ssh-host new-server-alias </dev/null)
[[ $init_output == *"$init_dir/new-server.env"* ]] || fail "init did not report the profile path: $init_output"
[[ -f $init_dir/new-server.env ]] || fail 'init did not write the profile'
validate_profile "$init_dir" new-server || fail 'init wrote a profile that does not validate'
init_profile=$(REMOTE_DEV_PROFILE_DIR=$init_dir "$repo_root/bin/remote-dev" --profile new-server profile)
for marker in \
  'profile=new-server' \
  'ingress=ssh-forward' \
  'egress=host' \
  'ssh_host=new-server-alias' \
  'coder_access_url=http://127.0.0.1:3080' \
  'coder_host_port=3080' \
  'egress_subnet=10.250.10.0/24'; do
  [[ $init_profile == *"$marker"* ]] || fail "init profile is missing: $marker"
done
grep -Fxq 'REMOTE_DEV_PROFILE_CONTROL_SUBNET=10.250.11.0/24' "$init_dir/new-server.env" || \
  fail 'init profile has an unexpected control subnet'
before=$(shasum -a 256 "$init_dir/new-server.env")
assert_refused 'profile already exists' \
  env REMOTE_DEV_PROFILE_DIR="$init_dir" "$repo_root/bin/remote-dev" init new-server --ssh-host other-alias
[[ $(shasum -a 256 "$init_dir/new-server.env") == "$before" ]] || fail 'init modified an existing profile'

assert_refused 'generated profile did not validate' \
  env REMOTE_DEV_PROFILE_DIR="$init_dir" "$repo_root/bin/remote-dev" init bad-subnet \
  --ssh-host bad-subnet --egress-subnet 10.250.20.0/33
[[ ! -e $init_dir/bad-subnet.env ]] || fail 'init left a profile behind after an invalid subnet'
assert_refused 'init requires --ssh-host <alias>' \
  env REMOTE_DEV_PROFILE_DIR="$init_dir" "$repo_root/bin/remote-dev" init no-alias
[[ ! -e $init_dir/no-alias.env ]] || fail 'init left a profile behind without an SSH alias'
assert_refused 'Usage:' \
  env REMOTE_DEV_PROFILE_DIR="$init_dir" "$repo_root/bin/remote-dev" init bad-flag \
  --ssh-host bad-flag --not-a-flag
[[ ! -e $init_dir/bad-flag.env ]] || fail 'init left a profile behind after an invalid flag'
leftovers=$(find "$init_dir" -mindepth 1 ! -name new-server.env)
[[ -z $leftovers ]] || fail "init left temporary files behind: $leftovers"

printf 'PASS     host profile ingress/egress modes, init, and SSH forward are enforced\n'
