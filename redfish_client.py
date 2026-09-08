"""
Shared Redfish HTTP client for Intel Server System (OpenBMC) BMCs.

Implements session-based authentication against SessionService and thin
GET/PATCH/POST/DELETE wrappers used by scan.py, build.py, decomm.py, and
every *_fix.py module. Endpoints and payloads follow the Intel Server
System Integrated BMC Firmware OpenBMC Redfish API Specification
(Rev 1.3, April 2024) plus the DMTF Redfish Base Message Registry for
error interpretation.
"""

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class RedfishError(Exception):
    """
    Raised by every RedfishClient HTTP method instead of a bare
    requests.HTTPError, so callers (and try_variants()'s aggregated
    error list) get full diagnostic detail instead of just "400 Client
    Error" or "405 Client Error":

      - method / url / request_body: exactly what was sent
      - status_code: the HTTP status returned
      - message_id: the short Redfish MessageId (e.g. "ActionNotSupported",
        "PropertyUnknown"), extracted from the last segment of the full
        registry-qualified MessageId (e.g. "Base.1.5.0.ActionNotSupported")
      - message / resolution: the human-readable Message and Resolution
        text from the response's @Message.ExtendedInfo, when the BMC
        returned a standard Redfish error body
      - related_properties: RelatedProperties list, when present (e.g.
        which specific property was unknown/unsupported)
      - raw_body: the full parsed JSON error body (or raw text if it
        wasn't JSON), for anything not captured above

    This is what lets fix modules answer "why did this fail" precisely
    (e.g. distinguish a 405 ActionNotSupported — this resource has no
    such action — from a 400 PropertyUnknown — the payload shape is
    wrong for this firmware) instead of guessing from a generic
    exception message.

    Per the DMTF Redfish Base Message Registry:
      - ActionNotSupported: "The action %1 is not supported by the
        resource." Resolution: "Check the Actions property in the
        resource for the supported actions." Typically paired with
        HTTP 405 Method Not Allowed.
      - PropertyUnknown: "The property %1 is not in the list of valid
        properties for the resource." Resolution: "Remove the unknown
        property from the request body and resubmit the request."
        Typically paired with HTTP 400 Bad Request.
    """

    def __init__(self, method, url, status_code, request_body=None, raw_body=None):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.request_body = request_body
        self.raw_body = raw_body
        self.message_id = None
        self.message = None
        self.resolution = None
        self.related_properties = None

        extended_info = []
        if isinstance(raw_body, dict):
            extended_info = raw_body.get("error", {}).get("@Message.ExtendedInfo", [])
        if extended_info:
            first = extended_info[0]
            full_message_id = first.get("MessageId", "")
            self.message_id = full_message_id.rsplit(".", 1)[-1] if full_message_id else None
            self.message = first.get("Message")
            self.resolution = first.get("Resolution")
            self.related_properties = first.get("RelatedProperties")

        super().__init__(self._render())

    def _render(self):
        lines = [f"{self.method} {self.url} -> HTTP {self.status_code}"]
        if self.message_id:
            lines.append(f"  MessageId: {self.message_id}")
        if self.message:
            lines.append(f"  Message: {self.message}")
        if self.related_properties:
            lines.append(f"  RelatedProperties: {self.related_properties}")
        if self.resolution:
            lines.append(f"  Resolution: {self.resolution}")
        if self.request_body is not None:
            lines.append(f"  RequestBody: {self.request_body}")
        if not (self.message_id or self.message or self.resolution):
            lines.append(f"  RawBody: {self.raw_body}")
        return "\n".join(lines)

    def is_action_not_supported(self):
        """True if the BMC reported this exact request as an unsupported action (HTTP 405 / MessageId ActionNotSupported)."""
        return self.status_code == 405 or self.message_id == "ActionNotSupported"

    def is_property_unknown(self):
        """True if the BMC rejected the request body for containing a property it doesn't recognize (MessageId PropertyUnknown)."""
        return self.message_id == "PropertyUnknown"


def try_variants(variants):
    """
    Shared retry-with-fallback helper used by every *_fix.py module.

    `variants` is a list of (label, callable) pairs. Different generations
    of Intel BMC firmware (older Intel server builds vs. newer OpenBMC
    releases) sometimes expose the same configuration through a slightly
    different endpoint/resource ID/payload shape. Each variant here is one
    such attempt; they are tried strictly in order (put the current,
    spec-documented shape first, then progressively older/alternate
    shapes as fallbacks).

    Returns (label, result) for the first variant that succeeds. If every
    variant raises, raises a single RuntimeError whose message lists the
    label and error for each failed attempt (RedfishError's rich
    method/url/MessageId/Resolution detail included when available), so
    the caller can see exactly what was tried and why each one failed,
    instead of only the last failure.
    """
    errors = []
    for label, func in variants:
        try:
            return label, func()
        except Exception as exc:
            errors.append(f"[{label}] {exc}")
    raise RuntimeError("All variants failed:\n" + "\n\n".join(errors))


class RedfishClient:
    """
    Thin wrapper around a Redfish session on an Intel OpenBMC BMC.

    Handles login (POST /redfish/v1/SessionService/Sessions), attaches the
    returned X-Auth-Token to subsequent requests, and exposes get/patch/
    post/delete helpers that raise RedfishError (not a bare
    requests.HTTPError) on HTTP errors, with full MessageId/Message/
    Resolution detail parsed out when the BMC returns a standard Redfish
    error body. Use as a context manager so the session is always deleted
    on the BMC when done:

        with RedfishClient("10.0.0.5", "root", "0penBmc123") as bmc:
            bmc.get("/redfish/v1/Systems/system")
    """

    def __init__(self, host, username, password, verify_ssl=False, timeout=30):
        self.base_url = f"https://{host}"
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.token = None
        self.session_uri = None
        self._http = requests.Session()
        self._http.verify = verify_ssl

    def login(self):
        """
        Create a Redfish session via POST /redfish/v1/SessionService/Sessions
        using {"UserName": ..., "Password": ...}, and store the X-Auth-Token
        and session URI (from the Location header) for reuse on every
        subsequent request and for logout.
        """
        url = f"{self.base_url}/redfish/v1/SessionService/Sessions"
        body = {"UserName": self.username, "Password": self.password}
        resp = self._http.post(url, json=body, timeout=self.timeout)
        self._raise_if_error("POST", url, resp, body)
        self.token = resp.headers.get("X-Auth-Token")
        self.session_uri = resp.headers.get("Location")
        self._http.headers.update({"X-Auth-Token": self.token})
        return self.token

    def logout(self):
        """
        Delete the active session via DELETE on the session URI returned at
        login, releasing the BMC-side session slot. Safe to call even if
        login() was never called.
        """
        if self.session_uri:
            self._http.delete(self.session_uri, timeout=self.timeout)
            self.session_uri = None
            self.token = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()

    def _url(self, path):
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def _raise_if_error(self, method, url, resp, request_body=None):
        if resp.ok:
            return
        try:
            raw_body = resp.json()
        except ValueError:
            raw_body = resp.text
        raise RedfishError(method, url, resp.status_code, request_body=request_body, raw_body=raw_body)

    def get(self, path):
        """Perform a GET on a Redfish resource path and return the parsed JSON body. Raises RedfishError on non-2xx."""
        url = self._url(path)
        resp = self._http.get(url, timeout=self.timeout)
        self._raise_if_error("GET", url, resp)
        return resp.json() if resp.content else {}

    def patch(self, path, body):
        """Perform a PATCH with a JSON body against a Redfish resource path. Raises RedfishError on non-2xx."""
        url = self._url(path)
        resp = self._http.patch(url, json=body, timeout=self.timeout)
        self._raise_if_error("PATCH", url, resp, body)
        return resp.json() if resp.content else {}

    def post(self, path, body=None):
        """Perform a POST (used for Actions and resource creation) with an optional JSON body. Raises RedfishError on non-2xx."""
        url = self._url(path)
        body = body or {}
        resp = self._http.post(url, json=body, timeout=self.timeout)
        self._raise_if_error("POST", url, resp, body)
        return resp.json() if resp.content else {}

    def put(self, path, body=None):
        """Perform a PUT with an optional JSON body against a Redfish resource path. Raises RedfishError on non-2xx."""
        url = self._url(path)
        body = body or {}
        resp = self._http.put(url, json=body, timeout=self.timeout)
        self._raise_if_error("PUT", url, resp, body)
        return resp.json() if resp.content else {}

    def delete(self, path):
        """Perform a DELETE against a Redfish resource path (e.g. removing an account or volume). Raises RedfishError on non-2xx."""
        url = self._url(path)
        resp = self._http.delete(url, timeout=self.timeout)
        self._raise_if_error("DELETE", url, resp)
        return resp.json() if resp.content else {}
