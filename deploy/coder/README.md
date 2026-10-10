# Coder deployment

Coder Community and PostgreSQL run as one Docker Compose project,
`remote-dev-coder`, on the server.

- Coder is pinned to `v2.36.0` by image digest; PostgreSQL 17 is pinned by
  digest too. Upgrades are explicit repository changes followed by
  verification.
- PostgreSQL is reachable only on the Compose network.
- Coder's port binds only to `127.0.0.1:<CODER_HOST_PORT>` on the server
  (`3080` for new profiles). Users reach it through `remote-dev forward` or, in
  the advanced topology, through the Cloudflare Tunnel.
- Workspace agents reach Coder over the internal `remote-dev-control` network
  at `http://coder-control:7080`. Client-facing app links use the access URL,
  which the template receives as a separate variable.
- Resource limits: Coder 2 CPU, 2 GiB, 1024 PIDs; PostgreSQL 1 CPU, 1 GiB, 256
  PIDs.
- The host Docker socket is mounted into Coder on purpose: the Terraform
  provisioner creates workspace containers through it. This gives the
  provisioner root-equivalent control of the host and is acceptable only with
  trusted templates and a single trusted operator. Workspaces never receive
  the socket.
- Coder's built-in GitHub External Auth provider is used; `doctor` verifies its
  effective availability through the API.

| File | Purpose |
| --- | --- |
| `compose.yaml` | Services, limits, networks and volumes |
| `.env.example` | Shape of the server-side `.env`; `install` generates the real one with a random PostgreSQL password, mode `0600` |
| `terraformrc` | Terraform CLI configuration for the provisioner; uses the public registry unless the profile sets `REMOTE_DEV_PROFILE_TERRAFORM_MIRROR` |

## Install and reconcile

Run from the repository root:

```bash
bin/remote-dev --profile <profile> install
```

It uploads the deployment and template to `/opt/remote-dev`, keeps existing
secrets, starts Compose, creates the first owner when needed, creates a
protected operator session, imports the template when its content changed,
reconciles the eight-hour autostop with a one-hour activity bump, and verifies
Coder and PostgreSQL. It ends with `remote-dev install reconciled
successfully`. Repeating it with unchanged inputs is safe.

## Owner account and operator session

On first install the owner account (username `owner`) is created with a random
password stored only at `/opt/remote-dev/coder/bootstrap-owner.env`, mode
`0600`. Reveal it once and remove the server copy after storing it:

```bash
bin/remote-dev --profile <profile> credentials reveal coder --confirm-profile <profile>
bin/remote-dev --profile <profile> credentials finalize coder --confirm-profile <profile> \
  --confirm-stored --receipt '<receipt shown with the credential>'
```

The CLI uses `/opt/remote-dev/coder/operator.session` (mode `0600`), not the
owner password. Coder refreshes active sessions; one unused beyond its
lifetime expires. Replace it with a new token from stdin, then rerun `doctor`:

```bash
coder login token | bin/remote-dev --profile <profile> token-update
```

The real `.env` exists only on the server. Never commit it.
