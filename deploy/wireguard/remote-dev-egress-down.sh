#!/usr/bin/env bash
set -euo pipefail

runtime_profile=/etc/remote-dev/host-profile.env
test -s "$runtime_profile"
# shellcheck disable=SC1090
. "$runtime_profile"
: "${REMOTE_DEV_RUNTIME_PROFILE:?missing runtime profile identity}"
# Host egress installs no routing state; the managed Docker networks stay.
[[ ${REMOTE_DEV_EGRESS:-wireguard} == wireguard ]] || exit 0
: "${REMOTE_DEV_WIREGUARD_INTERFACE:?missing WireGuard interface}"
: "${REMOTE_DEV_EGRESS_SUBNET:?missing egress subnet}"
: "${REMOTE_DEV_POLICY_TABLE:?missing policy table}"

interface=$REMOTE_DEV_WIREGUARD_INTERFACE
source_subnet=$REMOTE_DEV_EGRESS_SUBNET
table=$REMOTE_DEV_POLICY_TABLE
filter_chain=REMOTE-DEV-EGRESS

iptables -D DOCKER-USER -j "$filter_chain" 2>/dev/null || true
iptables -F "$filter_chain" 2>/dev/null || true
iptables -X "$filter_chain" 2>/dev/null || true
iptables -D DOCKER-USER -s "$source_subnet" -o "$interface" -j ACCEPT 2>/dev/null || true
iptables -D DOCKER-USER -d "$source_subnet" -i "$interface" \
  -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
iptables -D DOCKER-USER -s "$source_subnet" -j REJECT 2>/dev/null || true
iptables -D FORWARD -s "$source_subnet" -o "$interface" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -d "$source_subnet" -i "$interface" \
  -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
iptables -t mangle -D FORWARD -s "$source_subnet" -o "$interface" \
  -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true
iptables -t mangle -D FORWARD -s "$source_subnet" -o "$interface" \
  -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1280 2>/dev/null || true
ip link delete "$interface" 2>/dev/null || true
ip route replace unreachable default metric 42760 table "$table"
ip rule show | grep -q "from $source_subnet lookup $table" || \
  ip rule add priority 1100 from "$source_subnet" lookup "$table"
