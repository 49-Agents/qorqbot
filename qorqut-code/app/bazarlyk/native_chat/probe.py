"""Observe the registered agent's native session: the hook marker, then verification against the live process and pane.

Nothing is launched, resumed or written. The marker (``.bazarlyk/terminal-chat.json``, published by the native client's own
hook) names a conversation, a transcript and a process; it is a claim, not proof. It counts only while the process exists with
the recorded start ticks and is reachable the way its transport requires: inside the agent's one registered tmux pane, or, for a
shared Codex desktop server, through that exact conversation's held thread-writer lock.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from bazarlyk.bootstrap import codex_session
from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.bootstrap.terminal_chat import CODEX_SUFFIX, NATIVE_IDENTITY, process_fields
from bazarlyk.bootstrap.transcript_snapshot import SESSION_ID
from bazarlyk.transport.agent_terminal_control import NATIVE_ONLY

from . import codex_control, codex_viewer, liveness
from .contracts import RUNTIME_PROVIDERS

LOG = logging.getLogger(__name__)
MARKER = ".bazarlyk/terminal-chat.json"
BINDING = ".bazarlyk/native-binding.json"
REGISTRATION = ".bazarlyk/registration.json"
ACTIVITY = ".bazarlyk/activity"
MAX_MARKER_BYTES = 16384


def _process_argv(pid):
    if sys.platform == 'darwin':
        from bazarlyk.process_info import cmdline
        return [arg.encode() for arg in cmdline(pid)]
    return (codex_session.PROC_ROOT / str(pid) / 'cmdline').read_bytes().split(b'\0')

REASON_LIVE = "live"
REASON_NO_MARKER = "no_session"
REASON_CAPTURE_DISABLED = "capture_disabled"  # the registration turns transcript capture off: Chat has no conversation to show
REASON_CAPTURE_NOT_READY = "capture_not_ready"  # capture is on, but the session's hook has not published its transcript yet
REASON_SESSION_END = "session_end"
REASON_PROCESS_GONE = "process_gone"
REASON_UNSUPPORTED = "native_only"
REASON_UNSUPPORTED_RUNTIME = "unsupported_runtime"


@dataclass(frozen=True)
class Observation:
    """One verified live native session."""

    agent_id: str
    runtime: str
    provider: str
    conversation_id: str
    transcript_path: str | None
    pid: int
    started: str
    transport: str
    owner: str | None
    working: bool = False
    capture: bool = True
    capture_enabled: bool = True


@dataclass(frozen=True)
class ProbeResult:
    """``observation`` is set only for ``reason == 'live'``; every other reason says why the agent has no usable session.

    ``candidate`` is a second, different live conversation seen in the workspace marker while ``observation`` is the selected
    shared Codex thread, verified independently of that marker (see ``SessionProbe.observe``). It is never an input target."""

    reason: str
    observation: Observation | None = None
    marker: dict | None = None
    candidate: Observation | None = None


def _read_json(path: Path) -> dict | None:
    if path_has_symlink(path) or not path.is_file() or path.stat().st_size > MAX_MARKER_BYTES:
        return None
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else None


def transcript_matches(runtime: str, session: str, source: Path) -> bool:
    """The hook's own rule: a Claude transcript is named for its session; a Codex rollout carries it (and an optional suffix)."""
    if not source.is_absolute() or ".." in source.parts or path_has_symlink(source):
        return False
    if runtime == "claude-code":
        return source.name == session + ".jsonl"
    return re.fullmatch(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-" + re.escape(session) + CODEX_SUFFIX + r"\.jsonl", source.name) is not None


class SessionProbe:
    """Read and verify an agent's native session. ``backend`` returns the terminal control backend (late-bound: it is
    attached to the runtime after construction). ``marker_reader`` and ``process_probe`` are injection points for synthetic
    checks; the defaults read the workspace marker and the real ``/proc`` and tmux state."""

    def __init__(self, backend=lambda: None, *, marker_reader=None, process_probe=None, selected_reader=None):
        self._backend = backend
        self._marker_reader = marker_reader or self._read_marker
        self._process_probe = process_probe or self._verify_process
        # ``selected_reader(profile) -> (Selection | None, open native_sessions row | None)``; without it the marker decides.
        self._selected_reader = selected_reader

    def observe(self, profile: dict) -> ProbeResult:
        """Follow the verified foreground conversation in the registered terminal.

        A shared server may hold many threads. Its marker or saved binding counts only when the terminal displays that thread.
        """
        result = self._observe_marker(profile)
        if result.reason == REASON_LIVE or self._selected_reader is None:
            return result
        try:
            chosen, latest = self._selected_reader(profile)
        except (OSError, DomainError, sqlite3.Error):
            return result
        if chosen is None or not chosen.shared_server:
            return result
        if (result.reason == REASON_SESSION_END and (result.marker or {}).get("session_id") == chosen.conversation_id
                and RUNTIME_PROVIDERS.get((result.marker or {}).get("runtime")) == chosen.provider):
            return result
        selected = self._observe_selected(profile, chosen, latest)
        if selected is None:
            return result
        return ProbeResult(REASON_LIVE, selected, result.marker)

    def _observe_selected(self, profile: dict, chosen, latest: dict | None) -> Observation | None:
        """The selected shared Codex thread, from the owner row alone: its server process still exists with the recorded start
        ticks, is an app-server, and still holds this thread's exact writer lock as the adopted owner tuple. ``None`` when
        any of that is not positively verified; an unreadable process is not evidence."""
        pid, started, owner = chosen.pid, chosen.started, chosen.owner
        if liveness.of_binding(chosen.binding) != liveness.ALIVE or owner is None:
            return None
        try:
            argv = _process_argv(pid)
            if b"app-server" not in argv or codex_session.thread_owner(pid, chosen.conversation_id) != owner:
                return None
            if not self._displayed_codex(profile, owner, chosen.conversation_id):
                return None
            workspace = Path(profile["workspace"])
            config = _read_json(workspace / REGISTRATION)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not config or config.get("agent_id") != profile["agent_id"]:
            return None
        transcript = None
        if latest is not None and chosen.matches(latest.get("provider"), latest.get("conversation_id")):
            transcript = latest.get("transcript_path")
        stand_in = {"agent_id": profile["agent_id"], "session_id": chosen.conversation_id, "runtime": chosen.runtime,
                    "pid": pid, "started": started}
        return Observation(
            agent_id=profile["agent_id"], runtime=chosen.runtime, provider=chosen.provider, conversation_id=chosen.conversation_id,
            transcript_path=transcript, pid=pid, started=started, transport="codex_queue", owner=owner,
            working=self._working(profile, stand_in, {"owner": owner}), capture=bool(transcript),
            capture_enabled=config.get("capture_transcripts") is True)

    def _observe_marker(self, profile: dict) -> ProbeResult:
        runtime = _runtime(profile)
        if runtime is None:
            return ProbeResult(REASON_UNSUPPORTED_RUNTIME)
        try:
            marker = self._marker_reader(profile)
        except (OSError, ValueError, DomainError):
            marker = None
        if not marker:
            return ProbeResult(REASON_NO_MARKER)
        if marker.get("event") == "SessionEnd":
            return ProbeResult(REASON_SESSION_END, marker=marker)
        try:
            verified = self._process_probe(profile, dict(marker))
        except DomainError as error:
            return ProbeResult(REASON_UNSUPPORTED if error.message == NATIVE_ONLY else REASON_PROCESS_GONE, marker=marker)
        except (OSError, ValueError, KeyError, IndexError, AttributeError):
            verified = None
        if not verified:
            return ProbeResult(REASON_PROCESS_GONE, marker=marker)
        provider = RUNTIME_PROVIDERS[marker["runtime"]]
        observation = Observation(
            agent_id=profile["agent_id"], runtime=marker["runtime"], provider=provider, conversation_id=marker["session_id"],
            transcript_path=marker.get("transcript_path"), pid=int(marker["pid"]), started=str(marker["started"]),
            transport=verified["transport"], owner=verified.get("owner"), working=self._working(profile, marker, verified),
            capture=bool(marker.get("capture")), capture_enabled=bool(marker.get("capture_enabled", marker.get("capture"))))
        return ProbeResult(REASON_LIVE, observation, marker)

    # ----- marker ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _read_marker(profile: dict):
        """The hook observation for this agent. With transcript capture on, the Chat hook's marker names the transcript; the
        always-written native binding observation supplies the same exact session without one, which is enough to reach the
        terminal but leaves Chat with no conversation to show. ``capture_enabled`` keeps the registration's own choice apart
        from that: an explicit ``capture_transcripts: false`` is disabled and is never overridden, while capture that is on but
        has not published its marker yet is merely not ready."""
        workspace = Path(profile["workspace"])
        config = _read_json(workspace / REGISTRATION)
        if not config or config.get("agent_id") != profile["agent_id"]:
            return None
        observation = _valid(_read_json(workspace / BINDING), profile, transcript=False)
        if observation and observation.get("registration_id") not in (None, config.get("registration_id")):
            observation = None
        if config.get("capture_transcripts") is True:
            marker = _valid(_read_json(workspace / MARKER), profile, transcript=True)
            if marker and (observation is None or all(
                    marker.get(key) == observation.get(key) for key in NATIVE_IDENTITY)):
                return {**marker, "capture": True, "capture_enabled": True}
        # A previous transcript marker (including SessionEnd) cannot mask the
        # current binding while its first transcript publication is pending.
        if observation:
            return {**observation, "transcript_path": None, "capture": False,
                    "capture_enabled": config.get("capture_transcripts") is True}
        return None

    # ----- process ---------------------------------------------------------------------------------------------------

    def _verify_process(self, profile: dict, marker: dict) -> dict | None:
        pid = int(marker["pid"])
        native = process_fields(pid)
        if native[0] == "Z" or native[19] != marker["started"]:
            return None
        if (marker["runtime"] == "codex"
                and b"app-server" in _process_argv(pid)):
            # The desktop server is shared by many conversations. Only this session's held writer lock proves that its
            # native thread is open.
            owner = codex_session.thread_owner(pid, marker["session_id"])
            return ({"transport": "codex_queue", "owner": owner}
                    if owner and self._displayed_codex(profile, owner, marker["session_id"]) else None)
        backend = self._backend()
        if backend is None:
            return None
        broker, _, session, _, _ = backend.selected(profile)
        code, output = broker.tmux("list-panes", "-t", "=" + session, "-F", "#{pane_pid}")
        if code or len(output.strip().splitlines()) != 1:
            return None
        pane_pid = int(output.strip())
        if native[2] != process_fields(pane_pid)[5]:
            return None
        for _ in range(24):
            if pid == pane_pid:
                return {"transport": "terminal", "owner": None}
            pid = int(process_fields(pid)[1])
            if pid <= 1:
                break
        return None

    def _displayed_codex(self, profile: dict, owner: str, conversation: str) -> bool:
        """A server-held thread is current only when the registered foreground TUI displays it."""
        backend = self._backend()
        if backend is None:
            return False
        try:
            pane = backend.pane_observation(profile, "codex")
            client = pane.get("client")
            endpoint = "unix://" + codex_control.control_socket(owner)
            if (pane.get("kind") != "client" or not client
                    or not codex_viewer.configured(client["argv"], endpoint, profile["workspace"])
                    or codex_viewer.displayed_thread(owner, pane.get("title")) != conversation):
                return False
            # Thread lookup must not outlive a change of pane or foreground client.
            return backend.pane_observation(profile, "codex") == pane
        except (OSError, ValueError, TypeError, KeyError, DomainError):
            return False

    @staticmethod
    def _working(profile: dict, marker: dict, verified: dict) -> bool:
        """Whether a turn is running, from the small hook observation of this exact native process and session."""
        try:
            path = Path(profile["workspace"]) / ACTIVITY / (marker["session_id"] + ".json")
            if path_has_symlink(path) or not path.is_file() or path.stat().st_size > 8192:
                return False
            activity = json.loads(path.read_text())
            return (all(activity.get(key) == marker[key] for key in ("agent_id", "session_id", "runtime"))
                    and activity.get("owner") == (verified.get("owner") or f"{marker['pid']}:{marker['started']}")
                    and activity.get("state") == "running")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False


def _valid(marker: dict | None, profile: dict, *, transcript: bool) -> dict | None:
    if not marker:
        return None
    session, runtime = marker.get("session_id"), marker.get("runtime")
    if (marker.get("agent_id") != profile["agent_id"] or marker.get("workspace") != profile["workspace"]
            or runtime not in RUNTIME_PROVIDERS or not isinstance(session, str) or not SESSION_ID.fullmatch(session)
            or not isinstance(marker.get("pid"), int) or not isinstance(marker.get("started"), str)):
        return None
    if transcript and (not isinstance(marker.get("transcript_path"), str)
                       or not transcript_matches(runtime, session, Path(marker["transcript_path"]))):
        return None
    return marker


def _runtime(profile: dict) -> str | None:
    data = json.loads(profile["profile_json"]) if isinstance(profile["profile_json"], str) else profile["profile_json"]
    runtime = data.get("runtime") if isinstance(data, dict) else None
    return runtime if runtime in RUNTIME_PROVIDERS else None


def process_alive(pid: int, started: str) -> bool:
    """Whether the recorded process still exists with its start ticks (a recycled PID does not count)."""
    try:
        fields = process_fields(pid)
        return fields[0] != "Z" and fields[19] == started
    except (OSError, IndexError, ValueError):
        return False


__all__ = ["Observation", "ProbeResult", "SessionProbe", "process_alive", "transcript_matches"]
