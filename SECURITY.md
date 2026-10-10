# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability
reporting: open the repository's **Security** tab and choose **Report a
vulnerability**
(<https://github.com/anonima-studio/remote-development-toolkit/security/advisories/new>). Do not open a public issue for a suspected vulnerability.

Include what you can of:

- the affected file, command or component;
- the topology and mode involved (`ssh-forward`/`cloudflare-tunnel`,
  `host`/`wireguard`, `remote`/`local-agent`);
- steps to reproduce and the impact you expect;
- whether the issue is already public.

Do not include real credentials, tokens or private host details in a report;
synthetic values are enough.

Reports are handled on a best-effort basis by the maintainer. There is no
guaranteed response time and no bug bounty.

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release
branches; fixes land on `main`.

## Scope

In scope: the code and documentation in this repository, including the
deployment scripts, workspace template, Mac-side commands and agent skills.

Out of scope: vulnerabilities in upstream components (Coder, PostgreSQL,
Sysbox, Docker, cloudflared, WireGuard, restic, Mutagen, Codex and others);
report those to their maintainers. Also out of scope are risks that follow
from the documented trust model: a single trusted operator, trusted templates,
the Coder provisioner's host Docker socket, and the `dev` user's passwordless
sudo. See [docs/architecture.md](docs/architecture.md#threat-model) and
[docs/limitations.md](docs/limitations.md).
