#!/usr/bin/env python3
import ipaddress
import json
import sys


def fail(message: str) -> None:
    raise SystemExit(f"ERROR    {message}")


if len(sys.argv) != 7:
    fail("usage: validate-docker-network.py <name> <subnet> <gateway> <internal> <mtu> <attached-container-or-dash>")

name, expected_subnet, expected_gateway, expected_internal, expected_mtu, attached = sys.argv[1:]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeError) as error:
    fail(f"invalid Docker network inspect JSON: {error}")

if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    fail(f"Docker network {name} inspect must contain exactly one network")
network = payload[0]
if network.get("Name") != name or network.get("Driver") != "bridge":
    fail(f"Docker network {name} must be the expected bridge network")
if network.get("Internal") is not (expected_internal == "true"):
    fail(f"Docker network {name} Internal does not match the profile")

configs = network.get("IPAM", {}).get("Config", [])
ipv4 = [entry for entry in configs if isinstance(entry, dict) and ":" not in entry.get("Subnet", "")]
if len(ipv4) != 1:
    fail(f"Docker network {name} must have exactly one IPv4 IPAM config")
try:
    actual_subnet = ipaddress.ip_network(ipv4[0].get("Subnet", ""), strict=True)
    expected_network = ipaddress.ip_network(expected_subnet, strict=True)
except ValueError as error:
    fail(f"Docker network {name} has invalid IPAM: {error}")
if actual_subnet != expected_network or ipv4[0].get("Gateway") != expected_gateway:
    fail(f"Docker network {name} subnet or gateway does not match the profile")

actual_mtu = network.get("Options", {}).get("com.docker.network.driver.mtu", "")
expected_mtu_value = "" if expected_mtu == "-" else expected_mtu
if actual_mtu != expected_mtu_value:
    fail(f"Docker network {name} MTU does not match the profile")
if attached != "-" and attached not in {
        container.get("Name") for container in network.get("Containers", {}).values()
        if isinstance(container, dict)
}:
    fail(f"Docker network {name} is not attached to {attached}")
