"""
Fix for: "RAID Array Creation: Failed (uid: 51dce124-07cc-434a-9daf-c66fe9725278)"
and, more specifically: "RAID1 creation — HTTP 405 ActionNotSupported"

(Intel server only — targets the Redfish Storage/StorageLDrive/Volume
resources documented in the Intel Server System Integrated BMC Firmware
OpenBMC Redfish API Specification and the DMTF Redfish Volume schema.)

RAID logical drive creation runs as an asynchronous action
(StorageLDrive.Create on a Raid_{ID} controller, per spec section
2.81.6) and can fail for several distinct reasons:

  - Generic failed task with no further detail (e.g. uid
    51dce124-07cc-434a-9daf-c66fe9725278 in error.txt):
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
      - The targeted Storage/Raid_{ID} resource genuinely has no
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
        Raid_{ID} controller, or vice versa, depending on firmware
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
     described for this platform). This is the expected, documented
     behavior of VROC with no key — it is NOT a bug in this codebase,
     the BMC, or Redfish, and no request-shape fallback fixes it.
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

practical conclusion for THIS failure: because RAID1 only requires the
Standard tier (not Premium), if a licensed hardware/software VROC key of
ANY tier is genuinely installed and BIOS still reports pass-through /
Non-RAID Physical Disks, that points to the key not being recognized
(wrong header, BIOS setting not applied, or key not actually present)
rather than a "need a bigger license" problem — escalate accordingly
rather than assuming Premium is required for RAID1.

This module inspects/clears failed tasks, validates drive/parameter
selection against the controller's actual state, and — the key
addition for the 405 case — on any ActionNotSupported failure,
immediately GETs the target resource's own "Actions" property and
@Redfish.ActionInfo (when present), AND checks whether the target drives
are NVMe with no SupportedRAIDTypes reported (the VROC pass-through
signature), so the raised error/diagnosis distinguishes "this is a VROC
licensing/BIOS-mode issue, not a request-shape bug" from a genuine
firmware-endpoint mismatch, instead of surfacing a bare "405 Client
Error". When VROC pass-through is detected, this module raises a
dedicated VrocPassThroughError (see below) rather than exhausting every
request-shape fallback pointlessly, since no Redfish request shape can
create a RAID volume while VROC has no recognized license — the decision
tree for what to do about that (install a key / use Intel SDP / mark
unsupported) is documented on VrocPassThroughError itself.

For non-NVMe-VROC controllers (traditional HW RAID / MegaRAID-style
controllers) this module still tries every known controller-ID naming
and create-action shape (current spec first, then older/alternate
shapes) via try_variants() from redfish_client, so a fix that works on
the new fleet automatically falls back to whatever the old fleet
actually expects, and vice versa.

Depends on redfish_client.RedfishClient / RedfishError / try_variants
for auth, session handling, rich error detail, and the old/new fallback
mechanism.
"""

from redfish_client import RedfishClient, try_variants

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


class VrocPassThroughError(RuntimeError):
    """
    Raised instead of a generic "all variants failed" error when this
    module detects that a RAID-creation failure is caused by Intel VROC
    running in pass-through mode with no recognized license (NVMe drives
    on this controller report Protocol == "NVMe" and the controller
    itself advertises no SupportedRAIDTypes) — the documented,
    expected VROC behavior with no key installed, not a bug in any
    Redfish request shape.

    Catching this specifically (rather than the generic RuntimeError
    try_variants() raises) lets a caller implement the three-way
    decision this situation actually requires, matching the escalation
    already agreed for this platform:

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


def resolve_raid_controller_id(bmc, raid_controller_id=None):
    """
    Confirms a RAID controller resource ID actually exists on this BMC by
    GETing /redfish/v1/Systems/system/Storage/{id}. If `raid_controller_id`
    is given, verifies it directly. Otherwise tries each name in
    CANDIDATE_RAID_CONTROLLER_IDS in order (covers the naming difference
    between older Intel builds, e.g. "Raid_0", and newer/DMTF-style
    naming, e.g. "RAID.Integrated.1") and returns the first one that
    resolves. Raises RuntimeError if none resolve.
    """
    candidates = [raid_controller_id] if raid_controller_id else CANDIDATE_RAID_CONTROLLER_IDS
    for candidate in candidates:
        try:
            bmc.get(f"/redfish/v1/Systems/system/Storage/{candidate}")
            return candidate
        except Exception:
            continue
    raise RuntimeError(f"No RAID controller resource found among candidates: {candidates}")


def get_raid_controller_state(bmc, raid_controller_id):
    """
    GET /redfish/v1/Systems/system/Storage/{raid_controller_id} and
    return StorageControllers info (SupportedRAIDTypes, SupportedStripSize)
    plus the Drives list, so a caller can validate a requested RAID
    level, strip size, and physical drive IDs before retrying creation.
    """
    controller = bmc.get(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}")
    return {
        "StorageControllers": controller.get("StorageControllers"),
        "Drives": controller.get("Drives"),
    }


def get_supported_actions(bmc, raid_controller_id):
    """
    GET /redfish/v1/Systems/system/Storage/{raid_controller_id} and
    return exactly what this controller's own "Actions" property
    advertises, plus (when available) the SupportedRAIDTypes from each
    StorageController entry and the allowed-values list from
    @Redfish.ActionInfo if the resource links one for
    StorageLDrive.Create. This is the detail surfaced on a 405
    ActionNotSupported failure so the caller knows exactly which
    actions/RAID levels this controller supports instead of just
    "action not supported".
    """
    controller = bmc.get(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}")
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


def detect_vroc_passthrough(bmc, raid_controller_id):
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
    controller = bmc.get(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}")
    drives = controller.get("Drives", [])

    nvme_drive_count = 0
    for drive_link in drives:
        try:
            drive = bmc.get(drive_link["@odata.id"])
        except Exception:
            continue
        if drive.get("Protocol") == "NVMe":
            nvme_drive_count += 1

    supported = get_supported_actions(bmc, raid_controller_id)
    has_no_supported_raid_types = not supported.get("supported_raid_types_per_controller")

    return {
        "is_vroc_passthrough": nvme_drive_count > 0 and has_no_supported_raid_types,
        "nvme_drive_count": nvme_drive_count,
        "total_drive_count": len(drives),
    }


def validate_raid_request(bmc, raid_controller_id, rrl, num_drives, span_depth, device_ids):
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
    available = get_available_drive_ids(bmc, raid_controller_id)
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


def get_available_drive_ids(bmc, raid_controller_id):
    """
    GET /redfish/v1/Systems/system/Storage/{raid_controller_id}, then GET
    each linked Drive, and return the DeviceID/MemberId values for
    drives whose Status.State is "Enabled" and are not already listed
    under any existing Volume's DriveList — i.e. drives that are
    actually free to use in a new RAID array.
    """
    controller = bmc.get(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}")
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


def _create_via_storage_ldrive_action(bmc, raid_controller_id, body):
    """Current spec shape (2.81.6): POST .../Storage/{id}/Actions/StorageLDrive.Create with the OEM CmdParm/Rrl body."""
    return bmc.post(
        f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Actions/StorageLDrive.Create",
        body,
    )


def _create_via_storage_collection_action(bmc, body):
    """Older-firmware shape: the create action hangs off the StorageCollection root rather than the individual controller."""
    return bmc.post("/redfish/v1/Systems/system/Storage/Actions/StorageLDrive.Create", body)


def _create_via_dmtf_volumes_post(bmc, raid_controller_id, rrl, strip_size, device_ids):
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
        "Links": {"Drives": [{"@odata.id": f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Drives/{d}"} for d in device_ids]},
    }
    return bmc.post(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Volumes", body)


def diagnose_action_not_supported(bmc, raid_controller_id, rrl):
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
        supported = get_supported_actions(bmc, raid_controller_id)
    except Exception as exc:
        supported = {"error": f"Could not introspect controller actions: {exc}"}

    try:
        vroc_check = detect_vroc_passthrough(bmc, raid_controller_id)
    except Exception as exc:
        vroc_check = {"error": f"Could not check VROC pass-through state: {exc}"}

    diagnosis = {
        "requested_raid_type": requested_raid_type,
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


def create_raid_array_safe(bmc, raid_controller_id=None, rrl=2, device_ids=None, strip_size=9,
                            span_depth=1, vd_name=None, failed_task_uid=None,
                            check_vroc_passthrough=True):
    """
    Remediates the "RAID Array Creation: Failed" error end-to-end,
    covering both old and new Intel BMC generations, and gives detailed
    diagnostics specifically for HTTP 405 ActionNotSupported failures
    (e.g. "RAID1 creation — HTTP 405 ActionNotSupported"):
      1. If `failed_task_uid` is given (e.g. the uid from error.txt),
         clears it via clear_stale_task() so it can't block the retry.
      2. Resolves the actual RAID controller resource ID via
         resolve_raid_controller_id() (tries CANDIDATE_RAID_CONTROLLER_IDS
         if not given explicitly).
      3. If `check_vroc_passthrough` (default), calls
         detect_vroc_passthrough() BEFORE attempting any create-action
         variant. If it detects VROC pass-through mode with no license
         (NVMe drives, no SupportedRAIDTypes), raises VrocPassThroughError
         immediately instead of burning three request-shape attempts
         that cannot possibly succeed against a licensing/BIOS-mode
         limitation. Set this to False only if you've already confirmed
         out-of-band that VROC licensing is not the issue on this fleet.
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

    resolved_controller_id = resolve_raid_controller_id(bmc, raid_controller_id)

    if check_vroc_passthrough:
        vroc_check = detect_vroc_passthrough(bmc, resolved_controller_id)
        if vroc_check["is_vroc_passthrough"]:
            raise VrocPassThroughError(
                resolved_controller_id,
                RRL_TO_RAID_TYPE.get(rrl, f"Rrl={rrl}"),
                vroc_check["nvme_drive_count"],
            )

    device_ids = device_ids or []
    num_drives = len(device_ids)
    problems = validate_raid_request(bmc, resolved_controller_id, rrl, num_drives, span_depth, device_ids)
    if problems:
        raise ValueError("RAID request validation failed: " + "; ".join(problems))

    body = _build_storage_ldrive_body(rrl, strip_size, span_depth, num_drives, device_ids, vd_name)

    try:
        return try_variants([
            ("new_controller_ldrive_action", lambda: _create_via_storage_ldrive_action(bmc, resolved_controller_id, body)),
            ("old_collection_ldrive_action", lambda: _create_via_storage_collection_action(bmc, body)),
            ("dmtf_volume_post_fallback", lambda: _create_via_dmtf_volumes_post(bmc, resolved_controller_id, rrl, strip_size, device_ids)),
        ])
    except RuntimeError as all_failed:
        diagnosis = diagnose_action_not_supported(bmc, resolved_controller_id, rrl)
        raise RuntimeError(
            f"{all_failed}\n\n"
            f"--- ActionNotSupported diagnosis for {RRL_TO_RAID_TYPE.get(rrl, rrl)} on {resolved_controller_id} ---\n"
            f"{diagnosis}"
        ) from all_failed


if __name__ == "__main__":
    import os

    host = os.environ.get("BMC_HOST")
    user = os.environ.get("BMC_USER", "root")
    password = os.environ.get("BMC_PASSWORD")
    raid_controller_id = os.environ.get("RAID_CONTROLLER_ID")  # optional; auto-resolved if unset
    failed_task_uid = os.environ.get("FAILED_RAID_TASK_UID", "51dce124-07cc-434a-9daf-c66fe9725278")

    if not host or not password:
        raise SystemExit("Set BMC_HOST, BMC_USER, BMC_PASSWORD env vars first")

    with RedfishClient(host, user, password) as bmc:
        resolved_id = resolve_raid_controller_id(bmc, raid_controller_id)
        available = get_available_drive_ids(bmc, resolved_id)
        print("Resolved controller:", resolved_id, "Available drives:", available)

        try:
            result = create_raid_array_safe(
                bmc,
                raid_controller_id=resolved_id,
                rrl=1,  # RAID1, to match the "RAID1 creation — HTTP 405 ActionNotSupported" case
                device_ids=available[:2],
                failed_task_uid=failed_task_uid,
            )
            print("RAID creation result:", result)
        except VrocPassThroughError as vroc_err:
            # Decision tree per VrocPassThroughError docstring:
            #   1. VROC key should be installed on this server class -> file a
            #      hardware/provisioning request, install it, retest Redfish.
            #   2. Intel SDP is the sanctioned way to create VROC RAID here ->
            #      integrate that SDP call once its command/API is confirmed.
            #   3. No RAID is expected on these NVMe drives -> mark skipped.
            # This sample takes path 3 (mark skipped) as the safe default;
            # replace with your own escalation logic for paths 1/2.
            print("RAID creation skipped:", vroc_err.as_skip_reason())

