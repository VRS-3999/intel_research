"""
Fix for: "RAID Array Creation: Failed (uid: 51dce124-07cc-434a-9daf-c66fe9725278)"

(Intel server only — targets the Redfish Storage/StorageLDrive
resources documented in the Intel Server System Integrated BMC Firmware
OpenBMC Redfish API Specification.)

RAID logical drive creation runs as an asynchronous action
(StorageLDrive.Create on a Raid_{ID} controller, per spec section
2.81.6) and the failure was only reported by a task uid with no further
detail. Common root causes for a failed RAID create on this platform:

  - The chosen physical DeviceIDs are already part of another logical
    drive / are not in an "Available"/unconfigured state.
  - NumDrives / SpanDepth don't match the requested Rrl (RAID level) —
    e.g. requesting RAID10 with an odd NumDrives, or SpanDepth=1 for a
    RAID level that requires >1 span.
  - StripSize is not one of the controller's SupportedStripSize values.
  - A stale/failed task from the previous attempt was never cleared,
    so the controller rejects a new StorageLDrive.Create while the old
    one is still "outstanding".
  - The RAID controller resource ID or the create-action endpoint shape
    differs between old and new Intel BMC firmware generations.

This module inspects/clears the failed task, validates drive/parameter
selection against the controller's actual state, then tries every known
controller-ID naming and create-action shape (current spec first, then
older/alternate shapes) via try_variants() from redfish_client, so a fix
that works on the new fleet automatically falls back to whatever the old
fleet actually expects, and vice versa.

Depends on redfish_client.RedfishClient for auth/session handling.
"""

from redfish_client import RedfishClient, try_variants

CANDIDATE_RAID_CONTROLLER_IDS = ["Raid_0", "Raid_1", "RAID.Integrated.1", "RAID.0"]


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


def validate_raid_request(bmc, raid_controller_id, rrl, num_drives, span_depth, device_ids):
    """
    Cross-checks a proposed RAID creation request against the live
    controller state and returns a list of problems (empty if none):
      - any device_id not currently available (per get_available_drive_ids)
      - NumDrives not matching len(device_ids)
      - multi-span RAID levels (RAID00=7, RAID10=8, RAID50=9, RAID60=10)
        requested with span_depth <= 1
    Call this before create_raid_array_safe() to catch the kind of
    silent parameter mismatch that produces a failed task with no
    further detail.
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

    return problems


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
    """DMTF-standard fallback: POST a Volume directly to .../Storage/{id}/Volumes instead of using the Intel OEM action, for BMCs that only implement the generic VolumeCollection create path."""
    rrl_to_raid_type = {
        0: "RAID0", 1: "RAID1", 2: "RAID5", 3: "RAID6",
        7: "RAID00", 8: "RAID10", 9: "RAID50", 0xA: "RAID60",
    }
    body = {
        "RAIDType": rrl_to_raid_type.get(rrl, "RAID0"),
        "StripSizeBytes": strip_size,
        "Links": {"Drives": [{"@odata.id": f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Drives/{d}"} for d in device_ids]},
    }
    return bmc.post(f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Volumes", body)


def create_raid_array_safe(bmc, raid_controller_id=None, rrl=2, device_ids=None, strip_size=9,
                            span_depth=1, vd_name=None, failed_task_uid=None):
    """
    Remediates the "RAID Array Creation: Failed" error end-to-end,
    covering both old and new Intel BMC generations:
      1. If `failed_task_uid` is given (e.g. the uid from error.txt),
         clears it via clear_stale_task() so it can't block the retry.
      2. Resolves the actual RAID controller resource ID via
         resolve_raid_controller_id() (tries CANDIDATE_RAID_CONTROLLER_IDS
         if not given explicitly).
      3. Validates the requested RAID level/drives/span depth against
         live controller state via validate_raid_request(), raising
         ValueError with specifics if anything looks wrong.
      4. Tries every known create-action shape in order until one
         succeeds: the Intel OEM StorageLDrive.Create action on the
         controller (current spec), the same action hung off the
         Storage collection root (older firmware), and a plain DMTF
         VolumeCollection POST (generic fallback).
    `device_ids` is a list of integer physical drive IDs to include.
    Returns (label, result) for whichever variant succeeded.
    """
    if failed_task_uid:
        clear_stale_task(bmc, failed_task_uid)

    resolved_controller_id = resolve_raid_controller_id(bmc, raid_controller_id)

    device_ids = device_ids or []
    num_drives = len(device_ids)
    problems = validate_raid_request(bmc, resolved_controller_id, rrl, num_drives, span_depth, device_ids)
    if problems:
        raise ValueError("RAID request validation failed: " + "; ".join(problems))

    body = _build_storage_ldrive_body(rrl, strip_size, span_depth, num_drives, device_ids, vd_name)

    return try_variants([
        ("new_controller_ldrive_action", lambda: _create_via_storage_ldrive_action(bmc, resolved_controller_id, body)),
        ("old_collection_ldrive_action", lambda: _create_via_storage_collection_action(bmc, body)),
        ("dmtf_volume_post_fallback", lambda: _create_via_dmtf_volumes_post(bmc, resolved_controller_id, rrl, strip_size, device_ids)),
    ])


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

        result = create_raid_array_safe(
            bmc,
            raid_controller_id=resolved_id,
            rrl=2,  # RAID5
            device_ids=available[:3],
            failed_task_uid=failed_task_uid,
        )
        print("RAID creation result:", result)
