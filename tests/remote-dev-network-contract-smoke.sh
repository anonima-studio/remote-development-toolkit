#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
validator="$repo_root/deploy/wireguard/validate-docker-network.py"
fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT

write_network() {
  local path=$1 subnet=$2 gateway=$3 internal=$4 mtu=$5
  python3 - "$path" "$subnet" "$gateway" "$internal" "$mtu" <<'PY'
import json
import sys

path, subnet, gateway, internal, mtu = sys.argv[1:]
payload = [{
    "Name": "remote-dev-egress",
    "Driver": "bridge",
    "Internal": internal == "true",
    "IPAM": {"Config": [{"Subnet": subnet, "Gateway": gateway}]},
    "Options": {"com.docker.network.driver.mtu": mtu},
    "Containers": {"id": {"Name": "remote-dev-coder-coder-1"}},
}]
with open(path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream)
PY
}

write_network "$fixture/network.json" 10.231.10.0/24 10.231.10.1 false 1420
python3 "$validator" remote-dev-egress 10.231.10.0/24 10.231.10.1 false 1420 \
  remote-dev-coder-coder-1 < "$fixture/network.json"

for mismatch in subnet gateway internal mtu attachment; do
  write_network "$fixture/network.json" 10.231.10.0/24 10.231.10.1 false 1420
  args=(remote-dev-egress 10.231.10.0/24 10.231.10.1 false 1420 remote-dev-coder-coder-1)
  case "$mismatch" in
    subnet) args[1]=10.231.12.0/24 ;;
    gateway) args[2]=10.231.10.254 ;;
    internal) args[3]=true ;;
    mtu) args[4]=1500 ;;
    attachment) args[5]=wrong-container ;;
  esac
  if python3 "$validator" "${args[@]}" < "$fixture/network.json" >/dev/null 2>&1; then
    echo "expected Docker network $mismatch mismatch to be refused" >&2
    exit 1
  fi
done

printf 'PASS     exact Docker network contract smoke completed\n'

dropin="$repo_root/deploy/networkd/10-remote-dev-egress.conf"
installer_code=$(grep -Ev '^[[:space:]]*#' "$repo_root/deploy/install.sh")
python3 - "$dropin" <<'PY'
import configparser
import sys

parser = configparser.ConfigParser(interpolation=None, strict=True)
try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        parser.read_file(stream)
    for key in ("ManageForeignRoutingPolicyRules", "ManageForeignRoutes"):
        if parser.get("Network", key) != "no":
            raise SystemExit(f"networkd drop-in must set {key}=no in [Network]")
except configparser.Error as error:
    raise SystemExit(f"networkd drop-in is not valid: {error}")
PY
grep -Fq '/etc/systemd/networkd.conf.d/10-remote-dev-egress.conf' <<<"$installer_code" || {
  echo 'installer must place the networkd drop-in' >&2
  exit 1
}
if grep -Eq 'systemd-networkd.*(restart|reload|reconfigure|stop|kill)|(restart|reload|reconfigure|stop|kill).*systemd-networkd|networkctl' <<<"$installer_code"; then
  echo 'installer must not restart or reload systemd-networkd' >&2
  exit 1
fi
printf 'PASS     networkd drop-in file and installer wiring match the contract\n'
