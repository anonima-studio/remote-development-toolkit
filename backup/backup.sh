#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "backup must run as root" >&2
  exit 1
fi

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY is required}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE is required}"

staging_dir=/var/lib/remote-dev/backup-staging
dump_file="$staging_dir/coder.sql.gz"
volume_inventory="$staging_dir/workspace-volumes.txt"
install -d -m 0700 "$staging_dir"
volume_names_file=$(mktemp)
expected_workspace_ids_file=$(mktemp)
discovered_workspace_ids_file=$(mktemp)
trap 'rm -f "$dump_file" "$volume_names_file" "$expected_workspace_ids_file" "$discovered_workspace_ids_file"' EXIT

docker exec remote-dev-coder-database-1 \
  pg_dump --clean --if-exists --no-owner --no-privileges -U coder -d coder \
  | gzip -9 > "$dump_file"
test -s "$dump_file"

if ! docker volume ls --filter label=coder.workspace_id --format '{{.Name}}' \
  | sort > "$volume_names_file"; then
  echo "failed to enumerate Coder workspace volumes" >&2
  exit 1
fi

if ! docker exec remote-dev-coder-database-1 \
  psql -At -U coder -d coder \
  -c 'SELECT id FROM workspaces WHERE deleted = false ORDER BY id' \
  > "$expected_workspace_ids_file"; then
  echo "failed to enumerate workspaces from Coder PostgreSQL" >&2
  exit 1
fi

mapfile -t volume_names < "$volume_names_file"
home_paths=()
printf 'volume_name\tworkspace_id\towner\towner_id\tworkspace_name_at_creation\tmountpoint\n' > "$volume_inventory"
: > "$discovered_workspace_ids_file"
for volume_name in "${volume_names[@]}"; do
  [[ -n "$volume_name" ]] || continue
  mountpoint=$(docker volume inspect "$volume_name" --format '{{.Mountpoint}}')
  workspace_id=$(docker volume inspect "$volume_name" --format '{{index .Labels "coder.workspace_id"}}')
  owner=$(docker volume inspect "$volume_name" --format '{{index .Labels "coder.owner"}}')
  owner_id=$(docker volume inspect "$volume_name" --format '{{index .Labels "coder.owner_id"}}')
  workspace_name=$(docker volume inspect "$volume_name" --format '{{index .Labels "coder.workspace_name_at_creation"}}')
  if [[ -z "$workspace_id" || -z "$owner" || -z "$owner_id" || -z "$workspace_name" ]]; then
    echo "workspace volume has incomplete recovery labels: $volume_name" >&2
    exit 1
  fi
  if [[ ! -d "$mountpoint" ]]; then
    echo "workspace volume mountpoint is missing: $volume_name -> $mountpoint" >&2
    exit 1
  fi
  printf '%s\n' "$workspace_id" >> "$discovered_workspace_ids_file"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$volume_name" "$workspace_id" "$owner" "$owner_id" "$workspace_name" "$mountpoint" \
    >> "$volume_inventory"
  home_paths+=("$mountpoint")
done

sort -u -o "$discovered_workspace_ids_file" "$discovered_workspace_ids_file"
if ! diff -u "$expected_workspace_ids_file" "$discovered_workspace_ids_file"; then
  echo "Coder workspace state and labeled home volumes differ; refusing incomplete backup" >&2
  exit 1
fi

backup_paths=(/opt/remote-dev "$staging_dir")
backup_paths+=("${home_paths[@]}")

restic backup \
  --tag remote-dev \
  --host "$(hostname)" \
  --exclude-caches \
  --one-file-system \
  "${backup_paths[@]}"

restic check --read-data-subset="${RESTIC_CHECK_SUBSET:-1/50}"
