#!/usr/bin/env bash
set -euo pipefail

# Keep a Docker reference while Coder destroys stopped workspace containers.
# This container is never started and owns no volumes, ports, or host mounts.
image=${1:?usage: retain-workspace-image.sh image source-sha256}
source_hash=${2:?source-sha256 is required}
[[ $source_hash =~ ^[0-9a-f]{64}$ ]] || exit 2
actual_hash=$(docker image inspect --format \
  '{{index .Config.Labels "id0.remote-dev.workspace-source-sha256"}}' "$image")
[[ $actual_hash == "$source_hash" ]] || {
  echo 'IMAGE_SOURCE_MISMATCH: refusing retention of an unverified image' >&2
  exit 1
}
image_id=$(docker image inspect --format '{{.Id}}' "$image")
name=remote-dev-image-retention-${image_id#sha256:}
if docker container inspect "$name" >/dev/null 2>&1; then
  identity=$(docker container inspect --format \
    '{{.Image}}|{{.State.Status}}|{{index .Config.Labels "id0.remote-dev.purpose"}}' "$name")
  [[ $identity == "$image_id|created|image-retention" ]] || {
    echo "IMAGE_RETENTION_CONFLICT: inspect $name; no resource was replaced" >&2
    exit 1
  }
else
  docker create --name "$name" --network none --entrypoint /bin/true \
    --label id0.remote-dev.purpose=image-retention \
    --label id0.remote-dev.owner=remote-dev "$image" >/dev/null
fi
printf 'RETAINED %s via %s (never started)\n' "$image" "$name"
