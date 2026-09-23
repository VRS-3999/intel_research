INTEL RAID FIX PROMPT — Dynamic System/Storage Resolution + Reuse-First / Create-Only-If-Permitted / Else-Leave-As-Is
========================================================================================================================
(Use this prompt directly — it is self-contained and assumes NO access to Intel's
official Redfish specification PDF, Intel SDP documentation, or any other reference
beyond what is written below.)

CONTEXT / ROLE
--------------
You are fixing RAID array handling inside an existing automation codebase that
manages Intel Server System BMCs (Baseboard Management Controllers) over the
Redfish REST API. Two problems must both be fixed:

  1. THE RESOURCE-PATH BUG (this is the actual root cause of a "no-drives"
     failure — fix this first, it is likely why RAID creation is failing at all
     on some hosts).
  2. THE POLICY BUG: the current behavior always tries to CREATE a new RAID
     array first, without checking for existing storage or creation permission.
     That is the wrong default for Intel platforms and must be changed.

PROBLEM 1 — THE RESOURCE-PATH BUG (fix this first)
---------------------------------------------------
Symptom seen in production:

    RAID Array Creation: Failed (uid: a42fe434-3a58-49e8-8caf-644efa22a9) - no-drives

The attached diagnostic block for that same failure already contains the
*correct* underlying diagnosis:

    "vroc": {
      "nvme_drive_count": 7,
      "total_drive_count": 8,
      "is_vroc_passthrough": true,
      "supported_raid_types": []
    },
    "action": "skipped not supported",
    "reason": "vroc_passthrough",
    "message": "/redfish/v1/Systems/LUC223400173/Storage/1 exposes no RAID
      creation capability. NVMe drives are present and the controller
      advertises no SupportedRAIDTypes, which matches Intel VROC running in
      pass-through mode. Resolve out of band: install/enable a VROC key
      (Standard is enough for RAID1), create the array through Intel SDP, or
      confirm these drives are meant to stay pass-through. A key that is
      installed but still reports pass-through is not being recognised; a
      larger tier is not the answer.",
    "controller": "/redfish/v1/Systems/LUC223400173/Storage/1",
    "drive_protocols": { "NVMe": 7, "SATA": 1 }

Look closely at that controller path: **/redfish/v1/Systems/LUC223400173/Storage/1**.
The System resource ID here is "LUC223400173" — the server's own serial
number — NOT the literal string "system". And the Storage controller ID is a
plain "1" — not any Intel/DMTF-style name.

If the existing codebase's RAID-controller-resolution function does either of
the following, THAT is the actual bug producing the bare "no-drives" failure
(the orchestrator loses all the rich VROC diagnosis shown above and reports
only a generic failure) — because resolution throws before drive/VROC
detection ever runs:

  (a) Hardcodes the System segment as "/redfish/v1/Systems/system/..." instead
      of discovering the real System resource ID, and/or
  (b) Only tries a fixed guess-list of controller names (e.g. "Raid_0",
      "Raid_1", "RAID.Integrated.1", "RAID.0") for the Storage segment and
      raises if none of them match, instead of falling back to whatever the
      Storage collection itself reports (e.g. a plain "1").

THE FIX for Problem 1: stop guessing. Discover both resource IDs from the BMC
itself:

    GET /redfish/v1/Systems
    -> read Members[0]["@odata.id"], take its trailing path segment as the
       real System ID (e.g. "LUC223400173"). Only fall back to a hardcoded
       name if the caller has explicitly pinned one.

    GET /redfish/v1/Systems/{system_id}/Storage
    -> read every Members[i]["@odata.id"], take their trailing path segments
       as candidate Storage controller IDs. If any of them match a preferred
       name list (kept only for continuity with older/newer Intel naming —
       e.g. "Raid_0", "Raid_1", "RAID.Integrated.1", "RAID.0"), pick that one
       first; OTHERWISE just use the first member reported by the collection
       (e.g. "1") — a controller ID not matching a guessed name is still a
       perfectly valid controller and must not be treated as "not found".

Every other Storage/RAID Redfish call in the codebase must then be built from
these two DISCOVERED IDs — "/redfish/v1/Systems/{system_id}/Storage/{controller_id}/..."
— never from a hardcoded "/redfish/v1/Systems/system/Storage/{controller_id}/...".

PROBLEM 2 — THE POLICY BUG (fix this second, after Problem 1)
---------------------------------------------------------------
Intel servers may already have usable storage/RAID present, and on some Intel
platforms RAID creation via Redfish is not permitted at all (see "WHY CREATION
CAN BE BLOCKED" below). The fix must follow this exact decision order, every
time, using the resource IDs discovered in Problem 1's fix:

  1. CHECK IF STORAGE ALREADY EXISTS on the target controller.
       - If a Volume already exists -> REUSE IT. Do not create a new one.
       - Before reusing, OPTIONALLY CLEAN IT — but only if Redfish itself
         advertises permission to do so (i.e. the existing Volume's own
         "Actions" property lists the standard #Volume.Initialize action).
       - If Redfish does NOT advertise that permission, do nothing
         destructive: leave the volume exactly as it is and reuse it
         uncleaned.
  2. If NO storage exists yet, CHECK WHETHER INTEL ACTUALLY ALLOWS CREATING A
     RAID ARRAY on this controller at all (see VROC section below).
       - If creation is not permitted -> do NOT raise a hard failure.
         Return/report a "skipped — not supported on this platform" result
         and leave the controller untouched.
  3. Only if BOTH (a) nothing exists yet AND (b) creation is confirmed
     permitted, attempt to actually create a new RAID array.

In short: "it is all storage — if it is available then clean it (if permitted)
and re-use it; if not present, then make a new one, but only if Intel/Redfish
actually allows creating one; if it doesn't allow creation, check if one
already exists — if it does, no need to create, clean it once before use if
Redfish gives that permission, else leave it as it is."

WHY CREATION CAN BE BLOCKED ENTIRELY ON SOME INTEL PLATFORMS (VROC)
--------------------------------------------------------------------------
On Intel Xeon Scalable NVMe platforms (e.g. Intel M50CYP), NVMe RAID is
provided by Intel VROC (Virtual RAID on CPU), layered on Intel VMD (Volume
Management Device). Confirmed real-world case (see Problem 1's diagnostic
block above): BIOS showed VROC in "pass-through mode" and every NVMe disk was
listed as a "Non-RAID Physical Disk" (7 of 8 drives NVMe, 0 SupportedRAIDTypes).
This state has three possible underlying causes, and which one applies is a
HARDWARE/PROVISIONING QUESTION, not something any Redfish request shape can
resolve:

  A. A VROC license key (physical hardware key or factory software flag)
     SHOULD be installed on this server class but isn't -> hardware/
     provisioning team needs to install/enable it; Redfish RAID creation
     should then be retested.
  B. RAID on these drives is expected to be created via Intel SDP (System
     Debug/Deployment/Provisioning tool) or another out-of-band mechanism
     instead of Redfish -> the SDP command/API needs to be investigated and
     integrated as an additional path (not covered by this prompt — obtain
     the exact SDP command/API from your own organization's SDP
     documentation when you get there).
  C. No RAID is expected on these NVMe drives at all (they're meant to be used
     as independent/pass-through storage) -> the correct behavior is to mark
     this operation as SKIPPED / NOT SUPPORTED, not failed.

Until (A)/(B)/(C) is resolved for a given fleet, THE SAFE DEFAULT IS TO SKIP
GRACEFULLY, never to hard-fail and never to force a workaround.

VROC pass-through detection (how to tell if this is what's happening):
  - GET the Storage controller resource:
    /redfish/v1/Systems/{system_id}/Storage/{controller_id}
    (using the DISCOVERED system_id/controller_id from Problem 1's fix, not a
    hardcoded path)
  - For each linked Drive, GET it and check "Protocol" == "NVMe"
  - Check the controller's own StorageControllers[].SupportedRAIDTypes — if
    this is EMPTY/absent for an all-NVMe controller, that is the
    VROC-pass-through signature (not merely "RAID1 specifically is
    unsupported" — literally nothing is supported).
  - If both conditions hold (NVMe drives present AND no SupportedRAIDTypes at
    all), classify this as VROC pass-through with no license, and go straight
    to "skipped — not supported", per step 2 of the core policy above. Do not
    burn multiple create-action fallback attempts against a licensing/
    BIOS-mode limitation that cannot be fixed by retrying with a different
    endpoint shape.

VROC license tiers, for context only (sourced from published third-party Intel
VROC hardware-key SKU/pricing guides — verify against current Intel
documentation or your Intel account team before treating as authoritative for
a purchasing decision):

    Tier                 SKU            RAID levels unlocked
    -------------------  -------------  --------------------------------------
    No key installed     (none)         none — pass-through only
    Standard              VROCSTANMOD    RAID0, RAID1, RAID10
    Premium                VROCPREMMOD    RAID0, RAID1, RAID10, RAID5, RAID6
    Intel SSD Only         VROCISSDMOD    same as Premium, Intel-branded SSDs only

RAID1 only requires the Standard tier, not Premium. If a key of any tier is
genuinely already installed and BIOS/Redfish STILL reports pass-through /
Non-RAID Physical Disks, that points to the key not being recognized (wrong
header, BIOS setting not applied, key not actually present) — not "the
wrong/insufficient tier is installed". Surface this distinction if you end up
reporting on a VROC-blocked case.

STEP 0 — DISCOVER THE SYSTEM AND STORAGE CONTROLLER IDS (must run before
anything else, per Problem 1's fix above)
------------------------------------------------------------------------------
    GET /redfish/v1/Systems
    -> resolved_system_id = trailing segment of Members[0]["@odata.id"]
       (fall back to a caller-supplied override only if one was given)

    GET /redfish/v1/Systems/{resolved_system_id}/Storage
    -> for each Members[i]["@odata.id"], collect the trailing segment as a
       candidate controller ID
    -> prefer one matching ["Raid_0", "Raid_1", "RAID.Integrated.1", "RAID.0"]
       if present, else use the first member reported (e.g. "1")

Use resolved_system_id and resolved_controller_id for every step below —
never hardcode "/redfish/v1/Systems/system/...".

STEP 1 — CHECK FOR EXISTING STORAGE (must run before any create attempt)
------------------------------------------------------------------------------
GET the controller's Volumes collection and read every member:

    GET /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}
    GET /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/Volumes
    GET /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/Volumes/{volumeId}   // for each member

If this collection has one or more members, storage already exists. STOP — do
not attempt creation. Move to Step 1a (optional clean) then reuse it.

STEP 1a — CLEAN BEFORE REUSE, ONLY IF REDFISH PERMITS IT
------------------------------------------------------------
Inspect the existing Volume resource's own "Actions" property:

    {
      "@odata.id": "/redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/Volumes/1",
      "RAIDType": "RAID1",
      "Actions": {
        "#Volume.Initialize": {
          "target": ".../Volumes/1/Actions/Volume.Initialize",
          "@Redfish.ActionInfo": ".../Volumes/1/InitializeActionInfo"  // optional
        }
      }
    }

  - If "#Volume.Initialize" IS present under Actions, Redfish is explicitly
    granting permission to wipe/reinitialize this volume. You may POST it:

        POST .../Volumes/{volumeId}/Actions/Volume.Initialize
        Body: {"InitializeType": "Fast"}     // or "Slow"/"SlowOverwrite" if a full
                                              // wipe is genuinely required — check
                                              // any linked @Redfish.ActionInfo for
                                              // this controller's actual allowable
                                              // values before assuming "Fast" alone
                                              // is sufficient for your use case

  - If "#Volume.Initialize" is ABSENT from Actions, Redfish is NOT granting
    that permission on this resource. Do nothing destructive. Leave the
    volume exactly as it is and proceed to reuse it uncleaned. Never fall
    back to some other wipe mechanism (e.g. re-creating the logical drive)
    just because the standard action isn't offered — that would violate
    "else leave that".

  - Whether or not cleaning happened, the outcome here is REUSE: report which
    volume was reused, its RAIDType, and whether/how it was cleaned. Do not
    proceed to Step 2/3 below.

STEP 2 — IF NOTHING EXISTS, CHECK WHETHER CREATION IS EVEN PERMITTED
------------------------------------------------------------------------------
Only reached if Step 1 found zero existing Volumes. Before attempting
creation, run the VROC pass-through detection described above. Also GET the
controller's own "Actions" property (and any @Redfish.ActionInfo it links) to
see what create actions and RAID levels it actually advertises:

    GET /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}

    {
      "StorageControllers": [
        { "SupportedRAIDTypes": ["RAID0", "RAID5", "RAID6"] }   // if present but
                                                                  // RAID1 absent —
                                                                  // this level
                                                                  // specifically
                                                                  // isn't offered
      ],
      "Actions": {
        "#StorageCollection.CreateDrive": {
          "target": ".../Actions/StorageLDrive.Create",
          "@Redfish.ActionInfo": ".../CreateVolumeBasicDataActionInfo"
        }
      }
    }

If GET @Redfish.ActionInfo (when linked) confirms which Rrl/RAIDType values
are actually accepted:

    GET /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/CreateVolumeBasicDataActionInfo
    { "Parameters": [ { "Name": "Rrl", "AllowableValues": ["0", "2", "3"] } ] }

If creation is confirmed NOT permitted — either the VROC pass-through
signature is present, OR the controller's own Actions/ActionInfo genuinely
excludes the requested RAID level/has no matching create action at all — STOP.
Do not proceed to Step 3. Report this as SKIPPED / NOT SUPPORTED (not a
failure), including:
  - which check failed (VROC pass-through vs. unsupported RAID level vs. no
    matching action)
  - the controller's advertised SupportedRAIDTypes / AllowableValues, if any
  - for the VROC case specifically, the three-way guidance (A/B/C above) so a
    human can decide the next step

STEP 3 — ONLY IF PERMITTED: CREATE A NEW RAID ARRAY
------------------------------------------------------------
Reached only if Step 1 found nothing AND Step 2 confirmed creation is
permitted. Implement the SAME old/new-firmware fallback chain used elsewhere
in this codebase — try newest shape first, fall back to older shapes, stop at
first success. All paths below use the resolved_system_id and
resolved_controller_id discovered in Step 0 — never a hardcoded System ID.

  a. Attempt creation in this exact order, stopping at first success:

     i. Intel OEM action (current/new-server shape):

        POST /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/Actions/StorageLDrive.Create
        Body:
        {
          "CmdParm": 1,          // 0 = CLEAR CFG, 1 = ADD CFG
          "Rrl": 1,               // 0=RAID0,1=RAID1,2=RAID5,3=RAID6,4-6=RAID1E
                                   // variants,7=RAID00,8=RAID10,9=RAID50,0xA=RAID60
          "StripSize": 9,
          "InitState": 0,
          "DiskCachePolicy": 0,   // 0=Unchanged,1=Enabled,2=Disabled
          "SizeLow": 0,
          "SizeHigh": 0,
          "Readpolicy": 1,        // 0=No Read Ahead,1=Always Read Ahead
          "Writepolicy": 1,       // 0=Write Through,1=Always Write Back,2=WB w/ BBU
          "Iopolicy": 0,          // 0=DirectIO,1=CachedIO
          "Accesspolicy": 0,      // 0=Read-Write,1=Read Only,2=Blocked
          "SpanDepth": 1,          // >1 required for RAID00/10/50/60
          "NumDrives": 2,
          "DeviceID": [4, 5]
        }

    ii. Older-firmware shape — action hangs off the collection root instead of
        the individual controller:

        POST /redfish/v1/Systems/{resolved_system_id}/Storage/Actions/StorageLDrive.Create
        (same body as above)

   iii. DMTF-standard fallback — most likely to succeed if both (i) and (ii)
        405:

        POST /redfish/v1/Systems/{resolved_system_id}/Storage/{resolved_controller_id}/Volumes
        Body:
        {
          "RAIDType": "RAID1",
          "StripSizeBytes": 65536,
          "Links": { "Drives": [
            { "@odata.id": ".../Storage/{resolved_controller_id}/Drives/4" },
            { "@odata.id": ".../Storage/{resolved_controller_id}/Drives/5" }
          ] }
        }

        Full DMTF Volume.RAIDType enum for translating any numeric Rrl into
        the correct string: RAID0, RAID1, RAID3, RAID4, RAID5, RAID6, RAID10,
        RAID01, RAID1E, RAID50, RAID60, RAID00, RAID10E, None.

  b. If a step fails with HTTP 405 / MessageId "ActionNotSupported"
     specifically, re-run the Step 2 diagnostic (controller
     Actions/ActionInfo + VROC check) before trying the next fallback, and
     attach it to the failure record.

  c. If ALL THREE variants fail, do NOT treat this the same as Step 2's "not
     permitted" outcome if it wasn't already caught there — but DO still
     report it as skipped/not-supported rather than a hard pipeline failure,
     since by this point every avenue Redfish offers has been exhausted.
     Include every endpoint attempted, its exact failure (status +
     MessageId), and the controller's advertised capabilities.

  d. On success, verify the volume actually exists by re-GETing
     .../Storage/{resolved_controller_id}/Volumes and confirming a new member
     with the expected RAIDType — RAID creation is asynchronous, a 200/202
     response alone is not proof of completion.

VALIDATION TO PERFORM BEFORE ANY CREATE ATTEMPT (Step 3 only)
--------------------------------------------------------------------
  - Confirm every requested physical drive ID is free: GET the controller's
    Drives list, GET each Drive, proceed only if Status.State == "Enabled"
    and not already referenced by any existing Volume.
  - Confirm len(DeviceID) matches NumDrives.
  - Confirm len(DeviceID) >= 2 when Rrl == 1 (RAID1) / RAIDType == "RAID1".
  - Confirm SpanDepth > 1 for multi-span levels (RAID00, RAID10, RAID50,
    RAID60).
  - If DeviceID/device list isn't supplied by the caller, only auto-select a
    default for the simple levels (RAID0/RAID1, defaulting to the first 2
    available drives) — require an explicit drive list for
    RAID5/RAID6/RAID10/etc., since minimum counts and span layout vary by
    controller.

AUTHENTICATION (required for every request above)
------------------------------------------------------
    POST /redfish/v1/SessionService/Sessions
    Body: {"UserName": "<user>", "Password": "<password>"}

Response header "X-Auth-Token" -> attach as "X-Auth-Token" on every subsequent
request. Response header "Location" -> the session's own URI; DELETE it when
done:

    DELETE <session_uri_from_Location_header>

RETRY / FALLBACK PRINCIPLE (applies throughout)
----------------------------------------------------
This fix must work against both NEW and OLD Intel BMC firmware generations.
Every endpoint-shape decision above (System ID discovery, controller ID
discovery/naming, create-action location) must be implemented as "discover/
try the newest/current shape first, fall back to older shapes in order, stop
at first success" — never a single hardcoded call, and never a fixed
guess-list treated as exhaustive.

WHAT NOT TO DO
------------------
  - Do NOT hardcode the System resource ID as "system" (or any other single
    literal) — always discover it from GET /redfish/v1/Systems. This is the
    fix for the "no-drives" failure described in Problem 1.
  - Do NOT treat "none of my guessed controller names matched" as "no RAID
    controller exists" — fall back to whatever the Storage collection itself
    reports.
  - Do NOT attempt creation before checking for existing storage.
  - Do NOT wipe/reinitialize an existing volume unless Redfish's own Actions
    property explicitly advertises #Volume.Initialize on that resource.
  - Do NOT treat a VROC-pass-through-blocked controller, or a controller
    whose Actions/ActionInfo genuinely excludes the requested RAID level, as
    a hard failure — these are "not supported here", to be skipped and
    reported, not retried indefinitely or escalated as pipeline errors.
  - Do NOT assume Premium/Intel-SSD-Only VROC tier is required for RAID1 —
    Standard is sufficient; a still-blocked controller with a key installed
    means the key isn't being recognized, not that a bigger license is
    needed.
  - Do NOT hardcode a single controller-ID name or a single create-action
    path — always implement the old/new fallback order, on top of the
    discovered System/controller IDs.

DELIVERABLE
-----------
Implement this as a single primary entry point (e.g. "ensure_raid_array")
that:
  1. Discovers the System and RAID controller resource IDs from the BMC's own
     Systems/Storage collections (Step 0) rather than assuming a fixed name
     like "system" for the System ID — this alone fixes the "no-drives"
     failure, since it was previously caused by resolution failing outright
     before any VROC/drive check could even run.
  2. Checks for existing Volumes first; if found, optionally cleans (only if
     #Volume.Initialize is advertised) and returns a "reused existing
     storage" result — no creation attempted.
  3. If nothing exists, checks VROC pass-through + controller capabilities;
     if creation isn't permitted, returns a "skipped — not supported" result
     (never raises a hard failure for this case) with the specific reason and
     controller capability detail attached.
  4. Only if both nothing exists and creation is permitted, creates a new
     RAID array via the 3-step fallback chain, verifies it via the Volumes
     collection, and returns a "created new storage" result.
Keep a separate lower-level "create-only" function available for callers who
want to force a create attempt directly and handle the "not permitted" case
themselves, but make the reuse/skip-aware entry point the one used by default
everywhere else in the codebase.

REFERENCE IMPLEMENTATION
-------------------------
A reference implementation of everything above lives in raid_array_fix.py in
this same directory — use it as the concrete shape to replicate (or adapt
directly) on the target machine: resolve_system_id(), resolve_raid_controller_id()
(Step 0), find_existing_volumes()/clean_existing_volume() (Steps 1/1a),
detect_vroc_passthrough()/get_supported_actions()/diagnose_action_not_supported()
(Step 2), create_raid_array_safe() (Step 3), and ensure_raid_array() (the
single recommended entry point tying all of the above together).
