# Workspace networks and WireGuard egress

Every profile gets two Docker networks, created by the oneshot unit
`remote-dev-egress.service`:

- `remote-dev-egress`: the workspaces' default network, with the profile's
  subnet, gateway and MTU;
- `remote-dev-control`: an internal network that connects workspace agents
  directly to Coder at `http://coder-control:7080`, so agent traffic stays on
  the host.

The profile's `REMOTE_DEV_PROFILE_EGRESS` decides where workspace traffic goes.

## `host` egress (default for new profiles)

Workspaces leave through the server's default route with Docker's standard
NAT. The unit only creates and validates the two networks. MTU is usually
`1500`.

## `wireguard` egress (optional)

A source policy rule sends only the egress subnet through the WireGuard
interface to your peer. Host-originated SSH, Coder, PostgreSQL and
`cloudflared` traffic keeps the host route.

- The profile's policy table holds the WireGuard default route and a
  lower-priority `unreachable default`. If the interface or its route
  disappears, lookups fail instead of falling back to the host route: workspace
  egress fails closed.
- Workspace DNS uses explicit public resolvers through the egress path;
  Docker's embedded resolver still resolves the `coder-control` alias.
- Filtering lives in a dedicated `REMOTE-DEV-EGRESS` chain reached from
  `DOCKER-USER`; the scripts do not compete with Docker's generated rules.
- MTU is set on the Docker network (`1420` to match WireGuard) instead of a
  TCP MSS clamp. Nested Docker in `containers` workspaces uses the same MTU.
  An existing network with another MTU must be removed while all workspaces
  are stopped; the unit refuses to claim success otherwise.

| File | Purpose |
| --- | --- |
| `remote-dev-egress-up.sh`, `remote-dev-egress-down.sh` | Unit start and stop: networks; with `wireguard`, the interface, policy rule, routes and filter chain |
| `remote-dev-egress.service` | systemd oneshot unit |
| `validate-docker-network.py` | Checks an existing network's driver, IPAM, gateway, internal mode, MTU and attachments |
| `apparmor-local-wg` | Local AppArmor rule that lets `wg` read the generated runtime configuration under `/run/wireguard` |
| [`../networkd/10-remote-dev-egress.conf`](../networkd/10-remote-dev-egress.conf) | `systemd-networkd` drop-in that keeps foreign policy rules and routes |

The WireGuard client configuration contains a private key and stays outside
Git. The profile points to it with `REMOTE_DEV_PROFILE_WIREGUARD_CONFIG`;
`install` validates its address and key against the profile and installs it as
`/etc/wireguard/<interface>.conf`, mode `0600`. Setup:
[docs/advanced-topology.md](../../docs/advanced-topology.md).

## networkd and lost rules

`systemd-networkd` removes policy rules and routes it did not create whenever
it starts (`ManageForeignRoutingPolicyRules` and `ManageForeignRoutes` default
to yes). A networkd restart after an unattended upgrade can therefore drop the
source rule and the `unreachable default` route while the unit still reports
`active (exited)`; the filter chain then rejects every workspace packet.
`install` places the drop-in in `/etc/systemd/networkd.conf.d/`. It takes
effect at networkd's next start, which `install` does not trigger, and
`ManageForeignRoutes=no` also keeps other foreign routes on the host.

`doctor` reports the effective settings, whether networkd started after its
newest configuration file (a heuristic; only a restart proves it), and whether
every running workspace exits through an address different from the host's.
To restore a lost rule without dropping the tunnel, run only the `ip route` and
`ip rule` lines of `remote-dev-egress-up.sh`.

`install` ends with `systemctl restart remote-dev-egress.service`, which runs
the down script and deletes the tunnel interface: every workspace loses its
network until startup succeeds. Run `install` in a quiet window.

## Verify

```bash
bin/remote-dev --profile <profile> doctor
```

With `wireguard` egress, `doctor` checks the unit, interface, address, recent
handshake, policy rule, fail-closed route, filter chain, network contracts and
distinct host and workspace exit addresses. For a manual check, compare
`curl -4 -fsS https://api.ipify.org` run on the server with the same command in
a workspace terminal.
