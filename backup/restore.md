# Clean-host restore rehearsal

This runbook is destructive to its target and must be rehearsed on a disposable
clean Ubuntu 24.04 or 26.04 host, never on the primary. Keep the restored host isolated
from production DNS and user traffic until validation completes.

This procedure follows the backup script's output format. It has not been
rehearsed as part of this release; record your own rehearsal before relying
on it.

1. Run `bootstrap/ubuntu.sh` with the recovery SSH key, and create a separate
   profile for the recovery host (`bin/remote-dev init`). Do not run
   `remote-dev install` yet.
2. Configure a read-only or maintenance restic identity on the clean host. List
   snapshots and select one explicit snapshot ID; never restore implicit
   `latest` during an acceptance drill.
3. Restore into a staging directory, not `/`:

   ```bash
   sudo install -d -m 0700 /var/lib/remote-dev/restore
   sudo --preserve-env=RESTIC_REPOSITORY,RESTIC_PASSWORD_FILE \
     restic restore SNAPSHOT_ID --target /var/lib/remote-dev/restore
   ```

4. Inspect the restored tree and SQL dump. Copy the restored
   `opt/remote-dev` to `/opt/remote-dev`, preserving ownership and modes. Do not
   copy the old live PostgreSQL Docker volume; the SQL dump is authoritative.
5. In `/opt/remote-dev/coder`, start only PostgreSQL, wait for health, and
   restore the authoritative SQL dump before reconstructing workspace volumes:

   ```bash
   docker compose up -d database
   gunzip -c /var/lib/remote-dev/restore/var/lib/remote-dev/backup-staging/coder.sql.gz \
     | docker exec -i remote-dev-coder-database-1 psql -U coder -d coder
   ```

6. Read the restored
   `var/lib/remote-dev/backup-staging/workspace-volumes.txt`. Its header is
   followed by one tab-separated row per workspace home containing the exact
   volume name and the four recovery labels. Materialize the rows into a checked
   file, compare their workspace IDs with the restored Coder database, and only
   then recreate the labeled volumes. Run this from
   `/var/lib/remote-dev/restore`:

   ```bash
   set -euo pipefail
   inventory=var/lib/remote-dev/backup-staging/workspace-volumes.txt
   expected_header=$'volume_name\tworkspace_id\towner\towner_id\tworkspace_name_at_creation\tmountpoint'
   [[ -r "$inventory" ]]
   IFS= read -r actual_header < "$inventory"
   [[ "$actual_header" == "$expected_header" ]]

   inventory_rows=$(mktemp)
   expected_ids=$(mktemp)
   inventory_ids=$(mktemp)
   trap 'rm -f "$inventory_rows" "$expected_ids" "$inventory_ids"' EXIT
   tail -n +2 "$inventory" > "$inventory_rows"
   docker exec remote-dev-coder-database-1 \
     psql -At -U coder -d coder \
     -c 'SELECT id FROM workspaces WHERE deleted = false' \
     | LC_ALL=C sort > "$expected_ids"
   cut -f2 "$inventory_rows" | LC_ALL=C sort > "$inventory_ids"
   diff -u "$expected_ids" "$inventory_ids"

   while IFS=$'\t' read -r volume workspace_id owner owner_id workspace_name old_mountpoint; do
     [[ -n "$volume" && -n "$workspace_id" && -n "$owner" && -n "$owner_id" \
       && -n "$workspace_name" && -n "$old_mountpoint" ]]
     ! docker volume inspect "$volume" >/dev/null 2>&1
     source_path=".$old_mountpoint"
     [[ -d "$source_path" ]]
     docker volume create \
       --label "coder.workspace_id=$workspace_id" \
       --label "coder.owner=$owner" \
       --label "coder.owner_id=$owner_id" \
       --label "coder.workspace_name_at_creation=$workspace_name" \
       "$volume"
     mountpoint=$(docker volume inspect "$volume" --format '{{.Mountpoint}}')
     test -z "$(find "$mountpoint" -mindepth 1 -maxdepth 1 -print -quit)"
     cp -a "$source_path/." "$mountpoint/"
   done < "$inventory_rows"
   docker compose -f /opt/remote-dev/coder/compose.yaml \
     --env-file /opt/remote-dev/coder/.env up -d coder
   ```

   Stop on a missing/corrupt inventory, a workspace-ID mismatch, any missing
   field, a pre-existing/non-empty destination, or a missing staged `_data`
   path. The next backup repeats the database-to-volume validation and fails
   instead of silently omitting a home volume.
7. From the operator machine, run `bin/remote-dev --profile <recovery-profile> doctor`,
   `status`, and `audit` against the recovery host. Start one restored workspace, confirm its
   agent is healthy, inspect `git status`, and run that project's build/test and
   stateful restore/health hooks.
8. Record snapshot ID, start/end time, achieved RPO/RTO, missing files, and all
   validation output. Destroy the disposable target only after the evidence is
   stored outside both hosts.

The recovery-host maintenance identity separately runs full `restic check` and
retention (`forget --keep-within ... --prune`) under an owner-approved policy.
Those delete-capable credentials must never be copied to the primary host.
