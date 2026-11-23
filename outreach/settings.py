"""Local profile, mailbox account binding, and secret storage."""

import base64
import ctypes
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .mailbox import address

PROFILE_DEFAULTS = {
    "name": "", "role": "", "goal": "founder", "skills": "", "proof": "",
    "proof_url": "", "resume_url": "", "availability": "", "service_idea": "", "auto_respond": False,
}
GOALS = {"founder", "role", "referral", "freelance", "dm"}
MAIL_ENV = {name: "EMAILSENDER_" + name.upper() for name in
            ("address", "password", "smtp_host", "smtp_port", "imap_host", "imap_port")}


def load_env(path):
    """Read the optional local settings file. Process environment has priority."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in MAIL_ENV.values():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8",
                                    delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _windows_secret(value, protect):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    src, dst = Blob(len(value), buf), Blob()
    crypt32 = ctypes.windll.crypt32
    transform = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CRYPTPROTECT_UI_FORBIDDEN; default scope is the current Windows account.
    if not transform(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise OSError("Windows could not access the saved mailbox credential.")
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(dst.data, ctypes.c_void_p))


def write_mailbox(path, config):
    saved = dict(config)
    if os.name == "nt":
        password = saved.pop("password").encode("utf-8")
        saved["protected_password"] = base64.b64encode(_windows_secret(password, True)).decode("ascii")
    save_json(path, saved)


def read_mailbox(path):
    saved = read_json(path, {})
    if saved:
        if "protected_password" in saved:
            saved["password"] = _windows_secret(base64.b64decode(saved.pop("protected_password")), False).decode("utf-8")
        return validate_mailbox(saved)
    env = {key: os.environ.get(var, "") for key, var in MAIL_ENV.items()}
    if env["address"] and env["password"]:
        return validate_mailbox(env)
    return None


def validate_mailbox(data):
    sender = address(str(data.get("address", "")))
    password = data.get("password")
    if not isinstance(password, str) or not password.strip() or len(password) > 1024:
        raise ValueError("Enter the app password for this mailbox.")
    config = {"address": sender, "password": password}
    for protocol, default_port in (("smtp", 465), ("imap", 993)):
        host = str(data.get(protocol + "_host") or protocol + ".gmail.com").strip()
        if len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
            raise ValueError(f"Enter a valid {protocol.upper()} hostname.")
        try:
            port = int(data.get(protocol + "_port") or default_port)
        except (TypeError, ValueError):
            raise ValueError(f"Enter a valid {protocol.upper()} port.") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"Enter a valid {protocol.upper()} port.")
        config.update({protocol + "_host": host, protocol + "_port": port})
    return config


def clean_fields(data, fields):
    result = {}
    for key in fields:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or len(value) > 4000 or "\x00" in value:
            raise ValueError(f"Check the value for {key.replace('_', ' ')}.")
        value = value.strip()
        if key == "name" and any(ord(c) < 32 for c in value):
            raise ValueError("Enter the name on a single line.")
        if key in {"proof_url", "resume_url", "source", "job_url"} and value:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or any(c.isspace() for c in value):
                raise ValueError(f"Use a complete http or https URL for {key.replace('_', ' ')}.")
        result[key] = value
    return result


def validate_profile(data):
    profile = PROFILE_DEFAULTS | clean_fields(data, PROFILE_DEFAULTS.keys() - {"auto_respond"})
    if type(data.get("auto_respond", False)) is not bool:
        raise ValueError("The automatic reply setting must be true or false.")
    profile["auto_respond"] = data.get("auto_respond", False)
    for key in ("name", "role", "proof", "proof_url"):
        if not profile[key]:
            raise ValueError(f"Your profile needs a {key.replace('_', ' ')}.")
    if profile["goal"] not in GOALS:
        raise ValueError("Choose a supported outreach goal.")
    return profile
