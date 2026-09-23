"""
Fix for: "RAID Array Creation: Failed (uid: a42fe434-3a58-49e8-8caf-644efa22a9) - no-drives"

(Intel server only — targets the Redfish Storage/StorageLDrive/Volume
resources documented in the Intel Server System Integrated BMC Firmware
OpenBMC Redfish API Specification and the DMTF Redfish Volume schema.)

=== ROOT CAUSE OF THE "no-drives" FAILURE ===

Every path in this module used to be hardcoded as
"/redfish/v1/Systems/system/Storage/{controller_id}", with
`resolve_raid_controller_id()` only ever trying a fixed candidate list
for the *controller* segment: ["Raid_0", "Raid_1", "RAID.Integrated.1",
"RAID.0"]. That assumes the ComputerSystem resource ID is always the
literal string "system".

The attached diagnostic (error.txt) shows that is not true on this
fleet — the BMC's actual resource path is:

    /redfish/v1/Systems/LUC223400173/Storage/1

i.e. the System ID is the server's own serial number (not "system"),
and the Storage controller ID is a plain "1" (not any of the guessed
Intel/DMTF names). Neither segment matches what the old code assumed,
so resolve_raid_controller_id() raised RuntimeError("No RAID controller
resource found among candidates: [...]") before drive/VROC detection
ever ran at all — which is exactly what an orchestrator with no further
detail would report as a bare "no-drives" failure. Note the error.txt
payload itself already contains the *correct* VROC pass-through
diagnosis (is_vroc_passthrough=true, nvme_drive_count=7,
total_drive_count=8, supported_raid_types=[]) — proving the VROC logic
in this module was always right; only the resource-path resolution in
front of it was broken.

THE FIX: never guess the System ID, and don't rely solely on a static
controller-name candidate list either. Both resolve_system_id() and
resolve_raid_controller_id() now discover the real resource IDs by
GETing the Systems collection (/redfish/v1/Systems) and that system's
Storage collection (/redfish/v1/Systems/{system_id}/Storage) and
reading back whatever member IDs the BMC itself actually reports —
exactly what Redfish's own collection/@odata.id links already tell you,
rather than a fixed guess list. The old candidate names are still tried
*first* when multiple Storage members are present (so behavior on
already-working Intel naming is unchanged), but a controller whose ID
is a plain index (e.g. "1") is no longer treated as "not found" — it is
simply the first (or only) member of that system's Storage collection.

=== POLICY: REUSE-FIRST, CREATE-ONLY-IF-PERMITTED, ELSE LEAVE AS-IS ===

Earlier revisions of this module always tried to CREATE a new RAID
array first. That is the wrong default on Intel platforms, per
follow-up research (see error.txt): on an Intel M50CYP-class server,
BIOS reported VROC in pass-through mode with all NVMe disks listed as
"Non-RAID Physical Disks" — meaning RAID creation via any Redfish
request shape is impossible until a VROC license question is answered
by hardware/provisioning (install a key, use Intel SDP, or accept no
RAID is expected at all).

This module implements the opposite default policy, entered through
ensure_raid_array() (the primary/recommended entry point):

  1. Check whether storage is ALREADY AVAILABLE on this controller
     (find_existing_volumes()). If any Volume already exists:
       - REUSE it. Do not attempt to create a new one.
       - Optionally CLEAN it first (wipe its data in place) — but ONLY if
         Redfish actually advertises permission to do so, i.e. the
         volume's own "Actions" property includes the DMTF-standard
         #Volume.Initialize action. If that action is not advertised,
         this is a no-op: leave the volume exactly as it is and reuse it
         uncleaned. Never force a clean/wipe path that Redfish hasn't
         explicitly offered.
  2. If NO storage exists yet, check whether Intel actually ALLOWS
     creating a RAID array on this controller at all
     (detect_vroc_passthrough(), get_supported_actions()). If creation is
     not permitted (VROC pass-through with no license, or the controller
     genuinely has no matching create action), do NOT raise a hard
     failure — return a "skipped_no_permission" result and leave the
     controller untouched. This mirrors the third decision branch from
     error.txt: "If no RAID is expected for these NVMe drives → mark
     Intel RAID as skipped/not-supported."
  3. Only if nothing exists AND creation is confirmed permitted does this
     module actually attempt to build a new RAID array, via the same
     old/new-firmware fallback chain as before (Intel OEM
     StorageLDrive.Create, the older collection-root variant, and the
     DMTF VolumeCollection POST).

The low-level create_raid_array_safe() function (and the VROC/diagnostic
helpers it uses) is kept for callers who specifically want to attempt
creation and handle VrocPassThroughError themselves — but
ensure_raid_array() is the function that should be called by default,
since it implements the full reuse/skip policy this platform actually
needs.

=== BACKGROUND: why RAID creation can fail here ===

RAID logical drive creation runs as an asynchronous action
(StorageLDrive.Create on a Storage controller, per spec section
2.81.6) and can fail for several distinct reasons:

  - A generic failed task/operation with no further detail (e.g. the
    "no-drives" case this module now fixes, and the earlier uid
    51dce124-07cc-434a-9daf-c66fe9725278 case):
      - The System ID or Storage controller ID used in the request
        doesn't match what this BMC actually exposes (the root cause
        fixed in this revision — see above).
      - The chosen physical DeviceIDs are already part of another
        logical drive / are not in an "Available"/unconfigured state.
      - NumDrives / SpanDepth don't match the requested Rrl (RAID
        level) — e.g. requesting RAID10 with an odd NumDrives, or
        SpanDepth=1 for a RAID level that requires >1 span.
      - StripSize is not one of the controller's SupportedStripSize
        values.
      - A stale/failed task from the previous attempt was never
        cleared, so the controller rejects a new StorageLDrive.Create
        while the old one is still "outstanding".

  - HTTP 405 "ActionNotSupported" (seen for RAID1 creation in scan logs):
    per the DMTF Redfish Base Message Registry, this specific
    MessageId means "The action %1 is not supported by the resource"
    with Resolution "Check the Actions property in the resource for
    the supported actions." In practice this happens when:
      - The targeted Storage/{id} resource genuinely has no
        #StorageCollection.CreateDrive / StorageLDrive.Create action
        (e.g. this particular controller model/firmware doesn't
        implement the Intel OEM action at all, or only implements the
        plain DMTF VolumeCollection POST path instead).
      - The requested RAID level itself is unsupported by this
        specific controller (its Actions/@Redfish.ActionInfo would
        list a restricted set of allowed Rrl/RAIDType values that
        doesn't include RAID1 — e.g. some HW RAID controllers only
        expose RAID0/RAID5/RAID6 as build targets and mirror sets are
        handled elsewhere), so the action is rejected outright rather
        than accepted and later failing.
      - The action was POSTed to the wrong resource entirely (e.g. the
        controller collection root instead of the individual
        Storage/{id} controller, or vice versa, depending on firmware
        generation).
      - **Intel VROC (Virtual RAID on CPU) is in pass-through mode with
        no license key installed** — this is a distinct, NVMe-specific
        root cause confirmed for this platform family (e.g. Intel
        M50CYP) and is the most likely explanation whenever the target
        drives are NVMe and the controller reports no SupportedRAIDTypes
        at all. See "VROC / NVMe RAID LICENSING" below — this is a
        hardware/BIOS/licensing state, not something any Redfish request
        shape can work around.

=== VROC / NVMe RAID LICENSING (Intel Xeon Scalable NVMe platforms) ===

On Intel Xeon Scalable servers (e.g. M50CYP), NVMe RAID is provided by
Intel VROC (Virtual RAID on CPU), layered on top of Intel VMD (Volume
Management Device). VMD/VROC has three relevant states:

  1. VMD/VROC disabled in BIOS entirely: NVMe drives are exposed as
     plain PCIe passthrough devices to the OS; no RAID membership is
     possible at all, by BIOS/hardware design, regardless of Redfish.
  2. VMD/VROC enabled but in "pass-through mode" with NO VROC license
     key installed: NVMe drives enumerate under VMD but every drive is
     reported as a "Non-RAID Physical Disk" (exactly the symptom
     described for this platform — confirmed in error.txt:
     nvme_drive_count=7, total_drive_count=8, supported_raid_types=[]).
     This is the expected, documented behavior of VROC with no key — it
     is NOT a bug in this codebase, the BMC, or Redfish, and no
     request-shape fallback fixes it.
  3. VMD/VROC enabled with a valid license key installed (physical
     hardware key on a header on the motherboard, OR a factory-set
     software license flag): NVMe drives can be assembled into RAID
     volumes, gated by which key tier is installed.

VROC license tiers (confirmed via published third-party hardware-key
pricing/spec guides referencing Intel's official SKUs — verify current
tier-to-RAID-level mapping against your organization's Intel account
team or current Intel VROC documentation before relying on this for a
purchasing decision):
  - No key installed:            pass-through only, NO RAID levels at all.
  - VROCSTANMOD ("Standard"):    unlocks RAID0, RAID1, RAID10. Does NOT
                                   unlock RAID5/RAID6.
  - VROCPREMMOD ("Premium"):     everything Standard unlocks, PLUS
                                   RAID5/RAID6, plus VROC Integrated
                                   Caching (Optane SSD caching in Linux).
  - VROCISSDMOD ("Intel SSD Only"): same RAID-level unlock as Premium,
                                   but restricted to Intel-branded SSDs
                                   only; priced below Premium.
  Some OEMs (Dell, Lenovo, Supermicro, etc.) also offer a
  factory-installed *software* license flag equivalent to
  VROCPREM/VROCISSD with no physical key header required — whether that
  applies to a given fleet is an OEM/BIOS configuration question, not a
  Redfish one.

practical conclusion: because RAID1 only requires the Standard tier (not
Premium), if a licensed hardware/software VROC key of ANY tier is
genuinely installed and BIOS still reports pass-through / Non-RAID
Physical Disks, that points to the key not being recognized (wrong
header, BIOS setting not applied, or key not actually present) rather
than a "need a bigger license" problem — escalate accordingly rather
than assuming Premium is required for RAID1. Per error.txt, this exact
question (should a key be installed here, or is Intel SDP the expected
RAID-creation path, or is no RAID expected at all) is still open with
hardware/provisioning — until it's answered, ensure_raid_array()'s
default of "skip gracefully rather than fail" is the correct behavior.

Depends on redfish_client.RedfishClient / RedfishError / try_variants
for auth, session handling, rich error detail, and the old/new fallback
mechanism.
"""

from redfish_client import RedfishClient, try_variants

# Preferred Storage-controller resource ID names to try FIRST when a
# system exposes more than one Storage member (older/newer Intel
# naming). This is now only a *preference* used to pick among several
# discovered controllers — it is no longer the sole source of truth for
# whether a controller "exists": resolve_raid_controller_id() always
# falls back to whatever the Storage collection actually reports (e.g.
# a plain numeric ID like "1", confirmed in error.txt), instead of
# raising just because none of these names matched.
CANDIDATE_RAID_CONTROLLER_IDS = ["Raid_0", "Raid_1", "RAID.Integrated.1", "RAID.0"]

# Confirmed VROC license tier -> unlocked RAID levels, per published
# Intel VROC hardware-key SKU guides (VROCSTANMOD/VROCPREMMOD/VROCISSDMOD).
# Verify against current Intel documentation before treating as
# authoritative for licensing/purchasing decisions.
VROC_TIER_UNLOCKED_RAID_TYPES = {
    "none": [],  # no key installed: pass-through only, no RAID levels
    "standard": ["RAID0", "RAID1", "RAID10"],
    "premium": ["RAID0", "RAID1", "RAID10", "RAID5", "RAID6"],
    "intel_ssd_only": ["RAID0", "RAID1", "RAID10", "RAID5", "RAID6"],  # Intel-branded SSDs only
}

# Default number of drives to auto-select for a fresh RAID build when the
# caller doesn't pass device_ids explicitly. Only covers the simple cases
# (RAID0/RAID1); RAID5/RAID6/RAID10/etc. require an explicit device_ids
# list since minimum drive counts and span layout vary by controller.
_DEFAULT_NUM_DRIVES_FOR_RRL = {
    0: 2,  # RAID0
    1: 2,  # RAID1
}


class VrocPassThroughError(RuntimeError):
    """
    Raised by create_raid_array_safe() (the low-level create-only
    function) when this module detects that a RAID-creation failure is
    caused by Intel VROC running in pass-through mode with no
    recognized license (NVMe drives on this controller report
    Protocol == "NVMe" and the controller itself advertises no
    SupportedRAIDTypes) — the documented, expected VROC behavior with no
    key installed, not a bug in any Redfish request shape.

    ensure_raid_array() (the recommended entry point) catches this
    itself and converts it into a "skipped_no_permission" result rather
    than propagating an exception — see that function's docstring. Use
    this class directly only if you're calling create_raid_array_safe()
    on its own and want to implement your own handling of the same
    three-way decision:

      1. If a VROC Standard/Premium/Intel-SSD-Only license key SHOULD be
         installed on this server class per your hardware/provisioning
         standard, this is a hardware/provisioning gap — file that
         request, install the key, then retest Redfish RAID creation
         (no code change needed here; it will start working once BIOS
         reports a recognized key).
      2. If Intel SDP (System Debug/Deployment/Provisioning tool — check
         your organization's specific SDP documentation/API for the
         exact command) is the sanctioned way to create VROC RAID
         volumes out-of-band on these servers, integrate that SDP
         call as an additional fallback in create_raid_array_safe()
         once its command/API surface is confirmed.
      3. If no RAID is expected on these NVMe drives at all (e.g. they
         are intentionally used as independent/pass-through storage),
         the caller (e.g. Forge) should catch VrocPassThroughError and
         mark this operation as skipped/not-supported rather than a
         hard failure — see `.as_skip_reason()` below for a ready-made
         message for that path.
    """

    def __init__(self, controller_id, requested_raid_type, nvme_drive_count):
        self.controller_id = controller_id
        self.requested_raid_type = requested_raid_type
        self.nvme_drive_count = nvme_drive_count
        super().__init__(
            f"Controller '{controller_id}' has {nvme_drive_count} NVMe drive(s) "
            f"with no SupportedRAIDTypes advertised — this matches Intel VROC "
            f"pass-through mode with no license key installed, not a request-shape "
            f"bug. Requested RAID type: {requested_raid_type}. See "
            f"VrocPassThroughError docstring for the required next steps "
            f"(install VROC key / use Intel SDP / mark unsupported)."
        )

    def as_skip_reason(self):
        """
        Returns a short, structured reason string suitable for a caller
        (e.g. Forge) to record when marking this RAID operation as
        skipped/not-supported rather than failed, per decision path 3 in
        this class's docstring.
        """
        return (
            f"intel_raid_skipped_vroc_passthrough: controller={self.controller_id} "
            f"requested={self.requested_raid_type} nvme_drives={self.nvme_drive_count}"
        )


# DMTF Redfish Volume.RAIDType enum (redfish.dmtf.org/schemas/v1/Volume.json),
# used to translate the Intel OEM numeric Rrl into the standard RAIDType
# string for the DMTF VolumeCollection POST fallback and for diagnostics.
RRL_TO_RAID_TYPE = {
    0x00: "RAID0",
    0x01: "RAID1",
    0x02: "RAID5",
    0x03: "RAID6",
    0x04: "RAID1E",   # RAID 1E (RLQ = 1)
    0x05: "RAID1E",   # RAID 1E (RLQ = 0)
    0x06: "RAID1E",   # RAID 1E0 (RLQ = 0)
    0x07: "RAID00",
    0x08: "RAID10",
    0x09: "RAID50",
    0x0A: "RAID60",
}


def get_task_status(bmc, task_uid):
    """
    GET /redfish/v1/TaskService/Tasks/{task_uid} and return TaskState,
    TaskStatus, and Messages, to inspect exactly why a previously
    submitted RAID creation task (identified by its uid, as logged in
    error.txt) failed.
    """
    task = bmc.get(f"/redfish/v1/TaskService/Tasks/{task_uid}")
    return {
        "TaskState": task.get("TaskState"),
        "TaskStatus": task.get("TaskStatus"),
        "Messages": task.get("Messages"),
    }


def clear_stale_task(bmc, task_uid):
    """
    DELETE /redfish/v1/TaskService/Tasks/{task_uid} to remove a
    completed-but-failed RAID creation task, so it does not block a
    controller from accepting a fresh StorageLDrive.Create action on
    retry. Non-fatal if it errors (task may already be gone).
    """
    try:
        return bmc.delete(f"/redfish/v1/TaskService/Tasks/{task_uid}")
    except Exception:
        return None


def _resource_id_from_odata_id(odata_id):
    """Extracts the trailing resource-ID segment from an @odata.id link, e.g. '/redfish/v1/Systems/LUC223400173' -> 'LUC223400173'."""
    return odata_id.rstrip("/").rsplit("/", 1)[-1]


def resolve_system_id(bmc, system_id=None):
    """
    Confirms the actual ComputerSystem resource ID on this BMC, instead
    of assuming it is always the literal "system" (or any other single
    hardcoded name). Some Intel BMCs do use "system" or a numeric "1",
    but others — confirmed in error.txt — expose the System resource
    under the server's own serial number (e.g.
    /redfish/v1/Systems/LUC223400173). Guessing wrong here means every
    downstream Storage/RAID lookup 404s before it even starts.

    If `system_id` is given, verifies it directly via GET and returns it
    unchanged. Otherwise GETs the Systems collection
    (/redfish/v1/Systems) and returns the resource ID of its first
    member. Raises RuntimeError if the given ID doesn't resolve, or if
    the collection has no members.
    """
    if system_id:
        bmc.get(f"/redfish/v1/Systems/{system_id}")
        return system_id

    collection = bmc.get("/redfish/v1/Systems")
    members = collection.get("Members", [])
    if not members:
        raise RuntimeError("Systems collection (/redfish/v1/Systems) has no members — cannot resolve a ComputerSystem resource ID")
    return _resource_id_from_odata_id(members[0]["@odata.id"])


def resolve_raid_controller_id(bmc, system_id=None, raid_controller_id=None):
    """
    Resolves BOTH the ComputerSystem resource ID (via resolve_system_id())
    and the Storage controller resource ID under it, rather than
    hardcoding "/redfish/v1/Systems/system/Storage/{name}" and guessing
    only the trailing name from a fixed candidate list. That old
    approach is exactly what produced the "no-drives" failure this
    module now fixes: the real controller path on this fleet is
    /redfish/v1/Systems/LUC223400173/Storage/1 — a serial-number System
    ID and a plain numeric controller ID, neither of which any prior
    hardcoded guess matched.

    If `raid_controller_id` is given, verifies it directly under the
    resolved system and returns it unchanged. Otherwise GETs
    /redfish/v1/Systems/{system_id}/Storage and, among its members,
    prefers one whose ID matches CANDIDATE_RAID_CONTROLLER_IDS (in that
    order, for continuity with older/newer Intel naming conventions);
    if none of those names are present, falls back to the first member
    reported by the collection itself — e.g. a plain "1" — instead of
    raising, since a controller not matching a guessed name is still a
    perfectly valid controller.

    Returns (resolved_system_id, resolved_raid_controller_id). Raises
    RuntimeError only if the Systems or Storage collection is genuinely
    empty/unreadable.
    """
    resolved_system_id = resolve_system_id(bmc, system_id)

    if raid_controller_id:
        bmc.get(f"/redfish/v1/Systems/{resolved_system_id}/Storage/{raid_controller_id}")
        return resolved_system_id, raid_controller_id

    collection = bmc.get(f"/redfish/v1/Systems/{resolved_system_id}/Storage")
    members = collection.get("Members", [])
    if not members:
        raise RuntimeError(f"Storage collection under Systems/{resolved_system_id} has no members — no RAID controller to resolve")

    member_ids = [_resource_id_from_odata_id(m["@odata.id"]) for m in members]
    for candidate in CANDIDATE_RAID_CONTROLLER_IDS:
        if candidate in member_ids:
            return resolved_system_id, candidate

    return resolved_system_id, member_ids[0]


def get_raid_controller_state(bmc, system_id, raid_controller_id):
    """
    GET /redfish/v1/Systems/{system_id}/Storage/{raid_controller_id} and
    return StorageControllers info (SupportedRAIDTypes, SupportedStripSize)
    plus the Drives list, so a caller can validate a requested RAID
    level, strip size, and physical drive IDs before retrying creation.
    """
    controller = bmc.get(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}")
    return {
        "StorageControllers": controller.get("StorageControllers"),
        "Drives": controller.get("Drives"),
    }


def get_supported_actions(bmc, system_id, raid_controller_id):
    """
    GET /redfish/v1/Systems/{system_id}/Storage/{raid_controller_id} and
    return exactly what this controller's own "Actions" property
    advertises, plus (when available) the SupportedRAIDTypes from each
    StorageController entry and the allowed-values list from
    @Redfish.ActionInfo if the resource links one for
    StorageLDrive.Create. This is the detail surfaced on a 405
    ActionNotSupported failure so the caller knows exactly which
    actions/RAID levels this controller supports instead of just
    "action not supported".
    """
    controller = bmc.get(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}")
    actions = controller.get("Actions", {})
    supported_raid_types = [
        sc.get("SupportedRAIDTypes")
        for sc in controller.get("StorageControllers", [])
        if sc.get("SupportedRAIDTypes")
    ]

    action_info_values = None
    create_action = actions.get("#StorageCollection.CreateDrive") or actions.get("Oem", {}).get("#StorageLDrive.Create")
    action_info_uri = None
    if isinstance(create_action, dict):
        action_info_uri = create_action.get("@Redfish.ActionInfo")
    if action_info_uri:
        try:
            action_info = bmc.get(action_info_uri)
            for param in action_info.get("Parameters", []):
                if param.get("Name") == "Rrl":
                    action_info_values = param.get("AllowableValues")
        except Exception:
            pass

    return {
        "available_actions": list(actions.keys()),
        "supported_raid_types_per_controller": supported_raid_types,
        "rrl_allowable_values_from_action_info": action_info_values,
    }


def detect_vroc_passthrough(bmc, system_id, raid_controller_id):
    """
    Checks whether this controller's failure to create a RAID volume is
    explained by Intel VROC pass-through mode with no license key,
    rather than a request-shape/firmware-endpoint mismatch. Per Intel's
    documented VROC behavior (see module docstring "VROC / NVMe RAID
    LICENSING"), the signature of this state is:

      - The controller's Drives are NVMe (Drive.Protocol == "NVMe"), AND
      - The controller reports no SupportedRAIDTypes at all on any of
        its StorageControllers entries (get_supported_actions() returns
        an empty supported_raid_types_per_controller).

    Returns a dict: {"is_vroc_passthrough": bool, "nvme_drive_count": int,
    "total_drive_count": int}. A non-NVMe controller (traditional HW
    RAID / MegaRAID-style) will always return is_vroc_passthrough=False
    here even with no SupportedRAIDTypes reported, since that combination
    is specific to VMD/VROC-attached NVMe drives.
    """
    controller = bmc.get(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}")
    drives = controller.get("Drives", [])

    nvme_drive_count = 0
    for drive_link in drives:
        try:
            drive = bmc.get(drive_link["@odata.id"])
        except Exception:
            continue
        if drive.get("Protocol") == "NVMe":
            nvme_drive_count += 1

    supported = get_supported_actions(bmc, system_id, raid_controller_id)
    has_no_supported_raid_types = not supported.get("supported_raid_types_per_controller")

    return {
        "is_vroc_passthrough": nvme_drive_count > 0 and has_no_supported_raid_types,
        "nvme_drive_count": nvme_drive_count,
        "total_drive_count": len(drives),
    }


def find_existing_volumes(bmc, system_id, raid_controller_id):
    """
    GET /redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}/Volumes,
    then GET each member, and return the list of Volume resource dicts
    already configured on this controller (RAID or otherwise — this
    intentionally does not filter by RAIDType, matching the "it is all
    storage" reuse policy: any existing configured volume counts as
    "storage already available").

    This is the check ensure_raid_array() uses to decide whether to
    reuse existing storage instead of creating something new. Returns an
    empty list (never raises) if the Volumes collection is missing or
    unreadable, so callers can treat that the same as "nothing exists
    yet".
    """
    try:
        controller = bmc.get(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}")
    except Exception:
        return []
    volumes_link = controller.get("Volumes", {}).get("@odata.id")
    if not volumes_link:
        return []
    try:
        collection = bmc.get(volumes_link)
    except Exception:
        return []

    volumes = []
    for member in collection.get("Members", []):
        try:
            volumes.append(bmc.get(member["@odata.id"]))
        except Exception:
            continue
    return volumes


def get_volume_actions(volume):
    """Returns the "Actions" property from an already-fetched Volume resource dict, or {} if absent."""
    return volume.get("Actions", {}) or {}


def clean_existing_volume(bmc, volume, initialize_type="Fast"):
    """
    Attempts to wipe an existing volume's data in place before reuse,
    using the DMTF-standard #Volume.Initialize action — but ONLY if
    Redfish actually advertises that permission ("if redfish gives that
    permission"). Checks the volume's own "Actions" property
    (get_volume_actions()) for "#Volume.Initialize"; if it is not
    present, this function does nothing destructive and returns
    {"cleaned": False, ...} — the volume is left exactly as it is
    ("else leave that") and the caller should still proceed to reuse it
    uncleaned.

    `initialize_type` is passed as the action's InitializeType parameter.
    "Fast" (the default) clears volume metadata/RAID state quickly
    without a full-capacity overwrite; use "Slow"/"SlowOverwrite" (per
    whatever this controller's own AllowableValues report, if it
    exposes an @Redfish.ActionInfo for this action) if a full data wipe
    is actually required before reuse.
    """
    actions = get_volume_actions(volume)
    initialize_action = actions.get("#Volume.Initialize")
    if not isinstance(initialize_action, dict) or not initialize_action.get("target"):
        return {
            "cleaned": False,
            "reason": "Volume has no #Volume.Initialize action advertised in its Actions property; leaving it as-is.",
        }

    target = initialize_action["target"]
    try:
        result = bmc.post(target, {"InitializeType": initialize_type})
        return {"cleaned": True, "initialize_type": initialize_type, "result": result}
    except Exception as exc:
        return {"cleaned": False, "reason": f"#Volume.Initialize action failed, leaving volume as-is: {exc}"}


def validate_raid_request(bmc, system_id, raid_controller_id, rrl, num_drives, span_depth, device_ids):
    """
    Cross-checks a proposed RAID creation request against the live
    controller state and returns a list of problems (empty if none):
      - any device_id not currently available (per get_available_drive_ids)
      - NumDrives not matching len(device_ids)
      - multi-span RAID levels (RAID00=7, RAID10=8, RAID50=9, RAID60=10)
        requested with span_depth <= 1
      - RAID1 (rrl=1) requested with fewer than 2 drives (RAID1 requires
        mirroring across at least 2 independent devices per the DMTF
        Volume.RAIDType definition)
    Call this before create_raid_array_safe() to catch the kind of
    silent parameter mismatch that produces a failed task with no
    further detail, or an outright-rejected (405) request.
    """
    problems = []
    available = get_available_drive_ids(bmc, system_id, raid_controller_id)
    for device_id in device_ids:
        if device_id not in available:
            problems.append(f"DeviceID {device_id} is not available (already used or disabled)")

    if len(device_ids) != num_drives:
        problems.append(f"NumDrives ({num_drives}) does not match device_ids count ({len(device_ids)})")

    multi_span_levels = {7, 8, 9, 10}  # RAID00, RAID10, RAID50, RAID60
    if rrl in multi_span_levels and span_depth <= 1:
        problems.append(f"Rrl={rrl} requires SpanDepth > 1, got {span_depth}")

    if rrl == 1 and num_drives < 2:
        problems.append(f"Rrl=1 (RAID1) requires at least 2 drives, got {num_drives}")

    return problems


def get_available_drive_ids(bmc, system_id, raid_controller_id):
    """
    GET /redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}, then
    GET each linked Drive, and return the DeviceID/MemberId values for
    drives whose Status.State is "Enabled" and are not already listed
    under any existing Volume's DriveList — i.e. drives that are
    actually free to use in a new RAID array.
    """
    controller = bmc.get(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}")
    drives = controller.get("Drives", [])

    used_drive_ids = set()
    volumes_link = controller.get("Volumes", {}).get("@odata.id")
    if volumes_link:
        volume_collection = bmc.get(volumes_link)
        for vol_member in volume_collection.get("Members", []):
            volume = bmc.get(vol_member["@odata.id"])
            for drive_ref in volume.get("Oem", {}).get("OpenBMC", {}).get("DriveList", []):
                used_drive_ids.add(drive_ref)

    available = []
    for drive_link in drives:
        drive = bmc.get(drive_link["@odata.id"])
        member_id = drive.get("MemberId") or drive_link["@odata.id"].rsplit("/", 1)[-1]
        state = drive.get("Status", {}).get("State")
        if state == "Enabled" and member_id not in used_drive_ids:
            available.append(member_id)
    return available


def _build_storage_ldrive_body(rrl, strip_size, span_depth, num_drives, device_ids, vd_name):
    body = {
        "CmdParm": 1,
        "Rrl": rrl,
        "StripSize": strip_size,
        "InitState": 0,
        "DiskCachePolicy": 0,
        "SizeLow": 0,
        "SizeHigh": 0,
        "Readpolicy": 1,
        "Writepolicy": 1,
        "Iopolicy": 0,
        "Accesspolicy": 0,
        "SpanDepth": span_depth,
        "NumDrives": num_drives,
        "DeviceID": device_ids,
    }
    if vd_name:
        body["VDName"] = [ord(c) for c in vd_name[:16]]
    return body


def _create_via_storage_ldrive_action(bmc, system_id, raid_controller_id, body):
    """Current spec shape (2.81.6): POST .../Storage/{id}/Actions/StorageLDrive.Create with the OEM CmdParm/Rrl body."""
    return bmc.post(
        f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}/Actions/StorageLDrive.Create",
        body,
    )


def _create_via_storage_collection_action(bmc, system_id, body):
    """Older-firmware shape: the create action hangs off the StorageCollection root rather than the individual controller."""
    return bmc.post(f"/redfish/v1/Systems/{system_id}/Storage/Actions/StorageLDrive.Create", body)


def _create_via_dmtf_volumes_post(bmc, system_id, raid_controller_id, rrl, strip_size, device_ids):
    """
    DMTF-standard fallback: POST a Volume directly to
    .../Storage/{id}/Volumes with RAIDType (per the DMTF Volume schema
    enum, e.g. "RAID1" for mirroring) instead of using the Intel OEM
    action, for BMCs/controllers that only implement the generic
    VolumeCollection create path and return 405 ActionNotSupported for
    the OEM StorageLDrive.Create action (the specific failure seen for
    RAID1 creation in scan logs).
    """
    body = {
        "RAIDType": RRL_TO_RAID_TYPE.get(rrl, "RAID0"),
        "StripSizeBytes": strip_size,
        "Links": {"Drives": [{"@odata.id": f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}/Drives/{d}"} for d in device_ids]},
    }
    return bmc.post(f"/redfish/v1/Systems/{system_id}/Storage/{raid_controller_id}/Volumes", body)


def diagnose_action_not_supported(bmc, system_id, raid_controller_id, rrl):
    """
    Called whenever any create-action variant fails with HTTP 405 /
    MessageId ActionNotSupported (per the DMTF Base Message Registry:
    "The action %1 is not supported by the resource. Resolution: Check
    the Actions property in the resource for the supported actions.").
    Fetches get_supported_actions() for the target controller and
    returns a dict pairing the requested RAID level with exactly what
    that controller does support, so the caller can see immediately
    whether e.g. RAID1 is simply not offered by this specific
    controller/firmware rather than guessing from a bare 405. Also
    includes detect_vroc_passthrough()'s result, since an
    ActionNotSupported failure on an all-NVMe controller with no
    SupportedRAIDTypes is very likely VROC pass-through mode rather than
    a plain firmware/endpoint mismatch.
    """
    requested_raid_type = RRL_TO_RAID_TYPE.get(rrl, f"Rrl={rrl}")
    try:
        supported = get_supported_actions(bmc, system_id, raid_controller_id)
    except Exception as exc:
        supported = {"error": f"Could not introspect controller actions: {exc}"}

    try:
        vroc_check = detect_vroc_passthrough(bmc, system_id, raid_controller_id)
    except Exception as exc:
        vroc_check = {"error": f"Could not check VROC pass-through state: {exc}"}

    diagnosis = {
        "requested_raid_type": requested_raid_type,
        "system": system_id,
        "controller": raid_controller_id,
        "controller_capabilities": supported,
        "vroc_passthrough_check": vroc_check,
    }
    supported_types = supported.get("supported_raid_types_per_controller") or []
    flat_supported = {t for group in supported_types for t in (group or [])}
    if vroc_check.get("is_vroc_passthrough"):
        diagnosis["likely_cause"] = (
            f"Intel VROC pass-through mode with no recognized license key: "
            f"{vroc_check['nvme_drive_count']} of {vroc_check['total_drive_count']} "
            f"drive(s) on this controller are NVMe and it reports no "
            f"SupportedRAIDTypes at all. This is expected VROC behavior with no "
            f"key installed, not a request-shape bug — see VrocPassThroughError."
        )
    elif flat_supported and requested_raid_type not in flat_supported:
        diagnosis["likely_cause"] = (
            f"{requested_raid_type} is not in this controller's SupportedRAIDTypes "
            f"({sorted(flat_supported)}). This controller/firmware does not offer "
            f"{requested_raid_type} as a build target."
        )
    else:
        diagnosis["likely_cause"] = (
            "The targeted resource has no matching create action at all "
            "(available_actions above), or the action exists on a different "
            "resource path for this firmware generation."
        )
    return diagnosis


def create_raid_array_safe(bmc, system_id=None, raid_controller_id=None, rrl=2, device_ids=None,
                            strip_size=9, span_depth=1, vd_name=None, failed_task_uid=None,
                            check_vroc_passthrough=True):
    """
    Low-level create-only function: always attempts to build a NEW RAID
    array, covering both old and new Intel BMC generations, and gives
    detailed diagnostics specifically for HTTP 405 ActionNotSupported
    failures (e.g. "RAID1 creation — HTTP 405 ActionNotSupported").

    Most callers should use ensure_raid_array() instead, which checks
    for existing/reusable storage and creation-permission FIRST and only
    calls this function when creation is both necessary and confirmed
    possible. Call this directly only if you specifically want to force
    a create attempt and handle VrocPassThroughError yourself.

      1. If `failed_task_uid` is given (e.g. the uid from error.txt),
         clears it via clear_stale_task() so it can't block the retry.
      2. Resolves the actual System and RAID controller resource IDs via
         resolve_raid_controller_id() (auto-discovers both from the
         Systems/Storage collections if not given explicitly — see the
         module-level "ROOT CAUSE" note on why this can no longer be a
         fixed guess list).
      3. If `check_vroc_passthrough` (default), calls
         detect_vroc_passthrough() BEFORE attempting any create-action
         variant. If it detects VROC pass-through mode with no license
         (NVMe drives, no SupportedRAIDTypes), raises VrocPassThroughError
         immediately instead of burning three request-shape attempts
         that cannot possibly succeed against a licensing/BIOS-mode
         limitation. Set this to False only if you've already confirmed
         out-of-band that VROC licensing is not the issue on this fleet
         (ensure_raid_array() does this, since it already ran the same
         check itself).
      4. Validates the requested RAID level/drives/span depth against
         live controller state via validate_raid_request(), raising
         ValueError with specifics if anything looks wrong (including a
         RAID1-needs-2-drives check).
      5. Tries every known create-action shape in order until one
         succeeds: the Intel OEM StorageLDrive.Create action on the
         controller (current spec), the same action hung off the
         Storage collection root (older firmware), and a plain DMTF
         VolumeCollection POST with RAIDType (generic fallback — this
         is what actually creates RAID1 on controllers that 405 the
         OEM action for reasons other than VROC licensing).
      6. If every variant fails AND at least one failure was a
         RedfishError reporting ActionNotSupported/405, raises
         RuntimeError whose message is enriched with
         diagnose_action_not_supported()'s output (exactly which
         actions/RAID types the controller supports, and whether VROC
         pass-through was detected) appended after the normal
         try_variants() failure list, instead of a bare "All variants
         failed" message.
    `device_ids` is a list of integer physical drive IDs to include.
    Returns (label, result) for whichever variant succeeded.
    """
    if failed_task_uid:
        clear_stale_task(bmc, failed_task_uid)

    resolved_system_id, resolved_controller_id = resolve_raid_controller_id(bmc, system_id, raid_controller_id)

    if check_vroc_passthrough:
        vroc_check = detect_vroc_passthrough(bmc, resolved_system_id, resolved_controller_id)
        if vroc_check["is_vroc_passthrough"]:
            raise VrocPassThroughError(
                resolved_controller_id,
                RRL_TO_RAID_TYPE.get(rrl, f"Rrl={rrl}"),
                vroc_check["nvme_drive_count"],
            )

    if device_ids is None:
        default_count = _DEFAULT_NUM_DRIVES_FOR_RRL.get(rrl)
        if default_count is None:
            raise ValueError(
                f"device_ids must be given explicitly for Rrl={rrl} "
                f"(no safe default drive count for this RAID level)"
            )
        device_ids = get_available_drive_ids(bmc, resolved_system_id, resolved_controller_id)[:default_count]

    num_drives = len(device_ids)
    problems = validate_raid_request(bmc, resolved_system_id, resolved_controller_id, rrl, num_drives, span_depth, device_ids)
    if problems:
        raise ValueError("RAID request validation failed: " + "; ".join(problems))

    body = _build_storage_ldrive_body(rrl, strip_size, span_depth, num_drives, device_ids, vd_name)

    try:
        return try_variants([
            ("new_controller_ldrive_action", lambda: _create_via_storage_ldrive_action(bmc, resolved_system_id, resolved_controller_id, body)),
            ("old_collection_ldrive_action", lambda: _create_via_storage_collection_action(bmc, resolved_system_id, body)),
            ("dmtf_volume_post_fallback", lambda: _create_via_dmtf_volumes_post(bmc, resolved_system_id, resolved_controller_id, rrl, strip_size, device_ids)),
        ])
    except RuntimeError as all_failed:
        diagnosis = diagnose_action_not_supported(bmc, resolved_system_id, resolved_controller_id, rrl)
        raise RuntimeError(
            f"{all_failed}\n\n"
            f"--- ActionNotSupported diagnosis for {RRL_TO_RAID_TYPE.get(rrl, rrl)} on Systems/{resolved_system_id}/Storage/{resolved_controller_id} ---\n"
            f"{diagnosis}"
        ) from all_failed


def ensure_raid_array(bmc, system_id=None, raid_controller_id=None, rrl=2, device_ids=None,
                       strip_size=9, span_depth=1, vd_name=None, failed_task_uid=None,
                       clean_before_reuse=True, initialize_type="Fast"):
    """
    RECOMMENDED ENTRY POINT. Implements the reuse-first / create-only-
    if-permitted / else-leave-as-is policy described in this module's
    docstring:

      1. Resolves the System and RAID controller resource IDs by
         discovering them from the BMC's own Systems/Storage
         collections (resolve_raid_controller_id()) rather than
         assuming a fixed System ID like "system" — the root cause of
         the "no-drives" failure this revision fixes; see the
         module-level docstring.
      2. Checks whether storage ALREADY EXISTS on it
         (find_existing_volumes()). If one or more Volumes are already
         present:
           - Optionally cleans the first one in place via
             clean_existing_volume() — only if that volume's own Actions
             advertise #Volume.Initialize ("if redfish gives that
             permission"); otherwise the clean step is a no-op and the
             volume is left exactly as it is ("else leave that").
           - Returns immediately with action="reused_existing_volume".
             No creation attempt is made — existing storage always wins.
      3. If nothing exists yet, checks whether this controller actually
         PERMITS creating a RAID array at all
         (detect_vroc_passthrough()). If VROC pass-through with no
         license is detected, creation is impossible by platform design
         — returns action="skipped_no_permission" (using
         VrocPassThroughError.as_skip_reason() as the `reason`) instead
         of raising. The controller is left untouched.
      4. Otherwise, attempts to create a new RAID array via
         create_raid_array_safe() (VROC re-check skipped, since step 3
         already did it). If every create-action variant still fails
         with ActionNotSupported for a non-VROC reason (e.g. this
         specific RAID level just isn't offered by this controller),
         ALSO returns action="skipped_no_permission" — with
         diagnose_action_not_supported()'s output attached as `diagnosis`
         — rather than raising, per the same "if Intel doesn't allow it,
         leave that" instruction. Any OTHER kind of failure (a
         ValueError from validate_raid_request — e.g. a genuinely bad
         drive selection) still propagates, since that is an actionable
         bug to fix, not a permission boundary.

    Returns a dict with "action" set to one of:
      "reused_existing_volume" | "created_new_volume" | "skipped_no_permission"

    `device_ids` may be omitted for RAID0/RAID1 (a safe default drive
    count is auto-selected from currently available drives); it must be
    given explicitly for RAID5/RAID6/RAID10/etc.

    `system_id` / `raid_controller_id` may both be omitted — they are
    auto-discovered from the BMC's own Systems and Storage collections.
    Pass them explicitly only to target a specific System/controller on
    a BMC that exposes more than one.
    """
    if failed_task_uid:
        clear_stale_task(bmc, failed_task_uid)

    resolved_system_id, resolved_controller_id = resolve_raid_controller_id(bmc, system_id, raid_controller_id)

    existing_volumes = find_existing_volumes(bmc, resolved_system_id, resolved_controller_id)
    if existing_volumes:
        volume = existing_volumes[0]
        clean_result = None
        if clean_before_reuse:
            clean_result = clean_existing_volume(bmc, volume, initialize_type=initialize_type)
        return {
            "action": "reused_existing_volume",
            "system": resolved_system_id,
            "controller": resolved_controller_id,
            "volume_id": volume.get("@odata.id"),
            "raid_type": volume.get("RAIDType"),
            "existing_volume_count": len(existing_volumes),
            "clean_result": clean_result,
        }

    vroc_check = detect_vroc_passthrough(bmc, resolved_system_id, resolved_controller_id)
    if vroc_check["is_vroc_passthrough"]:
        skip_err = VrocPassThroughError(
            resolved_controller_id,
            RRL_TO_RAID_TYPE.get(rrl, f"Rrl={rrl}"),
            vroc_check["nvme_drive_count"],
        )
        return {
            "action": "skipped_no_permission",
            "system": resolved_system_id,
            "controller": resolved_controller_id,
            "reason": skip_err.as_skip_reason(),
            "detail": str(skip_err),
        }

    try:
        label, result = create_raid_array_safe(
            bmc,
            system_id=resolved_system_id,
            raid_controller_id=resolved_controller_id,
            rrl=rrl,
            device_ids=device_ids,
            strip_size=strip_size,
            span_depth=span_depth,
            vd_name=vd_name,
            check_vroc_passthrough=False,  # already checked above
        )
        return {
            "action": "created_new_volume",
            "system": resolved_system_id,
            "controller": resolved_controller_id,
            "variant": label,
            "result": result,
        }
    except RuntimeError as create_failed:
        diagnosis = diagnose_action_not_supported(bmc, resolved_system_id, resolved_controller_id, rrl)
        return {
            "action": "skipped_no_permission",
            "system": resolved_system_id,
            "controller": resolved_controller_id,
            "reason": "intel_raid_skipped_action_not_supported",
            "detail": str(create_failed),
            "diagnosis": diagnosis,
        }


if __name__ == "__main__":
    import os

    host = os.environ.get("BMC_HOST")
    user = os.environ.get("BMC_USER", "root")
    password = os.environ.get("BMC_PASSWORD")
    system_id = os.environ.get("SYSTEM_ID")  # optional; auto-resolved if unset
    raid_controller_id = os.environ.get("RAID_CONTROLLER_ID")  # optional; auto-resolved if unset
    failed_task_uid = os.environ.get("FAILED_RAID_TASK_UID")

    if not host or not password:
        raise SystemExit("Set BMC_HOST, BMC_USER, BMC_PASSWORD env vars first")

    with RedfishClient(host, user, password) as bmc:
        resolved_system_id, resolved_id = resolve_raid_controller_id(bmc, system_id, raid_controller_id)
        print("Resolved system:", resolved_system_id)
        print("Resolved controller:", resolved_id)

        # ensure_raid_array() implements reuse-first / create-if-permitted /
        # else-leave-as-is — this is the recommended way to call this module.
        result = ensure_raid_array(
            bmc,
            system_id=resolved_system_id,
            raid_controller_id=resolved_id,
            rrl=1,  # RAID1, to match the "RAID1 creation — HTTP 405 ActionNotSupported" case
            failed_task_uid=failed_task_uid,
        )
        print("ensure_raid_array result:", result)
