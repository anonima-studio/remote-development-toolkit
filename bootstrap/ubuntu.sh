#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run this script as root" >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
if [[ ${ID:-} != ubuntu || ! ${VERSION_ID:-} =~ ^(24\.04|26\.04)$ ]]; then
  echo "supported baselines are Ubuntu 24.04 and 26.04; found ${ID:-unknown} ${VERSION_ID:-unknown}" >&2
  exit 1
fi

: "${REMOTE_DEV_PUBLIC_KEY:?set REMOTE_DEV_PUBLIC_KEY to one complete SSH public key}"
if [[ ! "$REMOTE_DEV_PUBLIC_KEY" =~ ^ssh-(ed25519|rsa)[[:space:]] ]]; then
  echo "REMOTE_DEV_PUBLIC_KEY is not a supported OpenSSH public key" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y full-upgrade
apt-get install -y --no-install-recommends \
  ca-certificates curl git jq needrestart openssl restic rsync \
  unattended-upgrades ufw

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer

if ! id dev >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash dev
fi
install -d -o dev -g dev -m 0700 /home/dev/.ssh
printf '%s\n' "$REMOTE_DEV_PUBLIC_KEY" > /home/dev/.ssh/authorized_keys
chown dev:dev /home/dev/.ssh/authorized_keys
chmod 0600 /home/dev/.ssh/authorized_keys
printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-remote-dev
chmod 0440 /etc/sudoers.d/90-remote-dev
visudo -cf /etc/sudoers.d/90-remote-dev

install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
architecture=$(dpkg --print-architecture)
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: ${architecture}
Signed-By: /etc/apt/keyrings/docker.asc
EOF
rm -f /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker dev
systemctl enable --now docker

cat > /etc/ssh/sshd_config.d/00-remote-dev-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers dev
MaxAuthTries 3
X11Forwarding no
EOF
sshd -t

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

install -d -o dev -g dev -m 0750 /opt/remote-dev
systemctl reload ssh

echo "bootstrap complete; prove a fresh dev login before closing this root session"
