# Local agents, remote execution

This is the methodology the toolkit implements. Each principle is written as
**Problem → Rule → How it is enforced**. "Enforced" always points to code or a
test in this repository; a principle without an enforcement point would only
be advice.

Russian translation: [ru/methodology.md](ru/methodology.md).

## 1. Agents are workload, not the control plane

**Problem.** An agent that can create servers, edit firewall rules or decide
which host a command runs on turns every prompt into an infrastructure change.
Its mistakes are hard to review and hard to undo.

**Rule.** Deterministic programs and declarative state make infrastructure
decisions. Agents edit source and run project commands inside a boundary that a
program has already chosen and verified.

**How it is enforced.**

- Host baseline, deployment, template import and schedule reconciliation are
  shell programs: [`bootstrap/ubuntu.sh`](../bootstrap/ubuntu.sh),
  [`deploy/install.sh`](../deploy/install.sh),
  [`templates/docker/main.tf`](../templates/docker/main.tf).
- In `local-agent` mode the agent gets seven runner tools whose schemas contain
  no host, workspace, profile or path field. The routing comes from a trusted
  startup context: [`bin/remote-dev-local-agent-mcp.mjs`](../bin/remote-dev-local-agent-mcp.mjs),
  verified by [`tests/remote-dev-local-agent-mcp-smoke.sh`](../tests/remote-dev-local-agent-mcp-smoke.sh).
- Workspaces never receive the host Docker socket; only the Coder provisioner
  does ([`deploy/coder/compose.yaml`](../deploy/coder/compose.yaml)).

## 2. A result counts only when the user can see it

**Problem.** "The server is running on `localhost:3000`" is true inside a
remote container and useless on the operator's Mac. Infrastructure, tests and
configuration are easy to report as done while nobody can open the result.

**Rule.** Every delivered journey names four things: the entry point (command,
URL, file or UI control), the exact action, the visible result, and the
recovery path when the result does not appear. A `localhost` URL inside a
workspace is not a result.

**How it is enforced.**

- `remote-dev preview` prints `READY` only after it received an HTTP status
  through the tunnel it opened, and prints the Mac-side URL to open
  ([`bin/remote-dev`](../bin/remote-dev), `run_workspace_preview`;
  [`tests/remote-dev-preview-smoke.sh`](../tests/remote-dev-preview-smoke.sh)).
- `project-setup` waits for a real 2xx from the project's declared health URL
  and then prints the Mac entry points
  ([`tests/remote-dev-project-setup-smoke.sh`](../tests/remote-dev-project-setup-smoke.sh)).
- `codex-desktop` reports `READY` only after it reached the remote Codex binary
  through the exact SSH alias it generated
  ([`tests/remote-dev-codex-desktop-smoke.sh`](../tests/remote-dev-codex-desktop-smoke.sh)).
- Every error path in these commands prints one concrete recovery action; the
  guides in [`docs/`](.) carry a recovery table per journey.
- The rule is part of the agent contract in [`AGENTS.md`](../AGENTS.md) and the
  [`remote-development` skill](../.agents/skills/remote-development/SKILL.md).

## 3. Deployment identity fails closed

**Problem.** An SSH alias is not a deployment. Pointing the right command at the
wrong server, or a half-configured profile at a real one, silently mixes two
deployments' state.

**Rule.** Every command names its deployment. A deployment identity is a
complete, reviewed, non-secret profile file. Incomplete or contradictory
identity stops the operation before the first remote change.

**How it is enforced.**

- There is no default profile: [`deploy/host-profiles/load.sh`](../deploy/host-profiles/load.sh)
  refuses to run without `--profile` or `REMOTE_DEV_PROFILE`, rejects
  overriding a checked-in identity with `REMOTE_DEV_HOST`, and validates every
  field for the chosen topology.
- `install` performs a read-only identity check as its first remote action.
- Secret and credential operations require repeated confirmations of the
  workspace, profile and Coder origin (`--confirm`, `--confirm-profile`,
  `--confirm-origin`).
- [`tests/remote-dev-cli-smoke.sh`](../tests/remote-dev-cli-smoke.sh) runs the
  CLI against synthetic profiles only and checks the refusals.

## 4. Lifecycle is explicit; reconcile before retrying an unknown outcome

**Problem.** A lost SSH connection does not tell you whether a build, a
workspace creation or a remote command finished. Retrying blindly can run a
migration twice or create a second workspace. Hidden autostart turns a status
check into a resource decision.

**Rule.** Read-only operations never start a workspace. After an unknown
outcome, the next step is to read the recorded state and reconcile it, never to
resubmit. Every multi-step mutation writes a journal or receipt first and
resumes from it.

**How it is enforced.**

- Status, exec, preview, secrets and Codex Desktop SSH use Coder with
  autostart disabled; a stopped runner returns `WORKSPACE_STOPPED` and only the
  explicit `runner_resume` tool or `local-agent resume` starts it.
- Local Agent records an unknown execution and blocks further work until
  `--recover-unknown` is chosen explicitly
  ([`tests/remote-dev-local-agent-recovery-gate-test.sh`](../tests/remote-dev-local-agent-recovery-gate-test.sh),
  [`tests/remote-dev-local-agent-smoke.sh`](../tests/remote-dev-local-agent-smoke.sh)).
- Mode switching keeps a durable journal; `mode resume` repeats the same
  operation and an unknown workspace-creation outcome is never resubmitted
  ([`tests/remote-dev-mode-operation-test.py`](../tests/remote-dev-mode-operation-test.py),
  [`tests/remote-dev-workspace-plan-test.py`](../tests/remote-dev-workspace-plan-test.py)).
- `codex-update` and `pids-rollout` keep host receipts; an existing receipt
  blocks a second mutation
  ([`tests/remote-dev-codex-update-test.py`](../tests/remote-dev-codex-update-test.py)).

## 5. One source authority per working copy, one role per workspace

**Problem.** Two writable copies of the same source, kept in sync in both
directions, eventually disagree, and nobody can say which one a test result
came from.

**Rule.** Each working copy has exactly one authoritative location: the Git
checkout in a development workspace (`remote`), or the Mac Git root or working
set (`local-agent`). Transfer is one-way. A workspace has one role for its whole
life; changing role means a new workspace.

**How it is enforced.**

- The template writes an immutable role marker; managed entry points refuse a
  mismatch ([`templates/docker/workspace-role.sh`](../templates/docker/workspace-role.sh),
  [`tests/remote-dev-workspace-role-smoke.sh`](../tests/remote-dev-workspace-role-smoke.sh)).
- The runner receives a Git-less projection. Any remote change to managed
  source becomes `REMOTE_DRIFT`, pauses synchronization and blocks the next
  command until an explicit recovery choice. A runner is claimed by one
  binding at a time; a nested worktree inside a bound root fails with
  `NESTED_WORKTREE_UNBOUND` instead of borrowing the enclosing runner
  ([`tests/remote-dev-local-agent-smoke.sh`](../tests/remote-dev-local-agent-smoke.sh)).
- Every runner command produces a receipt naming the source manifest it ran
  against.
- Mode switching moves authority through fences and an inactive-then-active
  publication; presence at a path is never treated as authority
  ([`tests/remote-dev-mode-guard-test.py`](../tests/remote-dev-mode-guard-test.py),
  [`tests/remote-dev-mode-endpoint-test.py`](../tests/remote-dev-mode-endpoint-test.py)).

## 6. No control plane of our own: Coder plus narrow adapters

**Problem.** Remote development tools tend to grow their own scheduler,
registry, API and dashboard, each of which needs its own security model,
backup and upgrades.

**Rule.** Coder owns users, templates, workspace lifecycle and agent
connectivity. This toolkit adds thin, verifiable adapters around it and
introduces no daemon, registry or database of its own.

**How it is enforced.**

- `workspace create` is a wrapper around `coder create` that sets template
  parameters and verifies the role marker
  ([`bin/remote-dev-workspace`](../bin/remote-dev-workspace)).
- The only services the toolkit installs on the host are the Coder Compose
  project, a oneshot network unit, and the optional `cloudflared` and backup
  units ([`deploy/`](../deploy), [`backup/`](../backup)).
- `status` and `audit` are read-only views over Coder, Docker and the host; they
  never delete anything.

## 7. Secrets move only through reviewed manifests

**Problem.** Secrets leak through argument vectors (visible in `ps`), shell
history, logs, receipts, Git and "temporary" copies.

**Rule.** A secret file moves only along a route declared in a reviewed,
non-secret manifest, through stdin, into a mode-`0600` destination, and is
verified without printing its value or hash. Nothing the toolkit records
contains secret values or command arguments.

**How it is enforced.**

- [`bin/remote-dev-secrets`](../bin/remote-dev-secrets): `plan` transfers
  nothing; `apply` requires exact confirmations, refuses replacement by default,
  writes atomically and verifies value-silently
  ([`tests/remote-dev-secret-upload-test.py`](../tests/remote-dev-secret-upload-test.py),
  [`tests/remote-dev-secret-read-test.py`](../tests/remote-dev-secret-read-test.py),
  [`tests/remote-dev-secrets-binding-test.py`](../tests/remote-dev-secrets-binding-test.py)).
- Owner credentials are revealed once through a loopback-only page fed over
  stdin ([`bin/remote-dev-credential-helper.py`](../bin/remote-dev-credential-helper.py),
  [`tests/remote-dev-credentials-smoke.sh`](../tests/remote-dev-credentials-smoke.sh)).
- Runner execution receipts store the argument count and program name, never
  the arguments ([`bin/remote-dev-local-agent`](../bin/remote-dev-local-agent),
  `write_exec_receipt`).
- Secret files are excluded from source projection and from Git
  ([`.gitignore`](../.gitignore)); CI runs a secret scan on every push.

## 8. The operator's agent capabilities stay where they are configured

**Problem.** Moving an agent into a remote workspace silently drops the skills,
MCP servers, plugins, credentials and browser access configured on the Mac, or
invites copying them to a server.

**Rule.** When a project depends on local agent capabilities, keep the agent on
the Mac (`local-agent`) and move only the workloads. Integration adds one
namespaced entry to the agent configuration and touches nothing else.

**How it is enforced.**

- `local-agent install` adds one MCP entry, `remote_dev_local_agent`, and
  preserves every other configuration entry; `uninstall` removes only that
  entry and refuses while any binding is active
  ([`bin/remote-dev-local-agent-config.mjs`](../bin/remote-dev-local-agent-config.mjs),
  [`tests/remote-dev-local-agent-config-smoke.sh`](../tests/remote-dev-local-agent-config-smoke.sh)).
- The MCP server instructions route source reading, editing, Git, skills and
  configuration to local tools, and dependency, build, test, run, debug and
  Docker workloads to the runner tools.
- The toolkit does not claim to prevent workloads in the agent's local shell;
  it states the routing rule and proves remote placement through receipts. See
  [Limitations](limitations.md).
