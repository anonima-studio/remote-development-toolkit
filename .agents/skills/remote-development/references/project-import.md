# Project Import Workflow

## Contents

- [Purpose](#purpose)
- [Preflight](#preflight)
- [Mode Selection](#mode-selection)
- [Clone Mode](#clone-mode)
- [Bundle Overlay Mode](#bundle-overlay-mode)
- [Git Authentication](#git-authentication)
- [Acceptance](#acceptance)

## Purpose

Perform a one-time migration of an existing local project into a dedicated
Coder workspace. After acceptance, use Git for exchange and open the remote
checkout through Codex or the IDE. Do not keep Mac and Coder trees synchronized
with recurring `rsync`.

## Preflight

Before writing remotely:

1. Read the project root instructions and identify its explicit primary Git
   roots. Separate ordinary repositories from linked worktrees and auxiliary
   repositories.
2. For every primary root record:
   - absolute path and Git common directory;
   - HEAD and current branch;
   - full `git status` including untracked files;
   - remotes and branch upstream;
   - submodules and Git LFS use;
   - deleted tracked files;
   - likely secret paths among non-ignored untracked files.
3. Confirm that the target workspace exists, has the selected resource profile,
   and has an empty target path. Stop another heavyweight workspace first when
   host capacity requires it; stopping must retain its persistent home.
4. Check Git transport non-interactively. Record authentication as ready or
   blocked without copying credentials automatically.
5. Exclude dependencies, build output, caches, Docker data, ignored files, and
   historical worktrees from ordinary file transfer.

## Mode Selection

Use `clone` when all required commits are remote-reachable, the workspace can
read the remote, and there is no local-only working-tree state.

Use `bundle-overlay` when any primary repository has local-only commits, lacks
a remote, or contains tracked/untracked work that must be preserved.

Do not copy an entire dev parent merely because it contains the repositories.
Import each primary Git root independently and then restore only explicit
parent-level proxy files or symlinks.

## Clone Mode

1. Verify `git ls-remote` from the workspace with prompts disabled.
2. Clone into the final persistent workspace path.
3. Check out the exact source branch and HEAD.
4. Restore additional remotes and upstream metadata when required.
5. Continue with [Acceptance](#acceptance).

If any required commit or file is absent, stop and switch to `bundle-overlay`;
do not improvise by copying `.git` directories.

## Bundle Overlay Mode

For every primary repository:

1. Create a temporary full-history bundle with `git bundle create <file> --all`.
2. Run `git bundle verify` locally.
3. Transfer bundles with a resumable transport such as `rsync --partial`.
4. Compare local and remote SHA-256 values before restore. A completed transfer
   command without matching hashes is not evidence of integrity.
5. Initialize the empty remote repository, temporarily point unborn `HEAD` at
   an unused import branch, fetch all bundle refs, check out the original
   branch, and verify the exact full HEAD SHA.
6. Restore remote URLs and branch upstream metadata without credentials.
7. Overlay only:
   - existing tracked paths changed relative to `HEAD`, including staged and
     unstaged changes;
   - non-ignored untracked paths.
8. Apply tracked deletions from a separate explicit manifest.

Generate the existing-path overlay from `git diff HEAD` plus
`git ls-files --others --exclude-standard`. Do not overlay every tracked file:
that duplicates the bundle checkout and can introduce cross-platform CRLF or
filter noise for semantically unchanged files.

Do not use `rsync --delete` for the project root. Do not transfer `.git`,
ignored secrets, dependency stores, or build caches in the overlay.

After acceptance, remove task-created temporary bundles from both machines.

## Git Authentication

Treat Git identity, remote metadata, and transport credentials as separate
concerns:

- configure the intended non-secret `user.name`, `user.email`, and line-ending
  policy in the workspace;
- restore `origin` and other remote URLs exactly when they are still valid;
- verify HTTPS/SSH access with non-interactive `git ls-remote`;
- use Coder External Auth, a workspace-specific SSH key, or an approved token;
- prefer HTTPS remote URLs when using Coder External Auth because Coder injects
  the provider token through `GIT_ASKPASS`; SSH URLs use the separate Coder
  user key instead;
- for Coder's built-in GitHub provider, verify both the user OAuth link and the
  GitHub App installation for every repository that needs authenticated access,
  including public repositories when push is required; an authenticated
  provider link alone can still return HTTP 403;
- manage built-in Coder App installations at
  <https://github.com/apps/coder/installations/new>; the External Authentication
  menu may expose only token validation and unlink actions, and an installation
  on one GitHub user or organization cannot authorize repositories owned by a
  different account;
- add or update the workspace through the template's GitHub External Auth
  control before import; the template exposes the built-in provider but leaves
  it optional so non-GitHub projects can use another approved transport;
- verify the linked provider without logging its token:

  ```sh
  coder external-auth access-token github >/dev/null
  ```

- for an HTTPS GitHub remote, verify the exact repository from inside the
  workspace with prompts disabled, including a write dry-run when push access
  is required:

  ```sh
  GIT_TERMINAL_PROMPT=0 git ls-remote origin HEAD
  GIT_TERMINAL_PROMPT=0 git push --dry-run origin HEAD
  ```

  A public clone proves neither OAuth linkage nor write authorization;
- never copy a private SSH key from the Mac automatically;
- do not claim that a repository is connected merely because `remote -v`
  displays a URL.

Creating a remote repository, registering a key, authenticating a provider, or
pushing a branch is an external mutation and requires operator authorization.

## Acceptance

For every primary repository compare local and remote:

- full HEAD SHA;
- current branch;
- remote URLs and upstream metadata;
- full `git status --porcelain` including untracked files;
- content hashes for every overlaid path.

Then verify parent-level proxy files/symlinks and stop/start the Coder workspace
to prove checkout persistence. Re-run the repository checks after restart.

Report separately:

- `history parity`;
- `working-tree parity`;
- `Git authentication`;
- `runtime readiness`;
- excluded local roots, ignored secrets, and other deliberate non-scope.

If runtime readiness requires local ignored secret files, finish import
acceptance first, then use the separately reviewed
[`secret-transfer.md`](secret-transfer.md) workflow. Never broaden the import
overlay to include them.
