"""One explicit application POST. No retry, local request store or control effect."""

import http.client
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from .config import HandoffConfig
from .contracts import Binding, BrowserError, FrozenModel, canonical

MAX_REPLY_BYTES = 64 * 1024


class ObservedTab(FrozenModel):
    binding: Binding
    tab_id: str = Field(min_length=1, max_length=256)


class InputRequest(FrozenModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=4096)
    target_agent_id: UUID | None = None
    observed_tab: ObservedTab | None = None

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A nonblank user action is required")
        return value


class HandoffReceipt(FrozenModel):
    protocol: Literal[1]
    actor_id: UUID
    target_agent_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_id: UUID
    conversation_id: UUID
    surface_id: UUID
    state: Literal["requested", "ready", "completed", "cancelled", "interrupted"]
    browser_binding: Binding | None

    @model_validator(mode="after")
    def ready_binding(self):
        if self.state == "ready" and self.browser_binding is None:
            raise ValueError("A ready receipt requires its browser binding")
        if self.browser_binding is not None and self.browser_binding.identity.agent_id != self.target_agent_id:
            raise ValueError("The receipt browser must belong to the target agent")
        return self


class ApplicationError(FrozenModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=4096)


class ErrorEnvelope(FrozenModel):
    error: ApplicationError


class HandoffClient:
    def __init__(self, config: HandoffConfig, token: SecretStr, *, timeout=10.0):
        if not 0 < timeout <= 60:
            raise ValueError("A finite bridge timeout is required")
        self._url, self._token, self.timeout = config.url, token, timeout

    def request(self, request: InputRequest, *, actor_id: UUID, agent_id: UUID) -> HandoffReceipt:
        payload = request.model_dump(mode="json")
        payload.update(protocol=1, actor_id=str(actor_id),
                       target_agent_id=str(request.target_agent_id or agent_id))
        encoded = canonical(payload).encode("utf-8")
        url = urlsplit(self._url)
        factory = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = factory(url.hostname, url.port, timeout=self.timeout)
        # http.client neither inherits proxies nor follows redirects. Explicit
        # connect separates a known pre-send failure from all possible sends.
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException):
                raise BrowserError("unavailable", "Application handoff is unavailable before send") from None
            deadline = time.monotonic() + self.timeout
            try:
                path = (url.path or "/") + ("?" + url.query if url.query else "")
                connection.request("POST", path,
                                   body=encoded, headers={"Authorization": "Bearer " + self._token.get_secret_value(),
                                   "Content-Type": "application/json", "Accept": "application/json"})
                response = connection.getresponse()
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    # A close-delimited response may detach the connection socket.
                    if connection.sock is not None:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(8192, MAX_REPLY_BYTES + 1 - len(body)))
                    body.extend(chunk)
                    if len(body) > MAX_REPLY_BYTES:
                        raise ValueError()
                    if not chunk:
                        break
                if (response.length not in (None, 0)
                        or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"
                        or response.getheader("Content-Encoding", "identity") != "identity"):
                    raise ValueError()
                if response.status == 200:
                    receipt = HandoffReceipt.model_validate_json(body)
                    if (str(receipt.actor_id), str(receipt.target_agent_id), receipt.idempotency_key) != (
                            payload["actor_id"], payload["target_agent_id"], request.idempotency_key):
                        raise ValueError()
                    return receipt
                # Only well-formed definite rejections are treated as such. A
                # redirect or server failure could hide a committed request.
                allowed_errors = {
                    400: {"invalid_argument"}, 401: {"unauthenticated"}, 404: {"not_found"},
                    409: {"target_changed", "idempotency_conflict", "revision_conflict",
                          "invalid_transition", "uncertain_effect"},
                    422: {"unsupported_capability"},
                }
                error = ErrorEnvelope.model_validate_json(body).error
                if error.code in allowed_errors.get(response.status, set()):
                    raise BrowserError(error.code, "Application rejected the handoff request")
                raise ValueError()
            except BrowserError:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                raise BrowserError("uncertain_effect", "Application may have saved the request; retain its original key") from None
        finally:
            connection.close()
