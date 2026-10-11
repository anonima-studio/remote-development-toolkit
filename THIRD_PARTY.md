# Third-party components

The toolkit's own code and documentation are licensed under the
[Apache License 2.0](LICENSE). This page lists the third-party software the
toolkit installs, pins, builds into the workspace image, or uses in its
tooling, together with the license each project states.

**How components reach you.** The repository publishes scripts, a Compose
file, a Terraform template and a [Dockerfile](templates/docker/Dockerfile). It
does not redistribute a prebuilt image or binaries. When you run `install`,
your server pulls upstream images and packages from their publishers and
builds the workspace image locally. Each component is then used under its own
license, which you accept with its publisher.

Licenses were checked against each project's primary source (its repository
license file or official license page) when this list was written. Versions
change; check the component itself for the version you run. This page is
informational and is **not legal advice**.

## Server: deployment

| Component | Version or source | How it is used | License |
| --- | --- | --- | --- |
| [Coder](https://github.com/coder/coder) | [`compose.yaml`](deploy/coder/compose.yaml), image pinned by digest | Workspace control plane, pulled from `ghcr.io/coder/coder` | AGPL-3.0 for the open-source code; code under its `enterprise/` directory is under Coder's separate enterprise license. Only Community features are used |
| Terraform (inside the Coder image) | Version chosen by Coder | Coder's built-in provisioner | HashiCorp/IBM Business Source License 1.1 for Terraform 1.6.0 and later; earlier versions MPL-2.0 |
| [Terraform provider `coder/coder`](https://github.com/coder/terraform-provider-coder) | [`main.tf`](templates/docker/main.tf) | Workspace template | MPL-2.0 |
| [Terraform provider `kreuzwerker/docker`](https://github.com/kreuzwerker/terraform-provider-docker) | [`main.tf`](templates/docker/main.tf) | Workspace template | MPL-2.0 |
| [PostgreSQL](https://www.postgresql.org/about/licence/) | [`compose.yaml`](deploy/coder/compose.yaml), official image pinned by digest | Coder database | PostgreSQL License; image packaging ([docker-library/postgres](https://github.com/docker-library/postgres)) MIT |
| [Docker Engine, CLI, containerd, runc, Buildx, Compose](https://github.com/moby/moby) | Docker's Ubuntu repository (`docker-ce` and plugins) | Host container runtime | Apache-2.0 |
| [Sysbox CE](https://github.com/nestybox/sysbox) | [`install.sh`](deploy/sysbox/install.sh), checksum-pinned `.deb` | Runtime for `containers` workspaces (`sysbox-install`) | Apache-2.0 |
| [cloudflared](https://github.com/cloudflare/cloudflared) | Cloudflare's package repository | Optional `cloudflare-tunnel` ingress | Apache-2.0 |
| [wireguard-tools](https://git.zx2c4.com/wireguard-tools/) | Ubuntu package | Optional `wireguard` egress | GPL-2.0 |
| [restic](https://github.com/restic/restic) | Ubuntu package | Backup | BSD-2-Clause |
| Ubuntu packages (`ufw`, `openssh-server`, `unattended-upgrades`, `curl`, `git`, `jq`, `rsync` and others) | Ubuntu archive | Host baseline | Various free licenses, per package (see `/usr/share/doc/*/copyright` on the server) |

## Workspace image (built from the published Dockerfile)

| Component | Version | License |
| --- | --- | --- |
| [`codercom/enterprise-base:ubuntu`](https://github.com/coder/images) | Pinned by digest | Image definitions MIT; the Ubuntu packages inside keep their own licenses |
| [Node.js](https://github.com/nodejs/node) (from the official `node` image) | [`Dockerfile`](templates/docker/Dockerfile), digest-pinned Node stage | Node.js license (MIT) plus the licenses of its bundled dependencies; image packaging ([nodejs/docker-node](https://github.com/nodejs/docker-node)) MIT |
| [npm](https://github.com/npm/cli) | Bundled with Node.js | Artistic-2.0 |
| [Corepack](https://github.com/nodejs/corepack) | Bundled with Node.js | MIT |
| [pnpm](https://github.com/pnpm/pnpm) | [`Dockerfile`](templates/docker/Dockerfile), fallback pin | MIT |
| [Bun](https://github.com/oven-sh/bun) (from the official `oven/bun` image) | [`Dockerfile`](templates/docker/Dockerfile), digest-pinned Bun stage | MIT; statically links JavaScriptCore/WebKit under LGPL-2 and other libraries listed in Bun's license file |
| [Codex CLI](https://github.com/openai/codex) (`@openai/codex`) | [`Dockerfile`](templates/docker/Dockerfile), or the workspace pin selected with `codex-update` | Apache-2.0. Using Codex also requires an OpenAI account under OpenAI's terms |
| Docker Engine, CLI, containerd, runc, Compose plugin (from the official [`docker`](https://github.com/docker-library/docker) image) | [`Dockerfile`](templates/docker/Dockerfile), digest-pinned Docker stage (includes Compose) | Apache-2.0 |
| Ubuntu packages (`git`, `curl`, `iproute2`, `iptables`, `python3-minimal`, `xz-utils`) | Ubuntu archive | Various free licenses, per package |

## Mac client

The toolkit does not install these; the [quickstart](docs/quickstart.md) asks
you to install them.

| Component | Version | Used for | License |
| --- | --- | --- | --- |
| [Coder CLI](https://github.com/coder/coder) | Matching the server | Workspaces, SSH, Codex Desktop | AGPL-3.0 (enterprise code under Coder's enterprise license) |
| [Mutagen](https://github.com/mutagen-io/mutagen) | `0.18.1` exactly | `local-agent` synchronization | MIT, except code under its `sspl/` directory, which is SSPL; official release builds from v0.17 onward include the SSPL code |
| [Node.js](https://github.com/nodejs/node) | 24 | Local Agent MCP server, repository tooling | Node.js license (MIT) |
| Codex Desktop / Codex CLI | Your choice | Optional adapter | OpenAI's terms; Codex CLI is Apache-2.0 |
| JetBrains Toolbox and WebStorm | Your choice | Optional IDE app on the workspace card | Proprietary, under your JetBrains license |

## Repository tooling (from `pnpm-lock.yaml`)

The only development dependency is
[`markdownlint-cli2`](https://github.com/DavidAnson/markdownlint-cli2)
(MIT); its version is defined in
[`configs/markdownlint/package.json`](configs/markdownlint/package.json) and
[`pnpm-lock.yaml`](pnpm-lock.yaml). The license inventory at publication contained
86 packages; review it when the lockfile changes:

| License | Packages |
| --- | ---: |
| MIT | 81 |
| ISC | 2 (`fastq`, `glob-parent`) |
| BSD-2-Clause | 1 (`entities`) |
| BSD-3-Clause | 1 (`smol-toml`) |
| Python-2.0 | 1 (`argparse`) |

## Continuous integration

| Component | License |
| --- | --- |
| [`actions/checkout`](https://github.com/actions/checkout), [`actions/setup-node`](https://github.com/actions/setup-node) | MIT |
| [Gitleaks](https://github.com/gitleaks/gitleaks) CLI (`zricethezav/gitleaks` image, CI only) | MIT |

## Services

The optional advanced topology uses Cloudflare Tunnel and a WireGuard peer you
provide; GitHub access uses Coder's GitHub App. These are services used under
their providers' terms, not software distributed with the toolkit.
