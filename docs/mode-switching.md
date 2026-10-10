# Switching modes

`remote-dev mode` moves source authority for one project between a Git
checkout in a `development` workspace (`remote`) and a Mac checkout backed by a
Git-less `runner` (`local-agent`). Both modes use the same project commands,
image toolchain and project-pinned dependencies. Workspace roles never change;
switching moves the source, not the workspace.

Every switch is a reviewed plan followed by a journaled apply. Nothing is
copied implicitly, presence at a path is never treated as authority, and the
old source is kept as an inactive checkout.

## Prerequisites

- The project declares `.remote-dev/project.env` **version 2**: the
  [project setup](project-setup.md) contract plus `quiesce_command`,
  `stop_command`, `status_command` and `resource_id` (see
  [Project lifecycle hooks](#project-lifecycle-hooks)).
- The source endpoint is running; the destination workspace exists with the
  right role, or is named for preparation (below).
- The Mac Local Agent integration is installed for `--to local-agent`.

## 1. Plan

For a project that already has a binding, start from its stable Mac path:

```sh
bin/remote-dev mode status --for-context /absolute/path/to/my-app
bin/remote-dev mode plan --for-context /absolute/path/to/my-app \
  --to local-agent --data-policy isolated
```

For first onboarding, give every endpoint explicitly:

```sh
bin/remote-dev --profile example-profile mode plan \
  --project-id my-app --from remote --to local-agent \
  --local-project /absolute/path/to/my-app \
  --development-workspace my-app --runner-workspace my-app-runner \
  --remote-project-dir my-app \
  --allow-untracked .remote-dev --data-policy isolated
```

Review the reported inventory and add an exact `--allow-untracked` scope for
every other untracked source path that must move. Unselected files stay at the
old source. Secrets are never selected this way.

The output gives a plan ID and an owner-only JSON file with both paths, Git
snapshots, selected and unselected untracked inventory, the destination
fingerprint, runtime contract, blockers and expiry. Planning starts no
workspace and writes only the local plan record. It also checks the workspace
helpers against this toolkit's image sources, an internal Docker daemon when
the project declares `docker_capability=containers`, and disk headroom.

A project can declare extra browser ports with `preview_ports=9000` (up to
eight, comma-separated); they share the explicit tunnel used for the browser
and health ports. Slow hooks can be given a reviewed
`--lifecycle-timeout-seconds` between 30 and 7200.

### Preparing a missing destination (prototype)

`--prepare-workspace <name>` includes a missing runner or a stopped inactive
destination in the plan. A new runner also needs `--workspace-size` and an
exact `--workspace-template-version <uuid>`. Apply then runs `doctor`, reserves
host admission for one preparation, submits the frozen template version and
compares neighbouring containers, volumes, networks and egress policy after
creation. Uncapped neighbouring workloads need explicit
`--host-unbounded-memory-mib` and `--host-unbounded-cpu` budgets. After an
unknown creation outcome, the request is never resubmitted: inspect with
`bin/remote-dev --profile <p> workspace inspect <name>` and reconcile. This
path is covered by tests but has not been rehearsed against a live deployment
for this release.

## 2. Adopt a checkout that already exists (optional)

Publication renames the destination aside and puts a reconstruction of the
source at its path. A matching departure receipt proves a previous destination
is untouched managed residue. Without one, `--adopt-target` proves the
destination is the same repository at the same state:

```sh
bin/remote-dev mode plan --for-context /absolute/path/to/my-app \
  --to local-agent --data-policy isolated \
  --adopt-target --local-project /absolute/existing/checkout \
  --confirm-adopt-root /absolute/existing/checkout
```

It refuses a different origin, object format or HEAD
(`ADOPT_REPOSITORY_MISMATCH`), a different index (`ADOPT_INDEX_DIVERGED`) or
diverging work (`ADOPT_WORKTREE_DIVERGED`). Untracked files, ignored files and
extra refs block until accepted with `--accept-untracked-loss`,
`--accept-ignored-loss` or `--accept-ref-loss`; accepted entries are moved to
`previous/` in the operation area, not deleted.

## 3. Apply

Pause writers (IDEs, agents, other clients) at both endpoints, then:

```sh
bin/remote-dev mode apply <plan-id> --writers-paused
```

`--writers-paused` is your assertion; managed entry points and the Local Agent
watcher obey operation fences. Source drift is re-checked before capture,
publication and activation. The operation preserves refs, HEAD,
remotes/upstream, index, unstaged changes, deletions, safe symlinks and
executable bits. It reconstructs the checkout from a Git bundle and explicit
payloads (never by copying `.git`), verifies it in a staging area, publishes it
inactive, then binds, sets up, starts and verifies the preview before
activation.

Expected result: the active mode and the final source path. For
`local-agent`, open that Mac directory and use the bound commands:

```sh
bin/remote-dev local-agent status --for-context /absolute/path/to/my-app
bin/remote-dev local-agent exec --for-context /absolute/path/to/my-app -- corepack pnpm build
```

For `remote`, open the reported development workspace and directory, for
example through [Codex on Coder](codex-on-coder.md). The result includes
`preview.reopen_command`; run it in a Mac terminal and keep it open to use the
browser URL.

Switching does not move agent conversation history or change the working
directory of existing agent sessions.

## Re-anchor an active local-agent binding

A project already in `local-agent` whose Mac root should change has no mode
transition (`mode plan --to local-agent` reports `ALREADY_ACTIVE`). Use:

```sh
bin/remote-dev mode rebind --for-context /absolute/current/root \
  --to-root /absolute/intended/root \
  --confirm-root /absolute/intended/root --writers-paused
```

Nothing is transferred; the new root keeps its own content. A different
repository is refused (`REBIND_REPOSITORY_MISMATCH`); a different state of the
same repository needs `--accept-source-change` (`REBIND_SOURCE_CHANGED`). The
projection policy is preserved. There is no rollback once the runner has moved;
recovery is to repeat the same command. Working-set bindings use
[`local-agent reanchor`](local-agent-mode.md#re-anchor-a-working-set) instead.

## Recovery

```sh
bin/remote-dev mode status --for-context /absolute/path/to/my-app
bin/remote-dev mode resume <operation-id>
bin/remote-dev mode abort <operation-id>
```

- The journal is durable and owner-only. `apply`, `resume` and `abort` share a
  project mutex. Endpoint claims never expire automatically; do not delete
  them to make a retry pass.
- After a known transport interruption, repeat the same operation. A generic
  transport or storage error does not prove which source is active: read
  status and the journal, then `resume` to reconcile.
- Before activation, `abort` stops candidate resources and restores the
  previous destination only if its identity and fingerprint still match;
  otherwise it reports `RECOVERY_REQUIRED` and leaves everything in place.
- After activation, returning to the previous mode is a new plan in the other
  direction; `abort` cannot roll back over new work.
- Keep the toolkit version that created a pending plan; recovery refuses a
  mixture of implementations.

## Retained operation areas

Each endpoint works in `.remote-dev-mode-<operation>-<key>` beside the project
root. Transport archives are discarded on activation. `previous/` (the only copy
of a replaced destination) and `aborted-candidate/` (which may hold secrets)
stay until you discard them:

```sh
bin/remote-dev mode forget <operation-id>                              # report
bin/remote-dev mode forget <operation-id> --confirm-discard-previous   # remove
```

## Project lifecycle hooks

Version 2 of `.remote-dev/project.env` adds project-owned, repeatable hooks
that must cover detached Compose containers and every other project writer:

- `quiesce_command` and `stop_command` stop writers at the source;
- `status_command` prints a version-1 JSON receipt with the canonical project
  directory, resource group, state, health URL, health resource ID, preserved
  volumes and the actual runtime inventory (processes with PID, start identity
  and process group; containers with full ID, Compose labels and volumes). It
  must be derived from the live runtime, not composed;
- `resource_id` names the project's resource group.

A timeout leaves runtime state unverified and keeps the operation's claims.

An `explicit-input` single-root binding receives a fresh tracked-file receipt
for each `exec`, `start` and `restart` (Git cached file set, index identity,
accepted projection and execution ID) for projects that check their own source
inventory at runtime.

## Secrets and data

Source transfer never includes runtime `.env` files, credentials, licenses,
database contents or runner outputs. To move secrets with the switch, add an
existing version-2 manifest to the plan with
`--secrets-manifest /absolute/path/to/project-secrets.tsv`. Planning freezes
the routing metadata without reading values. Apply stops at
`published-inactive` with `SECRETS_APPLY_REQUIRED` and prints the command to
run:

```sh
bin/remote-dev mode secrets <operation-id> \
  --confirm my-app-runner --confirm-profile example-profile \
  --confirm-origin <coder-access-url>
bin/remote-dev mode resume <operation-id>
```

Add `--replace` only after review. A workspace source uses a version-3
manifest (`source_workspace`, `source_workspace_role`, `source_root`,
`source_context`) and an extra `--confirm-source <workspace>`; payloads move
through an OS pipe between two Coder streams and never land on the Mac.

`--data-policy isolated` keeps development data separate per endpoint. Moving
project data needs an explicit project dump and restore route; volumes are
never removed by mode switching.

## Limits

Linked Git worktrees and paths the projection transport does not support are
rejected before application. Changing an existing binding's Git-metadata
policy implicitly is refused.
