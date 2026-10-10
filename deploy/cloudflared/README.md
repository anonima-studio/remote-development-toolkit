# Cloudflare Tunnel ingress (optional)

Used only by profiles with `REMOTE_DEV_PROFILE_INGRESS=cloudflare-tunnel`. A
named Cloudflare Tunnel publishes Coder at a public HTTPS hostname while Coder
stays bound to server loopback. `cloudflared` makes outbound-only connections
to Cloudflare; the server opens no HTTP or HTTPS port.

| File | Purpose |
| --- | --- |
| `config.yml` | Template rendered by `install` with the profile's tunnel ID, hostname and Coder loopback port; installed as `/etc/cloudflared/config.yml` |
| `cloudflared.service` | systemd unit running `cloudflared tunnel --config /etc/cloudflared/config.yml run` |

The tunnel credential JSON is a secret and stays outside Git. The profile
points to it with `REMOTE_DEV_PROFILE_CLOUDFLARE_CREDENTIALS` (an absolute,
owner-only file on the Mac). `install` checks that its `TunnelID` matches the
profile, uploads it to `/etc/cloudflared/<tunnel-id>.json` with mode `0600`,
installs `cloudflared` from Cloudflare's package repository, validates the
ingress rules and enables the unit. If the Mac copy is absent, an existing
server copy for the same tunnel is kept.

Setup, verification and recovery: [docs/advanced-topology.md](../../docs/advanced-topology.md).

Quick checks:

```bash
bin/remote-dev --profile <profile> doctor      # Cloudflare Tunnel checks
curl -fsS https://coder.example.com/api/v2/buildinfo
ssh <profile> 'sudo systemctl is-active cloudflared'
```
