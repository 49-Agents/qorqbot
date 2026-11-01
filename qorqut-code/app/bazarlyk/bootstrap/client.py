"""Authenticated connection to the one shared service using local registration."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.safe_io import read_bytes


# These budgets cover actual service work, including configuration resolution
# and SQLite contention. Prompt responsiveness must not turn healthy requests
# into transport failures; asynchronous recovery uses the same request budget.
BOOTSTRAP_TIMEOUT = 15.0
REQUEST_TIMEOUT = 5.0


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def path_has_symlink(path: Path) -> bool:
    return path.is_symlink() or any(parent.is_symlink() for parent in path.parents)


def service_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainError("invalid_registration", "Service URL must be an explicit HTTP(S) endpoint without credentials")
    if parsed.path.rstrip("/") not in {"", "/mcp"}:
        raise DomainError("invalid_registration", "Service URL must identify the shared service or /mcp")
    return f"{parsed.scheme}://{parsed.netloc}"


def load_registration(path: Path) -> dict:
    path = Path(path)
    if path_has_symlink(path) or not path.is_file():
        raise DomainError("missing_registration", "Prepared local registration is missing")
    try:
        registration = json.loads(read_bytes(path, limit=1024 * 1024))
    except (OSError, ValueError) as exc:
        raise DomainError("invalid_registration", "Cannot read the prepared local registration") from exc
    required = {"schema_version", "registration_id", "agent_id", "workspace", "service_url", "credential_file"}
    if not isinstance(registration, dict) or not required.issubset(registration) or registration["schema_version"] != 1:
        raise DomainError("invalid_registration", "Unsupported prepared local registration")
    if type(registration.get("capture_transcripts", False)) is not bool:
        raise DomainError("invalid_registration", "Transcript capture selection must be a boolean")
    if type(registration.get("durable_delegation_enabled", False)) is not bool:
        raise DomainError("invalid_registration", "Durable delegation selection must be a boolean")
    workspace = Path(registration["workspace"])
    if not workspace.is_absolute() or path.resolve() != workspace.resolve() / ".bazarlyk/registration.json":
        raise DomainError("registration_conflict", "Local registration does not match its prepared workspace")
    if registration["credential_file"] != ".bazarlyk/credential":
        raise DomainError("invalid_registration", "Credential must use the prepared folder's scoped file")
    registration["service_url"] = service_url(registration["service_url"])
    return registration


def auth_headers(registration: dict) -> dict[str, str]:
    credential = Path(registration["workspace"]) / registration["credential_file"]
    if path_has_symlink(credential) or not credential.is_file() or credential.stat().st_mode & 0o077:
        raise DomainError("missing_credential", "Scoped credential is missing or must have mode 0600")
    try:
        token = read_bytes(credential, limit=1024, private=True).decode().strip()
    except OSError as exc:
        raise DomainError("missing_credential", "Cannot read the scoped credential") from exc
    if len(token) < 32 or len(token) > 512 or any(char.isspace() or ord(char) < 33 for char in token):
        raise DomainError("invalid_credential", "Scoped credential has invalid format")
    return {"Authorization": "Bearer " + token}


_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _safe_details(value) -> dict | None:
    """Keep only a machine-readable reason code from a service error.

    A caller can tell a per-record rejection from an endpoint-wide failure
    without the client relaying any other service-supplied text.
    """
    reason = value.get("reason") if isinstance(value, dict) else None
    return {"reason": reason} if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason) else None


class BootstrapClient:
    def __init__(self, registration: dict, *, timeout: float = 45):
        self.registration = registration
        self.timeout = timeout
        self.binding = None
        path = Path(registration['workspace'])/'.bazarlyk/session-binding.json'
        try:
            if not path_has_symlink(path):
                value = json.loads(path.read_text())
                if value['registration_id'] == registration['registration_id'] and type(value['generation']) is int:
                    self.binding = value
        except (OSError, ValueError, KeyError):pass

    def post(self, route: str, data: dict) -> dict:
        began = time.monotonic()
        if ('binding_generation' not in data and self.binding is not None
                and (route.startswith('/api/agent/inbox/') or route.startswith('/api/agent/delegation/') or route == '/api/agent/checkpoint')):
            session=data.get('consumer_id',data.get('session_id'))
            if self.binding['session_id']==session:
                data={**data,'binding_generation':self.binding['generation']}
        request = Request(self.registration["service_url"] + route,
            data=json.dumps(data, separators=(",", ":")).encode(),
            headers={**auth_headers(self.registration), "Content-Type": "application/json"}, method="POST")
        response = None
        try:
            try:
                response = build_opener(NoRedirects()).open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc  # The service's bounded JSON error preserves its domain code.
            with response:
                # JSON escaping can use six bytes per character in the 80k bundle.
                payload = response.read(1_000_001)
                if len(payload) > 1_000_000:
                    raise DomainError("bootstrap_unavailable", "Bootstrap response exceeds the bounded size")
                value = json.loads(payload)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            # Network exception strings can include URLs and headers; keep them out of context.
            status = getattr(response, 'status', None)
            if status in {401,403}:
                raise DomainError('invalid_credential' if status == 401 else 'forbidden',
                    'The service refused this agent identity') from exc
            cause = getattr(exc,'reason',exc)
            reason = 'timeout' if isinstance(cause,TimeoutError) else 'connection' if isinstance(exc,(URLError,OSError)) else 'invalid_response'
            raise DomainError("bootstrap_unavailable", "The shared service did not return a valid bootstrap response",
                              retryable=(isinstance(exc, (URLError, OSError)) or
                                         status in {502, 503, 504}),
                              details={'reason':reason,'elapsed_ms':round((time.monotonic()-began)*1000)}) from exc
        if response.status >= 400 and (not isinstance(value, dict) or value.get("status") != "error"):
            raise DomainError("bootstrap_unavailable", "The shared service refused the request",
                              retryable=response.status in {502, 503, 504})
        if not isinstance(value, dict):
            raise DomainError("bootstrap_unavailable", "Invalid bootstrap response")
        if value.get("status") == "error":
            error = value.get("error", {})
            if not isinstance(error, dict) or not isinstance(error.get("code", ""), str) or not isinstance(error.get("message", ""), str):
                raise DomainError("bootstrap_unavailable", "Invalid service error response")
            raise DomainError(error.get("code", "bootstrap_unavailable"), error.get("message", "Bootstrap was refused"),
                              retryable=error.get("retryable") is True, details=_safe_details(error.get("details")))
        return value.get("data", value)


def write_json_private(path: Path, value: dict) -> None:
    if path_has_symlink(path):
        raise DomainError("projection_conflict", "Local receipts cannot follow symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
