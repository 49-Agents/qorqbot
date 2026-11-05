"""The email engine's account API and managed sender routes, over private HTTP with the engine administration token.

The engine owns the provider session, account identity, sender-identity verification and the idempotent action ledger; this
client names a small allowlist of its endpoints and never exposes a provider method, URL or credential. A mutation is posted
at most once per request id by its caller: an uncertain outcome is recovered only by reading the engine's receipt."""

from __future__ import annotations

import hashlib
import json as jsonlib
from urllib.parse import quote

import httpx

from qorqut_operations.contracts import DomainError

from .config import FastmailConfig, MIN_TOKEN

MAX_RESULT_BYTES = 25 * 1024 * 1024
# Engine refusals that mean something specific to a caller; every other 4xx is a plain rejected request.
ENGINE_ERRORS = {
    "account_sender_identity_denied": ("sender_identity_denied", "The sender identity is not an identity of this Fastmail account"),
    "account_identity_mismatch": ("account_mismatch", "The request names a different Fastmail account than the engine's"),
    "account_request_conflict": ("request_conflict", "This request id already holds a different request in the engine ledger"),
    "account_draft_request_collision": ("request_conflict", "A draft for this request id already exists in the account"),
    "invalid_account_action": ("validation_error", "The engine rejected the action as invalid"),
    "invalid_account_request": ("validation_error", "The engine rejected the request as invalid"),
    "request_too_large": ("validation_error", "The request is too large for the engine"),
}
ROUTE_FIELDS = frozenset({"route_id", "revision", "generation", "client_id", "business_id", "mailbox_ref", "purpose",
                          "sender_ref", "active", "state", "ready", "blocker"})


def route_view(data) -> bool:
    return (isinstance(data, dict) and ROUTE_FIELDS <= set(data) and type(data["generation"]) is int
            and type(data["revision"]) is int and type(data["active"]) is bool and type(data["ready"]) is bool
            and data["state"] in {"provisioning", "ready", "uncertain", "failed", "disabled"})


def action_digest(request_id: str, action: str, payload: dict) -> str:
    """Hash of the exact JSON object posted to the engine (the engine echoes the same digest in its receipt)."""
    value = {**payload, "request_id": request_id, "action": action}
    return hashlib.sha256(jsonlib.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class EngineClient:
    def __init__(self, config: FastmailConfig, *, client_factory=httpx.Client, token: str | None = None):
        self.config = config
        self.client_factory = client_factory
        self._token = token

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = self.config.admin_token()
        if len(self._token) < MIN_TOKEN:
            raise DomainError("operation_unavailable", "The Fastmail engine token is invalid")
        return self._token

    # -- transport -------------------------------------------------------------------------------------------------
    def _open(self):
        return self.client_factory(base_url=self.config.engine_url, headers={"Authorization": "Bearer " + self.token},
                                   timeout=30, follow_redirects=False, trust_env=False)

    def _error_code(self, response):
        try:
            body = response.read() if hasattr(response, "read") else b""
            data = jsonlib.loads(body) if body and len(body) <= 65536 else None
        except (ValueError, httpx.HTTPError):
            data = None
        code = data.get("error") if isinstance(data, dict) else None
        return code if isinstance(code, str) else None

    def _refuse(self, response):
        """Map a non-success status to a stable code; the engine's own error name is used only for the known ones."""
        status = response.status_code
        code = self._error_code(response)
        if code in ENGINE_ERRORS:
            name, message = ENGINE_ERRORS[code]
            raise DomainError(name, message)
        if status in {401, 403}:
            raise DomainError("external_auth_failed", "The Fastmail engine connection requires owner attention")
        if status in {400, 404, 409, 413, 422}:
            raise DomainError("validation_error" if status != 409 else "request_conflict",
                              "The Fastmail engine rejected the request")
        raise DomainError("email_unavailable", "The Fastmail engine is unavailable", retryable=True)

    def _read(self, response, *, binary=False, metadata=False):
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESULT_BYTES:
                raise DomainError("external_result_invalid", "The Fastmail engine result exceeds the bounded size")
        if binary:
            return {"content": bytes(body), "headers": dict(response.headers)} if metadata else bytes(body)
        data = jsonlib.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data

    def call(self, path, *, params=None, method="GET", json=None, binary=False, metadata=False, missing=False, accept=(200,)):
        """One request to ``/fastmail<path>``. ``missing`` turns a 404 into ``None``; ``accept`` lists success statuses."""
        route = "/fastmail" + (("/" + path.lstrip("/")) if path else "")
        try:
            with self._open() as client:
                with client.stream(method, route, params=params, **({"json": json} if json is not None else {})) as response:
                    if response.status_code == 404 and missing:
                        return None
                    if response.status_code not in accept:
                        self._refuse(response)
                    return self._read(response, binary=binary, metadata=metadata)
        except httpx.HTTPError:
            raise DomainError("email_unavailable", "The Fastmail engine did not respond", retryable=True) from None
        except (ValueError, UnicodeError):
            raise DomainError("external_result_invalid", "The Fastmail engine returned an invalid result") from None

    def read(self, path, **options):
        """A read: an unreachable engine is reported as an unavailable operation, not as a retryable provider error."""
        try:
            return self.call(path, **options)
        except DomainError as exc:
            if exc.code == "email_unavailable":
                raise DomainError("operation_unavailable", "The Fastmail account is unavailable") from None
            raise

    # -- account reads ---------------------------------------------------------------------------------------------
    def capabilities(self):
        return self.read("")

    def folders(self):
        return self.read("folders")

    def identities(self):
        return self.read("identities")

    def search(self, query):
        return self.read("messages", params=query)

    def message(self, identity):
        result = self.read("messages/" + quote(identity, safe=""), missing=True)
        if result is None:
            raise DomainError("not_found", "The provider message no longer exists")
        return result

    def thread(self, identity, *, position=None, limit=None):
        params = {key: value for key, value in (("position", position), ("limit", limit)) if value is not None}
        return self.read("threads/" + quote(identity, safe=""), params=params or None)

    def attachment(self, blob_id, message_id, *, offset=0, limit=5 * 1024 * 1024):
        return self.read("attachments/" + quote(blob_id, safe=""),
                         params={"message_id": message_id, "offset": offset, "limit": limit}, binary=True, metadata=True)

    # -- the account action ledger ---------------------------------------------------------------------------------
    action_digest = staticmethod(action_digest)

    def mutation(self, request_id, action, payload):
        """Post one action. 200 and 202 both carry the engine's receipt; the caller never re-posts after an uncertain result."""
        return self.call("actions", method="POST", json={**payload, "request_id": request_id, "action": action}, accept=(200, 202))

    def receipt(self, request_id):
        return self.read("actions/" + quote(request_id, safe=""), missing=True)

    # -- managed integration sender routes (explicit optional Paper compatibility) ---------------------------------
    def route_change(self, action, body):
        """Ensure or deactivate one managed integration route of the engine; its refusals keep their own code."""
        if action not in {"ensure", "deactivate"}:
            raise DomainError("operation_unavailable", "Managed integration routes are not configured")
        try:
            with self._open() as client:
                response = client.post("/integrations/routes/" + action, json=body)
        except httpx.HTTPError:
            raise DomainError("email_unavailable", "The Fastmail engine did not respond", retryable=True) from None
        try:
            data = response.json() if len(response.content) <= 65536 else None
        except ValueError:
            data = None
        if response.status_code in {401, 403}:
            raise DomainError("external_auth_failed", "The Fastmail engine connection requires owner attention")
        if response.status_code in {400, 404, 409}:
            code = data.get("error") if isinstance(data, dict) else None
            raise DomainError(code if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 64
                              else "route_rejected", "The Fastmail engine refused the route change")
        if response.status_code != 200 or not route_view(data):
            raise DomainError("email_unavailable", "The Fastmail engine route change is unconfirmed", retryable=True)
        return data

    def routes(self, client_id):
        try:
            with self._open() as client:
                response = client.get("/integrations/routes", params={"client_id": client_id})
        except httpx.HTTPError:
            raise DomainError("email_unavailable", "The Fastmail engine did not respond", retryable=True) from None
        if response.status_code in {401, 403}:
            raise DomainError("external_auth_failed", "The Fastmail engine connection requires owner attention")
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200 or not isinstance(data, dict) or not isinstance(data.get("routes"), list) \
                or not all(route_view(item) for item in data["routes"]):
            raise DomainError("external_result_invalid", "The Fastmail engine returned an invalid route listing")
        return data["routes"]
