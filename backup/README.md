# Off-host backup

This directory provides a backup **mechanism**: a restic backup script,
systemd units and a restore runbook. No backup is enabled by default, and the
clean-host restore has not been rehearsed as part of this release. Treat it as
a prototype until you have rehearsed a restore yourself.

## Design

- The repository lives on an **independent host** (another provider, account
  or failure domain) running a restic REST server.
- The primary server gets an **append-only** credential: it can add snapshots
  but cannot prune or delete history.
- A separate recovery-host identity owns retention (`forget --prune`) and full
  `restic check`. Delete-capable credentials never reach the primary.

Each snapshot contains:

- `/opt/remote-dev`, including deployment configuration and server-side
  secrets (the restic repository is encrypted);
- an application-consistent `pg_dump` of the Coder database, never a copy of
  the live PostgreSQL volume;
- every persistent home volume labelled with a Coder workspace ID, plus an
  inventory file of those volumes checked against the database.

After each snapshot the primary reads a sample of repository data
(`RESTIC_CHECK_SUBSET`, default `1/50`).

Workspace homes are ordinary files that may change during a snapshot; Git
remains the consistency boundary for repositories. Stateful project services
must add their own dump hooks before their data can be called recoverable.

## Files

| File | Purpose |
| --- | --- |
| `backup.sh` | Runs as root: dump, volume inventory, `restic backup`, sampled `restic check` |
| `restic.env.example` | Shape of `/etc/remote-dev/restic.env` (repository URL, password file, REST credentials) |
| `remote-dev-backup.service` | Oneshot unit reading `/etc/remote-dev/restic.env` |
| `remote-dev-backup.timer` | Nightly timer with a randomized delay |
| [`restore.md`](restore.md) | Clean-host restore rehearsal |

`install` uploads these files to `/opt/remote-dev/backup` but does not enable
anything.

## Enable it

1. Prepare the independent REST server and an append-only credential for the
   primary.
2. On the primary, create `/etc/remote-dev/restic.env` from
   `restic.env.example` and a password file, both mode `0600`, owned by root.
   Keep a copy of the repository password outside both hosts.
3. Install the units under `/etc/systemd/system` and run one manual backup:
   `sudo systemctl start remote-dev-backup.service`, then check
   `journalctl -u remote-dev-backup.service`.
4. Rehearse a restore with [`restore.md`](restore.md) on a disposable host.
5. Only then enable the timer:
   `sudo systemctl enable --now remote-dev-backup.timer`.

Retention values are your decision and are applied by the recovery host. A
successful snapshot is not evidence of recovery; only a rehearsed restore is.
