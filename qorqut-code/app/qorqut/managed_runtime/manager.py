"""The service's view of managed agents: which are switched on, which have a verified host, and the few operations on them.

A managed agent runs its Claude Code session under one small detached host process (``host.py``); this module never drives
``claude`` itself. ``ManagedRuntime`` is what the rest of the service holds:

``verify(profile, marker)``             what the session probe needs to bind a managed ``claude``: ``{"transport", "owner"}`` or None
``identity(agent_id)``                  the verified session's identity (conversation, claude pid and ticks, owner) or None
``snapshot(agent_id)``                  the last known state for Chat's ``managed`` summary, without waiting on the host
``refresh(agent_id)``                   read status and new events from the host; True when what Chat shows changed
``drain_events(agent_id)``              the host events read since the last drain, oldest first
``ensure_open(profile, timeout)``       prepare the workspace and make sure a host is ready (spawns one when none is running)
``interrupt`` / ``answer`` / ``close``  control operations on the verified host
``close_all(reason)``                   ask every verified host to close
``running_hosts()``                     every verified host, for recovery quiescence
``start()`` / ``stop()``                ``start`` spawns nothing; ``stop`` closes nothing and kills nothing: hosts outlive the service

Also exposed because the input adapter needs the connection: ``handle(agent_id)`` (a verified ``Host`` with a ``client``).

The launcher (``launcher.py``: inspect, verify and start hosts) and the ``HostClient`` (``client.py``) are written elsewhere and used
through their module functions, so this module and its tests run against fakes. ``launcher`` is any object with:

``inspect_host(state_dir)``              ``Inspection`` (``status`` running|starting|unverified|absent, ``host`` a ``VerifiedHost`` with
                                         ``agent_id, instance, host_pid, host_identity, claude_pid, claude_identity, session_id,
                                         transcript_path, state`` and ``client()``)
``start_host(state_dir, spec, timeout=)``  write ``launch.json`` (private; it carries the environment, so secrets never reach argv
                                         or the process table), start the detached host and wait until it is ready; ``LaunchError``
``running_hosts(root)``                  ``{agent_id: VerifiedHost}`` for every verified host under ``<runtime>/managed-agents``

Nothing here is killed by name or port, and no secret is logged or kept after the launcher took it.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

from qorqut import account_state, agent_cli_launch
from qorqut_operations.contracts import DomainError
from qorqut_system import locks

from . import account_errors
from . import clients as table
from . import mode as runtime_mode
from . import models
from . import options as launch_options
from . import state as layout
from .client import HostUnavailable

# The most time one owner message spends opening a session, start to bound session (``ensure_open`` and the caller's wait for the
# binding share it): a request thread is never held longer.
OPEN_TIMEOUT = 45.0
POLL_INTERVAL = 0.25
# Sessions being started at once across all agents. A start prepares a workspace and runs a process; more than this is "busy".
MAX_CONCURRENT_OPENS = 4
# A message sent straight to a host before its session is bound (see ``send_first``) is remembered this long, so that the input
# that is admitted for it afterwards is recognised by the host and is not typed twice.
STAGED_SECONDS = 600
MAX_STAGED = 16
# What a permission card shows of a tool input: the whole command or input, up to this many characters.
INPUT_TEXT_LIMIT = 20000
IDLE_SECONDS = 900
ANCESTOR_HOPS = 24
ACTIVITY_INTERVAL = 1.0
MAX_BUFFERED_EVENTS = 1000
# States of a host record that mean a claude process may still be running.
LIVE_STATES = ("starting", "ready", "closing")
WORKING = ("working", "blocked")


CLIENT_HOME_VARIABLES = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")


class ManagedError(DomainError):
    """A managed operation that did not happen. ``code`` is stable: ``managed_unavailable``, ``session_closed``,
    ``host_unverified``, ``not_managed`` (this installation runs its agents in terminals), ``runtime_switching`` (retryable: the owner
    is switching how agents run), ``cli_unavailable``, ``resume_unavailable`` (the selected conversation's transcript is gone: the owner
    must choose a new conversation), ``resume_failed``, ``host_failed``, ``host_start_failed``, ``open_timeout`` and ``open_busy``
    (retryable), ``session_opening`` (retryable), ``host_busy`` (retryable), ``host_rejected``, ``driver_unavailable`` (the
    agent's client has no managed driver yet)."""


class HostUnverified(Exception):
    """A host claims to run for this agent but could not be verified: nothing may be sent to it."""


class Host:
    """A running host: its saved record and a ``HostClient`` for exactly that host. A host that is still starting has no
    client yet (``state == "starting"``)."""

    def __init__(self, agent_id, record, client):
        self.agent_id, self.record, self.client = agent_id, dict(record or {}), client

    @property
    def state(self):
        return self.record.get("state")

    @property
    def identity(self):
        return _identity(self.agent_id, self.record)


def is_host_error(error) -> bool:
    """The host did not answer as expected: ``client.HostUnavailable``, or an OS error from a socket."""
    return isinstance(error, (HostUnavailable, OSError))


def is_timeout(error) -> bool:
    """Whether a host failure was a timeout (the request may have been written) rather than a lost connection."""
    return isinstance(error, TimeoutError) or getattr(error, "reason", None) == "timeout"


# ----- shapes ---------------------------------------------------------------------------------------------------------

def _ticks(identity):
    """The start ticks of a ``boot_id:ticks`` process identity."""
    return identity.rsplit(":", 1)[1] if isinstance(identity, str) and ":" in identity else None


def _identity(agent_id, record):
    host_pid, host_started = record.get("host_pid"), _ticks(record.get("host_identity"))
    client = record.get("client")
    return {"agent_id": agent_id, "provider": client if table.known(client) else "claude", "conversation_id": record.get("session_id"),
            "pid": record.get("claude_pid"), "started": _ticks(record.get("claude_identity")),
            "owner": f"host:{host_pid}:{host_started}" if host_pid and host_started else None,
            "host_pid": host_pid, "host_started": host_started, "instance": record.get("instance"),
            "state": record.get("state"), "transcript_path": record.get("transcript_path")}


identity_of = _identity


def _summary(value, limit=240):
    """One short line about a tool input for a permission card's heading: a file, command or address when there is one."""
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "url", "pattern", "description"):
            if isinstance(value.get(key), str) and value[key].strip():
                value = value[key]
                break
        else:
            value = json.dumps(value, sort_keys=True, default=str)
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _input_text(value):
    """The tool input as the text a permission card shows, and whether it had to be cut here: a command exactly as it is,
    anything else as indented JSON. Bounded; the host bounds what it sends first and says so in ``input_truncated``."""
    if isinstance(value, dict) and isinstance(value.get("command"), str) and value["command"].strip():
        text = value["command"]
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return (text[:INPUT_TEXT_LIMIT], True) if len(text) > INPUT_TEXT_LIMIT else (text, False)


def _short(value, limit):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _pending(items):
    """What Chat shows of the host's pending requests. A permission request carries the whole (bounded) input as ``input`` and
    ``input_text``, ``input_truncated`` when any of it was cut (the owner then cannot see all of what they would allow),
    ``decision_reason`` and ``blocked_path`` when Claude gave them. A question carries its ``questions`` as asked."""
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("request_id"):
            continue
        given = item.get("input")
        text, clipped = _input_text(given) if given is not None else ("", False)
        small = given if given is not None and len(json.dumps(given, default=str)) <= INPUT_TEXT_LIMIT else None
        result.append({"request_id": str(item["request_id"]), "kind": item.get("kind") or "permission",
                       "tool": item.get("tool"), "input_summary": _summary(given) if given is not None else "",
                       "input": small, "input_text": text, "input_truncated": bool(item.get("input_truncated")) or clipped,
                       "decision_reason": _short(item.get("decision_reason"), 600), "blocked_path": _short(item.get("blocked_path"), 1000),
                       "questions": item.get("questions") if isinstance(item.get("questions"), list) else []})
    return result


CLOSED_VIEW = {"open": False, "state": "closed", "phase": None, "activity": None, "turn_started_at": None, "can_stop": False,
               "pending": [], "last_turn_status": None, "turn_error": None, "turn_error_kind": None, "turn_error_resets_at": None,
               "turn_error_resets_text": None, "turn_finished_at": None, "provider": None, "session_id": None, "closed_reason": None,
               "closed_text": None}

# Why a host that is gone stopped, in plain words. A host that ended because it was asked to, or sat idle, has no entry: nothing
# went wrong. Any other reason still says that the session ended unexpectedly.
NORMAL_ENDS = frozenset({"requested", "idle", "signal", "claude_ended"})
END_TEXT = {
    "claude_exited": "{client} stopped unexpectedly. Send a message to start the session again.",
    "host_gone": "The session stopped unexpectedly. Send a message to start it again.",
    "host_error": "The session stopped because of an internal error. Send a message to start it again.",
    "resume_failed": "The earlier conversation could not be continued. Send a message to start it again, or start a new conversation.",
    "driver_unavailable": "{client} could not be started. Check that it is installed and signed in under Settings > Model/Provider, then send a message.",
    "invalid_launch": "The session could not be started. Send a message to try again.",
}


def _turn_error(status):
    """The client's own words about the last turn when it ended in an error (an account that is refused, a usage limit with the time
    to try again, a failed sign-in), bounded; None when the last turn did not fail. The host keeps it as ``last_turn.error``."""
    last = status.get("last_turn") if isinstance(status.get("last_turn"), dict) else {}
    text = last.get("error")
    if last.get("status") != "error" or not isinstance(text, str):
        return None
    text = " ".join(text.split())[:300]
    return text or None


def _turn_label(status, provider):
    """``account_errors.label_of`` for the last turn the host reported: ``{"kind", "resets_at", "resets_text"}`` when it failed on a
    usage limit or a sign-in problem, else None. The host's own label is used; a host that started before labels existed is read by
    the client's known sentence."""
    last = status.get("last_turn") if isinstance(status.get("last_turn"), dict) else {}
    return account_errors.label_of(last, provider)


def _view(record, status):
    status = status if isinstance(status, dict) else {}
    record = {**record, **{key: value for key, value in status.items() if key in ("state", "session_id")}}
    phase = status.get("phase") or ("starting" if record.get("state") == "starting" else "idle")
    provider = record.get("client") if table.known(record.get("client")) else table.CLAUDE
    label = _turn_label(status, provider) or {}
    last = status.get("last_turn") if isinstance(status.get("last_turn"), dict) else {}
    return {"open": True, "state": record.get("state"), "phase": phase, "activity": status.get("activity"),
            "turn_started_at": status.get("turn_started_at"), "can_stop": phase in WORKING,
            "pending": _pending(status.get("pending")), "last_turn_status": status.get("last_turn_status"),
            "turn_error": _turn_error(status), "turn_error_kind": label.get("kind"), "turn_error_resets_at": label.get("resets_at"),
            "turn_error_resets_text": label.get("resets_text"),
            "turn_finished_at": last.get("finished_at") if isinstance(last.get("finished_at"), str) else None, "provider": provider,
            "session_id": record.get("session_id"), "closed_reason": None, "closed_text": None}


def _shown(view):
    """What Chat shows: a change here is worth telling the browser about."""
    return (view["open"], view["phase"], tuple(item["request_id"] for item in view["pending"]), view["activity"],
            view.get("closed_reason"), view.get("turn_error"), view.get("turn_error_kind"))


def _events_of(reply):
    items = reply.get("events") if isinstance(reply, dict) else reply
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


# ----- the runtime ----------------------------------------------------------------------------------------------------

class ManagedRuntime:
    """See the module text. ``runtime`` supplies ``paths`` (the runtime folder), ``database`` and ``managed_launch`` (workspace
    preparation); ``launcher`` is the host launcher (default: the ``launcher`` module of this package)."""

    def __init__(self, runtime, *, launcher=None, prepare=None, resume=None, executable=None, clock=time.monotonic, sleep=time.sleep,
                 idle_seconds=IDLE_SECONDS, max_opens=MAX_CONCURRENT_OPENS, ready=None, client_state=None):
        self._runtime = runtime
        self.paths = getattr(runtime, "paths", None)
        self._launcher, self._prepare_override, self._resume_override = launcher, prepare, resume
        self._executable = executable
        # ``ready(client)``: that CLI is installed and signed in. Asked only for an agent with no stored client (options.py).
        self._ready = ready
        # ``client_state()``: what each client's state is (``native_chat.client_state.states``), read once per open. Without it (and
        # without an injected ``ready``) the real status is read; with only ``ready`` injected nothing is checked before the open.
        self._client_state = client_state
        self._clock, self._sleep, self._idle = clock, sleep, idle_seconds
        self._guard = threading.RLock()
        self._views: dict[str, dict] = {}
        self._viewed: dict[str, float] = {}
        self._cursor: dict[str, int] = {}
        self._hosts: dict[str, tuple] = {}
        self._events: dict[str, deque] = {}
        self._activity_at: dict[str, float] = {}
        self._flights: dict[str, threading.Lock] = {}
        self._slots = threading.BoundedSemaphore(max_opens)
        self._staged: dict[str, dict] = {}
        self._started = False

    # ----- lifecycle: nothing is started, nothing is stopped ----------------------------------------------------------

    def start(self):
        """Begin serving. This spawns nothing: a host exists only because an owner message opened one, and a running one is
        found again by ``handle``/``verify`` when asked."""
        self._started = True

    def stop(self):
        """Forget what was cached. Hosts are detached on purpose and keep running; nothing is closed or killed here."""
        with self._guard:
            self._started = False
            self._views.clear()
            self._viewed.clear()
            self._cursor.clear()
            self._hosts.clear()
            self._events.clear()
            self._activity_at.clear()
            self._staged.clear()

    # ----- finding hosts --------------------------------------------------------------------------------------------

    def _launcher_for(self):
        if self._launcher is None:
            try:
                from . import launcher as module
            except Exception as error:  # noqa: BLE001 - not part of this build
                raise ManagedError("managed_unavailable", "The managed session launcher is not available") from error
            self._launcher = module
        return self._launcher

    def _state_dir(self, agent_id) -> Path:
        return layout.agent_state(self.paths, agent_id)

    @staticmethod
    def _record(found) -> dict:
        return {"host_pid": found.host_pid, "host_identity": found.host_identity, "state": found.state,
                "session_id": found.session_id, "transcript_path": found.transcript_path, "claude_pid": found.claude_pid,
                "claude_identity": found.claude_identity, "instance": found.instance, "agent_id": found.agent_id,
                "client": getattr(found, "agent_client", None)}

    def _find(self, agent_id):
        """The agent's ``Host`` (running, or still starting), or None when none runs. Raises ``HostUnverified`` when a process
        holds the host lock but cannot be proven to be this agent's host (it is then left alone)."""
        state_dir = self._state_dir(agent_id)
        inspected = self._launcher_for().inspect_host(state_dir)
        if inspected.status == "running":
            return Host(agent_id, self._record(inspected.host), inspected.host.client())
        if inspected.status == "starting":
            from . import state as layout
            return Host(agent_id, {**(layout.read_host(state_dir) or {}), "state": "starting"}, None)
        if inspected.status == "unverified":
            raise HostUnverified(agent_id)
        return None

    def handle(self, agent_id):
        """The verified host of this agent (a ``Host`` with ``record`` and ``client``), or None when none is running. Raises
        ``HostUnverified`` when one claims to run but cannot be verified, so a caller never sends to it."""
        try:
            return self._find(agent_id)
        except (HostUnverified, ManagedError):
            raise
        except Exception as error:  # noqa: BLE001 - whatever could not be proven is not a verified host
            raise HostUnverified(agent_id) from error

    def identity(self, agent_id):
        try:
            host = self.handle(agent_id)
        except (HostUnverified, ManagedError):
            return None
        return None if host is None else host.identity

    def verify(self, profile, marker):
        """For the session probe: ``{"transport": "managed", "owner": "host:<pid>:<ticks>"}`` when a verified host runs for this
        agent, the hook marker is the host's session, and the marker's process descends from the host. Else None."""
        agent_id = profile.get("agent_id") if isinstance(profile, dict) else None
        if not agent_id:
            return None
        try:
            host = self.handle(agent_id)
            if host is None or host.state not in LIVE_STATES:
                return None
            identity = host.identity
            if not identity["owner"]:
                return None
            session = identity["conversation_id"]
            if session and isinstance(marker, dict) and marker.get("session_id") != session:
                return None
            if not _descends(int(marker["pid"]), int(identity["host_pid"])):
                return None
        except (HostUnverified, ManagedError, KeyError, TypeError, ValueError, OSError):
            return None
        return {"transport": "managed", "owner": identity["owner"]}

    # ----- what Chat shows --------------------------------------------------------------------------------------------

    def snapshot(self, agent_id) -> dict:
        """The last known state, as a new dict. Reads the host once when nothing is known or the last reading is old; Chat's
        state reads never wait on a busy host more than that."""
        with self._guard:
            view = self._views.get(agent_id)
            age = self._clock() - self._viewed.get(agent_id, -1e9)
        if view is None or age > 5.0:
            self.refresh(agent_id)
            with self._guard:
                view = self._views.get(agent_id)
        return dict(view or CLOSED_VIEW)

    def refresh(self, agent_id) -> bool:
        """Read this agent's host (state and new events). True when ``(open, phase, pending request ids, activity)`` changed;
        activity-only changes count at most once a second. A host that cannot be read this time leaves the last state."""
        proven_absent = True
        try:
            # An agent that never had a state folder has no host: it costs one ``stat``.
            host = self._find(agent_id) if self._has_state(agent_id) else None
        except Exception:  # noqa: BLE001 - not verifiable now: it is not shown as open, and nothing is claimed about why
            host, proven_absent = None, False
        now = self._clock()
        if host is None:
            return self._store(agent_id, {**CLOSED_VIEW, **(self._ended(agent_id) if proven_absent else {})}, now, reset=True)
        if host.client is None:
            return self._store(agent_id, _view(host.record, {}), now)
        try:
            status = host.client.status()
            events = self._read_events(agent_id, host)
        except Exception as error:  # noqa: BLE001
            if not is_host_error(error):
                raise
            return False
        return self._store(agent_id, _view(host.record, status), now, events=events, instance=host.identity["owner"])

    def _ended(self, agent_id) -> dict:
        """Why the host of this agent stopped, when it was not because the owner (or idleness) closed it: ``closed_reason`` (a
        short code) and ``closed_text`` (a plain sentence). Read from the last host record, which the next start replaces.
        Empty when the host ended normally or has no record."""
        try:
            record = layout.read_host(self._state_dir(agent_id))
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(record, dict):
            return {}
        state = record.get("state")
        ended = record.get("exit") if isinstance(record.get("exit"), dict) else {}
        reason = ended.get("reason") if isinstance(ended.get("reason"), str) else None
        if state in layout.LIVE_STATES:
            reason = "host_gone"  # no host holds the lock, yet the record never said it finished: it died
        elif state not in ("closed", "failed"):
            return {}
        elif reason is None:
            reason = "host_gone" if state == "failed" else None
        if reason is None or reason in NORMAL_ENDS:
            return {}
        return {"closed_reason": reason[:60],
                "closed_text": END_TEXT.get(reason, "The session ended unexpectedly. Send a message to start it again.").format(
                    client=table.NAMES.get(record.get("client"), table.NAMES[table.CLAUDE]))}

    def _has_state(self, agent_id) -> bool:
        try:
            return layout.agent_state(self.paths, agent_id).is_dir()
        except (ValueError, AttributeError, TypeError, OSError):
            return False

    def _read_events(self, agent_id, host):
        with self._guard:
            known = self._hosts.get(agent_id)
            instance = host.identity["owner"]
            if known is not None and known != instance:
                self._cursor[agent_id] = 0
            self._hosts[agent_id] = instance
            since = self._cursor.get(agent_id, 0)
        events, last = [], since
        for _ in range(5):
            reply = host.client.events(last)
            page = _events_of(reply)
            events += page
            for event in page:
                if isinstance(event.get("seq"), int):
                    last = max(last, event["seq"])
            newest = reply.get("next") if isinstance(reply, dict) else None
            if isinstance(newest, int) and not isinstance(newest, bool):
                last = max(last, newest)
            if not page or not (isinstance(reply, dict) and isinstance(reply.get("seq"), int) and last < reply["seq"]):
                break
        with self._guard:
            self._cursor[agent_id] = last
        return [event for event in events if not isinstance(event.get("seq"), int) or event["seq"] > since]

    def _store(self, agent_id, view, now, *, events=(), instance=None, reset=False):
        with self._guard:
            old = self._views.get(agent_id)
            if reset:
                self._cursor.pop(agent_id, None)
                self._hosts.pop(agent_id, None)
            if events:
                buffer = self._events.setdefault(agent_id, deque(maxlen=MAX_BUFFERED_EVENTS))
                buffer.extend(events)
            if old is not None and _shown(view)[:3] == _shown(old)[:3] and view["activity"] != old["activity"]:
                # Only the activity line moved: show it at most once a second, otherwise keep what is shown.
                if now - self._activity_at.get(agent_id, -1e9) < ACTIVITY_INTERVAL:
                    view = {**view, "activity": old["activity"]}
            if old is None or view["activity"] != old["activity"]:
                self._activity_at[agent_id] = now
            self._views[agent_id] = view
            self._viewed[agent_id] = now
            self._feed_account_state(view)
            if old is None:
                return view["open"]
            return _shown(old) != _shown(view)

    @staticmethod
    def _feed_account_state(view):
        """Tell ``account_state`` how this agent's last turn ended, so one agent's usage limit marks the whole product."""
        if view.get("provider") and view.get("last_turn_status") in ("completed", "error"):
            account_state.observe_turn(view["provider"], view.get("turn_error_kind"), view.get("turn_error_resets_at"),
                                       view.get("turn_error_resets_text"), view.get("turn_finished_at"), view.get("last_turn_status"))

    def drain_events(self, agent_id) -> list:
        """The host events read by ``refresh`` since the last drain, oldest first (each drained once)."""
        with self._guard:
            buffer = self._events.get(agent_id)
            if not buffer:
                return []
            drained = list(buffer)
            buffer.clear()
            return drained

    # ----- opening a session ------------------------------------------------------------------------------------------

    def _require_managed_mode(self):
        """Opening is gated on the installation's mode, asked now (the owner can switch while the service runs): refused while a
        switch runs (retryable) and when agents run in terminals. Closing, stopping and reading status are never gated."""
        if runtime_mode.switching(self.paths):
            raise ManagedError("runtime_switching", "Agents are being switched. Try again in a minute.", retryable=True)
        if runtime_mode.is_terminal(self.paths):
            raise ManagedError("not_managed", "This agent runs in its terminal")

    def ensure_open(self, profile, timeout=OPEN_TIMEOUT, *, new_conversation=False) -> dict:
        """Make sure this managed agent has a ready host and return ``{"agent_id", "state", "session_id", "transcript_path",
        "host_pid", "started", "role_pending"}`` (``started``: this call started it; ``role_pending``: it is a fresh session that
        the host will send ``/role`` to). Raises ``ManagedError``. ``timeout`` is the whole budget of this call, waiting for
        another caller's open included.

        One open at a time per agent: a second concurrent caller waits for the first one's open and uses its host; it never
        prepares the workspace or starts anything itself. Across agents at most ``MAX_CONCURRENT_OPENS`` starts run at once; the
        others get the retryable ``open_busy``. An existing verified host that is ready is used as it is, one still starting is
        waited for (a host that comes up after this call gave up is the one the next call finds). Otherwise, with the lifecycle
        lock held and the absence of a host checked again under it, the workspace is prepared the way a terminal session would be
        and the launch (executable, arguments, environment) is handed to the launcher, which writes it to ``launch.json`` with
        owner-only permissions: the environment is never an argument.

        A fresh session is sent ``/role`` by the host itself; a resumed one is not. If the selected conversation cannot be
        resumed because its transcript is gone, ``resume_unavailable`` is raised and nothing starts: it is never silently replaced
        by a fresh conversation. ``new_conversation=True`` is the owner's explicit choice to start a fresh one instead."""
        self._require_managed_mode()
        agent_id = profile["agent_id"]
        deadline = self._clock() + timeout
        with self._one_open(agent_id, deadline):
            return self._open(profile, deadline, new_conversation)

    def _flight(self, agent_id):
        with self._guard:
            return self._flights.setdefault(agent_id, threading.Lock())

    @contextmanager
    def _one_open(self, agent_id, deadline, *, error="open_timeout", message="The session did not become ready in time"):
        """The in-process side of one open at a time per agent: wait (until ``deadline``) for the caller that is opening."""
        lock = self._flight(agent_id)
        while not lock.acquire(blocking=False):
            if self._clock() >= deadline:
                raise ManagedError(error, message, retryable=True)
            self._sleep(POLL_INTERVAL)
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def hold(self, agent_id, timeout=5.0):
        """Keep every open of this agent out while the caller changes its client (launch options). Waits up to ``timeout``;
        ``session_opening`` (retryable) when a session is being opened and does not finish in time."""
        deadline = self._clock() + timeout
        with self._one_open(agent_id, deadline, error="session_opening", message="A session is being opened for this agent; try again in a moment"):
            while True:
                with self._lifecycle(agent_id) as held:
                    if held:
                        yield
                        return
                if self._clock() >= deadline:
                    raise ManagedError("session_opening", "A session is being opened for this agent; try again in a moment", retryable=True)
                self._sleep(POLL_INTERVAL)

    def _open(self, profile, deadline, new_conversation):
        agent_id = profile["agent_id"]
        started, resume = False, None
        while True:
            try:
                host = self.handle(agent_id)
            except HostUnverified:
                raise ManagedError("host_unverified", "A session of this agent could not be verified", retryable=True) from None
            if host is not None and host.state == "ready" and host.record.get("session_id"):
                return self._opened(agent_id, host.record, started, resume)
            if host is None:
                with self._lifecycle(agent_id) as held:
                    if held:
                        # Another process (or an earlier caller whose lock we raced) may have started it: look again under the lock,
                        # and only a host that is still absent is prepared for and started.
                        try:
                            present = self.handle(agent_id)
                        except HostUnverified:
                            raise ManagedError("host_unverified", "A session of this agent could not be verified", retryable=True) from None
                        if present is None:
                            record, resume, client = self._launch(profile, deadline, new_conversation)
                            opened = self._opened(agent_id, record, True, resume)
                            launch_options.remember(self.paths, agent_id, client)   # sticky: the client that worked stays the agent's
                            return opened
            # Another caller holds the lifecycle lock, or the host is still starting or closing: look again shortly.
            if self._clock() >= deadline:
                raise ManagedError("open_timeout", "The session did not become ready in time", retryable=True)
            self._sleep(POLL_INTERVAL)

    def _client_states(self):
        """The state of each client for the pre-open check, or None when none is to be made (``ready`` injected alone, tests)."""
        if self._client_state is not None:
            return self._client_state()
        if self._ready is not None:
            return None
        from qorqut.native_chat import client_state
        return client_state.states()

    def client_for(self, profile, states=None) -> str:
        """The effective client of this agent: its stored one, else the best ready one (``options.effective_client``)."""
        if states is not None:
            ready = lambda client: (states.get(client) or {}).get("state") == "ready"  # noqa: E731
        else:
            ready = self._ready if self._ready is not None else launch_options.ready_function()
        return launch_options.effective_client(self.paths, profile["agent_id"], profile, ready)

    @staticmethod
    def _label(profile) -> str:
        """The name the owner knows this agent by, for the sentences about it."""
        try:
            from qorqut.native_chat import store
            return store.profile_label(profile)
        except Exception:  # noqa: BLE001 - a profile without a readable name is "This agent"
            return str(profile.get("name") or "") if isinstance(profile, dict) else ""

    def _launch(self, profile, deadline, new_conversation):
        """Under the lifecycle lock, with no host present: check that this agent may be started, then start it.
        ``(record, resume, client)``."""
        self._require_managed_mode()
        agent_id = profile["agent_id"]
        states = self._client_states()
        client = self.client_for(profile, states)
        if not table.available(client):
            raise ManagedError("driver_unavailable", "Running this agent with " + client.capitalize() + " is not available yet")
        # This agent's client must be installed and signed in before anything starts: one plain sentence, never a late failure.
        if states is not None:
            from qorqut.native_chat import client_state
            refused = client_state.refusal(self._label(profile), client, states)
            if refused is not None:
                raise ManagedError(refused[0], refused[1])
        if not self._slots.acquire(blocking=False):
            raise ManagedError("open_busy", "Several sessions are starting right now. Try again in a moment", retryable=True)
        try:
            resume = self._resume_id(agent_id, new_conversation, client)
            return self._start(profile, resume, max(1.0, deadline - self._clock()), client), resume, client
        finally:
            self._slots.release()

    def _opened(self, agent_id, record, started, resume):
        if resume and record.get("session_id") != resume:
            host = self._find_or_none(agent_id)
            if host is not None and host.client is not None:
                self._close_quietly(host, "now")
            raise ManagedError("resume_failed", "The selected conversation could not be resumed, so no session was opened")
        return {"agent_id": agent_id, "state": "ready", "session_id": record.get("session_id"),
                "transcript_path": record.get("transcript_path"), "host_pid": record.get("host_pid"), "started": started,
                "role_pending": bool(started and not resume)}

    def _find_or_none(self, agent_id):
        try:
            return self._find(agent_id)
        except Exception:  # noqa: BLE001
            return None

    @contextmanager
    def _lifecycle(self, agent_id):
        """One spawn at a time per agent, across processes: a non-blocking exclusive lock; yields whether this caller holds it."""
        folder = self._state_dir(agent_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = locks.open_lock(folder, "lifecycle.lock", mode=0o600)
        held = False
        try:
            try:
                handle.acquire(wait=False)
                held = True
            except BlockingIOError:
                pass
            yield held
        finally:
            if held:
                handle.release()
            handle.close()

    def _resume_id(self, agent_id, new_conversation=False, client=table.CLAUDE):
        """The conversation to resume, or None for a fresh start. A fresh start is only ever ``new_conversation`` (the owner chose
        it), no selection, a selection that is not this client's, or a selected conversation that has nothing recorded to lose. A
        selected conversation whose transcript is gone, while Qorqbot's archive holds what it said, is ``resume_unavailable``.
        The id is the selected conversation's own: Claude's session id, or a Codex thread id (its rollout file is the transcript)."""
        if new_conversation:
            return None
        if self._resume_override is not None:
            return self._resume_override(agent_id)
        database = getattr(self._runtime, "database", None)
        if database is None:
            return None
        from qorqut.native_chat import selection, store
        with database.read() as uow:
            chosen = selection.selected(uow.connection, agent_id)
            if chosen is None or chosen.provider != table.entry(client)["provider"]:
                return None
            capture = store.get_capture(uow.connection, agent_id, chosen.provider, chosen.conversation_id)
        path = capture["transcript_path"] if capture else None
        try:
            if path and Path(path).is_file():
                return chosen.conversation_id
        except OSError:
            pass
        if not capture or not (capture.get("receipt_id") or capture.get("captured_length")):
            return None  # nothing was ever recorded for it: a new session loses nothing
        raise ManagedError("resume_unavailable", "The earlier conversation can't be continued because its file is missing. "
                           "Start a new conversation to carry on without it.")

    def _prepared(self, agent_id):
        if self._prepare_override is not None:
            return self._prepare_override(agent_id)
        launch = getattr(self._runtime, "managed_launch", None)
        if launch is None or not hasattr(launch, "prepare_managed"):
            raise ManagedError("managed_unavailable", "Workspace preparation is not available")
        return launch.prepare_managed(agent_id)

    def _locate(self, client, env):
        """The executable of this client to run and where it looked: ``(path or None, searched)``. An explicit executable (tests,
        an operator) first; otherwise Qorqbot's own downloaded copy while it is the selected one, then the owner's own copy on the
        PATH their terminals use (the native user environment, not this service's minimal PATH), through the same selection the
        installer uses (``agent_cli_install.resolve``)."""
        program = table.entry(client)["provider"]
        if self._executable:
            if os.path.isabs(self._executable):
                return (self._executable if os.access(self._executable, os.X_OK) and os.path.isfile(self._executable) else None), [self._executable]
            found = shutil.which(self._executable, path=env.get("PATH") or os.environ.get("PATH"))
            return found, [env.get("PATH") or os.environ.get("PATH") or ""]
        path = agent_cli_launch.locate(program, env)
        folders = [item for item in (agent_cli_launch.search_environment(env).get("PATH") or "").split(os.pathsep) if item]
        return path, folders

    def _require_client(self, client, env, label="") -> str:
        """The executable to run for ``client``, or ``cli_unavailable`` (nothing starts) in the owner's plain words."""
        found, _searched = self._locate(client, env)
        if found:
            return found
        from qorqut.native_chat import client_state
        raise ManagedError("cli_unavailable", client_state.missing_text(label, client))

    def _start(self, profile, resume, timeout, client=table.CLAUDE):
        """Prepare, launch and wait for ``ready``. Returns the verified host's record. ``client`` is the agent's effective client:
        it decides the executable, the driver, the first message of a fresh session, the model and effort flags, and how the host
        answers permission requests."""
        agent_id = profile["agent_id"]
        if not table.available(client):
            raise ManagedError("driver_unavailable", "Running this agent with " + str(client).capitalize() + " is not available yet")
        item, name = table.entry(client), table.NAMES[client]
        # No usable client at all is said first and plainly: preparing a workspace for a client that cannot run refuses with a
        # preparation reason the owner cannot act on ("no installed agent CLI meets the baseline").
        self._require_client(client, {}, self._label(profile))
        prepared = self._prepared(agent_id)
        workspace, env = str(prepared["workspace"]), dict(prepared["env"])
        # The client runs as the owner's own command line would (their HOME, their sign-in), not with this service's private
        # HOME and folders: a value of None removes the variable in the client. What the owner set in Settings stays.
        for variable, value in agent_cli_launch.owner_variables().items():
            env.setdefault(variable, value)
        # The client's own configuration folder must be the one its conversations were created in: a resume only finds its
        # conversation there, and the host passes the client nothing but this environment (``launch.json``).
        for variable in CLIENT_HOME_VARIABLES:
            if variable not in env and os.environ.get(variable):
                env[variable] = os.environ[variable]
        found = self._require_client(client, env, self._label(profile))
        extra = agent_cli_launch.for_executable(item["provider"], found)
        args = list(extra["args"])
        args += table.always_arguments(client)
        args += table.settings_arguments(client, workspace)
        chosen = launch_options.read(self.paths, agent_id)
        # A stored model of the other client's family (left by an earlier client change, or written by the cutover tool) would make
        # the client refuse to start: it is not passed, the client's own default applies, and the stored value is left for the owner.
        model = chosen.model if models.fits(client, chosen.model) else None
        args += table.allowed_tool_arguments(client, workspace)
        args += table.launch_arguments(client, model, chosen.effort, workspace)
        # The client is started with the owner's search PATH as well as the session's: a client that is a script
        # (``#!/usr/bin/env node``, an npm install) finds its interpreter where the owner's terminal does, not only where this
        # service does. The programs a self-contained installation ships come first, then the folders the session names.
        env["PATH"] = agent_cli_launch.session_path(env) or env.get("PATH") or os.defpath
        env.update(table.launch_environment(client, own_compaction=self._own_compaction(agent_id)))
        spec = {"agent_id": agent_id, "folder": workspace, "executable": found, "args": args, "env": {**env, **extra["env"]},
                "resume_session_id": resume, "send_role": table.role_command(client) if resume is None else False,
                "idle_seconds": self._idle, "client": client, "driver": table.driver(client),
                "permission_mode": table.permission_mode(client, workspace), "model": model, "effort": chosen.effort}
        ready = table.ready_seconds(client)
        if ready is not None:
            spec["ready_seconds"] = ready
        launcher = self._launcher_for()
        try:
            verified = launcher.start_host(self._state_dir(agent_id), spec, timeout=timeout)
        except ManagedError:
            raise
        except Exception as error:  # noqa: BLE001 - LaunchError and anything the launcher raised; its detail may carry paths
            raise self._launch_failure(error) from None
        return self._record(verified)

    def _own_compaction(self, agent_id) -> bool:
        """Whether Qorqbot compacts this agent's conversation in place of the client (``CompactionController.replaces_client``)."""
        controller = getattr(self._runtime, "compaction", None)
        return controller is not None and controller.replaces_client(agent_id) is True

    @staticmethod
    def _launch_failure(error):
        reason = str(getattr(error, "reason", "") or "")
        if "resume" in reason:
            return ManagedError("resume_failed", "The selected conversation could not be resumed, so no session was opened")
        if reason == "host_unverified":
            return ManagedError("host_unverified", "A session of this agent could not be verified", retryable=True)
        if reason == "not_ready":
            return ManagedError("open_timeout", "The session did not become ready in time", retryable=True)
        if reason == "invalid_launch":
            return ManagedError("host_start_failed", "The session could not be started")
        return ManagedError("host_failed", "The session could not be started" + (f" ({reason})" if reason else ""),
                            details={"reason": reason} if reason else None)

    # ----- control ----------------------------------------------------------------------------------------------------

    def _required(self, agent_id):
        try:
            host = self.handle(agent_id)
        except HostUnverified:
            raise ManagedError("host_unverified", "The session could not be verified") from None
        if host is None:
            raise ManagedError("session_closed", "This agent has no open session")
        return host

    def interrupt(self, agent_id) -> dict:
        """Stop the running turn. An idle session ignores it."""
        return self._call(agent_id, lambda client: client.interrupt())

    def answer(self, agent_id, request_id, decision) -> dict:
        """Answer one pending permission or question request."""
        return self._call(agent_id, lambda client: client.answer(request_id, decision))

    def close(self, agent_id, mode="graceful") -> bool:
        """Ask the host to close the session (``graceful``, or ``now``). False when no host runs; the host then finishes on its
        own, and the session's end is seen through the session hooks and liveness."""
        try:
            host = self.handle(agent_id)
        except HostUnverified:
            return False
        if host is None:
            return False
        self._call_host(host, lambda client: client.close(mode))
        return True

    def close_all(self, reason="", mode="graceful") -> list:
        """Ask every verified host to close; the agent ids asked. One that cannot be reached is skipped, not an error."""
        asked = []
        for item in self.running_hosts():
            try:
                if self.close(item["agent_id"], mode):
                    asked.append(item["agent_id"])
            except ManagedError:
                continue
        return asked

    def running_hosts(self) -> list:
        """One entry per verified running host: ``{"agent_id", "state", "host_pid", "session_id", "instance"}``. A state
        folder whose host does not verify is not a running host."""
        if self.paths is None:
            return []
        try:
            found = self._launcher_for().running_hosts(layout.state_root(self.paths))
        except ManagedError:
            return []
        except Exception:  # noqa: BLE001 - nothing that cannot be verified counts
            return []
        return [{"agent_id": name, "state": host.state, "host_pid": host.host_pid, "session_id": host.session_id,
                 "instance": host.instance} for name, host in sorted(found.items())]

    def status(self, agent_id) -> dict:
        """The session's state read from the host right now, in the shape of ``snapshot`` (the cached view can be seconds old, and
        an answer must be checked against what is waiting this moment). ``ManagedError`` when there is no session or it cannot be
        read."""
        host = self._required(agent_id)
        if host.client is None:
            raise ManagedError("session_starting", "The session is still starting", retryable=True)
        try:
            reply = host.client.status()
        except Exception as error:  # noqa: BLE001
            if is_host_error(error):
                raise ManagedError("host_unverified", "The session did not answer", retryable=True) from error
            raise
        return _view(host.record, reply)

    # ----- the first message of a session -----------------------------------------------------------------------------

    @staticmethod
    def first_message_uuid(agent_id, key, text) -> str:
        """The uuid of an owner's message that opens a session, derived from the request (agent, idempotency key, text) so that a
        repeat of the same request is the same message to the host, which keeps one copy of each uuid."""
        digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"qorqut-managed-first:{agent_id}:{key}:{digest}"))

    @staticmethod
    def _digest(text) -> str:
        return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()

    def send_first(self, agent_id, text, message_uuid, deadline) -> None:
        """Hand the owner's message to the session's host directly, before the session is bound.

        Claude Code announces its session (so the hooks write the marker the binding is made from) only after it has taken up a
        first message. A fresh session gets ``/role`` from the host; a resumed one gets nothing until the owner's message, and no
        input can be admitted before there is a binding. So when no session is bound this delivers the message itself, with a
        uuid the host keeps one copy of: sending it again (a retry, a slow answer) is harmless. The message is remembered here
        (``staged_uuid``), and the input admitted for it once the session is bound is recognised by the host as this same message,
        not typed a second time. Waits for the host to take input until ``deadline`` (``open_timeout``, retryable); a host that
        is closing or gone is ``session_closed``."""
        digest = self._digest(text)
        with self._guard:
            known = self._staged.setdefault(agent_id, {})
            known[digest] = (message_uuid, self._clock() + STAGED_SECONDS)
            while len(known) > MAX_STAGED:
                known.pop(next(iter(known)))
        while True:
            try:
                host = self.handle(agent_id)
            except HostUnverified:
                raise ManagedError("host_unverified", "A session of this agent could not be verified", retryable=True) from None
            if host is None:
                raise ManagedError("session_closed", "This agent's session is not open. Send the message again to start it")
            if host.client is not None:
                try:
                    reply = host.client.send(text, message_uuid, True)
                except Exception as error:  # noqa: BLE001
                    if not is_host_error(error):
                        raise
                    reply = None  # unanswered: the same uuid is sent again below, and the host keeps one copy
                if isinstance(reply, dict) and reply.get("status") == "ok":
                    return
                reason = reply.get("reason") if isinstance(reply, dict) else None
                if reason in ("session_closing", "session_closed"):
                    raise ManagedError("session_closed", "This agent's session is closing. Send the message again to start it")
                if reason == "fenced" or (isinstance(reply, dict) and reply.get("status") not in ("ok", "rejected")):
                    raise ManagedError("host_rejected", "The session did not take the message")
            if self._clock() >= deadline:
                raise ManagedError("open_timeout", "The session did not become ready in time", retryable=True)
            self._sleep(POLL_INTERVAL)

    def staged_uuid(self, agent_id, text):
        """The uuid ``send_first`` gave this exact text for this agent, while it is remembered and not yet delivered through an
        input; else None."""
        with self._guard:
            known = self._staged.get(agent_id) or {}
            found = known.get(self._digest(text))
            if found is None:
                return None
            if found[1] < self._clock():
                known.pop(self._digest(text), None)
                return None
            return found[0]

    def unstage(self, agent_id, text) -> None:
        """Forget the staged uuid of this text: an input was submitted with it."""
        with self._guard:
            (self._staged.get(agent_id) or {}).pop(self._digest(text), None)

    def sent_before_exit(self, agent_id, message_uuid):
        """After the host of this agent has exited: ``"sent"`` when it recorded accepting this uuid, ``"absent"`` when it never
        did (the ledger is written before anything reaches Claude, so the message never left), ``"fenced"`` when it was fenced,
        None when this cannot be told (a host still holds the lock, or the ledger cannot be read)."""
        try:
            folder = self._state_dir(agent_id)
            if layout.lock_held(folder / layout.LOCK_FILE):
                return None
            raw = layout.read_private(folder / layout.SENT_FILE, 4 * 1024 * 1024)
        except Exception:  # noqa: BLE001
            return None
        found = "absent"
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("uuid") == message_uuid:
                found = "fenced" if record.get("state") == "fenced" else "sent"
        return found

    def _call(self, agent_id, operation):
        return self._call_host(self._required(agent_id), operation)

    @staticmethod
    def _call_host(host, operation):
        if host.client is None:
            raise ManagedError("session_starting", "The session is still starting", retryable=True)
        try:
            return operation(host.client)
        except Exception as error:  # noqa: BLE001
            if is_host_error(error):
                raise ManagedError("host_unverified", "The session did not answer", retryable=True) from error
            raise

    def _close_quietly(self, host, mode):
        try:
            host.client.close(mode)
        except Exception:  # noqa: BLE001
            pass


def _descends(pid, ancestor) -> bool:
    """Whether ``ancestor`` is a proper ancestor of ``pid`` (bounded walk up the parent chain)."""
    from qorqut.bootstrap.terminal_chat import process_fields
    if pid == ancestor:
        return False
    for _ in range(ANCESTOR_HOPS):
        try:
            pid = int(process_fields(pid)[1])
        except (OSError, IndexError, ValueError):
            return False
        if pid == ancestor:
            return True
        if pid <= 1:
            return False
    return False


def managed_of(runtime):
    """The ``ManagedRuntime`` of a service runtime, or None: ``runtime.managed_runtime``, else the one given to ``NativeChat``
    (``runtime.native_chat.managed``). One place every consumer asks."""
    found = getattr(runtime, "managed_runtime", None)
    if found is None:
        found = getattr(getattr(runtime, "native_chat", None), "managed", None)
    return found
