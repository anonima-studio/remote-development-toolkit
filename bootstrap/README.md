# Ubuntu host bootstrap

`ubuntu.sh` brings a fresh Ubuntu 24.04 or 26.04 server to the baseline the
toolkit expects. It refuses other distributions and releases, and owns only
host prerequisites:

- full package upgrade and unattended security updates;
- user `dev` with your SSH public key and passwordless sudo;
- Docker Engine and the Compose plugin from Docker's repository, with `dev` in
  the `docker` group;
- restic, Git, jq, rsync and a few base tools;
- SSH hardening: no root login, no password or keyboard-interactive
  authentication, `AllowUsers dev`, `MaxAuthTries 3`, no X11 forwarding;
- UFW: deny incoming except TCP/22, allow outgoing;
- `/opt/remote-dev` owned by `dev`.

## Usual path

`bin/remote-dev quickstart <profile> --ssh <admin>@<server>` runs this script
through `sudo` over the administrative login, keeps that connection open until
a fresh `dev` login with sudo and Docker is proven, and then continues with the
installation. See [docs/quickstart.md](../docs/quickstart.md).

## Manual run

From a verified root console or SSH session on the server:

```bash
sudo REMOTE_DEV_PUBLIC_KEY="$(cat remote-dev.pub)" ./ubuntu.sh
```

`REMOTE_DEV_PUBLIC_KEY` must be one complete `ssh-ed25519` or `ssh-rsa` public
key. Keep the root session open until a fresh `dev` login and `sudo -n true`
both succeed: after the script reloads SSH, root and password logins are
refused.

## Out of scope

Provider-specific VM creation, DNS, disks and snapshots. A short fail-fast
script is easier to audit than a configuration-management dependency for one
baseline; a tool such as Ansible becomes worthwhile when several host roles,
distributions or rolling updates create repeated convergence logic.
