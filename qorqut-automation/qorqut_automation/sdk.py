"""Run-bound SDK for a staged automation script (standard library only; no host import).

The host starts the script as a child process with one inherited socket (``QORQUT_AUTOMATION_FD``). Every capability below is a
request to the host bridge, which calls Ordubashy's operations with the run's bound identity: the script holds no credential and
cannot widen its authority. Authority is not an operating-system sandbox; the script process keeps the host user's OS access.

Durable replay. A run that waits (for an owner decision or an action's outcome), continues or crashes is started again from the top of the
script. Every ``call``, ``ask_owner``, ``ask_agent`` and ``publish`` carries a stable ``key``; a key that already finished returns its
recorded result without calling again, so a script must derive its keys deterministically (for example from a cursor and an index), never
from the clock or randomness. A different request under an existing key is refused.

Invocation ceiling. One invocation of the script is bounded (``run.invocation_seconds``, 60 seconds by default); the run itself is bounded
by an active-time total. Longer work saves its progress as JSON and continues explicitly::

    import qorqut_automation_sdk as sdk   # the host puts this file on the child's PYTHONPATH

    def work(run):
        position = (run.progress or {}).get("position", 0)
        while position < 1000:
            if run.remaining() < 15:
                run.continue_run({"position": position})     # ends this invocation; the run restarts with run.progress
            run.call("some_read", {"offset": position}, key="read-%d" % position)
            position += 50
        return {"done": position}

    sdk.main(work)
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time

EXIT_SUSPEND = 75
MAX_FRAME = 8 * 1024 * 1024


class AutomationError(Exception):
    """A refused or failed host request. ``code`` is the host's stable error code."""

    def __init__(self, code, message="", retryable=False, details=None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}


class Suspend(BaseException):
    """Raised when the run must stop until a decision or outcome arrives. Deliberately not an ``Exception``: a script's
    ``except Exception`` cannot swallow it, and the host restarts the script when the wait is settled."""


class Continue(BaseException):
    """Raised by ``continue_run`` after the host saved the progress: this invocation ends and the run restarts at once."""


def _send(sock, value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise AutomationError("request_too_large", "The request exceeds the bridge frame limit")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _read(sock, count):
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise AutomationError("bridge_closed", "The host closed the run bridge", retryable=False)
        buffer += chunk
    return bytes(buffer)


class Run:
    def __init__(self, sock=None):
        if sock is None:
            sock = socket.socket(fileno=int(os.environ["QORQUT_AUTOMATION_FD"]))
        self._sock, self._next = sock, 0
        hello = self._request("hello", {})
        self._started = time.monotonic()
        self.id = hello["run_id"]
        self.automation = hello["automation"]
        self.input = hello["input"]
        self.saved = hello["saved"]
        self.phases = hello["phases"]
        self.phase_name = hello.get("phase")
        self.progress = hello.get("progress")
        self.invocation_seconds = hello.get("invocation_seconds") or 60

    def _request(self, method, params):
        self._next += 1
        _send(self._sock, {"id": self._next, "method": method, "params": params})
        (size,) = struct.unpack(">I", _read(self._sock, 4))
        if size > MAX_FRAME:
            raise AutomationError("response_too_large", "The host response exceeds the bridge frame limit")
        reply = json.loads(_read(self._sock, size))
        if reply.get("suspend"):
            raise Suspend()
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise AutomationError(error.get("code", "bridge_error"), error.get("message", ""), bool(error.get("retryable")),
                                  error.get("details"))
        return reply.get("result")

    # -- time -----------------------------------------------------------------------------------------------------
    def remaining(self):
        """Seconds left in this invocation (waits end an invocation, so only active work counts)."""
        return self.invocation_seconds - (time.monotonic() - self._started)

    # -- capabilities ---------------------------------------------------------------------------------------------
    def call(self, operation, arguments=None, *, key, approval=None):
        """Call one operation as the bound Ordubashy and return its recorded result.

        Sending, replying, forwarding and other supported actions return only after the owner approved the exact action *and* the
        action reported its real outcome (the run waits and restarts meanwhile; the result carries what was actually approved and done).
        ``approval="required"`` additionally asks for the operation's full owner approval where the operation supports one. A refusal
        raises ``AutomationError``."""
        params = {"operation": operation, "arguments": arguments or {}, "key": key}
        if approval is not None:
            params["approval"] = approval
        return self._request("call", params)

    def ask_owner(self, key, form, attention=None):
        """Ask the owner a question (or for input) and wait for the answer; returns ``{"selected_choices", "notes", ...}``.

        ``form`` is the input-request form; ``response_policy`` is ``owner_only`` (default) or, for a plain question,
        ``management_allowed`` (the managing agent may answer instead)."""
        return self._request("ask_owner", {"key": key, "form": form, "attention": attention})

    def ask_agent(self, key, form, attention=None):
        """Ask the managing agent (the configured Ordubashy) a question and wait for its answer; the owner is not asked."""
        return self._request("ask_agent", {"key": key, "form": form, "attention": attention})

    ask = ask_owner

    def publish(self, key, summary, data=None, advance=None):
        """Durably publish one output event (idempotent by ``key``). ``advance`` is a state patch applied after, and in the
        same durable step as, the event, so a saved cursor can never get ahead of its published output."""
        return self._request("publish", {"key": key, "summary": summary, "data": data, "advance": advance})

    def phase(self, name):
        """Record entry into an explicitly saved phase (declared on the automation)."""
        result = self._request("phase", {"name": name})
        self.phase_name = name
        return result

    def save(self, patch):
        """Merge ``patch`` into the saved state carried to later runs (keep it small; use ``publish(advance=...)`` for cursors)."""
        self.saved = self._request("save", {"patch": patch})
        return self.saved

    def continue_run(self, progress=None):
        """Save ``progress`` (bounded JSON, readable as ``run.progress``) and end this invocation; the run restarts at once."""
        self._request("continue", {"progress": progress})
        raise Continue()

    def finish(self, result=None):
        return self._request("finish", {"result": result})

    def fail(self, message):
        return self._request("fail", {"message": str(message)[:1000]})


def connect():
    return Run()


def main(work):
    """Run ``work(run)`` and report its outcome. A suspension or continuation exits quietly; the host records the wait."""
    try:
        run = Run()
    except AutomationError as exc:
        print(f"automation bridge unavailable: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        result = work(run)
    except (Suspend, Continue):
        sys.exit(EXIT_SUSPEND)
    except AutomationError as exc:
        _safely(run.fail, f"{exc.code}: {exc.message}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - the script's own failure is reported, not raised through the bridge
        _safely(run.fail, f"{type(exc).__name__}: {exc}")
        sys.exit(1)
    try:
        run.finish(result)
    except (Suspend, Continue):
        sys.exit(EXIT_SUSPEND)
    sys.exit(0)


def _safely(call, *args):
    try:
        call(*args)
    except BaseException:  # noqa: BLE001
        pass
