# Local Agent mode

`local-agent` keeps the agent (Codex Desktop and its app server), the agent's
configuration, skills, MCP servers, plugins, credentials, browser access and
approvals, and the authoritative Git repository on the Mac. A dedicated Coder
**runner** workspace receives a verified, one-way, Git-less projection of the
local source and runs dependency installation, build, lint, typecheck, test,
run, debug and Docker workloads.

It is a first-class mode, not a degraded form of `remote`. Use it when the
project depends on agent capabilities that are configured on the Mac. The
reasoning is in [methodology.md](methodology.md#8-the-operators-agent-capabilities-stay-where-they-are-configured).

| Mode | Agent host | Source authority | Execution |
| --- | --- | --- | --- |
| `remote` | Coder development workspace | Git checkout in that workspace | Same workspace |
| `local-agent` | Mac | Local Git root or explicit working set | Dedicated runner workspace |

## Workspace boundary

A workspace has one role for its lifetime: `development` (for `remote`) or
`runner` (for `local-agent`). The role is recorded by the template and an
immutable marker at `/home/coder/.local/share/remote-dev/workspace-role`;
managed entry points refuse a mismatch. Never convert a workspace with
persistent state to another role; create a new one.

For a project `my-app`:

```text
Coder deployment
└── my-app-runner    role=runner
    └── /home/coder/projects/my-app    Git-less source projection
```

A separate `my-app` development workspace may exist for an independent
`remote` session, but it is not part of the `local-agent` deployment and shares
no filesystem, processes, Docker daemon or home with the runner.

Runner control metadata lives outside the projected tree:

```text
/home/coder/.local/share/remote-dev/
├── local-agent-workspace/      one workspace-global owner and lease
└── local-agent/<project-id>/   receipts, managed processes, recovery state
```

## Ownership

The **Mac** owns Codex Desktop and the local app server; user and project agent
configuration, skills, MCP servers, plugins, credentials, browser access and
approvals; the Git repository, branches, index, worktrees, tracked and allowed
untracked source, reading and editing.

The **runner** owns the projection; dependencies and caches; build, lint,
typecheck, test, development, debug and health processes; its internal Docker
daemon and Compose resources (with the `containers` capability); runtime
secrets installed through `secrets`; managed process handles and logs.

The **toolkit** owns the mapping, projection manifest, transfer barrier,
execution receipts, process lifecycle, role enforcement, status, recovery and
the Mac-visible preview and debug routes.

## Authority and synchronization

There is one writable source authority per managed path. Before a runner
operation, the bridge builds a deterministic projection from every Git-tracked
path plus explicitly allowed, non-ignored untracked paths. It never contains
`.git`, ignored paths, local secrets, dependencies, caches, build outputs or
project data. A tracked path may be excluded only by an explicit policy entry.

Standard runtime directories (`node_modules`, `.cache`, `dist`, `.next`,
`.nuxt`, `.output`, `coverage`, `target`) are runner-owned at every depth.
Other runner-writable paths, such as an ignored `.env`, must be declared with
`--remote-writable`. Managed source and runner-writable paths cannot overlap.

Transfer is one-way, Mac → runner, using [Mutagen](https://mutagen.io) 0.18.1
over Coder SSH. Mutagen moves bytes; correctness comes from the bridge's
content-hash manifests. A change to managed source on the runner becomes
`REMOTE_DRIFT`, pauses synchronization and blocks the next operation until an
explicit recovery choice. Writes to declared runner-owned paths are expected.

The runner is Git-less. A project declares `--git-metadata not-required`,
`explicit-input` or `required`; `required`, or an observed dependency on Git
metadata, fails with `UNSUPPORTED_GIT_METADATA` and points to `remote` mode or
a project-owned version input.

## Execution contract

Every terminating runner command performs this barrier:

1. materialize and hash the local projection;
2. synchronize it to the runner;
3. flush and pause synchronization;
4. require exact remote managed-tree parity;
5. run a structured argument vector (not a shell string) in the directory
   resolved from the trusted local context;
6. stream stdout and stderr and preserve timeout, signal and exit status;
7. check the managed tree for drift;
8. write a receipt, and resume synchronization only after a clean drift check.

Local edits made after step 1 affect the next command. The receipt names the
binding, local root, member, remote directory, profile, runner, role, topology
digest, source manifest, timestamps and result. It records the argument count
and program name, never argument values.

Remote placement is guaranteed for operations made through the runner CLI or
MCP tools. The agent's built-in local shell still exists; the integration
installs a routing instruction (local shell for source, Git and Mac tooling;
runner tools for workloads) but cannot forbid the local shell.

## Install the Mac integration

```bash
brew install mutagen-io/mutagen/mutagen
mutagen version          # must print 0.18.1
bin/remote-dev local-agent install
```

If Homebrew refuses the tap because its companion channels are untrusted,
review the tap and trust only `mutagen-io/mutagen/mutagen-beta` and
`mutagen-io/mutagen/mutagen-edge`. The bridge refuses any other Mutagen
version.

`install` requires Node.js, adds one MCP entry named `remote_dev_local_agent`
to `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`) and preserves every
other entry. Expected output starts with `READY    Local Agent integration is
installed.` Restart Codex Desktop and check that its MCP inventory lists
`remote_dev_local_agent`. Rerunning `install` is idempotent.

## Create a runner and bind a project

Create the runner through Coder (with `forward` running in the minimal
topology):

```bash
bin/remote-dev --profile example-profile workspace create my-app-runner \
  --role runner --capability containers --size standard
```

`workspace create` wraps `coder create`, waits for the agent, verifies the role
marker and prints workspace, role, capability, size and Coder URL. In the
dashboard the equivalent is **Create Workspace** → **Remote Dev Docker** →
**Workspace role: Runner**.

Bind it to one local Git root:

```bash
bin/remote-dev --profile example-profile local-agent up my-app-runner \
  --project-id my-app \
  --local-project /absolute/path/to/my-app \
  --remote-writable .env
```

Add `--allow-untracked <path>` only for reviewed, non-ignored source that must
run before it is committed. Expected result:

```text
READY          Local Agent / Remote Runner
Codex host     macOS local
Source         /absolute/path/to/my-app (authoritative)
Runner         example-profile/my-app-runner (role=runner)
Remote workdir /home/coder/projects/my-app (Git-less projection)
Execution      build/test/run/debug commands are remote through runner tools
```

`up` never creates or converts a workspace; it prints the exact recovery
command when the workspace is missing or not a runner.

### Add reviewed untracked files later

```bash
bin/remote-dev local-agent allow-untracked \
  --for-context /absolute/path/to/my-app \
  --path src/new-feature.ts --path tests/new-feature.test.ts
```

Each `--path` names one regular, non-ignored, untracked file. Directories,
hidden paths, common secret filenames, symlinks and patterns are refused;
filename checks cannot detect every secret, so review the content first. The
command verifies the runner tree, transfers the expanded projection, then
publishes the new policy. If transfer fails, the previous policy is restored
and synchronization stays paused with `SYNC_FAILED`; repair connectivity and
rerun `up` with the bound profile, workspace, project ID, root and
`--git-metadata` value, **without** any projection options, so it keeps the
accepted policy.

## Use it

Codex receives seven tools bound to the trusted startup context:
`runner_status`, `runner_resume`, `runner_exec`, `runner_start`,
`runner_logs`, `runner_stop`, `runner_restart`. None of them accepts a host,
workspace, profile, path or working directory. Each MCP call returns one
result after the command finishes, with stdout and stderr capped at 1 MiB and
explicit truncation flags.

CLI equivalents:

```bash
bin/remote-dev --profile example-profile local-agent exec my-app-runner -- pnpm test
bin/remote-dev --profile example-profile local-agent start my-app-runner -- pnpm dev
bin/remote-dev --profile example-profile local-agent logs my-app-runner <handle>
bin/remote-dev --profile example-profile local-agent stop my-app-runner <handle>
bin/remote-dev local-agent status --for-context /absolute/path/to/my-app
```

Open an HTTP service from the runner on the Mac:

```bash
bin/remote-dev --profile example-profile preview my-app-runner 3000
```

Wait for `READY`, an HTTP status and a loopback URL, and keep the command
running. For a debugger or other TCP protocol, start it on runner loopback
first, then:

```bash
bin/remote-dev --profile example-profile debug-forward my-app-runner 9229 --local-port 9230
```

It verifies the runner role and the remote listener before opening the tunnel,
prints `READY` and `127.0.0.1:9230`, and runs until `Ctrl+C`.

## Stopped runners and resume

No ordinary operation starts a workspace. A stopped runner returns
`WORKSPACE_STOPPED`. Codex then calls `runner_resume` once and checks
`runner_status` again. The CLI equivalent:

```bash
bin/remote-dev local-agent resume --for-context /absolute/path/inside/my-app
```

Resume starts only the bound workspace, re-verifies its `runner` role,
reacquires the same lease, runs the synchronization barrier and restores the
watcher. It does not restart managed processes, accept drift, change topology
or resolve an unknown execution result.

## Multi-repository working sets

A product made of sibling repositories is one binding with one aggregate
projection, one Mutagen session, one lock, one watcher and one aggregate
revision. The wrapper repository tracks a tab-separated manifest, for example
`.remote-dev/local-agent-working-set.tsv`:

```text
schema             1
anchor             ../..
execution_member   wrapper
member             wrapper  my-product-wrapper.git
member             app      my-product-app.git
member             api      my-product-api.git
allow_untracked    app      <path>
exclude_tracked    api      <path>
remote_writable    app      <path>
git_metadata       app      not-required
```

`anchor` is relative to the manifest; member paths are relative to the anchor.
Member roots must be disjoint; nesting is refused. The binding claims the
anchor and every member atomically.

```bash
bin/remote-dev --profile example-profile local-agent up my-product-runner \
  --project-id my-product \
  --working-set /absolute/path/to/my-product-wrapper.git/.remote-dev/local-agent-working-set.tsv
```

Starting Codex at the anchor routes commands to `execution_member`; starting it
inside a member routes to that member and keeps the relative directory.

The normalized topology is immutable for an active binding. A changed manifest
blocks work with `WORKING_SET_TOPOLOGY_DRIFT`. Run `local-agent down` (it
verifies the old managed tree byte-for-byte, removes only that verified
source, keeps runner-owned data and retires the binding), then `up` again.

### Re-anchor a working set

When the anchor moves, commit the new manifest and move the binding in one
journaled operation on the same runner and project ID:

```bash
bin/remote-dev --profile example-profile local-agent reanchor my-product-runner \
  --project-id my-product \
  --working-set /absolute/new/wrapper.git/.remote-dev/local-agent-working-set.tsv \
  --confirm-anchor /absolute/new/anchor \
  --for-context /absolute/path/inside/the/current/binding
```

It retires the current binding, verifies every old managed path, removes only
verified old source, moves the runner's stored root hash and binds the new
manifest. Old member paths not reused become **retained** areas that are never
synchronized or deleted by the toolkit. If the binding is already retired, pass
`--retired-binding <dir> --confirm-retired <dir>` instead of `--for-context`.
After any failure, repeat the exact command; it resumes from its journal.
Secrets are not moved: route them again with a new version-2 manifest.

## Concurrency

A runner maps to one local root or working set and one active bridge owner,
protected by a lease. A second local worktree is a separate source authority
and needs its own runner. A linked worktree or repository created inside a
bound directory is never projected by the enclosing binding; commands from it
fail with `NESTED_WORKTREE_UNBOUND` and name the root to bind. The runner
accepts one operation at a time (`OPERATION_LOCK_BUSY` otherwise).

## Lifecycle

- `local-agent down` stops bridge-owned synchronization and managed processes.
  It does not delete the workspace, caches or data, or touch the local
  checkout.
- `local-agent retire --for-context <path>` releases a binding that is already
  down so another local root can use the runner.
- `mode rebind` moves a single-root binding to another local root; see
  [mode-switching.md](mode-switching.md).
- `local-agent uninstall` removes only the MCP entry and inactive local state,
  and refuses while any session, process, unknown outcome or lease remains.
- There is no required migration to `remote`.

## Secrets

Runtime secrets use version-2 `secrets` manifests after the binding exists.
Destinations must be declared `--remote-writable` and lie outside managed
source; the projection never deletes or overwrites them. See the
[secret transfer reference](../.agents/skills/remote-development/references/secret-transfer.md).

## Status and errors

`status` reports normalized `Transfer`, `Activity`, `Watcher` and `Health`
states. Set `REMOTE_DEV_LOCAL_AGENT_STATUS_VERBOSE=true` for raw Mutagen
diagnostics.

| Result | Meaning | Recovery |
| --- | --- | --- |
| `WORKSPACE_STOPPED` | The runner is stopped | `runner_resume` or `local-agent resume --for-context <path>` |
| `CODER_CONTEXT_UNAVAILABLE` | Deployment or session not verified | Check connectivity (and `forward` in the minimal topology); the error prints the login command |
| `RUNNER_UNREACHABLE` | SSH or agent failed or timed out | Keep the binding; repair connectivity |
| `WORKSPACE_ROLE_INVALID` / `WORKSPACE_ROLE_MISMATCH` | Marker missing or not `runner` | Inspect the startup log; never convert a development workspace |
| `REMOTE_DRIFT` | Managed source changed on the runner | Restore the runner tree, then `up ... --recover-drift` |
| `SYNC_FAILED` | Transfer failed; last accepted revision kept | Repair connectivity and retry |
| `SOURCE_CHANGED` | Local source kept changing during capture | Retry when edits settle |
| `UNSUPPORTED_GIT_METADATA` | Project needs Git metadata | Use `remote` mode or an explicit version input |
| `NESTED_WORKTREE_UNBOUND` | Command from an unbound nested worktree | Bind that root with its own runner |
| Unknown execution outcome | A command lost its transport | Inspect status; reconcile and choose `--recover-unknown` explicitly |

Tunables: `REMOTE_DEV_LOCAL_AGENT_PREFLIGHT_TIMEOUT_SECONDS` (default 30) for
session and role checks; `REMOTE_DEV_LOCAL_AGENT_MUTAGEN_SSH_CONNECT_TIMEOUT_SECONDS`
(default 45, minimum 5) for the Mutagen daemon's SSH connect timeout, because
the Coder proxy can take several seconds to reach a runner;
`REMOTE_DEV_LOCAL_AGENT_TIMING_FILE` to record phase timings without argv or
source contents.

## Verification

The mode is verified by two gates, automated with fake Coder and Mutagen
transports in the test suite and repeatable against a real runner:

**Gate A, terminating commands:** idempotent install and uninstall that
preserve unrelated agent configuration; runner role enforcement; Git-metadata
declaration; dirty tracked and allowed untracked source reach a remote test
without commit or push; one stable aggregate revision for working sets;
re-anchor verification; secrets never in projection, logs, manifests or
receipts; exact stdout, stderr, timeout, signal and exit status; deterministic
drift, disconnect, unknown-execution and stale-lease recovery; stopped status
and explicit resume.

**Gate B, development and debugging:** managed start, logs, stop, restart and
orphan recovery; process records keep member and directory; continuous one-way
projection with revision receipts; drift pauses synchronization; a development
server and debug port reachable from the Mac; local edits visible in the
remote service; stop/start persistence of caches and data; resume does not
restart processes or clear recovery gates.

```bash
pnpm test                                        # includes the Local Agent suites
pnpm test:local-agent-performance
python3 tests/local-agent-latency-probe.py --context /absolute/project/root --repeat 3
```

The latency probe uses the real MCP server against your bound runner and prints
elapsed times and receipts.

## Non-goals

Copying the user's agent configuration; bidirectional synchronization, SSHFS
or two writable copies; a remote filesystem MCP or second editor; returning
generated changes automatically; depending on experimental remote-execution
features of the agent; a scheduler, CI service or dashboard; automatic mode
switching or workspace deletion.
