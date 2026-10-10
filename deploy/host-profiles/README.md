# Host profiles

A host profile is the complete, non-secret deployment identity of one server.
Every command selects one explicitly; there is no default:

```bash
bin/remote-dev --profile <name> <command>
REMOTE_DEV_PROFILE=<name> bin/remote-dev <command>
```

Profiles live in this directory as `<name>.env`. They are local operator files
and are ignored by Git, except [`example.env`](example.env). A profile may
route to credential files but must never contain secret values.

## Create a profile

- `bin/remote-dev quickstart <name> --ssh <user>@<server>` writes a minimal
  profile as part of server setup (see [docs/quickstart.md](../../docs/quickstart.md)).
  It reuses an existing `<name>.env` instead of overwriting it.
- `bin/remote-dev init <name> --ssh-host <alias>` writes only the profile, for a
  server you already reach through an SSH alias. Options: `--local-port`
  (default `3080`), `--host-port` (default `3080`), `--egress-subnet`
  (default `10.250.10.0/24`), `--egress-gateway` (default `10.250.10.1`),
  `--control-subnet` (default `10.250.11.0/24`). The generated file is
  validated immediately and removed if it is invalid.
- For the advanced topology, copy [`example.env`](example.env) and fill in the
  advanced fields; see [docs/advanced-topology.md](../../docs/advanced-topology.md).

Inspect the selected identity with:

```bash
bin/remote-dev --profile <name> profile
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `REMOTE_DEV_PROFILE_NAME` | always | Must equal the file name without `.env`; lowercase letters, digits, `-` |
| `REMOTE_DEV_PROFILE_INGRESS` | recommended | `ssh-forward` or `cloudflare-tunnel`. Missing means `cloudflare-tunnel` (compatibility with older profiles) |
| `REMOTE_DEV_PROFILE_EGRESS` | recommended | `host` or `wireguard`. Missing means `wireguard` |
| `REMOTE_DEV_PROFILE_SSH_HOST` | always | SSH alias or host for the `dev` user |
| `REMOTE_DEV_PROFILE_CODER_ACCESS_URL` | always | `ssh-forward`: `http://127.0.0.1:<local-port>`. `cloudflare-tunnel`: `https://<tunnel hostname>` |
| `REMOTE_DEV_PROFILE_CODER_HOST_PORT` | always | Coder's loopback port on the server |
| `REMOTE_DEV_PROFILE_EGRESS_SUBNET`, `_EGRESS_GATEWAY` | always | IPv4 subnet and gateway of `remote-dev-egress` |
| `REMOTE_DEV_PROFILE_EGRESS_MTU` | always | `1500` for `host`, `1420` for `wireguard` (allowed range 1280–9000) |
| `REMOTE_DEV_PROFILE_CONTROL_SUBNET` | always | IPv4 subnet of the internal `remote-dev-control` network |
| `REMOTE_DEV_PROFILE_CLOUDFLARE_TUNNEL_ID` | `cloudflare-tunnel` | Tunnel UUID, lowercase |
| `REMOTE_DEV_PROFILE_CLOUDFLARE_HOSTNAME` | `cloudflare-tunnel` | Public hostname; must equal the access URL's host |
| `REMOTE_DEV_PROFILE_CLOUDFLARE_CREDENTIALS` | `cloudflare-tunnel` | Absolute path to the tunnel credential JSON on the Mac |
| `REMOTE_DEV_PROFILE_WIREGUARD_INTERFACE` | `wireguard` | Interface name, for example `wg-egress` |
| `REMOTE_DEV_PROFILE_WIREGUARD_ADDRESS` | `wireguard` | Interface address from the peer, CIDR notation |
| `REMOTE_DEV_PROFILE_WIREGUARD_PUBLIC_KEY` | `wireguard` | Public key derived from the client private key |
| `REMOTE_DEV_PROFILE_WIREGUARD_CONFIG` | `wireguard` | Absolute path to the client configuration on the Mac |
| `REMOTE_DEV_PROFILE_POLICY_TABLE` | `wireguard` | Free routing table number, not 253–255 |
| `REMOTE_DEV_PROFILE_TERRAFORM_MIRROR` | optional | `https://…/` provider network mirror, for servers that cannot reach the public Terraform registry |
| `REMOTE_DEV_PROFILE_EXPECTED_HOSTNAME` | optional | `install` refuses a server whose hostname differs |
| `REMOTE_DEV_PROFILE_SYSTEM_ROLE`, `_DISPLAY_NAME` | optional | Labels recorded with the identity |
| `REMOTE_DEV_PROFILE_COOLIFY_ACCESS_URL` | optional | HTTPS origin of a platform colocated on the same server, used by `credentials` |

The egress, control and WireGuard networks must not overlap each other or
other networks on the server. Pick ranges that do not collide with your LAN or
VPN.

Paths may use `$HOME`; the file is read by Bash.

## Fail-closed rules

`deploy/host-profiles/load.sh` validates the profile before any remote action:

- no profile selected → refused;
- file name and `REMOTE_DEV_PROFILE_NAME` differ → refused;
- a field required by the chosen topology is missing or malformed → refused;
- `REMOTE_DEV_HOST` (or another identity variable) set while the profile is
  complete → refused, because an SSH destination alone is not a deployment
  identity. Environment overrides are accepted only for identity fields the
  profile leaves empty, and then for all of those fields at once.

`install` records the identity on the server in
`/etc/remote-dev/host-profile.env` and refuses to run a different identity
against that server later. Changing a server's topology is therefore a
deliberate step; see
[docs/advanced-topology.md](../../docs/advanced-topology.md#change-the-topology-of-an-installed-server).

`REMOTE_DEV_PROFILE_DIR` selects another profile directory; the test suite
uses it with the synthetic profiles in `tests/fixtures/host-profiles/`.

## Other files

| File | Purpose |
| --- | --- |
| `load.sh` | Profile loader and validation, sourced by every command |
| `validate-credentials.py` | Checks a tunnel credential or WireGuard configuration against the profile without printing secrets |
| `example.env` | Commented template |
