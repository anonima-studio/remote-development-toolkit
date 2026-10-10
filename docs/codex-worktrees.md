# Codex worktrees and project variables

Codex-managed worktrees and project variable synchronization are two
independent features. They can be combined, but neither is a hidden
precondition of the other. Both apply to `remote` mode in a `development`
workspace.

## 1. Codex-managed worktrees

The workspace keeps the managed-worktree root inside its persistent home
volume:

```text
/home/coder/projects/.codex-worktrees
```

After installing the current template and choosing **Update** for the
workspace in Coder, run the Mac entry point:

```bash
bin/remote-dev --profile example-profile codex-desktop my-app --project-dir my-app
```

It verifies that the root and the branch-sync skill exist in the workspace and
prints a `Worktrees` line. In Codex Desktop, open **Settings → Worktrees →
Worktree root** for the remote connection and enter the printed path. Start a
session in Worktree mode.

Expected result: Codex creates a real linked Git checkout below the persistent
remote root. The checkout, its `.git` indirection, dependencies, commands and
services all stay in Linux. The toolkit never binds a Mac checkout to a remote
Docker daemon.

Recovery:

- If the output says `Worktree capability is not ready`, run
  `bin/remote-dev --profile <p> install`, choose **Update** for the workspace in
  Coder and rerun `codex-desktop`. If the skill state is `conflict`, move the
  existing non-symlink skill path aside first.
- If Codex reports a worktree setup failure, inspect the project's
  `.codex/environments` setup script and run it in a remote terminal.
- Codex owns cleanup of its managed worktrees. Commit valuable work; an
  automatically managed checkout is not a backup.

The image installs the `worktree-branch-sync` skill at:

```text
/home/coder/.agents/skills/worktree-branch-sync
```

It chooses `reset --mixed`, `rebase` or an explicit stop from the actual
worktree state. It does not create worktrees and does not guess project
environment semantics. Source: [`SKILL.md`](../templates/docker/skills/worktree-branch-sync/SKILL.md).

## 2. Variable files in every linked worktree

The source project declares exact file routes in a non-secret manifest:

```text
.remote-dev/worktree-vars.tsv
```

Each non-comment line is `source<TAB>destination`, relative to the two
worktree roots:

```text
.env<TAB>.env
apps/api/.env.local<TAB>apps/api/.env.local
```

Replace each visible `<TAB>` with one literal tab character. The manifest holds
paths only; values stay in ignored files inside the workspace. The helper
requires source and target to share one Git common directory, rejects symlinks
and path escapes, writes mode `0600` and never prints contents or hashes.

Run it against any existing linked worktree, whoever created it:

```bash
bin/remote-dev --profile example-profile worktree-vars my-app \
  --source-dir my-app \
  --worktree-dir .codex-worktrees/my-app-feature
```

If Codex chose a generated directory name, list the real paths instead of
guessing:

```bash
git -C /home/coder/projects/my-app worktree list --porcelain
```

Pass the target path relative to `/home/coder/projects` as `--worktree-dir`.

Expected result: `READY` with source, target and value-silent copied and
unchanged counts. If a target file differs, the command stops without
overwriting it; inspect the target and rerun with `--replace` only when the
source must win. The command does not start a stopped workspace.

### Codex creation hooks

Do not rely on `.codex/environments/environment.toml` to copy variable files
when Codex Desktop creates a worktree for a Remote SSH project. At the time of
writing, that creation path can create the worktree without running the
selected environment setup script (tracked upstream in
[openai/codex#23648](https://github.com/openai/codex/issues/23648)). The
supported path is the explicit `worktree-vars` command after the worktree
exists. A project may still call the in-workspace helper
`remote-dev-worktree-vars` from its own setup script as a convenience.

### After branch synchronization

When an agent applies `worktree-branch-sync`, it first finishes and validates
the Git operation. If the project declares the manifest, it then runs
`remote-dev-worktree-vars` as a separate follow-up. Moving Git history never
silently copies or overwrites project files.
