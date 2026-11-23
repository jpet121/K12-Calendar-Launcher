"""Environment configuration; credentials and Oracle record IDs never cross profiles."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import re
from urllib.parse import urlsplit

DEFAULT_BASE = "https://tenant-a.example.invalid"
ASSERTIONS = ("administrator_asserted_full_calendar_visibility", "administrator_asserted_no_legacy_event_coverage")


def normalize_origin(value):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 33 for c in value):
        raise ValueError("Environment must be an HTTPS Oracle origin without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Environment URL is invalid.") from None
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or "?" in value or "#" in value or "\\" in value):
        raise ValueError("Environment must be an HTTPS origin only, without credentials, path, query or fragment.")
    host = parsed.hostname.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or ".." in host:
        raise ValueError("Environment requires a valid DNS hostname.")
    if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in host.split(".")):
        raise ValueError("Environment requires a valid DNS hostname.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Environment port is invalid.")
    return "https://" + host + (":" + str(port) if port is not None and port != 443 else "")


def origin_from_url(value):
    """Accept an Oracle browser URL, retaining only its validated HTTPS origin.

    This deliberately does not replace normalize_origin: integration links and
    configured origins still have to satisfy that stricter routing boundary.
    No exception includes the pasted browser URL or its session parameters.
    """
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise ValueError("Paste the Oracle environment's HTTPS URL.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Environment URL contains invalid characters.")
    value = value.strip(" ")
    if not value or any(c.isspace() for c in value):
        raise ValueError("Environment URL must not contain spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme.lower() != "https" or not host or parsed.username is not None
                or parsed.password is not None or len(host) > 253 or parsed.netloc.endswith(":")):
            raise ValueError
        origin = "https://" + host + (":" + str(port) if port is not None else "")
        return normalize_origin(origin)
    except ValueError:
        raise ValueError("Use a valid HTTPS Oracle URL without an embedded username or password.") from None


def url_profile_id(origin):
    return "url-" + hashlib.sha256(normalize_origin(origin).encode("utf-8")).hexdigest()[:24]


def _profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Profile ID must use 1-64 letters, digits, underscores or hyphens.")
    return value


def _integration(value):
    if not isinstance(value, dict):
        raise ValueError("Profile integration must be an object.")
    result = deepcopy(value)
    for key in ("refresh_flow_id", "key_map_flow_id"):
        if key in result and result[key] is not None:
            if isinstance(result[key], bool) or not re.fullmatch(r"[1-9][0-9]*", str(result[key])):
                raise ValueError(key + " must be a positive Oracle flow ID or omitted for discovery.")
            result[key] = str(result[key])
    ref = result.get("reference_event")
    if ref is not None:
        if not isinstance(ref, dict) or not isinstance(ref.get("ShortCode"), str) or not ref["ShortCode"].strip():
            raise ValueError("reference_event requires a ShortCode and exact Oracle record IDs.")
        for key in ("CalendarEventId", "CalendarId", "CalendarCalEventId"):
            if not re.fullmatch(r"[1-9][0-9]*", str(ref.get(key, ""))):
                raise ValueError("reference_event requires numeric " + key + ".")
            ref[key] = str(ref[key])
    return result


def resolve_config(value, profile_id=None):
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object.")
    profiles = value.get("profiles")
    if "profiles" in value:
        if not isinstance(profiles, dict):
            raise ValueError("Configuration requires at least one environment profile.")
        if profile_id is None:
            raise ValueError("Paste or select an Oracle environment before continuing.")
        selected = _profile_id(profile_id)
        if selected not in profiles or not isinstance(profiles[selected], dict):
            raise ValueError("Choose a configured environment profile.")
        profile = deepcopy(profiles[selected])
        if "environment" not in profile:
            raise ValueError("Each profile requires its Oracle environment URL.")
        result = {k: deepcopy(v) for k, v in value.items() if k not in ("profiles", "active_profile", "environment", "integration", *ASSERTIONS)}
        result.update(profile)
        result["profile_id"] = selected
        result["profile_name"] = profile.get("name", selected)
        for key in ASSERTIONS:
            result[key] = profile.get(key, False)
    else:
        result = deepcopy(value)
        if "environment" not in value:
            raise ValueError("An explicit Oracle environment is required.")
        selected = _profile_id(profile_id if profile_id is not None else value.get("profile_id", "sample-a" if value["environment"] == DEFAULT_BASE else "default"))
        result["profile_id"] = selected
        result["profile_name"] = value.get("profile_name", value.get("name", selected))
    result["environment"] = normalize_origin(result.get("environment"))
    name = result["profile_name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError("Profile name must be 1-100 printable characters.")
    result["integration"] = _integration(result.get("integration", {}))
    for field, default, low, high in (("request_timeout_seconds", 30, 5, 60), ("poll_timeout_seconds", 300, 30, 600)):
        setting = result.get(field, default)
        if type(setting) not in (int, float) or not low <= setting <= high:
            raise ValueError(field + " is outside the supported range.")
        result[field] = setting
    for key in ASSERTIONS:
        setting = result.get(key, False)
        if type(setting) is not bool:
            raise ValueError(key + " must be true or false.")
        result[key] = setting
    return result


def available_profiles(value):
    if isinstance(value, dict) and "profiles" in value:
        if not isinstance(value["profiles"], dict):
            raise ValueError("Configuration requires at least one environment profile.")
        rows = [resolve_config(value, key) for key in value["profiles"]]
    else:
        rows = [resolve_config(value)]
    return [{"id": row["profile_id"], "name": row["profile_name"], "environment": row["environment"]} for row in rows]


def state_key(config):
    identity = _profile_id(config.get("profile_id", "default"))
    origin = normalize_origin(config["environment"])
    return identity + "-" + hashlib.sha256(origin.encode("utf-8")).hexdigest()[:20]
