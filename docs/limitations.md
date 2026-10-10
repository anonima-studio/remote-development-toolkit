# Limitations

These are properties of the product, stated so you can decide whether it fits.
They are not tied to any particular installation.

## Scope and trust

- **Single trusted operator.** One person controls the Mac, the server and the
  Coder owner account. There is no multi-tenant isolation; do not run untrusted
  code or give untrusted users workspaces.
- **The Coder provisioner has the host Docker socket**, which is
  root-equivalent. Templates must be trusted.
- **Nested Docker relies on Sysbox**, not on a virtual machine boundary.
- **The `dev` user has passwordless sudo.** The operator SSH key is a key to the
  whole server; the generated key has no passphrase until you add one.
- **First SSH contact is trust on first use** (`StrictHostKeyChecking=accept-new`).

## Platform

- The operator client is **macOS only**. Several commands use macOS paths
  (`~/Library/Application Support/remote-dev`) and refuse other systems.
- The server must be **Ubuntu 24.04 or 26.04 on x86_64**. The bootstrap refuses
  other releases; the Sysbox package is amd64-only.
- **One server per profile, no high availability.** Coder, PostgreSQL and, in
  the advanced topology, one `cloudflared` replica run on the same host.
- Only **Coder Community** features are used. Enterprise-only policies, such as
  enforced dormancy cleanup, are neither assumed nor imitated.

## Resources

- CPU, RAM and process counts are hard per-workspace limits. **Disk is not
  quota-backed**: `audit` compares each workspace's storage with a soft budget
  only when it runs.
- There is **no continuous monitoring or external alerting**. `status` and
  `audit` are on-demand views.
- Resource sizes are starting points; real project peaks must be measured.
- Community users can override the autostop TTL; `audit` reports running
  workspaces without a deadline but does not enforce one.

## Backup and recovery

- **Off-host backup is provided as a mechanism.** The script, systemd units and
  restore runbook exist, but no backup timer is enabled by default, and the
  clean-host restore has **not been rehearsed as part of this release**.
- Workspace homes are copied as files while they may change. Git remains the
  consistency boundary for repositories; stateful project services need their
  own dump and restore hooks before their data can be called recoverable.
- `audit` never deletes anything; cleanup of stopped workspaces and orphaned
  volumes is a manual Coder action.

## Modes and adapters

- **`local-agent` cannot forbid the agent's local shell.** The integration
  routes workloads to the runner through instructions and tools, and proves
  remote placement with receipts, but a command typed into the agent's local
  shell still runs on the Mac.
- The runner is **Git-less**. Projects whose build reads Git metadata must
  declare it (`--git-metadata explicit-input`) or use `remote` mode.
- Transfer to the runner is **one-way**. Files generated on the runner inside
  managed source (formatters, code generators) are treated as drift, not copied
  back.
- One runner serves **one binding and one command at a time**; parallel agents
  need separate runners or must serialize.
- For this release, `local-agent` was verified by the automated suite with fake
  Coder and Mutagen transports; its end-to-end journey against a freshly
  installed server was not rehearsed. The minimal-topology quickstart, `doctor`,
  the SSH forward, the owner login and a first `development` workspace were.
- **Mode switching rejects linked Git worktrees** and paths the projection
  transport does not support. Automated workspace preparation inside mode
  switching (`--prepare-workspace`) is a prototype.
- **Changing a server's topology** after the first install is a manual,
  unrehearsed identity change; see [advanced-topology.md](advanced-topology.md).
- The **Codex adapter is optional** and tracks a specific Codex CLI version
  baked into the workspace image. Codex Desktop features that depend on the
  vendor (remote connections, worktree creation hooks, cross-host handoff) may
  change independently of this toolkit. The Codex environment setup hook is not
  invoked for worktrees created over Remote SSH at the time of writing, so
  variable files are synchronized with an explicit command.
- **Model availability** after a Codex update is an account matter; the
  toolkit verifies only the installed CLI version.

## Supply chain

- Container images and Coder are pinned by digest; Terraform providers are
  pinned by version, but no provider lock file or checksum set is committed.
- The workspace image is built on your server from a published Dockerfile; OS
  package repositories can change between builds, so two builds with the same
  source hash are not guaranteed to be byte-identical.
- No prebuilt image is distributed.

## Support

Released as is, without an SLA. See [SUPPORT.md](../SUPPORT.md).
