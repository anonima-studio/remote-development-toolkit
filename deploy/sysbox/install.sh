#!/usr/bin/env bash
set -euo pipefail

version=0.7.1
package="sysbox-ce_${version}.linux_amd64.deb"
checksum=9d6d5484f980d0a17f86c492c1262015c2afb66280bdb97215b79fde6a0261c5
url="https://github.com/nestybox/sysbox/releases/download/v${version}/${package}"
download="/tmp/${package}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
apparmor_profile=/etc/apparmor.d/fusermount3
apparmor_local=/etc/apparmor.d/local/fusermount3
apparmor_source="$script_dir/apparmor-local-fusermount3"

configure_fusermount_apparmor() {
  if [[ ! -f $apparmor_profile ]]; then
    return 0
  fi

  [[ -f $apparmor_source && ! -L $apparmor_source ]] || {
    echo "Missing canonical Sysbox AppArmor policy: $apparmor_source" >&2
    exit 1
  }
  if [[ -e $apparmor_local || -L $apparmor_local ]]; then
    if [[ -L $apparmor_local || ! -f $apparmor_local ]] || \
       ! cmp --silent "$apparmor_source" "$apparmor_local"; then
      echo "Refusing to replace an existing non-canonical $apparmor_local." >&2
      echo 'Merge the Sysbox mount rules explicitly, then rerun this installer.' >&2
      exit 1
    fi
  fi

  install -D -o root -g root -m 0644 "$apparmor_source" "$apparmor_local"
  apparmor_parser -r "$apparmor_profile"
  apparmor_parser -Q "$apparmor_profile"
}

# Refuse a policy conflict before downloading packages or restarting Docker.
configure_fusermount_apparmor

if [[ $(dpkg --print-architecture) != amd64 ]]; then
  echo 'This installer is pinned for amd64 hosts.' >&2
  exit 1
fi

if dpkg-query -W -f='${db:Status-Abbrev} ${Version}\n' sysbox-ce 2>/dev/null \
  | grep -q "^ii  ${version}"; then
  systemctl is-active --quiet sysbox-mgr
  systemctl is-active --quiet sysbox-fs
  docker info --format '{{json .Runtimes}}' | grep -q 'sysbox-runc'
  echo "Sysbox ${version} is already active."
  exit 0
fi

curl -fL "$url" -o "$download"
printf '%s  %s\n' "$checksum" "$download" | sha256sum --check
DEBIAN_FRONTEND=noninteractive apt-get install -y "$download"
rm -f "$download"

systemctl is-active --quiet sysbox-mgr
systemctl is-active --quiet sysbox-fs
docker info --format '{{json .Runtimes}}' | grep -q 'sysbox-runc'
sysbox-runc --version
