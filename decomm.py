"""
Redfish DECOMM (decommission) operations for Intel Server System
(OpenBMC) BMCs.

Functions here tear down/reset a server via its BMC's Redfish
interface prior to retirement, redeployment, or return — removing
accounts, ejecting media, wiping RAID volumes, clearing logs, and
resetting the BMC to factory defaults — per the Intel Server System
Integrated BMC Firmware OpenBMC Redfish API Specification (Rev 1.3,
April 2024). Pass in a logged-in RedfishClient (see redfish_client.py)
to each function.

These operations are destructive and largely irreversible. Callers
should confirm intent (and capture a scan report) before invoking them.
"""


def power_off_system(bmc, graceful=True):
    """
    POST /redfish/v1/Systems/system/Actions/ComputerSystem.Reset with
    ResetType "GracefulShutdown" (graceful=True) or "ForceOff"
    (graceful=False), to power the host down as the first step of
    decommissioning before any destructive storage/config operations.
    """
    reset_type = "GracefulShutdown" if graceful else "ForceOff"
    return bmc.post(
        "/redfish/v1/Systems/system/Actions/ComputerSystem.Reset",
        {"ResetType": reset_type},
    )


def eject_all_virtual_media(bmc):
    """
    GET /redfish/v1/Managers/bmc/VirtualMedia, then POST
    VirtualMedia.EjectMedia on every member currently reporting
    Inserted=true, to ensure no ISO/image is left mounted before the
    server is shipped out or repurposed.
    """
    collection = bmc.get("/redfish/v1/Managers/bmc/VirtualMedia")
    results = []
    for member in collection.get("Members", []):
        media = bmc.get(member["@odata.id"])
        if media.get("Inserted"):
            results.append(
                bmc.post(f"{member['@odata.id']}/Actions/VirtualMedia.EjectMedia")
            )
    return results


def delete_raid_volume(bmc, raid_controller_id, logical_drive_id):
    """
    POST /redfish/v1/Systems/system/Storage/{raid_controller_id}/Actions/StorageLDrive.Delete
    with {"LDriveId": logical_drive_id} to delete a RAID logical drive,
    wiping its configuration from the controller. Used during decomm to
    remove customer data volumes before the array is reused.
    """
    return bmc.post(
        f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Actions/StorageLDrive.Delete",
        {"LDriveId": logical_drive_id},
    )


def clear_raid_configuration(bmc, raid_controller_id):
    """
    POST /redfish/v1/Systems/system/Storage/{raid_controller_id}/Actions/StorageLDrive.Create
    with {"CmdParm": 0} (CLEAR CFG) to wipe the entire RAID
    configuration on a controller in one step, rather than deleting
    volumes individually. Use ahead of drive removal/redeployment.
    """
    return bmc.post(
        f"/redfish/v1/Systems/system/Storage/{raid_controller_id}/Actions/StorageLDrive.Create",
        {"CmdParm": 0},
    )


def delete_local_account(bmc, account_id):
    """
    DELETE /redfish/v1/AccountService/Accounts/{account_id} to remove a
    local BMC user account entirely, revoking its access as part of
    decommissioning.
    """
    return bmc.delete(f"/redfish/v1/AccountService/Accounts/{account_id}")


def disable_local_account(bmc, account_id):
    """
    PATCH /redfish/v1/AccountService/Accounts/{account_id} with
    {"Enabled": false} to disable (rather than delete) a local account,
    useful when an audit trail of the account must be preserved during
    decommissioning.
    """
    return bmc.patch(f"/redfish/v1/AccountService/Accounts/{account_id}", {"Enabled": False})


def clear_event_log(bmc):
    """
    POST /redfish/v1/Systems/system/LogServices/EventLog/Actions/LogService.ClearLog
    (no parameters) to clear the System Event Log, removing historical
    fault records before the server changes ownership.
    """
    return bmc.post("/redfish/v1/Systems/system/LogServices/EventLog/Actions/LogService.ClearLog")


def clear_crashdump_log(bmc):
    """
    POST /redfish/v1/Systems/system/LogServices/Crashdump/Actions/LogService.ClearLog
    (no parameters) to clear stored crash dump entries during decomm.
    """
    return bmc.post("/redfish/v1/Systems/system/LogServices/Crashdump/Actions/LogService.ClearLog")


def clear_indicator_led(bmc):
    """
    PATCH /redfish/v1/Systems/system with {"IndicatorLED": "Off"} to turn
    off the system identify LED after decommissioning work is complete.
    """
    return bmc.patch("/redfish/v1/Systems/system", {"IndicatorLED": "Off"})


def clear_asset_tag(bmc):
    """
    PATCH /redfish/v1/Systems/system with {"AssetTag": ""} to remove the
    organization's asset tag before the system leaves inventory/custody.
    """
    return bmc.patch("/redfish/v1/Systems/system", {"AssetTag": ""})


def reset_boot_override_to_none(bmc):
    """
    PATCH /redfish/v1/Systems/system with
    {"Boot": {"BootSourceOverrideEnabled": "Disabled",
    "BootSourceOverrideTarget": "None"}} to clear any one-time/continuous
    boot override left over from build/imaging, returning the system to
    its normal boot order.
    """
    return bmc.patch(
        "/redfish/v1/Systems/system",
        {"Boot": {"BootSourceOverrideEnabled": "Disabled", "BootSourceOverrideTarget": "None"}},
    )


def reset_bmc_to_factory_defaults(bmc, keep_reserved_settings=False):
    """
    POST /redfish/v1/Managers/bmc/Actions/Manager.ResetToDefaults with
    ResetToDefaultsType "ResetAll" (keep_reserved_settings=False) or
    "ResetToDefaultButKeepReservedSettings" (keep_reserved_settings=True)
    to factory-reset the BMC — wiping accounts, network config, and
    customizations. This is the terminal step of a full decommission and
    is irreversible; the BMC will reboot afterward.
    """
    reset_type = (
        "ResetToDefaultButKeepReservedSettings" if keep_reserved_settings else "ResetAll"
    )
    return bmc.post(
        "/redfish/v1/Managers/bmc/Actions/Manager.ResetToDefaults",
        {"ResetToDefaultsType": reset_type},
    )


def full_decommission(bmc, raid_controller_ids=None):
    """
    Convenience orchestrator that runs a full decommission sequence in
    order: power off the host, eject all virtual media, clear RAID
    configuration on each controller in `raid_controller_ids`, clear the
    boot override, clear the identify LED and asset tag, clear the event
    and crash dump logs, and finally factory-reset the BMC. Returns a
    dict of the individual step results. Intended as a reference
    sequence — review and confirm each destructive step is appropriate
    for the target hardware before running unattended.
    """
    results = {
        "power_off": power_off_system(bmc, graceful=True),
        "eject_media": eject_all_virtual_media(bmc),
    }
    raid_results = []
    for controller_id in raid_controller_ids or []:
        raid_results.append(clear_raid_configuration(bmc, controller_id))
    results["raid_clear"] = raid_results
    results["boot_override_reset"] = reset_boot_override_to_none(bmc)
    results["indicator_led_cleared"] = clear_indicator_led(bmc)
    results["asset_tag_cleared"] = clear_asset_tag(bmc)
    results["event_log_cleared"] = clear_event_log(bmc)
    results["crashdump_cleared"] = clear_crashdump_log(bmc)
    results["bmc_factory_reset"] = reset_bmc_to_factory_defaults(bmc)
    return results
