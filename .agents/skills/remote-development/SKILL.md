---
name: remote-development
description: Configure, adopt, audit, or use the Remote development toolkit on Coder. Use when asked to use a remote server for development, choose between remote and local-agent mode, keep the agent and Git local while running dependency installation, build, lint, typecheck, test, run, debug, or Docker workloads on a dedicated remote runner, select workspace capability or size, or diagnose differences between local and remote execution.
---

# Remote development

Use this skill before creating or changing a project's remote-development
setup, while implementing the required platform or project changes, and
afterwards for an evidence-backed readiness check.

## Protected decision

Keep the boundary between centrally managed workspace capabilities and
project-owned development behavior. Prevent four recurring failures:

- floating or silently conflicting tool versions;
- tools that exist only in an interactive shell and disappear for agent or IDE
  processes;
- Docker clients presented as working Docker environments without a daemon;
- local bind-mounted worktrees pointed at a remote Docker daemon.

## Sources

The toolkit repository that contains this skill owns the workflow and shared
defaults. Read [the remote-development standard](references/standard.md)
before making decisions, then the target project's instructions and runtime
files. Treat project-local pins and commands as authoritative within the
precedence rules of the standard.

Read when relevant:

- [project import](references/project-import.md) before importing a local
  checkout into `remote` mode;
- [secret transfer](references/secret-transfer.md) when ignored secret files
  must reach a workspace; it is an explicit operation after `remote` import or
  `local-agent` binding, never part of clone, bundle overlay or projection;
- [parallel agents through Git](references/parallel-agents-git.md) when a
  local line and a remote line of the same project run side by side;
- [browser automation on a runner](references/agent-browser-on-runner.md) when
  browser tests or screenshots belong next to an app on the runner.

## Workflow

1. Establish the target project root and the requested action. For a product
   made of sibling repositories, identify the aggregate anchor, the
   wrapper-owned working-set manifest, the execution member and every exact
   Git root instead of collapsing the product to the repository that contains
   the current directory. Stay read-only for audits and explanations;
   implement and verify when the operator asks to configure, adopt, migrate or
   fix the project.
2. Inspect, when present: `AGENTS.md` and project documentation; `.mise.toml`,
   `.tool-versions`, `.node-version` and `package.json`; lockfiles and
   `packageManager`; Dockerfiles, Compose files, Dev Container files and
   runtime scripts; environment examples, health checks, ports, data services
   and secrets instructions.
3. Select the development mode explicitly:
   - `remote` when the agent, the authoritative checkout and execution belong
     in one Coder development workspace;
   - `local-agent` when the agent and the authoritative local Git root or
     explicit working set stay on the Mac and execution belongs in a dedicated
     runner workspace.

   For `local-agent`, follow
   [`docs/local-agent-mode.md`](../../../docs/local-agent-mode.md) and verify
   the runner role. Never implement it as a second directory inside a
   development workspace. For a stopped bound runner, use the `runner_resume`
   tool (or `local-agent resume --for-context <path>`); it starts only the
   bound workspace, re-verifies its role, reacquires the lease and restores the
   watcher. It must not take routing from the model, recover drift or unknown
   execution implicitly, or restart managed processes. No status, exec,
   process, preview, debug or secret operation may autostart a workspace.
   To add reviewed untracked source to a single-root binding, use
   `local-agent allow-untracked --for-context <path> --path <exact-file>`; never
   stage files or edit binding state to change projection scope.
4. Classify the capability: `core` for projects that do not build or run
   containers; `containers` for Compose, image builds, Dev Containers or other
   nested containers. Choose a provisional size for the workload in that
   workspace and confirm it by measurement. Require an effective
   per-workspace cgroup PID limit before workloads run; a connected agent that
   cannot fork is a process-limit failure, not a connection outage.
5. Separate execution contexts before resolving versions: workspace tools,
   build images and runtime service images may use different versions. Within
   each context resolve each tool by: explicit project pin; approved toolkit
   default; otherwise report the version as unresolved instead of using
   `latest`.
6. Keep package-manager intent intact. pnpm and Bun in the shared image do not
   authorize changing a lockfile or the package manager.
7. Apply the mode boundary:
   - `remote`: agent, checkout, IDE backend, dependencies, builds, tests,
     processes and project containers in the same development workspace;
   - `local-agent`: agent and Git roots on the Mac; one verified Git-less
     projection, dependencies, builds, tests, processes, debugging and
     containers in the runner. A sibling-repository product uses one tracked
     manifest, one aggregate projection and one binding.

   The Mac is the browser and connection client in both modes. Never share a
   workspace between roles.
8. Make shared tools available to login shells, non-interactive Coder SSH,
   agents and IDE terminals through one stable system path; do not rely on
   shell startup files alone.
9. Keep project-specific bootstrap, start, test, health, port, secret, dump and
   restore behavior in the project, not in this skill or the base image.
10. For Codex Worktree mode in a development workspace, keep managed checkouts
    under the persistent Linux home. Treat worktree creation and variable sync
    as independent; apply only the project's exact
    `.remote-dev/worktree-vars.tsv` routes with the image-owned helper.
11. Change the platform only through the image and template release path, and
    the project only in the project. Never claim that a documented standard is
    already active in an existing workspace.
12. To import an existing checkout into `remote`, choose `clone` or
    `bundle-overlay` from [project import](references/project-import.md). Never
    treat `rsync` as ongoing synchronization or copy a multi-repository parent
    blindly. For `local-agent`, use only the one-way projection.
13. For parallel local and remote lines on different branches, follow
    [parallel agents through Git](references/parallel-agents-git.md).
14. To move source authority between modes, follow
    [`docs/mode-switching.md`](../../../docs/mode-switching.md). Presence at a
    path is never authority: a nonempty destination needs a matching departure
    receipt or `mode plan --adopt-target` with every accepted loss named. Never
    create a second copy of a project the operator already has on disk, and
    never relax the check by hand. To move only the Mac anchor of an active
    `local-agent` project use `mode rebind`; for a working set use
    `local-agent reanchor`. Both resume by repeating the same command. Report
    retained operation areas and discard them only with
    `mode forget --confirm-discard-previous`.
15. To move ignored secrets, create or review an exact non-secret manifest, run
    `secrets plan`, and require the workspace-named confirmation for
    `secrets apply`. Prefer a project-owned vault when one exists.
16. Define the user-visible entry point before calling anything useful: the
    command, URL, file or UI control; the exact action; the visible result; and
    the recovery path. A bare `localhost` URL from a workspace is not a Mac
    entry point: report the remote port and the exact `remote-dev preview`
    command, wait for its `READY`, and use the local URL it prints. For
    iterative UI work follow the standard's
    [fast feedback loop](references/standard.md#fast-feedback-loop).
17. Verify with the acceptance checks below and report deviations as explicit
    project exceptions. For `local-agent`, pass both gates in the Local Agent
    document before claiming the build, test, run and debug journey.

## Project adoption output

For each project, produce a compact contract:

```text
project root and repository, or aggregate anchor and exact member Git roots
development mode: remote | local-agent
source authority and local root when applicable
Coder workspace name
workspace role: development | runner
runner binding and remote project root when applicable
projection policy: allowed untracked, reviewed tracked exclusions,
remote-writable paths, when applicable
working-set manifest, execution member, topology digest, member paths,
when applicable
Git-metadata declaration and preflight when applicable
capability: core | containers
resource size
Node, package manager and Bun: source and resolved version per execution context
bootstrap command
development commands
Compose files and services, when applicable
preview ports and health checks
stopped-workspace resume and managed-process recovery behavior
secrets source, never secret values
persistent data and backup/restore hooks
import mode and Git authentication status
exceptions from the shared standard
verification evidence
```

Prefer existing project files as sources of truth. Add a project-local
manifest only when existing files cannot express the configuration.

## Acceptance checks

From a non-interactive Coder session:

```sh
command -v node npm npx corepack pnpm pnpx bun codex docker
node --version
pnpm --version
bun --version
docker version
docker compose version
```

Then verify:

- installed versions match project pins or approved defaults, per execution
  context;
- dependency installation uses the project's package manager and lockfile;
- the documented build or test command succeeds in proportion to the change;
- a working set is one stable aggregate revision and rejects overlapping
  roots, duplicate claims, topology drift and mixed captures;
- `core` reports that no Docker daemon is provided; `containers` reports both
  client and server and runs the required Compose flow;
- a remote HTTP service is reachable from the Mac through the verified
  `preview` route or a template-owned Coder app, not only on workspace
  `localhost`;
- every transferred secret matches its approved source, is a regular non-empty
  file owned by `coder` with mode `0600`, and was installed without printing its
  value or hash;
- stop and start preserve the checkout and expected caches and data;
- the pinned workspace image stays retained while every workspace is stopped
  (`doctor` image checks);
- mode transfers onto an existing checkout proved parity and named every
  accepted loss; the replaced checkout is retrievable from the operation area;
- a re-anchored binding kept the new root's content, moved the runner root
  hash and re-projected; old working-set member paths are retained intact;
- transport failures stay distinct from invalid role markers; never recreate
  a runner after an SSH error alone;
- a stopped runner returns `WORKSPACE_STOPPED` without autostart; explicit
  resume restores the same binding and watcher; drift, unknown execution,
  foreign ownership, topology change and stopped processes stay fail-closed;
- measured combined workload fits the selected size;
- the agent starts only after the per-workspace PID cap is in place;
- no host Docker socket and no local-worktree/remote-daemon bind mounts;
- the documented entry point gives the stated visible result from the
  operator's actual client, and its recovery instructions work.

For an imported checkout, also compare HEAD, branch, remotes, upstream, full
`git status` and stop/start persistence per repository, and report Git
transport authentication separately from history restoration.

## When a runner has no network

`runner_status` prints `network unavailable` when a DNS lookup of a public name
fails inside the runner; `network ok` proves only that lookup. Installs failing
with `EAI_AGAIN` or timeouts usually accompany it. Run
`remote-dev --profile <name> doctor` on the host: failing source policy,
fail-closed route or VPN egress checks point at the egress path. Repair the
host only for a confirmed fault and with the operator's approval, then confirm
with `runner_status` and the project's own install. Do not restart the egress
service to restore a lost rule (it drops the tunnel for every workspace);
follow [the egress notes](../../../deploy/wireguard/README.md). Do not install
dependencies on the Mac as a workaround.

## Update discipline

- One development workspace to an exact Codex version: `remote-dev codex-update
  <workspace> --version <v> --check`, then `--restart`. Requires automatic
  template updates disabled; leaves the shared template unchanged.
- Shared template release: `remote-dev install`, then `remote-dev workspace
  update <name> --expect-codex <v> --check` and `--restart`. Refuses runners.
- Change shared defaults only in the toolkit repository: pin exact versions,
  build a new image, publish a template version, check both capabilities, and
  update or recreate workspaces before calling the new defaults active.

Details: [`docs/workspace-updates.md`](../../../docs/workspace-updates.md).

## Non-goals

This skill does not replace project architecture, containerization,
deployment configuration, secret management, backup policy or code review. It
does not make every project use Docker, pnpm, Bun or one lifecycle command; it
makes their availability and ownership predictable.
