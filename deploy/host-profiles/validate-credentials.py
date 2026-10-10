#!/usr/bin/env python3
import base64
import binascii
import ipaddress
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional


def fail(message: str) -> None:
    raise SystemExit(f"ERROR    {message}")


def decode_wireguard_key(value: str, label: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        fail(f"{label} is not valid base64")
    if len(decoded) != 32:
        fail(f"{label} must decode to 32 bytes")


def validate_cloudflare(path: pathlib.Path, expected_tunnel: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"invalid Cloudflare credentials JSON: {error}")
    if not isinstance(payload, dict):
        fail("Cloudflare credentials must be a JSON object")
    if payload.get("TunnelID") != expected_tunnel:
        fail("Cloudflare credentials TunnelID does not match the selected profile")
    for field in ("AccountTag", "TunnelSecret"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            fail(f"Cloudflare credentials are missing {field}")
    try:
        base64.b64decode(payload["TunnelSecret"], validate=True)
    except (ValueError, binascii.Error):
        fail("Cloudflare TunnelSecret is not valid base64")


def x25519_public_key(private_key: bytes) -> bytes:
    scalar = bytearray(private_key)
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    value = int.from_bytes(scalar, "little")
    prime = 2**255 - 19
    x_1, x_2, z_2, x_3, z_3, swap = 9, 1, 0, 9, 1, 0
    for position in range(254, -1, -1):
        bit = (value >> position) & 1
        swap ^= bit
        if swap:
            x_2, x_3 = x_3, x_2
            z_2, z_3 = z_3, z_2
        swap = bit
        a = (x_2 + z_2) % prime
        aa = a * a % prime
        b = (x_2 - z_2) % prime
        bb = b * b % prime
        e = (aa - bb) % prime
        c = (x_3 + z_3) % prime
        d = (x_3 - z_3) % prime
        da = d * a % prime
        cb = c * b % prime
        x_3 = (da + cb) ** 2 % prime
        z_3 = x_1 * (da - cb) ** 2 % prime
        x_2 = aa * bb % prime
        z_2 = e * (aa + 121665 * e) % prime
    if swap:
        x_2, x_3 = x_3, x_2
        z_2, z_3 = z_3, z_2
    return (x_2 * pow(z_2, prime - 2, prime) % prime).to_bytes(32, "little")

def validate_wireguard(path: pathlib.Path, expected_address: str,
                       expected_public_key: str) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        fail(f"invalid WireGuard config: {error}")

    section = ""
    interface_keys: List[str] = []
    interface_addresses: List[str] = []
    peers: List[Dict[str, str]] = []
    current_peer: Optional[Dict[str, str]] = None
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section == "Peer":
                current_peer = {}
                peers.append(current_peer)
            elif section == "Interface":
                current_peer = None
            else:
                fail(f"unsupported WireGuard section on line {line_number}: {section}")
            continue
        if "=" not in line or section not in ("Interface", "Peer"):
            fail(f"invalid WireGuard config line {line_number}")
        key, value = (part.strip() for part in line.split("=", 1))
        if not value:
            fail(f"empty WireGuard value on line {line_number}")
        if section == "Interface" and key == "PrivateKey":
            interface_keys.append(value)
        elif section == "Interface" and key == "Address":
            interface_addresses.extend(part.strip() for part in value.split(","))
        elif section == "Peer" and current_peer is not None:
            current_peer[key] = value

    if len(interface_keys) != 1:
        fail("WireGuard config must contain exactly one Interface PrivateKey")
    decode_wireguard_key(interface_keys[0], "WireGuard Interface PrivateKey")
    private_key = base64.b64decode(interface_keys[0])
    actual_public_key = base64.b64encode(x25519_public_key(private_key)).decode()
    if actual_public_key != expected_public_key:
        fail("WireGuard Interface public key does not match the selected profile")
    try:
        expected_interface = ipaddress.ip_interface(expected_address)
        actual_interfaces = [ipaddress.ip_interface(value) for value in interface_addresses]
    except ValueError as error:
        fail(f"invalid WireGuard Interface Address: {error}")
    if actual_interfaces != [expected_interface]:
        fail("WireGuard Interface Address does not match the selected profile")
    if len(peers) != 1:
        fail("WireGuard config must contain exactly one Peer")
    for index, peer in enumerate(peers, start=1):
        for field in ("PublicKey", "Endpoint", "AllowedIPs"):
            if not peer.get(field):
                fail(f"WireGuard Peer {index} is missing {field}")
        decode_wireguard_key(peer["PublicKey"], f"WireGuard Peer {index} PublicKey")
        endpoint = peer["Endpoint"]
        if endpoint.startswith("["):
            match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", endpoint)
            if not match:
                fail(f"WireGuard Peer {index} Endpoint has malformed IPv6 brackets")
            endpoint_host, endpoint_port = match.groups()
            try:
                endpoint_address = ipaddress.ip_address(endpoint_host)
            except ValueError:
                fail(f"WireGuard Peer {index} bracketed Endpoint host must be IPv6")
            if endpoint_address.version != 6:
                fail(f"WireGuard Peer {index} bracketed Endpoint host must be IPv6")
        else:
            if endpoint.count(":") != 1 or "[" in endpoint or "]" in endpoint:
                fail(f"WireGuard Peer {index} Endpoint must be hostname:port, IPv4:port, or [IPv6]:port")
            endpoint_host, endpoint_port = endpoint.split(":", 1)
            try:
                endpoint_address = ipaddress.ip_address(endpoint_host)
            except ValueError:
                labels = endpoint_host.split(".")
                if (len(endpoint_host) > 253 or any(
                        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                        for label in labels)):
                    fail(f"WireGuard Peer {index} Endpoint host is invalid")
        try:
            port = int(endpoint_port)
        except ValueError:
            fail(f"WireGuard Peer {index} Endpoint port must be numeric")
        if not 1 <= port <= 65535:
            fail(f"WireGuard Peer {index} Endpoint port is out of range")
        try:
            allowed_networks = {
                ipaddress.ip_network(network.strip(), strict=False)
                for network in peer["AllowedIPs"].split(",")
            }
        except ValueError as error:
            fail(f"WireGuard Peer {index} has invalid AllowedIPs: {error}")
        if ipaddress.ip_network("0.0.0.0/0") not in allowed_networks:
            fail(f"WireGuard Peer {index} AllowedIPs must include 0.0.0.0/0")


def main() -> None:
    if len(sys.argv) < 3:
        fail("usage: validate-credentials.py cloudflare <path> <tunnel-id> | wireguard <path> <address> <public-key>")
    kind = sys.argv[1]
    path = pathlib.Path(sys.argv[2])
    if kind == "cloudflare" and len(sys.argv) == 4:
        validate_cloudflare(path, sys.argv[3])
    elif kind == "wireguard" and len(sys.argv) == 5:
        validate_wireguard(path, sys.argv[3], sys.argv[4])
    else:
        fail("invalid credential validator arguments")


if __name__ == "__main__":
    main()
