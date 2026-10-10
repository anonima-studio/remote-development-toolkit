# Universal project setup

`project-setup` packages the reusable handoff from an existing Coder checkout
to a healthy, Mac-visible development service. It does not create workspaces,
import Git state, supervise processes, or infer a project's runtime.

## Project contract

Each participating repository owns `.remote-dev/project.env`. The file is
versioned with the project, contains no secrets, and uses one literal
`key=value` per line:

```text
version=1
setup_command=corepack pnpm install --frozen-lockfile
start_command=corepack pnpm dc:up
health_url=http://127.0.0.1:3000/health/ready
browser_port=3000
coder_hostname=my-workspace.coder
```

Required keys are `version`, `setup_command`, `start_command`, `health_url`, and
`browser_port`. `coder_hostname` is optional and must be a verified `.coder`
hostname. Unknown and duplicate keys are rejected. The health URL must use
loopback HTTP with an explicit port; the browser port must be from 1 through
65535.

The setup and start commands run through `bash -lc` from the canonical project
directory. They receive these non-secret variables:

- `REMOTE_DEV_PROFILE_NAME`
- `REMOTE_DEV_WORKSPACE_NAME`
- `REMOTE_DEV_PROJECT_DIR`
- `REMOTE_DEV_CODER_HOSTNAME`

Both commands must be idempotent. The start command must return after leaving
the service under project-owned durable supervision, such as a detached Compose
stack with an intentional restart policy. It must not leave the only service
process attached to the setup SSH session.

Secrets do not belong in this contract. Transfer ignored secret files through
the separately confirmed `remote-dev secrets` operation before setup.

Version 2 of the same file adds lifecycle hooks used by
[mode switching](mode-switching.md#project-lifecycle-hooks).

## User journey

Prerequisites:

1. The Coder workspace exists, is running, and reports a healthy agent.
2. The authoritative checkout already exists below `/home/coder/projects`.
3. The workspace has been updated to the current Remote Dev image after the
   latest `remote-dev install` rollout.

Use the workspace's unqualified name for the current Coder user. Owner-qualified
`owner/workspace` input is rejected because the generated Desktop and preview
entry points use one concrete unqualified SSH alias.

From the Mac, run:

```bash
bin/remote-dev --profile <profile> project-setup <workspace> \
  --project-dir <relative-project-path>
```

The relative path may identify a normal checkout or a Codex-managed linked
worktree below `.codex-worktrees`; hidden path components are accepted while
`.`/`..`, absolute paths, symlink escapes, and paths outside `~/projects` remain
rejected. Worktree variable files are configured separately through
[`worktree-vars`](codex-worktrees.md).

The command validates the workspace arguments and installed helper, validates
the project contract before executing it, runs setup and start, and waits up to
120 seconds for a real 2xx response from the declared health URL. It disables
Coder SSH autostart, so an intentionally stopped workspace remains stopped.

On success it prints:

- `READY` and the authoritative remote project path;
- the `.coder` URL when the project declares one;
- a copy-pasteable `remote-dev preview` fallback;
- the post-restart recovery action.

`project-setup` does not hide `preview` inside itself. Preview is an explicit
foreground Mac session with its own verified local URL, `Ctrl+C` lifecycle, and
dropped-tunnel recovery.

## Recovery without dead ends

| Situation | Visible result | Recovery |
| --- | --- | --- |
| Workspace is stopped or unhealthy | Setup refuses before project commands | Start it in Coder, wait for a healthy agent, rerun |
| Workspace image lacks the helper | Exact missing-helper error | Run `remote-dev install`, choose **Update** in Coder, rerun |
| Checkout or contract is absent | Exact expected remote path | Import/fix the project checkout, add its contract, rerun |
| Contract is malformed | Key, line, URL, or port validation error | Correct the project-owned contract before any command runs |
| Setup or start exits nonzero | Named phase, remote cwd, and original exit code | Fix the project command and rerun the same setup command |
| Health does not become ready | Declared URL and bounded timeout | Inspect project service logs/health, then rerun |
| `.coder` does not resolve | Project may be healthy but Mac URL is unavailable | Toggle Coder Connect off/on, or run the printed preview command |
| Preview tunnel cannot start | Transport-specific error | Reconfigure the SSH/Desktop route or choose a free local port |
| Preview tunnel later drops | Foreground command exits | Rerun the same preview command |
| Workspace restarts | Same `.coder` URL; project restart policy may restore services | Refresh; if health did not return, rerun `project-setup` |

An intentionally stopped workspace remains stopped. There is no background
registry, scheduler, retry daemon, public ingress, or generic process manager.
Coder owns workspace lifecycle, the project owns commands and health, and
Remote Dev owns validation plus the explicit setup and transport journey.
