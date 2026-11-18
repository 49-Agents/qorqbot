"""The control protocol between the service (``HostClient``) and a managed agent host: NDJSON over a Unix-domain socket.

One JSON object per line, UTF-8, at most ``MAX_LINE`` bytes. A connection carries requests one at a time; the host answers each
with exactly one line. Every request is ``{"id", "op", "token", ...arguments}``; every response echoes ``id`` and has a ``status``:

    {"id", "status": "ok", ...fields}                     the operation was carried out (or was a harmless no-op, see fields)
    {"id", "status": "rejected", "reason", ...}           nothing was done; ``reason`` is one of REJECT_REASONS
    {"id", "status": "error", "error": code, "message"}   the request was bad or not allowed; ``code`` is one of ERROR_CODES

Operations and their arguments:

    hello      -                                   identity of the host and its Claude process
    status     -                                   phase, activity, pending requests, last turn, identity (state, not events)
    send       text, message_uuid, steer=false, confirm_seconds=3
    lookup     message_uuid, fence=false           was this uuid accepted? ``fence`` makes an unseen uuid "never accept"
    interrupt  -                                   stop the running turn
    answer     request_id, decision                reply to a permission request or question; ``decision`` is true/false, "allow"/"deny"
                                                   or an object limited to ``behavior`` ("allow"|"deny"), ``message``, ``interrupt``
                                                   and ``answers`` (AskUserQuestion only); other keys (``updated_input``,
                                                   ``updated_permissions`` ...) are dropped, never passed to the CLI
    close      mode="graceful"|"now"               end the session and exit
    events     since=0, limit=200                  events with ``seq > since``
"""

from __future__ import annotations

import json
import re
import uuid

MAX_LINE = 4 * 1024 * 1024        # a message may be 2 MiB, JSON-escaped
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_DECISION_BYTES = 64 * 1024

OPS = ("hello", "status", "send", "lookup", "interrupt", "answer", "close", "events")
STATUS_OK, STATUS_REJECTED, STATUS_ERROR = "ok", "rejected", "error"

REJECT_SESSION_STARTING = "session_starting"
REJECT_SESSION_CLOSING = "session_closing"
REJECT_SESSION_CLOSED = "session_closed"
REJECT_TURN_RUNNING = "turn_running"
REJECT_HOST_BUSY = "host_busy"
REJECT_FENCED = "fenced"
REJECT_REASONS = (REJECT_SESSION_STARTING, REJECT_SESSION_CLOSING, REJECT_SESSION_CLOSED, REJECT_TURN_RUNNING,
                  REJECT_HOST_BUSY, REJECT_FENCED)

ERROR_BAD_REQUEST, ERROR_UNAUTHORIZED, ERROR_UNKNOWN_OP = "bad_request", "unauthorized", "unknown_op"
ERROR_TOO_LARGE, ERROR_INTERNAL = "too_large", "internal"
ERROR_CODES = (ERROR_BAD_REQUEST, ERROR_UNAUTHORIZED, ERROR_UNKNOWN_OP, ERROR_TOO_LARGE, ERROR_INTERNAL)

PHASES = ("starting", "idle", "working", "blocked", "closing")
SEND_MODES = ("started", "queued")
CLOSE_MODES = ("graceful", "now")

# Event types a host records: the driver's (session_started, turn_started, activity, permission_request, question,
# request_cancelled, prompt_blocked, turn_finished, error, exited) and the host's own.
HOST_EVENTS = ("host_state", "role_sent")

DECISION_KEYS = ("behavior", "message", "interrupt", "answers")

DEFAULT_CONFIRM_SECONDS = 3.0
MAX_CONFIRM_SECONDS = 10.0
DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT = 200, 1000

_ID = re.compile(r"[\x21-\x7e]{1,64}")
_REQUEST_ID = re.compile(r"[\x21-\x7e]{1,200}")


class ProtocolError(ValueError):
    """A line or a request that is not valid protocol. ``code`` is the error code a host answers with."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


# -- framing -----------------------------------------------------------------------------------------------------------------

def encode(message: dict) -> bytes:
    """One NDJSON line, newline included. Raises ProtocolError when it would exceed ``MAX_LINE``."""
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) > MAX_LINE:
        raise ProtocolError(ERROR_TOO_LARGE, "message too large")
    return line


def decode(line: bytes) -> dict:
    if len(line) > MAX_LINE + 1:
        raise ProtocolError(ERROR_TOO_LARGE, "line too long")
    try:
        value = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError(ERROR_BAD_REQUEST, "not valid JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError(ERROR_BAD_REQUEST, "not a JSON object")
    return value


class LineReader:
    """Reads newline-terminated lines from a socket, never holding more than ``MAX_LINE`` bytes. ``readline`` returns the line
    without its newline, ``None`` at end of stream, and raises ProtocolError(too_large) for an over-long line. Socket timeouts
    and errors propagate."""

    def __init__(self, connection, limit: int = MAX_LINE):
        self._connection, self._limit, self._buffer = connection, limit, bytearray()

    def readline(self):
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[:index + 1]
                return line
            if len(self._buffer) > self._limit:
                raise ProtocolError(ERROR_TOO_LARGE, "line too long")
            chunk = self._connection.recv(65536)
            if not chunk:
                if self._buffer:
                    line, self._buffer = bytes(self._buffer), bytearray()
                    return line
                return None
            self._buffer += chunk


# -- requests ----------------------------------------------------------------------------------------------------------------

def request(op: str, request_id, token: str, /, **arguments) -> dict:
    """A request message. ``None`` arguments are left out."""
    message = {"id": request_id, "op": op, "token": token}
    message.update({key: value for key, value in arguments.items() if value is not None})
    return message


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _uuid(value, name):
    if not isinstance(value, str):
        raise ProtocolError(ERROR_BAD_REQUEST, name + " must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ProtocolError(ERROR_BAD_REQUEST, name + " must be a UUID") from None


def _flag(value, name, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProtocolError(ERROR_BAD_REQUEST, name + " must be true or false")
    return value


def _number(value, name, default, low, high):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ProtocolError(ERROR_BAD_REQUEST, "%s must be a number from %s to %s" % (name, low, high))
    return value


def sanitize_decision(decision):
    """The part of an ``answer`` decision the host forwards: a bool or string as given, an object reduced to ``DECISION_KEYS``
    with the right types (``behavior`` allow/deny, ``message`` string, ``interrupt`` bool, ``answers`` object). Anything else in
    the object, notably ``updated_input`` and ``updated_permissions``, is dropped: a caller can allow or deny a request or answer
    a question, not rewrite the tool call or grant standing permissions."""
    if not isinstance(decision, dict):
        return decision
    clean = {key: decision[key] for key in DECISION_KEYS if key in decision}
    behavior = clean.get("behavior")
    if behavior is not None and (not isinstance(behavior, str) or behavior.strip().lower() not in ("allow", "deny")):
        raise ProtocolError(ERROR_BAD_REQUEST, "behavior must be allow or deny")
    if "message" in clean and not isinstance(clean["message"], str):
        raise ProtocolError(ERROR_BAD_REQUEST, "message must be a string")
    if "interrupt" in clean and not isinstance(clean["interrupt"], bool):
        raise ProtocolError(ERROR_BAD_REQUEST, "interrupt must be true or false")
    if "answers" in clean and not isinstance(clean["answers"], dict):
        raise ProtocolError(ERROR_BAD_REQUEST, "answers must be an object")
    return clean


def validate_request(message: dict):
    """``(id, op, arguments)`` with the arguments checked and normalised (uuids canonical, defaults filled), or ProtocolError.

    The token is not checked here; the host does that first, in constant time."""
    request_id = message.get("id")
    if isinstance(request_id, int) and not isinstance(request_id, bool):
        request_id = str(request_id)
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ProtocolError(ERROR_BAD_REQUEST, "id must be a short printable string")
    op = message.get("op")
    if op not in OPS:
        raise ProtocolError(ERROR_UNKNOWN_OP, "unknown operation")
    arguments: dict = {}
    if op == "send":
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError(ERROR_BAD_REQUEST, "text must be a non-empty string")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ProtocolError(ERROR_TOO_LARGE, "text too large")
        arguments = {"text": text, "message_uuid": _uuid(message.get("message_uuid"), "message_uuid"),
                     "steer": _flag(message.get("steer"), "steer"),
                     "confirm_seconds": float(_number(message.get("confirm_seconds"), "confirm_seconds",
                                                      DEFAULT_CONFIRM_SECONDS, 0, MAX_CONFIRM_SECONDS))}
    elif op == "lookup":
        arguments = {"message_uuid": _uuid(message.get("message_uuid"), "message_uuid"),
                     "fence": _flag(message.get("fence"), "fence")}
    elif op == "answer":
        identifier = message.get("request_id")
        if not isinstance(identifier, str) or not _REQUEST_ID.fullmatch(identifier):
            raise ProtocolError(ERROR_BAD_REQUEST, "request_id must be a printable string")
        decision = message.get("decision")
        if not isinstance(decision, (bool, str, dict)):
            raise ProtocolError(ERROR_BAD_REQUEST, "decision must be a boolean, a string or an object")
        if len(json.dumps(decision).encode("utf-8")) > MAX_DECISION_BYTES:
            raise ProtocolError(ERROR_TOO_LARGE, "decision too large")
        arguments = {"request_id": identifier, "decision": sanitize_decision(decision)}
    elif op == "close":
        mode = message.get("mode", "graceful")
        if mode not in CLOSE_MODES:
            raise ProtocolError(ERROR_BAD_REQUEST, "mode must be graceful or now")
        arguments = {"mode": mode}
    elif op == "events":
        arguments = {"since": int(_number(message.get("since"), "since", 0, 0, 2 ** 53)),
                     "limit": int(_number(message.get("limit"), "limit", DEFAULT_EVENT_LIMIT, 1, MAX_EVENT_LIMIT))}
    return request_id, op, arguments


# -- responses ---------------------------------------------------------------------------------------------------------------

def ok(request_id, /, **fields) -> dict:
    return {"id": request_id, "status": STATUS_OK, **fields}


def rejected(request_id, reason: str, /, **fields) -> dict:
    return {"id": request_id, "status": STATUS_REJECTED, "reason": reason, **fields}


def error(request_id, code: str, message: str = "") -> dict:
    return {"id": request_id, "status": STATUS_ERROR, "error": code, "message": message or code}


def check_response(message: dict, request_id) -> dict:
    """The response if it answers ``request_id`` and has a known status; ProtocolError otherwise."""
    if message.get("id") != request_id and not (message.get("id") is None and message.get("status") == STATUS_ERROR):
        raise ProtocolError(ERROR_BAD_REQUEST, "response does not match the request")
    if message.get("status") not in (STATUS_OK, STATUS_REJECTED, STATUS_ERROR):
        raise ProtocolError(ERROR_BAD_REQUEST, "response has no status")
    return message
