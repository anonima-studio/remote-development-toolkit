# Remote development standard

## Contents

- [Scope](#scope)
- [Development Modes](#development-modes)
- [Workspace Boundary](#workspace-boundary)
- [Version Precedence](#version-precedence)
- [Approved Shared Defaults](#approved-shared-defaults)
- [Installation Boundary](#installation-boundary)
- [Docker Capability](#docker-capability)
- [Host Platform Coexistence](#host-platform-coexistence)
- [Git Authentication](#git-authentication)
- [Project Import](#project-import)
- [Secret Transfer](#secret-transfer)
- [Fast Feedback Loop](#fast-feedback-loop)
- [User-Visible Outcome](#user-visible-outcome)
- [Ownership](#ownership)
- [Release And Rollout](#release-and-rollout)

## Scope

Use this standard for Coder workspaces managed by this toolkit.
It defines shared defaults and project-override rules. It is a target contract,
not proof that a particular running workspace already conforms.

## Development Modes

The toolkit supports two first-class modes:

- `remote`: Codex, the authoritative Git checkout, and project execution live
  in one Coder development workspace;
- `local-agent`: Codex and the authoritative local Git root or explicit set of
  disjoint member roots live on the Mac, while a dedicated Coder runner
  workspace receives one one-way, Git-less source projection and owns project
  execution.

Neither mode is a fallback or migration state. Select one explicitly in each
project adoption contract. Follow
[`docs/local-agent-mode.md`](../../../../docs/local-agent-mode.md) for the full
`local-agent` authority, synchronization, lifecycle, and acceptance contract.

## Workspace Boundary

Use one Coder workspace for one project or deliberately coupled working set.
Assign each workspace exactly one persistent role:

- `development` for `remote` mode;
- `runner` for `local-agent` mode.

In `remote` mode, run the authoritative checkout, Codex, remote IDE backend,
dependencies, language servers, builds, tests, application processes, and
project containers inside the development workspace. Use the Mac for the IDE
UI, browser, and Coder/SSH connection.

In `local-agent` mode, keep Codex, its host configuration, the authoritative
Git root or explicit multi-repository working set, and source editing on the
Mac. Use a dedicated runner workspace for the verified Git-less projection,
dependencies, builds, tests,
application processes, debugging, and project containers. Do not place the
runner mirror beside a normal checkout in the same workspace. The development
and runner roles may use the same base image and capability profile, but they
must not share a persistent home, process namespace, internal Docker daemon, or
workspace role.

Give every workspace its own finite cgroup PID budget, including Coder,
Mutagen, language servers, browsers, build workers, and nested project
containers. Do not use a finite `RLIMIT_NPROC` as the workspace boundary when
multiple workspaces share a host UID: Linux counts all of that UID's threads.
The agent must not start until the provisioner installs and verifies the
workspace PID cap. Confirm the effective `Max processes`, Docker
`HostConfig.PidsLimit`, cgroup `pids.max/current/events`, and the host's
thread and memory headroom for each resource profile. Existing workspaces
retain their old template policy until updated; an updated workspace may still
raise the shared UID count enough to block old workspaces.

Keep workspace startup explicit. Status, execution, managed-process, preview,
debug, and secret operations must use Coder with autostart disabled. A stopped
bound runner must return `WORKSPACE_STOPPED`. Codex may then call the no-argument
context-bound `runner_resume` tool; the Mac CLI equivalent is `remote-dev
local-agent resume --for-context <path>`. Resume starts only the workspace
stored in the trusted binding, re-verifies its immutable `runner` role,
reacquires the same lease, restores the synchronization barrier and watcher,
and leaves managed processes stopped. It must not accept model-selected
routing or implicitly recover drift, topology changes, unknown execution,
foreign ownership, or process state.

Do not run an application against one checkout while Codex edits a different
unidentified copy. In `local-agent`, the projection manifest and execution
receipt identify the exact local source state used remotely. Do not point
Compose for a local bind-mounted worktree at a remote Docker daemon: bind mounts
resolve on the daemon host.

## Version Precedence

Resolve tool versions in this order:

1. An explicit project pin.
2. The exact toolkit default in
   [`templates/docker/Dockerfile`](../../../../templates/docker/Dockerfile).
3. An explicit unresolved exception.

Never fetch a floating `latest` during ordinary workspace startup. Update
defaults through a reviewed image/template release.

Apply precedence separately to each execution context. Workspace shells,
build-stage images, and runtime service images can have different explicit
pins. Preserve those boundaries; a service Dockerfile's Node version is not a
workspace Node override.

## Approved Shared Defaults

| Capability | Default | Project override |
| --- | --- | --- |
| Node.js | digest-pinned Node stage in the Dockerfile | `.mise.toml`, `.tool-versions`, `.node-version`, or an equivalent explicit pin |
| pnpm | exact fallback pin in the Dockerfile, through Corepack | exact `packageManager` in `package.json` |
| Bun | digest-pinned Bun stage in the Dockerfile | project mise/tool pin |
| Codex CLI | exact version pinned by the toolkit image | none unless the project documents a compatibility exception |
| Docker CLI | exact version pinned by the toolkit image | no ordinary project override |
| Docker Compose | exact plugin version pinned by the toolkit image | no ordinary project override |

Node 24 is the shared baseline, not a forced project migration. When a project
pin or supported-engine range conflicts with the baseline, preserve the
project contract and record the exception.

Enable Corepack centrally and install the exact fallback pnpm release. At a
project root, Corepack must honor the project's exact `packageManager` value.
Do not rewrite an existing lockfile to the fallback major.

Install Bun as an available exact-version tool. Its presence does not make Bun
the project's runtime or package manager. Use it only when project commands or
the operator require it.

## Installation Boundary

Bake shared tools into a versioned toolkit base image. Do not download the
entire shared toolchain on every workspace start. Startup may restore
lightweight links, initialize user state, or activate project pins, but it must
not silently move global versions.

Expose at least these commands through one stable path visible to interactive
and non-interactive processes:

```text
node npm npx corepack pnpm pnpx bun codex docker
```

Keep version-manager shims and caches in persistent workspace storage when
project overrides require them.

## Docker Capability

Install Docker CLI and the Compose plugin in the shared base image for both
capabilities. This guarantees command availability, not daemon availability.

### `core`

- Do not provide a Docker daemon.
- Do not mount `/var/run/docker.sock`.
- Make daemon absence explicit in adoption and verification output.

### `containers`

- Run the workspace with `sysbox-runc`.
- Start an internal Docker daemon inside the workspace.
- Keep Docker data persistent according to the workspace data policy.
- Do not mount the host Docker socket.
- Set both the daemon bridge MTU and the default MTU for user-defined bridge
  networks to the template-owned workspace egress MTU (the profile's
  `REMOTE_DEV_PROFILE_EGRESS_MTU`, `1420` with WireGuard egress). Compose
  networks must not silently fall back to `1500` behind a smaller outer
  interface.
- Verify both the Docker client and server before running Compose.
- Verify the effective MTU inside a representative Compose container and smoke
  a response larger than one network segment; ping/count-only checks do not
  detect a PMTU black hole.

Use `containers` for projects that run Compose, build images, or launch Dev
Containers. Use `core` otherwise. Do not select `containers` only because the
Docker CLI happens to be installed.

## Host Platform Coexistence

Coder may share a physical server and host Docker daemon with another
self-hosted platform (for example a deployment PaaS) when the combined workload
has measured capacity and both platforms remain separately owned. Colocation is
an operational optimization, not permission to merge their lifecycle or
recovery boundaries.

For a colocated platform and Coder:

- keep Coder outside the other platform's managed-resource lifecycle, with
  separate Compose projects, PostgreSQL databases, volumes, networks, backups,
  upgrades, and restore procedures;
- assign public ports `80` and `443` to exactly one reverse proxy. The other
  platform may own those ports while Coder stays bound to its loopback origin
  and is reached through the SSH forward or its Cloudflare Tunnel;
- keep the two platforms' domains and wildcard DNS scopes distinct. Do not
  route a broad wildcard to one platform when it can capture names owned by
  the other;
- treat every control plane with access to the host Docker socket as
  root-equivalent. Never extend that access into project workspaces; the
  `containers` capability must continue using its internal Docker daemon;
- keep Docker network subnets disjoint from `remote-dev-egress` and
  `remote-dev-control`, and preserve the Coder agent's private
  `coder-control:7080` route;
- configure image, build-cache, container, and volume cleanup so it cannot
  prune Coder workspace data, retained workspace images, or other resources
  outside that platform's ownership;
- reserve explicit CPU, memory, PID, disk, and I/O headroom for the Coder
  control plane, PostgreSQL, active workspaces, the other platform, its
  application workloads, and concurrent image builds. Product minimums are
  installation requirements, not evidence of safe combined capacity;
- prefer separate servers when either platform carries critical production
  traffic, independent failure recovery is required, or measured peak load
  leaves insufficient headroom.

Before and after introducing or changing a colocated platform, record and
compare:

- listeners and firewall exposure, including ports `80`, `443`, and the Coder
  loopback origin;
- Docker networks, subnets, volumes, runtime configuration, and ownership
  labels;
- `DOCKER-USER`, policy routing, MTU, fail-closed behavior (with WireGuard
  egress), and `sysbox-runc` availability;
- Coder control-plane health, workspace start/stop persistence, agent access to
  `coder-control:7080`, and a representative `containers` workspace check;
- the other platform's proxy, application deployment, cleanup behavior,
  backup, and restore;
- combined idle and peak CPU, memory, disk, I/O, and build load.

Do not describe colocation as ready merely because both dashboards start. A
host change is accepted only after these isolation, recovery, networking, and
capacity checks pass.

## Git Authentication

Treat Git authentication as three separate gates:

1. the Coder deployment exposes the intended External Auth provider;
2. the workspace owner has linked that provider and, for GitHub App-backed
   access, installed or configured the App for every repository that needs
   authenticated access;
3. the exact repository transport succeeds non-interactively for the required
   read and write operations.

The shared Docker template must declare the built-in GitHub provider as an
optional `coder_external_auth` dependency. This makes the OAuth connection
available during workspace create/update without forcing GitHub on projects
that intentionally use SSH or another provider. Do not disable Coder's built-in
provider. `doctor` must verify the effective runtime instead of inferring it
from a configuration default: provider ID `github` must be available through
the authenticated Coder API.

For a GitHub repository using External Auth, use an HTTPS remote. Link the user
account through the workspace create/update flow or Coder External Auth
settings, then configure the GitHub App installation for every repository that
needs authenticated access, including a public repository when push is
required. Never treat a successful OAuth link as proof of repository
authorization.

For Coder's built-in provider, manage GitHub App installations at
<https://github.com/apps/coder/installations/new>. The Coder External
Authentication menu may expose only token validation and unlink actions; it is
not the installation-management surface. GitHub installations are
account-scoped, so an installation on one user or organization cannot authorize
a repository owned by another account.

From inside the target workspace, verify the provider without printing its
token, then verify the exact remote with terminal prompts disabled:

```sh
coder external-auth access-token github >/dev/null
GIT_TERMINAL_PROMPT=0 git ls-remote origin HEAD
GIT_TERMINAL_PROMPT=0 git push --dry-run origin HEAD
```

The dry-run is required when the project needs push access. A public clone or
successful fetch proves only read access. SSH remains a separate approved path
and requires its own key registration and non-interactive read/write checks.

## Project Import

This import workflow applies to `remote` mode. The Coder checkout becomes
authoritative after cutover. Do not configure continuous bidirectional file
synchronization with the Mac.

`local-agent` does not import or clone its authoritative checkout into the
runner. It projects the approved local source set one-way according to the
Local Agent mode contract. Never use a runner projection as a Git repository or
as input to the `remote` import workflow.

For a multi-repository Local Agent working set, require one tracked
wrapper-owned manifest naming the aggregate anchor, execution member, exact
disjoint Git roots, and member-scoped projection policy. Preserve the
anchor-relative topology in one stable aggregate projection and one binding;
do not run independent member sync sessions or infer members by scanning a
parent directory.

Treat the normalized working-set topology as immutable for an active binding.
When it changes, exact `down` must pause synchronization and compare the remote
managed tree with the stored old managed manifest before retiring anything. A
mismatch is `REMOTE_DRIFT` and blocks retirement. A clean retirement may remove
only the verified old managed source; it must preserve remote-writable secrets,
dependencies, caches, build outputs, and data before the changed topology is
explicitly rebound.

When the aggregate anchor itself moves, the runner-stored root hash moves with
it through one journaled re-anchor on the same workspace and project ID. The
old managed tree must verify byte-for-byte or be already absent before only that
source is removed. Old member paths that no new member reuses stay in place as
retained runner-owned areas, and a partial overlap with a new member path is
refused.

Treat standard dependency, cache, and output directory names as remote-owned at
every depth, including nested package paths created after adoption. Configure
the pinned transfer session deterministically without inheriting operator-global
ignore configuration. An idle watcher may use a local source token and a
periodic remote audit, but it must run a full barrier after local source change
and yield operation-lock priority to a verified foreground waiter.

Use `clone` only when the required commits are reachable from a configured
remote, the workspace has verified read access, and no local-only working-tree
state must be preserved. Otherwise use `bundle-overlay` as defined in
[`project-import.md`](project-import.md).

Treat a multi-repository working set as explicit Git roots. Exclude historical
linked worktrees, dependency directories, ignored caches, and auxiliary Git
repositories unless project documentation includes them in the primary set.
Never infer the import scope from every `.git` marker below a parent directory.

Restore Git URLs and branch tracking as non-secret metadata. Verify credentials
separately; do not copy a private SSH key from the Mac automatically. Transfer
ignored secrets only through a separately approved secret path.

## Secret Transfer

Secret transfer is a distinct operation after a `remote` checkout import or a
`local-agent` runner binding. Keep an exact manifest of non-secret
source/destination routes in a reviewed location; keep all values outside
Git. Each route binds the project identity, workspace, workspace role, source,
and destination. The operator flow is `plan`, explicit workspace-named
`apply`, then value-silent integrity and permission verification.

Constrain remote destinations below the verified project root under
`/home/coder/projects/`: the authoritative checkout in `remote`, or the bound
runner project root in `local-agent`. Reject role or identity mismatches,
symlinks, and recursive/glob inventories. A `local-agent` secret destination
must be excluded from the managed source projection, and projection
reconciliation must neither delete nor overwrite it. Write through mode-`0600`
temporary files and atomically replace per file. Refuse existing destinations
unless the operator separately requests replacement. Do not autostart a
stopped workspace during this operation.

When a workspace is stopped, return the exact profile-aware `remote-dev
workspace start <workspace>` recovery command. Starting must preserve and
re-verify the immutable workspace role.

A working-set runner keeps the version-2 manifest contract. Its `local_root` is
the aggregate anchor, paths are aggregate-relative, and the route binds the
active normalized topology digest. Destinations must be covered by the exact
member-prefixed remote-writable policy.

This path transfers already-approved files; it is not a secret manager. Prefer
a project-owned vault or environment injector when one exists, and retain
project ownership of acquisition and rotation.

## Fast Feedback Loop

For iterative site and interface work, establish a visible preview before spending
multiple cycles on production builds or automated screenshots. A preview is a
review checkpoint with named defects; final acceptance still follows the
project's checks and browser requirements.

1. In `local-agent` mode, check `runner_status` before the first workload and
   use only the bound runner. Keep its dependency cache, one managed development
   server, and the verified Mac preview route alive across related edits. If the
   workspace is stopped, use context-bound resume; do not use ordinary SSH
   for development workloads or silently switch execution to the Mac. The
   browser reference permits a diagnostic capture of an already running app
   during a projection failure, but it cannot prove current source parity.
2. Use the project's development server and hot reload for visual iteration.
   Inspect the current page after a coherent edit. Restart the server or run a
   production build only when the changed behavior requires it, hot reload has
   failed, or a project acceptance gate calls for it. Reuse checks that already
   ran against the same source state.
3. Select the smallest check that exercises the changed risk: focused lint,
   typecheck, unit test, interaction, or browser state. Batch related fixes
   before an expensive full build, screenshot sweep, or Lighthouse run. Inspect
   existing captures before requesting another sweep. Keep final production
   and accessibility checks when the delivery boundary requires them.
4. Preserve a valid frozen dependency installation. Reinstall when the
   lockfile, toolchain, or install state actually changes, not on each source
   edit. Use a package or app-scoped command for an app-scoped change unless
   shared dependencies or the acceptance boundary require a wider run.
5. For WebGL or other GPU-heavy pages, inspect interaction and first-frame
   time on a representative real GPU. A software-rendered headless browser is
   useful for repeatable regression captures but does not establish real-device
   performance. Keep meaningful content readable while the scene loads;
   complete any required static fallback before final acceptance.
6. When an iteration is slow, time source synchronization, command execution,
   server startup, first usable frame, and capture/analysis separately. State
   which stage was measured and which is still unknown. Diagnose that stage
   before repeating the same expensive command; do not label the entire cycle
   a slow build without command-level timing.

Do not weaken source receipts, runner-role checks, drift protection, or the
project's final quality gates to make the inner loop faster.

## User-Visible Outcome

Do not accept a platform action as useful or complete only because its backend,
deployment, or tests pass. It must end at a surface the user can see or interact
with.

Every accepted user journey must identify and verify:

1. the entry point: command, URL, file, or UI control;
2. the exact user action;
3. the expected visible or interactive result;
4. the recovery path when that result does not appear.

For a service started inside a remote workspace, `localhost` identifies the
workspace, not the operator's Mac. Never present a bare workspace-local URL as
the desktop result. Use a template-owned Coder app when the project defines
one, or use `remote-dev preview <workspace> <remote-port>` to establish and
verify a loopback-only Mac route. The project owns the preview port and health
behavior; the toolkit owns the shared transport command and its recovery path.

Treat backend-only work as an intermediate checkpoint. If the requested client
cannot use the implemented backend, report that incompatibility before
rollout, and do not present a CLI, file mount, synchronized copy, or different
execution environment as the requested experience. A supported substitute is
acceptable only when it is explicitly distinguished and has its own complete
user journey.

## Ownership

The toolkit owns:

- the approved defaults and base-image contents;
- stable PATH behavior;
- Coder templates and `core`/`containers` capabilities;
- the `development`/`runner` workspace-role contract and role verification;
- resource profiles, workspace persistence, and template rollout;
- smoke checks proving the platform capabilities;
- a persistent Linux root for Codex-managed linked worktrees and installation
  of the pinned generic worktree branch-sync skill;
- value-silent transport for project-declared variable-file routes between
  linked worktrees of one Git repository.

Each project owns:

- its selected `remote` or `local-agent` development mode;
- for `local-agent`, allowed untracked source paths, reviewed tracked
  exclusions, and remote-writable dependency/cache/output/log/data paths; the
  managed-source and remote-writable sets must not overlap;
- for a multi-repository `local-agent` working set, the tracked topology
  manifest, execution member, exact member roots, and immutable normalized
  topology digest;
- for `local-agent`, the Git-metadata compatibility declaration and any
  project-owned compatibility preflight or explicit version input;
- repository and branch/worktree policy;
- the non-secret `.remote-dev/worktree-vars.tsv` route manifest and the choice
  to call it manually, from Codex environment setup, or after branch sync;
- the explicit primary Git-root inventory and import exceptions;
- runtime and package-manager pins that override defaults;
- dependency, start, build, test, lint, and health commands;
- Compose topology, service selection, preview ports, and environment examples;
- secret acquisition without storing secret values in skill artifacts;
- application-consistent dump, restore, and migration commands for mutable
  services.

## Release And Rollout

For an explicit Codex-only update of one development workspace,
`remote-dev codex-update` owns a scoped release: use an exact existing image ID
as the base, install the requested exact Codex package into a new image, smoke
both runtimes, retain the image, publish an inactive version of the workspace's
existing template with only its image reference changed, and start that
workspace with the exact new template UUID. Require automatic updates to be
disabled so a later start cannot silently replace this release. This does not
change approved defaults or the active shared template. Record the previous
version and each build ID in the host release receipt before reporting success.

For a shared-tool change:

1. Change the pinned version in the toolkit source.
2. Build and identify an immutable base image.
3. update both Coder capability templates;
4. run clean-workspace smoke checks for `core` and `containers`;
5. update an existing representative workspace and verify persistence;
6. record the version and evidence;
7. only then make the new version the default.

For a project adoption:

1. inspect project-owned pins and commands;
2. select `remote` or `local-agent` and verify the corresponding workspace
   role;
3. select capability and a provisional resource profile for the workload that
   actually runs in that workspace;
4. apply the smallest missing project/platform changes;
5. verify from non-interactive Coder execution and measure the combined
   workload against the selected profile;
6. for `local-agent`, also pass both acceptance gates in the Local Agent mode
   contract before claiming build/test/run/debug completion;
7. prove stopped-workspace recovery through the context-bound resume path and
   re-run status plus one representative terminating command;
8. record project exceptions without weakening the global standard.
