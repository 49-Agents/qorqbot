"""One detached host process per open managed agent session.

``python -m qorqut.managed_runtime.host --state <runtime>/managed-agents/<agent_id>`` is started detached by the launcher with
this fixed argv; everything else comes from the private ``launch.json`` the launcher wrote first, which the host reads and unlinks
at once (it carries the agent's resolved environment, so it never travels in argv). The host owns one ``ClaudeSession`` (the
driver class is injected: ``launch.json`` names it as ``module:Class``, default ``claude_driver:ClaudeSession``), a control socket
speaking ``protocol.py``, and the event log, so a service restart or release does not end a running turn.

It holds ``host.lock`` for its whole life (a second copy exits 75, after retrying for about a second so that a probe holding the
lock for an instant does not count as a second host), binds the control socket (0600), and refuses any connection
whose peer is not this user (SO_PEERCRED / LOCAL_PEERCRED) or whose request lacks the token. ``status`` and ``pending`` are state,
re-read after a service restart; the event log (monotonic ``seq``, bounded) is for latency and history. Every accepted message
uuid is kept in a ledger, so a client that lost an answer asks ``lookup`` and, if the host has not seen the uuid, fences it
("never accept") so a late arrival cannot be typed after the client gave up on it.

Ready: the real CLI announces its session (``system``/``init``) only after the first message was taken up, so the host does not wait
for that. It is ready once the Claude process has started and stayed alive for ``ready_seconds`` (default 0.4; the session id is the
one the driver chose or resumed), or earlier when the driver announces the session. ``ready_seconds`` 0 turns this off (only the
announcement makes it ready). The transcript file does not exist until a message was processed: ``transcript_path`` is reported
anyway and ``transcript_exists`` says whether the file is there yet.

Permissions: the owner is never asked. A ``permission_request`` from the driver is answered by the host itself (``_answer_permission``):
allow when ``launch.json`` says ``permission_mode`` ``bypass`` (the manager reads the agent's prepared settings), deny for anything
else, including a missing or unknown mode. It is logged (``permission_request`` without its input, then ``permission_answered``) and never
stored as pending, so ``pending`` only ever holds questions (``kind`` ``question``). ``launch.json`` also names the ``client`` (``claude``
or ``codex``; default ``claude``), which picks the default role command and is published in ``hello`` and ``host.json``.

Exit codes follow the browser supervisor: 0 stopped (closed on request, idle, or Claude ended cleanly), 1 failed, 75 already
supervised, 78 invalid configuration. ``host.json`` records ``exit{code, reason}``: reasons are ``requested``, ``idle``,
``signal``, ``claude_exited`` (unexpected), ``claude_ended``, ``driver_unavailable`` (the driver or CLI could not start),
``invalid_launch`` and ``host_error``.
"""

from __future__ import annotations

import argparse
import collections
import hmac
import importlib
import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from . import clients as table
from . import protocol, state as layout
from .state import (EXIT_BUSY, EXIT_CONFIG, EXIT_FAILED, EXIT_STOPPED, HOST_FILE, LAUNCH_FILE, LOCK_FILE, TOKEN_FILE)

TICK = 0.1
CONNECTION_TIMEOUT = 30.0
MAX_CONNECTIONS = 32
MAX_EVENT_BYTES = 64 * 1024
DEFAULT_IDLE_SECONDS = 900
DEFAULT_EVENT_LIMIT = 2000
DEFAULT_CLOSE_SECONDS = 20.0
DEFAULT_READY_SECONDS = 0.4
LEDGER_LIMIT = 5000
MAX_PENDING = 50
MAX_PENDING_ITEM_BYTES = 32 * 1024       # one pending request in status: input (16 KiB) + suggestions (8 KiB) + short fields
MAX_STATUS_BYTES = 1024 * 1024            # the whole status reply stays far under protocol.MAX_LINE (4 MiB)
MAX_OUTCOMES = 50
EVENT_DROP_KEYS = {                       # fields the event log (memory and events.jsonl) does not keep; status has what the UI needs
    "turn_finished": ("result_text",),
    "permission_request": ("input", "suggestions"),
    "permission_answered": ("input",),
    "question": ("input", "questions"),
}
RESERVED_EVENT_KEYS = ("seq", "ts", "type")
# What a client is told when a permission request is refused, according to the agent's prepared settings.
DENY_MESSAGE = "Qorqbot does not allow this action for this agent."


def log(message: str) -> None:
    """A diagnostic line on stderr (``host.log``). Never a token, an environment value or message text."""
    print("%s managed host: %s" % (layout.now_iso(), message), file=sys.stderr, flush=True)


def load_driver(path: str):
    module, _, name = str(path).partition(":")
    if not module or not name:
        raise ValueError("driver must be module:Class")
    return getattr(importlib.import_module(module), name)


class EventLog:
    """The bounded event log: the newest ``limit`` events in memory, mirrored to ``events.jsonl``, which is rewritten from memory
    whenever it grows past twice the limit."""

    def __init__(self, path: Path, limit: int):
        self.path, self.limit = path, max(1, int(limit))
        self.seq = 0
        self.items: collections.deque = collections.deque(maxlen=self.limit)
        self._lines = 0
        self._stream = self._open("wb")

    def _open(self, mode):
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if mode == "wb" else os.O_APPEND), 0o600)
        return os.fdopen(descriptor, "ab" if mode != "wb" else "wb")

    def append(self, kind: str, fields: dict) -> dict:
        self.seq += 1
        entry = {"seq": self.seq, "ts": layout.now_iso(), "type": kind}
        entry.update({key: value for key, value in fields.items() if key not in RESERVED_EVENT_KEYS})
        encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > MAX_EVENT_BYTES:
            entry = self._shrink(entry)
            encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str)
        self.items.append(entry)
        try:
            self._stream.write(encoded.encode("utf-8") + b"\n")
            self._stream.flush()
            self._lines += 1
            if self._lines > 2 * self.limit:
                self._compact()
        except OSError:
            pass
        return entry

    @staticmethod
    def _shrink(entry: dict) -> dict:
        """An oversized event reduced to its short scalar fields (ids, status, reasons), so ``request_id`` and ``message_uuid``
        survive; lists and objects are dropped."""
        kept = {"seq": entry["seq"], "ts": entry["ts"], "type": entry["type"], "truncated": True}
        for key, value in entry.items():
            if key in kept:
                continue
            if isinstance(value, str) and len(value) <= 300 or isinstance(value, (bool, int, float)) or value is None:
                kept[key] = value
            elif isinstance(value, str):
                kept[key] = value[:300]
            elif isinstance(value, list) and key.endswith("uuids") and all(isinstance(item, str) and len(item) <= 64 for item in value):
                kept[key] = value[:20]
        if len(json.dumps(kept, ensure_ascii=False, default=str)) > MAX_EVENT_BYTES // 2:
            kept = {key: kept[key] for key in ("seq", "ts", "type", "truncated", "request_id", "message_uuid") if key in kept}
        return kept

    def _compact(self):
        temporary = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for entry in self.items:
                stream.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8") + b"\n")
        os.replace(temporary, self.path)
        self._stream.close()
        self._stream = self._open("ab")
        self._lines = len(self.items)

    def since(self, seq: int, limit: int) -> dict:
        oldest = self.items[0]["seq"] if self.items else self.seq + 1
        events = [entry for entry in self.items if entry["seq"] > seq][:limit]
        last = events[-1]["seq"] if events else seq
        return {"events": events, "next": last, "seq": self.seq, "oldest": oldest, "gap": seq + 1 < oldest and self.seq > seq}

    def close(self):
        try:
            self._stream.close()
        except OSError:
            pass


class Ledger:
    """The uuid ledger: every accepted message uuid (``sent``) and every fenced one, bounded, mirrored to ``sent.jsonl``."""

    def __init__(self, path: Path, limit: int = LEDGER_LIMIT):
        self.path, self.limit = path, limit
        self.entries: collections.OrderedDict = collections.OrderedDict()
        self._lines = 0
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        self._stream = os.fdopen(descriptor, "wb")

    def get(self, message_uuid):
        return self.entries.get(message_uuid)

    def add(self, message_uuid: str, state: str, **extra) -> dict:
        entry = {"state": state, "turn_started": False, "finished": False, "outcome": None, "at": layout.now_iso(), **extra}
        self.entries[message_uuid] = entry
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        self._write(message_uuid, entry)
        return entry

    def _write(self, message_uuid, entry):
        record = {"uuid": message_uuid, "state": entry["state"], "mode": entry.get("mode"), "at": entry["at"]}
        try:
            self._stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n")
            self._stream.flush()
            self._lines += 1
            if self._lines > 2 * self.limit:
                self._stream.seek(0)
                self._stream.truncate()
                for key, value in self.entries.items():
                    self._stream.write(json.dumps({"uuid": key, "state": value["state"], "mode": value.get("mode"),
                                                   "at": value["at"]}, separators=(",", ":")).encode("utf-8") + b"\n")
                self._stream.flush()
                self._lines = len(self.entries)
        except OSError:
            pass

    def close(self):
        try:
            self._stream.close()
        except OSError:
            pass


class ManagedHost:
    """The host's state machine and control server. ``run`` blocks until the session is over and returns the exit code."""

    def __init__(self, state, *, driver_class=None, peer_credentials=None):
        self.state_dir = Path(state)
        self._driver_class = driver_class
        self._peer_credentials = peer_credentials or layout.peer_credentials
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._publish_lock = threading.Lock()
        self._version = self._written = 0
        self._ever_ready = False
        self._session_confirmed = False             # the CLI announced the session, or a message was accepted
        self._command = threading.Lock()           # held while a message is handed to the driver
        self._stop: threading.Event | None = None
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._server: socket.socket | None = None
        self._serving = False
        self._driver = None
        self._close_request = None                  # (mode, reason)
        self._driver_exit = None                    # (code, unexpected)
        self._ready_since = None
        # published record
        self.record: dict = {}
        self.phase_state = "starting"
        # session facts
        self.session_id = self.transcript_path = self.claude_pid = self.claude_identity = None
        self.activity = self.turn_started_at = None
        self.pending: collections.OrderedDict = collections.OrderedDict()
        self.inflight: collections.OrderedDict = collections.OrderedDict()
        self.last_turn: dict | None = None
        self.last_error = None
        self.last_activity_mono = time.monotonic()
        self.last_activity_at = layout.now_iso()
        self.events: EventLog | None = None
        self.ledger: Ledger | None = None
        self.launch: dict = {}
        self._token = ""
        self._socket_path: Path | None = None
        self._managed_host_file: Path | None = None

    # -- entry --------------------------------------------------------------------------------------------------------------
    def run(self, stop: threading.Event) -> int:
        state = self.state_dir
        try:
            if not state.is_absolute() or state.is_symlink() or not state.is_dir():
                raise ValueError("state directory")
            info = state.lstat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("state directory is not private")
            descriptor = os.open(state / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except (OSError, ValueError):
            log("invalid state directory; nothing was started")
            return EXIT_CONFIG
        if not layout.acquire_lock(descriptor):
            os.close(descriptor)
            return EXIT_BUSY
        try:
            return self._run_locked(stop)
        finally:
            os.close(descriptor)

    def _run_locked(self, stop):
        self._stop = stop
        state = self.state_dir
        try:
            self.launch = self._load_launch()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log("launch.json is missing or invalid; nothing was started")
            self._publish_failure("invalid_launch", EXIT_CONFIG)
            return EXIT_CONFIG
        self._token = self.launch["token"]
        launch = self.launch
        try:
            identity = layout.process_identity(os.getpid())
        except (OSError, ValueError, IndexError):
            identity = None
        try:
            self.events = EventLog(state / layout.EVENTS_FILE, launch["event_limit"])
            self.ledger = Ledger(state / layout.SENT_FILE)
            layout.write_private(state / TOKEN_FILE, self._token.encode("ascii"))
            self._socket_path, fallback = (Path(launch["socket_path"]), True) if launch.get("socket_path") else layout.socket_path_for(state)
            self._bind(self._socket_path)
        except (OSError, RuntimeError, ValueError) as error:
            log("could not set up the control socket: " + type(error).__name__)
            self._publish_failure("host_error", EXIT_FAILED)
            return EXIT_FAILED
        self.record = {"schema_version": layout.SCHEMA_VERSION, "agent_id": launch["agent_id"], "instance": launch["instance"],
                       "host_pid": os.getpid(), "host_identity": identity, "protocol": layout.PROTOCOL,
                       "state": "starting", "started_at": layout.now_iso(), "session_id": None, "transcript_path": None,
                       "claude_pid": None, "claude_identity": None, "last_activity_at": self.last_activity_at,
                       "socket_path": str(self._socket_path), "socket_fallback": fallback, "exit": None,
                       "client": launch.get("client") or table.CLAUDE}
        self._write_managed_host_file(identity)
        self._publish()
        self._serving = True
        threading.Thread(target=self._accept_loop, name="managed-host-accept", daemon=True).start()
        code, reason, detail = EXIT_FAILED, "host_error", None
        try:
            code, reason, detail = self._lifecycle()
        except Exception as error:   # the host must always record how it ended
            log("unexpected failure: " + type(error).__name__)
            detail = type(error).__name__
        finally:
            self._finish(code, reason, detail)
        return code

    # -- configuration -------------------------------------------------------------------------------------------------------
    def _load_launch(self) -> dict:
        path = self.state_dir / LAUNCH_FILE
        try:
            raw = layout.read_private(path, layout.LAUNCH_LIMIT)
        finally:
            try:
                path.unlink()                  # the environment inside is secret: gone as soon as it was read (or unreadable)
            except FileNotFoundError:
                pass
        launch = json.loads(raw)
        if not isinstance(launch, dict) or launch.get("schema_version") != layout.SCHEMA_VERSION:
            raise ValueError("schema")
        agent_id = launch["agent_id"]
        if str(uuid.UUID(agent_id)) != agent_id or agent_id != self.state_dir.name:
            raise ValueError("agent")
        token = launch["token"]
        if not isinstance(token, str) or not 16 <= len(token) <= 200 or not token.isascii() or not token.isprintable():
            raise ValueError("token")
        folder, executable = launch["folder"], launch["executable"]
        if not (isinstance(folder, str) and os.path.isabs(folder) and isinstance(executable, str) and executable):
            raise ValueError("paths")
        args = launch.get("args") or []
        env = launch.get("env") or {}
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("args")
        if not isinstance(env, dict) or not all(isinstance(key, str) and (value is None or isinstance(value, str))
                                                for key, value in env.items()):
            raise ValueError("env")
        host_env = launch.get("host_env") or {}
        if not isinstance(host_env, dict) or not all(isinstance(key, str) and key and isinstance(value, str)
                                                     for key, value in host_env.items()):
            raise ValueError("host_env")
        resume = launch.get("resume_session_id")
        if resume is not None and not isinstance(resume, str):
            raise ValueError("resume")
        role = launch.get("send_role", False)
        if not isinstance(role, (bool, str)):
            raise ValueError("send_role")
        number = lambda key, default: float(launch[key]) if launch.get(key) is not None else float(default)
        client = launch.get("client")
        if client is not None and not table.known(client):
            raise ValueError("client")
        # How permission requests are answered here. Anything but an explicit ``bypass`` is ``deny``: a launch that says nothing,
        # or something unrecognised, never allows a tool.
        permission_mode = table.BYPASS if launch.get("permission_mode") == table.BYPASS else table.DENY
        launch.update(client=client or table.CLAUDE, permission_mode=permission_mode, args=args, env=env, host_env=host_env, instance=str(launch.get("instance") or uuid.uuid4()),
                      idle_seconds=number("idle_seconds", DEFAULT_IDLE_SECONDS), close_seconds=number("close_seconds", DEFAULT_CLOSE_SECONDS),
                      ready_seconds=number("ready_seconds", DEFAULT_READY_SECONDS),
                      event_limit=int(number("event_limit", DEFAULT_EVENT_LIMIT)), send_role=role,
                      driver=str(launch.get("driver") or layout.DEFAULT_DRIVER))
        if launch.get("socket_path") is not None and not (isinstance(launch["socket_path"], str) and os.path.isabs(launch["socket_path"])):
            raise ValueError("socket_path")
        return launch

    # -- socket ---------------------------------------------------------------------------------------------------------------
    def _bind(self, path: Path) -> None:
        if not layout.socket_fits(path):
            raise RuntimeError("socket path too long")
        try:
            if path.is_socket():
                path.unlink()                  # a stale socket from a host that is gone (the lock proves nobody holds it)
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            server.bind(str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        server.listen(16)
        server.settimeout(0.25)
        self._server = server

    def _accept_loop(self):
        while self._serving:
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._connections.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self._connection, args=(connection,), name="managed-host-connection", daemon=True).start()

    def _connection(self, connection):
        try:
            with connection:
                connection.settimeout(CONNECTION_TIMEOUT)
                _pid, uid = self._peer_credentials(connection)
                if uid is None or uid != os.geteuid():
                    return                     # not this user (or the platform cannot say): not even an answer
                reader = protocol.LineReader(connection)
                while self._serving:
                    try:
                        line = reader.readline()
                    except protocol.ProtocolError as error:
                        self._reply(connection, protocol.error(None, error.code, str(error)))
                        return
                    except (OSError, socket.timeout):
                        return
                    if line is None:
                        return
                    try:
                        message = protocol.decode(line)
                    except protocol.ProtocolError as error:
                        self._reply(connection, protocol.error(None, error.code, str(error)))
                        return
                    reply = self._handle(message)
                    if not self._reply(connection, reply) or reply.get("error") == protocol.ERROR_UNAUTHORIZED:
                        return
        except OSError:
            return
        finally:
            self._connections.release()

    @staticmethod
    def _reply(connection, message) -> bool:
        try:
            connection.sendall(protocol.encode(message))
            return True
        except (OSError, protocol.ProtocolError):
            return False

    def _handle(self, message: dict) -> dict:
        request_id = message.get("id") if isinstance(message.get("id"), (str, int)) else None
        token = message.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            return protocol.error(request_id, protocol.ERROR_UNAUTHORIZED)
        try:
            request_id, op, arguments = protocol.validate_request(message)
        except protocol.ProtocolError as error:
            return protocol.error(request_id, error.code, str(error))
        try:
            return getattr(self, "_op_" + op)(request_id, arguments)
        except Exception as error:
            log("operation %s failed: %s" % (op, type(error).__name__))
            return protocol.error(request_id, protocol.ERROR_INTERNAL, "the host could not carry out the request")

    # -- state ----------------------------------------------------------------------------------------------------------------
    def _phase(self) -> str:
        if self.phase_state in ("closing", "closed", "failed"):
            return "closing"
        if self.phase_state == "starting":
            return "starting"
        if self.pending:
            return "blocked"
        return "working" if self.inflight else "idle"

    def _identity(self) -> dict:
        return {"agent_id": self.launch["agent_id"], "instance": self.launch["instance"], "host_pid": os.getpid(),
                "client": self.launch.get("client") or table.CLAUDE,
                "host_identity": self.record.get("host_identity"), "host_ticks": self._ticks(self.record.get("host_identity")),
                "state": self.phase_state, "protocol": layout.PROTOCOL,
                "session_id": self.session_id, "transcript_path": self.transcript_path,
                "transcript_exists": self._transcript_exists(), "claude_pid": self.claude_pid,
                "claude_identity": self.claude_identity, "claude_ticks": self._ticks(self.claude_identity)}

    def _transcript_exists(self) -> bool:
        """The transcript file is only created once a message was processed; its path is reported before that."""
        try:
            return bool(self.transcript_path) and os.path.isfile(self.transcript_path)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _ticks(identity):
        return layout.process_ticks(identity) if identity else None

    def _status(self) -> dict:
        pending = [dict(item) for item in self.pending.values()]
        status = {**self._identity(), "phase": self._phase(), "activity": self.activity, "turn_started_at": self.turn_started_at,
                  "pending": pending, "last_turn_status": (self.last_turn or {}).get("status"), "last_turn": self.last_turn,
                  "inflight": list(self.inflight)[:20], "seq": self.events.seq, "last_activity_at": self.last_activity_at,
                  "idle_seconds": self.launch["idle_seconds"], "last_error": self.last_error}
        if len(json.dumps(status, ensure_ascii=False, default=str)) > MAX_STATUS_BYTES:
            # never let the answer outgrow the frame (the session would be unreadable): keep every request id and the way to
            # answer it, drop what only decorates it
            status["pending"] = [{key: value for key, value in item.items()
                                  if key in ("request_id", "kind", "tool", "tool_use_id", "input_truncated", "decision_reason",
                                             "blocked_path", "title", "description")} | {"input": None, "input_truncated": True}
                                 for item in pending]
            status["status_truncated"] = True
        return status

    def _touch(self):
        self.last_activity_mono = time.monotonic()
        self.last_activity_at = layout.now_iso()

    def _publish(self, **fields):
        with self._lock:
            self.record.update(state=self.phase_state, session_id=self.session_id, transcript_path=self.transcript_path,
                               claude_pid=self.claude_pid, claude_identity=self.claude_identity,
                               last_activity_at=self.last_activity_at, **fields)
            self._version += 1
            version, snapshot = self._version, dict(self.record)
        with self._publish_lock:        # never takes the state lock, so a caller may hold it; a stale snapshot is skipped
            if version < self._written:
                return
            self._written = version
            try:
                layout.write_json_private(self.state_dir / HOST_FILE, snapshot)
            except OSError:
                log("could not write host.json")

    def _publish_failure(self, reason, code):
        """Before the host has a record of its own: leave one that says why nothing started."""
        if self.record:
            return
        try:
            layout.write_json_private(self.state_dir / HOST_FILE, {
                "schema_version": layout.SCHEMA_VERSION, "agent_id": self.state_dir.name, "instance": None,
                "host_pid": os.getpid(), "host_identity": None, "protocol": layout.PROTOCOL, "state": "failed",
                "started_at": layout.now_iso(), "session_id": None, "transcript_path": None, "claude_pid": None,
                "claude_identity": None, "last_activity_at": layout.now_iso(), "socket_path": None,
                "socket_fallback": False, "exit": {"code": code, "reason": reason}})
        except OSError:
            pass

    def _set_state(self, value: str):
        with self._lock:
            if self.phase_state == value:
                return
            self.phase_state = value
            self.events.append("host_state", {"state": value})
            self._cond.notify_all()
        self._publish()

    def _write_managed_host_file(self, identity):
        """``<workspace>/.qorqut/managed-host.json``: what the hook side needs to accept a headless claude whose ancestor is
        this host."""
        try:
            directory = Path(self.launch["folder"]) / ".qorqut"
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / "managed-host.json"
            layout.write_json_private(path, {
                "schema_version": layout.SCHEMA_VERSION, "host_id": self.launch["instance"],
                "host_pid": os.getpid(), "host_ticks": layout.process_ticks(identity) if identity else None,
                "agent_id": self.launch["agent_id"], "instance": self.launch["instance"]})
            self._managed_host_file = path
        except OSError:
            log("could not write managed-host.json")

    # -- the session ----------------------------------------------------------------------------------------------------------
    def _lifecycle(self):
        launch = self.launch
        for key, value in launch["host_env"].items():
            os.environ[key] = value     # credential-bearing variables (proxy URLs) travel in launch.json, not in argv; claude inherits them
        try:
            driver_class = self._driver_class or load_driver(launch["driver"])
        except (ImportError, AttributeError, ValueError):
            return EXIT_FAILED, "driver_unavailable", "driver_import_failed"
        try:
            self._driver = driver_class(launch["folder"], launch["executable"], env=launch["env"], args=tuple(launch["args"]),
                                        resume_session_id=launch.get("resume_session_id"), on_event=self._on_event)
            self._driver.start()
        except ValueError:
            self._record_error("The launch arguments were refused by the driver.")
            return EXIT_CONFIG, "invalid_launch", "driver_refused_arguments"
        except Exception as error:
            self._record_error("The session could not be started (%s)." % type(error).__name__)
            return EXIT_FAILED, "driver_unavailable", "cli_start_failed"
        with self._lock:
            self.claude_pid = getattr(self._driver, "pid", None)
            self._read_claude_identity()
        self._ready_since = time.monotonic()
        self._publish()
        stop = self._stop
        while True:
            stop.wait(TICK)
            with self._lock:
                if self._driver_exit is not None:
                    code, unexpected = self._driver_exit
                    if self._close_request is not None or not unexpected:
                        reason = self._close_request[1] if self._close_request else "claude_ended"
                        return EXIT_STOPPED, reason, None
                    if self.launch.get("resume_session_id") and not self._session_confirmed:
                        return EXIT_FAILED, "resume_failed", self.last_error     # never silently a fresh conversation
                    return EXIT_FAILED, "claude_exited", self.last_error
                if self._close_request is not None:
                    mode, reason = self._close_request
                elif stop.is_set():
                    mode, reason = "graceful", "signal"
                elif self._idle_due():
                    mode, reason = "graceful", "idle"
                else:
                    if self.phase_state == "starting" and launch["ready_seconds"] > 0 and \
                            time.monotonic() - self._ready_since >= launch["ready_seconds"] and self._claude_running():
                        self._become_ready()      # the CLI is up and stayed up; it announces its session only after a message
                    continue
                self._mark_closing(mode, reason)  # decided and marked in one critical section: a send cannot slip in between
            self._close_session(mode, reason)
            return EXIT_STOPPED, reason, None

    def _claude_running(self) -> bool:
        """Under the lock: the Claude process this host started is still there (or its pid/identity cannot be checked)."""
        if self._driver_exit is not None:
            return False
        if self.claude_pid is None or self.claude_identity is None:
            return self.claude_pid is not None
        return layout.process_alive(self.claude_pid, self.claude_identity)

    def _read_claude_identity(self):
        if self.claude_pid is not None and self.claude_identity is None:
            try:
                self.claude_identity = layout.process_identity(self.claude_pid)
            except (OSError, ValueError, IndexError):
                pass

    def _idle_due(self) -> bool:
        seconds = self.launch["idle_seconds"]
        return (seconds > 0 and self.phase_state == "ready" and not self.inflight and not self.pending
                and time.monotonic() - self.last_activity_mono >= seconds)

    def _become_ready(self):
        """Under the lock. The session exists: accept messages, and give a fresh session its role as the first one."""
        if self.phase_state != "starting":
            return
        driver = self._driver
        self.session_id = self.session_id or getattr(driver, "session_id", None)
        self.transcript_path = self.transcript_path or getattr(driver, "transcript_path", None)
        self.claude_pid = self.claude_pid or getattr(driver, "pid", None)
        self._read_claude_identity()
        self._touch()
        self.phase_state = "ready"
        self._ever_ready = True
        self.events.append("host_state", {"state": "ready"})
        role = self.launch["send_role"]
        if role and not self.launch.get("resume_session_id"):
            self._send_role(role if isinstance(role, str) else table.role_command(self.launch.get("client") or table.CLAUDE))
        self._cond.notify_all()
        self._publish()

    def _send_role(self, text: str):
        message_uuid = str(uuid.uuid4())
        entry = self.ledger.add(message_uuid, "sent", mode="started", role=True)
        self.inflight[message_uuid] = "queued"
        try:
            self._driver.send(text, message_uuid)
        except Exception as error:
            self.inflight.pop(message_uuid, None)
            self.ledger.entries.pop(message_uuid, None)
            self._record_error("The role message could not be sent (%s)." % type(error).__name__)
            return
        entry["mode"] = "started"
        self.events.append("role_sent", {"message_uuid": message_uuid})

    def _record_error(self, text):
        self.last_error = text[:300]
        if self.events is not None:
            self.events.append("error", {"message": self.last_error, "kind": "host"})

    def _mark_closing(self, mode, reason):
        """Under the lock. From here on every send, answer and interrupt is rejected as closing. Idempotent."""
        self._close_request = self._close_request or (mode, reason)
        if self.phase_state in ("closing", "closed", "failed"):
            return
        self.phase_state = "closing"
        self.events.append("host_state", {"state": "closing", "reason": reason})
        self._cond.notify_all()

    def _close_session(self, mode, reason):
        """Reject new sends, close the driver, and wait for the end of the Claude process, escalating on the exact process."""
        with self._lock:
            self._mark_closing(mode, reason)
        self._publish()
        driver = self._driver
        runner = threading.Thread(target=self._close_driver, args=(driver, mode), name="managed-host-close", daemon=True)
        runner.start()
        deadline = time.monotonic() + self.launch["close_seconds"]
        while time.monotonic() < deadline:
            with self._lock:
                if self._driver_exit is not None:
                    break
            runner.join(0.05)
        self._escalate()

    @staticmethod
    def _close_driver(driver, mode):
        try:
            if mode == "now":
                try:
                    driver.close(timeout=0.0)
                except TypeError:
                    driver.close()
            else:
                driver.close()
        except Exception as error:
            log("driver.close failed: " + type(error).__name__)

    def _escalate(self):
        """The Claude process this host started is still there after the wait: terminate, then kill, its own process group. Only
        that exact pid (identity checked), never a name or a port."""
        pid, identity = self.claude_pid, self.claude_identity
        if pid is None or identity is None or not layout.process_alive(pid, identity):
            return
        for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
            try:
                group = os.getpgid(pid)
                if group == pid and group != os.getpgrp():
                    os.killpg(group, sig)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not layout.process_alive(pid, identity):
                    return
                time.sleep(0.05)

    def _finish(self, code, reason, detail):
        """Record how this host ended, stop serving, remove what must not outlive it. The lock is released by the caller."""
        with self._lock:
            self.phase_state = "closed" if code == EXIT_STOPPED else "failed"
            exit_record = {"code": code, "reason": reason}
            if detail:
                exit_record["detail"] = str(detail)[:300]
            self.record["exit"] = exit_record
            if self.events is not None:
                self.events.append("host_state", {"state": self.phase_state, "reason": reason})
            self._cond.notify_all()
        if self._driver is not None and self._driver_exit is None:
            self._escalate()
        self._publish()
        self._serving = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        for path in (self._socket_path, self.state_dir / TOKEN_FILE, self._managed_host_file):
            if path is None:
                continue
            try:
                if path.name == "managed-host.json":
                    mine = layout.read_json(path) or {}
                    if mine.get("host_pid") != os.getpid():
                        continue
                path.unlink()
            except OSError:
                pass
        if self.record.get("socket_fallback") and self._socket_path is not None:
            try:
                self._socket_path.parent.rmdir()
            except OSError:
                pass
        for item in (self.events, self.ledger):
            if item is not None:
                item.close()

    # -- driver events --------------------------------------------------------------------------------------------------------
    def _on_event(self, event: dict):
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "event")
        with self._lock:
            handler = getattr(self, "_event_" + kind, None)
            if handler is not None:
                handler(event)
            dropped = EVENT_DROP_KEYS.get(kind, ())
            self.events.append(kind, {key: value for key, value in event.items() if key != "type" and key not in dropped})
            self._cond.notify_all()
        if kind == "permission_request":
            self._answer_permission(event)

    def _answer_permission(self, event):
        """The host's own answer to a client's permission request: allow when the agent's prepared settings say bypass, deny
        otherwise (also for an unknown or missing mode). The request is never stored as pending and never reaches the service.
        Called on the driver's event thread without the state lock, because the driver writes the reply itself. A request without
        a usable id cannot be answered and is dropped (the client then times out its own request)."""
        request_id = event.get("request_id")
        driver = self._driver
        if not isinstance(request_id, str) or not request_id or driver is None:
            return
        allow = self.launch.get("permission_mode") == table.BYPASS
        decision = True if allow else {"behavior": "deny", "message": DENY_MESSAGE}
        delivered = False
        try:
            delivered = bool(driver.answer(request_id, decision))
        except Exception as error:  # noqa: BLE001 - a ValueError (bad decision) or a driver that is closing: nothing more can be done
            log("permission answer failed: " + type(error).__name__)
        with self._lock:
            if self.events is not None:
                self.events.append("permission_answered", {
                    "request_id": request_id[:200], "tool": str(event.get("tool") or "")[:200],
                    "decision": "allow" if allow else "deny", "delivered": delivered})

    def _event_session_started(self, event):
        self._session_confirmed = True
        self.session_id = event.get("session_id") or self.session_id
        self.transcript_path = event.get("transcript_path") or self.transcript_path
        self.claude_pid = getattr(self._driver, "pid", None) or self.claude_pid
        self._read_claude_identity()
        if self.phase_state == "starting":
            self._become_ready()
        else:
            self._publish()

    def _event_turn_started(self, event):
        entry = self.ledger.get(event.get("message_uuid"))
        if entry is not None:
            entry["turn_started"] = True
        if self.turn_started_at is None:
            self.turn_started_at = layout.now_iso()
        if event.get("message_uuid") in self.inflight:
            self.inflight[event["message_uuid"]] = "started"
        self._touch()

    def _event_activity(self, event):
        self.activity = str(event.get("text") or "")[:200] or None
        self._touch()                                  # a turn the host did not start (a wake-up) is still activity

    def _event_permission_request(self, event):
        # Never a pending request: the owner is not asked for permissions (``_answer_permission`` replies, outside the lock).
        self._touch()

    def _event_question(self, event):
        self._add_pending(event, "question")

    def _add_pending(self, event, kind):
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or len(self.pending) >= MAX_PENDING:
            return
        item = {key: value for key, value in event.items() if key != "type"}
        item.update(kind=kind, tool=item.get("tool") or ("AskUserQuestion" if kind == "question" else None))
        item["request_id"] = request_id
        item.setdefault("input_truncated", False)
        item.setdefault("decision_reason", None)
        item.setdefault("blocked_path", None)
        self.pending[request_id] = self._bounded_pending(item)
        self._touch()

    @staticmethod
    def _bounded_pending(item: dict) -> dict:
        """One pending request is at most ``MAX_PENDING_ITEM_BYTES`` of JSON whatever the driver sent. Over the limit the
        suggestions go first, then (for a question, which repeats them in ``input``) the input, then any other extra field;
        ``request_id``, ``kind``, ``tool`` and ``input_truncated`` are never dropped, and ``input_truncated`` says when the input
        shown is not the whole input."""
        def size(value):
            return len(json.dumps(value, ensure_ascii=False, default=str))
        if size(item) <= MAX_PENDING_ITEM_BYTES:
            return item
        item = dict(item)
        item.pop("suggestions", None)
        if size(item) > MAX_PENDING_ITEM_BYTES and item.get("kind") == "question" and isinstance(item.get("questions"), list):
            item["input"], item["input_truncated"] = None, True
        keep = ("request_id", "kind", "tool", "tool_use_id", "input", "questions", "input_truncated", "decision_reason", "blocked_path")
        for key in [key for key in item if key not in keep]:
            if size(item) <= MAX_PENDING_ITEM_BYTES:
                break
            item.pop(key)
        if size(item) > MAX_PENDING_ITEM_BYTES:
            item["input"], item["input_truncated"] = None, True
            item.pop("questions", None)
            for key in ("decision_reason", "blocked_path", "tool_use_id", "tool"):
                if isinstance(item.get(key), str):
                    item[key] = item[key][:200]
        return item

    def _event_request_cancelled(self, event):
        self.pending.pop(event.get("request_id"), None)

    def _event_turn_finished(self, event):
        status = event.get("status")
        blocked = [item for item in event.get("blocked_message_uuids") or () if isinstance(item, str)]
        finished = [item for item in event.get("message_uuids") or () if isinstance(item, str)]
        everything = list(dict.fromkeys([*finished, *blocked]))      # a hook-blocked uuid is finished whichever list named it
        outcomes = {}
        for message_uuid in everything:
            outcomes[message_uuid] = "blocked" if message_uuid in blocked else status
        for message_uuid in event.get("dropped_message_uuids") or ():
            if isinstance(message_uuid, str) and message_uuid not in outcomes:
                outcomes[message_uuid] = "dropped"
        for message_uuid, outcome in outcomes.items():
            self.inflight.pop(message_uuid, None)
            entry = self.ledger.get(message_uuid)
            if entry is not None:
                entry["finished"], entry["outcome"] = True, outcome
        if getattr(self._driver, "busy", None) is False:
            self.inflight.clear()                      # the driver is the authority on whether anything is still running
        if not self.inflight:
            self.activity = self.turn_started_at = None
            self.pending.clear()
        self.last_turn = {"status": status, "message_uuids": everything,
                          "outcomes": dict(list(outcomes.items())[:MAX_OUTCOMES]), "finished_at": layout.now_iso()}
        for key in ("error", "reason", "blocked_message_uuids", "stop_reason"):
            if event.get(key):
                self.last_turn[key] = event[key]
        self._touch()

    def _event_error(self, event):
        self.last_error = str(event.get("message") or "")[:300] or self.last_error

    def _event_exited(self, event):
        self._driver_exit = (event.get("code"), bool(event.get("unexpected")))
        self.inflight.clear()
        self.pending.clear()
        self.activity = self.turn_started_at = None

    # -- operations -----------------------------------------------------------------------------------------------------------
    def _op_hello(self, request_id, _arguments):
        with self._lock:
            return protocol.ok(request_id, **self._identity())

    def _op_status(self, request_id, _arguments):
        with self._lock:
            return protocol.ok(request_id, **self._status())

    def _unavailable(self, request_id):
        """The rejection that says why the session cannot take input, or ``None`` when it can. Under the lock."""
        if self.phase_state == "starting":
            return protocol.rejected(request_id, protocol.REJECT_SESSION_STARTING)
        if self.phase_state == "closing" or self._close_request is not None:
            return protocol.rejected(request_id, protocol.REJECT_SESSION_CLOSING)
        if self.phase_state != "ready" or self._driver_exit is not None:
            return protocol.rejected(request_id, protocol.REJECT_SESSION_CLOSED)
        return None

    def _op_send(self, request_id, arguments):
        text, message_uuid = arguments["text"], arguments["message_uuid"]
        steer, confirm_seconds = arguments["steer"], arguments["confirm_seconds"]
        if not self._command.acquire(blocking=False):
            return protocol.rejected(request_id, protocol.REJECT_HOST_BUSY)
        try:
            with self._lock:
                entry = self.ledger.get(message_uuid)
                if entry is not None and entry["state"] == "fenced":
                    return protocol.rejected(request_id, protocol.REJECT_FENCED)
                if entry is not None:             # the same uuid again: the first send stands, nothing reaches the driver twice
                    return protocol.ok(request_id, message_uuid=message_uuid, mode=entry.get("mode"), duplicate=True,
                                       turn_started=entry["turn_started"] or entry["finished"])
                refusal = self._unavailable(request_id)
                if refusal is not None:
                    return refusal
                if (self.inflight or self.pending) and not steer:
                    return protocol.rejected(request_id, protocol.REJECT_TURN_RUNNING)
                mode = "queued" if (self.inflight or self.pending) else "started"
                entry = self.ledger.add(message_uuid, "sent", mode=mode)
                self._session_confirmed = True
                self.inflight[message_uuid] = "queued"
                try:
                    self._driver.send(text, message_uuid)
                except ValueError:
                    self.inflight.pop(message_uuid, None)
                    self.ledger.entries.pop(message_uuid, None)
                    return protocol.error(request_id, protocol.ERROR_BAD_REQUEST, "the driver refused the message")
                except Exception:
                    self.inflight.pop(message_uuid, None)
                    self.ledger.entries.pop(message_uuid, None)
                    return protocol.rejected(request_id, protocol.REJECT_SESSION_CLOSED)
                self._touch()
        finally:
            self._command.release()
        turn_started = False
        if mode == "started" and confirm_seconds > 0:
            deadline = time.monotonic() + confirm_seconds
            with self._lock:
                while not (entry["turn_started"] or entry["finished"]) and self._driver_exit is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
                turn_started = entry["turn_started"] or entry["finished"]
        elif mode == "started":
            turn_started = entry["turn_started"] or entry["finished"]
        return protocol.ok(request_id, message_uuid=message_uuid, mode=mode, duplicate=False, turn_started=turn_started)

    def _op_lookup(self, request_id, arguments):
        message_uuid, fence = arguments["message_uuid"], arguments["fence"]
        with self._lock:
            entry = self.ledger.get(message_uuid)
            if entry is None and fence:
                entry = self.ledger.add(message_uuid, "fenced")
            if entry is None:
                return protocol.ok(request_id, message_uuid=message_uuid, seen=False, fenced=False)
            if entry["state"] == "fenced":
                return protocol.ok(request_id, message_uuid=message_uuid, seen=False, fenced=True)
            return protocol.ok(request_id, message_uuid=message_uuid, seen=True, fenced=False, mode=entry.get("mode"),
                               turn_started=entry["turn_started"], finished=entry["finished"], outcome=entry.get("outcome"))

    def _op_interrupt(self, request_id, _arguments):
        with self._lock:
            refusal = self._unavailable(request_id)
            if refusal is not None:
                return refusal
            sent = bool(self._driver.interrupt())
            if sent:
                self._touch()
            return protocol.ok(request_id, sent=sent)

    def _op_answer(self, request_id, arguments):
        with self._lock:
            refusal = self._unavailable(request_id)
            if refusal is not None:
                return refusal
            target = arguments["request_id"]
            try:
                delivered = bool(self._driver.answer(target, arguments["decision"]))
            except ValueError:                      # not a valid decision for that request: it stays pending, nothing was sent
                return protocol.error(request_id, protocol.ERROR_BAD_REQUEST, "that decision is not valid for this request")
            self.pending.pop(target, None)          # delivered, or no longer pending in the driver either way
            if delivered:
                self._touch()
            return protocol.ok(request_id, delivered=delivered)

    def _op_command(self, request_id, arguments):
        """A command the client takes only through its own interface (Codex ``/compact`` and ``/model`` are plain text as messages)."""
        name = arguments["name"]
        with self._lock:
            refusal = self._unavailable(request_id)
            if refusal is not None:
                return refusal
            method = getattr(self._driver, "compact" if name == "compact" else "update_settings", None)
            if method is None:
                return protocol.rejected(request_id, protocol.REJECT_UNSUPPORTED_COMMAND)
            try:
                accepted = bool(method() if name == "compact" else method(model=arguments["model"], effort=arguments["effort"]))
            except ValueError:
                return protocol.error(request_id, protocol.ERROR_BAD_REQUEST, "the client refused that value")
            except Exception:
                return protocol.rejected(request_id, protocol.REJECT_SESSION_CLOSED)
            if accepted:
                self._touch()
            return protocol.ok(request_id, name=name, accepted=accepted)

    def _op_close(self, request_id, arguments):
        mode = arguments["mode"]
        with self._lock:
            if self._close_request is None and self.phase_state not in ("closing", "closed", "failed"):
                self._close_request = (mode, "requested")
            elif mode == "now" and self._close_request is not None and self._close_request[0] == "graceful":
                self._close_request = ("now", self._close_request[1])
            self._cond.notify_all()
            state = "closing" if self._close_request is not None or self.phase_state != "ready" else self.phase_state
        return protocol.ok(request_id, state=state)

    def _op_events(self, request_id, arguments):
        with self._lock:
            return protocol.ok(request_id, instance=self.launch["instance"],
                               **self.events.since(arguments["since"], arguments["limit"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    arguments = parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    return ManagedHost(arguments.state).run(stop)


if __name__ == "__main__":
    raise SystemExit(main())
