"""
Fix for the FAILED/blocked steps captured across two related decomm
failures on an Intel server.

=== decomm.txt step failures ===

  Step 11.1  ILO A-Record Check/Rename (serial-ilo): FAILED
             error: IloIPReconfig <uid> did not complete within 1800s
             (last status: in progress)

  Step 15    Mojo Serial Check: FAILED
             error: Mojo Check API call failed with status 500:
             {'error': 'Failed to check serials in Mojo: ... No data found'}

  Step 16.1  Mojo Power State Check/Off: FAILED
             error: Mojo Check API call failed with status 500 (same
             upstream Mojo outage as step 15)

  Step 17    Server Power-Off Validation (Redfish): FAILED
             error: Failed to login with any available credentials from
             vault. Last error: ... Connection timed out (connect
             timeout=30)

=== Additional decomm failure (Mojo power-state set, distinct bug) ===

  {'api_req_uuid': '312615cf-4775-412e-9ef9-5418c4598c37',
   'desired_power_state': 'off',
   'err_msg': "Error in set_power_state wrapper: module "
              "'apt_mojo_consumer.apt_mojo_methods' has no attribute "
              "'set_power_state'",
   'parent_api_reqid': '68feadcc-c496-48d3-bfd9-183889828132',
   'serial': 'LUC223400173', 'status': 'failed', 'success': False}

  This is NOT an HTTP error and NOT a credentials/timeout error — it is
  a Python AttributeError raised *inside* the internal Mojo consumer
  package itself (apt_mojo_consumer.apt_mojo_methods has no
  set_power_state function/method at all). That means the automation's
  chosen power-off path — routing the "set power state to off" request
  through Mojo — is broken at the code level on the Mojo side (e.g. a
  renamed/removed method after a Mojo library upgrade, or a
  version-skewed apt_mojo_consumer install). No retry against Mojo can
  fix this; only bypassing Mojo and issuing the power-off directly to
  the BMC via Redfish resolves it.

=== Root causes and how each is remediated here, via Redfish rather
    than the failing dependency ===

  - Step 11.1 got stuck because the automation waited on an internal
    "IloIPReconfig" job instead of driving the BMC's Ethernet interface
    directly. This module clears the stuck task and PATCHes HostName on
    the EthernetInterface resource directly (spec section 2.30), then
    resets the manager so the new hostname/DNS registration takes effect.

  - Steps 15 and 16.1 both failed because the external Mojo inventory
    service was down (HTTP 500 / "No data found"), not because of
    anything wrong on the BMC. This module reads SerialNumber and
    PowerState directly from the BMC's own Redfish resources instead,
    so decomm validation can proceed on Mojo's own outages.

  - Step 17 failed to authenticate at all (wrong/rotated vault
    credential) and then timed out connecting. This module retries every
    supplied credential with backoff before giving up, and once
    connected, re-runs the actual power-off validation via Redfish.

  - The Mojo "set_power_state" AttributeError failure is remediated by
    set_power_state_via_redfish() below: it never calls into
    apt_mojo_consumer at all. It issues ComputerSystem.Reset with the
    Redfish ResetType that corresponds to the requested
    `desired_power_state` ("off" -> GracefulShutdown, falling back to
    ForceOff if the graceful request doesn't change PowerState in time),
    then verifies PowerState directly from the BMC. The original
    api_req_uuid/serial from the failed Mojo call are accepted as
    optional correlation fields purely for logging/traceability, since
    they identify which decomm request this is remediating.

Every BMC resource access in this module is attempted against each
firmware generation defined in intel.yaml (modeled on the existing
dell.yaml), in the order they're declared there: "openbmc_current" (new
Intel server naming, e.g. /Systems/system, /Managers/bmc) first, then
"openbmc_legacy" (old Intel server naming, e.g. /Systems/1,
/Managers/1) as a fallback. This is done via try_variants() from
redfish_client, so a fix that works on a new server automatically falls
back to whatever an old server actually exposes, and vice versa.

Depends on redfish_client.RedfishClient / RedfishError / try_variants
for auth, session handling, rich error detail, and the old/new fallback
mechanism, and on PyYAML to load intel.yaml.
"""

import os
import time

import yaml

from redfish_client import RedfishClient, try_variants

# Maps a Mojo-style desired_power_state string to the Redfish ResetType
# that achieves it on first attempt, per the ComputerSystem.Reset action
# (Intel OpenBMC spec section 2.49 Actions / DMTF ComputerSystem schema
# ResetType enum).
DESIRED_POWER_STATE_TO_RESET_TYPE = {
    "off": "GracefulShutdown",
    "on": "On",
}

# Escalation used if the first ResetType above doesn't change PowerState
# within the timeout — matches the corresponding forceful ResetType.
ESCALATED_RESET_TYPE = {
    "off": "ForceOff",
    "on": "ForceOn",
}


def load_intel_config(path=None):
    """
    Loads intel.yaml — the per-firmware-generation Redfish endpoint map
    for Intel servers, modeled on dell.yaml. Defaults to intel.yaml next
    to this file. Versions are returned in the order declared in the
    YAML (openbmc_current before openbmc_legacy), which is what makes
    every try_variants() call below try the new-server shape first and
    fall back to the old-server shape second.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intel.yaml")
    with open(path, "r") as f:
        return yaml.safe_load(f)


def connect_with_credential_retry(host, credentials, timeout=30,
                                   max_attempts_per_credential=3, backoff_seconds=5):
    """
    Remediates Step 17's "Failed to login with any available credentials
    from vault ... Connection timed out" error. `credentials` is a list
    of (username, password) tuples (e.g. the candidate vault secrets for
    this host). For each credential in order, retries the login up to
    `max_attempts_per_credential` times with a linear backoff between
    attempts, to absorb the kind of transient ConnectTimeoutError seen
    in the failure, before moving on to the next credential.

    Returns a logged-in RedfishClient on the first successful
    (credential, attempt) combination. Raises RuntimeError listing every
    failed attempt if all credentials/retries are exhausted.
    """
    errors = []
    for username, password in credentials:
        for attempt in range(1, max_attempts_per_credential + 1):
            client = RedfishClient(host, username, password, timeout=timeout)
            try:
                client.login()
                return client
            except Exception as exc:
                errors.append(f"user={username!r} attempt={attempt}: {exc}")
                if attempt < max_attempts_per_credential:
                    time.sleep(backoff_seconds)
    raise RuntimeError(
        f"Could not authenticate to {host} with any of {len(credentials)} credential(s):\n"
        + "\n".join(errors)
    )


def _version_endpoint_variants(config, endpoint_key, call):
    """
    Builds one (version_label, thunk) pair per firmware version declared
    in intel.yaml that defines `endpoint_key`, in declaration order
    (openbmc_current, then openbmc_legacy). `call(path, version_cfg)`
    builds the actual request thunk for that version's endpoint path.
    Feed the result straight into redfish_client.try_variants().
    """
    variants = []
    for version_name, version_cfg in config.get("versions", {}).items():
        path = version_cfg.get("endpoints", {}).get(endpoint_key)
        if path:
            variants.append((version_name, (lambda p=path, c=version_cfg: call(p, c))))
    if not variants:
        raise RuntimeError(f"No firmware version in intel.yaml defines endpoint '{endpoint_key}'")
    return variants


def get_task_status(bmc, task_uid):
    """
    GET /redfish/v1/TaskService/Tasks/{task_uid} and return TaskState,
    TaskStatus, and Messages — used to inspect the stuck IloIPReconfig
    task from Step 11.1 before deciding to stop waiting on it.
    """
    task = bmc.get(f"/redfish/v1/TaskService/Tasks/{task_uid}")
    return {
        "TaskState": task.get("TaskState"),
        "TaskStatus": task.get("TaskStatus"),
        "Messages": task.get("Messages"),
    }


def clear_stuck_task(bmc, task_uid):
    """
    DELETE /redfish/v1/TaskService/Tasks/{task_uid} to remove the stuck
    IloIPReconfig task from Step 11.1 so it stops being reported as
    "in progress" and cannot block a subsequent direct rename attempt.
    Non-fatal if it errors (the task may already be gone or the BMC may
    not allow deleting an in-flight task, in which case the direct
    HostName PATCH below still proceeds regardless).
    """
    try:
        return bmc.delete(f"/redfish/v1/TaskService/Tasks/{task_uid}")
    except Exception as exc:
        return {"cleared": False, "error": str(exc)}


def fix_ilo_a_record_rename(bmc, config, new_hostname, stuck_task_uid=None, apply_via_reset=True):
    """
    Remediates Step 11.1 "ILO A-Record Check/Rename (serial-ilo): FAILED
    — IloIPReconfig ... did not complete within 1800s (last status: in
    progress)":
      1. If `stuck_task_uid` is given, inspects it (best-effort) and
         clears it via clear_stuck_task() instead of continuing to wait.
      2. PATCHes HostName directly on the EthernetInterface resource
         (spec section 2.30 — a top-level RW property), trying every
         firmware version's ethernet_interface_path from intel.yaml
         (new-server shape first, old-server shape as fallback).
      3. If `apply_via_reset` (default), issues Manager.Reset
         (GracefulRestart) via each version's manager_reset_action_path
         so the BMC re-registers the new hostname/DNS entry, again
         falling back new-to-old. Note this reboots the BMC, so the
         current Redfish session will be invalidated afterward.
      4. Re-reads the EthernetInterface to confirm HostName now matches
         `new_hostname`.
    Returns a dict describing which firmware version succeeded at each
    step and the final verification result.
    """
    result = {}

    if stuck_task_uid:
        try:
            result["task_status_before_clear"] = get_task_status(bmc, stuck_task_uid)
        except Exception as exc:
            result["task_status_before_clear"] = {"error": str(exc)}
        result["task_cleared"] = clear_stuck_task(bmc, stuck_task_uid)

    patch_variants = _version_endpoint_variants(
        config, "ethernet_interface_path",
        lambda path, cfg: bmc.patch(path, {"HostName": new_hostname}),
    )
    version_used, patch_result = try_variants(patch_variants)
    result["hostname_patch_version"] = version_used
    result["hostname_patch_result"] = patch_result

    if apply_via_reset:
        try:
            reset_variants = _version_endpoint_variants(
                config, "manager_reset_action_path",
                lambda path, cfg: bmc.post(path, cfg.get("manager_reset_action_payload", {"ResetType": "GracefulRestart"})),
            )
            reset_version, reset_result = try_variants(reset_variants)
            result["manager_reset_version"] = reset_version
            result["manager_reset_result"] = reset_result
        except RuntimeError as exc:
            result["manager_reset_error"] = str(exc)

    verify_variants = _version_endpoint_variants(
        config, "ethernet_interface_path",
        lambda path, cfg: bmc.get(path),
    )
    verify_version, iface_after = try_variants(verify_variants)
    result["verified_version"] = verify_version
    result["hostname_now"] = iface_after.get("HostName")
    result["fqdn_now"] = iface_after.get("FQDN")
    result["matches_expected"] = iface_after.get("HostName") == new_hostname
    return result


def get_serial_number_with_fallback(bmc, config):
    """
    Remediates Step 15 "Mojo Serial Check: FAILED — Mojo Check API call
    failed with status 500 ... No data found". Rather than depending on
    the external Mojo inventory service, this reads the serial number
    directly from the BMC via Redfish: tries each firmware version's
    system_path (ComputerSystem.SerialNumber) first, then falls back to
    each version's chassis_path (Chassis.SerialNumber), new-server shape
    before old-server shape in both groups. Returns (version_label,
    serial_number); raises RuntimeError if no resource in any version
    yields a SerialNumber.
    """
    variants = _version_endpoint_variants(config, "system_path", lambda path, cfg: bmc.get(path))
    variants += _version_endpoint_variants(
        config, "chassis_path",
        lambda path, cfg: bmc.get(path),
    )
    version_used, resource = try_variants(variants)
    serial = resource.get("SerialNumber")
    if not serial:
        raise RuntimeError(f"Resolved a resource via '{version_used}' but it has no SerialNumber")
    return version_used, serial


def get_power_state_with_fallback(bmc, config):
    """
    Remediates Step 16.1 "Mojo Power State Check/Off: FAILED — Mojo
    Check API call failed with status 500" (and doubles as the Redfish
    side of Step 17's power-off validation). Reads PowerState directly
    from the BMC's ComputerSystem resource, bypassing the failed Mojo
    dependency entirely, trying each firmware version's system_path
    (new-server shape first, old-server shape as fallback). Returns a
    dict with the firmware version used, the raw PowerState value, and
    whether it matches that version's configured power_off_values
    (i.e. the server is confirmed powered off).
    """
    variants = _version_endpoint_variants(
        config, "system_path",
        lambda path, cfg: (cfg, bmc.get(path)),
    )
    version_used, (version_cfg, resource) = try_variants(variants)
    power_state = resource.get(version_cfg.get("power_state_field", "PowerState"))
    confirmed_off = power_state in version_cfg.get("power_off_values", ["Off"])
    return {"version": version_used, "power_state": power_state, "confirmed_off": confirmed_off}


def fix_power_off_validation(host, credentials, config, max_attempts_per_credential=3,
                              backoff_seconds=5, timeout=30):
    """
    Remediates Step 17 "Server Power-Off Validation (Redfish): FAILED —
    Failed to login with any available credentials from vault ...
    Connection timed out" end-to-end: connects via
    connect_with_credential_retry() (multi-credential + backoff retry
    for the login/connectivity failure), then re-runs the actual
    power-off check via get_power_state_with_fallback() (new/old
    endpoint retry). The session is always logged out afterward, even
    if the validation itself raises.
    """
    bmc = connect_with_credential_retry(
        host, credentials, timeout=timeout,
        max_attempts_per_credential=max_attempts_per_credential,
        backoff_seconds=backoff_seconds,
    )
    try:
        return get_power_state_with_fallback(bmc, config)
    finally:
        bmc.logout()


def set_power_state_via_redfish(bmc, config, desired_power_state="off",
                                 poll_interval_seconds=10, timeout_seconds=180,
                                 api_req_uuid=None, serial=None):
    """
    Remediates the Mojo failure:
      "Error in set_power_state wrapper: module
       'apt_mojo_consumer.apt_mojo_methods' has no attribute
       'set_power_state'"

    That error is an AttributeError inside Mojo's own consumer package —
    the method the automation tried to call does not exist in the
    installed apt_mojo_consumer version. No amount of retrying the Mojo
    call will help; this function bypasses apt_mojo_consumer entirely
    and drives the power state change straight through Redfish:

      1. Maps `desired_power_state` ("off"/"on") to the corresponding
         ComputerSystem.Reset ResetType (GracefulShutdown/On) via
         DESIRED_POWER_STATE_TO_RESET_TYPE, and POSTs it against every
         firmware version's reboot_action_path from intel.yaml
         (new-server shape first, old-server shape as fallback).
      2. Polls get_power_state_with_fallback() every
         `poll_interval_seconds` until PowerState reflects the desired
         state or `timeout_seconds` elapses.
      3. If the graceful reset didn't take effect in time, escalates to
         the forceful ResetType (ForceOff/ForceOn per
         ESCALATED_RESET_TYPE) and polls again for the same timeout.
      4. Returns a dict including the original `api_req_uuid`/`serial`
         (if given) purely for correlating this remediation back to the
         specific failed Mojo request it replaces — they are not sent
         to the BMC.

    Raises RuntimeError with the full attempt history if the desired
    power state is still not reached after both the graceful and
    escalated attempts.
    """
    if desired_power_state not in DESIRED_POWER_STATE_TO_RESET_TYPE:
        raise ValueError(f"Unsupported desired_power_state '{desired_power_state}'; expected 'on' or 'off'")

    result = {"api_req_uuid": api_req_uuid, "serial": serial, "desired_power_state": desired_power_state}

    def _issue_reset(reset_type):
        variants = _version_endpoint_variants(
            config, "reboot_action_path",
            lambda path, cfg: bmc.post(path, {"ResetType": reset_type}),
        )
        return try_variants(variants)

    def _wait_for_state(deadline):
        while time.time() < deadline:
            state = get_power_state_with_fallback(bmc, config)
            target_is_off = desired_power_state == "off"
            reached = state["confirmed_off"] if target_is_off else not state["confirmed_off"]
            if reached:
                return state
            time.sleep(poll_interval_seconds)
        return None

    graceful_reset_type = DESIRED_POWER_STATE_TO_RESET_TYPE[desired_power_state]
    reset_version, reset_response = _issue_reset(graceful_reset_type)
    result["graceful_reset_type"] = graceful_reset_type
    result["graceful_reset_version"] = reset_version
    result["graceful_reset_response"] = reset_response

    final_state = _wait_for_state(time.time() + timeout_seconds)
    if final_state is not None:
        result["final_power_state"] = final_state
        result["escalated"] = False
        return result

    escalated_reset_type = ESCALATED_RESET_TYPE[desired_power_state]
    reset_version, reset_response = _issue_reset(escalated_reset_type)
    result["escalated_reset_type"] = escalated_reset_type
    result["escalated_reset_version"] = reset_version
    result["escalated_reset_response"] = reset_response

    final_state = _wait_for_state(time.time() + timeout_seconds)
    if final_state is not None:
        result["final_power_state"] = final_state
        result["escalated"] = True
        return result

    raise RuntimeError(
        f"Power state '{desired_power_state}' not reached after graceful "
        f"({graceful_reset_type}) and escalated ({escalated_reset_type}) resets, "
        f"each polled for {timeout_seconds}s. Attempt detail: {result}"
    )


def run_decomm_fixes(host, credentials, new_hostname, config_path=None,
                      stuck_ilo_task_uid=None, apply_hostname_via_reset=True,
                      mojo_power_state_failure=None):
    """
    End-to-end remediation for every FAILED/blocked step captured in
    decomm.txt, plus the separate Mojo "set_power_state" AttributeError
    failure when one is supplied:
      - Step 17: credential/connectivity retry, then power-off validation
      - Step 11.1: stuck ILO A-record rename
      - Step 15 / 16.1: Mojo-independent serial number + power state
      - Mojo set_power_state AttributeError: if `mojo_power_state_failure`
        is given as a dict with keys desired_power_state/api_req_uuid/
        serial (matching the failed Mojo payload), remediates it via
        set_power_state_via_redfish() instead of retrying the broken
        Mojo call.
    Each step's failure is caught and recorded individually rather than
    aborting the run, mirroring how the original decomm continued past
    each FAILED entry to attempt the remaining steps. Note that if
    Step 11.1 resets the BMC (apply_hostname_via_reset=True, the
    default), the session may be invalidated for whatever steps run
    after it — this is expected and each step's own error is still
    reported individually.
    """
    config = load_intel_config(config_path)
    results = {}

    try:
        bmc = connect_with_credential_retry(host, credentials)
    except Exception as exc:
        results["step_17_login"] = {"error": str(exc)}
        return results
    results["step_17_login"] = {"connected": True}

    try:
        results["step_17_power_off_validation"] = get_power_state_with_fallback(bmc, config)
    except Exception as exc:
        results["step_17_power_off_validation"] = {"error": str(exc)}

    try:
        results["step_11_1_ilo_a_record_rename"] = fix_ilo_a_record_rename(
            bmc, config, new_hostname,
            stuck_task_uid=stuck_ilo_task_uid,
            apply_via_reset=apply_hostname_via_reset,
        )
    except Exception as exc:
        results["step_11_1_ilo_a_record_rename"] = {"error": str(exc)}

    try:
        version_used, serial = get_serial_number_with_fallback(bmc, config)
        results["step_15_mojo_serial_check"] = {"version": version_used, "serial_number": serial}
    except Exception as exc:
        results["step_15_mojo_serial_check"] = {"error": str(exc)}

    try:
        results["step_16_1_mojo_power_state_check"] = get_power_state_with_fallback(bmc, config)
    except Exception as exc:
        results["step_16_1_mojo_power_state_check"] = {"error": str(exc)}

    if mojo_power_state_failure:
        try:
            results["mojo_set_power_state_fix"] = set_power_state_via_redfish(
                bmc, config,
                desired_power_state=mojo_power_state_failure.get("desired_power_state", "off"),
                api_req_uuid=mojo_power_state_failure.get("api_req_uuid"),
                serial=mojo_power_state_failure.get("serial"),
            )
        except Exception as exc:
            results["mojo_set_power_state_fix"] = {"error": str(exc)}

    bmc.logout()
    return results


def _parse_credentials_env(value):
    """Parses a "user1:pass1,user2:pass2" env var into a list of (username, password) tuples."""
    creds = []
    for pair in value.split(","):
        if ":" in pair:
            username, password = pair.split(":", 1)
            creds.append((username, password))
    return creds


if __name__ == "__main__":
    host = os.environ.get("BMC_HOST")
    new_hostname = os.environ.get("NEW_HOSTNAME")
    stuck_ilo_task_uid = os.environ.get("STUCK_ILO_TASK_UID", "5b18ddf2-c632-4bfb-88cc-34073eb413e8")
    config_path = os.environ.get("INTEL_YAML_PATH")

    credentials_env = os.environ.get("VAULT_CREDENTIALS")
    if credentials_env:
        credentials = _parse_credentials_env(credentials_env)
    else:
        single_user = os.environ.get("BMC_USER", "root")
        single_password = os.environ.get("BMC_PASSWORD")
        credentials = [(single_user, single_password)] if single_password else []

    if not host or not new_hostname or not credentials:
        raise SystemExit(
            "Set BMC_HOST, NEW_HOSTNAME, and either VAULT_CREDENTIALS "
            "(user1:pass1,user2:pass2) or BMC_USER/BMC_PASSWORD env vars first"
        )

    # Matches the failed Mojo payload:
    # {'api_req_uuid': '312615cf-4775-412e-9ef9-5418c4598c37',
    #  'desired_power_state': 'off',
    #  'parent_api_reqid': '68feadcc-c496-48d3-bfd9-183889828132',
    #  'serial': 'LUC223400173'}
    mojo_power_state_failure = {
        "desired_power_state": os.environ.get("MOJO_DESIRED_POWER_STATE", "off"),
        "api_req_uuid": os.environ.get("MOJO_API_REQ_UUID"),
        "serial": os.environ.get("MOJO_SERIAL"),
    }

    results = run_decomm_fixes(
        host, credentials, new_hostname,
        config_path=config_path,
        stuck_ilo_task_uid=stuck_ilo_task_uid,
        mojo_power_state_failure=mojo_power_state_failure,
    )
    print(results)
