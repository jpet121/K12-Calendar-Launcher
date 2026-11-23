"""Local Oracle calendar launcher. Python standard library only; never public HTTP."""
from __future__ import annotations
import argparse
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from environments import ASSERTIONS, available_profiles, normalize_origin, origin_from_url, resolve_config, state_key, url_profile_id

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.3-rc1"
SAMPLE_A = "https://tenant-a.example.invalid"
MAX_BODY = 24 * 1024 * 1024


def config_source(root):
    private = Path(root) / "config.json"
    return private if private.exists() else Path(root) / "config.example.json"


def load_config(path=None, profile_id=None):
    """Resolve an explicitly supplied profile; never choose a saved default."""
    return resolve_config(json.loads((Path(path) if path else config_source(ROOT)).read_text(encoding="utf-8")), profile_id)


@contextmanager
def process_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / "launcher.lock").open("a+b")
    acquired = False
    try:
        handle.seek(0, 2)
        if not handle.tell(): handle.write(b"0"); handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise RuntimeError("This package is already open. Use the existing launcher window.") from error
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def safe_json(value):
    """Drop authentication fields from results before crossing into receipts/UI."""
    if isinstance(value, Path): return str(value)
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items() if str(k).lower() not in
                {"password", "authorization", "credentials", "username", "token"}}
    if isinstance(value, (list, tuple)): return [safe_json(v) for v in value]
    return value


def redact_values(value, authorization):
    secrets_to_hide = [authorization] if authorization else []
    if authorization.startswith("Basic "):
        encoded = authorization[6:]
        secrets_to_hide.append(encoded)
        try:
            decoded = base64.b64decode(encoded).decode()
            secrets_to_hide.extend([decoded, decoded.split(":", 1)[1]])
        except (ValueError, UnicodeError, IndexError): pass
    def scrub(item):
        if isinstance(item, str):
            for secret in secrets_to_hide:
                if secret: item = item.replace(secret, "[redacted]")
            return re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [redacted]", item, flags=re.IGNORECASE)
        if isinstance(item, dict): return {k: scrub(v) for k, v in item.items()}
        if isinstance(item, list): return [scrub(v) for v in item]
        return item
    return scrub(safe_json(value))


class Application:
    def __init__(self, root=ROOT, config=None, service_factory=None, create_runner=None, event_factory=None):
        self.root = Path(root)
        self.config_source = copy.deepcopy(config) if config is not None else json.loads(config_source(self.root).read_text(encoding="utf-8"))
        self.saved_profiles = {}
        self.load_saved_environments()
        # Configuration and saved catalogs describe choices, never a target.
        available_profiles(self.config_source)
        self.config = None
        self.state_dir = None
        self.receipts = {}
        self.token = secrets.token_urlsafe(48)
        self.authorization = ""
        self.service_factory, self.create_runner, self.event_factory = service_factory, create_runner, event_factory
        self.mutex = threading.RLock()
        self.busy, self.logs, self.result, self.error = False, [], None, None
        self.job_id, self.job_name, self.plans, self.downloads = None, None, {}, {}
        self.snapshot = None
        self.service = self.event_service = None
        self.thread = None

    @staticmethod
    def source_with_profiles(source):
        if "profiles" in source:
            return copy.deepcopy(source)
        selected = resolve_config(source)
        profile = {"name": selected["profile_name"], "environment": selected["environment"],
                   "integration": copy.deepcopy(selected["integration"]),
                   **{key: selected[key] for key in ASSERTIONS}}
        result = {key: copy.deepcopy(value) for key, value in source.items()
                  if key not in {"environment", "integration", "name", "profile_name", "profile_id", *ASSERTIONS}}
        result.update({"active_profile": selected["profile_id"], "profiles": {selected["profile_id"]: profile}})
        return result

    @staticmethod
    def saved_profile(profile_id, value):
        allowed = {"name", "environment", "integration", *ASSERTIONS}
        if not isinstance(value, dict) or set(value) != allowed:
            raise ValueError("Saved environment entry has invalid fields.")
        resolved = resolve_config({"profiles": {profile_id: value}}, profile_id)
        origin = resolved["environment"]
        if (profile_id != url_profile_id(origin) or value["environment"] != origin
                or value["name"] != urllib.parse.urlsplit(origin).netloc
                or value["integration"] != {} or any(value[key] is not False for key in ASSERTIONS)):
            raise ValueError("Saved environment entry must contain only a normalized URL and default settings.")
        return copy.deepcopy(value)

    def load_saved_environments(self):
        path = self.root / "state" / "saved_environments.json"
        if not path.exists():
            return
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError("Saved environments file is too large.")
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or set(saved) != {"schema", "active_profile", "profiles"}
                    or saved["schema"] != "K12-SAVED-ENVIRONMENTS-1" or not isinstance(saved["profiles"], dict)
                    or len(saved["profiles"]) > 100):
                raise ValueError("Saved environments file has an unsupported format.")
            merged = self.source_with_profiles(self.config_source)
            for identity, entry in saved["profiles"].items():
                entry = self.saved_profile(identity, entry)
                if identity in merged["profiles"] and merged["profiles"][identity] != entry:
                    raise ValueError("A saved environment conflicts with config.json. Resolve that profile before connecting.")
                merged["profiles"][identity] = entry
                self.saved_profiles[identity] = entry
            # Retain the catalog format for compatibility, but never activate
            # the last selected profile when this launcher is restarted.
            available_profiles(merged)
            if saved["active_profile"] not in merged["profiles"]:
                raise ValueError("Saved environment selection is not in the profile catalog.")
            self.config_source = merged
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("Saved environment settings could not be read. Check state/saved_environments.json.") from None

    def persist_environments(self, profiles, active_profile):
        """Atomically save origins only; never save authentication/browser URLs."""
        rows = {identity: self.saved_profile(identity, value) for identity, value in profiles.items()}
        if len(rows) > 100:
            raise ValueError("The launcher supports up to 100 saved pasted environments.")
        folder = self.root / "state"
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / ("saved_environments." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"schema": "K12-SAVED-ENVIRONMENTS-1", "active_profile": active_profile,
                           "profiles": rows}, stream, indent=2)
                stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, folder / "saved_environments.json")
        except OSError:
            raise ValueError("The environment could not be saved. Check write access to this extracted launcher folder; no Oracle request was sent.") from None
        finally:
            temporary.unlink(missing_ok=True)

    def select_or_add_url(self, value):
        with self.mutex:
            if self.busy and threading.current_thread() is not self.thread:
                raise RuntimeError("An operation is already running. Wait before switching environments.")
            self.clear_session()
            origin = origin_from_url(value)
            rows = available_profiles(self.config_source)
            if self.config and self.config["environment"] == origin:
                identity = self.config["profile_id"]
                source, profiles = self.config_source, self.saved_profiles
            else:
                matches = [row for row in rows if row["environment"] == origin]
                if len(matches) > 1:
                    raise ValueError("Several saved profiles use this URL. Choose the intended saved environment before connecting.")
                source, profiles = self.source_with_profiles(self.config_source), copy.deepcopy(self.saved_profiles)
                if matches:
                    identity = matches[0]["id"]
                else:
                    identity = url_profile_id(origin)
                    if identity in source["profiles"]:
                        raise ValueError("The environment profile ID conflicts with config.json. Resolve that profile before connecting.")
                    entry = {"name": urllib.parse.urlsplit(origin).netloc, "environment": origin, "integration": {},
                             **{key: False for key in ASSERTIONS}}
                    source["profiles"][identity] = entry
                    profiles[identity] = entry
            selected = resolve_config(source, identity)
            # Drop existing credentials before changing or persisting the target.
            # A failed disk write must never cause credentials to reach Oracle.
            self.clear_session()
            self.persist_environments(profiles, identity)
            self.config_source, self.saved_profiles = source, profiles
            return self.select_environment(identity, persist=False)

    def require_environment(self):
        if self.config is None or self.state_dir is None:
            raise ValueError("Paste or select an Oracle environment before continuing.")
        return self.config

    def load_receipt_index(self):
        self.require_environment()
        self.receipts = {}
        receipt_index = self.state_dir / "receipt_index.json"
        if receipt_index.exists():
            saved = json.loads(receipt_index.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for key, item in saved.items():
                    if isinstance(item, dict) and isinstance(item.get("relative_path"), str):
                        candidate = (self.state_dir / item["relative_path"]).resolve()
                        if candidate.is_relative_to(self.state_dir.resolve()) and candidate.is_file(): self.receipts[key] = item
        self.refresh_event_receipts()

    def clear_session(self):
        self.authorization = ""
        self.service = self.event_service = self.snapshot = None
        self.plans.clear()
        self.downloads.clear()

    def select_environment(self, profile_id, *, persist=True):
        # Called within the serialized job. A direct caller cannot interrupt it.
        with self.mutex:
            if self.busy and threading.current_thread() is not self.thread:
                raise RuntimeError("An operation is already running. Wait before switching environments.")
            self.clear_session()
            selected = resolve_config(self.config_source, profile_id=profile_id)
            if persist:
                self.persist_environments(self.saved_profiles, selected["profile_id"])
            self.config = selected
            self.state_dir = self.root / "state" / "environments" / state_key(selected)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.load_receipt_index()
            return {"status": "environment_selected", "environment": selected["environment"],
                    "profile_id": selected["profile_id"], "profile_name": selected["profile_name"]}

    def refresh_event_receipts(self):
        if self.state_dir is None:
            return
        folder = self.state_dir / "event_edits"
        for path in folder.glob("event_edits/receipts/*.json"):
            if re.fullmatch(r"[a-f0-9]{32}", path.stem) and path.resolve().is_relative_to(self.state_dir.resolve()):
                self.receipts[path.stem] = {"relative_path": str(path.relative_to(self.state_dir)), "operation": "event_edit"}

    def progress(self, message):
        with self.mutex:
            self.logs.append(redact_values(str(message), self.authorization))
            self.logs = self.logs[-400:]

    def public_state(self):
        with self.mutex:
            selected = self.config or {}
            return safe_json({"version": VERSION, "environment": selected.get("environment", ""),
                              "profile_id": selected.get("profile_id", ""), "profile_name": selected.get("profile_name", ""),
                              "environment_state_key": state_key(self.config) if self.config else "", "profiles": available_profiles(self.config_source),
                              "signed_in": bool(self.authorization), "busy": self.busy, "job_id": self.job_id,
                              "job_name": self.job_name, "logs": list(self.logs), "result": self.result, "error": self.error,
                              "receipts": [{"receipt_id": key, "operation": item["operation"]} for key, item in self.receipts.items()]})

    def new_service(self):
        self.require_environment()
        if not self.authorization: raise ValueError("Sign in to Oracle first.")
        if self.service_factory is None:
            from operations import CalendarService
            factory = CalendarService
        else: factory = self.service_factory
        return factory(self.authorization, self.config, self.state_dir / "management", self.progress)

    def new_event_service(self):
        self.require_environment()
        if not self.authorization: raise ValueError("Sign in to Oracle first.")
        if self.event_factory is None:
            from event_updates import EventUpdateService
            factory = EventUpdateService
        else: factory = self.event_factory
        return factory(self.authorization, self.config, self.state_dir / "event_edits", self.progress, calendar_service=self.service)

    def start(self, action, data):
        allowed = {"connect", "disconnect", "select_environment", "select_url", "read", "export", "preview_create", "apply_create",
                   "preview_calendar_edit", "apply_calendar_edit", "preview_event_edit", "apply_event_edit",
                   "prepare_restore", "reconcile_event", "reconcile_calendar", "check_delete", "apply_delete",
                   "prepare_acceptance", "run_acceptance"}
        if action not in allowed: raise ValueError("Unknown operation.")
        with self.mutex:
            if self.busy: raise RuntimeError("An operation is already running. Wait for its result.")
            selected = self.config or {}
            profile_id, environment = selected.get("profile_id", ""), selected.get("environment", "")
            if action != "select_environment":
                if data.get("expected_profile_id", profile_id) != profile_id or data.get("expected_environment", environment) != environment:
                    raise ValueError("The selected environment changed. Review the current target before continuing.")
            self.busy, self.logs, self.result, self.error = True, [], None, None
            self.job_id, self.job_name = secrets.token_hex(8), action
            self.thread = threading.Thread(target=self._run, args=(action, data), daemon=False)
            self.thread.start()
            return {"job_id": self.job_id}

    def _run(self, action, data):
        attempted_authorization = ""
        if action == "connect" and isinstance(data.get("username"), str) and isinstance(data.get("password"), str):
            attempted_authorization = "Basic " + base64.b64encode((data["username"].strip() + ":" + data["password"]).encode()).decode()
        try:
            if self.config:
                self.progress("Target: " + self.config["profile_name"] + " | " + self.config["environment"])
            result = self.execute(action, data)
            if isinstance(result, dict) and self.config:
                result["environment"] = self.config["environment"]
                result["profile_id"] = self.config["profile_id"]
                result["profile_name"] = self.config["profile_name"]
            self.attach_receipt(result, action)
            with self.mutex: self.result = redact_values(result, self.authorization)
        except Exception as error:
            # Expected integrations return safe diagnostic strings. Never dump a
            # request body, stack locals, authentication headers or config secrets.
            message = redact_values(redact_values(str(error), self.authorization), attempted_authorization)
            message = re.sub(r"Basic\s+[A-Za-z0-9+/=]+", "Basic [redacted]", message, flags=re.IGNORECASE)
            if self.authorization:
                message = message.replace(self.authorization, "[redacted]")
            password = data.get("password")
            if isinstance(password, str) and password: message = message.replace(password, "[redacted]")
            recovery = {"status": "failed", "receipt_path": getattr(error, "receipt_path", None)}
            self.attach_receipt(recovery, action)
            with self.mutex:
                self.error = message or type(error).__name__
                if recovery.get("recovery_receipt_id"): self.result = recovery
        finally:
            attempted_authorization = ""
            data.clear()
            self.refresh_event_receipts()
            with self.mutex: self.busy = False

    def attach_receipt(self, result, action):
        if self.state_dir is None or not isinstance(result, dict): return
        container = result.get("report", result)
        if not isinstance(container, dict): return
        raw = container.get("receipt_path") or container.get("report_path")
        native_event_id = container.get("receipt_id")
        if not raw and self.event_service and isinstance(native_event_id, str) and re.fullmatch(r"[a-f0-9]{32}", native_event_id):
            raw = self.event_service.folder / "receipts" / (native_event_id + ".json")
        if not raw: return
        path = Path(raw).resolve()
        if not path.is_relative_to(self.state_dir.resolve()) or not path.is_file(): return
        relative = str(path.relative_to(self.state_dir.resolve()))
        existing = next((key for key, value in self.receipts.items() if value["relative_path"] == relative), None)
        identifier = existing or (native_event_id if "event" in action and native_event_id else secrets.token_urlsafe(18))
        operation = "calendar_edit" if "calendar" in action else ("event_edit" if "event" in action or action == "prepare_restore" else "create")
        self.receipts[identifier] = {"relative_path": relative, "operation": operation}
        temporary = self.state_dir / "receipt_index.tmp"
        temporary.write_text(json.dumps(self.receipts, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_dir / "receipt_index.json")
        result["recovery_receipt_id"] = identifier
        result["receipt_download"] = self.register_file(path)

    def register_file(self, path):
        self.require_environment()
        path = Path(path).resolve()
        if not path.is_file() or not path.is_relative_to(self.root.resolve()):
            raise ValueError("Result file is outside this package.")
        identifier = secrets.token_urlsafe(18)
        self.downloads[identifier] = path
        return {"download_id": identifier, "filename": path.name}

    def remember(self, kind, preview, **extra):
        self.require_environment()
        identifier = secrets.token_urlsafe(24)
        preview = copy.deepcopy(preview)
        self.plans[identifier] = {"kind": kind, "preview": preview, "environment_state_key": state_key(self.config), **extra}
        return {"preview_id": identifier, "kind": kind, "preview": copy.deepcopy(preview)}

    def take_plan(self, identifier, kind):
        self.require_environment()
        value = self.plans.get(identifier)
        if not value or value["kind"] != kind or value.get("environment_state_key") != state_key(self.config): raise ValueError("Preview expired or does not match this operation. Preview again.")
        return value

    def execute(self, action, data):
        if action == "select_environment":
            return self.select_environment(data.get("profile_id"))
        if action == "select_url":
            return self.select_or_add_url(data.get("environment_url"))
        if action == "prepare_acceptance":
            self.require_environment()
            from live_acceptance import prepare_live_acceptance
            saved = sorted((self.state_dir / "live_acceptance").glob("*/preview.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if saved:
                preview = json.loads(saved[0].read_text(encoding="utf-8"))
            else:
                preview = prepare_live_acceptance(self.config, self.state_dir)
            return self.remember("acceptance", preview)
        if action == "connect":
            self.clear_session()
            if "environment_url" in data:
                self.select_or_add_url(data["environment_url"])
                self.progress("Connecting target: " + self.config["profile_name"] + " | " + self.config["environment"])
            self.require_environment()
            username, password = data.get("username", ""), data.get("password", "")
            if not isinstance(username, str) or not username.strip() or len(username) > 256 or any(c in username for c in ":\r\n"):
                raise ValueError("Enter a valid Oracle REST username.")
            if not isinstance(password, str) or not password or len(password) > 4096: raise ValueError("Enter the Oracle REST password (maximum 4096 characters).")
            self.clear_session()
            self.authorization = "Basic " + base64.b64encode((username.strip() + ":" + password).encode()).decode()
            try:
                self.service = self.new_service()
                self.progress("Signing in and reading the visible calendar inventory...")
                self.snapshot = self.service.read()
                self.event_service = None
                self.plans.clear()
                return {"status": "connected", "snapshot": self.snapshot}
            except Exception:
                self.clear_session(); raise
        if action == "disconnect":
            self.clear_session(); return {"status": "signed_out"}
        self.require_environment()
        if action == "preview_create":
            from workbooks import load_plan
            if data.get("example"):
                source = self.root / "templates" / "Calendar_Build_Sample.xlsx"
            else:
                raw = data.get("content_base64", "")
                if not isinstance(raw, str): raise ValueError("Choose an Excel workbook.")
                try: content = base64.b64decode(raw, validate=True)
                except ValueError as error: raise ValueError("The workbook upload was invalid.") from error
                if not 0 < len(content) <= 12 * 1024 * 1024: raise ValueError("Workbook must be 12 MB or smaller.")
                uploads = self.state_dir / "inputs"; uploads.mkdir(exist_ok=True)
                source = uploads / (hashlib.sha256(content).hexdigest() + ".xlsx")
                source.write_bytes(content)
            plan = load_plan(source, environment=self.config["environment"])
            blockers = []
            try:
                from engine.create_adapter import validate_plan
                validate_plan(plan)
            except (ValueError, RuntimeError) as error: blockers.append(str(error))
            except Exception as error:
                # The engine's PilotError derives from ReadError, not ValueError.
                if type(error).__name__ in ("PilotError", "ReadError"): blockers.append(str(error))
                else: raise
            public = {"counts": plan["counts"], "request_id": plan["request_id"], "calendars": plan["calendars"],
                      "events": plan["events"], "associations": plan["associations"], "blockers": blockers,
                      "environment": plan.get("environment", self.config["environment"])}
            return self.remember("create", public, plan=plan, source=str(source))
        if not self.authorization: raise ValueError("Sign in to Oracle first.")
        if action == "run_acceptance":
            from live_acceptance import run_live_acceptance
            saved = self.take_plan(data.get("preview_id"), "acceptance")
            return run_live_acceptance(copy.deepcopy(saved["preview"]), self.authorization, self.config, self.state_dir, self.progress)
        if action == "read":
            self.snapshot = self.service.read(); return {"status": "read_verified", "snapshot": self.snapshot}
        if action == "export":
            from workbooks import export_calendar_data
            snapshot = self.service.read()
            folder = self.state_dir / "exports"; folder.mkdir(exist_ok=True)
            name = "Calendar_Export_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".zip"
            path = export_calendar_data(snapshot, folder / name)
            return {"status": "exported", "counts": snapshot.get("counts", {}), **self.register_file(path)}
        if action == "apply_create":
            saved = self.take_plan(data.get("preview_id"), "create")
            if saved["preview"]["blockers"]: raise ValueError("Resolve the workbook issues before applying.")
            plan = saved["plan"]
            if plan.get("environment") != self.config["environment"]: raise ValueError("Workbook environment differs from the configured tenant.")
            digest = hashlib.sha256((self.config["environment"] + "\n" + plan["request_id"]).encode()).hexdigest()[:24]
            output = self.state_dir / "create" / digest
            if self.create_runner is None:
                from engine.create_adapter import run_create
                runner = lambda plan, auth, output, progress: run_create(plan, auth, output, progress, config=self.config)
            else: runner = self.create_runner
            self.progress("Building or resuming request " + plan["request_id"] + "...")
            report = runner(copy.deepcopy(plan), self.authorization, output, self.progress)
            return {"status": report.get("status", "failed"), "report": report,
                    "calendar_names": [c["Name"] for c in plan["calendars"]]}
        if action == "preview_calendar_edit":
            preview = self.service.plan_calendar_edit(data.get("calendar_id"), data.get("changes", {}))
            return self.remember("calendar_edit", preview)
        if action == "apply_calendar_edit":
            saved = self.take_plan(data.get("preview_id"), "calendar_edit")
            return self.service.apply_calendar_edit(copy.deepcopy(saved["preview"]))
        if action == "reconcile_calendar":
            saved = self.receipts.get(data.get("receipt_id"))
            if not saved or saved["operation"] != "calendar_edit": raise ValueError("Choose a saved calendar-edit receipt from this launcher.")
            path = (self.state_dir / saved["relative_path"]).resolve()
            if not path.is_relative_to(self.state_dir.resolve()): raise ValueError("Invalid receipt location.")
            return self.service.reconcile_calendar_edit(path)
        if action in {"preview_event_edit", "apply_event_edit", "prepare_restore", "reconcile_event"}:
            if self.event_service is None: self.event_service = self.new_event_service()
            if action == "preview_event_edit":
                preview = self.event_service.plan_event_edit(data.get("event_id"), data.get("start"), data.get("end"))
                return self.remember("event_edit", preview)
            if action == "prepare_restore":
                preview = self.event_service.prepare_restore(data.get("receipt_id"))
                return self.remember("event_edit", preview)
            if action == "reconcile_event": return self.event_service.reconcile_event_edit(data.get("receipt_id"))
            saved = self.take_plan(data.get("preview_id"), "event_edit")
            return self.event_service.apply_event_edit(copy.deepcopy(saved["preview"]))
        if action == "check_delete":
            return self.remember("delete", self.service.plan_delete_calendar(data.get("calendar_id")))
        if action == "apply_delete":
            saved = self.take_plan(data.get("preview_id"), "delete")
            return self.service.apply_delete_calendar(copy.deepcopy(saved["preview"]))
        raise ValueError("Unknown operation.")


def make_server(app, port=0):
    class Handler(BaseHTTPRequestHandler):
        server_version = "K12Launcher"
        def log_message(self, *args): pass
        def valid_host(self): return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)
        def allowed(self):
            return self.valid_host() and secrets.compare_digest(self.headers.get("X-Launcher-Token", ""), app.token)
        def send(self, status, body, kind="application/json; charset=utf-8", download=None):
            if not isinstance(body, bytes): body = json.dumps(safe_json(body), ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY"); self.send_header("Referrer-Policy", "no-referrer")
            # The bundled, static guide is also a standalone HTML file with an
            # inline stylesheet. Keep the interactive application's stricter CSP.
            style_policy = "'self' 'unsafe-inline'" if urllib.parse.urlsplit(self.path).path == "/guide" else "'self'"
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src " + style_policy + "; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download: self.send_header("Content-Disposition", 'attachment; filename="' + download.replace('"', '') + '"')
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if not self.valid_host(): return self.send(403, {"error": "Invalid local host."})
            path = urllib.parse.urlsplit(self.path).path
            static = {"/": ("ui/index.html", "text/html; charset=utf-8"), "/app.js": ("ui/app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("ui/style.css", "text/css; charset=utf-8"), "/guide": ("RED_TEAM_GUIDE.html", "text/html; charset=utf-8")}
            if path in static:
                name, kind = static[path]
                file = app.root / name
                if not file.exists(): return self.send(404, {"error": "File missing from package."})
                return self.send(200, file.read_bytes(), kind)
            if not self.allowed(): return self.send(403, {"error": "Open the launcher from START-CALENDAR-LAUNCHER.cmd."})
            if path == "/api/state": return self.send(200, app.public_state())
            if path.startswith("/api/download/"):
                file = app.downloads.get(path.rsplit("/", 1)[-1])
                if file and file.is_file(): return self.send(200, file.read_bytes(), "application/octet-stream", file.name)
            return self.send(404, {"error": "Not found."})
        def do_POST(self):
            origin = "http://127.0.0.1:" + str(self.server.server_port)
            if not self.allowed() or self.headers.get("Origin") != origin:
                return self.send(403, {"error": "Request must come from this launcher window."})
            if self.headers.get_content_type() != "application/json": return self.send(415, {"error": "JSON is required."})
            try: length = int(self.headers.get("Content-Length", "0"))
            except ValueError: return self.send(400, {"error": "Invalid request length."})
            if not 0 < length <= MAX_BODY: return self.send(413, {"error": "Request exceeds the file size limit."})
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict): raise ValueError("Request must be an object.")
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/close":
                    if app.busy: raise RuntimeError("Wait for the running operation to finish before closing.")
                    app.authorization = ""; app.service = app.event_service = None
                    self.send(200, {"status": "closed"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start(); return
                if path != "/api/action": return self.send(404, {"error": "Not found."})
                action = data.pop("action", "")
                self.send(202, app.start(action, data))
            except RuntimeError as error: self.send(409, {"error": str(error)})
            except (ValueError, TypeError) as error: self.send(400, {"error": str(error)})
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="Offline package and regression checks; no Oracle sign-in")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10): print("Python 3.10 or newer is required."); return 2
    if args.self_test:
        import socket
        import unittest
        from unittest.mock import patch
        suite = unittest.TestSuite([unittest.TestLoader().discover(str(ROOT / "tests")),
                                    unittest.TestLoader().discover(str(ROOT / "engine"), top_level_dir=str(ROOT))])
        connect = socket.create_connection
        def local_only(address, *positional, **keywords):
            if address[0] not in ("127.0.0.1", "localhost", "::1"):
                raise AssertionError("Offline self-test blocked a non-local network request.")
            return connect(address, *positional, **keywords)
        with patch("socket.create_connection", side_effect=local_only):
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        folder = ROOT / "state"; folder.mkdir(exist_ok=True)
        (folder / "self_test_result.json").write_text(json.dumps({"version": VERSION,
            "time_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
            "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "status": "passed" if result.wasSuccessful() else "failed", "live_oracle_requests": 0}, indent=2) + "\n")
        return 0 if result.wasSuccessful() else 1
    try:
        with process_lock(ROOT / "state"):
            app = Application()
            server = make_server(app)
            url = "http://127.0.0.1:" + str(server.server_port) + "/#" + app.token
            print("Calendar Launcher " + VERSION)
            print("Oracle target: not selected. Paste or select an environment in the launcher.")
            print("Keep this window open. Closing it does not cancel jobs already submitted to Oracle.")
            print("Local launcher link (do not share): " + url, flush=True)
            if not args.no_browser: webbrowser.open(url)
            try: server.serve_forever(poll_interval=0.3)
            except KeyboardInterrupt: print("Stopped locally. Keep the state folder to resume submitted work.")
            finally:
                server.server_close()
                if app.thread and app.thread.is_alive():
                    print("Waiting for the current request to record its outcome...", flush=True)
                    app.thread.join()
                app.authorization = ""
            return 0
    except (OSError, ValueError, RuntimeError) as error:
        print("Launcher could not start: " + str(error)); return 1


if __name__ == "__main__": raise SystemExit(main())
