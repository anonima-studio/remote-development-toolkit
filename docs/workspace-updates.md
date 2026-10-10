# Workspace updates

Shared tools, including the Codex CLI, live in the immutable workspace image.
Updating an application on the Mac does not update that image. Publishing a new
template version does not change existing workspaces either: each workspace is
updated explicitly. This page covers the three update procedures.

All three are Mac commands run from this repository. They interrupt the
selected workspace's processes; finish active work first.

## Shared release through the active template

Use this when you changed the image or template in this repository.

1. Publish the new image and active template:

   ```sh
   bin/remote-dev --profile example-profile install
   ```

2. Check one running development workspace without changing anything:

   ```sh
   bin/remote-dev --profile example-profile workspace update my-app \
     --expect-codex <version> --check
   ```

   It needs a Mac Coder login for this profile, created by `codex-desktop` or
   `workspace create`. It prints the installed and expected Codex versions and starts nothing.
   `--expect-codex` is an assertion, not an install request.

3. Apply the active template to that workspace:

   ```sh
   bin/remote-dev --profile example-profile workspace update my-app \
     --expect-codex <version> --restart
   ```

   This runs Coder's update operation; an outdated workspace is stopped and
   started with its existing parameters. `READY` means the role and the exact
   Codex version were verified over SSH afterwards. Reconnect the workspace in
   Codex Desktop and reopen the model picker.

Alternatively, choose **Update** for the workspace in the Coder dashboard and
wait for a healthy agent.

The command refuses `runner` workspaces, whose leases and synchronization need
the Local Agent procedure below.

## Scoped Codex update for one workspace

Use this to move one development workspace to an exact Codex CLI version
without changing the shared template:

```sh
bin/remote-dev --profile example-profile codex-update my-app --version <version> --check
bin/remote-dev --profile example-profile codex-update my-app --version <version> --restart
```

`--check` reports the installed version and the planned image. `--restart`
checks the host, builds a new image from the workspace's current image
(verified by image ID), installs the exact Codex version, updates the
image-owned launcher's expected version, tests the image under both `runc` and
`sysbox-runc`, retains it, and publishes an **inactive** template version that
differs from the workspace's current version only in the image reference. It
then stops and starts only this workspace on that exact version and verifies
the image, launcher and Codex over SSH. A launcher version mismatch fails the
release even if bare `codex --version` works.

Requirements: a running development workspace with automatic template updates
disabled (`workspace create --template-version` disables them). Local source
changes in this repository are not deployed by this command.

Host receipts under `/opt/remote-dev/coder/codex-releases/` record template
IDs, image ID, build IDs and the current stage, without credentials. An
existing receipt blocks a repeated mutation; no unknown build is retried. The
previous template version stays available for an explicit rollback.

## Per-workspace process policy

The template gives each `core` workspace 1024 cgroup PIDs and each
`containers` workspace 2048, sets `RLIMIT_NPROC` to unlimited (Linux counts that
limit across all workspaces sharing a host UID), and starts the Coder agent
only after the provisioner has applied and verified the PID cap. Workspaces
created before this policy keep their old limits until updated.

Check one workspace:

```sh
bin/remote-dev --profile example-profile pids-rollout my-app
```

Apply it after quiescing the workspace. For a Local Agent runner, first stop
the binding with `bin/remote-dev local-agent down --for-context <local-root>`:

```sh
bin/remote-dev --profile example-profile pids-rollout my-app \
  --apply --maintenance-ready
```

`--maintenance-ready` records your check that the workspace can be
interrupted. The command refuses a runner with active or unresolved remote
execution records, publishes an inactive derived version only when needed,
restarts only the selected workspace with its existing parameters and image,
and verifies Docker `PidsLimit`, the effective cgroup limit, unlimited `nproc`
and agent health. Afterwards restore a runner with its original
`local-agent up` command and check `status` before running a workload.

Receipts in `/opt/remote-dev/coder/pids-rollouts/` record the workspace,
versions, build IDs and last completed stage; an existing receipt blocks a
second mutation.

## Recovery

| Situation | Recovery |
| --- | --- |
| Preflight failure | Repair transport, or start the workspace: `bin/remote-dev --profile <p> workspace start <name>` |
| Failed or uncertain build | Inspect the Coder build log and the host receipt before any retry; the commands never retry or roll back automatically |
| Version mismatch after `workspace update` | Publish an image and template that contain the expected version, then rerun |
| Missing models after reconnect | Check the remote model catalog and your account; the CLI version check does not cover model access |
| Workspace cannot start because its image is missing | Run `doctor`; restore the exact image or update the workspace to a retained template version. Do not retag an unrelated image |
| Runner workspace | Use `local-agent down`, the update, then `local-agent up` and `status` |
