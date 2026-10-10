# Agent instructions

Instructions for AI coding agents working in this repository. Humans can read
them as the contribution contract.

## What this repository is

A scripts-first toolkit around Coder Community: Mac-side `bin/remote-dev`,
host bootstrap and deployment under `bootstrap/` and `deploy/`, a Docker
workspace template under `templates/docker/`, and offline tests under `tests/`.
Read [README.md](README.md), [docs/methodology.md](docs/methodology.md) and
[docs/architecture.md](docs/architecture.md) before changing behavior.

Keep the existing operational paths at the repository root; they are part of
the deployment and recovery contract. Do not add `apps/*`, `packages/*`,
TypeScript, frontend tooling, a daemon, a database or an orchestration
framework without a real consumer in this repository.

## User-visible outcome

Every action presented as useful or complete must end in a result the user can
see or interact with. Infrastructure, automation, configuration and tests are
intermediate work until the user has an explicit entry point.

For every delivered user journey, document and verify:

- where the user starts (command, URL, file or UI control);
- the exact action to take;
- the visible or interactive result to expect;
- the recovery path when the result does not appear.

If the requested client cannot expose the implemented backend, say so before
claiming completion, and do not present a CLI, a mounted copy or a different
execution environment as the requested experience.

A `localhost` URL produced inside a remote workspace is not a Mac-visible
result. For an HTTP service, provide and verify either a template-owned Coder
app URL or the Mac-side `remote-dev preview` command and the local URL it
prints.

## Routing

| Task | Read first |
| --- | --- |
| Adopt a project into a workspace, choose `remote` or `local-agent`, pick capability and size | [`remote-development` skill](.agents/skills/remote-development/SKILL.md) and its [standard](.agents/skills/remote-development/references/standard.md) |
| Import a local checkout into a workspace | [project import](.agents/skills/remote-development/references/project-import.md) |
| Move ignored secret files | [secret transfer](.agents/skills/remote-development/references/secret-transfer.md) |
| Move a linked worktree onto a new base | [`worktree-branch-sync` skill](templates/docker/skills/worktree-branch-sync/SKILL.md) |
| Local Agent behavior | [docs/local-agent-mode.md](docs/local-agent-mode.md) |
| Mode switching | [docs/mode-switching.md](docs/mode-switching.md) |
| Host profiles and topologies | [deploy/host-profiles/README.md](deploy/host-profiles/README.md), [docs/advanced-topology.md](docs/advanced-topology.md) |
| Image, template or Codex version changes | [templates/docker/README.md](templates/docker/README.md), [docs/workspace-updates.md](docs/workspace-updates.md) |

## Safety rules

- Never put secret values, tokens, private keys or real host identities in
  Git, command arguments, logs, receipts or test fixtures. Tests use synthetic
  profiles under `tests/fixtures/host-profiles/` only.
- Never add a default host profile. Every command names its deployment.
- Do not weaken fail-closed checks (profile validation, role markers, drift
  detection, confirmations, receipts) to make a command or test pass.
- Do not run commands against a real server or Coder deployment unless the
  operator asked for it. The test suite never needs one.
- A workspace-local edit is not a release: changes to the image or template
  reach existing workspaces only through `install` and an explicit update.

## Checks

Requirements: Node.js 24, pnpm through Corepack (`corepack enable`), Python 3,
Bash. On macOS, the scripts must keep working with the system Bash 3.2.

```bash
pnpm install --frozen-lockfile
pnpm lint      # shell syntax, Python/Node syntax, Markdown
pnpm test      # offline suite with fake Coder, Mutagen and SSH transports
```

Focused suites: `pnpm test:mode`, `pnpm test:local-agent-policy`,
`pnpm test:local-agent-performance`, `pnpm test:codex-update`,
`pnpm test:secrets:owner`. Run the suite that covers your change before the
full one. Do not edit a Bash script while a lifecycle test is running it.

Run `pnpm lint:md:fix` for Markdown formatting. Keep documentation claims
checkable: link the file, command or test that backs them.
