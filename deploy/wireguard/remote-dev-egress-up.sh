#!/usr/bin/env bash
set -euo pipefail

runtime_profile=/etc/remote-dev/host-profile.env
test -s "$runtime_profile"
# shellcheck disable=SC1090
. "$runtime_profile"
: "${REMOTE_DEV_RUNTIME_PROFILE:?missing runtime profile identity}"
# Profiles written before egress modes existed use WireGuard egress.
egress_mode=${REMOTE_DEV_EGRESS:-wireguard}
: "${REMOTE_DEV_EGRESS_SUBNET:?missing egress subnet}"
: "${REMOTE_DEV_EGRESS_GATEWAY:?missing egress gateway}"
: "${REMOTE_DEV_EGRESS_MTU:?missing egress MTU}"
: "${REMOTE_DEV_CONTROL_SUBNET:?missing control subnet}"
if [[ $egress_mode == wireguard ]]; then
  : "${REMOTE_DEV_WIREGUARD_INTERFACE:?missing WireGuard interface}"
  : "${REMOTE_DEV_WIREGUARD_ADDRESS:?missing WireGuard address}"
  : "${REMOTE_DEV_POLICY_TABLE:?missing policy table}"
elif [[ $egress_mode != host ]]; then
  echo "unsupported REMOTE_DEV_EGRESS: $egress_mode" >&2
  exit 1
fi

interface=${REMOTE_DEV_WIREGUARD_INTERFACE:-}
wireguard_address=${REMOTE_DEV_WIREGUARD_ADDRESS:-}
source_subnet=$REMOTE_DEV_EGRESS_SUBNET
source_gateway=$REMOTE_DEV_EGRESS_GATEWAY
egress_network=remote-dev-egress
egress_mtu=$REMOTE_DEV_EGRESS_MTU
control_subnet=$REMOTE_DEV_CONTROL_SUBNET
control_network=remote-dev-control
filter_chain=REMOTE-DEV-EGRESS
table=${REMOTE_DEV_POLICY_TABLE:-}
config="/etc/wireguard/$interface.conf"
runtime_config="/run/wireguard/$interface.conf"
network_validator=/usr/local/lib/remote-dev/validate-docker-network.py

ensure_network() {
  local name=$1
  local subnet=$2
  local gateway=$3
  local internal=$4
  local mtu=$5
  shift
  shift 4
  if docker network inspect "$name" >/dev/null 2>&1; then
    docker network inspect "$name" | python3 "$network_validator" \
      "$name" "$subnet" "$gateway" "$internal" "$mtu" -
  else
    docker network create "$@" "$name" >/dev/null
  fi
}

ensure_networks() {
  ensure_network "$egress_network" \
    "$source_subnet" "$source_gateway" false "$egress_mtu" \
    --driver bridge --subnet "$source_subnet" --gateway "$source_gateway" \
    --opt "com.docker.network.driver.mtu=$egress_mtu"
  control_gateway=$(python3 -c \
    'import ipaddress,sys; print(next(ipaddress.ip_network(sys.argv[1]).hosts()))' \
    "$control_subnet")
  ensure_network "$control_network" \
    "$control_subnet" "$control_gateway" true - \
    --driver bridge --internal --subnet "$control_subnet" \
    --gateway "$control_gateway"
}

# Host egress: workspaces leave through the host's own default route with
# Docker's standard NAT. Only the two managed networks are needed.
if [[ $egress_mode == host ]]; then
  ensure_networks
  exit 0
fi

test -s "$config"
install -d -m 0700 /run/wireguard
awk '!/^(Address|DNS|MTU|Table|PreUp|PostUp|PreDown|PostDown)[[:space:]]*=/' \
  "$config" > "$runtime_config"
chmod 0600 "$runtime_config"

ip link show "$interface" >/dev/null 2>&1 || ip link add "$interface" type wireguard
wg setconf "$interface" "$runtime_config"
ip address replace "$wireguard_address" dev "$interface"
ip link set mtu "$egress_mtu" up dev "$interface"

ip route replace default dev "$interface" metric 10 table "$table"
ip route replace unreachable default metric 42760 table "$table"
ip rule show | grep -q "from $source_subnet lookup $table" || \
  ip rule add priority 1100 from "$source_subnet" lookup "$table"

iptables -N "$filter_chain" 2>/dev/null || true
iptables -F "$filter_chain"
iptables -A "$filter_chain" -d "$source_subnet" -i "$interface" \
  -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
iptables -A "$filter_chain" -s "$source_subnet" -o "$interface" -j ACCEPT
iptables -A "$filter_chain" -s "$source_subnet" -j REJECT
iptables -A "$filter_chain" -j RETURN
iptables -C DOCKER-USER -j "$filter_chain" 2>/dev/null || \
  iptables -I DOCKER-USER 1 -j "$filter_chain"

ensure_networks
