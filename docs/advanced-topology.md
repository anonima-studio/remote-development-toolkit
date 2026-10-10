# Advanced topology: Cloudflare Tunnel and WireGuard egress

The minimal topology from the [quickstart](quickstart.md) publishes nothing and
lets workspaces use the server's own Internet route. The advanced topology adds
two independent options:

- **`cloudflare-tunnel` ingress:** Coder gets a public HTTPS hostname, such as
  `https://coder.example.com`, through an outbound-only Cloudflare Tunnel. The
  server still opens no HTTP ports. Use it when you want the dashboard and
  Coder clients to work without a running SSH forward.
- **`wireguard` egress:** workspace traffic leaves through a WireGuard peer
  instead of the server's address, and fails closed when the tunnel is down.
  Host services keep the server's own route. Use it when workspace traffic must
  come from a different, stable address than the server.

You can enable either one alone. The design is described in
[architecture.md](architecture.md#topologies).

## Choose before the first install

The topology is part of the deployment identity that `install` records on the
server. Decide it before the first install whenever possible:

1. Write the profile by hand from
   [`deploy/host-profiles/example.env`](../deploy/host-profiles/example.env) as
   `deploy/host-profiles/<profile>.env`, with
   `REMOTE_DEV_PROFILE_SSH_HOST=<profile>`.
2. Place the credentials (below).
3. Run `bin/remote-dev quickstart <profile> --ssh root@<server>`. It reuses an
   existing profile file instead of writing the minimal one.

## Required profile fields

Field reference: [host profiles](../deploy/host-profiles/README.md).

### Cloudflare Tunnel ingress

```bash
REMOTE_DEV_PROFILE_INGRESS=cloudflare-tunnel
REMOTE_DEV_PROFILE_CODER_ACCESS_URL=https://coder.example.com
REMOTE_DEV_PROFILE_CLOUDFLARE_TUNNEL_ID=<tunnel UUID, lowercase>
REMOTE_DEV_PROFILE_CLOUDFLARE_HOSTNAME=coder.example.com
REMOTE_DEV_PROFILE_CLOUDFLARE_CREDENTIALS="$HOME/.config/remote-dev/<profile>/cloudflared.json"
```

The access URL must be `https://` and its host must equal the tunnel hostname.

### WireGuard egress

```bash
REMOTE_DEV_PROFILE_EGRESS=wireguard
REMOTE_DEV_PROFILE_WIREGUARD_INTERFACE=wg-egress
REMOTE_DEV_PROFILE_WIREGUARD_ADDRESS=<interface address from the peer, e.g. 10.66.0.2/32>
REMOTE_DEV_PROFILE_WIREGUARD_PUBLIC_KEY=<public key of the client private key>
REMOTE_DEV_PROFILE_WIREGUARD_CONFIG="$HOME/.config/remote-dev/<profile>/wg-egress.conf"
REMOTE_DEV_PROFILE_EGRESS_MTU=1420
REMOTE_DEV_PROFILE_POLICY_TABLE=<free routing table number, e.g. 4242>
```

The egress subnet, control subnet and WireGuard address must not overlap. The
policy table must not be a reserved value (253, 254, 255) and must be unused on
the server.

## Credentials

Both files are secrets. They stay outside Git, on the Mac, owned by you with
owner-only permissions; `install` refuses them otherwise and validates them
against the profile before uploading.

```bash
mkdir -p ~/.config/remote-dev/<profile>
chmod 700 ~/.config/remote-dev/<profile>
```

### Cloudflare tunnel credential

With [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
on the Mac and a domain on your Cloudflare account:

```bash
cloudflared tunnel login
cloudflared tunnel create <tunnel-name>        # prints the tunnel UUID
cloudflared tunnel route dns <tunnel-name> coder.example.com
install -m 600 ~/.cloudflared/<tunnel-uuid>.json \
  ~/.config/remote-dev/<profile>/cloudflared.json
```

`install` checks that the file's `TunnelID` equals
`REMOTE_DEV_PROFILE_CLOUDFLARE_TUNNEL_ID` and that it contains `AccountTag` and
`TunnelSecret`. On the server it installs `cloudflared` from Cloudflare's
package repository, writes `/etc/cloudflared/config.yml` from
[`deploy/cloudflared/config.yml`](../deploy/cloudflared/config.yml) and the
credential with mode `0600`, validates the ingress rules and enables the
`cloudflared` unit.

The tunnel makes the Coder login page reachable from the Internet. Coder's own
authentication protects it; putting Cloudflare Access in front of it is
possible but not managed by this toolkit.

### WireGuard client configuration

You need a WireGuard peer that will forward and masquerade workspace traffic,
and a client configuration for this server from that peer. The file must
contain exactly one `[Interface]` with `PrivateKey` and an `Address` equal to
`REMOTE_DEV_PROFILE_WIREGUARD_ADDRESS`, and at least one `[Peer]` with
`PublicKey`, `Endpoint` and `AllowedIPs` (usually `0.0.0.0/0`).

```bash
install -m 600 <client-config> ~/.config/remote-dev/<profile>/wg-egress.conf
# Public key for the profile:
sed -n 's/^PrivateKey *= *//p' ~/.config/remote-dev/<profile>/wg-egress.conf | wg pubkey
```

`install` checks the address and that the private key matches the profile's
public key. On the server it installs `wireguard-tools`, stores the
configuration at `/etc/wireguard/<interface>.conf` (mode `0600`), and installs
the `remote-dev-egress` unit and a `systemd-networkd` drop-in that keeps the
policy rule across networkd restarts. `Address`, `DNS`, `MTU`, `Table` and hook
lines are stripped from the runtime copy; routing is owned by the unit. Details:
[`deploy/wireguard/README.md`](../deploy/wireguard/README.md).

## Change the topology of an installed server

`install` refuses to proceed when the server's recorded identity
(`/etc/remote-dev/host-profile.env`) differs from the profile; this protects
against pointing a profile at the wrong server. Changing the topology is
therefore a deliberate identity change. This procedure follows the code but
has not been rehearsed as part of this release:

1. Stop every workspace in Coder.
2. Edit the profile and place the credentials as above.
3. On the server, move the recorded identity aside:
   `ssh <profile> 'sudo mv /etc/remote-dev/host-profile.env /etc/remote-dev/host-profile.env.previous'`.
4. If the egress MTU changes (for example 1500 → 1420), remove the egress
   network while no workspace container exists:
   `ssh <profile> 'docker network rm remote-dev-egress'`. The network unit
   refuses to reuse a network with a different MTU.
5. If the Coder access URL changes, force the template to be re-imported with
   the new URL: `ssh <profile> 'rm -f /opt/remote-dev/coder/template.sha256'`.
6. Run `bin/remote-dev --profile <profile> install`, then `doctor`.
7. Start each workspace, choosing **Update** where Coder offers it.
8. Rerun `codex-desktop` for any workspace you use from Codex Desktop, so the
   Mac-side Coder configuration uses the new access URL.

## Verify

```bash
bin/remote-dev --profile <profile> doctor
curl -fsS https://coder.example.com/api/v2/buildinfo
```

`doctor` adds these checks for the advanced topology: `Cloudflare Tunnel
enabled`, `active`, `identity`, `hostname` and `credentials`; `owned HTTPS
Coder API`; `WireGuard interface`, `WireGuard address` and `recent WireGuard
handshake`; `workspace source policy`, `workspace fail-closed route` and
`Docker egress policy chain`. With at least one running workspace it also
compares the workspace exit address with the server's and prints
`OK   all N running workspaces use a VPN egress distinct from the host`.

## Recovery

| Symptom | Recovery |
| --- | --- |
| `install`: credentials file must be owner-only / does not match | Fix ownership and mode (`chmod 600`), or the profile's tunnel ID, address or public key |
| `install`: `existing /etc/remote-dev/host-profile.env does not match profile` | You are changing the identity of an installed server; follow [the procedure above](#change-the-topology-of-an-installed-server), or check that the profile points at the intended server |
| HTTPS hostname unreachable | `ssh <profile> 'sudo systemctl status cloudflared'`; check the DNS route in Cloudflare. Meanwhile reach the dashboard over SSH: `ssh -N -L 127.0.0.1:3080:127.0.0.1:<host-port> <profile>` and open <http://127.0.0.1:3080> |
| Workspaces have no network, `doctor` fails the policy or fail-closed route | The tunnel or its rule is down; workspaces fail closed by design. Check `sudo wg show` and the peer. To restore only a lost rule, run the `ip route` and `ip rule` lines of `remote-dev-egress-up.sh`; restarting the unit drops the tunnel for every workspace |
| Policy rule disappears after an upgrade | `systemd-networkd` removed it; the drop-in prevents this from its next start. See [`deploy/wireguard/README.md`](../deploy/wireguard/README.md) |
| Large responses stall inside nested containers | MTU mismatch; keep `REMOTE_DEV_PROFILE_EGRESS_MTU=1420` and update the workspace so its inner Docker daemon uses it |

`install` restarts the egress unit at its end, which briefly interrupts
workspace networking. Run it in a quiet window.
