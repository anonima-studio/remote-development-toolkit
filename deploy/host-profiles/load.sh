#!/usr/bin/env bash

remote_dev_profile_fail() {
  printf 'ERROR    %s\n' "$*" >&2
  return 1
}

remote_dev_variable_is_set() {
  declare -p "$1" >/dev/null 2>&1
}

# Profiles live in the repository by default. REMOTE_DEV_PROFILE_DIR selects
# another reviewed directory, for example fixtures in tests.
remote_dev_profile_dir() {
  printf '%s\n' "${REMOTE_DEV_PROFILE_DIR:-$1/deploy/host-profiles}"
}

# Ingress and egress are chosen per profile. Missing values keep the original
# topology: Cloudflare Tunnel ingress and WireGuard egress.
remote_dev_identity_fields() {
  local ingress=$1 egress=$2
  printf '%s\n' REMOTE_DEV_SSH_HOST REMOTE_DEV_CODER_ACCESS_URL REMOTE_DEV_CODER_HOST_PORT
  if [[ $ingress == cloudflare-tunnel ]]; then
    printf '%s\n' REMOTE_DEV_CLOUDFLARE_TUNNEL_ID REMOTE_DEV_CLOUDFLARE_HOSTNAME
  fi
  if [[ $egress == wireguard ]]; then
    printf '%s\n' REMOTE_DEV_WIREGUARD_ADDRESS REMOTE_DEV_WIREGUARD_PUBLIC_KEY \
      REMOTE_DEV_WIREGUARD_INTERFACE
  fi
  printf '%s\n' REMOTE_DEV_EGRESS_SUBNET REMOTE_DEV_EGRESS_GATEWAY REMOTE_DEV_EGRESS_MTU \
    REMOTE_DEV_CONTROL_SUBNET
  if [[ $egress == wireguard ]]; then
    printf '%s\n' REMOTE_DEV_POLICY_TABLE
  fi
}

remote_dev_load_profile() {
  local repo_root=$1
  local requested_profile profile_file field profile_field profile_value
  local ingress_mode egress_mode
  local override_count=0 missing_count=0
  local -a identity_fields missing_fields override_fields known_override_fields

  if (( $# >= 2 )); then
    requested_profile=$2
  else
    requested_profile=${REMOTE_DEV_PROFILE:-}
  fi

  # There is no default server: every operation names its deployment identity.
  [[ -n "$requested_profile" ]] || \
    remote_dev_profile_fail 'select a host profile with --profile <name> or REMOTE_DEV_PROFILE'
  [[ "$requested_profile" =~ ^[a-z0-9][a-z0-9-]*$ ]] || \
    remote_dev_profile_fail "invalid profile name: $requested_profile"
  profile_file="$(remote_dev_profile_dir "$repo_root")/$requested_profile.env"
  [[ -f "$profile_file" && ! -L "$profile_file" ]] || \
    remote_dev_profile_fail "unknown remote-dev profile: $requested_profile"

  if remote_dev_variable_is_set REMOTE_DEV_HOST; then
    ! remote_dev_variable_is_set REMOTE_DEV_SSH_HOST || \
      remote_dev_profile_fail 'REMOTE_DEV_HOST and REMOTE_DEV_SSH_HOST cannot both be set'
    REMOTE_DEV_SSH_HOST=$REMOTE_DEV_HOST
  fi
  if remote_dev_variable_is_set REMOTE_DEV_CODER_URL; then
    ! remote_dev_variable_is_set REMOTE_DEV_CODER_ACCESS_URL || \
      remote_dev_profile_fail 'REMOTE_DEV_CODER_URL and REMOTE_DEV_CODER_ACCESS_URL cannot both be set'
    REMOTE_DEV_CODER_ACCESS_URL=$REMOTE_DEV_CODER_URL
  fi

  # The topology modes are read before the identity so that overrides are
  # checked against the fields the selected modes actually use.
  ingress_mode=$(sed -n 's/^REMOTE_DEV_PROFILE_INGRESS=//p' "$profile_file" | tail -n 1)
  egress_mode=$(sed -n 's/^REMOTE_DEV_PROFILE_EGRESS=//p' "$profile_file" | tail -n 1)
  ingress_mode=${ingress_mode:-cloudflare-tunnel}
  egress_mode=${egress_mode:-wireguard}
  [[ $ingress_mode == cloudflare-tunnel || $ingress_mode == ssh-forward ]] || \
    remote_dev_profile_fail "unsupported ingress in $profile_file: $ingress_mode"
  [[ $egress_mode == wireguard || $egress_mode == host ]] || \
    remote_dev_profile_fail "unsupported egress in $profile_file: $egress_mode"
  identity_fields=()
  while IFS= read -r field; do
    identity_fields+=("$field")
  done < <(remote_dev_identity_fields "$ingress_mode" "$egress_mode")
  for field in "${identity_fields[@]}"; do
    if remote_dev_variable_is_set "$field"; then
      override_fields+=("$field")
    fi
  done

  REMOTE_DEV_PROFILE_NAME=''
  REMOTE_DEV_PROFILE_INGRESS=''
  REMOTE_DEV_PROFILE_EGRESS=''
  REMOTE_DEV_PROFILE_EXPECTED_HOSTNAME=''
  REMOTE_DEV_PROFILE_SYSTEM_ROLE=''
  REMOTE_DEV_PROFILE_DISPLAY_NAME=''
  REMOTE_DEV_PROFILE_SSH_HOST=''
  REMOTE_DEV_PROFILE_CODER_ACCESS_URL=''
  REMOTE_DEV_PROFILE_COOLIFY_ACCESS_URL=''
  REMOTE_DEV_PROFILE_CODER_HOST_PORT=''
  REMOTE_DEV_PROFILE_CLOUDFLARE_TUNNEL_ID=''
  REMOTE_DEV_PROFILE_CLOUDFLARE_HOSTNAME=''
  REMOTE_DEV_PROFILE_CLOUDFLARE_CREDENTIALS=''
  REMOTE_DEV_PROFILE_WIREGUARD_ADDRESS=''
  REMOTE_DEV_PROFILE_WIREGUARD_PUBLIC_KEY=''
  REMOTE_DEV_PROFILE_WIREGUARD_CONFIG=''
  REMOTE_DEV_PROFILE_WIREGUARD_INTERFACE=''
  REMOTE_DEV_PROFILE_EGRESS_SUBNET=''
  REMOTE_DEV_PROFILE_EGRESS_GATEWAY=''
  REMOTE_DEV_PROFILE_EGRESS_MTU=''
  REMOTE_DEV_PROFILE_CONTROL_SUBNET=''
  REMOTE_DEV_PROFILE_POLICY_TABLE=''
  REMOTE_DEV_PROFILE_TERRAFORM_MIRROR=''

  # shellcheck disable=SC1090
  . "$profile_file"

  [[ "$REMOTE_DEV_PROFILE_NAME" == "$requested_profile" ]] || \
    remote_dev_profile_fail "profile identity mismatch in $profile_file"

  for field in "${identity_fields[@]}"; do
    profile_field=${field/REMOTE_DEV_/REMOTE_DEV_PROFILE_}
    profile_value=${!profile_field}
    if [[ -z "$profile_value" ]]; then
      missing_fields+=("$field")
      missing_count=$((missing_count + 1))
      if remote_dev_variable_is_set "$field"; then
        override_count=$((override_count + 1))
      fi
    elif remote_dev_variable_is_set "$field"; then
      known_override_fields+=("$field")
    fi
  done

  if (( missing_count == 0 && ${#override_fields[@]} > 0 )); then
    remote_dev_profile_fail \
      "profile $requested_profile has immutable identity; remove env overrides: ${override_fields[*]}"
  fi
  if (( ${#known_override_fields[@]} > 0 )); then
    remote_dev_profile_fail \
      "checked-in identity fields cannot be overridden for profile $requested_profile: ${known_override_fields[*]}"
  fi
  if (( override_count > 0 && override_count != missing_count )); then
    remote_dev_profile_fail \
      "incomplete profile $requested_profile requires an all-or-none identity set: ${missing_fields[*]}"
  fi

  for field in "${identity_fields[@]}"; do
    profile_field=${field/REMOTE_DEV_/REMOTE_DEV_PROFILE_}
    profile_value=${!profile_field}
    if [[ -n "$profile_value" ]]; then
      printf -v "$field" '%s' "$profile_value"
    elif ! remote_dev_variable_is_set "$field"; then
      printf -v "$field" '%s' ''
    fi
  done

  # Fields unused by the selected modes are defined as empty values so that
  # callers running with nounset can test them uniformly.
  while IFS= read -r field; do
    remote_dev_variable_is_set "$field" || printf -v "$field" '%s' ''
  done < <(remote_dev_identity_fields cloudflare-tunnel wireguard)

  REMOTE_DEV_PROFILE=$requested_profile
  REMOTE_DEV_INGRESS=$ingress_mode
  REMOTE_DEV_EGRESS=$egress_mode
  REMOTE_DEV_EXPECTED_HOSTNAME=$REMOTE_DEV_PROFILE_EXPECTED_HOSTNAME
  REMOTE_DEV_SYSTEM_ROLE=$REMOTE_DEV_PROFILE_SYSTEM_ROLE
  REMOTE_DEV_DISPLAY_NAME=$REMOTE_DEV_PROFILE_DISPLAY_NAME
  REMOTE_DEV_CODER_URL=$REMOTE_DEV_CODER_ACCESS_URL
  REMOTE_DEV_COOLIFY_ACCESS_URL=$REMOTE_DEV_PROFILE_COOLIFY_ACCESS_URL
  REMOTE_DEV_CLOUDFLARE_CREDENTIALS=${REMOTE_DEV_CLOUDFLARE_CREDENTIALS:-$REMOTE_DEV_PROFILE_CLOUDFLARE_CREDENTIALS}
  REMOTE_DEV_WIREGUARD_CONFIG=${REMOTE_DEV_WIREGUARD_CONFIG:-$REMOTE_DEV_PROFILE_WIREGUARD_CONFIG}
  REMOTE_DEV_TERRAFORM_MIRROR=$REMOTE_DEV_PROFILE_TERRAFORM_MIRROR

  export REMOTE_DEV_PROFILE REMOTE_DEV_INGRESS REMOTE_DEV_EGRESS
}

remote_dev_require_profile_fields() {
  local operation=$1
  shift
  local field
  local -a missing=()

  for field in "$@"; do
    [[ -n "${!field:-}" ]] || missing+=("$field")
  done
  if (( ${#missing[@]} > 0 )); then
    remote_dev_profile_fail \
      "profile $REMOTE_DEV_PROFILE is incomplete for $operation; set: ${missing[*]}"
  fi
}

remote_dev_validate_profile_identity() {
  if [[ -n "$REMOTE_DEV_EXPECTED_HOSTNAME" ]]; then
    [[ "$REMOTE_DEV_EXPECTED_HOSTNAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_EXPECTED_HOSTNAME has an unsupported value'
  fi
  if [[ -n "$REMOTE_DEV_SYSTEM_ROLE" ]]; then
    [[ "$REMOTE_DEV_SYSTEM_ROLE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_SYSTEM_ROLE has an unsupported value'
  fi
  [[ "$REMOTE_DEV_DISPLAY_NAME" != *$'\n'* && "$REMOTE_DEV_DISPLAY_NAME" != *$'\r'* ]] || \
    remote_dev_profile_fail 'REMOTE_DEV_DISPLAY_NAME must be single-line text'
  if [[ -n "$REMOTE_DEV_COOLIFY_ACCESS_URL" ]]; then
    python3 - "$REMOTE_DEV_COOLIFY_ACCESS_URL" <<'PY'
import sys
import urllib.parse

parsed = urllib.parse.urlsplit(sys.argv[1])
if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
        parsed.password or parsed.path or parsed.query or parsed.fragment):
    raise SystemExit("ERROR    REMOTE_DEV_COOLIFY_ACCESS_URL must be an https origin without credentials or a path")
try:
    parsed.port
except ValueError:
    raise SystemExit("ERROR    REMOTE_DEV_COOLIFY_ACCESS_URL contains an invalid port")
PY
  fi
  if [[ -n "$REMOTE_DEV_TERRAFORM_MIRROR" ]]; then
    [[ "$REMOTE_DEV_TERRAFORM_MIRROR" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/([A-Za-z0-9._~/-]*)$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_TERRAFORM_MIRROR must be an https URL ending with /'
  fi
  [[ "$REMOTE_DEV_SSH_HOST" =~ ^[A-Za-z0-9._@+-]+$ ]] || \
    remote_dev_profile_fail 'REMOTE_DEV_SSH_HOST has an unsupported value'
  if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel ]]; then
    [[ "$REMOTE_DEV_CLOUDFLARE_TUNNEL_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_CLOUDFLARE_TUNNEL_ID must be a lowercase UUID'
    [[ "$REMOTE_DEV_CLOUDFLARE_HOSTNAME" =~ ^[A-Za-z0-9.-]+$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_CLOUDFLARE_HOSTNAME has an unsupported value'
  fi
  if [[ $REMOTE_DEV_EGRESS == wireguard ]]; then
    [[ "$REMOTE_DEV_WIREGUARD_INTERFACE" =~ ^[A-Za-z0-9_.-]+$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_WIREGUARD_INTERFACE has an unsupported value'
    [[ "$REMOTE_DEV_WIREGUARD_PUBLIC_KEY" =~ ^[A-Za-z0-9+/]{43}=$ ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_WIREGUARD_PUBLIC_KEY must be a base64 WireGuard public key'
  fi

  python3 - \
    "$REMOTE_DEV_INGRESS" \
    "$REMOTE_DEV_EGRESS" \
    "$REMOTE_DEV_CODER_ACCESS_URL" \
    "$REMOTE_DEV_CODER_HOST_PORT" \
    "$REMOTE_DEV_CLOUDFLARE_HOSTNAME" \
    "$REMOTE_DEV_WIREGUARD_ADDRESS" \
    "$REMOTE_DEV_EGRESS_SUBNET" \
    "$REMOTE_DEV_EGRESS_GATEWAY" \
    "$REMOTE_DEV_EGRESS_MTU" \
    "$REMOTE_DEV_CONTROL_SUBNET" \
    "$REMOTE_DEV_POLICY_TABLE" <<'PY'
import ipaddress
import sys
import urllib.parse

(ingress, egress_mode, origin, port_text, tunnel_hostname, wireguard_text,
 egress_text, gateway_text, mtu_text, control_text, table_text) = sys.argv[1:]

parsed = urllib.parse.urlsplit(origin)
if (not parsed.hostname or parsed.username or parsed.password or parsed.path or
        parsed.query or parsed.fragment):
    raise SystemExit("ERROR    REMOTE_DEV_CODER_ACCESS_URL must be an origin without credentials or a path")
try:
    access_port = parsed.port
except ValueError:
    raise SystemExit("ERROR    REMOTE_DEV_CODER_ACCESS_URL contains an invalid port")
if ingress == "cloudflare-tunnel":
    if parsed.scheme != "https":
        raise SystemExit("ERROR    REMOTE_DEV_CODER_ACCESS_URL must be an https origin")
    if parsed.hostname != tunnel_hostname:
        raise SystemExit("ERROR    Coder access host must equal the Cloudflare hostname")
else:
    # ssh-forward publishes nothing: the browser reaches Coder through a local
    # SSH forward, so the access URL is the Mac-side loopback endpoint.
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not access_port:
        raise SystemExit("ERROR    ssh-forward requires REMOTE_DEV_CODER_ACCESS_URL=http://127.0.0.1:<local-port>")
try:
    port = int(port_text)
except ValueError:
    raise SystemExit("ERROR    REMOTE_DEV_CODER_HOST_PORT must be numeric")
if not 1 <= port <= 65535:
    raise SystemExit("ERROR    REMOTE_DEV_CODER_HOST_PORT must be between 1 and 65535")

try:
    egress = ipaddress.ip_network(egress_text, strict=True)
    gateway = ipaddress.ip_address(gateway_text)
    control = ipaddress.ip_network(control_text, strict=True)
    networks = [egress, control]
    if egress_mode == "wireguard":
        wireguard = ipaddress.ip_interface(wireguard_text)
        networks.append(wireguard.network)
except ValueError as error:
    raise SystemExit(f"ERROR    invalid IPv4 network profile value: {error}")
if not all(value.version == 4 for value in networks + [gateway]):
    raise SystemExit("ERROR    WireGuard and Docker network values must be IPv4")
if gateway not in egress or gateway in (egress.network_address, egress.broadcast_address):
    raise SystemExit("ERROR    egress gateway must be a usable address inside REMOTE_DEV_EGRESS_SUBNET")
if any(a.overlaps(b) for i, a in enumerate(networks) for b in networks[i + 1:]):
    raise SystemExit("ERROR    WireGuard, egress, and control subnets must not overlap")

try:
    mtu = int(mtu_text)
except ValueError:
    raise SystemExit("ERROR    MTU and policy table must be numeric")
if not 1280 <= mtu <= 9000:
    raise SystemExit("ERROR    REMOTE_DEV_EGRESS_MTU must be between 1280 and 9000")
if egress_mode == "wireguard":
    try:
        table = int(table_text)
    except ValueError:
        raise SystemExit("ERROR    MTU and policy table must be numeric")
    if not 1 <= table <= 4294967295 or table in (253, 254, 255):
        raise SystemExit("ERROR    REMOTE_DEV_POLICY_TABLE must be a non-reserved value between 1 and 4294967295")
PY
}

remote_dev_validate_profile_values() {
  remote_dev_validate_profile_identity
  if [[ $REMOTE_DEV_INGRESS == cloudflare-tunnel ]]; then
    [[ "$REMOTE_DEV_CLOUDFLARE_CREDENTIALS" == /* && \
       "$REMOTE_DEV_CLOUDFLARE_CREDENTIALS" != *$'\n'* && \
       "$REMOTE_DEV_CLOUDFLARE_CREDENTIALS" != *$'\r'* ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_CLOUDFLARE_CREDENTIALS must be a single-line absolute path'
  fi
  if [[ $REMOTE_DEV_EGRESS == wireguard ]]; then
    [[ "$REMOTE_DEV_WIREGUARD_CONFIG" == /* && \
       "$REMOTE_DEV_WIREGUARD_CONFIG" != *$'\n'* && \
       "$REMOTE_DEV_WIREGUARD_CONFIG" != *$'\r'* ]] || \
      remote_dev_profile_fail 'REMOTE_DEV_WIREGUARD_CONFIG must be a single-line absolute path'
  fi
}
