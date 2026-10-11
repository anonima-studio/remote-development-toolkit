# Docker workspace template

The **Remote Dev Docker** Coder template (`remote-dev-docker`), derived from
the Docker example shipped with Coder (the deployment pin is in
[`compose.yaml`](../../deploy/coder/compose.yaml)). It provisions one unprivileged
container per workspace with a persistent Docker volume for `/home/coder`.
`bin/remote-dev install` imports it whenever `main.tf` or this README changes.

## Parameters

| Parameter | Values | Changeable later |
| --- | --- | --- |
| Workspace role (`workspace_role`) | `development` (remote checkout, agent, IDE), `runner` (Local Agent execution) | No: create a new workspace |
| Docker capability (`docker_capability`) | `core` (CLI only), `containers` (internal daemon under Sysbox) | Through a workspace update |
| Workspace size (`workspace_size`) | see below | Through a workspace update |

The role is immutable. A persistent marker fails startup when an existing home
is presented with another role. Runner workspaces keep the shared toolchain,
SSH, port forwarding and the optional internal Docker daemon, but omit
development-only IDE apps and Codex worktree setup.

## Sizes and limits

| Size | CPU | RAM | Soft storage budget | Intended use |
| --- | ---: | ---: | ---: | --- |
| `small` | 1 | 2 GiB | 20 GiB | light maintenance and documentation |
| `standard` | 2 | 4 GiB | 30 GiB | normal application development |
| `large` | 3 | 6 GiB | 40 GiB | temporary heavy builds |
| `jetbrains` | 4 | 8 GiB | 50 GiB | WebStorm remote backend |
| `jetbrains-xl` | 4 | 12 GiB | 60 GiB | WebStorm plus a full project stack on a large host |

CPU and RAM are hard Docker limits. Each workspace also has a cgroup PID limit:
1024 for `core`, 2048 for `containers` (including the nested Docker daemon and
its descendants). `nproc` is unlimited because Linux counts that limit across
all workspaces sharing a host UID. The Coder agent starts only after the
provisioner has set and verified the PID limit through Docker's API; a failed
check blocks the agent and fails the build. Existing workspaces keep their
published limits until each one is updated.

Persistent volumes are not quota-backed. `audit` measures each workspace's home
plus inner-Docker data against the soft budget and separately warns at host
disk thresholds.

## Isolation

- Workspaces never receive the host Docker socket.
- `core` exposes the Docker CLI but no daemon.
- `containers` selects the host `sysbox-runc` runtime (install it once with
  `bin/remote-dev --profile <p> sysbox-install`) and starts an isolated daemon
  inside the workspace. Docker data persists in its own volume.
- Stopping a workspace removes its container and keeps its volumes; deleting
  it also removes the Terraform-managed volumes.

## Networks

Workspaces use `remote-dev-egress` as their default network and also attach to
the internal `remote-dev-control` network, used only by the Coder agent to
reach `http://coder-control:7080` on the host. Public DNS uses explicit
resolvers; Docker's embedded DNS resolves the private control alias. Where
egress goes depends on the profile; see
[deploy/wireguard/README.md](../../deploy/wireguard/README.md).

The inner Docker daemon and every user-defined Compose bridge use the profile's
egress MTU, so nested containers do not fall back to `1500` behind a smaller
outer interface. Override `REMOTE_DEV_DOCKER_MTU` only if the template-owned
egress MTU changes.

## Image

The workspace image is built on the server from [`Dockerfile`](Dockerfile),
assembled from digest-pinned upstream stages. The Dockerfile is the single
source for Node.js, npm, Corepack, fallback pnpm, Bun, Codex CLI and the Docker
Engine/CLI/Compose bundle. Bun comes from the Bun stage; Docker and Compose
come together from the Docker stage, so Compose has no separate install pin.
A project's
`packageManager` stays authoritative through Corepack. Version changes are
explicit image and template changes followed by clean and representative
workspace checks.

The image tag includes a hash of the Dockerfile and helper sources. `install`
refuses a source/hash mismatch, inspects the image label on every run, and
rebuilds the exact tag when the local image was pruned. Every image still
referenced by an existing workspace version is kept alive by a never-started
retention container, so a stopped workspace can start again. `doctor` checks
both.

After editing any image input, run `pnpm image:hash` at the repository root.
It prints the new hash prefix and updates the installer, Terraform tag and
current test pins. `pnpm image:hash --check` and `pnpm test` reject stale pins
without writing; recover by running `pnpm image:hash` and committing its changes.
The input list and order come directly from `deploy/install.sh`. CI builds the
image on Linux without pushing it. See the [update policy](../../SUPPORT.md#dependency-updates).

## In-workspace helpers

| Command | Purpose |
| --- | --- |
| `remote-dev-codex` | First-run entry point for the optional Codex adapter: verifies the pinned CLI, constrains the directory to `/home/coder/projects`, creates persistent `~/.codex`, runs device-code login only when needed, then starts Codex. It never downloads or upgrades Codex |
| `remote-dev-project-setup` | Runs a project's `.remote-dev/project.env` contract when the Mac-side `project-setup` command asks; workspace startup never discovers or runs projects |
| `remote-dev-worktree-vars` | Copies the variable files declared in `.remote-dev/worktree-vars.tsv` into a linked worktree of the same repository; refuses escapes, symlinks and implicit replacement; never prints values |
| `remote-dev-workspace-role` | Shows or requires the immutable role |
| `remote-dev-project-contract`, `remote-dev-mode-guard`, `remote-dev-managed-unlink` | Helpers used by mode switching and Local Agent |
| `remote-dev-start-docker` | Starts the internal daemon in `containers` workspaces |

When a `development` workspace starts, its startup script creates
`/home/coder/projects/.codex-worktrees` in the persistent home and links the
image-owned [`worktree-branch-sync`](skills/worktree-branch-sync/SKILL.md)
skill into `~/.agents/skills`. Codex Desktop owns linked-worktree creation and
cleanup; the template provides the persistent root and the base-sync method.

From the Mac:

```bash
bin/remote-dev --profile <profile> codex <workspace> --project-dir <dir>
bin/remote-dev --profile <profile> codex <workspace> --check
```

## Apps and integrations

- **WebStorm (`WS`)** on the workspace card opens `/home/coder/projects`
  through JetBrains Toolbox 2.7+ with the Coder provider, on Coder 2.24+. The
  IDE UI runs locally; backend, indexing, terminals and processes run in the
  workspace. The desktop link uses the public access URL, never the private
  control route. The WebStorm build is an exact template pin.
- **GitHub External Auth:** the template declares Coder's built-in GitHub
  provider with `optional = true`, so workspace create and update offer the
  connection without blocking projects that use SSH or another Git provider.
  Linking, App installation scope and read/write access are separate checks.

## Schedule and updates

`install` applies an eight-hour default autostop with a one-hour activity bump.
Community users can override it, so `audit` flags running workspaces without a
deadline.

Publishing a template version does not update existing workspaces. Choose
**Update** for each workspace in Coder, or use
[`workspace update`](../../docs/workspace-updates.md). The Codex helper's
preflight prints this action when the image-owned helper is missing.
