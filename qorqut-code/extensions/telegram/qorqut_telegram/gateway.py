"""Local program gateway: a Unix-domain socket for registered local programs to post Urgent Notifications.

The socket lives in an owner-only directory (socket mode 0600). One request per connection: a single JSON line of at most
64 KiB. A caller is identified by its Linux peer UID and a per-producer bearer secret whose SHA-256 is the only thing the
configuration stores; the producer identity and the target chat are never taken from the request. The gateway offers
``notify`` and the status of that producer's own notifications, builds no owner context and is not a general proxy. There
is no TCP listener. A disabled or removed credential is refused immediately and cancels that producer's queued sends.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time

from bazarlyk.private_files import require_private_directory
from qorqut_operations.contracts import DomainError
from . import notify as urgent
from . import store

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_DEADLINE_SECONDS = 3.0
MAX_CONNECTIONS = 8
FORBIDDEN_FIELDS = ("producer", "producer_id", "chat", "chat_id", "target", "agent_id", "token")
ACTIONS = {"notify", "status"}


def _error(code, message, retryable=False):
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


class Gateway:
    def __init__(self, bridge):
        self.bridge = bridge
        self.path = None
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._workers = set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ----- lifecycle -----------------------------------------------------------------------------------

    def start(self, config):
        """Bind the socket. The caller holds the installation's poller lock, so a leftover socket file is stale."""
        if self.running:
            return
        gateway = config.gateway
        if not gateway.enabled or gateway.socket_path is None:
            return
        path = Path(gateway.socket_path)
        require_private_directory(path.parent, create=True)
        if os.path.lexists(path):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise DomainError("invalid_configuration", "The gateway socket path is occupied by something that is not a socket")
            os.unlink(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old = os.umask(0o177)
            try:
                server.bind(str(path))
            finally:
                os.umask(old)
            os.chmod(path, 0o600)
            server.listen(MAX_CONNECTIONS)
            server.settimeout(0.5)
        except BaseException:
            server.close()
            raise
        self._server, self.path = server, path
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="telegram-gateway", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        thread, server, path = self._thread, self._server, self.path
        if thread is not None:
            thread.join(timeout)
        for worker in list(self._workers):
            worker.join(max(0.0, timeout / 2))
        if server is not None:
            server.close()
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._thread = self._server = self.path = None

    # ----- serving -------------------------------------------------------------------------------------

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            worker = threading.Thread(target=self._handle, args=(connection,), name="telegram-gateway-request", daemon=True)
            self._workers.add(worker)
            worker.start()

    def _handle(self, connection):
        try:
            with connection:
                response = self._respond(connection)
                try:
                    connection.settimeout(REQUEST_DEADLINE_SECONDS)
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
                except OSError:
                    pass
        finally:
            self._workers.discard(threading.current_thread())
            self._slots.release()

    @staticmethod
    def peer_uid(connection):
        pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid

    def _read(self, connection):
        deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
        data = b""
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DomainError("timeout", "The request was not completed in time")
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(4096)
            except (socket.timeout, OSError):
                raise DomainError("timeout", "The request was not completed in time") from None
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REQUEST_BYTES + 1:
                raise DomainError("payload_too_large", "A gateway request is at most 64 KiB")
        line = data.split(b"\n", 1)[0]
        if len(line) > MAX_REQUEST_BYTES:
            raise DomainError("payload_too_large", "A gateway request is at most 64 KiB")
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DomainError("invalid_request", "A gateway request is one JSON object on one line") from None
        if not isinstance(request, dict):
            raise DomainError("invalid_request", "A gateway request is one JSON object on one line")
        return request

    def authenticate(self, connection, request):
        """The configured producer for this peer and secret, or None. Every candidate is compared: no early exit."""
        secret = request.get("secret")
        if type(secret) is not str or not 16 <= len(secret) <= 512:
            return None
        digest = hashlib.sha256(secret.encode()).hexdigest()
        try:
            uid = self.peer_uid(connection)
        except OSError:
            return None
        found = None
        for producer in self.bridge.config.gateway.producers:
            secret_matches = hmac.compare_digest(digest, producer.secret_sha256)
            expected = os.geteuid() if producer.peer_uid is None else producer.peer_uid
            if secret_matches and uid == expected and producer.enabled:
                found = producer
        return found if self.bridge.config.gateway.enabled and self.bridge.config.enabled else None

    def _respond(self, connection):
        try:
            request = self._read(connection)
            producer = self.authenticate(connection, request)
            if producer is None:
                return _error("unauthenticated", "The credential or peer is not accepted")
            action = request.get("action")
            if action not in ACTIONS:
                return _error("invalid_request", "action must be notify or status")
            body = {key: value for key, value in request.items() if key not in ("secret", "action", "version")}
            for name in FORBIDDEN_FIELDS:
                if name in body:
                    return _error("validation_error", f"{name} is chosen by the service and cannot be supplied")
            if request.get("version", 1) != 1:
                return _error("invalid_request", "Unsupported gateway protocol version")
            if action == "notify":
                unknown = set(body) - set(urgent.INPUT_SCHEMA)
                if unknown:
                    return _error("validation_error", "Unknown field: " + sorted(unknown)[0])
                return self._notify(producer, body)
            unknown = set(body) - {"event_key"}
            if unknown or type(body.get("event_key")) is not str:
                return _error("validation_error", "status takes exactly event_key")
            return self._status(producer, body["event_key"])
        except DomainError as exc:
            return _error(exc.code, exc.message, exc.retryable)
        except Exception:  # noqa: BLE001 - one bad request must never take the gateway or the service down
            LOGGER.exception("Telegram gateway request failed")
            return _error("internal_error", "The request failed")

    def _notify(self, producer, body):
        bridge = self.bridge
        with bridge.write("gateway.notify") as uow:
            result = urgent.notify(bridge, uow, f"gateway:{producer.producer_id}", body, gateway_producer=producer.producer_id)
        return {"ok": True, "result": result}

    def _status(self, producer, event_key):
        with self.bridge.read() as uow:
            row = uow.repository("telegram_events").get((f"gateway:{producer.producer_id}", event_key))
            if row is None:
                raise DomainError("not_found", "No such notification for this producer")
            outbox = store.outbox(uow, row["last_outbox_id"]) if row["last_outbox_id"] else None
        return {"ok": True, "result": {
            "event_key": event_key, "state": row["state"], "occurrences": row["occurrences"],
            "delivery_state": outbox["state"] if outbox else None, "error_code": outbox["error_code"] if outbox else None,
            "sent_at": outbox["sent_at"] if outbox else None}}
