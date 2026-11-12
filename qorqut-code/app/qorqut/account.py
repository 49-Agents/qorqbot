"""The owner's Qorqbot account: email sign-in with the account service, a daily heartbeat and the latest-release notice.

Qorqbot asks the owner to sign in once per installation with an emailed one-time code (``/root/account`` routes in
``transport/http.py``, the dashboard's ``js/account-gate.js``). The account service (``QORQBOT_ACCOUNT_API``, default
``https://api.qorqbot.com``) learns only what this module sends it:

* at sign-in: the email, how the owner uses Qorqbot (personal or work, and the company for work), whether they want
  product emails, a random installation ID, the Qorqbot version and the platform;
* in a heartbeat at most once a day while the dashboard is open: the installation ID, version and platform.

It never receives conversations, files, prompts, records or anything agents do. The session token is kept in
``<runtime>/account.json`` (mode 0600), never in ``.env``, which agent terminals receive, and never in the browser.

When the service cannot be reached the owner may continue without signing in; the choice lasts until this process
stops, so the sign-in screen comes back on the next start. ``QORQBOT_ACCOUNT=off`` turns the sign-in requirement off
for development and test installations.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from qorqut_operations.contracts import DomainError, utc_now

ACCOUNT_FILE = "account.json"
DEFAULT_API = "https://api.qorqbot.com"
DOWNLOAD_PAGE = "https://qorqbot.com/downloads/"
COMMERCIAL_FREE_UNTIL = "2027-01-01"
TIMEOUT = 10
HEARTBEAT_EVERY = 20 * 3600     # at most one a day; the service also counts one per installation per day
RELEASE_EVERY = 6 * 3600
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
USES = {"personal", "work"}

_lock = threading.Lock()
_deferred: set[str] = set()      # runtime directories whose owner chose "sign in later" in this process
_release_cache: dict = {}        # {"checked": monotonic, "latest": {...} | None}
_heartbeat_started: dict = {}    # runtime directory -> monotonic time of the last background heartbeat attempt


def api_base(environ=None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("QORQBOT_ACCOUNT_API") or DEFAULT_API).rstrip("/")


def required(environ=None) -> bool:
    """Sign-in is required in released builds (a folder install with a payload version). A source checkout, which
    development and the test harnesses run, asks only with ``QORQBOT_ACCOUNT=on``; ``off`` turns it off anywhere."""
    env = os.environ if environ is None else environ
    choice = (env.get("QORQBOT_ACCOUNT") or "").strip().lower()
    if choice in {"off", "0", "false", "no"}:
        return False
    if choice in {"on", "1", "true", "yes"}:
        return True
    return _facts(env)["app_version"] != "dev"


def path(runtime_directory) -> Path:
    return Path(runtime_directory) / ACCOUNT_FILE


def _read(runtime_directory) -> dict:
    file = path(runtime_directory)
    if not file.exists():
        return {"version": 1}
    try:
        saved = json.loads(file.read_text())
        if isinstance(saved, dict) and saved.get("version") == 1:
            return saved
    except (ValueError, OSError):
        pass
    raise DomainError("configuration_unavailable", "The Qorqbot account file could not be read")


def _write(runtime_directory, saved: dict) -> None:
    file = path(runtime_directory)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd, temporary = tempfile.mkstemp(dir=file.parent, prefix=".account-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(saved, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def install_id(runtime_directory) -> str:
    """A random ID for this installation, made once. It names the installation, not the person."""
    with _lock:
        saved = _read(runtime_directory)
        if not isinstance(saved.get("install_id"), str):
            saved["install_id"] = str(uuid.uuid4())
            _write(runtime_directory, saved)
        return saved["install_id"]


def _facts(environ=None):
    """The version and platform sent with sign-in and heartbeats."""
    from qorqut.connectors import host_platform
    from qorqut.owner_settings import _payload_version
    root = (os.environ if environ is None else environ).get("QORQUT_FOLDER_ROOT")
    version = _payload_version(root) if root and Path(root).is_absolute() else None
    return {"app_version": version or "dev", "platform": host_platform()}


def _call(method, route, body=None, token=None, environ=None):
    """One request to the account service. A network failure or a 5xx is ``provider_unavailable`` (the dashboard offers
    to continue without signing in); a refusal keeps the service's own message."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api_base(environ) + route, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "Qorqbot"})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            raw = reply.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}")
        except ValueError:
            detail = {}
        if error.code >= 500:
            raise DomainError("provider_unavailable", "The Qorqbot sign-in service is not responding", retryable=True)
        code = detail.get("error") if isinstance(detail, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else None
        if error.code == 401:
            raise DomainError("unauthenticated", message or "Sign in to Qorqbot again", details={"service_code": code})
        raise DomainError("validation_error", message or "The sign-in service refused the request",
                          details={"service_code": code, "status": error.code})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise DomainError("provider_unavailable", "Qorqbot cannot reach its sign-in service", retryable=True)


def _public(saved) -> dict:
    account = saved.get("account") if isinstance(saved.get("account"), dict) else None
    return {"signed_in": bool(saved.get("token") and account),
            "account": {key: account.get(key) for key in ("email", "use", "company", "created")} if account else None}


def owner_view(runtime_directory, environ=None) -> dict:
    """What the dashboard shows: whether sign-in is needed, the account, the licence note and any newer release.
    The token is never returned. A due heartbeat and a due release check run in the background."""
    saved = _read(runtime_directory)
    view = _public(saved)
    view.update({
        "required": required(environ) and not view["signed_in"] and str(runtime_directory) not in _deferred,
        "deferred": str(runtime_directory) in _deferred,
        "license": {"commercial_free_until": COMMERCIAL_FREE_UNTIL,
                    "commercial": bool(view["account"] and view["account"].get("use") == "work")},
        "update": update_view(environ),
    })
    now = time.monotonic()
    with _lock:
        beat = (view["signed_in"] and time.time() - float(saved.get("heartbeat_at") or 0) > HEARTBEAT_EVERY
                and now - _heartbeat_started.get(str(runtime_directory), -3600) >= 3600)
        if beat:
            _heartbeat_started[str(runtime_directory)] = now
        check = now - _release_cache.get("checked", -RELEASE_EVERY) >= RELEASE_EVERY
        if check:
            _release_cache["checked"] = now
    if beat:
        threading.Thread(target=_heartbeat_quietly, args=(runtime_directory, environ), daemon=True).start()
    if check:
        threading.Thread(target=_refresh_release, args=(environ,), daemon=True).start()
    return view


def start(runtime_directory, body, environ=None) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("email"), str) or not EMAIL.match(body["email"].strip()):
        raise DomainError("validation_error", "Enter a valid email address")
    _call("POST", "/v1/auth/email/start", {"email": body["email"].strip().lower(), "client": "desktop",
                                           "install_id": install_id(runtime_directory)}, environ=environ)
    return {"sent": True, "email": body["email"].strip().lower()}


def verify(runtime_directory, body, environ=None) -> dict:
    if not isinstance(body, dict):
        raise DomainError("validation_error", "Sign-in details must be an object")
    email, code, use = body.get("email"), body.get("code"), body.get("use")
    company = body.get("company")
    if not isinstance(email, str) or not EMAIL.match(email.strip()):
        raise DomainError("validation_error", "Enter a valid email address")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{8}", code.replace("-", "").replace(" ", "")):
        raise DomainError("validation_error", "Enter the 8-character code from the email")
    if use not in USES:
        raise DomainError("validation_error", "Choose personal or work use")
    if use == "work" and (not isinstance(company, str) or not company.strip() or len(company) > 200):
        raise DomainError("validation_error", "Enter your company name")
    reply = _call("POST", "/v1/auth/email/verify", {
        "email": email.strip().lower(), "code": code.replace("-", "").replace(" ", "").upper(),
        "install_id": install_id(runtime_directory), **_facts(environ), "use": use,
        "company": company.strip() if use == "work" else None, "marketing_ok": body.get("marketing_ok") is True,
    }, environ=environ)
    if not isinstance(reply, dict) or not isinstance(reply.get("token"), str) or not isinstance(reply.get("account"), dict):
        raise DomainError("provider_unavailable", "The sign-in service sent an unexpected reply", retryable=True)
    with _lock:
        saved = _read(runtime_directory)
        saved.update({"token": reply["token"], "account": reply["account"], "signed_in_at": utc_now(),
                      "heartbeat_at": time.time()})
        _write(runtime_directory, saved)
    _deferred.discard(str(runtime_directory))
    return owner_view(runtime_directory, environ)


def defer(runtime_directory, environ=None) -> dict:
    """Continue without signing in until this process stops, for when the service cannot be reached."""
    _deferred.add(str(runtime_directory))
    return owner_view(runtime_directory, environ)


def logout(runtime_directory, environ=None) -> dict:
    with _lock:
        saved = _read(runtime_directory)
        token = saved.pop("token", None)
        saved.pop("account", None)
        _write(runtime_directory, saved)
    if token:
        try:
            _call("POST", "/v1/auth/logout", {}, token=token, environ=environ)
        except DomainError:
            pass    # The local sign-out stands; the service forgets an unused token by itself.
    return owner_view(runtime_directory, environ)


def heartbeat(runtime_directory, environ=None) -> bool:
    """Tell the service this installation is in use today. A revoked token signs the installation out."""
    saved = _read(runtime_directory)
    if not saved.get("token"):
        return False
    try:
        _call("POST", "/v1/heartbeat", {"install_id": install_id(runtime_directory), **_facts(environ)},
              token=saved["token"], environ=environ)
    except DomainError as error:
        if error.code == "unauthenticated":
            with _lock:
                current = _read(runtime_directory)
                if current.get("token") == saved["token"]:
                    current.pop("token", None)
                    current.pop("account", None)
                    _write(runtime_directory, current)
        return False
    with _lock:
        current = _read(runtime_directory)
        current["heartbeat_at"] = time.time()
        _write(runtime_directory, current)
    return True


def _heartbeat_quietly(runtime_directory, environ):
    try:
        heartbeat(runtime_directory, environ)
    except Exception:   # A background courtesy: never surfaces.
        pass


def _version_key(text):
    """``1.3.0-preview.3`` < ``1.3.0`` < ``1.3.1``; anything unparsable sorts lowest."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", str(text or ""))
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def _refresh_release(environ=None):
    try:
        latest = _call("GET", "/v1/releases/latest", environ=environ)
        if isinstance(latest, dict) and isinstance(latest.get("version"), str):
            _release_cache["latest"] = latest
    except DomainError:
        pass


def update_view(environ=None):
    """The newer release to tell the owner about, or None. Updates are never downloaded or applied here."""
    latest = _release_cache.get("latest")
    if not latest:
        return None
    current = _facts(environ)["app_version"]
    newer, installed = _version_key(latest.get("version")), _version_key(current)
    if newer is None or installed is None or newer <= installed:
        return None
    return {"version": latest["version"].lstrip("v"), "url": DOWNLOAD_PAGE, "notes_url": latest.get("notes_url")}
