"""Host client of the personal WhatsApp connector's managed-send transport.

The connector exposes this transport on its own private Unix socket, separate from the read socket the agent's
``whatsapp_read`` MCP uses. Every request carries an HMAC made with an operator-created secret file that only the host
reads (never the MCP configuration). The client holds no approval logic: the host operation and the Signoff engine decide
whether a send may happen; this module only speaks the wire protocol, derives the stable request ID from the job, and
binds every receipt to the request, recipient, payload and account before the host believes it.

This is not a sandbox. A process running as the same Unix user can read the secret file and the socket; see
``docs/v1.2/WHATSAPP_SIGNOFFS.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket
import time

from qorqut_operations.contracts import DomainError

SIGN_PREFIX = "qorqut-whatsapp-managed/v1"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ACCOUNT_REF = re.compile(r"^[a-f0-9]{64}$")
CHAT_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}@(s\.whatsapp\.net|lid|g\.us)$")
MAX_TEXT_BYTES = 16384
SECRET_MIN, SECRET_MAX = 32, 4096
RESPONSE_LIMIT = 16384
SEND_TIMEOUT = 32.0
STATES = ("accepted", "unknown")


class UnknownOutcome(Exception):
    """A send request was written to the connector but no complete answer came back. The message may or may not have
    been sent; only a receipt-only lookup of the same request may settle it. Never carries request content."""


def request_id_for_job(job_id: str) -> str:
    """The one stable connector request ID of a job. It is derived, never supplied by a caller, and never changes, so a
    retry, restart or recovery of the same job always names the same connector receipt."""
    value = "qorqut-job-" + job_id
    if not isinstance(job_id, str) or REQUEST_ID.fullmatch(value) is None:
        raise DomainError("invalid_plan", "WhatsApp send job has no valid identity")
    return value


# -- the connector's request fingerprint -----------------------------------------------------------------------------

_SHORT = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f",
          "<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"}


def go_json_string(value: str) -> str:
    """The text Go's ``encoding/json`` produces for a string (HTML escaping on), byte for byte.

    The connector's receipt fingerprint is ``sha256(json.Marshal([chat_id, text]))``. Python's own encoder differs for
    ``<``, ``>``, ``&``, U+2028 and U+2029, so the encoding is spelled out here and locked against Go-computed vectors by
    ``tests/test_whatsapp_signoffs_fingerprint.py``. A string Go could not represent (lone surrogates) is refused."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DomainError("validation_error", "Text must be valid Unicode") from None
    out = []
    for char in value:
        if char in _SHORT:
            out.append(_SHORT[char])
        elif ord(char) < 0x20:
            out.append("\\u%04x" % ord(char))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def payload_fingerprint(chat_id: str, text: str) -> str:
    body = "[" + go_json_string(chat_id) + "," + go_json_string(text) + "]"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# -- payload validation shared by preparation, application and dispatch ---------------------------------------------

def validate_payload(chat_id, text) -> None:
    if not isinstance(chat_id, str) or CHAT_ID.fullmatch(chat_id) is None:
        raise DomainError("validation_error", "chat_id must be an exact direct or group chat ID from whatsapp_read")
    if not isinstance(text, str) or not text.strip():
        raise DomainError("validation_error", "text must be nonempty")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise DomainError("validation_error", "text must be valid Unicode") from None
    if size > MAX_TEXT_BYTES:
        raise DomainError("validation_error", "text must be at most 16384 UTF-8 bytes")


# -- transport -------------------------------------------------------------------------------------------------------

def read_secret(path) -> bytes:
    """The operator-created secret: an absolute, real, private (0600) file of this user. Never logged or returned."""
    target = Path(path)
    fixed = DomainError("operation_unavailable", "The personal WhatsApp send credential is unavailable")
    try:
        info = target.lstat()
        if (not target.is_absolute() or not os.path.isfile(target) or target.is_symlink() or info.st_uid != os.getuid()
                or info.st_mode & 0o177 or not SECRET_MIN <= info.st_size <= SECRET_MAX + 1):
            raise fixed
        raw = target.read_bytes()
    except OSError:
        raise fixed from None
    secret = raw[:-1] if raw.endswith(b"\n") else raw
    if not SECRET_MIN <= len(secret) <= SECRET_MAX:
        raise fixed
    return secret


def sign(secret: bytes, method: str, path: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    for part in (SIGN_PREFIX, method, path, timestamp, hashlib.sha256(body).hexdigest()):
        mac.update(part.encode() + b"\n")
    return mac.hexdigest()


def _refused_before_sending(data: bytes) -> bool:
    """The connector's refusal body: JSON with ``"sent": false``, produced only for requests rejected before a send could
    start (authentication, validation, account mismatch, every error before the durable reservation)."""
    try:
        value = json.loads(data)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("sent") is False


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("whatsapp.local", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class ManagedWhatsApp:
    """One configured managed-send connector: the version 2 binding's socket, secret file and frozen account reference."""

    def __init__(self, config: dict, *, clock=time.time):
        self.socket, self.secret_file, self.account_ref = config["socket"], config["secret_file"], config["account_ref"]
        self.clock = clock

    def _exchange(self, route: str, payload: dict, *, timeout: float, sending: bool = False):
        """One signed request. Failures before the request is on the wire are DomainErrors that prove nothing was
        sent; once a send request may have reached the connector, an unanswered one raises UnknownOutcome."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        stamp = str(int(self.clock()))
        secret = read_secret(self.secret_file)
        headers = {"Content-Type": "application/json", "X-Qorqut-Timestamp": stamp,
                   "X-Qorqut-Signature": sign(secret, "POST", route, stamp, body)}
        connection = _UnixConnection(self.socket, timeout)
        try:
            try:
                connection.connect()
            except OSError:
                raise DomainError("operation_unavailable", "The personal WhatsApp connector is unavailable") from None
            try:
                connection.request("POST", route, body=body, headers=headers)
                reply = connection.getresponse()
                data = reply.read(RESPONSE_LIMIT + 1)
            except (OSError, http.client.HTTPException):
                if sending:
                    raise UnknownOutcome() from None
                raise DomainError("operation_unavailable", "The personal WhatsApp connector did not answer") from None
            status = reply.status
        finally:
            connection.close()
        if sending and status != 200 and not _refused_before_sending(data):
            # Only the connector's explicit "this request sent nothing" body proves a send did not happen. Any other
            # non-success answer to a send (a bare 503 included) may come from after the message went out.
            raise UnknownOutcome()
        if status == 401:
            raise DomainError("operation_unavailable", "The personal WhatsApp connector rejected the host credential")
        if status == 409:
            raise DomainError("forbidden", "The personal WhatsApp connector refused the request or account")
        if status == 503:
            raise DomainError("operation_unavailable", "The personal WhatsApp connector is not ready; nothing was sent")
        if status == 400:
            raise DomainError("validation_error", "The personal WhatsApp connector rejected the request; nothing was sent")
        if status != 200 or len(data) > RESPONSE_LIMIT:
            if sending:
                raise UnknownOutcome()
            raise DomainError("external_result_invalid", "The personal WhatsApp connector returned an invalid response")
        try:
            value = json.loads(data)
        except ValueError:
            if sending:
                raise UnknownOutcome() from None
            raise DomainError("external_result_invalid", "The personal WhatsApp connector returned an invalid response") from None
        if not isinstance(value, dict):
            if sending:
                raise UnknownOutcome()
            raise DomainError("external_result_invalid", "The personal WhatsApp connector returned an invalid response")
        return value

    def account(self) -> dict:
        value = self._exchange("/managed/account", {}, timeout=5)
        ref = value.get("account_ref")
        if not isinstance(ref, str) or ACCOUNT_REF.fullmatch(ref) is None or value.get("managed_send") is not True:
            raise DomainError("external_result_invalid", "The connector did not report managed sending")
        return {"account_ref": ref, "connected": value.get("connected") is True}

    def chat(self, chat_id: str) -> dict:
        value = self._exchange("/managed/chat", {"chat_id": chat_id}, timeout=5)
        name = value.get("name", "")
        if not isinstance(value.get("known"), bool) or not isinstance(name, str):
            raise DomainError("external_result_invalid", "The connector returned an invalid chat description")
        return {"known": value["known"], "name": name[:200]}

    def send(self, request_id: str, chat_id: str, text: str, account_ref: str) -> dict:
        """Submit one exact message under its stable request ID. The connector reserves the ID durably before sending and
        never resends a recorded one, so this call is idempotent for the same ID and payload."""
        payload = {"request_id": request_id, "chat_id": chat_id, "text": text, "account_ref": account_ref}
        value = self._exchange("/managed/send", payload, timeout=SEND_TIMEOUT, sending=True)
        receipt = value.get("receipt")
        if value.get("found") is not True or not isinstance(receipt, dict):
            raise UnknownOutcome()
        return receipt

    def receipt(self, request_id: str, account_ref: str) -> dict | None:
        """Receipt-only lookup. It never reserves, sends or repairs anything; None means the connector has no record."""
        value = self._exchange("/managed/receipt", {"request_id": request_id, "account_ref": account_ref}, timeout=10)
        if value.get("found") is False:
            return None
        receipt = value.get("receipt")
        if value.get("found") is not True or not isinstance(receipt, dict):
            raise DomainError("external_result_invalid", "The connector returned an invalid receipt lookup")
        return receipt


def check_receipt(receipt, *, request_id: str, chat_id: str, text: str, account_ref: str) -> dict:
    """Bind a connector receipt to this exact request, recipient, payload and account, or refuse it. Returns the redacted
    receipt the host may store: identity and state only, never the recipient or the text."""
    invalid = DomainError("external_result_invalid", "The personal WhatsApp connector returned a receipt for another request")
    if not isinstance(receipt, dict):
        raise invalid
    if (receipt.get("request_id") != request_id or receipt.get("chat_id") != chat_id
            or receipt.get("account_ref") != account_ref
            or receipt.get("fingerprint") != payload_fingerprint(chat_id, text)):
        raise invalid
    state, message_id, sent_at = receipt.get("state"), receipt.get("message_id"), receipt.get("sent_at", 0)
    if (state not in STATES or not isinstance(message_id, str) or not 1 <= len(message_id) <= 256
            or type(sent_at) is not int or sent_at < 0):
        raise invalid
    return {"request_id": request_id, "message_id": message_id, "state": state, "sent_at": sent_at,
            "account_ref": account_ref, "fingerprint": receipt["fingerprint"]}
