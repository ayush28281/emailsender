"""Loopback-only web interface and the background mailbox worker."""

import hashlib
import json
import logging
import re
import threading
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .engine import Engine
from .jobs import JobTracker
from .mailbox import Mailbox
from .settings import (
    PROFILE_DEFAULTS, clean_fields, read_json, read_mailbox, save_json,
    validate_mailbox, validate_profile, write_mailbox,
)


STATIC = Path(__file__).with_name("static")
CONTACT_FIELDS = {"email", "name", "company", "scenario", "context", "source", "role", "job_url", "idea"}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/jobs.css": ("jobs.css", "text/css; charset=utf-8"),
    "/static/jobs.js": ("jobs.js", "text/javascript; charset=utf-8"),
    "/static/work-samples.css": ("work-samples.css", "text/css; charset=utf-8"),
    "/static/work-samples.js": ("work-samples.js", "text/javascript; charset=utf-8"),
}


def workspace_key(folder):
    return hashlib.sha256(str(Path(folder).resolve()).encode("utf-8")).hexdigest()[:24]


def create_mailbox(config):
    return Mailbox(sender=config["address"], password=config["password"],
                   smtp_host=config["smtp_host"], smtp_port=config["smtp_port"],
                   imap_host=config["imap_host"], imap_port=config["imap_port"])


def contact_input(data, *, allow_email=False):
    fields = CONTACT_FIELDS if allow_email else CONTACT_FIELDS - {"email"}
    contact = clean_fields(data, fields)
    if "reuse_context" in data:
        if type(data["reuse_context"]) is not bool:
            raise ValueError("Choose whether to reuse this observation for this company.")
        contact["reuse_context"] = data["reuse_context"]
    return contact


class Workspace:
    def __init__(self, folder, interval=60):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile_path = self.folder / "profile.json"
        self.mailbox_path = self.folder / "mailbox.json"
        self.identity_path = self.folder / "account.json"
        self.profile = PROFILE_DEFAULTS | read_json(self.profile_path, {})
        self.config = read_mailbox(self.mailbox_path)
        self.mailbox = create_mailbox(self.config) if self.config else None
        if self.config:
            self._bind(self.config["address"])
        self.db = self.folder / "queue.sqlite3"
        self.interval = interval
        self.lock = threading.RLock()
        self.cycle_lock = threading.Lock()
        self.closing = threading.Event()
        self.wake = threading.Event()
        self.worker = None
        self.last_run = self.last_error = ""
        self.engine().close()
        self.jobs = JobTracker(self.db)

    def _bind(self, address):
        current = read_json(self.identity_path, "")
        if current and current.casefold() != address.casefold():
            raise ValueError("This queue belongs to another mailbox. Use a separate --data-dir for a different sender.")
        if not current:
            save_json(self.identity_path, address)

    def engine(self):
        with self.lock:
            return Engine(self.db, self.profile.copy(), mailbox=self.mailbox)

    def set_profile(self, data):
        profile = validate_profile(data)
        with self.lock:
            save_json(self.profile_path, profile)
            self.profile = profile
        self.wake.set()

    def connect(self, data):
        config = validate_mailbox(data)
        candidate = create_mailbox(config)
        with self.cycle_lock:
            try:
                candidate.verify()
            except Exception:
                raise ValueError("Mailbox verification failed. Check the app password, provider hostnames, ports and mailbox access.") from None
            with self.lock:
                self._bind(config["address"])
                write_mailbox(self.mailbox_path, config)
                self.config, self.mailbox = config, candidate
                self.last_error = ""
        self.wake.set()

    def add(self, data):
        if not self.profile["name"]:
            raise ValueError("Save your sender profile in Sender setup first.")
        contact = contact_input(data, allow_email=True)
        if self.config and contact.get("email", "").casefold() == self.config["address"].casefold():
            raise ValueError("Enter the address of the person you want to contact.")
        with closing(self.engine()) as engine:
            item = engine.add(contact)
        self.wake.set()
        return item

    def tick(self):
        if not self.mailbox or not self.profile["name"]:
            raise ValueError("Connect your mailbox and save your sender profile first.")
        if not self.cycle_lock.acquire(blocking=False):
            return {"sent": 0, "replies": 0, "errors": [], "busy": True}
        try:
            with closing(self.engine()) as engine:
                result = engine.tick(stop_requested=self.closing.is_set)
            with self.lock:
                self.last_run = datetime.now(timezone.utc).isoformat()
                self.last_error = " ".join(result.get("errors", []))
            return result
        finally:
            self.cycle_lock.release()

    def start(self):
        def work():
            while not self.closing.is_set():
                self.wake.clear()
                if self.profile["name"] and self.mailbox:
                    try:
                        self.tick()
                    except Exception:
                        with self.lock:
                            self.last_error = "Cycle interrupted. Check your mailbox connection and try again."
                        logging.exception("Mailbox cycle interrupted")
                self.wake.wait(self.interval)

        self.worker = threading.Thread(target=work, daemon=True, name="emailsender-worker")
        self.worker.start()

    def close(self):
        self.closing.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=35)

    def dashboard(self):
        with closing(self.engine()) as engine:
            result = engine.dashboard()
        result.update(self.jobs.dashboard())
        metrics = result["metrics"]
        metrics["replies"] = metrics["replied"]
        metrics["positive_rate"] = metrics["positive_reply_rate"] / 100
        for key in ("reply_rate", "outcome_rate"):
            metrics[key] /= 100
        for contact in result["contacts"]:
            contact["next_at"] = contact.get("next_due")
            contact["reply_suggestion"] = contact.get("suggestion", "")
        with self.lock:
            mailbox = {key: value for key, value in (self.config or {}).items() if key != "password"}
            return {**result, "profile": self.profile.copy(),
                    "mailbox": {"configured": bool(self.config), "address": "", **mailbox},
                    "worker": {"running": bool(self.worker and self.worker.is_alive()),
                               "last_run": self.last_run, "last_error": self.last_error}}

    def post(self, path, data):
        if path == "/api/profile":
            self.set_profile(data)
        elif path == "/api/mailbox":
            self.connect(data)
        elif path == "/api/contacts":
            return {"contact": self.add(data)}
        elif path == "/api/jobs":
            return {"job": self.jobs.add(data)}
        elif path == "/api/run":
            return self.tick()
        else:
            job_path = re.fullmatch(r"/api/jobs/([1-9][0-9]{0,15})/update", path)
            if job_path:
                return {"job": self.jobs.update(int(job_path[1]), data)}
            match = re.fullmatch(r"/api/contacts/([1-9][0-9]{0,15})/(stop|outcome|update|company-suggestion|approve)", path)
            if not match:
                raise LookupError("Unknown endpoint.")
            ident, action = int(match[1]), match[2]
            with closing(self.engine()) as engine:
                if action == "company-suggestion":
                    company = clean_fields(data, {"company"}).get("company")
                    return {"suggestion": engine.company_suggestion(ident, company)}
                if action == "stop":
                    engine.stop(ident)
                elif action == "approve":
                    contact = engine.approve(ident, data.get("subject"), data.get("body"), data.get("review_token"))
                    self.wake.set()
                    return {"contact": contact}
                elif action == "outcome":
                    value = data.get("value")
                    if value not in ("none", "call", "interview", "other"):
                        raise ValueError("Choose a supported outcome.")
                    engine.outcome(ident, "" if value == "none" else value)
                else:
                    contact = engine.update(ident, contact_input(data))
                    self.wake.set()
                    return {"contact": contact}
        return {}


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, port, workspace):
        self.workspace = workspace
        self.workspace_key = workspace_key(workspace.folder)
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _local(self):
        port = self.server.server_port
        hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
        if port == 80:
            hosts += ("127.0.0.1", "localhost")
        return self.headers.get("Host", "").lower() in hosts

    def _respond(self, status, content_type, content):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status, data):
        self._respond(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if not self._local():
            return self._json(403, {"error": "Use the local dashboard address."})
        path = urlsplit(self.path).path
        if path == "/api/dashboard":
            try:
                return self._json(200, self.server.workspace.dashboard())
            except Exception:
                logging.exception("Could not load dashboard")
                return self._json(500, {"error": "Could not read the local queue."})
        if path in ASSETS:
            name, mime = ASSETS[path]
            return self._respond(200, mime, (STATIC / name).read_bytes())
        if path == "/api/instance":
            return self._json(200, {"app": "Emailsender", "workspace": self.server.workspace_key})
        self._json(404, {"error": "Page not found."})

    def do_POST(self):
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin")
        if not self._local() or (origin is not None and origin != "http://" + host) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._json(403, {"error": "Open the app from its local address."})
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "Send JSON with Content-Type application/json."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 65536:
                return self._json(413, {"error": "Request body is too large."})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Send a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/quit":
                self.server.workspace.closing.set()
                self.server.workspace.wake.set()
                try:
                    self._json(200, {"ok": True})
                finally:
                    self.server.shutdown()
                return
            result = self.server.workspace.post(path, body)
            self._json(200, {"ok": True, **result})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except LookupError as exc:
            self._json(404, {"error": str(exc)})
        except Exception:
            logging.exception("Local operation failed")
            self._json(500, {"error": HTTPStatus.INTERNAL_SERVER_ERROR.phrase})

    def log_message(self, format, *args):
        # Request paths and local contact activity are private to the workspace.
        pass
