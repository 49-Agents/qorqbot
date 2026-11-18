"""``HostClient``: the service's side of the control protocol with one managed agent host.

Every call opens a short connection, so a host that went away is noticed on the next call. The client never trusts a socket just
because it exists: it requires that the peer is this user and (where the platform says) that the peer's pid is the ``host_pid``
in ``host.json``; and it pins the host ``instance`` on its first call, so a later host in the same directory is never mistaken
for the one a caller verified.

Outcomes: a dict for ``status`` ``ok`` and ``rejected`` replies (``reply["status"]``; a rejection has ``reason``), and
``HostUnavailable`` for everything else. ``HostUnavailable.reason`` is one of ``no_host``, ``not_running``, ``no_token``,
``unreachable``, ``unverified``, ``instance_changed``, ``refused``, ``timeout``, ``connection_lost``, ``protocol``; ``sent`` is
True when the request may already have reached the host (the connection was made and writing began), which is what a caller
needs to decide between "nothing happened" and "ask ``lookup``".
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from . import protocol, state as layout

DEFAULT_TIMEOUT = 5.0
SEND_TIMEOUT = 10.0
LOOKUP_TIMEOUT = 2.0


class HostUnavailable(RuntimeError):
    def __init__(self, reason: str, message: str = "", *, sent: bool = False):
        super().__init__(message or reason)
        self.reason, self.sent = reason, sent


class HostClient:
    def __init__(self, state_dir, *, timeout: float = DEFAULT_TIMEOUT, expect_instance: str | None = None,
                 verify_peer: bool = True):
        self.state_dir = Path(state_dir)
        self.timeout = timeout
        self.instance = expect_instance
        self.verify_peer = verify_peer

    # -- operations -----------------------------------------------------------------------------------------------------------
    def hello(self, timeout=None) -> dict:
        return self._call("hello", timeout=timeout)

    def status(self, timeout=None) -> dict:
        return self._call("status", timeout=timeout)

    def send(self, text: str, message_uuid: str, steer: bool = False, *, confirm_seconds=None, timeout=SEND_TIMEOUT) -> dict:
        """``{"status":"ok","mode":"started"|"queued","turn_started":bool,"duplicate":bool}`` or a rejection (``reason`` one of
        session_starting, session_closing, session_closed, turn_running, host_busy, fenced)."""
        return self._call("send", timeout=timeout, text=text, message_uuid=message_uuid, steer=bool(steer),
                          confirm_seconds=confirm_seconds)

    def lookup(self, message_uuid: str, fence: bool = False, *, timeout=LOOKUP_TIMEOUT) -> dict:
        """``{"status":"ok","seen":bool,"fenced":bool,...}``; for a seen uuid also ``mode``, ``turn_started``, ``finished`` and
        ``outcome`` (``completed``, ``interrupted``, ``error``, ``blocked`` for a hook-blocked prompt, ``dropped``; ``None`` until
        finished). With ``fence`` an unseen uuid is recorded as never accepted."""
        return self._call("lookup", timeout=timeout, message_uuid=message_uuid, fence=bool(fence))

    def interrupt(self, timeout=None) -> dict:
        return self._call("interrupt", timeout=timeout)

    def answer(self, request_id: str, decision, timeout=None) -> dict:
        """``decision``: True/False, "allow"/"deny", or ``{"behavior", "message", "interrupt", "answers"}``; anything else in an
        object is dropped by the protocol. A decision the request cannot take (``answers`` for a permission request, no behavior)
        raises ``HostUnavailable("protocol")`` and the request stays pending."""
        return self._call("answer", timeout=timeout, request_id=request_id, decision=decision)

    def close(self, mode: str = "graceful", timeout=None) -> dict:
        return self._call("close", timeout=timeout, mode=mode)

    def events(self, since: int = 0, limit=None, timeout=None) -> dict:
        """``{"events":[...], "next", "seq", "oldest", "gap", "instance"}``; ``gap`` means events after ``since`` were dropped."""
        return self._call("events", timeout=timeout, since=int(since), limit=limit)

    # -- transport ------------------------------------------------------------------------------------------------------------
    def _locate(self):
        record = layout.read_host(self.state_dir)
        if record is None:
            raise HostUnavailable("no_host", "no host record")
        if record["state"] not in layout.LIVE_STATES:
            raise HostUnavailable("not_running", "the host is not running")
        if record.get("protocol") != layout.PROTOCOL:
            raise HostUnavailable("protocol", "unsupported host protocol")
        if self.instance is not None and record.get("instance") != self.instance:
            raise HostUnavailable("instance_changed", "another host instance is running")
        token = layout.read_token(self.state_dir)
        if token is None:
            raise HostUnavailable("no_token", "no control token")
        path = record.get("socket_path") if record.get("socket_fallback") else str(self.state_dir / layout.SOCKET_FILE)
        if not isinstance(path, str) or not isinstance(record.get("host_pid"), int):
            raise HostUnavailable("no_host", "incomplete host record")
        return record, token, path

    def _call(self, op: str, timeout=None, **arguments) -> dict:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        record, token, path = self._locate()
        if self.instance is None:
            self.instance = record.get("instance")        # pinned from now on

        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout()
            return left

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sent = False
        try:
            try:
                connection.settimeout(remaining())
                connection.connect(path)
            except socket.timeout:
                raise HostUnavailable("timeout", "connecting timed out") from None
            except OSError:
                raise HostUnavailable("unreachable", "cannot connect to the host") from None
            if self.verify_peer:
                pid, uid = layout.peer_credentials(connection)
                if uid != os.geteuid() or pid != record["host_pid"]:
                    raise HostUnavailable("unverified", "the socket is not held by the recorded host")
            request_id = protocol.new_request_id()
            try:
                data = protocol.encode(protocol.request(op, request_id, token, **arguments))
            except protocol.ProtocolError:
                raise HostUnavailable("protocol", "request too large") from None
            try:
                sent = True
                connection.settimeout(remaining())
                connection.sendall(data)
                reader = protocol.LineReader(connection)
                while True:
                    connection.settimeout(remaining())
                    line = reader.readline()
                    if line is None:
                        raise HostUnavailable("connection_lost", "the host closed the connection", sent=True)
                    reply = protocol.check_response(protocol.decode(line), request_id)
                    break
            except socket.timeout:
                raise HostUnavailable("timeout", "the host did not answer in time", sent=True) from None
            except protocol.ProtocolError:
                raise HostUnavailable("protocol", "invalid reply from the host", sent=True) from None
            except OSError:
                raise HostUnavailable("connection_lost", "the connection to the host was lost", sent=True) from None
        finally:
            connection.close()
        if reply["status"] == protocol.STATUS_ERROR:
            code = reply.get("error")
            raise HostUnavailable("refused" if code == protocol.ERROR_UNAUTHORIZED else "protocol",
                                  "the host answered: %s" % code, sent=sent)
        if op in ("hello", "status") and reply.get("status") == protocol.STATUS_OK:
            if reply.get("instance") != record.get("instance") or reply.get("host_pid") != record["host_pid"]:
                raise HostUnavailable("unverified", "the host does not match its record", sent=True)
        return reply
