"""
Fix for: "BIOS / ILO Configuration: Failed — BIOS config failed"

(Intel server only — targets the Redfish Bios/Bios.Settings resources
documented in the Intel Server System Integrated BMC Firmware OpenBMC
Redfish API Specification.)

The failure was generic ("BIOS config failed") with no ExtendedInfo
captured, which usually means one of:

  - Attribute name is not present in the BIOS AttributeRegistry
  - Attribute value is not one of its allowable enum values
  - Settings were PATCHed while a prior BIOS config job was still
    pending (no reboot to apply it happened between attempts)
  - The BIOS Settings endpoint/registry moved between old and new Intel
    BMC firmware generations

This module validates every attribute/value pair against the BMC's live
AttributeRegistry before PATCHing, and tries every known
endpoint/registry-lookup shape (current spec first, then older/alternate
shapes) via try_variants() from redfish_client, so a fix that works on
the new fleet automatically falls back to whatever the old fleet
actually expects, and vice versa.

Depends on redfish_client.RedfishClient for auth/session handling.
For the related "ILO User Creation: Failed" error, see ilo_user_fix.py.
"""

from redfish_client import RedfishClient, try_variants


def _get_registry_via_bios_link(bmc):
    """Current spec shape: /Systems/system/Bios.AttributeRegistry -> /Registries/{name} -> Location[0].Uri."""
    bios = bmc.get("/redfish/v1/Systems/system/Bios")
    registry_name = bios.get("AttributeRegistry")
    if not registry_name:
        raise RuntimeError("Bios resource has no AttributeRegistry property")
    registry = bmc.get(f"/redfish/v1/Registries/{registry_name}")
    location = (registry.get("Location") or [{}])[0]
    registry_uri = location.get("Uri")
    if not registry_uri:
        raise RuntimeError(f"Registry '{registry_name}' has no Location Uri")
    return bmc.get(registry_uri)


def _get_registry_via_settings_link(bmc):
    """Older-firmware shape: AttributeRegistry lives on Bios/Settings rather than Bios itself."""
    settings = bmc.get("/redfish/v1/Systems/system/Bios/Settings")
    registry_name = settings.get("AttributeRegistry")
    if not registry_name:
        raise RuntimeError("Bios/Settings resource has no AttributeRegistry property")
    registry = bmc.get(f"/redfish/v1/Registries/{registry_name}")
    location = (registry.get("Location") or [{}])[0]
    registry_uri = location.get("Uri")
    if not registry_uri:
        raise RuntimeError(f"Registry '{registry_name}' has no Location Uri")
    return bmc.get(registry_uri)


def _get_registry_direct_biosattributeregistry(bmc):
    """Legacy fallback: some early Intel builds expose the registry directly at a fixed well-known URI."""
    return bmc.get("/redfish/v1/Registries/BiosAttributeRegistry.v1_0_0")


def get_bios_attribute_registry(bmc):
    """
    Fetches the full BIOS attribute registry (attribute name -> allowable
    values/type), trying every known lookup shape in order: via the Bios
    resource's AttributeRegistry link (current spec), via Bios/Settings'
    AttributeRegistry link (older firmware), or a fixed legacy URI
    (oldest fallback). Returns {} if every attempt fails, since callers
    treat an empty registry as "cannot validate, but don't block" rather
    than a hard error.
    """
    try:
        _, registry_doc = try_variants([
            ("new_bios_link", lambda: _get_registry_via_bios_link(bmc)),
            ("old_settings_link", lambda: _get_registry_via_settings_link(bmc)),
            ("legacy_fixed_uri", lambda: _get_registry_direct_biosattributeregistry(bmc)),
        ])
    except RuntimeError:
        return {}

    attributes = {}
    for attr in registry_doc.get("RegistryEntries", {}).get("Attributes", []):
        attributes[attr.get("AttributeName")] = attr
    return attributes


def validate_bios_attributes(bmc, attributes):
    """
    Checks each key in `attributes` against the BIOS AttributeRegistry
    (via get_bios_attribute_registry()) and returns a list of problems —
    unknown attribute names, or values outside the registry's declared
    Value/enum list — without sending any PATCH. Use this to find out
    exactly why "BIOS config failed" before retrying. If the registry
    could not be fetched at all (empty dict), returns no problems so
    validation doesn't block older firmware that hides its registry.
    """
    registry = get_bios_attribute_registry(bmc)
    if not registry:
        return []
    problems = []
    for name, value in attributes.items():
        entry = registry.get(name)
        if entry is None:
            problems.append(f"Unknown BIOS attribute: {name}")
            continue
        allowed = [v.get("ValueName") for v in entry.get("Value", [])] if entry.get("Value") else None
        if allowed and value not in allowed:
            problems.append(f"'{value}' is not a valid value for {name}; allowed: {allowed}")
    return problems


def _patch_bios_settings_current(bmc, attributes):
    """Current spec shape: PATCH /Systems/system/Bios/Settings with {"Attributes": {...}}."""
    return bmc.patch("/redfish/v1/Systems/system/Bios/Settings", {"Attributes": attributes})


def _patch_bios_direct(bmc, attributes):
    """Older-firmware shape: some early builds accept PATCH directly on /Systems/system/Bios."""
    return bmc.patch("/redfish/v1/Systems/system/Bios", {"Attributes": attributes})


def _patch_bios_settings_via_settings_object(bmc, attributes):
    """Alternate shape: PATCH the @Redfish.Settings SettingsObject link discovered from Bios, rather than a hardcoded path."""
    bios = bmc.get("/redfish/v1/Systems/system/Bios")
    settings_uri = bios.get("@Redfish.Settings", {}).get("SettingsObject", {}).get("@odata.id")
    if not settings_uri:
        raise RuntimeError("Bios resource has no @Redfish.Settings SettingsObject link")
    return bmc.patch(settings_uri, {"Attributes": attributes})


def fix_bios_configuration(bmc, attributes):
    """
    Remediates the "BIOS / ILO Configuration: Failed — BIOS config
    failed" error. Validates every attribute/value pair against the live
    AttributeRegistry via validate_bios_attributes(); if any are invalid,
    raises ValueError listing the specific problems instead of retrying
    blindly. Otherwise tries every known PATCH endpoint shape in order —
    current spec (/Bios/Settings), the @Redfish.Settings-discovered link,
    and the older direct-/Bios fallback — stopping at the first that
    succeeds, so old and new Intel BMC generations are both covered.
    Changes are applied on next reboot per spec section 2.52.
    """
    problems = validate_bios_attributes(bmc, attributes)
    if problems:
        raise ValueError("BIOS attribute validation failed: " + "; ".join(problems))

    return try_variants([
        ("new_bios_settings_patch", lambda: _patch_bios_settings_current(bmc, attributes)),
        ("settings_object_link_patch", lambda: _patch_bios_settings_via_settings_object(bmc, attributes)),
        ("old_direct_bios_patch", lambda: _patch_bios_direct(bmc, attributes)),
    ])


if __name__ == "__main__":
    import os

    host = os.environ.get("BMC_HOST")
    user = os.environ.get("BMC_USER", "root")
    password = os.environ.get("BMC_PASSWORD")

    if not host or not password:
        raise SystemExit("Set BMC_HOST, BMC_USER, BMC_PASSWORD env vars first")

    with RedfishClient(host, user, password) as bmc:
        bios_result = fix_bios_configuration(bmc, {})
        print("BIOS configuration result:", bios_result)
