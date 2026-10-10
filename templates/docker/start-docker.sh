#!/usr/bin/env bash
set -euo pipefail

state_dir="$HOME/.local/state/remote-dev"
log_file="$state_dir/dockerd.log"
docker_mtu="${REMOTE_DEV_DOCKER_MTU:-1420}"
mkdir -p "$state_dir"

if docker info >/dev/null 2>&1; then
  exit 0
fi

sudo install -d -o root -g root -m 0755 /var/run/remote-dev
sudo sh -c "nohup /usr/local/bin/dockerd \
  --host=unix:///var/run/docker.sock \
  --group=coder \
  --data-root=/var/lib/docker \
  --default-network-opt='bridge=com.docker.network.driver.mtu=$docker_mtu' \
  --mtu='$docker_mtu' \
  --pidfile=/var/run/remote-dev/dockerd.pid \
  >'$log_file' 2>&1 </dev/null &"

for _ in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

tail -n 80 "$log_file" >&2 || true
echo 'Docker daemon did not become ready within 60 seconds' >&2
exit 1
