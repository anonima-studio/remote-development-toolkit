#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
. "$repo_root/deploy/host-profiles/load.sh"
remote_dev_load_profile "$repo_root" "${REMOTE_DEV_PROFILE:-}"
required_fields=()
while IFS= read -r field; do
  required_fields+=("$field")
done < <(remote_dev_identity_fields "$REMOTE_DEV_INGRESS" "$REMOTE_DEV_EGRESS")
[[ $REMOTE_DEV_INGRESS != cloudflare-tunnel ]] || required_fields+=(REMOTE_DEV_CLOUDFLARE_CREDENTIALS)
[[ $REMOTE_DEV_EGRESS != wireguard ]] || required_fields+=(REMOTE_DEV_WIREGUARD_CONFIG)
remote_dev_require_profile_fields install "${required_fields[@]}"
remote_dev_validate_profile_values

# The original topology (Cloudflare Tunnel + WireGuard) keeps its runtime
# profile format unchanged; other topologies record their modes explicitly.
default_topology=false
if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel && $REMOTE_DEV_EGRESS == wireguard ]]; then
  default_topology=true
fi

host=$REMOTE_DEV_SSH_HOST
display_name_b64=$(printf '%s' "$REMOTE_DEV_DISPLAY_NAME" | base64 | tr -d '\n')
ssh_options=(-o ControlMaster=no -o ControlPath=none -o ClearAllForwardings=yes)
cloudflare_tunnel_id=$REMOTE_DEV_CLOUDFLARE_TUNNEL_ID
cloudflare_credentials=$REMOTE_DEV_CLOUDFLARE_CREDENTIALS
wireguard_config=$REMOTE_DEV_WIREGUARD_CONFIG
credential_validator="$repo_root/deploy/host-profiles/validate-credentials.py"
rendered_cloudflare_config=$(mktemp)
remote_stage=''

remote() {
  ssh "${ssh_options[@]}" "$host" "$@"
}

# ssh joins its arguments into one remote command line, which drops empty
# values and re-splits the rest. Positional arguments for a remote script are
# therefore quoted one by one.
remote_script() {
  local command=$1 quoted argument
  shift
  for argument in "$@"; do
    printf -v quoted '%q' "$argument"
    command+=" $quoted"
  done
  ssh "${ssh_options[@]}" "$host" "$command"
}

copy() {
  scp "${ssh_options[@]}" "$@"
}

prepare_managed_copy_targets() {
  local script quoted command target
  script='set -euo pipefail
for target in "$@"; do
  if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" ]] || {
      printf "managed copy target is not a regular non-symlink file: %s\n" "$target" >&2
      exit 1
    }
    chown dev:dev -- "$target"
    chmod u+w -- "$target"
  fi
done'
  printf -v quoted '%q' "$script"
  command="sudo bash -c $quoted remote-dev-managed-copy-targets"
  for target in "$@"; do
    printf -v quoted '%q' "$target"
    command+=" $quoted"
  done
  remote "$command"
}

cleanup() {
  rm -f "$rendered_cloudflare_config"
  if [[ "$remote_stage" =~ ^/tmp/remote-dev-install\.[A-Za-z0-9]+$ ]]; then
    remote "sudo rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

secret_source_state() {
  local path=$1 label=$2 mode owner_uid
  if [[ ! -e "$path" && ! -L "$path" ]]; then
    return 1
  fi
  if [[ -L "$path" || ! -f "$path" || ! -r "$path" || ! -s "$path" ]]; then
    printf 'ERROR    %s must be a readable non-empty regular non-symlink file: %s\n' \
      "$label" "$path" >&2
    return 2
  fi
  if [[ $(uname -s) == Darwin ]]; then
    mode=$(/usr/bin/stat -f '%Lp' "$path")
    owner_uid=$(/usr/bin/stat -f '%u' "$path")
  else
    mode=$(stat -c '%a' "$path")
    owner_uid=$(stat -c '%u' "$path")
  fi
  if (( (8#$mode & 077) != 0 )) || [[ "$owner_uid" != "$(id -u)" ]]; then
    printf 'ERROR    %s must be owned by the current user with owner-only permissions: %s\n' \
      "$label" "$path" >&2
    return 2
  fi
  return 0
}

cloudflare_source=false
if [[ $REMOTE_DEV_INGRESS != cloudflare-tunnel ]]; then
  :
elif secret_source_state "$cloudflare_credentials" 'Cloudflare credentials'; then
  cloudflare_source=true
  python3 "$credential_validator" cloudflare \
    "$cloudflare_credentials" "$cloudflare_tunnel_id"
else
  source_status=$?
  (( source_status == 1 )) || exit "$source_status"
fi
wireguard_source=false
if [[ $REMOTE_DEV_EGRESS != wireguard ]]; then
  :
elif secret_source_state "$wireguard_config" 'WireGuard config'; then
  wireguard_source=true
  python3 "$credential_validator" wireguard "$wireguard_config" \
    "$REMOTE_DEV_WIREGUARD_ADDRESS" "$REMOTE_DEV_WIREGUARD_PUBLIC_KEY"
else
  source_status=$?
  (( source_status == 1 )) || exit "$source_status"
fi

if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel ]]; then
  sed \
    -e "s/{{TUNNEL_ID}}/$REMOTE_DEV_CLOUDFLARE_TUNNEL_ID/g" \
    -e "s/{{HOSTNAME}}/$REMOTE_DEV_CLOUDFLARE_HOSTNAME/g" \
    -e "s/{{CODER_HOST_PORT}}/$REMOTE_DEV_CODER_HOST_PORT/g" \
    "$repo_root/deploy/cloudflared/config.yml" > "$rendered_cloudflare_config"
fi

# This is the first remote action and is deliberately read-only. A checked-in
# profile never adopts or rewrites a host carrying another recorded identity.
if ! remote_script 'sudo bash -s -- remote-dev-profile-preflight' \
  "$REMOTE_DEV_PROFILE" \
  "$REMOTE_DEV_SSH_HOST" \
  "$REMOTE_DEV_CODER_ACCESS_URL" \
  "$REMOTE_DEV_CODER_HOST_PORT" \
  "$REMOTE_DEV_CLOUDFLARE_TUNNEL_ID" \
  "$REMOTE_DEV_CLOUDFLARE_HOSTNAME" \
  "$REMOTE_DEV_WIREGUARD_ADDRESS" \
  "$REMOTE_DEV_WIREGUARD_PUBLIC_KEY" \
  "$REMOTE_DEV_WIREGUARD_INTERFACE" \
  "$REMOTE_DEV_EGRESS_SUBNET" \
  "$REMOTE_DEV_EGRESS_GATEWAY" \
  "$REMOTE_DEV_EGRESS_MTU" \
  "$REMOTE_DEV_CONTROL_SUBNET" \
  "$REMOTE_DEV_POLICY_TABLE" \
  "$REMOTE_DEV_EXPECTED_HOSTNAME" \
  "$REMOTE_DEV_SYSTEM_ROLE" \
  "$display_name_b64" \
  "$default_topology" \
  "$REMOTE_DEV_INGRESS" \
  "$REMOTE_DEV_EGRESS" <<'REMOTE_PROFILE_PREFLIGHT'
set -euo pipefail
marker=$1
shift
[[ "$marker" == remote-dev-profile-preflight ]]
default_topology=${*: -3:1}
ingress=${*: -2:1}
egress=${*: -1}
set -- "${@:1:$(($# - 3))}"
profile_file=/etc/remote-dev/host-profile.env
[[ -e "$profile_file" ]] || exit 0
[[ -f "$profile_file" && ! -L "$profile_file" ]]
[[ $(stat -c %U "$profile_file") == root ]]
[[ $(stat -c %a "$profile_file") == 644 ]]
expected_names=(
  REMOTE_DEV_RUNTIME_PROFILE
  REMOTE_DEV_SSH_HOST
  REMOTE_DEV_CODER_ACCESS_URL
  REMOTE_DEV_CODER_HOST_PORT
  REMOTE_DEV_CLOUDFLARE_TUNNEL_ID
  REMOTE_DEV_CLOUDFLARE_HOSTNAME
  REMOTE_DEV_WIREGUARD_ADDRESS
  REMOTE_DEV_WIREGUARD_PUBLIC_KEY
  REMOTE_DEV_WIREGUARD_INTERFACE
  REMOTE_DEV_EGRESS_SUBNET
  REMOTE_DEV_EGRESS_GATEWAY
  REMOTE_DEV_EGRESS_MTU
  REMOTE_DEV_CONTROL_SUBNET
  REMOTE_DEV_POLICY_TABLE
)
if [[ -n "${17}" ]]; then
  decoded_display_name=$(printf '%s' "${17}" | base64 -d)
  printf -v decoded_display_name '%q' "$decoded_display_name"
  set -- "${@:1:16}" "$decoded_display_name"
fi
if [[ -n "${15}" || -n "${16}" || -n "${17}" ]]; then
  expected_names+=(
    REMOTE_DEV_EXPECTED_HOSTNAME
    REMOTE_DEV_SYSTEM_ROLE
    REMOTE_DEV_DISPLAY_NAME
  )
else
  set -- "${@:1:14}"
fi
if ! grep -q '^REMOTE_DEV_WIREGUARD_PUBLIC_KEY=' "$profile_file"; then
  expected_names=("${expected_names[@]:0:7}" "${expected_names[@]:8}")
  set -- "${@:1:7}" "${@:9}"
fi
if [[ $default_topology != true ]]; then
  expected_names+=(REMOTE_DEV_INGRESS REMOTE_DEV_EGRESS)
  set -- "$@" "$ingress" "$egress"
fi
(( $# == ${#expected_names[@]} ))
[[ $(wc -l < "$profile_file") -eq ${#expected_names[@]} ]]
for index in "${!expected_names[@]}"; do
  position=$((index + 1))
  grep -Fxq "${expected_names[$index]}=${!position}" "$profile_file"
done
REMOTE_PROFILE_PREFLIGHT
then
  remote_dev_profile_fail \
    "existing /etc/remote-dev/host-profile.env does not match profile $REMOTE_DEV_PROFILE"
fi

if [[ -n "$REMOTE_DEV_EXPECTED_HOSTNAME" ]]; then
  if ! remote_script 'bash -s --' "$REMOTE_DEV_EXPECTED_HOSTNAME" <<'REMOTE_HOSTNAME_CHECK'
set -euo pipefail
[[ "$(hostname)" == "$1" ]]
REMOTE_HOSTNAME_CHECK
  then
    remote_dev_profile_fail \
      "host reached through $REMOTE_DEV_SSH_HOST does not report expected hostname $REMOTE_DEV_EXPECTED_HOSTNAME"
  fi
fi

if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel && "$cloudflare_source" != true ]] && \
   ! remote "sudo python3 - cloudflare '/etc/cloudflared/$cloudflare_tunnel_id.json' '$cloudflare_tunnel_id'" \
     < "$credential_validator"; then
  remote_dev_profile_fail \
    "Cloudflare credentials are absent locally and remotely for profile $REMOTE_DEV_PROFILE: $cloudflare_credentials"
fi
if [[ $REMOTE_DEV_EGRESS == wireguard && "$wireguard_source" != true ]] && \
   ! remote "sudo python3 - wireguard '/etc/wireguard/$REMOTE_DEV_WIREGUARD_INTERFACE.conf'" \
     "$REMOTE_DEV_WIREGUARD_ADDRESS" "$REMOTE_DEV_WIREGUARD_PUBLIC_KEY" \
     < "$credential_validator"; then
  remote_dev_profile_fail \
    "WireGuard config is absent locally and remotely for profile $REMOTE_DEV_PROFILE: $wireguard_config"
fi

# The first mutating remote action creates the only allowed staging location
# for secret payloads. The local EXIT trap removes it on success and failure.
remote_stage=$(remote 'sudo mktemp -d -p /tmp remote-dev-install.XXXXXXXX')
[[ "$remote_stage" =~ ^/tmp/remote-dev-install\.[A-Za-z0-9]+$ ]] || \
  remote_dev_profile_fail 'remote returned an invalid install staging path'
remote "sudo chown root:root '$remote_stage' && sudo chmod 0700 '$remote_stage' && sudo test -d '$remote_stage'"

stage_secret() {
  local source=$1 destination=$2
  remote "sudo sh -c 'umask 077; cat > \"$remote_stage/$destination\"; chown root:root \"$remote_stage/$destination\"; chmod 0600 \"$remote_stage/$destination\"'" < "$source"
}
if [[ "$cloudflare_source" == true ]]; then
  stage_secret "$cloudflare_credentials" cloudflare-credentials.json
fi
if [[ "$wireguard_source" == true ]]; then
  stage_secret "$wireguard_config" wireguard.conf
fi

remote 'sudo install -d -o dev -g dev -m 0750 /opt/remote-dev/coder /opt/remote-dev/templates/docker /opt/remote-dev/templates/docker/skills /opt/remote-dev/templates/docker/skills/worktree-branch-sync /opt/remote-dev/templates/docker/skills/worktree-branch-sync/references /opt/remote-dev/backup && sudo install -d -o root -g root -m 0755 /opt/remote-dev/sysbox && sudo install -d -o root -g root -m 0700 /etc/cloudflared'

prepare_managed_copy_targets \
  /opt/remote-dev/coder/compose.yaml \
  /opt/remote-dev/coder/terraformrc \
  /opt/remote-dev/coder/.env.example \
  /opt/remote-dev/coder/README.md \
  /opt/remote-dev/coder/retain-workspace-image.sh \
  /opt/remote-dev/coder/workspace-images.py \
  /opt/remote-dev/templates/docker/Dockerfile \
  /opt/remote-dev/templates/docker/codex-start.sh \
  /opt/remote-dev/templates/docker/project-setup.sh \
  /opt/remote-dev/templates/docker/project-contract.py \
  /opt/remote-dev/templates/docker/mode-guard.py \
  /opt/remote-dev/templates/docker/worktree-vars.sh \
  /opt/remote-dev/templates/docker/workspace-role.sh \
  /opt/remote-dev/templates/docker/managed-unlink.py \
  /opt/remote-dev/templates/docker/start-docker.sh \
  /opt/remote-dev/templates/docker/main.tf \
  /opt/remote-dev/templates/docker/README.md \
  /opt/remote-dev/templates/docker/skills/worktree-branch-sync/SKILL.md \
  /opt/remote-dev/templates/docker/skills/worktree-branch-sync/references/worktree-branch-sync.md \
  /opt/remote-dev/backup/backup.sh \
  /opt/remote-dev/backup/README.md \
  /opt/remote-dev/backup/restore.md \
  /opt/remote-dev/backup/restic.env.example \
  /opt/remote-dev/backup/remote-dev-backup.service \
  /opt/remote-dev/backup/remote-dev-backup.timer

rendered_terraformrc=$(mktemp)
if [[ -n $REMOTE_DEV_TERRAFORM_MIRROR ]]; then
  printf '%s\n' \
    'provider_installation {' \
    '  network_mirror {' \
    "    url     = \"$REMOTE_DEV_TERRAFORM_MIRROR\"" \
    '    include = ["registry.terraform.io/*/*"]' \
    '  }' \
    '' \
    '  direct {' \
    '    exclude = ["registry.terraform.io/*/*"]' \
    '  }' \
    '}' > "$rendered_terraformrc"
else
  cp "$repo_root/deploy/coder/terraformrc" "$rendered_terraformrc"
fi
copy "$rendered_terraformrc" "$host:/opt/remote-dev/coder/terraformrc"
rm -f "$rendered_terraformrc"
copy \
  "$repo_root/deploy/coder/compose.yaml" \
  "$repo_root/deploy/coder/.env.example" \
  "$repo_root/deploy/coder/README.md" \
  "$repo_root/deploy/retain-workspace-image.sh" \
  "$repo_root/deploy/workspace-images.py" \
  "$host:/opt/remote-dev/coder/"
copy \
  "$repo_root/templates/docker/Dockerfile" \
  "$repo_root/templates/docker/codex-start.sh" \
  "$repo_root/templates/docker/project-setup.sh" \
  "$repo_root/templates/docker/project-contract.py" \
  "$repo_root/templates/docker/mode-guard.py" \
  "$repo_root/templates/docker/worktree-vars.sh" \
  "$repo_root/templates/docker/workspace-role.sh" \
  "$repo_root/templates/docker/managed-unlink.py" \
  "$repo_root/templates/docker/start-docker.sh" \
  "$repo_root/templates/docker/main.tf" \
  "$repo_root/templates/docker/README.md" \
  "$host:/opt/remote-dev/templates/docker/"
copy \
  "$repo_root/templates/docker/skills/worktree-branch-sync/SKILL.md" \
  "$host:/opt/remote-dev/templates/docker/skills/worktree-branch-sync/"
copy \
  "$repo_root/templates/docker/skills/worktree-branch-sync/references/worktree-branch-sync.md" \
  "$host:/opt/remote-dev/templates/docker/skills/worktree-branch-sync/references/"
copy \
  "$repo_root/backup/backup.sh" \
  "$repo_root/backup/README.md" \
  "$repo_root/backup/restore.md" \
  "$repo_root/backup/restic.env.example" \
  "$repo_root/backup/remote-dev-backup.service" \
  "$repo_root/backup/remote-dev-backup.timer" \
  "$host:/opt/remote-dev/backup/"
copy \
  "$repo_root/deploy/sysbox/install.sh" \
  "$host:/tmp/remote-dev-install-sysbox"
copy \
  "$repo_root/deploy/sysbox/apparmor-local-fusermount3" \
  "$host:/tmp/remote-dev-apparmor-local-fusermount3"
remote 'sudo install -o root -g root -m 0755 /tmp/remote-dev-install-sysbox /opt/remote-dev/sysbox/install.sh && sudo install -o root -g root -m 0644 /tmp/remote-dev-apparmor-local-fusermount3 /opt/remote-dev/sysbox/apparmor-local-fusermount3 && rm -f /tmp/remote-dev-install-sysbox /tmp/remote-dev-apparmor-local-fusermount3'

if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel ]]; then
  copy "$rendered_cloudflare_config" "$host:/tmp/remote-dev-cloudflared-config.yml"
  copy "$repo_root/deploy/cloudflared/cloudflared.service" "$host:/tmp/remote-dev-cloudflared.service"
fi
copy "$repo_root/deploy/wireguard/remote-dev-egress-up.sh" "$host:/tmp/remote-dev-egress-up"
copy "$repo_root/deploy/wireguard/validate-docker-network.py" "$host:/tmp/remote-dev-validate-docker-network"
copy "$repo_root/deploy/wireguard/remote-dev-egress-down.sh" "$host:/tmp/remote-dev-egress-down"
copy "$repo_root/deploy/wireguard/remote-dev-egress.service" "$host:/tmp/remote-dev-egress.service"
copy "$repo_root/deploy/wireguard/apparmor-local-wg" "$host:/tmp/remote-dev-apparmor-local-wg"
copy "$repo_root/deploy/networkd/10-remote-dev-egress.conf" "$host:/tmp/remote-dev-networkd-foreign-routing.conf"

if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel ]]; then
remote_script 'sudo bash -s --' "$cloudflare_tunnel_id" "$remote_stage" <<'REMOTE_CLOUDFLARE'
set -euo pipefail
tunnel_id=$1
stage=$2

install -d -m 0755 /usr/share/keyrings
if [[ ! -r /usr/share/keyrings/cloudflare-main.gpg ]]; then
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    -o /usr/share/keyrings/cloudflare-main.gpg
fi
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared

install -o root -g root -m 0600 /tmp/remote-dev-cloudflared-config.yml /etc/cloudflared/config.yml
install -o root -g root -m 0644 /tmp/remote-dev-cloudflared.service /etc/systemd/system/cloudflared.service
if [[ -e "$stage/cloudflare-credentials.json" ]]; then
  install -o root -g root -m 0600 \
    "$stage/cloudflare-credentials.json" "/etc/cloudflared/$tunnel_id.json"
fi
rm -f /tmp/remote-dev-cloudflared-config.yml /tmp/remote-dev-cloudflared.service
test -s "/etc/cloudflared/$tunnel_id.json"
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
systemctl daemon-reload
systemctl enable --now cloudflared.service
REMOTE_CLOUDFLARE
fi

remote_script 'sudo bash -s --' \
  "$REMOTE_DEV_PROFILE" \
  "$REMOTE_DEV_SSH_HOST" \
  "$REMOTE_DEV_CODER_ACCESS_URL" \
  "$REMOTE_DEV_CODER_HOST_PORT" \
  "$REMOTE_DEV_CLOUDFLARE_TUNNEL_ID" \
  "$REMOTE_DEV_CLOUDFLARE_HOSTNAME" \
  "$REMOTE_DEV_WIREGUARD_INTERFACE" \
  "$REMOTE_DEV_WIREGUARD_ADDRESS" \
  "$REMOTE_DEV_WIREGUARD_PUBLIC_KEY" \
  "$REMOTE_DEV_EGRESS_SUBNET" \
  "$REMOTE_DEV_EGRESS_GATEWAY" \
  "$REMOTE_DEV_EGRESS_MTU" \
  "$REMOTE_DEV_CONTROL_SUBNET" \
  "$REMOTE_DEV_POLICY_TABLE" \
  "$REMOTE_DEV_EXPECTED_HOSTNAME" \
  "$REMOTE_DEV_SYSTEM_ROLE" \
  "$display_name_b64" \
  "$remote_stage" \
  "$default_topology" \
  "$REMOTE_DEV_INGRESS" \
  "$REMOTE_DEV_EGRESS" <<'REMOTE_WIREGUARD'
set -euo pipefail
profile=$1
ssh_host=$2
coder_access_url=$3
coder_host_port=$4
cloudflare_tunnel_id=$5
cloudflare_hostname=$6
wireguard_interface=$7
wireguard_address=$8
wireguard_public_key=$9
egress_subnet=${10}
egress_gateway=${11}
egress_mtu=${12}
control_subnet=${13}
policy_table=${14}
expected_hostname=${15}
system_role=${16}
display_name=$(printf '%s' "${17}" | base64 -d)
stage=${18}
default_topology=${19}
ingress=${20}
egress=${21}

if [[ $egress == wireguard ]]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wireguard-tools
  install -d -o root -g root -m 0700 /etc/wireguard
fi
install -d -o root -g root -m 0755 /etc/remote-dev
runtime_profile=$(mktemp /etc/remote-dev/.host-profile.XXXXXXXX)
trap 'rm -f "$runtime_profile"' EXIT
umask 077
printf '%s\n' \
  "REMOTE_DEV_RUNTIME_PROFILE=$profile" \
  "REMOTE_DEV_SSH_HOST=$ssh_host" \
  "REMOTE_DEV_CODER_ACCESS_URL=$coder_access_url" \
  "REMOTE_DEV_CODER_HOST_PORT=$coder_host_port" \
  "REMOTE_DEV_CLOUDFLARE_TUNNEL_ID=$cloudflare_tunnel_id" \
  "REMOTE_DEV_CLOUDFLARE_HOSTNAME=$cloudflare_hostname" \
  "REMOTE_DEV_WIREGUARD_INTERFACE=$wireguard_interface" \
  "REMOTE_DEV_WIREGUARD_ADDRESS=$wireguard_address" \
  "REMOTE_DEV_WIREGUARD_PUBLIC_KEY=$wireguard_public_key" \
  "REMOTE_DEV_EGRESS_SUBNET=$egress_subnet" \
  "REMOTE_DEV_EGRESS_GATEWAY=$egress_gateway" \
  "REMOTE_DEV_EGRESS_MTU=$egress_mtu" \
  "REMOTE_DEV_CONTROL_SUBNET=$control_subnet" \
  "REMOTE_DEV_POLICY_TABLE=$policy_table" \
  > "$runtime_profile"
if [[ -n "$expected_hostname" || -n "$system_role" || -n "$display_name" ]]; then
  printf '%s\n' \
    "REMOTE_DEV_EXPECTED_HOSTNAME=$expected_hostname" \
    "REMOTE_DEV_SYSTEM_ROLE=$system_role" \
    >> "$runtime_profile"
  printf 'REMOTE_DEV_DISPLAY_NAME=%q\n' "$display_name" >> "$runtime_profile"
fi
if [[ $default_topology != true ]]; then
  printf '%s\n' "REMOTE_DEV_INGRESS=$ingress" "REMOTE_DEV_EGRESS=$egress" >> "$runtime_profile"
fi
chown root:root "$runtime_profile"
chmod 0644 "$runtime_profile"
mv -f "$runtime_profile" /etc/remote-dev/host-profile.env
trap - EXIT
if [[ $egress == wireguard ]]; then
  if [[ -e "$stage/wireguard.conf" ]]; then
    install -o root -g root -m 0600 \
      "$stage/wireguard.conf" "/etc/wireguard/$wireguard_interface.conf"
  fi
  test -s "/etc/wireguard/$wireguard_interface.conf"
fi
install -o root -g root -m 0755 \
  /tmp/remote-dev-egress-up /usr/local/sbin/remote-dev-egress-up
install -d -o root -g root -m 0755 /usr/local/lib/remote-dev
install -o root -g root -m 0755 \
  /tmp/remote-dev-validate-docker-network \
  /usr/local/lib/remote-dev/validate-docker-network.py
install -o root -g root -m 0755 \
  /tmp/remote-dev-egress-down /usr/local/sbin/remote-dev-egress-down
install -o root -g root -m 0644 \
  /tmp/remote-dev-egress.service /etc/systemd/system/remote-dev-egress.service
if [[ $egress == wireguard ]]; then
# systemd-networkd reads this at its next start; do not restart it here. Rewrite
# it only when it differs, so a reinstall does not look like a newer change.
install -d -o root -g root -m 0755 /etc/systemd/networkd.conf.d
if ! cmp -s /tmp/remote-dev-networkd-foreign-routing.conf \
     /etc/systemd/networkd.conf.d/10-remote-dev-egress.conf; then
  install -o root -g root -m 0644 \
    /tmp/remote-dev-networkd-foreign-routing.conf \
    /etc/systemd/networkd.conf.d/10-remote-dev-egress.conf
fi
install -d -o root -g root -m 0755 /etc/apparmor.d/local
if [[ -e /etc/apparmor.d/local/wg ]] && \
   ! cmp -s /tmp/remote-dev-apparmor-local-wg /etc/apparmor.d/local/wg; then
  echo '/etc/apparmor.d/local/wg differs; merge the Remote Dev rule explicitly' >&2
  exit 1
fi
install -o root -g root -m 0644 \
  /tmp/remote-dev-apparmor-local-wg /etc/apparmor.d/local/wg
apparmor_parser -r /etc/apparmor.d/wg
fi
rm -f /tmp/remote-dev-egress-up /tmp/remote-dev-validate-docker-network \
  /tmp/remote-dev-egress-down \
  /tmp/remote-dev-egress.service /tmp/remote-dev-apparmor-local-wg \
  /tmp/remote-dev-networkd-foreign-routing.conf
systemctl daemon-reload
systemctl enable remote-dev-egress.service
systemctl restart remote-dev-egress.service
REMOTE_WIREGUARD

remote "sudo rm -rf -- '$remote_stage'"
remote_stage=''

remote_script 'bash -s --' \
  "$REMOTE_DEV_CODER_ACCESS_URL" \
  "$REMOTE_DEV_CODER_HOST_PORT" \
  "$REMOTE_DEV_EGRESS_MTU" <<'REMOTE'
set -euo pipefail
coder_access_url=$1
coder_host_port=$2
egress_mtu=$3
cd /opt/remote-dev/coder

workspace_image_expected_hash=cf2a26a364ef23228b22b3cdcd2dc5012ca6f154fa0355542de9532fe60ea962
workspace_image_tag=remote-dev-workspace:node24-20260906-cf2a26a364e
workspace_image_hash=$(cat \
  /opt/remote-dev/templates/docker/Dockerfile \
  /opt/remote-dev/templates/docker/codex-start.sh \
  /opt/remote-dev/templates/docker/project-setup.sh \
  /opt/remote-dev/templates/docker/project-contract.py \
  /opt/remote-dev/templates/docker/mode-guard.py \
  /opt/remote-dev/templates/docker/worktree-vars.sh \
  /opt/remote-dev/templates/docker/workspace-role.sh \
  /opt/remote-dev/templates/docker/managed-unlink.py \
  /opt/remote-dev/templates/docker/skills/worktree-branch-sync/SKILL.md \
  /opt/remote-dev/templates/docker/skills/worktree-branch-sync/references/worktree-branch-sync.md \
  /opt/remote-dev/templates/docker/start-docker.sh | sha256sum | cut -d' ' -f1)
if [[ "$workspace_image_hash" != "$workspace_image_expected_hash" ]]; then
  echo "workspace image source hash mismatch: expected $workspace_image_expected_hash, got $workspace_image_hash" >&2
  echo 'assign a new content-derived image tag before deployment' >&2
  exit 1
fi
workspace_image_installed_hash=$(docker image inspect \
  --format '{{ index .Config.Labels "id0.remote-dev.workspace-source-sha256" }}' \
  "$workspace_image_tag" 2>/dev/null || true)
if [[ "$workspace_image_installed_hash" != "$workspace_image_expected_hash" ]]; then
  docker build \
    --build-arg REMOTE_DEV_WORKSPACE_SOURCE_SHA256="$workspace_image_expected_hash" \
    --tag "$workspace_image_tag" \
    /opt/remote-dev/templates/docker
fi
printf '%s\n' "$workspace_image_expected_hash" > workspace-image.sha256
bash /opt/remote-dev/coder/retain-workspace-image.sh \
  "$workspace_image_tag" "$workspace_image_expected_hash"

if [[ ! -e .env ]]; then
  umask 077
  docker_gid=$(getent group docker | cut -d: -f3)
  postgres_password=$(openssl rand -hex 32)
  printf '%s\n' \
    "CODER_HOST_PORT=$coder_host_port" \
    "CODER_ACCESS_URL=$coder_access_url" \
    "REMOTE_DEV_EGRESS_MTU=$egress_mtu" \
    "DOCKER_GID=$docker_gid" \
    'POSTGRES_USER=coder' \
    "POSTGRES_PASSWORD=$postgres_password" \
    'POSTGRES_DB=coder' > .env
  chmod 0600 .env
fi

if grep -q '^CODER_ACCESS_URL=' .env; then
  sed -i "s|^CODER_ACCESS_URL=.*|CODER_ACCESS_URL=$coder_access_url|" .env
else
  printf 'CODER_ACCESS_URL=%s\n' "$coder_access_url" >> .env
fi
if grep -q '^CODER_HOST_PORT=' .env; then
  sed -i "s|^CODER_HOST_PORT=.*|CODER_HOST_PORT=$coder_host_port|" .env
else
  printf 'CODER_HOST_PORT=%s\n' "$coder_host_port" >> .env
fi
if grep -q '^REMOTE_DEV_EGRESS_MTU=' .env; then
  sed -i "s|^REMOTE_DEV_EGRESS_MTU=.*|REMOTE_DEV_EGRESS_MTU=$egress_mtu|" .env
else
  printf 'REMOTE_DEV_EGRESS_MTU=%s\n' "$egress_mtu" >> .env
fi

docker compose config --quiet
docker compose pull
docker compose up -d

ready=false
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$coder_host_port/api/v2/buildinfo" >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  docker compose ps
  exit 1
fi

first_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$coder_host_port/api/v2/users/first")
if [[ "$first_status" == 404 ]]; then
  umask 077
  owner_password=$(openssl rand -hex 24)
  docker exec \
    -e CODER_USERNAME=owner \
    -e CODER_EMAIL=owner@remote-dev.local \
    -e CODER_PASSWORD="$owner_password" \
    remote-dev-coder-coder-1 \
    /opt/coder server create-admin-user >/dev/null
  printf 'CODER_USERNAME=owner\nCODER_EMAIL=owner@remote-dev.local\nCODER_PASSWORD=%s\n' \
    "$owner_password" > bootstrap-owner.env
  chmod 0600 bootstrap-owner.env
elif [[ "$first_status" != 200 ]]; then
  echo "unexpected Coder first-user API status: $first_status" >&2
  exit 1
fi

if [[ ! -s operator.session ]]; then
  if [[ ! -r bootstrap-owner.env ]]; then
    echo 'operator.session is missing and bootstrap credentials are unavailable' >&2
    echo 'create a Coder token named remote-dev-operator and store it mode 0600 at /opt/remote-dev/coder/operator.session' >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1091
  . ./bootstrap-owner.env
  set +a
  login_payload=$(python3 -c 'import json,os; print(json.dumps({"email": os.environ["CODER_EMAIL"], "password": os.environ["CODER_PASSWORD"]}))')
  umask 077
  curl -fsS -X POST "http://127.0.0.1:$coder_host_port/api/v2/users/login" \
    -H 'Content-Type: application/json' --data "$login_payload" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_token"])' \
    > operator.session
  chmod 0600 operator.session
fi

token=$(tr -d '\r\n' < operator.session)
template_hash=$(sha256sum \
  /opt/remote-dev/templates/docker/main.tf \
  /opt/remote-dev/templates/docker/README.md | sha256sum | cut -d' ' -f1)
if [[ ! -r template.sha256 || $(<template.sha256) != "$template_hash" ]]; then
  docker exec remote-dev-coder-coder-1 rm -rf /tmp/remote-dev-docker-template
  docker cp /opt/remote-dev/templates/docker remote-dev-coder-coder-1:/tmp/remote-dev-docker-template
  docker exec \
    -e CODER_URL=http://127.0.0.1:7080 \
    -e CODER_SESSION_TOKEN="$token" \
    remote-dev-coder-coder-1 \
    /opt/coder templates push remote-dev-docker \
    --directory /tmp/remote-dev-docker-template \
    --variable "remote_dev_coder_access_url=$coder_access_url" \
    --ignore-lockfile --yes --message "Reconciled by remote-dev install"
  printf '%s\n' "$template_hash" > template.sha256
fi

docker exec \
  -e CODER_URL=http://127.0.0.1:7080 \
  -e CODER_SESSION_TOKEN="$token" \
  remote-dev-coder-coder-1 \
  /opt/coder templates edit remote-dev-docker \
  --default-ttl 8h --activity-bump 1h \
  --display-name 'Remote Dev Docker' \
  --description 'Bounded Docker workspace' \
  --yes >/dev/null

curl -fsS "http://127.0.0.1:$coder_host_port/api/v2/buildinfo" >/dev/null
docker exec remote-dev-coder-database-1 pg_isready -U coder -d coder >/dev/null
python3 /opt/remote-dev/coder/workspace-images.py --port "$coder_host_port" --retain
echo 'remote-dev install reconciled successfully'
REMOTE
