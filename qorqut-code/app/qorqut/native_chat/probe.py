"""Observe the agent's native session: the hook marker, then verification against the live process and its host.

Nothing is launched, resumed or written. The marker (``.qorqut/terminal-chat.json``, published by the native client's own
hook) names a conversation, a transcript and a process; it is a claim, not proof. It counts only while the process exists with
the recorded start ticks and the managed runtime verifies the host process that drives it and that the client descends from it
(for Codex also that the client is the app-server holding that exact conversation's thread-writer lock).

In Terminals mode (``terminal_mode``, asked on every observation) the same marker counts only for a Claude Code client that runs
inside the agent's one registered tmux pane; the managed runtime is never asked. In managed mode nothing here touches tmux.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap import codex_session
from qorqut.bootstrap.client import path_has_symlink
from qorqut.bootstrap.terminal_chat import CODEX_SUFFIX, NATIVE_IDENTITY, process_fields
from qorqut.bootstrap.transcript_snapshot import SESSION_ID
from qorqut_system import platform

from .contracts import RUNTIME_PROVIDERS

LOG = logging.getLogger(__name__)
MARKER = ".qorqut/terminal-chat.json"
BINDING = ".qorqut/native-binding.json"
REGISTRATION = ".qorqut/registration.json"
ACTIVITY = ".qorqut/activity"
MAX_MARKER_BYTES = 16384


def _process_argv(pid):
    if platform.family() == 'mac':
        from qorqut.process_info import cmdline
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
MANAGED_WORKING = frozenset({"working", "blocked"})


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
    # What the managed host reports about its session (``phase`` is one of starting/idle/working/blocked/closing).
    phase: str | None = None
    activity: str | None = None
    pending: tuple = ()


@dataclass(frozen=True)
class ProbeResult:
    """``observation`` is set only for ``reason == 'live'``; every other reason says why the agent has no usable session."""

    reason: str
    observation: Observation | None = None
    marker: dict | None = None


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
    """Read and verify an agent's native session. ``marker_reader`` and ``process_probe`` are injection points for synthetic
    checks; the defaults read the workspace marker and the real ``/proc`` state. ``managed`` is the managed runtime, used duck-typed:
    ``verify(profile, marker) -> {"transport", "owner"} | None``, ``identity(agent_id)`` and ``snapshot(agent_id) -> dict | None``.
    """

    def __init__(self, *, marker_reader=None, process_probe=None, managed=None, terminal_backend=None, terminal_mode=None):
        """``terminal_backend`` is a zero-argument callable returning the terminal control backend (late-bound: it is attached
        to the runtime after construction) or None. ``terminal_mode`` is a zero-argument callable answering whether the
        installation runs in terminals; it is asked on every observation and defaults to never."""
        self._managed = managed
        self._backend = terminal_backend or (lambda: None)
        self._terminal_mode = terminal_mode or (lambda: False)
        self._marker_reader = marker_reader or self._read_marker
        self._injected_probe = process_probe
        self._process_probe = process_probe or self._verify_process

    def observe(self, profile: dict) -> ProbeResult:
        """The agent's verified live session, or the reason it has none."""
        if self._terminal_mode():
            return self._observe_marker(profile, terminal=True)
        return self._observe_marker(profile)

    def _observe_marker(self, profile: dict, terminal: bool = False) -> ProbeResult:
        runtime = _runtime(profile)
        if runtime is None:
            return ProbeResult(REASON_UNSUPPORTED_RUNTIME)
        try:
            marker = self._marker_reader(profile)
        except (OSError, ValueError, DomainError):
            marker = None
        if not marker:
            return ProbeResult(REASON_NO_MARKER)
        if terminal and marker.get("runtime") != "claude-code":
            # Terminals run Claude Code only. The client that registered decides, not the profile: the owner can run an agent
            # whose profile names Codex on Claude Code (Settings), and that agent has a terminal session like any other.
            return ProbeResult(REASON_UNSUPPORTED_RUNTIME, marker=marker)
        if marker.get("event") == "SessionEnd":
            return ProbeResult(REASON_SESSION_END, marker=marker)
        process_probe = (self._injected_probe or self._verify_terminal) if terminal else self._process_probe
        try:
            verified = process_probe(profile, dict(marker))
        except DomainError as error:
            if terminal and error.message == _native_only():
                return ProbeResult(REASON_UNSUPPORTED, marker=marker)
            return ProbeResult(REASON_PROCESS_GONE, marker=marker)
        except (OSError, ValueError, KeyError, IndexError, AttributeError):
            verified = None
        if not verified:
            return ProbeResult(REASON_PROCESS_GONE, marker=marker)
        provider = RUNTIME_PROVIDERS[marker["runtime"]]
        extra = {} if terminal else self._managed_state(profile)
        working = self._working(profile, marker) or extra.get("phase") in MANAGED_WORKING
        observation = Observation(
            agent_id=profile["agent_id"], runtime=marker["runtime"], provider=provider, conversation_id=marker["session_id"],
            transcript_path=marker.get("transcript_path"), pid=int(marker["pid"]), started=str(marker["started"]),
            transport=verified["transport"], owner=verified.get("owner"), working=working,
            capture=bool(marker.get("capture")), capture_enabled=bool(marker.get("capture_enabled", marker.get("capture"))),
            **extra)
        return ProbeResult(REASON_LIVE, observation, marker)

    def _managed_state(self, profile: dict) -> dict:
        """The host's own account of its session; empty when the runtime cannot give it."""
        if self._managed is None:
            return {}
        try:
            snapshot = self._managed.snapshot(profile["agent_id"])
        except Exception:
            LOG.debug("Managed session state is unavailable for %s", profile["agent_id"], exc_info=True)
            return {}
        if not isinstance(snapshot, dict):
            return {}
        phase, activity = snapshot.get("phase"), snapshot.get("activity")
        pending = snapshot.get("pending")
        return {"phase": phase if isinstance(phase, str) else None, "activity": activity if isinstance(activity, str) else None,
                "pending": tuple(item for item in pending if isinstance(item, dict)) if isinstance(pending, (list, tuple)) else ()}

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

    def _is_the_hosted_client(self, profile: dict, marker: dict, hit: dict) -> bool:
        """Whether the marker's process is the claude the verified host reports, not merely some descendant of the host.

        ``managed.verify`` proves that the marker's process descends from the token-verified host; a process an agent starts
        from its own Bash tool descends from it too, and the marker is a file the agent can write. So the process the marker
        names must be exactly the one the host reports (pid and start ticks), the host named by the owner must be the one the
        host reports, and the conversation must be the host's. A runtime that cannot say is a refusal."""
        identity_of = getattr(self._managed, "identity", None)
        identity = identity_of(profile["agent_id"]) if callable(identity_of) else None
        if not isinstance(identity, dict):
            return False
        pid, started = identity.get("pid"), identity.get("started")
        return (type(pid) is int and isinstance(started, str) and pid == int(marker["pid"]) and started == str(marker["started"])
                and identity.get("conversation_id") in (None, marker.get("session_id"))
                and isinstance(identity.get("owner"), str) and identity["owner"] == hit.get("owner"))

    def _managed_hit(self, profile: dict, marker: dict, pid: int) -> dict | None:
        """``{"transport": "managed", "owner"}`` for the marker's process when it is exactly the client of this agent's verified
        host, else ``None``. For Codex the process must also be an ``app-server --listen stdio://`` holding this conversation's
        thread-writer lock (a platform that cannot show locks, macOS, relies on the host identity alone), so a thread another process
        serves, or a ``codex`` that is not the host's app-server, never counts even when it descends from the host."""
        try:
            hit = self._managed.verify(profile, marker)
            hit = hit if hit and self._is_the_hosted_client(profile, marker, hit) else None
            if hit and marker["runtime"] == "codex" and not self._serves_thread(pid, marker["session_id"]):
                hit = None
        except Exception:
            LOG.debug("Managed host verification failed for %s", profile["agent_id"], exc_info=True)
            return None
        if hit and hit.get("transport") == "managed" and isinstance(hit.get("owner"), str):
            return {"transport": "managed", "owner": hit["owner"]}
        return None

    @staticmethod
    def _serves_thread(pid: int, session: str) -> bool:
        if not codex_session.runs_app_server(_process_argv(pid)):
            return False
        return platform.family() == "mac" or codex_session.thread_owner(pid, session, home=False) is not None

    def _verify_process(self, profile: dict, marker: dict) -> dict | None:
        pid = int(marker["pid"])
        native = process_fields(pid)
        if native[0] == "Z" or native[19] != marker["started"]:
            return None
        if self._managed is not None and marker["runtime"] in ("claude-code", "codex"):
            # The client has no pane: Claude's ``claude -p``, Codex's ``app-server --listen stdio://`` (one thread per app-server).
            # Its host must be verified and the marker's process must be the very client the host reports.
            return self._managed_hit(profile, marker, pid)
        return None

    def _verify_terminal(self, profile: dict, marker: dict) -> dict | None:
        """Terminals mode: the marker's process is alive with its start ticks, is a descendant of the agent's one registered tmux
        pane, and shares the pane shell's foreground process group. Nothing else counts (no host is asked)."""
        pid = int(marker["pid"])
        native = process_fields(pid)
        if native[0] == "Z" or native[19] != marker["started"] or marker["runtime"] != "claude-code":
            return None
        backend = self._backend()
        if backend is None:
            return None
        broker, _, session, _, _ = backend.selected(profile)
        code, output = broker.tmux("list-panes", "-t", "=" + session, "-F", "#{pane_pid}")
        if code or len(output.strip().splitlines()) != 1:
            return None
        pane_pid = int(output.strip())
        if native[2] != process_fields(pane_pid, terminal=True)[5]:
            return None
        for _ in range(24):
            if pid == pane_pid:
                return {"transport": "terminal", "owner": None}
            pid = int(process_fields(pid)[1])
            if pid <= 1:
                break
        return None

    @staticmethod
    def _working(profile: dict, marker: dict) -> bool:
        """Whether a turn is running, from the small hook observation of this exact native process and session."""
        try:
            path = Path(profile["workspace"]) / ACTIVITY / (marker["session_id"] + ".json")
            if path_has_symlink(path) or not path.is_file() or path.stat().st_size > 8192:
                return False
            activity = json.loads(path.read_text())
            # The hook records its client process, ``pid:ticks``. A managed observation's owner is its host, so the client
            # process is what the activity file has to name.
            owner = f"{marker['pid']}:{marker['started']}"
            # A managed Codex app-server's hook writes its held-lock owner (``pid:ticks:fd:dev:inode``): the same process.
            written = activity.get("owner")
            if marker["runtime"] == "codex":
                written = codex_session.owner_process(written)
            return (all(activity.get(key) == marker[key] for key in ("agent_id", "session_id", "runtime"))
                    and written == owner
                    and activity.get("state") == "running")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False


def _native_only() -> str:
    from qorqut.transport.agent_terminal_control import NATIVE_ONLY
    return NATIVE_ONLY


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
