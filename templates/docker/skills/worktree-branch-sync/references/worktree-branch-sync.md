# Worktree Branch Sync

Use this reference when an agent must move an existing Git worktree onto a target branch or commit while preserving local work and cleaning the routine fallout from the base change.

## Purpose

This pattern exists for repeatable worktree maintenance:

- move a linked worktree onto a newer branch or specific commit without losing local work
- choose the right Git operation based on whether the worktree carries local commits or only uncommitted changes
- clean base-only noise after the move instead of leaving shared files falsely modified
- keep repo-specific post-update fixes delegated to local skills and runbooks

## Trigger

Use this pattern when the operator is effectively asking for:

- "pull this worktree onto the new `dev`/`main`"
- "rebase this worktree on top of branch X"
- "update this worktree to commit Y and keep local changes"
- "move the worktree base and clean the fallout"

Do not use it for creating a new worktree, splitting commits, or landing prepared commits onto a canonical branch.

## Required Inputs

Before changing anything, collect:

- worktree path
- target ref: branch, tag, or commit SHA
- whether the worktree has local commits to replay
- whether the worktree also has uncommitted changes
- any known repo-local follow-up such as env sync, dependency install, generated-file refresh, or port cleanup

## Safety Model

- Do not use `git reset --hard` unless the operator explicitly asks for destructive cleanup.
- Do not use stash or shelf flows by default. If local commits exist and the worktree is also dirty, stop and ask for an explicit safe path.
- Keep the worktree-focused operation separate from repo-specific follow-up flows.
- Prefer the smallest Git operation that preserves the operator's current work.

## Workflow

### 1. Read the local contract first

Read the repo-local operating contract before changing a worktree:

- `AGENTS.md`, `CLAUDE.md`, or equivalent
- local branch/worktree rules
- local validation requirements

### 2. Inspect the current topology

Use narrow inspection before changing refs:

```bash
git -C <worktree> branch --show-current
git -C <worktree> rev-parse --short HEAD
git -C <worktree> status --short --branch
git -C <worktree> log --oneline <target-ref>..HEAD
```

Use this to answer two questions:

- does `HEAD` contain local commits that are not already in `<target-ref>`?
- does the worktree also contain uncommitted changes?

#### Tooling caveat: `get_changed_files` in linked worktrees

When the checkout is a linked worktree, do not rely on VS Code `get_changed_files` as the authoritative source for worktree-local diff ownership.

In a real linked-worktree run, `get_changed_files(repositoryPath=<worktree>)` returned the parent checkout diff instead of the worktree-local diff. The most defensible explanation is that the tool resolved against the parent workspace SCM repo or main checkout instead of the worktree's `.git` indirection.

For authoritative inspection, prefer CLI Git commands rooted at the worktree:

```bash
git -C <worktree> status --short --branch
git -C <worktree> diff --name-only
git -C <worktree> diff --stat
```

Use those commands when deciding whether a change belongs to the linked worktree before rebasing, resetting, cleaning fallout, or writing reports.

### 3. Choose the base-update operation

#### Case A: no local commits, only uncommitted changes

If `git log <target-ref>..HEAD` is empty and the worktree only carries uncommitted changes, move the worktree with:

```bash
git -C <worktree> reset --mixed <target-ref>
```

This is the right move when the worktree is just a dirty working tree on an older base.

#### Case B: local commits exist and the worktree is clean

If the worktree has local commits to replay and no uncommitted changes, use:

```bash
git -C <worktree> rebase <target-ref>
```

Use this only when there are real commits to replay.

#### Case C: local commits exist and the worktree is also dirty

Do not improvise.

At that point, the correct next step depends on operator intent:

- checkpoint and stop
- commit/split the dirty work first
- or use stash/shelf only with explicit approval

This skill does not assume stash is acceptable.

### 4. Clean base-only fallout

After the base move, inspect what still differs:

```bash
git -C <worktree> status --short --branch
git -C <worktree> diff --name-only
```

Common cleanup actions:

- if a shared file now belongs entirely to the new base, accept it with:

```bash
git -C <worktree> restore <path>
```

- if an obsolete generated or ignored helper directory is no longer used on the new base, remove it only after confirming it is safe to delete
- if the new base changed repo-local bootstrap assumptions, run the repo-local follow-up flow next instead of encoding it into this global skill

### 5. Run repo-local follow-up only when needed

Examples of follow-up that belong to project-local canon, not this global skill:

- env sync for a linked worktree
- dependency install in a worktree without `node_modules`
- generated schema or lockfile refresh
- app-specific port or service cleanup

If a reusable project-local skill already exists, use it rather than expanding this global skill with project-owned logic.

### 6. Validate the result

At minimum:

```bash
git -C <worktree> status --short --branch
git -C <worktree> diff --check
```

If the operator expected the worktree to inherit a shared base change, verify that the shared files are no longer part of the local diff.

## Common Failure Modes

- using `rebase` when the worktree has no local commits and only needed a base reset
- using `reset --hard` and destroying local work
- rebasing a dirty worktree without explicit operator approval for stash or another safe parking step
- treating repo-local env/bootstrap fallout as if it were part of the generic Git operation
- leaving base-only files in the diff after the branch move
- assuming a shared-file diff is feature work when it only reflects an outdated base

## Boundaries

- This skill owns the generic Git worktree-base update procedure.
- It does not own repo-specific bootstrap, env sync, or service recovery.
- It does not replace commit-split, context-checkpoint, or project-local worktree helper skills.
