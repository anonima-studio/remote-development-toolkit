# Codex on Coder

This is the `remote` mode journey with the optional Codex adapter: Codex
Desktop on the Mac connects over SSH to a Coder `development` workspace and
opens the authoritative checkout there. Codex, its shell commands, builds,
tests and project containers run in the workspace; the Mac is the UI and SSH
client.

Codex Desktop supports this through **Settings → Connections → SSH**. It starts
a Codex app server through the remote user's login shell, so `codex` must be on
the remote `PATH`; the workspace image provides an exact pinned version.
References: [Codex remote connections](https://developers.openai.com/codex/remote-connections),
[Coder `config-ssh`](https://coder.com/docs/reference/cli/config-ssh).

Examples use profile `example-profile`, workspace `my-app` and project folder
`/home/coder/projects/my-app`.

## Before you start

- A running `development` workspace with a healthy agent
  ([quickstart](quickstart.md#create-the-first-workspace)).
- The project checkout below `/home/coder/projects`, for example cloned from a
  remote terminal or imported with the
  [project import workflow](../.agents/skills/remote-development/references/project-import.md).
- In the minimal topology, `bin/remote-dev --profile example-profile forward`
  running in another terminal.

## 1. Configure the Mac

```bash
bin/remote-dev --profile example-profile codex-desktop my-app --project-dir my-app
```

The command:

1. keeps a separate Coder CLI configuration per profile under
   `~/Library/Application Support/remote-dev/coder/<profile>`;
2. starts the interactive `coder login` when this Mac is not signed in; Coder
   opens or prints its `/cli-auth` page; complete it in the browser;
3. generates one concrete SSH host, `<profile>.<workspace>`, without wildcard
   entries, in `~/.ssh/remote-dev-<profile>.conf`;
4. adds an `Include` for that file before the existing `Host` blocks of
   `~/.ssh/config` and keeps a backup when it changes an existing file;
5. connects through that alias and verifies the login-shell `codex`, its
   version, authentication state and the project directory.

Expected result:

```text
READY    Codex Desktop SSH connection is configured on this Mac.
Host     example-profile.my-app
Folder   /home/coder/projects/my-app
Worktrees /home/coder/projects/.codex-worktrees
Version  codex-cli <version>
```

followed by the exact Desktop steps. The operator token from the server is
never copied to the Mac.

## 2. Add the host in Codex Desktop

1. Open **Codex → Settings → Connections → SSH** and choose **Add**.
2. Select `example-profile.my-app`, give it a label and enable it.
3. Choose **Connect** and wait for a green dot and **Connected**.
4. At the right end of the connection row, click the folder-with-plus button
   and select `/home/coder/projects/my-app`.
5. Open the added remote project and start a new session.

The green **Connected** state, the remote project entry and the Codex version
are the visible acceptance result. Generated SSH configuration alone is not.

## 3. Verify the first session

Send a read-only prompt such as "Run `pwd` and `git status --short`; change
nothing." It must report `/home/coder/projects/my-app`, which proves Desktop
controls the workspace checkout, not a Mac copy.

## 4. Open a remote service on the Mac

A service started in the workspace listens on the workspace's `localhost`; a
bare `http://localhost:3000` is not reachable from the Mac. Keep the service
running and, in a Mac terminal:

```bash
bin/remote-dev --profile example-profile preview my-app 3000 --local-port 3001
```

Expected result:

```text
READY    Remote workspace preview is reachable from this Mac.
Open     http://127.0.0.1:3001
HTTP     200
Stop     Press Ctrl+C in this Terminal.
```

Open the printed URL and keep the command running. Nothing is published on a
public interface. For a project with a declared health check, use
[`project-setup`](project-setup.md), which also prints this command.

## Codex authentication in the workspace

Check without opening Codex:

```bash
bin/remote-dev --profile example-profile codex my-app --check --project-dir my-app
```

Expected keys: `codex_path`, `codex_version`, `codex_auth=ready` and
`project_dir`. If authentication is required, run the terminal entry point
once:

```bash
bin/remote-dev --profile example-profile codex my-app --project-dir my-app
```

It starts a stopped workspace and runs `codex login --device-auth`: open the
printed URL on the Mac, enter the one-time code and approve. Device-code login
must be allowed in your ChatGPT security or workspace settings. Credentials
persist in the workspace home volume, never in this repository. See
[OpenAI's headless login guidance](https://developers.openai.com/codex/auth#login-on-headless-devices).

The same command without `--check` is a terminal alternative to Desktop: it
opens the Codex TUI in the project directory.

## GitHub access from the workspace

The template exposes Coder's built-in GitHub External Auth as optional on
workspace create and update. Linking the provider, installing the Coder GitHub
App for each repository, and Git read/write access are separate checks. From
inside the workspace:

```bash
coder external-auth access-token github >/dev/null
GIT_TERMINAL_PROMPT=0 git ls-remote origin HEAD
GIT_TERMINAL_PROMPT=0 git push --dry-run origin HEAD   # when push is needed
```

Manage the App's repository access at
<https://github.com/apps/coder/installations/new>. An installation on one
account or organization cannot authorize another account's repositories. Use
HTTPS remotes with External Auth; SSH remotes need their own key.

## Recovery

| Symptom | Recovery |
| --- | --- |
| Remote session prints only a `localhost` URL | Run the `preview` command above with that port; pick another `--local-port` if busy |
| Desktop does not list the host | Check `ssh -G example-profile.my-app` and that `~/.ssh/remote-dev-example-profile.conf` contains `Host example-profile.my-app`; rerun `codex-desktop`. Wildcard entries are not discoverable |
| Coder says the Mac is signed out | Rerun `codex-desktop` and complete the browser login (with `forward` running in the minimal topology) |
| SSH reports a Coder client/server version problem | `brew upgrade coder`, then rerun `codex-desktop` |
| Workspace has no `codex` or helper | `bin/remote-dev --profile example-profile install`, choose **Update** for the workspace in Coder, rerun. Do not install a floating Codex version by hand |
| Desktop opens the wrong folder | Remove the project entry and add `/home/coder/projects/my-app` again |
| Opaque infrastructure failure | `bin/remote-dev --profile example-profile status` and `doctor`; keep the original output |

Updating Codex in the workspace: [workspace-updates.md](workspace-updates.md).
Managed worktrees and variable files: [codex-worktrees.md](codex-worktrees.md).
