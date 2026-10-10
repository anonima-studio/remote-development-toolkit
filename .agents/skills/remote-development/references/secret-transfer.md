# Project Secret Transfer

## Purpose

Move an explicitly approved set of existing local secret files into a verified
Coder project root. In `remote` mode this is the authoritative checkout after
project import. In `local-agent` mode this is the bound Git-less project root in
a dedicated runner workspace. Secret transfer is never a side effect of clone,
bundle-overlay, source projection, workspace startup, or ordinary file
synchronization.

## Availability

The CLI supports version-1 manifests for existing `development` workspaces and
version-2 manifests for both modes. Version 1 fails closed when its target has
role `runner`. Version 2 can target a runner only after its exact local-agent
binding exists and its destination is declared `remote-writable` and outside
the managed source projection.

## Manifest

Keep only routing metadata in Git. New manifests use version 2 and declare one
project identity, workspace, workspace role, absolute local root, remote root
below `/home/coder/projects/`, and exact relative source/destination pairs:

```text
manifest_version<TAB>2
project_id<TAB>project-id
workspace<TAB>project-workspace
workspace_role<TAB>development-or-runner
local_root<TAB>/absolute/local/project-root
remote_root<TAB>/home/coder/projects/project-root
file<TAB>repo/.env<TAB>repo/.env
```

`workspace_role` is `development` for `remote` mode and `runner` for
`local-agent`. Existing version-1 manifests remain valid only for their current
`remote` development workspaces and are interpreted as `development`; they
cannot target runner workspaces. Migrate a version-1 manifest before changing
its workspace, project identity, or destination inventory.

Never put values, tokens, private keys, content hashes, shell commands, or
secret-manager credentials in this manifest. Use exact file inventory rather
than globs or directory recursion.

## Controlled Flow

1. Review the manifest in Git.
2. Run `bin/remote-dev --profile <profile> secrets plan <manifest>`. Planning
   validates regular,
   non-empty local files, constrained relative paths, remote parents, symlink
   rejection, workspace identity and role, project-root identity, and whether
   destinations already exist. For `runner`, it also verifies the active local
   runner binding and rejects any destination managed by source projection. It
   transfers no content.
3. Resolve every `BLOCKED` result. Treat a group/world-readable local source as
   a warning and harden it locally when compatible with the project.
4. Run:

   ```bash
   bin/remote-dev --profile <profile> secrets apply <manifest> \
     --confirm <workspace> \
     --confirm-profile <profile> \
     --confirm-origin <coder-access-url>
   ```

   Existing destinations are refused by default. Replacement additionally
   requires `--replace` after reviewing the destination and recovery path.
5. Apply writes each file through an owner-only temporary file and atomically
   renames it into place with owner `coder` and mode `0600`.
6. Apply automatically verifies content integrity, ownership, mode, and
   non-empty state without printing values or hashes. `bin/remote-dev --profile
   <profile> secrets verify <manifest>` can repeat this check later while the
   approved local source still exists.
7. Run the project-owned config/Compose validation and health checks.

File integrity proves only that the approved bytes arrived unchanged. It does
not prove that context-dependent values such as private DNS names, database
URIs, callback URLs, or ports are valid from Coder. Treat runtime validation as
a required semantic check and use a separately reviewed workspace override or
project-owned secret source when the local value belongs to another network or
deployment context.

The operation is atomic per file, not transactional across the full manifest.
If a later entry fails, earlier verified entries remain installed; correct the
failure and rerun with explicit replacement only for already-present entries.

## Boundaries

- Coder must already be running; the command disables SSH autostart so a secret
  action cannot silently start a workspace. A stopped workspace must produce
  the exact `bin/remote-dev --profile <profile> workspace start <workspace>`
  recovery command; after starting, rerun the original secret operation.
- The manifest workspace role must match the immutable remote workspace marker.
  The project identity and remote root must match the authoritative `remote`
  adoption or active `local-agent` runner binding.
- In `local-agent`, every destination must be outside the managed source set.
  Projection reconciliation must neither delete nor overwrite approved secret
  destinations.
- Do not print or persist secret values, content hashes, or command tracing.
- Do not copy SSH private keys or provider credentials through this path unless
  a separately reviewed project manifest explicitly requires that exact file.
- Do not delete local sources automatically after transfer.
- Do not treat this file-transfer mechanism as a general secret manager,
  rotation system, or team distribution service. Projects that already use a
  vault or environment injector should bootstrap from that owner instead.
