---
name: worktree-branch-sync
description: Use when a linked Git worktree must be moved onto a target branch or commit while preserving local work and cleaning base-only fallout.
---

# Worktree branch sync

Read the complete procedure before acting:

- [`references/worktree-branch-sync.md`](references/worktree-branch-sync.md)

This skill is generic Git maintenance and is installed into every development
workspace by the workspace image. Keep project-specific behavior out of it:
repo-local environment files, dependency installs, generated files and service
restarts belong to the project's own instructions. In a workspace managed by
the Remote development toolkit, project variable files are synchronized
separately with `remote-dev-worktree-vars` after the Git operation succeeds.
