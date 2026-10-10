#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Synthetic host profiles; tests never read the deployment profiles.
export REMOTE_DEV_PROFILE_DIR="$repo_root/tests/fixtures/host-profiles"
help_output=$($repo_root/bin/remote-dev --help)
workspace_image_hash=cf2a26a364ef23228b22b3cdcd2dc5012ca6f154fa0355542de9532fe60ea962
workspace_image_tag=remote-dev-workspace:node24-20260906-cf2a26a364e

for marker in 'Usage: remote-dev [--profile <name>] <command>' init quickstart profile install doctor status audit codex workspace local-agent worktree-vars credentials forward \
  'There is no' 'Keep the Coder dashboard forwarded to this Mac over SSH'; do
  if [[ "$help_output" != *"$marker"* ]]; then
    printf 'missing help marker: %s\n' "$marker" >&2
    exit 1
  fi
done

calculated_workspace_image_hash=$(node -e '
const crypto = require("node:crypto");
const fs = require("node:fs");
const hash = crypto.createHash("sha256");
for (const file of process.argv.slice(1)) hash.update(fs.readFileSync(file));
process.stdout.write(hash.digest("hex"));
' \
  "$repo_root/templates/docker/Dockerfile" \
  "$repo_root/templates/docker/codex-start.sh" \
  "$repo_root/templates/docker/project-setup.sh" \
  "$repo_root/templates/docker/project-contract.py" \
  "$repo_root/templates/docker/mode-guard.py" \
  "$repo_root/templates/docker/worktree-vars.sh" \
  "$repo_root/templates/docker/workspace-role.sh" \
  "$repo_root/templates/docker/managed-unlink.py" \
  "$repo_root/templates/docker/skills/worktree-branch-sync/SKILL.md" \
  "$repo_root/templates/docker/skills/worktree-branch-sync/references/worktree-branch-sync.md" \
  "$repo_root/templates/docker/start-docker.sh")
if [[ $calculated_workspace_image_hash != "$workspace_image_hash" ]]; then
  printf 'workspace image hash mismatch: expected %s, got %s\n' \
    "$workspace_image_hash" "$calculated_workspace_image_hash" >&2
  exit 1
fi
for invariant in \
  "$repo_root/deploy/install.sh:workspace_image_expected_hash=$workspace_image_hash" \
  "$repo_root/deploy/install.sh:workspace_image_tag=$workspace_image_tag" \
  "$repo_root/templates/docker/main.tf:image    = \"$workspace_image_tag\""; do
  invariant_file=${invariant%%:*}
  invariant_value=${invariant#*:}
  if ! grep -Fq "$invariant_value" "$invariant_file"; then
    printf 'missing workspace image invariant in %s: %s\n' \
      "$invariant_file" "$invariant_value" >&2
    exit 1
  fi
done
grep -Fq 'docker image inspect' "$repo_root/deploy/install.sh"
grep -Fq 'id0.remote-dev.workspace-source-sha256' \
  "$repo_root/templates/docker/Dockerfile"

if grep -Fq 'CODER_EXTERNAL_AUTH_GITHUB_DEFAULT_PROVIDER_ENABLE' \
  "$repo_root/deploy/coder/compose.yaml"; then
  echo 'Coder v2.36 misparses the default-provider environment override' >&2
  exit 1
fi
grep -Fq 'data "coder_external_auth" "github" {' \
  "$repo_root/templates/docker/main.tf"
grep -Fq 'id       = "github"' "$repo_root/templates/docker/main.tf"
grep -Fq 'optional = true' "$repo_root/templates/docker/main.tf"
grep -Fq -- '--variable "remote_dev_coder_access_url=$coder_access_url"' \
  "$repo_root/deploy/install.sh"
grep -Fq 'url = "http://coder-control:7080"' \
  "$repo_root/templates/docker/main.tf"
grep -Fq 'urlencode(var.remote_dev_coder_access_url)' \
  "$repo_root/templates/docker/main.tf"
grep -Fq 'default     = "262.9437.145"' \
  "$repo_root/templates/docker/main.tf"
if grep -Fq 'data.coder_workspace.me.access_url' \
  "$repo_root/templates/docker/main.tf"; then
  echo 'desktop links must not inherit the internal Terraform provider URL' >&2
  exit 1
fi
grep -Fq "api/v2/external-auth/github" "$repo_root/bin/remote-dev"
grep -Fq 'and "coolify" in networks' "$repo_root/bin/remote-dev"
grep -Fq 'not ({"remote-dev-control", "remote-dev-egress"} & set(networks))' \
  "$repo_root/bin/remote-dev"
if grep -Fq 'set(networks) == {"coolify"}' "$repo_root/bin/remote-dev"; then
  echo 'doctor must allow Coolify application networks on coolify-proxy' >&2
  exit 1
fi
grep -Fq 'git push --dry-run origin HEAD' \
  "$repo_root/.agents/skills/remote-development/references/project-import.md"
grep -Fq -- '--for-context|--for-root) local_agent_context_bound=true' \
  "$repo_root/bin/remote-dev"
if grep -Fq 'local_agent_root_bound' "$repo_root/bin/remote-dev"; then
  echo 'context-bound Local Agent dispatch must not inject a default profile' >&2
  exit 1
fi
grep -Fq 'https://github.com/apps/coder/installations/new' \
  "$repo_root/.agents/skills/remote-development/references/project-import.md"

set +e
$repo_root/bin/remote-dev definitely-not-a-command >/dev/null 2>&1
status=$?
set -e

if (( status != 2 )); then
  printf 'invalid command exited %s, expected 2\n' "$status" >&2
  exit 1
fi

assert_refused() {
  local expected=$1
  shift
  local output status
  set +e
  output=$("$@" 2>&1)
  status=$?
  set -e
  if (( status == 0 )) || [[ "$output" != *"$expected"* ]]; then
    printf 'expected refusal containing %q, got status %s: %s\n' \
      "$expected" "$status" "$output" >&2
    exit 1
  fi
}

# There is no default deployment: every profile-bound command names one.
for profile_command in profile install doctor status; do
  assert_refused 'select a host profile with --profile <name> or REMOTE_DEV_PROFILE' \
    env -u REMOTE_DEV_PROFILE "$repo_root/bin/remote-dev" "$profile_command"
done
assert_refused 'select a host profile with --profile <name> or REMOTE_DEV_PROFILE' \
  "$repo_root/bin/remote-dev" --profile '' profile

example_profile=$($repo_root/bin/remote-dev --profile example-profile profile)
for marker in \
  'profile=example-profile' \
  'ingress=cloudflare-tunnel' \
  'egress=wireguard' \
  'expected_hostname=example-host' \
  'system_role=dev-server' \
  'display_name=Example dev server' \
  'ssh_host=example-host' \
  'coder_access_url=https://coder.example.invalid' \
  'coolify_access_url=https://coolify.example.invalid'; do
  if [[ "$example_profile" != *"$marker"* ]]; then
    printf 'example profile output is missing: %s\n' "$marker" >&2
    exit 1
  fi
done
[[ "$(env REMOTE_DEV_PROFILE=example-profile "$repo_root/bin/remote-dev" profile)" == "$example_profile" ]]
if [[ "$example_profile" == *'10.232.'* || "$example_profile" == *'example-direct'* ]]; then
  echo 'example-profile output reused the identity of another profile' >&2
  exit 1
fi

assert_refused 'unknown remote-dev profile' \
  env REMOTE_DEV_PROFILE=not-a-profile "$repo_root/bin/remote-dev" profile
assert_refused 'invalid profile name' \
  "$repo_root/bin/remote-dev" --profile '../example-profile' profile
assert_refused 'profile example-profile has immutable identity' \
  env REMOTE_DEV_PROFILE=example-profile REMOTE_DEV_HOST=other-host \
  "$repo_root/bin/remote-dev" profile
assert_refused 'profile example-profile has immutable identity' \
  env REMOTE_DEV_PROFILE=example-profile REMOTE_DEV_HOST=other-host \
  "$repo_root/bin/remote-dev" install
assert_refused 'profile example-profile has immutable identity' \
  env REMOTE_DEV_PROFILE=example-profile REMOTE_DEV_SSH_HOST=other-host \
  "$repo_root/bin/remote-dev" install
assert_refused 'profile example-profile has immutable identity' \
  env REMOTE_DEV_PROFILE=example-profile \
  REMOTE_DEV_CODER_ACCESS_URL=https://other.example.invalid \
  "$repo_root/bin/remote-dev" profile
assert_refused 'profile example-profile has immutable identity' \
  env REMOTE_DEV_PROFILE=example-profile \
  REMOTE_DEV_CLOUDFLARE_TUNNEL_ID=11111111-2222-4333-8444-555555555555 \
  "$repo_root/bin/remote-dev" profile
assert_refused 'profile example-direct has immutable identity' \
  env REMOTE_DEV_PROFILE=example-direct REMOTE_DEV_EGRESS_SUBNET=10.232.20.0/24 \
  "$repo_root/bin/remote-dev" profile
assert_refused 'manifest must be a regular non-symlink file' \
  env REMOTE_DEV_PROFILE=example-profile \
  "$repo_root/bin/remote-dev" secrets plan /does/not/matter

fixture=$(mktemp -d)
fake_bin="$fixture/bin"
command_log="$fixture/commands.log"
manifest="$fixture/secrets.manifest.tsv"
source_file="$fixture/synthetic.env"
cleanup() {
  rm -rf -- "$fixture"
}
trap cleanup EXIT
mkdir -p "$fake_bin"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf '\''ssh %s\n'\'' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  'if [[ "$*" == *remote-dev-profile-preflight* && "${REMOTE_DEV_TEST_PROFILE_MISMATCH:-false}" == true ]]; then' \
  '  cat >/dev/null' \
  '  exit 1' \
  'fi' \
  'if [[ "$*" == *remote-dev-codex* && "${REMOTE_DEV_TEST_CODEX_HELPER_ABSENT:-false}" == true ]]; then' \
  '  cat >/dev/null' \
  '  echo "/bin/bash: line 1: /usr/local/bin/remote-dev-codex: No such file or directory" >&2' \
  '  exit 127' \
  'fi' \
  'if [[ "$*" == *remote-dev-codex* && "${REMOTE_DEV_TEST_CODEX_PREFLIGHT_FAIL:-false}" == true ]]; then' \
  '  cat >/dev/null' \
  '  echo "synthetic Coder SSH failure" >&2' \
  '  exit 17' \
  'fi' \
  'if [[ "$*" == *remote-dev-codex* && "${REMOTE_DEV_TEST_CODEX_OUTER_NOT_FOUND:-false}" == true ]]; then' \
  '  cat >/dev/null' \
  '  echo "docker: command not found" >&2' \
  '  exit 127' \
  'fi' \
  'if [[ "${REMOTE_DEV_TEST_SYSBOX_POLICY_MISSING:-false}" == true && "$*" == *"/etc/apparmor.d/local/fusermount3"* ]]; then' \
  '  cat >/dev/null' \
  '  echo missing' \
  '  exit 0' \
  'fi' \
  'if [[ "${REMOTE_DEV_TEST_SYSBOX_NOT_INSTALLED:-false}" == true && "$*" == *"dpkg-query -W"*sysbox-ce* ]]; then' \
  '  cat >/dev/null' \
  '  exit 1' \
  'fi' \
  'if [[ "${REMOTE_DEV_TEST_SYSBOX_NOT_INSTALLED:-false}" == true && "$*" == *"docker ps --filter"* ]]; then' \
  '  cat >/dev/null' \
  '  exit 1' \
  'fi' \
  'if [[ "${REMOTE_DEV_TEST_SYSBOX_NOT_INSTALLED:-false}" == true && "$*" == *"bash -s --"* ]]; then' \
  '  cat >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  '  exit 0' \
  'fi' \
  'if [[ "${REMOTE_DEV_TEST_HOSTNAME_MISMATCH:-false}" == true && "$*" == *"bash -s -- example-host" ]]; then' \
  '  cat >/dev/null' \
  '  exit 1' \
  'fi' \
  'if [[ "$*" == *"sudo mktemp -d -p /tmp remote-dev-install.XXXXXXXX"* ]]; then' \
  '  printf '\''/tmp/remote-dev-install.TESTSTAGE\n'\''' \
  'fi' \
  'if [[ "$*" == *"sudo install -d -o dev"* && "${REMOTE_DEV_TEST_FAIL_AFTER_STAGE:-false}" == true ]]; then' \
  '  cat >/dev/null' \
  '  exit 1' \
  'fi' \
  'cat >/dev/null' \
  'exit 0' > "$fake_bin/ssh"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf '\''scp %s\n'\'' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  'for argument in "$@"; do' \
  '  if [[ -f "$argument" ]] && grep -q '\''^tunnel: '\'' "$argument"; then' \
  '    sed '\''s/^/rendered-cloudflared /'\'' "$argument" >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  '  fi' \
  'done' \
  'exit 0' > "$fake_bin/scp"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf '\''coder %s\n'\'' "$*" >> "$REMOTE_DEV_TEST_COMMAND_LOG"' \
  'if [[ "${REMOTE_DEV_TEST_WORKSPACE_STOPPED:-false}" == true && "$*" == *" ssh "* ]]; then' \
  '  echo "workspace must be started" >&2' \
  '  exit 1' \
  'fi' \
  'if [[ "$*" == *".local/share/remote-dev/workspace-role"* ]]; then' \
  '  if [[ "${REMOTE_DEV_TEST_WORKSPACE_ROLE:-legacy-development}" == invalid ]]; then' \
  '    exit 1' \
  '  fi' \
  '  printf '\''%s'\'' "${REMOTE_DEV_TEST_WORKSPACE_ROLE:-legacy-development}"' \
  'else' \
  '  printf ABSENT' \
  'fi' > "$fake_bin/coder"
chmod +x "$fake_bin/ssh" "$fake_bin/scp" "$fake_bin/coder"

assert_refused 'codex requires a workspace name' \
  env PATH="$fake_bin:$PATH" "$repo_root/bin/remote-dev" --profile example-profile codex
assert_refused 'invalid Coder workspace name' \
  env PATH="$fake_bin:$PATH" "$repo_root/bin/remote-dev" --profile example-profile codex --unsafe

: > "$command_log"
env \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile codex example-app \
  --check
for marker in \
  'example-host' \
  'docker exec -i' \
  'CODER_URL=http://127.0.0.1:7080' \
  '/usr/local/bin/remote-dev-workspace-role\ require\ development' \
  '/opt/coder ssh --wait=yes example-app -- /usr/local/bin/remote-dev-codex --check'; do
  if ! grep -Fq "$marker" "$command_log"; then
    printf 'Codex workspace launch did not preserve value: %s\n' "$marker" >&2
    exit 1
  fi
done

: > "$command_log"
assert_refused 'does not contain the current Remote Dev Codex helper' \
  env \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_CODEX_HELPER_ABSENT=true \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile codex example-app \
  --check
if ! grep -Fq '/usr/local/bin/remote-dev-codex' "$command_log" || \
   grep -Fq -- '-- /usr/local/bin/remote-dev-codex --check' "$command_log"; then
  echo 'Codex helper preflight did not stop before launch' >&2
  exit 1
fi

: > "$command_log"
set +e
infrastructure_output=$(env \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_CODEX_PREFLIGHT_FAIL=true \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile codex example-app \
  --check 2>&1)
infrastructure_status=$?
set -e
if (( infrastructure_status != 17 )) || \
   [[ $infrastructure_output != *'synthetic Coder SSH failure'* ]] || \
   [[ $infrastructure_output != *'Unable to verify Codex helper in workspace example-app (exit 17)'* ]]; then
  printf 'Codex infrastructure failure lost its diagnostic: %s\n' \
    "$infrastructure_output" >&2
  exit 1
fi
if grep -Fq -- '-- /usr/local/bin/remote-dev-codex --check' "$command_log"; then
  echo 'Codex infrastructure preflight failure did not stop before launch' >&2
  exit 1
fi

: > "$command_log"
set +e
outer_not_found_output=$(env \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_CODEX_OUTER_NOT_FOUND=true \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" --profile example-profile codex example-app \
  --check 2>&1)
outer_not_found_status=$?
set -e
if (( outer_not_found_status != 127 )) || \
   [[ $outer_not_found_output != *'docker: command not found'* ]] || \
   [[ $outer_not_found_output != *'Unable to verify Codex helper in workspace example-app (exit 127)'* ]] || \
   [[ $outer_not_found_output == *'does not contain the current Remote Dev Codex helper'* ]]; then
  printf 'Codex outer command failure was misclassified: %s\n' \
    "$outer_not_found_output" >&2
  exit 1
fi

: > "$command_log"
assert_refused 'operation requires --confirm-profile example-profile' \
  env REMOTE_DEV_PROFILE=example-profile \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" credentials reveal coder --confirm-profile wrong-profile
if [[ -s "$command_log" ]]; then
  echo 'credential reveal contacted the host before exact profile confirmation' >&2
  exit 1
fi

assert_refused 'existing /etc/remote-dev/host-profile.env does not match' \
  env \
  REMOTE_DEV_PROFILE=example-profile \
  HOME="$fixture/home" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_TEST_PROFILE_MISMATCH=true \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" install
if [[ $(grep -o 'ssh ' "$command_log" | wc -l | tr -d ' ') != 1 ]] || \
   grep -Eq 'scp |mktemp|sudo install -d' "$command_log"; then
  echo 'remote mutation was attempted after existing profile mismatch' >&2
  exit 1
fi
: > "$command_log"

# Without local credential files the installer verifies the remote copies.
env \
  REMOTE_DEV_PROFILE=example-profile \
  HOME="$fixture/home" \
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log" \
  REMOTE_DEV_CODER_BIN="$fake_bin/coder" \
  PATH="$fake_bin:$PATH" \
  "$repo_root/bin/remote-dev" install
for marker in \
  'example-host' \
  'https://coder.example.invalid' \
  '7e78eeea-fa3e-4db6-aa87-62477ead3fef' \
  '10.231.0.2/32' \
  '10.231.10.0/24' \
  '10.231.11.0/24'; do
  if ! grep -Fq "$marker" "$command_log"; then
    printf 'selected example profile did not preserve value: %s\n' "$marker" >&2
    exit 1
  fi
done
: > "$command_log"

printf '%s\n' 'SYNTHETIC_TEST_VALUE=not-a-secret' > "$source_file"
chmod 0600 "$source_file"
printf '%s\n' \
  '{"AccountTag":"synthetic","TunnelID":"7e78eeea-fa3e-4db6-aa87-62477ead3fef","TunnelSecret":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}' \
  > "$fixture/cloudflared.json"
printf '%s\n' \
  '[Interface]' \
  'PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' \
  'Address = 10.231.0.2/32' \
  '[Peer]' \
  'PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=' \
  'Endpoint = wireguard.invalid:51820' \
  'AllowedIPs = 0.0.0.0/0' \
  > "$fixture/wg-test.conf"

chmod 0644 "$fixture/cloudflared.json" "$fixture/wg-test.conf"
printf 'workspace\ttest-workspace\nlocal_root\t%s\nremote_root\t/home/coder/projects/test-workspace\nfile\tsynthetic.env\tsynthetic.env\n' \
  "$fixture" > "$manifest"

profile_env=(
  REMOTE_DEV_PROFILE=example-profile
  REMOTE_DEV_CLOUDFLARE_CREDENTIALS="$fixture/cloudflared.json"
  HOME="$fixture/home"
  REMOTE_DEV_TEST_COMMAND_LOG="$command_log"
  REMOTE_DEV_CODER_BIN="$fake_bin/coder"
  PATH="$fake_bin:$PATH"
)

env "${profile_env[@]}" REMOTE_DEV_TEST_SYSBOX_POLICY_MISSING=true \
  "$repo_root/bin/remote-dev" sysbox-install
for marker in \
  'apparmor-local-fusermount3' \
  '/tmp/remote-dev-install-sysbox' \
  'sudo /opt/remote-dev/sysbox/install.sh'; do
  if ! grep -Fq "$marker" "$command_log"; then
    printf 'active Sysbox policy repair omitted marker: %s\n' "$marker" >&2
    exit 1
  fi
done
if grep -Fq 'docker compose down' "$command_log"; then
  echo 'active Sysbox policy repair unnecessarily stopped the control plane' >&2
  exit 1
fi
: > "$command_log"

env "${profile_env[@]}" REMOTE_DEV_TEST_SYSBOX_NOT_INSTALLED=true \
  "$repo_root/bin/remote-dev" sysbox-install
for marker in \
  'apparmor-local-fusermount3' \
  '/tmp/remote-dev-install-sysbox' \
  '/opt/remote-dev/sysbox/apparmor-local-fusermount3' \
  'docker compose down'; do
  if ! grep -Fq "$marker" "$command_log"; then
    printf 'sysbox install path omitted marker: %s\n' "$marker" >&2
    exit 1
  fi
done
apparmor_preflight_line=$(grep -n -m1 '/etc/apparmor.d/local/fusermount3' "$command_log" | cut -d: -f1)
compose_down_line=$(grep -n -m1 'docker compose down' "$command_log" | cut -d: -f1)
if (( apparmor_preflight_line >= compose_down_line )); then
  echo 'Sysbox AppArmor preflight did not occur before Docker shutdown' >&2
  exit 1
fi
: > "$command_log"

assert_refused 'owner-only permissions' \
  env "${profile_env[@]}" "$repo_root/bin/remote-dev" install
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before insecure local secret refusal' >&2
  exit 1
fi
chmod 0600 "$fixture/cloudflared.json" "$fixture/wg-test.conf"

assert_refused 'public key does not match the selected profile' \
  env "${profile_env[@]}" REMOTE_DEV_WIREGUARD_CONFIG="$fixture/wg-test.conf" \
  "$repo_root/bin/remote-dev" install
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before WireGuard profile-key refusal' >&2
  exit 1
fi

assert_refused 'does not report expected hostname example-host' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_HOSTNAME_MISMATCH=true \
  "$repo_root/bin/remote-dev" install
if [[ $(grep -c '^ssh ' "$command_log" || true) != 2 ]] || \
   grep -Eq 'scp |mktemp|sudo install -d' "$command_log"; then
  echo 'remote mutation was attempted after hostname mismatch' >&2
  exit 1
fi
: > "$command_log"

ln -s "$fixture/wg-test.conf" "$fixture/wg-test-link.conf"
assert_refused 'regular non-symlink file' \
  env "${profile_env[@]}" \
  REMOTE_DEV_WIREGUARD_CONFIG="$fixture/wg-test-link.conf" \
  "$repo_root/bin/remote-dev" install
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before symlinked local secret refusal' >&2
  exit 1
fi

printf '%s\n' \
  '{"AccountTag":"synthetic","TunnelID":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","TunnelSecret":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}' \
  > "$fixture/cloudflared.json"
assert_refused 'TunnelID does not match the selected profile' \
  env "${profile_env[@]}" "$repo_root/bin/remote-dev" install
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before Cloudflare credential identity refusal' >&2
  exit 1
fi
printf '%s\n' \
  '{"AccountTag":"synthetic","TunnelID":"7e78eeea-fa3e-4db6-aa87-62477ead3fef","TunnelSecret":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}' \
  > "$fixture/cloudflared.json"

printf '%s\n' \
  '[Interface]' \
  'PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' \
  'Address = 10.231.0.2/32' \
  > "$fixture/wg-test.conf"
assert_refused 'WireGuard config must contain exactly one Peer' \
  python3 "$repo_root/deploy/host-profiles/validate-credentials.py" wireguard \
    "$fixture/wg-test.conf" 10.231.0.2/32 L+V9o0fNYkMVKNqsX7spBzD/9oSvxM/C7ZCZX1jLO3Q=
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before invalid WireGuard config refusal' >&2
  exit 1
fi
printf '%s\n' \
  '[Interface]' \
  'PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=' \
  'Address = 10.231.0.2/32' \
  '[Peer]' \
  'PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=' \
  'Endpoint = wireguard.invalid:51820' \
  'AllowedIPs = 0.0.0.0/0' \
  > "$fixture/wg-test.conf"

sed 's/wireguard.invalid/bad host/' "$fixture/wg-test.conf" > "$fixture/wg-invalid.conf"
chmod 0600 "$fixture/wg-invalid.conf"
assert_refused 'Endpoint host is invalid' \
  python3 "$repo_root/deploy/host-profiles/validate-credentials.py" wireguard \
    "$fixture/wg-invalid.conf" 10.231.0.2/32 L+V9o0fNYkMVKNqsX7spBzD/9oSvxM/C7ZCZX1jLO3Q=
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before invalid WireGuard endpoint refusal' >&2
  exit 1
fi
sed 's/wireguard.invalid/[192.0.2.1/' "$fixture/wg-test.conf" > "$fixture/wg-invalid.conf"
assert_refused 'Endpoint has malformed IPv6 brackets' \
  python3 "$repo_root/deploy/host-profiles/validate-credentials.py" wireguard \
    "$fixture/wg-invalid.conf" 10.231.0.2/32 L+V9o0fNYkMVKNqsX7spBzD/9oSvxM/C7ZCZX1jLO3Q=
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before malformed WireGuard bracket refusal' >&2
  exit 1
fi
sed 's/wireguard.invalid/2001:db8::1/' "$fixture/wg-test.conf" > "$fixture/wg-invalid.conf"
assert_refused 'Endpoint must be hostname:port, IPv4:port, or [IPv6]:port' \
  python3 "$repo_root/deploy/host-profiles/validate-credentials.py" wireguard \
    "$fixture/wg-invalid.conf" 10.231.0.2/32 L+V9o0fNYkMVKNqsX7spBzD/9oSvxM/C7ZCZX1jLO3Q=
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before unbracketed WireGuard IPv6 refusal' >&2
  exit 1
fi
sed 's#0.0.0.0/0#10.0.0.0/8#' "$fixture/wg-test.conf" > "$fixture/wg-invalid.conf"
assert_refused 'AllowedIPs must include 0.0.0.0/0' \
  python3 "$repo_root/deploy/host-profiles/validate-credentials.py" wireguard \
    "$fixture/wg-invalid.conf" 10.231.0.2/32 L+V9o0fNYkMVKNqsX7spBzD/9oSvxM/C7ZCZX1jLO3Q=
if [[ -s "$command_log" ]]; then
  echo 'remote action occurred before non-full-tunnel WireGuard refusal' >&2
  exit 1
fi

assert_refused 'profile example-profile has immutable identity' \
  env "${profile_env[@]}" REMOTE_DEV_EGRESS_GATEWAY=192.0.2.1 \
  "$repo_root/bin/remote-dev" doctor
assert_refused 'profile example-profile has immutable identity' \
  env "${profile_env[@]}" REMOTE_DEV_CONTROL_SUBNET=10.231.10.0/24 \
  "$repo_root/bin/remote-dev" doctor

: > "$command_log"
assert_refused '' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_FAIL_AFTER_STAGE=true \
  "$repo_root/bin/remote-dev" install
if ! grep -Fq "sudo rm -rf -- '/tmp/remote-dev-install.TESTSTAGE'" "$command_log"; then
  echo 'remote secret staging was not cleaned after an install failure' >&2
  exit 1
fi
: > "$command_log"

env "${profile_env[@]}" "$repo_root/bin/remote-dev" doctor
env "${profile_env[@]}" "$repo_root/bin/remote-dev" secrets plan "$manifest"
assert_refused 'RUNNER_SECRETS_UNSUPPORTED' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_WORKSPACE_ROLE=runner \
  "$repo_root/bin/remote-dev" secrets plan "$manifest"
assert_refused 'workspace role preflight failed' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_WORKSPACE_ROLE=invalid \
  "$repo_root/bin/remote-dev" secrets plan "$manifest"
assert_refused 'bin/remote-dev --profile example-profile workspace start test-workspace' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_WORKSPACE_STOPPED=true \
  "$repo_root/bin/remote-dev" secrets plan "$manifest"

runner_project=$fixture/runner-project
runner_state=$fixture/runner-state
runner_manifest=$fixture/runner-secrets.manifest.tsv
managed_manifest=$fixture/managed-manifest.tsv
excluded_manifest=$fixture/excluded-manifest.tsv
mkdir -p "$runner_project"
git -C "$runner_project" init -q
printf '.env\n' > "$runner_project/.gitignore"
printf 'tracked\n' > "$runner_project/README.md"
printf 'RUNNER_SECRET=synthetic\n' > "$runner_project/.env"
chmod 0600 "$runner_project/.env"
git -C "$runner_project" add .gitignore README.md
git -C "$runner_project" -c user.name='Remote Dev Test' \
  -c user.email=remote-dev@example.test commit -qm initial
runner_project=$(cd "$runner_project" && pwd -P)
runner_root_hash=$(printf '%s' "$runner_project" | shasum -a 256 | awk '{print $1}')
runner_binding_dir=$runner_state/bindings/by-root/$runner_root_hash
mkdir -p "$runner_binding_dir"
printf 'path\ttype\tmode\tdigest\tsymlink_target\tstate\nREADME.md\tfile\t0644\tplaceholder\t-\tpresent\n' \
  > "$managed_manifest"
printf 'path\treason\n.env\tremote-writable\n' > "$excluded_manifest"
printf 'schema\t1\nproject_id\ttest-project\nworkspace\ttest-runner\nworkspace_role\trunner\nprofile\texample-profile\ncoder_origin\thttps://coder.example.invalid\nlocal_root\t%s\nremote_root\t/home/coder/projects/test-project\nmanaged_manifest_path\t%s\nexcluded_manifest_path\t%s\n' \
  "$runner_project" "$managed_manifest" "$excluded_manifest" \
  > "$runner_binding_dir/binding.tsv"
printf 'manifest_version\t2\nproject_id\ttest-project\nworkspace\ttest-runner\nworkspace_role\trunner\nlocal_root\t%s\nremote_root\t/home/coder/projects/test-project\nfile\t.env\t.env\n' \
  "$runner_project" > "$runner_manifest"
runner_plan=$(env "${profile_env[@]}" \
  REMOTE_DEV_TEST_WORKSPACE_ROLE=runner \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$runner_state" \
  "$repo_root/bin/remote-dev" secrets plan "$runner_manifest")
[[ $runner_plan == *'READY    plan is valid; no secret content was transferred'* ]]

python3 "$repo_root/tests/remote-dev-secrets-binding-test.py"

printf '.env\tfile\t0600\tplaceholder\t-\tpresent\n' >> "$managed_manifest"
assert_refused 'runner secret destination overlaps managed source' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_WORKSPACE_ROLE=runner \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$runner_state" \
  "$repo_root/bin/remote-dev" secrets plan "$runner_manifest"
printf 'path\ttype\tmode\tdigest\tsymlink_target\tstate\nREADME.md\tfile\t0644\tplaceholder\t-\tpresent\n' \
  > "$managed_manifest"
printf 'path\treason\n.env\tobserved-ignored\n' > "$excluded_manifest"
assert_refused 'runner secret destination is not declared remote-writable' \
  env "${profile_env[@]}" REMOTE_DEV_TEST_WORKSPACE_ROLE=runner \
  REMOTE_DEV_LOCAL_AGENT_STATE_ROOT="$runner_state" \
  "$repo_root/bin/remote-dev" secrets plan "$runner_manifest"
coder_calls_before=$(grep -c '^coder ' "$command_log" || true)
assert_refused 'apply requires --confirm-profile example-profile' \
  env "${profile_env[@]}" "$repo_root/bin/remote-dev" secrets apply "$manifest" \
  --confirm test-workspace --confirm-origin https://coder.example.invalid
assert_refused 'apply requires --confirm-origin https://coder.example.invalid' \
  env "${profile_env[@]}" "$repo_root/bin/remote-dev" secrets apply "$manifest" \
  --confirm test-workspace --confirm-profile example-profile \
  --confirm-origin https://wrong-origin.invalid
coder_calls_after=$(grep -c '^coder ' "$command_log" || true)
if [[ "$coder_calls_before" != "$coder_calls_after" ]]; then
  echo 'secret transfer contacted Coder before exact profile/origin confirmation' >&2
  exit 1
fi
env "${profile_env[@]}" "$repo_root/bin/remote-dev" install

for marker in \
  'ssh -o ControlMaster=no -o ControlPath=none -o ClearAllForwardings=yes example-host' \
  'https://coder.example.invalid' \
  'coder --no-version-warning --global-config' \
  'example-host' \
  'dev-server' \
  "$(printf '%s' 'Example dev server' | base64 | tr -d '\n')" \
  '7e78eeea-fa3e-4db6-aa87-62477ead3fef' \
  '10.231.0.2/32' \
  '10.231.10.0/24' \
  '10.231.11.0/24' \
  "sudo chmod 0700 '/tmp/remote-dev-install.TESTSTAGE'" \
  '/tmp/remote-dev-install.TESTSTAGE/cloudflare-credentials.json' \
  'chmod 0600 "/tmp/remote-dev-install.TESTSTAGE/cloudflare-credentials.json"' \
  "sudo rm -rf -- '/tmp/remote-dev-install.TESTSTAGE'" \
  'rendered-cloudflared   - hostname: coder.example.invalid' \
  'rendered-cloudflared     service: http://127.0.0.1:3080'; do
  if ! grep -Fq "$marker" "$command_log"; then
    printf 'profile value did not propagate through dry fake transport: %s\n' "$marker" >&2
    exit 1
  fi
done

if grep -Eq 'example-direct|10\.232\.' "$command_log"; then
  echo 'example-profile reused the identity of another profile during fake transport' >&2
  exit 1
fi

if grep -Eq '/tmp/remote-dev-cloudflared-credentials\.json|/tmp/remote-dev-wg-egress\.conf|scp .*cloudflared\.json|scp .*wg-test\.conf' \
  "$command_log"; then
  echo 'secret payload used an unprotected remote path or scp staging' >&2
  exit 1
fi

printf 'PASS     remote-dev host profiles fail closed and propagate explicitly\n'
