# Parallel local and remote agents through Git

Use this route when one project already has a local task line and another task
needs agents (for example Codex and Claude Code) inside a Coder `development`
workspace. The two
lines may use different modes at the same time. Keep one authoritative checkout
per line and one Git branch or worktree per concurrent writer. Do not point two
agents at the same writable worktree, runtime data, or deployment target.

## 1. Inventory and choose the boundary

1. Read the project instructions, task state owner, `git worktree list --porcelain`,
   full status including untracked/ignored paths, remotes/upstreams, and the
   current Remote Dev binding. Check `runner_status` before any workload in a
   `local-agent` line. A blocked release runner does not authorize using it for
   another worktree or repairing it from the other line.
2. Keep the existing local line on its accepted `local-agent` binding. Create a
   separate Coder workspace with immutable `development` role for the remote
   line. Check host capacity and neighboring workloads; choose `core` or
   `containers`, a measured size, and an exact template version. Verify role,
   Docker server when applicable, tool versions, and stop/start persistence.
3. A linked Mac worktree is not an input to `remote-dev mode apply`: current
   mode switching rejects linked-worktree transfer. Treat the new remote line
   as a one-time project import. Do not silently switch source authority for
   the entire project or make the release runner's Git-less projection a checkout.

## 2. Preserve and import the task branch

1. Give a detached worktree a named branch. Review and commit only that line's
   intended tracked and untracked source; preserve ignored files separately.
   Record the source HEAD, branch, `git status`, and exact file inventory.
2. Verify the intended Git remote, repository access, privacy, and payload
   before publishing. Use `GIT_TERMINAL_PROMPT=0 git ls-remote origin HEAD`,
   `git ls-remote --heads origin`, and `git push --dry-run` as transport checks;
   none proves privacy. If the remote has no refs, the first push publishes all
   reachable history and the current tree, not only the staged diff. Count and
   review that scope. Publish the exact branch only when the destination and
   full payload are authorized. A failed authorization check is a stop, not a reason
   to use a bundle, `rsync`, another repository, or copied credentials.
3. On the new workspace, authenticate its Git provider through its own Coder
   External Auth flow. First sign into Coder as the **existing workspace owner**
   in the browser, then open `/external-auth/github` and complete the separate
   GitHub authorization. Coder login and GitHub External Auth are different
   sessions. If the Coder owner has `login_type=password`, a GitHub device login
   may try to create another Coder user and end at `Signups are disabled` even
   though GitHub reports that the device is connected. Return to the Coder
   password login for the existing owner; do not change login type or create a
   new account to bypass that result. If the owner must enter a password or
   review an OAuth grant, hand the browser to them. Check
   `GIT_TERMINAL_PROMPT=0 git ls-remote` from the workspace after linking.
   If the provider link succeeds but Git returns HTTP 403 with `Write access to
   repository not granted`, inspect the built-in Coder GitHub App installation
   for the repository owner. On GitHub, select the exact organization and
   `Only select repositories` before reviewing the requested permissions.
   Installing the app grants repository administration, code, actions, checks,
   issues, pull requests and workflows write permissions within that selected
   scope. The owner must review and authorize this separate access grant.
   Recheck the workspace Git read and write dry-run after installation.
   Clone the authorized branch into an empty `/home/coder/projects/<project>`.
   Check remote HEAD, branch, clean status, file hashes, and upstream against the
   recorded Mac source. For local-only history, use the reviewed
   `project-import.md` bundle-overlay route, not ongoing file synchronization.
   Do not transfer ignored secrets with either route.

## 3. Make both agents usable

1. Check the image-owned Codex command/version in a non-interactive Coder SSH
   session. Connect Codex Desktop through `remote-dev codex-desktop` after the
   checkout exists; complete device authentication in the remote workspace if
   `codex login status` says it is required. Verify the selected project path and
   a harmless read-only task from the actual Codex client. After a scoped
   Codex upgrade, rerun `remote-dev codex-desktop`: the image-owned launcher
   must accept the installed version as well as the bare `codex --version`.
2. Install Claude Code in the persistent workspace home or through its official
   Desktop SSH connection. Pin and record the exact installed version. The
   [official native installer](https://code.claude.com/docs/en/setup#install-a-specific-version) supports a version argument; verify
   `~/.local/bin/claude --version` and `bash -lc 'command -v claude'` separately.
   Complete Claude authentication inside this workspace and verify
   `claude auth status`. Do not copy Mac tokens, API keys, or auth directories.
   If a non-interactive process lacks `~/.local/bin` on `PATH`, use the verified
   absolute binary path or a reviewed durable image change; shell startup alone
   is not proof of universal availability.
3. Give Codex and Claude separate remote branches/worktrees for simultaneous
   writes. The first remote checkout can be an integration checkout; create
   additional linked worktrees inside the persistent Linux home. Follow
   `docs/codex-worktrees.md` for exact variable routes. One integration owner
   reviews and combines changes. A second agent can review read-only without
   another worktree.

## 4. Exchange through Git and retire the Mac copy

- Remote to local: commit on the remote task branch, push it, then fetch in the
  Mac checkout and review the diff before merging or fast-forwarding the local
  target. Local to remote: commit on a separate local branch, push, fetch in the
  remote checkout, and review before integration. Never run automatic pull in a
  dirty or concurrently edited worktree. Use Git refs as the exchange record;
  runtime data and secrets need their own approved route.
- Before removing a local linked worktree, confirm the remote checkout and Git
  ref contain the exact committed source, the local worktree is clean, ignored
  and untracked data have explicit retention decisions, and no active task or
  process still uses its path. Hand the agent session over to the matching remote
  saved project and verify it opens there before cleanup. Then use ordinary
  `git worktree remove <exact-path>`; do not use `--force` to conceal drift.
  Preserve the branch until integration and recovery are complete. Removing a
  worktree does not remove the main local checkout or its `local-agent` binding.
  If Codex Desktop refuses a cross-host handoff, verify the session still belongs
  to the Mac and retain its local worktree. For example, the app may return
  `Paginated chats cannot be continued on another host yet` before artifact
  transfer. CLI access to the remote checkout does not complete the task
  handoff. A separately authorized remote continuation needs its own task and
  explicit source/context route before the Mac worktree can be retired.
- Recheck source authority, active branch, agent entry points, Git round trip,
  and the existing local line after cleanup. Update each task's own state owner;
  do not mark product work or deployment complete because migration passed.
