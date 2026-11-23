"""Profile-configured calendar creation engine; retained developer CLI defaults to SAMPLE-A."""
from __future__ import annotations

import argparse
import base64
import ctypes
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import gzip
import getpass
import html
import io
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zipfile import BadZipFile
from copy import deepcopy

# Standalone legacy CLI and imported launcher share the same origin validation.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from environments import normalize_origin

ROOT = Path(__file__).resolve().parent
DISCOVERY = ROOT / "support" if (ROOT / "support").is_dir() else ROOT.parent / "K12_SAMPLE_A_Calendar_Discovery"
if __package__:
    from .support.export_calendars import DEFAULT_BASE, NoRedirect, ReadError, read_secret, record_id
    from .support.event_test import hdl_datetime, make_archive, monitor, successful
    from .validate_pilot import create_plan
    from .build_input import require_supported_loader
else:  # Preserve the original developer CLI and its regression suite.
    sys.path.insert(0, str(DISCOVERY))
    from export_calendars import DEFAULT_BASE, NoRedirect, ReadError, read_secret, record_id
    from event_test import hdl_datetime, make_archive, monitor, successful
    from validate_pilot import create_plan
    from build_input import require_supported_loader

API = "/hcmRestApi/resources/11.13.18.05"
CALENDARS = API + "/absenceCalendars"
DATASETS = API + "/dataLoadDataSets"
HDL_ACTION_CONTENT_TYPE = "application/vnd.oracle.adf.action+json"
SOURCE_CALENDAR = "810000331861655"
SOURCE_ASSOCIATION = "810000331861676"
KNOWN_CODE = "HOLIDAY_A"
EVENT_FIELDS = "CalendarCalEventId,CalendarEventId,CalendarId,Name,ShortCode,StartDateTime,EndDateTime,AllDayFlag,HalfDayForElapsed,CategoryCode,EventType"
CALENDAR_FIELDS = "CalendarId,Name,Description,StartDate,EndDate"
SAFE_RESOURCE = re.compile(re.escape(API) + r"(?:/absenceCalendars(?:/[0-9]+(?:/child/AbsenceCalendarEvent(?:/[0-9]+)?)?)?|/dataLoadDataSets(?:/[0-9]+(?:/child/businessObjects)?)?(?:/action/(?:uploadFile|createFileDataSet))?)")
FLOW_RESOURCE = re.compile(re.escape(API) + r"/flowPatterns(?:/[0-9]+(?:/child/flowParameters|/action/submitFlow)?)?")
FLOW_INSTANCE_RESOURCE = re.compile(re.escape(API) + r"/flowInstances(?:/[0-9]+(?:/child/checklists)?)?")
SOAP_WSDL_PATH = "/hcmService/PayrollProcessingActionService?WSDL"
SOAP_SERVICE_PATH = "/hcmService/PayrollProcessingActionService"
SOAP_TYPES_NS = "http://xmlns.oracle.com/apps/hcm/batchProcesses/core/batchProcessesCoreService/types/"
SOAP_ACTION = "http://xmlns.oracle.com/apps/hcm/batchProcesses/core/batchProcessesCoreService/fetchExtractOutput"
SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
REFRESH_FLOW_ID = "810000151665170"
KEY_MAP_FLOW_ID = "810000151665169"
LEGACY_REFERENCE_EVENT = {"ShortCode": KNOWN_CODE, "CalendarEventId": "810000331861667",
                          "CalendarId": SOURCE_CALENDAR, "CalendarCalEventId": SOURCE_ASSOCIATION}
FLOW_NAMES = {"refresh_flow_id": "Integration Object User Key Refresh",
              "key_map_flow_id": "Integration Object User Key Map Extract"}
WINDOWS_CREDENTIAL_TARGET = "Codex.OracleFusion.SAMPLE-A"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_ERROR_NOT_FOUND = 1168
STORAGE_AUTHORIZATION = ContextVar("creation_storage_authorization", default="")


class PilotError(ReadError):
    pass


class OraclePending(PilotError):
    """A submitted Oracle job is still running; resume status checks next time."""


class EmptyExtractOutput(PilotError):
    """The SOAP service accepted a completed run but exposed no output in the requested mode."""


def progress(client, message):
    callback = getattr(client, "progress_callback", None)
    if callback is not None:
        callback(message)
    if not getattr(client, "show_progress", False):
        client.last_stage = message
        return
    current = time.monotonic()
    if message != getattr(client, "last_stage", None) or current - getattr(client, "last_progress", 0) >= 10:
        elapsed = int(current - client.run_started)
        print("[%02d:%02d] %s" % (elapsed // 60, elapsed % 60, message), flush=True)
        client.last_progress = current
    client.last_stage = message


def now(): return datetime.now(timezone.utc).isoformat()
def journal_path(output_dir): return output_dir / ".K12_SAMPLE_A_Create_Pilot_journal.json"


class WINDOWS_CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
                ("Comment", ctypes.c_wchar_p), ("LastWritten", ctypes.c_uint64), ("CredentialBlobSize", ctypes.c_uint32),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
                ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p),
                ("UserName", ctypes.c_wchar_p)]


def validate_oracle_username(username):
    if not username or any(c in username for c in ":\r\n"):
        raise PilotError("A REST username without a colon or line break is required.")
    return username


def windows_credential_api():
    if os.name != "nt":
        raise PilotError("Windows Credential Manager is unavailable on this execution host.")
    try:
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        api.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.POINTER(WINDOWS_CREDENTIALW))]
        api.CredReadW.restype = ctypes.c_int
        api.CredWriteW.argtypes = [ctypes.POINTER(WINDOWS_CREDENTIALW), ctypes.c_uint32]
        api.CredWriteW.restype = ctypes.c_int
        api.CredFree.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError):
        raise PilotError("Windows Credential Manager API is unavailable to the approved execution account.") from None
    return api


def read_windows_oracle_credential():
    api = windows_credential_api()
    credential = ctypes.POINTER(WINDOWS_CREDENTIALW)()
    if not api.CredReadW(WINDOWS_CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
        code = ctypes.get_last_error()
        if code == CRED_ERROR_NOT_FOUND: return None
        raise PilotError("Windows Credential Manager read failed (Win32 error %s)." % code)
    try:
        item = credential.contents
        raw = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize)
        password = raw.decode("utf-16-le")
        return validate_oracle_username(item.UserName or ""), password
    except (UnicodeError, ValueError):
        raise PilotError("The stored SAMPLE-A credential is invalid; rerun local sign-in.") from None
    finally:
        api.CredFree(credential)


def store_windows_oracle_credential(username, password):
    api = windows_credential_api()
    username = validate_oracle_username(username)
    if not password:
        raise PilotError("A nonempty Oracle password is required.")
    blob = ctypes.create_string_buffer(password.encode("utf-16-le"))
    credential = WINDOWS_CREDENTIALW(0, CRED_TYPE_GENERIC, WINDOWS_CREDENTIAL_TARGET, None, 0, len(blob.raw) - 1,
                                    ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)), CRED_PERSIST_LOCAL_MACHINE,
                                    0, None, None, username)
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise PilotError("Windows Credential Manager write failed (Win32 error %s)." % ctypes.get_last_error())


def make_basic_authorization(username, password):
    value = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
    return value


def redact_storage(value, authorization=None):
    """Remove known credentials recursively before diagnostics cross a boundary."""
    auth = authorization if authorization is not None else STORAGE_AUTHORIZATION.get()
    secrets = set()
    if isinstance(auth, str) and auth:
        secrets.add(auth)
        if auth.startswith("Basic "):
            token = auth[6:]
            if token:
                secrets.add(token)
            try:
                decoded = base64.b64decode(token, validate=True).decode("utf-8")
                secrets.add(decoded)
                _, separator, password = decoded.partition(":")
                if separator and password:
                    secrets.update((password, html.escape(password), html.escape(password, quote=False),
                                    urllib.parse.quote(password, safe=""), urllib.parse.quote_plus(password)))
            except (ValueError, UnicodeError):
                pass
    ordered = sorted((secret for secret in secrets if secret), key=len, reverse=True)
    def scrub(item):
        if isinstance(item, str):
            for secret in ordered:
                item = item.replace(secret, "[redacted]")
            return re.sub(r"(?i)\bBasic\s+[A-Za-z0-9+/]+={0,2}", "Basic [redacted]", item)
        if isinstance(item, dict):
            return {scrub(key) if isinstance(key, str) else key: scrub(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [scrub(child) for child in item]
        return item
    return scrub(value)


def redact_excerpt(value, authorization=None):
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    text = redact_storage(text, authorization)
    text = re.sub(r"(?is)(<(?:\w+:)?(?:Password|Username|BinarySecurityToken)\b[^>]*>).*?(</[^>]+>)", r"\1<redacted>\2", text)
    text = re.sub(r"(?i)(authorization|password|token|cookie)\s*[:=]\s*[^\r\n]+", r"\1=<redacted>", text)
    text = " ".join(text.split())
    return text[:2000]


def bounded_error_body(raw, content_encoding=""):
    """Decode a bounded error body once; Oracle sometimes gzip-compresses JSON error pages."""
    compressed = "gzip" in content_encoding.casefold() or raw.startswith(b"\x1f\x8b")
    if not compressed:
        return raw, False
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
            value = archive.read(32769)
    except (OSError, EOFError):
        return raw, False
    return value, len(value) > 32768


def xml_local_name(value):
    return value.rsplit("}", 1)[-1]


def read_response(response, limit):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise PilotError("Oracle response exceeded the %d-byte limit." % limit)
    if "gzip" in response.headers.get("Content-Encoding", "").lower() or raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
            raw = archive.read(limit + 1)
        if len(raw) > limit:
            raise PilotError("Decoded Oracle response exceeded its size limit.")
    return raw


def soap_fault_summary(raw):
    """Return a bounded SOAP fault summary without retaining raw response XML."""
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        return ""
    faults = [node for node in root.iter() if xml_local_name(node.tag) == "Fault"]
    values = []
    for node in (child for fault in faults for child in fault.iter()):
        if xml_local_name(node.tag) in ("faultcode", "faultstring", "Value", "Text", "Message", "detail") and node.text:
            values.append("%s=%s" % (xml_local_name(node.tag), " ".join(node.text.split())[:1000]))
    return redact_excerpt("; ".join(dict.fromkeys(values))) if values else ("SOAP Fault (no text)" if faults else "")


def extract_result(raw):
    """Unwrap SOAP result text. Extract output can be escaped XML or base64 XML."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise PilotError("Extract Actions returned non-XML SOAP content.") from None
    fault = soap_fault_summary(raw)
    if fault:
        raise PilotError("Extract Actions returned SOAP fault: %s" % fault)
    results = [node for node in root.iter() if xml_local_name(node.tag) == "result"]
    if len(results) != 1:
        raise EmptyExtractOutput("Extract Actions returned an empty fetchExtractOutputResponse.")
    if len(results[0]) == 1:
        return results[0][0]
    result = results[0].text or ""
    result = result.strip()
    if not result:
        raise EmptyExtractOutput("Extract Actions returned an empty extract result.")
    if not result.lstrip().startswith("<"):
        try:
            decoded = base64.b64decode(result, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise PilotError("Extract Actions result was neither XML nor base64-encoded XML.") from None
        result = decoded
    try:
        return ET.fromstring(result.encode("utf-8"))
    except ET.ParseError:
        raise PilotError("Extract Actions result did not contain parseable XML.") from None


def resolve_calendar_event_ids(xml_root, codes, require_all=True):
    """Resolve CalendarEvent user keys from the documented key-map hierarchy.

    SAMPLE-A returns ``Integration_Object/Object_Name``, followed by one or
    more ``Integration_Key`` entries.  Each key carries its numeric
    ``Key_Details/Surrogate_Id`` and user key attributes under
    ``User_Keys/User_Key_Attribute``.  Keep those fields in the same
    Integration_Key context; never infer an ID from element position.
    """
    wanted, resolved, object_count = set(codes), {}, 0

    def direct_text(element, name):
        matches = [child.text.strip() for child in element
                   if xml_local_name(child.tag) == name and child.text and child.text.strip()]
        if len(matches) > 1:
            raise PilotError("Key-map record has ambiguous %s fields; inspect the saved XML." % name)
        return matches[0] if matches else None

    for integration_object in xml_root.iter():
        if xml_local_name(integration_object.tag) != "Integration_Object":
            continue
        if direct_text(integration_object, "Object_Name") != "CalendarEvent":
            continue
        object_count += 1
        for integration_key in [child for child in integration_object
                                if xml_local_name(child.tag) == "Integration_Key"]:
            details_nodes = [child for child in integration_key if xml_local_name(child.tag) == "Key_Details"]
            if len(details_nodes) != 1:
                raise PilotError("Key-map record has missing or ambiguous identity fields; inspect the saved XML.")
            details = details_nodes[0]
            event_id = direct_text(details, "Surrogate_Id") if details is not None else None
            user_key_nodes = [child for child in integration_key if xml_local_name(child.tag) == "User_Keys"]
            if len(user_key_nodes) != 1:
                raise PilotError("Key-map record has missing or ambiguous identity fields; inspect the saved XML.")
            user_keys = user_key_nodes[0]
            code_values = []
            if user_keys is not None:
                for attribute in user_keys:
                    if xml_local_name(attribute.tag) != "User_Key_Attribute":
                        continue
                    if direct_text(attribute, "Name") == "ShortCode":
                        value = direct_text(attribute, "Value")
                        if value:
                            code_values.append(value)
            # Do not assume that HDL user-key matching is case-sensitive. A
            # case-only match must not look like an available new event code.
            folded_wanted = {code.casefold(): code for code in wanted}
            if any(code.casefold() in folded_wanted and code != folded_wanted[code.casefold()] for code in code_values):
                raise PilotError("Key-map contains an event code differing only by letter case; resolve the existing identity before creating anything.")
            # Validate every record, including nonmatching codes: malformed output
            # must never be interpreted as proof that a requested identity is absent.
            if len(code_values) != 1 or not event_id:
                raise PilotError("Key-map record has missing or ambiguous identity fields; inspect the saved XML.")
            if not event_id.isdigit() or int(event_id) <= 0:
                raise PilotError("Key-map record contains an invalid CalendarEvent ID.")
            matching_codes = set(code_values) & wanted
            if not matching_codes:
                continue
            code = next(iter(matching_codes))
            if code in resolved and resolved[code] != event_id:
                raise PilotError("Key-map XML returned multiple CalendarEventId values for %s." % code)
            resolved[code] = event_id
    if not object_count:
        raise PilotError("Key-map XML did not contain an explicit CalendarEvent Integration_Object; no absence or identity is proven.")
    missing = sorted(wanted - set(resolved))
    if missing and require_all:
        raise PilotError("Key-map XML did not contain explicit CalendarEventId records for: %s. Inspect the saved XML before creating anything." % ", ".join(missing))
    return resolved


def capture_soap_response(client, raw, log):
    """Save response evidence before parsing. Never retain a SOAP security header."""
    log["response_byte_count"] = len(raw)
    log["response_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        log["response_structure"] = {"parseable_xml": False}
        return  # Do not store arbitrary HTML login pages, cookies or non-XML text.
    namespaces = (SOAP_ENVELOPE_NS, "http://www.w3.org/2003/05/soap-envelope")
    bodies = [child for child in root if child.tag in {"{" + ns + "}Body" for ns in namespaces}]
    log["response_structure"] = {
        "root": root.tag, "root_children": [child.tag for child in root][:20],
        "soap_body_count": len(bodies),
        "result_element_count": sum(xml_local_name(n.tag) == "result" for n in root.iter()),
    }
    if root.tag not in {"{" + ns + "}Envelope" for ns in namespaces} or len(bodies) != 1:
        return  # Shape alone is sufficient to recognize an unexpected login/error page.
    body = ET.fromstring(ET.tostring(bodies[0], encoding="utf-8"))
    log["response_structure"]["body_children"] = [child.tag for child in body][:20]
    log["response_structure"]["body_elements"] = sorted({node.tag for node in body.iter()})[:100]
    sensitive = {"security", "password", "username", "binarysecuritytoken", "authorization", "cookie", "token"}
    for node in body.iter():
        if xml_local_name(node.tag).casefold() in sensitive:
            node.clear(); node.text = "[redacted]"
    sanitized = redact_storage(ET.tostring(body, encoding="unicode"), getattr(client, "authorization", ""))
    log["response_body_xml"] = sanitized[:16000]
    log["response_body_truncated"] = len(sanitized) > 16000
    folder = getattr(client, "diagnostic_dir", None)
    if folder is not None:
        folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("Oracle_Extract_Response_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".xml")
        path.write_text(sanitized, encoding="utf-8")
        log["response_body_file"] = str(path)


class Client:
    """Host-locked, redirect-blocking client. Routes are constrained before every request."""
    def __init__(self, authorization, config=None):
        self.authorization, self.requests = authorization, []
        # Only the retained developer CLI gets legacy defaults. Every launcher
        # invocation passes explicit profile configuration; there is no pod fallback.
        selected = {"environment": DEFAULT_BASE, "integration": {
            "refresh_flow_id": REFRESH_FLOW_ID, "key_map_flow_id": KEY_MAP_FLOW_ID,
            "reference_event": LEGACY_REFERENCE_EVENT}} if config is None else deepcopy(config)
        if "environment" not in selected:
            raise PilotError("An explicit client configuration requires its environment URL.")
        self.environment = normalize_origin(selected["environment"])
        self.profile_id = selected.get("profile_id", "")
        integration = selected.get("integration", {})
        if not isinstance(integration, dict):
            raise PilotError("Profile integration settings must be an object.")
        for field in FLOW_NAMES:
            value = integration.get(field)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)):
                raise PilotError(field + " must be a positive Oracle flow ID or omitted for discovery.")
            setattr(self, field, value)
        self.reference_event = deepcopy(integration.get("reference_event"))
        if self.reference_event is not None:
            reference = self.reference_event
            if not isinstance(reference, dict) or not isinstance(reference.get("ShortCode"), str) or not reference["ShortCode"].strip():
                raise PilotError("The optional reference_event requires a ShortCode and CalendarEventId.")
            for field in ("CalendarEventId", "CalendarId", "CalendarCalEventId"):
                value = reference.get(field)
                if (field == "CalendarEventId" or value is not None) and (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)):
                    raise PilotError("reference_event." + field + " must be a positive Oracle ID.")
        self.advertised_resources = set()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def request(self, method, resource, params=None, payload=None, content_type="application/json"):
        if getattr(self, "creation_requests_disabled", False) and method != "GET":
            raise PilotError("Diagnostic mode blocks job submissions and Oracle record writes.")
        if (not SAFE_RESOURCE.fullmatch(resource) and not FLOW_RESOURCE.fullmatch(resource) and
                not FLOW_INSTANCE_RESOURCE.fullmatch(resource) and resource not in self.advertised_resources):
            raise PilotError("Request outside the approved calendar operation scope was blocked.")
        url = self.environment + resource + (("?" + urllib.parse.urlencode(params)) if params else "")
        headers = {"Authorization": self.authorization, "Accept": "application/json", "REST-Framework-Version": "4"}
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = content_type
        log = {"method": method, "resource": resource, "endpoint": self.environment + resource,
               "query_parameters": params or {}, "query": urllib.parse.urlencode(params or {})}
        if payload is not None:
            log["payload_fields"] = sorted(payload)
            log["request_content_type"] = content_type
        self.requests.append(log)
        try:
            with self.opener.open(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=getattr(self, "request_timeout_seconds", 30)) as response:
                if response.status not in (200, 201, 202): raise PilotError("Unexpected HTTP %s from Oracle." % response.status)
                raw = read_response(response, 2 * 1024 * 1024)
                value = json.loads(raw)
                if not isinstance(value, dict): raise PilotError("Oracle returned an invalid JSON object.")
                log["http_status"] = response.status
                if isinstance(value.get("items"), list):
                    log["response_page"] = {"item_count": len(value["items"]), "offset": value.get("offset"),
                                            "limit": value.get("limit"), "hasMore": value.get("hasMore")}
                return value
        except urllib.error.HTTPError as error:
            log["http_status"] = error.code
            log["content_type"] = error.headers.get("Content-Type", "")
            log["content_encoding"] = error.headers.get("Content-Encoding", "")
            body, over_limit = bounded_error_body(error.read(32769), log["content_encoding"])
            if over_limit or len(body) > 32768:
                detail = "Oracle error response exceeded the diagnostic limit."
            else:
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeError):
                    value = None
                allowed = ("code", "errorCode", "title", "detail", "message", "severity", "localizedMessage")
                details = []
                def collect(item):
                    if isinstance(item, dict):
                        for key in allowed:
                            if isinstance(item.get(key), (str, int, float)):
                                details.append("%s=%s" % (key, str(item[key])[:1000]))
                        for child in item.values(): collect(child)
                    elif isinstance(item, list):
                        for child in item: collect(child)
                collect(value)
                if details:
                    detail = "; ".join(dict.fromkeys(details))
                else:
                    detail = "Oracle returned no parseable diagnostic fields."
                    log["response_body_excerpt"] = redact_excerpt(body, self.authorization)
            detail = redact_storage(detail, self.authorization)
            log["oracle_error"] = detail
            for header, label in (("x-oracle-dms-ecid", "ecid"), ("x-oracle-dms-rid", "rid")):
                value = error.headers.get(header)
                if value: log[label] = value
            messages = {400: "Oracle rejected the request.", 401: "Authentication was not accepted.",
                        403: "Forbidden; cause unconfirmed.", 404: "Resource is hidden or unavailable."}
            failure = PilotError("HTTP %s %s endpoint=%s query=%s: %s OracleError=%s" % (error.code, method, self.environment + resource, urllib.parse.urlencode(params or {}),
                messages.get(error.code, "Oracle returned an error."), detail))
            failure.http_status = error.code
            raise failure from None
        except (urllib.error.URLError, ssl.SSLError, OSError, ValueError):
            raise PilotError("TLS, network, or JSON failure while contacting Oracle.") from None

    def get(self, resource, params): return self.request("GET", resource, params)
    def post(self, resource, payload, content_type="application/json"):
        return self.request("POST", resource, payload=payload, content_type=content_type)

    def calendars_by_name(self, name):
        data = self.get(CALENDARS, {"q": "Name='" + name.replace("'", "''") + "'", "fields": CALENDAR_FIELDS,
                                    "onlyData": "true", "limit": 10, "offset": 0})
        if not isinstance(data.get("items"), list) or data.get("hasMore") is not False:
            raise PilotError("Calendar name lookup was incomplete; no create is allowed.")
        return [row for row in data["items"] if row.get("Name") == name]

    def events_for_calendar(self, calendar_id, code):
        return [row for row in self.all_calendar_events(calendar_id) if row.get("ShortCode") == code]

    def all_calendar_events(self, calendar_id):
        rows, offset = [], 0
        for _ in range(50):
            value = self.get(CALENDARS + "/" + calendar_id + "/child/AbsenceCalendarEvent",
                             {"fields": EVENT_FIELDS, "limit": 100, "offset": offset})
            items = value.get("items")
            if not isinstance(items, list) or not isinstance(value.get("hasMore"), bool):
                raise PilotError("Event collection was incomplete; no create is allowed.")
            rows.extend(items)
            if value["hasMore"] is False:
                return rows
            if not items:
                raise PilotError("Event collection made no pagination progress.")
            offset += len(items)
        raise PilotError("Event collection exceeded the pagination bound.")

    def source_event_pages(self):
        """Known-good collection GET: no item endpoint, no link filter, local MLK selection."""
        reference = getattr(self, "reference_event", LEGACY_REFERENCE_EVENT)
        if not reference or not reference.get("CalendarId"):
            raise PilotError("No reference calendar is configured for this environment.")
        resource = CALENDARS + "/" + reference["CalendarId"] + "/child/AbsenceCalendarEvent"
        rows, offset = [], 0
        while True:
            data = self.get(resource, {"fields": EVENT_FIELDS, "limit": 100, "offset": offset})
            items = data.get("items")
            if not isinstance(items, list):
                raise PilotError("Oracle returned invalid source event collection data.")
            rows.extend(items)
            if data.get("hasMore") is not True:
                return rows
            if not items or len(rows) >= 1000:
                raise PilotError("Source event pagination was incomplete; pilot stopped.")
            offset += len(items)

    def visible_calendar_ids(self):
        data = self.get(CALENDARS, {"fields": "CalendarId", "onlyData": "true", "limit": 100, "offset": 0})
        if data.get("hasMore") is not False or not isinstance(data.get("items"), list):
            raise PilotError("Visible calendar inventory was incomplete; pilot stopped.")
        return [record_id(row.get("CalendarId")) for row in data["items"]]

    def existing_master(self, code):
        """Supported resolver for already-linked masters; never guesses an unlinked endpoint."""
        matches = []
        for calendar_id in self.visible_calendar_ids():
            matches += self.events_for_calendar(calendar_id, code)
        ids = {record_id(row.get("CalendarEventId")) for row in matches}
        if len(ids) > 1: raise PilotError("Multiple master IDs were returned for %s; pilot stopped." % code)
        return (next(iter(ids)), matches) if ids else (None, [])

    def source_master_lookup_link(self):
        reference = getattr(self, "reference_event", LEGACY_REFERENCE_EVENT)
        if not reference or not reference.get("CalendarId"):
            return None
        rows = [row for row in self.source_event_pages() if row.get("ShortCode") == reference["ShortCode"]]
        if len(rows) != 1:
            raise PilotError("The reference calendar did not return exactly one configured event association.")
        row = rows[0]
        if (record_id(row.get("CalendarEventId")) != reference["CalendarEventId"] or
                (reference.get("CalendarCalEventId") and record_id(row.get("CalendarCalEventId")) != reference["CalendarCalEventId"])):
            raise PilotError("The reference event does not match this environment's configured IDs.")
        # An advertised master-item link is the only permitted basis for an unlinked-master resolver.
        for link in row.get("links", []):
            href = link.get("href") if isinstance(link, dict) else ""
            parsed = urllib.parse.urlsplit(href)
            if (isinstance(link, dict) and link.get("rel") not in ("self", "canonical") and parsed.scheme == "https" and
                    parsed.netloc == urllib.parse.urlsplit(getattr(self, "environment", DEFAULT_BASE)).netloc and parsed.path.startswith(API + "/") and
                    "calendarevent" in parsed.path.casefold()):
                item = parsed.path
                parent = item.rsplit("/", 1)[0]
                self.advertised_resources.add(parent)
                return parent
        return None

    def paged_get(self, resource, fields, page_limit=100):
        """Bounded collection retrieval using response offset plus returned item count."""
        rows, pages, seen_offsets, seen_pages, offset = [], [], set(), set(), 0
        for number in range(1, 51):
            data = self.get(resource, {"fields": fields, "onlyData": "true", "limit": page_limit, "offset": offset})
            items, returned_offset, has_more = data.get("items"), data.get("offset"), data.get("hasMore")
            if not isinstance(items, list) or not isinstance(returned_offset, int) or not isinstance(has_more, bool):
                raise PilotError("Pagination stopped: Oracle omitted items, offset, or hasMore on page %d." % number)
            page = {"page": number, "requested_offset": offset, "returned_offset": returned_offset,
                    "requested_limit": page_limit, "returned_limit": data.get("limit"),
                    "item_count": len(items), "hasMore": has_more}
            pages.append(page)
            fingerprint = json.dumps(items, sort_keys=True, separators=(",", ":"), default=str)
            if returned_offset in seen_offsets or fingerprint in seen_pages:
                raise PilotError("Pagination stopped: Oracle repeated a previously returned page.")
            seen_offsets.add(returned_offset); seen_pages.add(fingerprint); rows.extend(items)
            if not has_more:
                return rows, {"status": "complete", "pages": pages, "stopping_reason": "hasMore=false"}
            if not items:
                raise PilotError("Pagination stopped: hasMore=true with zero returned items.")
            next_offset = returned_offset + len(items)
            if next_offset <= offset:
                raise PilotError("Pagination stopped: returned offset and item count made no forward progress.")
            offset = next_offset
        raise PilotError("Pagination stopped: 50-page safety bound reached.")

    def key_map_flow_metadata(self):
        """Resolve only this profile's flows; absent IDs require unique active exact names."""
        progress(self, "Checking this environment's extract flow definitions...")
        selected = {"refresh_flow_id": getattr(self, "refresh_flow_id", REFRESH_FLOW_ID),
                    "key_map_flow_id": getattr(self, "key_map_flow_id", KEY_MAP_FLOW_ID)}
        discovery = None
        if any(value is None for value in selected.values()):
            rows, discovery = self.paged_get(API + "/flowPatterns", "FlowId,FlowPattern,FlowStatus")
            for field, expected_name in FLOW_NAMES.items():
                if selected[field] is not None:
                    continue
                candidates = [row for row in rows if row.get("FlowPattern") == expected_name
                              and str(row.get("FlowStatus", "")).casefold() in ("a", "active")]
                if len(candidates) != 1:
                    raise PilotError("This environment must expose exactly one active flow named '" + expected_name +
                                     "', or its administrator must configure the exact flow ID in the selected profile.")
                identity = record_id(candidates[0].get("FlowId"))
                if not identity or not re.fullmatch(r"[1-9][0-9]*", identity):
                    raise PilotError("Discovered flow has an invalid ID.")
                selected[field] = identity
        if len(set(selected.values())) != 2:
            raise PilotError("Refresh and Key Map Extract must identify different Oracle flows.")
        wanted = []
        for field, flow_id in selected.items():
            row = self.get(API + "/flowPatterns/" + flow_id, {})
            if record_id(row.get("FlowId")) != flow_id:
                raise PilotError("Flow metadata returned a different ID from the selected flow.")
            name = str(row.get("FlowPattern") or "")
            expected_words = ("integration object", "refresh") if field == "refresh_flow_id" else ("integration object", "map extract")
            if not all(word in name.casefold() for word in expected_words):
                raise PilotError("Configured flow does not identify the required integration object operation.")
            parameter_rows, parameter_pagination = self.paged_get(API + "/flowPatterns/" + flow_id + "/child/flowParameters",
                                                                 "FlowParameterId,FlowParameter,Display,DisplayFormat,ParameterBasis,Usage,Sequence,LegislationCode,LegislativeDataGroupId")
            wanted.append({"FlowId": flow_id, "FlowPattern": name, "BaseFlowName": row.get("BaseFlowName"),
                           "FlowStatus": row.get("FlowStatus"), "LDGRequired": row.get("LDGRequired"),
                           "parameters": parameter_rows, "parameter_pagination": parameter_pagination})
            setattr(self, field, flow_id)
        result = {"flows": wanted, "lookup": "configured_flow_ids" if discovery is None else "unique_active_flow_names",
                  "search_result": "matching_extracts_found", "selected_flow_ids": selected}
        if discovery is not None:
            result["discovery_pagination"] = discovery
        return result

    def flow_metadata_by_id(self, metadata, flow_id, expected_words):
        candidates = [row for row in metadata["flows"] if row["FlowId"] == flow_id]
        if len(candidates) != 1 or not all(word in candidates[0]["FlowPattern"].casefold() for word in expected_words):
            raise PilotError("Configured flow %s was not returned as the expected active tenant flow." % flow_id)
        flow = candidates[0]
        if str(flow.get("FlowStatus", "")).casefold() not in ("a", "active"):
            raise PilotError("Configured flow %s is not active." % flow_id)
        parameters = [row for row in flow["parameters"] if row.get("FlowParameter") == "Business Object"]
        if len(parameters) != 1 or str(parameters[0].get("Display", "")).casefold() not in ("m", "mandatory"):
            raise PilotError("Configured flow %s did not return exactly one mandatory Business Object parameter." % flow_id)
        parameter_id = record_id(parameters[0].get("FlowParameterId"))
        if not parameter_id or not parameter_id.isdigit():
            raise PilotError("Configured flow %s returned an invalid Business Object parameter identifier." % flow_id)
        return flow, parameter_id

    def submit_flow(self, flow_id, instance_name, business_object_parameter_id):
        payload = {"flowInstanceName": instance_name,
                   "parameterIdValues": {business_object_parameter_id: "CalendarEvent"}}
        value = self.post(API + "/flowPatterns/" + flow_id + "/action/submitFlow", payload,
                          "application/vnd.oracle.adf.action+json")
        flow_instance_id = record_id(value.get("result"))
        if not flow_instance_id or not flow_instance_id.isdigit():
            raise PilotError("Flow submission did not return a numeric FlowInstanceId.")
        return {"FlowId": flow_id, "FlowInstanceId": flow_instance_id, "FlowInstanceName": instance_name,
                "parameterIdValues": {business_object_parameter_id: "CalendarEvent"}, "submitted_utc": now()}

    def flow_instance(self, flow_instance_id):
        # Item schema varies by tenant. Omit fields and onlyData: the returned links remain available for downstream reconciliation.
        return self.get(API + "/flowInstances/" + flow_instance_id, {})

    def flow_checklists(self, flow_instance_id):
        value = self.get(API + "/flowInstances/" + flow_instance_id + "/child/checklists", {"limit": 100, "offset": 0})
        items = value.get("items")
        if not isinstance(items, list):
            raise PilotError("Flow checklist response did not contain a collection.")
        return items

    def soap_contract(self):
        """Read the tenant WSDL and accept only its advertised HTTPS SOAP endpoint."""
        progress(self, "Reading Oracle's extract output service definition...")
        url = self.environment + SOAP_WSDL_PATH
        log = {"method": "GET", "endpoint": self.environment + "/hcmService/PayrollProcessingActionService", "soap_operation": "WSDL"}
        self.requests.append(log)
        try:
            with self.opener.open(urllib.request.Request(url, headers={"Authorization": self.authorization, "Accept": "text/xml"}), timeout=getattr(self, "request_timeout_seconds", 30)) as response:
                raw = read_response(response, 2 * 1024 * 1024)
                log["http_status"] = response.status; log["content_type"] = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            log["http_status"] = error.code; log["content_type"] = error.headers.get("Content-Type", "")
            excerpt = error.read(32768); log["response_body_excerpt"] = redact_excerpt(excerpt, self.authorization)
            raise PilotError("Extract Actions WSDL HTTP %s; SOAP retrieval was not attempted." % error.code) from None
        try:
            root = ET.fromstring(raw)
            # Evidence only: record advertised policies without guessing which alternative
            # the tenant enforces, fetching external policy URLs, or logging the WSDL.
            log["advertised_policy_references"] = sorted({n.get("URI", "")[:500] for n in root.iter()
                                                           if xml_local_name(n.tag) == "PolicyReference"})[:30]
            log["advertised_security_assertions"] = sorted({xml_local_name(n.tag) for n in root.iter()
                if xml_local_name(n.tag) in ("IncludeTimestamp", "UsernameToken", "TransportBinding",
                                             "AsymmetricBinding", "SymmetricBinding", "SamlToken")})
            wsdl, soap = "{http://schemas.xmlsoap.org/wsdl/}", "{http://schemas.xmlsoap.org/wsdl/soap/}"
            outputs = [output for port in root.findall(wsdl + "portType")
                       for op in port.findall(wsdl + "operation") if op.get("name") == "fetchExtractOutput"
                       for output in op.findall(wsdl + "output")]
            message_names = {output.get("message", "").rsplit(":", 1)[-1] for output in outputs}
            log["extract_output_messages"] = [{"name": msg.get("name"), "parts": [dict(part.attrib) for part in msg.findall(wsdl + "part")]}
                                                for msg in root.findall(wsdl + "message") if msg.get("name") in message_names]
            xsd = "{http://www.w3.org/2001/XMLSchema}"
            log["schema_locations"] = [n.get("schemaLocation") for n in root.iter()
                                       if n.tag in (xsd + "import", xsd + "include") and n.get("schemaLocation")][:20]
            log["inline_extract_response_schemas"] = [ET.tostring(n, encoding="unicode")[:8000] for n in root.iter()
                                                      if n.tag == xsd + "element" and n.get("name") == "fetchExtractOutputResponse"][:3]
            candidates = set()
            for port in root.findall(".//" + wsdl + "port"):
                address = port.find(soap + "address")
                binding_name = port.get("binding", "").rsplit(":", 1)[-1]
                for binding in root.findall(wsdl + "binding"):
                    if binding.get("name") != binding_name or address is None:
                        continue
                    for operation in binding.findall(wsdl + "operation"):
                        soap_op = operation.find(soap + "operation")
                        if operation.get("name") == "fetchExtractOutput" and soap_op is not None:
                            candidates.add((address.get("location", ""), soap_op.get("soapAction", "")))
            if len(candidates) != 1:
                raise PilotError("WSDL did not expose one unambiguous SOAP 1.1 fetchExtractOutput binding.")
            endpoint, action = next(iter(candidates))
        except ET.ParseError:
            raise PilotError("Extract Actions WSDL was not valid XML.") from None
        parsed, expected = urllib.parse.urlsplit(endpoint), urllib.parse.urlsplit(self.environment)
        if (parsed.scheme != "https" or parsed.hostname != expected.hostname or parsed.path != SOAP_SERVICE_PATH
                or (parsed.port or 443) != (expected.port or 443) or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise PilotError("Extract Actions WSDL advertised an unexpected SOAP endpoint; request stopped.")
        if not action or any(c in action for c in '\r\n"'):
            raise PilotError("WSDL fetchExtractOutput binding did not advertise a usable SOAP action.")
        return {"endpoint": endpoint, "soap_action": action,
                "authentication": "HTTPS with Basic credential, timestamp and UsernameToken; tenant acceptance remains to be verified"}

    def fetch_extract_output(self, instance_name, task_name, mode="ARCHIVER"):
        """Read a completed extract; retain response Body diagnostics without request credentials or SOAP headers."""
        if mode not in ("ARCHIVER", "FLOW"):
            raise PilotError("Extract Actions mode must be ARCHIVER or FLOW.")
        contract = self.soap_contract()
        try:
            raw_auth = base64.b64decode(self.authorization.split(" ", 1)[1]).decode("utf-8")
            username, password = raw_auth.split(":", 1)
        except (IndexError, ValueError, UnicodeError):
            raise PilotError("The local credential mechanism did not provide a valid Basic credential for SOAP.") from None
        envelope = ET.Element("{" + SOAP_ENVELOPE_NS + "}Envelope")
        header = ET.SubElement(envelope, "{" + SOAP_ENVELOPE_NS + "}Header")
        security = ET.SubElement(header, "{" + WSSE_NS + "}Security", {"{" + SOAP_ENVELOPE_NS + "}mustUnderstand": "1"})
        # OWSM validates message freshness separately from HTTP Basic authentication.
        # Create after the WSDL GET, in UTC, with a new nonce and IDs per request.
        created_at = datetime.now(timezone.utc)
        created = created_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expires = (created_at + timedelta(minutes=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        message_id = os.urandom(16).hex()
        timestamp = ET.SubElement(security, "{" + WSU_NS + "}Timestamp", {"{" + WSU_NS + "}Id": "TS-" + message_id})
        ET.SubElement(timestamp, "{" + WSU_NS + "}Created").text = created
        ET.SubElement(timestamp, "{" + WSU_NS + "}Expires").text = expires
        token = ET.SubElement(security, "{" + WSSE_NS + "}UsernameToken", {"{" + WSU_NS + "}Id": "UT-" + message_id})
        ET.SubElement(token, "{" + WSSE_NS + "}Username").text = username
        ET.SubElement(token, "{" + WSSE_NS + "}Password", {"Type": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText"}).text = password
        ET.SubElement(token, "{" + WSSE_NS + "}Nonce", {"EncodingType": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"}).text = base64.b64encode(os.urandom(16)).decode("ascii")
        ET.SubElement(token, "{" + WSU_NS + "}Created").text = created
        body = ET.SubElement(envelope, "{" + SOAP_ENVELOPE_NS + "}Body")
        request = ET.SubElement(body, "{" + SOAP_TYPES_NS + "}fetchExtractOutput")
        for name, value in (("instanceName", instance_name), ("taskName", task_name), ("mode", mode)):
            ET.SubElement(request, "{" + SOAP_TYPES_NS + "}" + name).text = value
        message = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        del raw_auth, username, password
        log = {"method": "POST", "endpoint": contract["endpoint"], "soap_operation": "fetchExtractOutput",
               "instance_name": instance_name, "task_name": task_name, "mode": mode,
               "ws_security": {"timestamp_created_utc": created, "timestamp_expires_utc": expires,
                               "username_token": True, "nonce_included": True, "must_understand": True}}
        self.requests.append(log)
        try:
            headers = {"Authorization": self.authorization, "Accept": "text/xml", "Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"' + contract["soap_action"] + '"'}
            soap_timeout = getattr(self, "soap_timeout_seconds", 60)
            progress(self, "Downloading the completed key-map XML (network timeout: %s seconds)..." % soap_timeout)
            with self.opener.open(urllib.request.Request(contract["endpoint"], data=message, headers=headers, method="POST"), timeout=soap_timeout) as response:
                raw = read_response(response, 8 * 1024 * 1024)
                log["http_status"] = response.status; log["content_type"] = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            log["http_status"] = error.code; log["content_type"] = error.headers.get("Content-Type", "")
            raw, oversized = bounded_error_body(error.read(32769), error.headers.get("Content-Encoding", ""))
            capture_soap_response(self, raw, log)
            log["response_body_excerpt"] = "Error body exceeded limit" if oversized else redact_excerpt(raw, self.authorization)
            fault = redact_storage(soap_fault_summary(raw), self.authorization)
            if fault: log["soap_fault"] = fault
            detail = ""
            if "invalidsecurity" in fault.casefold():
                detail = " Oracle rejected the timestamped security header. No authentication fallback or job resubmission was attempted; retained WSDL policy references and UTC timestamps are in this report."
            raise PilotError("Extract Actions fetchExtractOutput HTTP %s%s.%s" % (error.code, (" SOAP fault=" + fault) if fault else "", detail)) from None
        capture_soap_response(self, raw, log)
        return extract_result(raw)

    def advertised_master(self, collection, code):
        data = self.get(collection, {"q": "ShortCode='" + code.replace("'", "''") + "'", "fields": EVENT_FIELDS,
                                     "onlyData": "true", "limit": 10, "offset": 0})
        rows = [row for row in data.get("items", []) if row.get("ShortCode") == code]
        ids = {record_id(row.get("CalendarEventId")) for row in rows}
        if len(ids) != 1: raise PilotError("Advertised master resolver did not return exactly one master for %s." % code)
        return next(iter(ids)), rows[0]

    def create_calendar(self, payload): return self.post(CALENDARS, payload)
    def create_link(self, calendar_id, event_id):
        return self.post(CALENDARS + "/" + calendar_id + "/child/AbsenceCalendarEvent",
                         {"CalendarEventId": int(event_id), "CalendarId": int(calendar_id), "EventType": "EVENT"})

    def upload_and_start(self, archive, label):
        import base64
        uploaded = self.post(DATASETS + "/action/uploadFile", {"content": base64.b64encode(archive.read_bytes()).decode("ascii"), "fileName": archive.name}, HDL_ACTION_CONTENT_TYPE)
        content_id = uploaded.get("result", {}).get("ContentId")
        started = self.post(DATASETS + "/action/createFileDataSet", {"contentId": content_id, "fileAction": "IMPORT_AND_LOAD", "dataSetName": label}, HDL_ACTION_CONTENT_TYPE)
        request_id = str(started.get("result", {}).get("RequestId", ""))
        if not isinstance(content_id, str) or not request_id.isdigit(): raise PilotError("HDL submission did not return ContentId and RequestId.")
        return {"ContentId": content_id, "RequestId": request_id}

    def data_set(self, request_id):
        return self.get(DATASETS + "/" + request_id, {"onlyData": "true", "fields": "RequestId,DataSetStatusCode,ImportStatusCode,LoadStatusCode,ObjectLoadErrorCount"})
    def business_objects(self, request_id):
        return self.get(DATASETS + "/" + request_id + "/child/businessObjects", {"onlyData": "true", "fields": "BusinessObjectName,LoadStatusCode,ObjectLoadErrorCount"}).get("items", [])


def prompt_credentials():
    """A fresh local masked prompt; do not persist the entered secret."""
    username = validate_oracle_username(input("Oracle REST username: ").strip())
    password = read_secret("Oracle REST password (hidden): ")
    value = make_basic_authorization(username, password)
    del password
    return value


def credentials():
    stored = read_windows_oracle_credential()
    if stored:
        username, password = stored
        value = make_basic_authorization(username, password)
        del password
        return value
    return prompt_credentials()


def establish_windows_credential():
    """One local masked setup step. The password is stored only by Windows Credential Manager."""
    username = validate_oracle_username(input("Oracle REST username: ").strip())
    password = ""
    try:
        password = read_secret("Oracle REST password (hidden): ")
        store_windows_oracle_credential(username, password)
        return make_basic_authorization(username, password)
    finally:
        del password


def load_journal(folder, environment=DEFAULT_BASE, profile_id=None):
    environment = normalize_origin(environment)
    path = journal_path(folder)
    if not path.exists():
        value = {"environment": environment, "created_utc": now(), "events": {}, "calendars": {}, "links": {}}
        if profile_id:
            value["profile_id"] = profile_id
        return value
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("environment") != environment:
        raise PilotError("Pilot journal belongs to a different environment.")
    if profile_id and value.get("profile_id") != profile_id:
        raise PilotError("Pilot journal belongs to a different profile; existing state must not be copied between profiles.")
    return value

def save_journal(folder, journal):
    folder.mkdir(parents=True, exist_ok=True)
    journal["updated_utc"] = now()
    temporary = journal_path(folder).with_suffix(".json.tmp")
    temporary.write_text(json.dumps(redact_storage(journal), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temporary, journal_path(folder))

def make_event_archive(folder, events):
    require_supported_loader({"events": events})
    lines = ["METADATA|CalendarEvent|ShortCode|Name|Category|StartDateTime|EndDateTime|HalfDayForElapsed"]
    for event in events:
        lines.append("MERGE|CalendarEvent|%s|%s|PH|%s|%s|N" % (event["ShortCode"], event["Name"], hdl_datetime(event["StartDateTimeLocal"] + "+00:00"), hdl_datetime(event["EndDateTimeLocal"] + "+00:00")))
    import zipfile
    path = folder / ("K12_Create_Pilot_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive: archive.writestr("CalendarEvent.dat", "\n".join(lines) + "\n")
    return path, "\n".join(lines)


def flow_status(value):
    return str(value.get("Status") or value.get("GroupStatus") or "").upper()


def wait_for_flow(client, submission, timeout_seconds=None):
    """Bounded status GETs. A nonterminal flow is retained in the journal and never resubmitted."""
    if timeout_seconds is None:
        timeout_seconds = getattr(client, "poll_timeout_seconds", 300)
    deadline, snapshots = time.monotonic() + timeout_seconds, []
    progress(client, "Checking saved %s job %s..." % (submission.get("kind", "extract"), submission["FlowInstanceId"]))
    while True:
        value = client.flow_instance(submission["FlowInstanceId"])
        if value.get("FlowInstanceId") is not None and str(value["FlowInstanceId"]) != submission["FlowInstanceId"]:
            raise PilotError("Flow status returned a different instance ID.")
        if value.get("FlowName") != submission["FlowInstanceName"]:
            raise PilotError("Flow instance name differs from the recorded submission; reconciliation stopped.")
        # FlowName is the documented REST attribute. The SOAP instanceName is the exact submitted name retained in the journal.
        submission["flow_name"] = value.get("FlowName")
        submission["actual_instance_name"] = submission["FlowInstanceName"]
        if isinstance(value.get("links"), list):
            submission["flow_links"] = [{key: link.get(key) for key in ("rel", "href", "name") if key in link}
                                        for link in value["links"] if isinstance(link, dict)]
        status = flow_status(value)
        snapshots.append({"status": status, "completed_tasks": value.get("CompletedTasks"), "number_of_tasks": value.get("NumberOfTasks")})
        submission["flow_status"] = status; submission["status_snapshots"] = snapshots
        progress(client, "Oracle job %s: %s" % (submission["FlowInstanceId"], status or "pending"))
        if status in ("COMPLETED", "COMPLETE", "SUCCESS", "SUCCEEDED"):
            submission["status"] = "completed"; submission["flow_status"] = status; submission["status_snapshots"] = snapshots
            return value
        if status in ("ERROR", "ERRORS", "C_ERRORS", "FAILED", "CANCELED", "CANCELLED", "STOPPED", "ROLLEDBACK", "IS_ROLLEDBACK", "IS_MFR", "INCOMPLETE", "SKIPPED"):
            submission["status"] = "failed"; submission["flow_status"] = status; submission["status_snapshots"] = snapshots
            raise PilotError("Flow %s completed with status %s." % (submission["FlowInstanceId"], status))
        if time.monotonic() >= deadline:
            submission["status"] = "submitted_outcome_unverified"; submission["status_snapshots"] = snapshots
            raise OraclePending("Flow %s is still %s. Run the same command from this folder to resume status checks; this job will not be submitted again." % (submission["FlowInstanceId"], status or "pending"))
        time.sleep(2)


def reconcile_flow(client, entry, output_dir, journal):
    if not entry.get("FlowInstanceId"):
        name = entry["FlowInstanceName"].replace("'", "''")
        data = client.get(API + "/flowInstances", {"q": "FlowName='" + name + "'", "limit": 10, "offset": 0})
        rows = [r for r in data.get("items", []) if r.get("FlowName") == entry["FlowInstanceName"]
                and str(r.get("BaseFlowId")) == entry["FlowId"]]
        if data.get("hasMore") is not False or len(rows) != 1:
            raise PilotError("Prior flow submission has an uncertain outcome; no resubmission. Name: " + entry["FlowInstanceName"])
        entry["FlowInstanceId"] = record_id(rows[0].get("FlowInstanceId"))
    try:
        wait_for_flow(client, entry)
    finally:
        save_journal(output_dir, journal)


def extract_task_name_from_checklist(client, submission):
    """Use the tenant-returned completed ChecklistName for FLOW mode; never invent a task name."""
    rows = client.flow_checklists(submission["FlowInstanceId"])
    complete = [row for row in rows if str(row.get("Status") or row.get("GroupStatus") or "").upper()
                in ("COMPLETED", "COMPLETE", "SUCCESS", "SUCCEEDED")]
    names = {str(row.get("ChecklistName")).strip() for row in complete
             if isinstance(row.get("ChecklistName"), str) and row.get("ChecklistName").strip()}
    if len(names) != 1:
        fields = sorted({key for row in rows if isinstance(row, dict) for key in row})
        raise PilotError("Completed extract checklist did not return exactly one ChecklistName for FLOW retrieval; fields=%s." % ",".join(fields))
    name = next(iter(names))
    submission["checklist_task_name"] = name
    submission["checklist_task_ids"] = [record_id(row.get("FlowTaskInstanceId")) for row in complete]
    return name


def submit_tracked_flow(client, journal, output_dir, kind, flow_id, parameter_id, generation):
    prefix = "KeyRefresh" if kind == "refresh" else "KeyMap"
    name = "K12_Calendar_" + prefix + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    entry = {"FlowId": flow_id, "FlowInstanceName": name, "kind": kind,
             "generation": generation, "status": "submission_pending",
             "parameterIdValues": {parameter_id: "CalendarEvent"}}
    journal["key_map_submissions"].append(entry)
    save_journal(output_dir, journal)  # Record intent before the POST, including on a lost response.
    entry.update(client.submit_flow(flow_id, name, parameter_id))
    save_journal(output_dir, journal)
    reconcile_flow(client, entry, output_dir, journal)
    return entry


def resolve_with_key_map(client, journal, report, output_dir, codes, require_all=True, generation="baseline", resume_only=False):
    """Resume one archived snapshot, or create a new Refresh+Map pair after a new HDL load.

    ARCHIVER uses the recorded flow instance name. It does not depend on checklist
    task names or CompletedTasks counters (the supplied completed run had count 0).
    """
    submissions = journal.setdefault("key_map_submissions", [])
    for entry in submissions:
        if entry.get("status") not in ("completed", "verified"):
            reconcile_flow(client, entry, output_dir, journal)
    relevant = [e for e in submissions if e.get("generation", "baseline") == generation]
    refreshes = [e for e in relevant if e.get("kind") == "refresh"]
    maps = [e for e in relevant if e.get("kind") == "key_map_extract"]
    if len(refreshes) > 1 or len(maps) > 1:
        raise PilotError("Multiple Refresh/Map submissions exist for this snapshot; reconcile before proceeding.")
    if resume_only and not maps:
        raise PilotError("No existing key-map run is recorded. Resume-only mode submits no jobs.")
    if not resume_only and (not refreshes or not maps):
        metadata = client.key_map_flow_metadata(); report["key_map_flow_metadata"] = metadata
        refresh_id = getattr(client, "refresh_flow_id", REFRESH_FLOW_ID)
        map_id = getattr(client, "key_map_flow_id", KEY_MAP_FLOW_ID)
        _, refresh_parameter = client.flow_metadata_by_id(metadata, refresh_id, ("integration object", "refresh"))
        _, map_parameter = client.flow_metadata_by_id(metadata, map_id, ("integration object", "map extract"))
        if not refreshes:
            refreshes = [submit_tracked_flow(client, journal, output_dir, "refresh", refresh_id, refresh_parameter, generation)]
        if not maps:
            maps = [submit_tracked_flow(client, journal, output_dir, "key_map_extract", map_id, map_parameter, generation)]
    map_run = maps[0]
    # Re-read even a completed journal entry: this also detects a removed/refreshed tenant instance.
    reconcile_flow(client, map_run, output_dir, journal)
    instance_name = map_run.get("actual_instance_name") or map_run["FlowInstanceName"]
    try:
        output = client.fetch_extract_output(instance_name, "", "ARCHIVER")
        map_run["output_mode"] = "ARCHIVER"
    except EmptyExtractOutput as archiver_empty:
        # ARCHIVER is documented to need only the flow instance.  A completed response with no result
        # is not an authorization failure.  Use FLOW only with the actual ChecklistName returned by this tenant.
        map_run["archiver_result"] = "empty"
        map_run["archiver_diagnostic"] = str(archiver_empty)
        task_name = extract_task_name_from_checklist(client, map_run)
        save_journal(output_dir, journal)
        output = client.fetch_extract_output(instance_name, task_name, "FLOW")
        map_run["output_mode"] = "FLOW"
    xml_path = output_dir / ("CalendarEvent_KeyMap_" + map_run["FlowInstanceId"] + ".xml")
    xml_bytes = redact_storage(ET.tostring(output, encoding="unicode"), getattr(client, "authorization", None)).encode("utf-8")
    xml_path.write_bytes(xml_bytes)
    map_run["xml_file"] = xml_path.name
    map_run["xml_sha256"] = hashlib.sha256(xml_bytes).hexdigest()
    report["key_map_output"] = {"file": str(xml_path), "FlowInstanceId": map_run["FlowInstanceId"],
                                 "mode": map_run.get("output_mode"), "element_names": sorted({xml_local_name(n.tag) for n in output.iter()})[:150]}
    reference = getattr(client, "reference_event", LEGACY_REFERENCE_EVENT)
    wanted = list(dict.fromkeys(([reference["ShortCode"]] if reference else []) + list(codes)))
    try:
        resolved = resolve_calendar_event_ids(output, wanted, require_all=require_all)
        if reference and resolved.get(reference["ShortCode"]) != reference["CalendarEventId"]:
            raise PilotError("Key-map output did not confirm the configured reference event identity; no dependent write allowed.")
        map_run["status"] = "verified"
        map_run["resolved_codes"] = resolved
    finally:
        save_journal(output_dir, journal)
    return {code: resolved[code] for code in codes if code in resolved}


def plan_signature(plan):
    value = {"events": sorted(plan["events"], key=lambda e: e["ShortCode"]),
             "calendars": sorted(plan["calendars"], key=lambda c: c["key"]),
             "associations": sorted(plan["associations"], key=lambda a: (a["calendar_key"], a["event_code"]))}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def expected_event(event):
    return {"ShortCode": event["ShortCode"], "Name": event["Name"],
            "StartDateTime": event["StartDateTimeLocal"] + "+00:00",
            "EndDateTime": event["EndDateTimeLocal"] + "+00:00",
            "AllDayFlag": event["expected_readback_AllDayFlag"],
            "HalfDayForElapsed": event["HalfDayForElapsed"], "CategoryCode": event["Category"]}


def verify_event(row, event, event_id):
    if record_id(row.get("CalendarEventId")) != event_id:
        raise PilotError("Event readback returned a different master ID for " + event["ShortCode"])
    for key, wanted in expected_event(event).items():
        actual = row.get(key)
        if key in ("StartDateTime", "EndDateTime"):
            try:
                actual = datetime.fromisoformat(str(actual).replace("Z", "+00:00"))
                wanted = datetime.fromisoformat(wanted)
            except ValueError:
                raise PilotError("Invalid event timestamp in readback.") from None
        if actual != wanted:
            raise PilotError("Event readback differs for %s: %s." % (event["ShortCode"], key))


def calendar_values_match(row, expected):
    """Oracle may represent an intentionally empty optional description as null."""
    for key in ("Name", "StartDate", "EndDate", "Description"):
        actual, wanted = row.get(key), expected[key]
        if key == "Description" and wanted == "" and actual is None:
            actual = ""
        if actual != wanted:
            return False
    return True


def start_master_load(client, plan, missing, journal, report, folder):
    archive, dat = make_event_archive(folder, missing)
    report["CalendarEvent.dat"] = dat
    submission = {"state": "upload_pending", "event_codes": [e["ShortCode"] for e in missing],
                  "archive_file": archive.name, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "label": "K12 Calendar Launcher masters"}
    journal["master_submission"] = submission; save_journal(folder, journal)
    uploaded = client.post(DATASETS + "/action/uploadFile", {
        "content": base64.b64encode(archive.read_bytes()).decode("ascii"), "fileName": archive.name}, HDL_ACTION_CONTENT_TYPE)
    content_id = uploaded.get("result", {}).get("ContentId")
    if not isinstance(content_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", content_id):
        raise PilotError("uploadFile returned no usable ContentId; upload outcome is unresolved.")
    submission.update(ContentId=content_id, state="uploaded")
    save_journal(folder, journal)
    submit_uploaded_master(client, submission, journal, folder)


def submit_uploaded_master(client, submission, journal, folder):
    submission["state"] = "submission_pending"; save_journal(folder, journal)
    value = client.post(DATASETS + "/action/createFileDataSet", {
        "contentId": submission["ContentId"], "fileAction": "IMPORT_AND_LOAD", "dataSetName": submission["label"]}, HDL_ACTION_CONTENT_TYPE)
    request_id = str(value.get("result", {}).get("RequestId", ""))
    if not request_id.isdigit():
        raise PilotError("createFileDataSet returned no RequestId; reconcile the recorded ContentId, do not resubmit.")
    submission.update(RequestId=request_id, state="submitted", submitted_utc=now())
    save_journal(folder, journal)


def reconcile_pre_content_upload_failure(journal, folder):
    """Release only a conclusively rejected pre-ContentId upload attempt.

    An ``upload_pending`` marker protects against accidental repeats after an
    interrupted request.  A local receipt that records HTTP 403 from
    uploadFile, with no ContentId, is different: Oracle rejected it before it
    could create a submission.  Preserve that evidence, then allow the
    corrected request to be attempted once.
    """
    submission = journal.get("master_submission")
    if not submission or submission.get("state") != "upload_pending":
        return False
    archive_sha = submission.get("archive_sha256")
    if not re.fullmatch(r"[a-f0-9]{64}", str(archive_sha or "")):
        raise PilotError("The earlier upload has no valid archive identity; its outcome remains unresolved.")
    receipt_paths = sorted(folder.glob("K12_SAMPLE_A_Create_Pilot_*.json"), reverse=True)
    for path in receipt_paths:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recorded = receipt.get("journal", {}).get("master_submission", {})
        if recorded.get("archive_sha256") != archive_sha:
            continue
        uploads = [request for request in receipt.get("requests", [])
                   if request.get("method") == "POST" and request.get("resource") == DATASETS + "/action/uploadFile"]
        if len(uploads) != 1 or uploads[0].get("http_status") != 403:
            continue
        if "ContentId" in recorded or "ContentId" in uploads[0]:
            raise PilotError("The prior upload may have created a ContentId; reconciliation must not retry it.")
        journal.setdefault("reconciled_upload_failures", []).append({
            "receipt": path.name, "archive_sha256": archive_sha, "http_status": 403,
            "endpoint": uploads[0].get("endpoint"), "ecid": uploads[0].get("ecid"),
            "rid": uploads[0].get("rid"), "outcome": "forbidden_before_content_id",
            "reconciled_utc": now(),
        })
        del journal["master_submission"]
        save_journal(folder, journal)
        return True
    raise PilotError("The earlier upload outcome is unresolved; no new upload or load was submitted.")


def monitor_master_load(client, journal, report, folder, timeout=None):
    submission = journal["master_submission"]
    if submission.get("state") == "upload_pending":
        raise PilotError("The earlier upload outcome is unresolved; no new upload or load was submitted.")
    if submission.get("state") == "uploaded":
        submit_uploaded_master(client, submission, journal, folder)
    content_id = submission.get("ContentId", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", content_id):
        raise PilotError("Existing HDL submission lacks a usable ContentId; no resubmission.")
    if timeout is None:
        timeout = getattr(client, "poll_timeout_seconds", 300)
    deadline = time.monotonic() + timeout
    progress(client, "Checking the saved event load %s..." % content_id)
    while True:
        request_id = str(submission.get("status_RequestId") or submission.get("RequestId") or "")
        status = None
        if request_id.isdigit():
            try:
                status = client.data_set(request_id)
            except PilotError as error:
                if getattr(error, "http_status", None) != 404:
                    raise
        if status is None:
            page = client.get(DATASETS, {"q": "ContentId='" + content_id.replace("'", "''") + "'", "limit": 10, "offset": 0})
            items = page.get("items")
            if not isinstance(items, list) or page.get("hasMore") is not False:
                raise PilotError("HDL ContentId lookup was incomplete; submission remains unresolved.")
            matches = [r for r in items if str(r.get("ContentId")) == content_id]
            if len(matches) > 1:
                raise PilotError("Multiple data sets share the recorded ContentId; no resubmission.")
            if not matches:
                # A just-created data set can be temporarily hidden from both its
                # RequestId route and the ContentId collection.  That is an
                # observation to persist and poll, not proof that the submitted
                # load failed or permission to submit it again.
                submission["last_status_lookup"] = {
                    "state": "not_visible_by_content_id",
                    "ContentId": content_id,
                    "RequestId": request_id if request_id.isdigit() else None,
                    "checked_utc": now(),
                }
                save_journal(folder, journal)
                progress(client, "Submitted event load %s is not visible yet; polling without resubmission." % content_id)
                if time.monotonic() >= deadline:
                    raise OraclePending("The submitted event load is not visible by ContentId yet. Resume the same request to continue status checks; this load will not be submitted again.")
                time.sleep(5)
                continue
            status = matches[0]
            request_id = record_id(status.get("RequestId"))
            if not request_id or not request_id.isdigit():
                raise PilotError("HDL lookup returned no usable status RequestId.")
            submission["status_RequestId"] = request_id
        submission["last_data_set_status"] = status
        save_journal(folder, journal)
        progress(client, "Oracle event load %s: %s" % (content_id, status.get("DataSetStatusCode") or "pending"))
        if status.get("DataSetStatusCode") in ("ORA_COMPLETE", "ORA_SUCCESS", "ORA_IN_ERROR", "ORA_CANCELLED", "ORA_TERMINATED"):
            objects = client.business_objects(request_id)
            report["master_load"] = {"data_set": status, "business_objects": objects}
            completed = status.get("DataSetStatusCode") in ("ORA_COMPLETE", "ORA_SUCCESS")
            object_ok = [item for item in objects if re.sub(r"\s+", "", str(item.get("BusinessObjectName", ""))) == "CalendarEvent"]
            load_ok = (completed and status.get("ImportStatusCode") == "SUCCESS" and status.get("LoadStatusCode") == "SUCCESS"
                       and status.get("ObjectLoadErrorCount", 0) in (0, None) and len(object_ok) == 1
                       and object_ok[0].get("LoadStatusCode") == "SUCCESS" and object_ok[0].get("ObjectLoadErrorCount", 0) in (0, None))
            if not load_ok:
                submission["state"] = "load_failed"; save_journal(folder, journal)
                raise PilotError("CalendarEvent HDL did not complete successfully; dependent writes stopped.")
            submission["state"] = "loaded"; save_journal(folder, journal)
            return
        if time.monotonic() >= deadline:
            raise OraclePending("The event load is still pending. Run the same command from this folder to resume status checks; this load will not be submitted again.")
        time.sleep(5)


def verify_pilot(client, plan, journal):
    events = {e["ShortCode"]: e for e in plan["events"]}
    counts = {"events": 0, "calendars": 0, "links": 0}
    seen_events = set()
    for calendar in plan["calendars"]:
        stored = journal["calendars"].get(calendar["key"], {})
        calendar_id = stored.get("CalendarId")
        if not calendar_id:
            raise PilotError("Pilot is incomplete: missing calendar " + calendar["key"])
        row = client.get(CALENDARS + "/" + calendar_id, {})
        if record_id(row.get("CalendarId")) != calendar_id or not calendar_values_match(row, calendar):
            raise PilotError("Calendar readback differs for " + calendar["key"])
        desired = [a for a in plan["associations"] if a["calendar_key"] == calendar["key"]]
        rows = client.all_calendar_events(calendar_id)
        if len(rows) != len(desired):
            raise PilotError("Calendar event count differs for " + calendar["key"])
        for link in desired:
            code = link["event_code"]
            event_id = journal["events"].get(code, {}).get("CalendarEventId")
            exact = [r for r in rows if r.get("ShortCode") == code]
            if not event_id or len(exact) != 1:
                raise PilotError("Pilot event/link is missing or ambiguous for " + code)
            verify_event(exact[0], events[code], event_id)
            association_id = record_id(exact[0].get("CalendarCalEventId"))
            key = calendar["key"] + ":" + code
            if association_id != journal["links"].get(key, {}).get("CalendarCalEventId"):
                raise PilotError("Association ID changed for " + key)
            seen_events.add(event_id); counts["links"] += 1
        counts["calendars"] += 1
    counts["events"] = len(seen_events)
    if counts != {"events": len(plan["events"]), "calendars": len(plan["calendars"]), "links": len(plan["associations"])}:
        raise PilotError("Final pilot counts do not match the workbook.")
    return counts


def apply_pilot(client, plan, journal, report, folder):
    # Detect calendar collisions before any new master submission.
    for calendar in plan["calendars"]:
        matches = client.calendars_by_name(calendar["Name"])
        stored = journal["calendars"].get(calendar["key"], {})
        if len(matches) > 1 or (matches and record_id(matches[0].get("CalendarId")) != stored.get("CalendarId")):
            raise PilotError("Calendar name collision or unresolved prior create: " + calendar["Name"])
        if stored and not matches:
            raise PilotError("A recorded calendar is absent from readback; no duplicate create will be attempted.")
    if all(e["ShortCode"] in journal["events"] for e in plan["events"]):
        resolved = {e["ShortCode"]: journal["events"][e["ShortCode"]]["CalendarEventId"] for e in plan["events"]}
    else:
        submission = journal.get("master_submission")
        if submission and submission.get("state") == "upload_pending":
            reconcile_pre_content_upload_failure(journal, folder)
            submission = journal.get("master_submission")
        if submission:
            progress(client, "Resuming the recorded event load...")
            monitor_master_load(client, journal, report, folder)
        # A new invocation with no HDL submission must not reuse an old absent-code
        # snapshot. Existing submissions retain their ContentId generation.
        generation = "hdl:" + submission["ContentId"] if submission else getattr(client, "baseline_generation", "baseline")
        progress(client, "Resolving event identities from the saved key-map job...")
        codes = [e["ShortCode"] for e in plan["events"]]
        mapped = resolve_with_key_map(client, journal, report, folder, codes, require_all=bool(submission), generation=generation)
        report["key_map_preflight_ids"] = mapped
        resolved = {}
        owned = set((submission or {}).get("event_codes", []))
        for event in plan["events"]:
            code = event["ShortCode"]; event_id = mapped.get(code)
            prior = journal["events"].get(code, {})
            if event_id:
                if code not in owned and prior.get("CalendarEventId") != event_id:
                    raise PilotError("Existing event code has no matching creation journal: " + code)
                if prior and prior.get("CalendarEventId") != event_id:
                    raise PilotError("Master ID conflicts with the journal: " + code)
                resolved[code] = event_id
        missing = [e for e in plan["events"] if e["ShortCode"] not in resolved]
        if missing:
            if submission:
                raise PilotError("A loaded master is absent from the refreshed map; do not reload.")
            progress(client, "Submitting the %d new pilot events once..." % len(missing))
            start_master_load(client, plan, missing, journal, report, folder)
            monitor_master_load(client, journal, report, folder)
            generation = "hdl:" + journal["master_submission"]["ContentId"]
            created = resolve_with_key_map(client, journal, report, folder,
                                          [e["ShortCode"] for e in missing], generation=generation)
            resolved.update({e["ShortCode"]: created[e["ShortCode"]] for e in missing})
        for event in plan["events"]:
            journal["events"][event["ShortCode"]] = {"CalendarEventId": resolved[event["ShortCode"]],
                                                    "expected_values": expected_event(event), "state": "identity_resolved"}
        save_journal(folder, journal)
    report["resolved_master_ids"] = resolved
    progress(client, "Creating or resuming %d calendars and %d selected links..." % (len(plan["calendars"]), len(plan["associations"])))
    for calendar in plan["calendars"]:
        key = calendar["key"]; stored = journal["calendars"].get(key)
        progress(client, "Checking calendar: " + calendar["Name"])
        payload = {k: calendar[k] for k in ("Name", "StartDate", "EndDate", "Description")}
        if not stored:
            stored = {"state": "create_pending", "values": payload}
            journal["calendars"][key] = stored; save_journal(folder, journal)
            row = client.create_calendar(payload)
            stored.update(CalendarId=record_id(row.get("CalendarId")), state="created")
            save_journal(folder, journal)
        if not stored.get("CalendarId"):
            raise PilotError("Earlier calendar creation has an unresolved outcome: " + key)
        row = client.get(CALENDARS + "/" + stored["CalendarId"], {})
        if not calendar_values_match(row, payload):
            raise PilotError("Calendar readback differs for " + key)
    for link in plan["associations"]:
        code = link["event_code"]; event = next(e for e in plan["events"] if e["ShortCode"] == code)
        progress(client, "Checking event link: %s / %s" % (link["calendar_key"], code))
        calendar_id = journal["calendars"][link["calendar_key"]]["CalendarId"]
        event_id = resolved[code]; key = link["calendar_key"] + ":" + code
        rows = client.events_for_calendar(calendar_id, code)
        if len(rows) > 1 or (rows and record_id(rows[0].get("CalendarEventId")) != event_id):
            raise PilotError("Existing association conflicts with the intended master: " + key)
        if not rows:
            if journal["links"].get(key):
                raise PilotError("Prior link submission is not visible; no repeated POST: " + key)
            journal["links"][key] = {"state": "create_pending", "CalendarId": calendar_id, "CalendarEventId": event_id}
            save_journal(folder, journal)
            client.create_link(calendar_id, event_id)
            rows = client.events_for_calendar(calendar_id, code)
        if len(rows) != 1:
            raise PilotError("New association is not visible yet; journal retained for readback.")
        verify_event(rows[0], event, event_id)
        journal["links"][key] = {"CalendarId": calendar_id, "CalendarEventId": event_id,
                                  "CalendarCalEventId": record_id(rows[0].get("CalendarCalEventId")), "state": "verified"}
        save_journal(folder, journal)
    progress(client, "Reading back %d events, %d calendars, and %d links..." % (len(plan["events"]), len(plan["calendars"]), len(plan["associations"])))
    report["result_counts"] = verify_pilot(client, plan, journal)
    journal["pilot_status"] = "verified"; save_journal(folder, journal)
    report["status"] = "applied_verified"


def main(argv=None, result_callback=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "sign-in", "apply", "verify", "keymap-prerequisites", "keymap-resolve", "diagnose-extract"), nargs="?", default="plan")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume-existing", action="store_true", help="Retrieve a recorded key map without submitting any flow")
    parser.add_argument("--workbook", type=Path, default=ROOT / "K12_SAMPLE_A_Create_Pilot.xlsx")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--prompt-credentials", action="store_true", help="Prompt locally even if a credential is saved")
    parser.add_argument("--check-repeatability", action="store_true", help="Automatically repeat the completed build with writes disabled")
    args = parser.parse_args(argv)
    report, client = {"operation": args.action, "environment": DEFAULT_BASE, "status": "failed"}, None
    try:
        plan = create_plan(args.workbook); report["plan"] = plan
        if args.action == "plan" and plan.get("schema") == "K12-CALENDAR-AB-1":
            require_supported_loader(plan)
        journal = load_journal(args.output_dir); report["journal"] = journal
        signature = plan_signature(plan)
        if journal.get("plan_signature") not in (None, signature):
            raise PilotError("Workbook data differs from the journaled pilot; reconcile before changing this creation run.")
        journal["plan_signature"] = signature
        if args.resume_existing and args.action != "keymap-resolve":
            raise PilotError("--resume-existing is only valid with keymap-resolve.")
        if args.action == "plan": report["status"] = "plan_ready"
        else:
            if args.action in ("apply", "keymap-resolve") and not args.apply:
                raise PilotError("%s requires --apply. No Oracle write was attempted." % args.action)
            if args.action == "apply":
                require_supported_loader(plan)
            auth = establish_windows_credential() if args.action == "sign-in" else (prompt_credentials() if args.prompt_credentials else credentials())
            client = Client(auth); del auth
            client.diagnostic_dir = args.output_dir
            client.creation_requests_disabled = args.action == "diagnose-extract" or (args.action == "keymap-resolve" and args.resume_existing)
            client.show_progress = True; client.run_started = time.monotonic()
            progress(client, "Starting SAMPLE-A workflow: " + args.action)
            print("Each Oracle job is checked for up to 5 minutes. Pending jobs are saved for resume.", flush=True)
            if args.action == "sign-in":
                # Same runtime, TLS settings, host lock, and request method used by the pilot.
                client.source_master_lookup_link()
                report["authentication"] = {"status": "verified", "credential_store": "Windows Credential Manager",
                                            "credential_target": WINDOWS_CREDENTIAL_TARGET,
                                            "execution_account": getpass.getuser(), "proof": "GET existing HOLIDAY_A association returned HTTP 200"}
                report["status"] = "signed_in_verified"; return 0
            if args.action == "diagnose-extract":
                report["read_only_diagnostic"] = True
                try:
                    resolved = resolve_with_key_map(client, journal, report, args.output_dir, [KNOWN_CODE], require_all=True, resume_only=True)
                    report["known_master_check"] = resolved.get(KNOWN_CODE)
                except PilotError as error:
                    if not any("response_structure" in r for r in client.requests):
                        raise
                    report["diagnostic_finding"] = str(error)
                report["status"] = "diagnostic_captured"
                return 0
            if args.action == "keymap-resolve":
                # Reconciliation starts with the journaled status item: no unrelated REST inventory request first.
                resolved = resolve_with_key_map(client, journal, report, args.output_dir, [KNOWN_CODE], require_all=True, resume_only=args.resume_existing)
                report["known_master_check"] = resolved.get(KNOWN_CODE)
                if report["known_master_check"] != "810000331861667":
                    raise PilotError("Key-map SOAP retrieval did not independently resolve HOLIDAY_A to 810000331861667.")
                report["status"] = "keymap_xml_verified"; return 0
            progress(client, "Reading the existing HOLIDAY_A association...")
            advertised = client.source_master_lookup_link(); report["advertised_master_lookup"] = advertised
            if args.action == "keymap-prerequisites":
                report["existing_event_read_passed"] = True
                report["key_map_flow_metadata"] = client.key_map_flow_metadata()
                names = [row["FlowPattern"].casefold() for row in report["key_map_flow_metadata"]["flows"]]
                report["creation_prerequisites_passed"] = (any("refresh" in name for name in names) and
                                                            any("map extract" in name for name in names))
                report["status"] = "keymap_prerequisites_ready" if report["creation_prerequisites_passed"] else "keymap_prerequisites_missing"
                return 0 if report["creation_prerequisites_passed"] else 1
            if args.action == "verify":
                report["result_counts"] = verify_pilot(client, plan, journal)
                report["status"] = "verified"; return 0
            apply_pilot(client, plan, journal, report, args.output_dir)
            if args.check_repeatability:
                progress(client, "Checking the unchanged rerun automatically; writes are disabled...")
                report["initial_build_status"] = report["status"]
                report["status"] = "repeatability_check_incomplete"
                before = len(client.requests)
                client.creation_requests_disabled = True
                repeat = {}
                apply_pilot(client, plan, journal, repeat, args.output_dir)
                writes = sum(r.get("method") != "GET" for r in client.requests[before:])
                if writes:
                    raise PilotError("Repeatability check attempted a write.")
                report["repeatability"] = {"status": "verified", "write_requests": 0, "result_counts": repeat["result_counts"]}
                report["status"] = "applied_verified"
    except OraclePending as error:
        report["status"] = "awaiting_oracle"; report["error"] = str(error)
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "Stopped locally. Oracle jobs may still be running; use this same folder to reconcile and resume."
    except (ReadError, OSError, ValueError, EOFError, KeyError, BadZipFile, ET.ParseError, json.JSONDecodeError) as error:
        report["error"] = str(error)
    finally:
        if client:
            report["requests"] = client.requests; client.authorization = ""
            report["elapsed_seconds"] = round(time.monotonic() - client.run_started, 1)
            report["last_stage"] = getattr(client, "last_stage", "")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        out = args.output_dir / ("K12_SAMPLE_A_Create_Pilot_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".json")
        out.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        if report.get("error"): print("Error: " + report["error"])
        if report.get("diagnostic_finding"): print("Finding: " + report["diagnostic_finding"])
        print("Result: " + report["status"]); print("File: " + str(out))
        if report.get("result_counts"):
            print("Counts: " + ", ".join("%s=%s" % item for item in report["result_counts"].items()))
        if result_callback is not None:
            result_callback(report, out)
    return 0 if report["status"] in ("plan_ready", "signed_in_verified", "verified", "applied_verified", "keymap_prerequisites_ready", "keymap_xml_verified") else 1

if __name__ == "__main__": raise SystemExit(main())
