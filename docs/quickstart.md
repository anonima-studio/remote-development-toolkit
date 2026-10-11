# Quickstart

From a fresh Ubuntu server to a Coder dashboard on your Mac with one command,
then to a first workspace. This guide uses the minimal topology: nothing is
published on the Internet, the dashboard is reached through an SSH forward, and
workspaces use the server's own Internet route.

Russian translation: [ru/quickstart.md](ru/quickstart.md).

## Requirements

### Mac (operator client)

The client is macOS. The scripts run with the system Bash 3.2.

| Tool | Needed for | Install |
| --- | --- | --- |
| OpenSSH client, `ssh-keygen`, `curl`, `nc` | everything | included with macOS |
| Git and Python 3 | everything | `xcode-select --install` (Command Line Tools) |
| [Homebrew](https://brew.sh) | installing the tools below | see its website |
| Coder CLI | workspaces, Codex Desktop, Local Agent, secrets | `brew install coder` |
| Node.js 24 and pnpm (via Corepack) | repository checks, Local Agent | `brew install node@24`, then `corepack enable` |
| Mutagen 0.18.1 exactly | `local-agent` mode only | `brew install mutagen-io/mutagen/mutagen` |
| Codex Desktop or Codex CLI | optional Codex adapter only | from OpenAI |

Keep the Coder CLI close to the server pin in
[`deploy/coder/compose.yaml`](../deploy/coder/compose.yaml); SSH reports a
client/server version problem otherwise.

### Server

- A fresh **Ubuntu 24.04 or 26.04** server, x86_64. Other releases are refused
  by the bootstrap. The Sysbox package used for nested Docker is amd64-only.
- An administrative SSH login that works with a key: `root`, or a user with
  `sudo`. The bootstrap disables root and password logins afterwards.
- Outbound HTTPS to Ubuntu and Docker package repositories, container
  registries (`ghcr.io`, Docker Hub) and the Terraform registry. If the
  Terraform registry is unreachable from your server, see
  [Troubleshooting](#troubleshooting).
- Recommended size: **4+ vCPU, 8+ GiB RAM, 80+ GB disk.** These are
  recommendations, not checked minimums. Coder and PostgreSQL are limited to
  3 CPU and 3 GiB together; each workspace adds its own hard limit
  (a `standard` workspace is 2 CPU and 4 GiB).

Nothing else is required. The advanced topology (public HTTPS hostname and
VPN egress) is optional and needs third-party credentials; see
[advanced-topology.md](advanced-topology.md).

## Run it

```bash
git clone https://github.com/anonima-studio/remote-development-toolkit.git remote-dev
cd remote-dev
bin/remote-dev quickstart example-profile --ssh root@203.0.113.10
```

Replace `example-profile` with a short lowercase name for this server (letters,
digits and `-`) and `203.0.113.10` with your server's address. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--ssh <user>@<server>` | required | First administrative login (`root` or a sudo user) |
| `--admin-key <path>` | your SSH agent and default keys | Private key for that administrative login, for example the one your provider installed |
| `--key <path>` | `~/.ssh/remote-dev` | Operator key for the `dev` user; created if missing |
| `--local-port <port>` | `3080` | Mac port for the dashboard forward |
| `--host-port <port>` | `3080` | Coder's loopback port on the server |

## What it does

The command prints one `==>` header per step.

1. **Operator key.** Uses `~/.ssh/remote-dev`, or creates an ed25519 key there
   without a passphrase. Add a passphrase later with
   `ssh-keygen -p -f ~/.ssh/remote-dev`.
2. **Host bootstrap.** If `dev@<server>` already works with that key, the step
   is skipped. Otherwise it opens the administrative login, keeps that
   connection open for up to 15 minutes, and runs
   [`bootstrap/ubuntu.sh`](../bootstrap/ubuntu.sh) through `sudo`. The
   bootstrap upgrades packages, enables unattended security updates, creates
   user `dev` with your public key and passwordless sudo, installs Docker and
   restic, hardens SSH (no root login, no passwords, `AllowUsers dev`) and
   enables UFW with only TCP/22 open. The command then proves a fresh `dev`
   login with passwordless sudo and Docker before it closes the administrative
   connection.
3. **SSH alias and profile.** Writes `~/.ssh/remote-dev-server-<profile>.conf`
   (`Host <profile>` → `dev@<server>` with your operator key and
   `IdentitiesOnly`) and adds an `Include` line for it at the top of
   `~/.ssh/config`. Creates `deploy/host-profiles/<profile>.env` with the
   minimal topology, unless it already exists. See
   [host profiles](../deploy/host-profiles/README.md).
4. **Install Coder and the workspace template.** Runs `install`: uploads the
   deployment, builds the workspace image, starts Coder and PostgreSQL on
   server loopback, creates the first owner account and an operator session,
   imports the **Remote Dev Docker** template and sets an eight-hour default
   autostop. The first run builds the workspace image; together with the system
   upgrade, expect 15–30 minutes in total.
5. **Doctor.** Runs `doctor` and prints one `OK`, `FAIL` or `SKIP` line per
   check.
6. **Next steps.** Prints the commands below.

The first SSH contact uses `StrictHostKeyChecking=accept-new`: the server's
host key is trusted on first use and enforced afterwards.

## Expected output

Abbreviated; your addresses and paths differ:

```text
==> Operator key
Created  ~/.ssh/remote-dev (no passphrase; add one with ssh-keygen -p -f ~/.ssh/remote-dev)

==> Host bootstrap
...
bootstrap complete; prove a fresh dev login before closing this root session
Verified dev@203.0.113.10 with passwordless sudo and Docker

==> SSH alias and profile
SSH      alias example-profile -> dev@203.0.113.10 (~/.ssh/remote-dev-server-example-profile.conf)
Profile  ./deploy/host-profiles/example-profile.env

==> Install Coder and the workspace template
...
remote-dev install reconciled successfully

==> Doctor
OK   passwordless sudo
OK   Docker daemon
...
SKIP Cloudflare Tunnel (ssh-forward ingress publishes nothing)
...
SKIP WireGuard egress (host egress uses the host default route)
...
OK   PostgreSQL healthy
...

==> Next steps
1. Keep the dashboard forwarded (leave it running):
     bin/remote-dev --profile example-profile forward
2. Open http://127.0.0.1:3080 in your browser.
3. Get the first owner login (shown once, then store it in a password manager):
     bin/remote-dev --profile example-profile credentials reveal coder --confirm-profile example-profile
4. Create a workspace from the 'remote-dev-docker' template in the dashboard, or:
     bin/remote-dev --profile example-profile workspace --help
```

The command exits non-zero when any doctor check fails; read the `FAIL` lines.

## Open the dashboard

1. In one terminal, start the forward and leave it running:

   ```bash
   bin/remote-dev --profile example-profile forward
   ```

   It prints `Forwarding http://127.0.0.1:3080 to Coder on example-profile;
   keep this command running.` Every Mac-side command that talks to Coder
   (browser, `workspace`, `codex-desktop`, `local-agent`, `credentials`) needs
   this forward while you use the minimal topology.
2. In a second terminal, reveal the owner login once:

   ```bash
   bin/remote-dev --profile example-profile credentials reveal coder \
     --confirm-profile example-profile
   ```

   A one-time page opens on `127.0.0.1` with the owner email and generated
   password, and clears itself after five minutes. Store the password in your
   password manager.
3. Open <http://127.0.0.1:3080> and sign in.
4. After you have stored the password and signed in, remove the server's
   bootstrap copy with the receipt shown on the page:

   ```bash
   bin/remote-dev --profile example-profile credentials finalize coder \
     --confirm-profile example-profile --confirm-stored \
     --receipt '<receipt shown with the credential>'
   ```

   Until you do, the password stays on the server in a mode-`0600` file so a
   lost handoff can be repeated.

## Create the first workspace

In the dashboard choose **Create Workspace** → **Remote Dev Docker**, then:

- **Workspace role:** *Development* (for `remote` mode) or *Runner* (for
  `local-agent` mode). The role cannot be changed later.
- **Docker capability:** *Core* (Docker CLI only) or *Containers* (internal
  Docker daemon; needs Sysbox, see below).
- **Workspace size:** *Standard* is a good start.

Or from the Mac, with the forward running:

```bash
bin/remote-dev --profile example-profile workspace create my-app \
  --role development --capability core --size standard
```

The command logs the Mac Coder CLI in to this deployment when needed (a
browser page asks you to confirm), waits for the agent, verifies the role
marker and prints the workspace, role, capability, size and Coder URL.

Expected visible result: the workspace card shows **Running** and a healthy
agent. Next steps:

- open it from Codex Desktop over SSH: [codex-on-coder.md](codex-on-coder.md);
- run a project's setup and health check: [project-setup.md](project-setup.md);
- keep your agent on the Mac and use the workspace as a runner:
  [local-agent-mode.md](local-agent-mode.md);
- open a service from the workspace on the Mac:
  `bin/remote-dev --profile example-profile preview my-app 3000`, wait for
  `READY`, open the printed `http://127.0.0.1:...` URL.

### Containers capability

`containers` workspaces need Sysbox on the host. Install it once, during a
quiet window, because the first installation restarts the host Docker service:

```bash
bin/remote-dev --profile example-profile sysbox-install
```

Stop running workspaces first; Coder and PostgreSQL restart on their own.

## Troubleshooting

| Step | Symptom | Recovery |
| --- | --- | --- |
| Start | `missing required tool: ...` | Install the named tool (see [Requirements](#requirements)) and rerun |
| Start | `invalid profile name` | Use lowercase letters, digits and `-` |
| Host bootstrap | `cannot open the administrative SSH login` | Check the address and user; pass `--admin-key` with the key your provider installed; check that the provider firewall allows TCP/22 |
| Host bootstrap | `supported baselines are Ubuntu 24.04 and 26.04` | Reinstall the server with a supported release |
| Host bootstrap | `dev@<server> does not work after bootstrap` | The administrative connection stays open for 15 minutes; the error prints the `ssh -o ControlPath=...` command to reuse it. Inspect `/home/dev/.ssh/authorized_keys` and `sshd -t`. Without that connection, use your provider's web console |
| Any later step | Root login is refused | Expected after bootstrap. Rerun the same `quickstart` command: a working `dev` login skips the bootstrap |
| SSH alias | `refusing non-regular SSH configuration state` | `~/.ssh/config` or the fragment is a symlink or not a regular file; replace it with a regular file and rerun |
| Profile | `generated profile did not validate` | Usually a subnet overlap. Create the profile with `init` and explicit `--egress-subnet`, `--egress-gateway`, `--control-subnet`, then rerun `quickstart` (an existing profile is reused) |
| Install | Image pull or package download fails | Check outbound HTTPS from the server, then rerun `bin/remote-dev --profile <p> install`; it is idempotent |
| Install | Template push fails resolving Terraform providers | The server cannot reach the public Terraform registry. Set `REMOTE_DEV_PROFILE_TERRAFORM_MIRROR=https://<mirror>/` in the profile and rerun `install` |
| Install | `workspace image source hash mismatch` | The template sources changed without a new image tag; do not edit files under `templates/docker/` locally unless you intend a new release |
| Doctor | A `FAIL` line | Fix the named component and rerun `bin/remote-dev --profile <p> doctor` |
| Dashboard | Browser cannot connect to `127.0.0.1:3080` | Start `forward` and keep it running; if the port is busy, recreate the profile with another `--local-port` |
| Credentials | Reveal fails to log in | Check that `forward` is running; rerun `reveal`. The server copy is kept until `finalize` |
| Workspace | Creation fails | Keep the Coder build log, run `doctor`, then retry. A `containers` workspace needs `sysbox-install` first |

To start over on the Mac side only, delete
`deploy/host-profiles/<profile>.env` and `~/.ssh/remote-dev-server-<profile>.conf`
and remove the matching `Include` line from `~/.ssh/config`. The toolkit never
deletes server data on its own; to start over on the server, reinstall it.
