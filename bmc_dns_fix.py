"""
Fix for: "BMC DNS: FAILED ... PATCH failed HTTP 400: Base.1.5.0.PropertyUnknown,
RelatedProperties: ['#/StaticNameServers']"

(Intel server only — targets the Redfish EthernetInterface resource
documented in the Intel Server System Integrated BMC Firmware OpenBMC
Redfish API Specification.)

Root cause: StaticNameServers is a top-level, RW property directly on the
EthernetInterface resource (spec section 2.30, "BMC Ethernet Interface").
Older Intel BMC firmware generations exposed the same setting through a
different shape (nested under Oem, or only settable via
IPv4StaticAddresses/NetworkProtocol), so a request built for one
generation returns Base.1.5.0.PropertyUnknown on another.

This module tries every known request shape in order — current OpenBMC
spec shape first, then progressively older/alternate shapes — via
try_variants() from redfish_client, so a fix that works on the new fleet
automatically falls back to whatever the old fleet actually expects
(and vice versa), instead of failing outright on the first mismatch.

Depends on redfish_client.RedfishClient for auth/session handling.
"""

from redfish_client import RedfishClient, try_variants

CANDIDATE_INTERFACE_IDS = ["eth0", "eth1"]


def list_bmc_ethernet_interfaces(bmc):
    """
    GET /redfish/v1/Managers/bmc/EthernetInterfaces, then GET each member,
    to list the BMC's logical NICs along with their current
    NameServers/StaticNameServers/DHCPv4 state, so the correct interface
    ID can be identified before patching DNS. Falls back to
    CANDIDATE_INTERFACE_IDS (eth0/eth1) if the collection itself can't be
    read (seen on some older firmware that restricts collection GETs).
    """
    try:
        collection = bmc.get("/redfish/v1/Managers/bmc/EthernetInterfaces")
        members = collection.get("Members", [])
        if members:
            return [bmc.get(m["@odata.id"]) for m in members]
    except Exception:
        pass

    interfaces = []
    for candidate_id in CANDIDATE_INTERFACE_IDS:
        try:
            interfaces.append(bmc.get(f"/redfish/v1/Managers/bmc/EthernetInterfaces/{candidate_id}"))
        except Exception:
            continue
    return interfaces


def _patch_static_name_servers_top_level(bmc, interface_id, dns_servers):
    """New/current shape (spec section 2.30): StaticNameServers as a top-level EthernetInterface property."""
    return bmc.patch(
        f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}",
        {"StaticNameServers": dns_servers},
    )


def _patch_static_name_servers_oem(bmc, interface_id, dns_servers):
    """Older-firmware shape: DNS servers exposed as an Oem/OpenBMC extension instead of a top-level property."""
    return bmc.patch(
        f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}",
        {"Oem": {"OpenBMC": {"DNSServers": dns_servers}}},
    )


def _patch_static_name_servers_ipv4(bmc, interface_id, dns_servers):
    """Legacy pre-OpenBMC shape: DNS servers folded into the IPv4StaticAddresses array's Gateway-adjacent fields."""
    return bmc.patch(
        f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}",
        {"IPv4StaticAddresses": [{"NameServers": dns_servers}]},
    )


def _patch_static_name_servers_network_protocol(bmc, dns_servers):
    """Oldest fallback: some early Intel BMC builds only exposed DNS config via the Manager NetworkProtocol resource."""
    return bmc.patch(
        "/redfish/v1/Managers/bmc/NetworkProtocol",
        {"DNS": {"NameServers": dns_servers}},
    )


def fix_bmc_dns(bmc, interface_id, dns_servers):
    """
    Remediates the "BMC DNS: FAILED ... PropertyUnknown ... StaticNameServers"
    error by trying every known request shape for setting static DNS
    servers, oldest-fleet-compatible fallback included, stopping at the
    first one that succeeds:
      1. Top-level StaticNameServers on EthernetInterface (current spec).
      2. Oem.OpenBMC.DNSServers on EthernetInterface (older firmware).
      3. IPv4StaticAddresses[].NameServers on EthernetInterface (legacy).
      4. Managers/bmc/NetworkProtocol DNS block (oldest fallback).

    `dns_servers` must be a list of IPv4/IPv6 address strings, e.g.
    ["10.1.1.1", "10.1.1.2"]. Returns (label, result) for whichever
    variant succeeded; raises RuntimeError listing every attempt if all
    fail.
    """
    if not isinstance(dns_servers, list) or not all(isinstance(s, str) for s in dns_servers):
        raise ValueError("dns_servers must be a list of IP address strings")

    return try_variants([
        ("new_top_level_static_name_servers", lambda: _patch_static_name_servers_top_level(bmc, interface_id, dns_servers)),
        ("old_oem_dns_servers", lambda: _patch_static_name_servers_oem(bmc, interface_id, dns_servers)),
        ("legacy_ipv4_static_addresses", lambda: _patch_static_name_servers_ipv4(bmc, interface_id, dns_servers)),
        ("oldest_network_protocol_dns", lambda: _patch_static_name_servers_network_protocol(bmc, dns_servers)),
    ])


def disable_dhcp_dns_override(bmc, interface_id):
    """
    PATCH /redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id} with
    {"DHCPv4": {"UseDNSServers": false}} so DHCP-supplied DNS servers do
    not silently override the StaticNameServers just configured. Call
    this alongside fix_bmc_dns() when the interface has DHCPv4 enabled.
    Non-fatal if it errors (older firmware may not expose this knob) —
    callers should not treat its failure as blocking.
    """
    return bmc.patch(
        f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}",
        {"DHCPv4": {"UseDNSServers": False}},
    )


def verify_dns_applied(bmc, interface_id, expected_servers):
    """
    GET /redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id} and
    confirm the NameServers (effective, read-only) list matches
    `expected_servers`, to validate the DNS fix actually took effect
    rather than trusting the PATCH response alone.
    """
    interface = bmc.get(f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}")
    current = interface.get("NameServers", [])
    return {
        "current_name_servers": current,
        "static_name_servers": interface.get("StaticNameServers", []),
        "matches_expected": sorted(current) == sorted(expected_servers),
    }


def resolve_and_fix_bmc_dns(bmc, dns_servers, interface_id=None):
    """
    End-to-end remediation for the BMC DNS failure, covering both old and
    new Intel BMC generations:
      1. If `interface_id` is not given, auto-discover interfaces (with
         eth0/eth1 fallback via list_bmc_ethernet_interfaces()).
      2. Best-effort disable DHCP DNS override if DHCPv4 looks enabled.
      3. Try every known StaticNameServers request shape via fix_bmc_dns()
         until one succeeds.
      4. Verify the change actually took effect.
    Returns a dict with the interface used, which variant succeeded, the
    patch result, and the verification result.
    """
    if interface_id is None:
        interfaces = list_bmc_ethernet_interfaces(bmc)
        if not interfaces:
            raise RuntimeError("No EthernetInterfaces found on /redfish/v1/Managers/bmc")
        interface_id = interfaces[0].get("Id") or interfaces[0]["@odata.id"].rsplit("/", 1)[-1]

    try:
        interface = bmc.get(f"/redfish/v1/Managers/bmc/EthernetInterfaces/{interface_id}")
        if interface.get("DHCPv4", {}).get("DHCPEnabled"):
            disable_dhcp_dns_override(bmc, interface_id)
    except Exception:
        pass

    variant_label, patch_result = fix_bmc_dns(bmc, interface_id, dns_servers)
    verification = verify_dns_applied(bmc, interface_id, dns_servers)
    return {
        "interface_id": interface_id,
        "variant_used": variant_label,
        "patch_result": patch_result,
        "verification": verification,
    }


if __name__ == "__main__":
    import os

    host = os.environ.get("BMC_HOST")
    user = os.environ.get("BMC_USER", "root")
    password = os.environ.get("BMC_PASSWORD")
    dns_servers = os.environ.get("BMC_DNS_SERVERS", "").split(",")

    if not host or not password or not dns_servers[0]:
        raise SystemExit("Set BMC_HOST, BMC_USER, BMC_PASSWORD, BMC_DNS_SERVERS env vars first")

    with RedfishClient(host, user, password) as bmc:
        result = resolve_and_fix_bmc_dns(bmc, dns_servers)
        print(result)
