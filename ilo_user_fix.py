"""
Fix for: "ILO User Creation: Failed — AddUser failed"

(Intel server only — targets the Redfish AccountService resources
documented in the Intel Server System Integrated BMC Firmware OpenBMC
Redfish API Specification.)

The failure was generic ("AddUser failed") with no ExtendedInfo
captured, which usually means one of:

  - UserName already exists (duplicate account)
  - Password does not satisfy AccountService.MinPasswordLength /
    MaxPasswordLength / PasswordPolicyComplexity
  - RoleId is not one of Administrator/Operator/ReadOnly/NoAccess
  - Accounts collection is full (all account slots in use)
  - The account-creation endpoint/payload shape differs between old and
    new Intel BMC firmware generations

This module validates the requested username/password/role against the
BMC's actual AccountService constraints, and tries every known
account-creation request shape (current spec first, then older/alternate
shapes) via try_variants() from redfish_client, so a fix that works on
the new fleet automatically falls back to whatever the old fleet
actually expects, and vice versa.

Depends on redfish_client.RedfishClient for auth/session handling.
For the related "BIOS / ILO Configuration: Failed" error, see
bios_ilo_configuration_fix.py.
"""

from redfish_client import RedfishClient, try_variants


def get_account_service_policy(bmc):
    """
    GET /redfish/v1/AccountService and return the constraints new
    accounts must satisfy: MinPasswordLength, MaxPasswordLength, and
    AccountLockoutThreshold — used to validate a password/role before
    attempting account creation.
    """
    service = bmc.get("/redfish/v1/AccountService")
    return {
        "MinPasswordLength": service.get("MinPasswordLength"),
        "MaxPasswordLength": service.get("MaxPasswordLength"),
        "AccountLockoutThreshold": service.get("AccountLockoutThreshold"),
    }


def get_valid_role_ids(bmc):
    """
    GET /redfish/v1/AccountService/Roles, then GET each member, to
    return the list of valid RoleId values on this BMC (normally
    Administrator, Operator, ReadOnly, NoAccess) so a caller can catch a
    bad RoleId before POSTing an account. Falls back to the standard
    four-role list if the Roles collection can't be read (seen on some
    older firmware).
    """
    try:
        collection = bmc.get("/redfish/v1/AccountService/Roles")
        roles = []
        for member in collection.get("Members", []):
            role = bmc.get(member["@odata.id"])
            roles.append(role.get("RoleId") or member["@odata.id"].rsplit("/", 1)[-1])
        if roles:
            return roles
    except Exception:
        pass
    return ["Administrator", "Operator", "ReadOnly", "NoAccess"]


def get_existing_usernames(bmc):
    """
    GET /redfish/v1/AccountService/Accounts, then GET each member, to
    return the set of UserName values already in use, so
    fix_ilo_user_creation() can detect a duplicate-username failure
    before retrying the POST.
    """
    collection = bmc.get("/redfish/v1/AccountService/Accounts")
    usernames = set()
    for member in collection.get("Members", []):
        account = bmc.get(member["@odata.id"])
        if account.get("UserName"):
            usernames.add(account["UserName"])
    return usernames


def _create_account_current(bmc, username, password, role_id, enabled):
    """Current spec shape: POST /AccountService/Accounts with UserName/Password/RoleId/Enabled."""
    body = {"UserName": username, "Password": password, "RoleId": role_id, "Enabled": enabled}
    return bmc.post("/redfish/v1/AccountService/Accounts", body)


def _create_account_role_uri(bmc, username, password, role_id, enabled):
    """Older-firmware shape: RoleId must be passed as a full Links.Role @odata.id reference instead of a bare string."""
    body = {
        "UserName": username,
        "Password": password,
        "Enabled": enabled,
        "Links": {"Role": {"@odata.id": f"/redfish/v1/AccountService/Roles/{role_id}"}},
    }
    return bmc.post("/redfish/v1/AccountService/Accounts", body)


def _create_account_put(bmc, username, password, role_id, enabled):
    """Legacy fallback: some early Intel builds require PUT to a pre-existing empty account slot rather than POST."""
    body = {"UserName": username, "Password": password, "RoleId": role_id, "Enabled": enabled}
    resp = bmc._http.put(bmc._url(f"/redfish/v1/AccountService/Accounts/{username}"), json=body, timeout=bmc.timeout)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def fix_ilo_user_creation(bmc, username, password, role_id="Administrator", enabled=True):
    """
    Remediates the "ILO User Creation: Failed — AddUser failed" error by
    validating inputs against the BMC's actual constraints, then trying
    every known account-creation request shape in order until one
    succeeds:
      1. If the username already exists, PATCH that account's
         password/role/enabled state instead of creating a duplicate
         (a common cause of "AddUser failed" on any generation).
      2. Otherwise, POST with a bare RoleId string (current spec).
      3. Fall back to POST with RoleId expressed as a Links.Role
         @odata.id reference (older firmware).
      4. Fall back to PUT against the account slot directly (legacy).
    Raises ValueError first if role_id/password fail the BMC's own
    AccountService policy, rather than letting every variant fail for
    the same reason.
    """
    valid_roles = get_valid_role_ids(bmc)
    if role_id not in valid_roles:
        raise ValueError(f"RoleId '{role_id}' is invalid; valid roles are {valid_roles}")

    policy = get_account_service_policy(bmc)
    min_len = policy.get("MinPasswordLength")
    max_len = policy.get("MaxPasswordLength")
    if min_len and len(password) < min_len:
        raise ValueError(f"Password is shorter than MinPasswordLength ({min_len})")
    if max_len and len(password) > max_len:
        raise ValueError(f"Password is longer than MaxPasswordLength ({max_len})")

    existing = get_existing_usernames(bmc)
    if username in existing:
        result = bmc.patch(
            f"/redfish/v1/AccountService/Accounts/{username}",
            {"Password": password, "RoleId": role_id, "Enabled": enabled, "Locked": False},
        )
        return "existing_account_patch", result

    return try_variants([
        ("new_post_bare_role_id", lambda: _create_account_current(bmc, username, password, role_id, enabled)),
        ("old_post_role_uri_link", lambda: _create_account_role_uri(bmc, username, password, role_id, enabled)),
        ("legacy_put_account_slot", lambda: _create_account_put(bmc, username, password, role_id, enabled)),
    ])


if __name__ == "__main__":
    import os

    host = os.environ.get("BMC_HOST")
    user = os.environ.get("BMC_USER", "root")
    password = os.environ.get("BMC_PASSWORD")

    if not host or not password:
        raise SystemExit("Set BMC_HOST, BMC_USER, BMC_PASSWORD env vars first")

    with RedfishClient(host, user, password) as bmc:
        new_user_result = fix_ilo_user_creation(
            bmc,
            username=os.environ.get("NEW_ILO_USER", "svc_build"),
            password=os.environ.get("NEW_ILO_PASSWORD", ""),
            role_id=os.environ.get("NEW_ILO_ROLE", "Administrator"),
        )
        print("User creation result:", new_user_result)
