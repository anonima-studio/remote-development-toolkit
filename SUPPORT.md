# Support

The Remote development toolkit is published by Anonima Studio as a portfolio
project and a usable product, **as is**. There is no service-level agreement.

## Where to go

| Need | Where |
| --- | --- |
| Install and first run | [docs/quickstart.md](docs/quickstart.md), including its troubleshooting table |
| A command fails | The command's own recovery line, then the guide for that feature in [docs/](docs) |
| Known gaps | [docs/limitations.md](docs/limitations.md) |
| A bug or a documentation error | A GitHub issue |
| A security problem | Private report, see [SECURITY.md](SECURITY.md) |

## Dependency updates

[Renovate](renovate.json) proposes routine updates on the first day of each
month (UTC), one pull request per component family. It covers workspace stage
digests (including Bun and the Docker/Compose bundle), Codex CLI, pnpm, Coder
and PostgreSQL images, GitHub Actions and the npm/pnpm lockfile. Maintainers
review and merge updates after CI; no automatic merge or deployment is enabled.
The maintainer must enable the Renovate GitHub App for this repository.

Security fixes are handled immediately, outside the monthly schedule.
Renovate vulnerability alerts bypass the schedule and grouping; GitHub/npm
alerts and OSV coverage depend on advisory availability. Docker and installed
OS-package advisories still require maintainer triage: open a focused fix as
soon as an advisory is known rather than wait for scheduled Renovate discovery.
Report a new vulnerability privately via [SECURITY.md](SECURITY.md).

**Pinned** means the committed Docker digests identify immutable upstream
image content, package versions are selected explicitly, and the lockfile fixes
the npm dependency graph. The workspace source hash binds the Dockerfile and
helpers to the template tag. Pins allow deliberate, reviewable updates; they
do not promise that a version stays secure, or byte-identical builds of apt
packages fetched from a live Ubuntu repository. Versions live in
[`templates/docker/Dockerfile`](templates/docker/Dockerfile) and
[`deploy/coder/compose.yaml`](deploy/coder/compose.yaml), not prose inventories.

For a workspace-specific Codex version, choose an explicit stable version from
[OpenAI's releases](https://github.com/openai/codex/releases), then run on your Mac
(replace `<version>`, profile and workspace):

```bash
bin/remote-dev --profile example-profile codex-update my-app --version <version> --check
bin/remote-dev --profile example-profile codex-update my-app --version <version> --restart
```

The check reports the current/target version and prerequisites. `--restart`
prints a receipt, restarts the workspace and verifies the installed version. It affects only that workspace,
leaving the image default and other workspaces unchanged. Finish active sessions before running `--restart`; run it only when you can
interrupt that workspace. If a prerequisite or receipt blocks the update,
follow the recovery guide before retrying. Verify with the same `--check` command afterwards and
use the recovery steps in [workspace-updates.md](docs/workspace-updates.md).
An image update is a separate operation and must be applied explicitly to each
existing workspace.

When updating image inputs in a pull request, run `pnpm image:hash` from the
repository root and commit the rewritten installer, template and test pins.
Then run `pnpm image:hash --check`, `pnpm lint` and `pnpm test < /dev/null`.
Hosted Renovate cannot run `postUpgradeTasks` here; CI failures name that exact
repair command. CI also builds the workspace image on Linux without pushing it
and validates Coder Compose with a sample environment.

## Issues

Issues are welcome. A useful issue states the command you ran, the topology
and mode, the full output of the failing command (with secrets removed), and
the output of `bin/remote-dev --profile <name> doctor` when the server is
involved. Never paste credentials, tokens or private keys.

## Pull requests

Pull requests are reviewed at the maintainer's discretion and may be closed
without detailed review. Before opening one:

- keep the change small and focused on one problem;
- run `pnpm lint` and `pnpm test`;
- follow [AGENTS.md](AGENTS.md), which also applies to human contributors:
  user-visible outcomes, fail-closed checks, no secrets or real host identities.

By submitting a contribution you agree that it is licensed under the
[Apache License 2.0](LICENSE).
