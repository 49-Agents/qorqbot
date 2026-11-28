"""Verified conversation attachment and compatibility controls for older terminal clients (Terminals mode, Claude Code only).

The current UI mirrors the registered terminal directly. Chat's observer follows its verified foreground conversation.
Older clients can still request a specific attachment token or an explicit resume; those requests retain their identity
checks and never interrupt a different foreground process. On an installation that runs managed sessions none of this acts:
the view says so and a resume is refused before any terminal is touched. A selected conversation that is not a Claude Code
conversation counts as nothing selected: a terminal resumes only Claude Code conversations.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_system import platform as _platform, private_files
from qorqut.bootstrap import features
from qorqut.managed_runtime import mode
from qorqut.transport.agent_terminal_control import NATIVE_ONLY, TERMINAL_MODE_OFF, start_flags

from . import liveness, selection, store
from .probe import REASON_LIVE, REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME

LOG = logging.getLogger(__name__)

ATTACHED, RECONNECTING, DISCONNECTED, MISMATCH, UNSUPPORTED = (
    "attached", "reconnecting", "disconnected", "mismatch", "unsupported")
ACTION_RECONNECT, ACTION_SELECT = "reconnect", "select_candidate"
MODE_SELECTED, MODE_FIRST_LAUNCH, MODE_UNMANAGED = "selected", "first_launch", "unmanaged"

# A submitted resume keeps the view ``reconnecting`` for this long: while its client (or, for a shell prompt that has not yet run
# the command, the shell) has not shown the selected conversation. A launch that never registers then reads as a failure.
SHELL_LAUNCH_SECONDS = 6
CLIENT_LAUNCH_SECONDS = 90
STOP_WAIT_SECONDS = 5
LAUNCHER_LOOKUP_SECONDS = 5
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLIENT_NAMES = {"claude": "Claude Code"}
PROC_ROOT = Path("/proc")


def _view(agent_id, chosen, state, reason, detail, *, action=None, observed=None, candidate=None, token=None,
          mode=None, auto_reconnect=False) -> dict:
    return {"agent_id": agent_id, "state": state, "reason": reason, "detail": detail, "action": action,
            "mode": mode or (MODE_SELECTED if chosen is not None else MODE_FIRST_LAUNCH),
            "selection": chosen.public(terminal=True) if chosen is not None else None,
            "observed": observed, "candidate": candidate, "token": token, "auto_reconnect": auto_reconnect}


def _same_pane(first: dict, second: dict) -> bool:
    """The same registered pane and the same shell process in it: tmux session id, pane id, shell pid and its start ticks."""
    return (first["state"] == second["state"] == "present"
            and all(first[key] == second[key] for key in ("session_id", "pane_id", "pane_pid", "shell_started")))


def effective_executable(client: str, shell_pid: int) -> str:
    """The executable the registered pane's shell will actually run for ``client``, resolved the way that shell resolves it.

    The pane's shell is the prepared agent shell (``bash --rcfile .qorqut/agent-shell.bash -i``, whose ``claude`` function
    runs ``command claude`` with the launch settings). Its PATH is whatever its startup files make it, which can differ from
    this service's. So the lookup runs that same shell, with that shell's own environment, working directory and arguments, and
    asks it for the executable (``type -P`` skips functions and aliases, exactly what ``command claude`` runs). Anything that
    cannot be established this way is ``unsupported_runtime``; no other binary is ever substituted."""
    root = PROC_ROOT / str(shell_pid)
    unsupported = DomainError("unsupported_runtime", "The terminal's launcher for this client cannot be established")
    try:
        if _platform.family() == 'mac':
            from qorqut import process_info
            before = process_info.start_token(shell_pid)
            argv, environment = process_info.cmdline(shell_pid), process_info.environ(shell_pid)
            executable, cwd = str(process_info.exe(shell_pid)), str(process_info.cwd(shell_pid))
            if process_info.start_token(shell_pid) != before:
                raise unsupported
        else:
            argv = [item.decode("utf-8") for item in (root / "cmdline").read_bytes().split(b"\0") if item]
            environment = dict(item.split("=", 1) for item in (root / "environ").read_bytes().decode("utf-8").split("\0") if "=" in item)
            executable, cwd = private_files.read_link(root / "exe"), private_files.read_link(root / "cwd")
    except (OSError, ValueError):
        raise unsupported from None
    if Path(executable).name != "bash" or "--rcfile" not in argv or "-i" not in argv:
        raise unsupported
    try:
        found = subprocess.run([executable, *argv[1:], "-c", f"type -P {client}"], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=LAUNCHER_LOOKUP_SECONDS, env=environment, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        raise unsupported from None
    lines = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    path = lines[-1] if found.returncode == 0 and lines else ""
    if not path.startswith("/") or not os.access(path, os.X_OK):
        raise unsupported
    return path


class AttachmentService:
    """Evaluate (read-only) and, only on an explicit owner action, re-establish the view of the selected conversation.

    Authorization is the caller's: the Terminal route has already checked the owner and protected control of this agent, and
    Chat checks its own target. Every method here re-reads the selection and the pane, so a concurrent change is seen, not
    assumed away. ``locks`` is the runtime's one ``AgentLocks`` (shared with the arbiter, the backend, selection and the
    controller); the service holds the agent's lock across the final recheck and the side effect itself."""

    def __init__(self, runtime, probe, locks):
        self.runtime, self.probe, self.locks = runtime, probe, locks
        # Launch grace periods only. Positive attachment always comes from fresh native evidence.
        self._launches: dict[str, tuple[str, str, float]] = {}

    # ----- reads -----------------------------------------------------------------------------------------------------

    def _backend(self):
        return getattr(getattr(self.runtime, "terminal_control", None), "backend", None)

    def _terminals(self) -> bool:
        return mode.is_terminal(getattr(self.runtime, "paths", None))

    def _require_terminals(self) -> None:
        """The gate in front of every action: a managed installation (or one mid-switch) has no terminal to resume into."""
        paths = getattr(self.runtime, "paths", None)
        if mode.switching(paths):
            raise DomainError("runtime_switching", "Agents are being switched. Try again in a minute.", retryable=True)
        if not mode.is_terminal(paths):
            raise DomainError("terminal_mode_off", TERMINAL_MODE_OFF)

    def _load(self, agent_id: str):
        """The agent's profile and its selected conversation; a selection of any client but Claude Code is no selection."""
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
            chosen = selection.selected(uow.connection, agent_id) if profile is not None else None
        if chosen is not None and chosen.provider != "claude":
            chosen = None
        # The same rule as the backend's: an agent whose effective client is not Claude Code has nothing a terminal can resume.
        client_of = getattr(self._backend(), "terminal_client", None)
        if chosen is not None and client_of is not None and client_of(profile) is None:
            chosen = None
        return profile, chosen

    def revision(self, agent_id: str) -> str | None:
        """The selected revision now (None: no selection). The controller captures it before it changes a terminal."""
        _, chosen = self._load(agent_id)
        return chosen.revision if chosen is not None else None

    def state(self, agent_id: str) -> dict:
        """The attachment as it is now. Starts, resumes and types nothing."""
        profile, chosen = self._load(agent_id)
        if profile is None:
            return _view(agent_id, None, UNSUPPORTED, "not_a_native_agent", "This agent has no native conversation",
                         mode=MODE_UNMANAGED)
        return self._evaluate(profile, chosen)

    def check_input(self, agent_id: str, token) -> None:
        """The raw terminal input and stream boundary: with a selection, keystrokes and output are for the selected conversation
        only while the terminal is positively attached to it and the caller's view is of exactly this attachment. Re-read on
        every request. Without a selection (first launch) or a native agent, the terminal keeps its plain-shell semantics."""
        view = self.state(agent_id)
        if view["mode"] != MODE_SELECTED:
            return
        if view["state"] != ATTACHED:
            raise DomainError("terminal_not_attached", view["detail"] or "The terminal is not showing the selected conversation")
        if not isinstance(token, str) or token != view["token"]:
            raise DomainError("terminal_changed", "The terminal changed; reconnect to the selected conversation")

    def guarded(self, agent_id: str, token, action):
        """Run ``action`` (the raw terminal write) only if, under the agent's lock, the selected attachment still is the one the
        caller names. A select, reconnect or controller restart waits for the lock, so none can land between the check and
        the write."""
        with self.locks(agent_id):
            self.check_input(agent_id, token)
            return action()

    def _launching(self, agent_id: str, chosen, pane: dict, seconds: float) -> bool:
        record = self._launches.get(agent_id)
        return (record is not None and record[0] == chosen.revision and record[1] == pane.get("pane_id")
                and time.monotonic() - record[2] < seconds)

    def _evaluate(self, profile: dict, chosen) -> dict:
        agent_id = profile["agent_id"]
        if not self._terminals():
            # A managed agent runs headless under its own host: it has no pane to attach to, so no pane is looked at (and no
            # "not in terminal" or mismatch is reported). ``selected`` keeps the raw terminal input boundary closed for it.
            return _view(agent_id, chosen, UNSUPPORTED, "managed", "Qorqbot runs this agent; it has no terminal to attach",
                         mode=MODE_SELECTED)
        observed = self.probe.observe(profile)
        live = observed.observation if observed.reason == REASON_LIVE else None
        relation = selection.relation(chosen, live)
        other = live if relation == selection.CANDIDATE else None
        candidate = ({"provider": other.provider, "conversation_id": other.conversation_id, "transport": other.transport}
                     if other is not None else None)
        if observed.reason in (REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME):
            return _view(agent_id, chosen, UNSUPPORTED, observed.reason, "This agent's native session cannot be attached here")
        if chosen is None:
            return _view(agent_id, None, DISCONNECTED, selection.REASON_NO_SELECTION,
                         "No conversation is selected for this agent yet", action=ACTION_SELECT if candidate else None,
                         candidate=candidate)
        client = chosen.provider
        backend = self._backend()
        if backend is None:
            return _view(agent_id, chosen, UNSUPPORTED, "terminal_unavailable", "Host terminal control is unavailable")
        try:
            pane = backend.pane_observation(profile, client)
        except DomainError as error:
            if error.message == NATIVE_ONLY:
                return _view(agent_id, chosen, UNSUPPORTED, "native_only", "Attaching needs this agent's native session on the host")
            return _view(agent_id, chosen, DISCONNECTED, "unverifiable", "The terminal cannot be verified right now",
                         candidate=candidate)
        if candidate is not None:
            return _view(agent_id, chosen, MISMATCH, selection.REASON_CANDIDATE,
                         "Another conversation is running in the terminal; select it to use it, or reconnect the selected one "
                         "after it stops", action=ACTION_SELECT, candidate=candidate)
        state = liveness.of_binding(chosen.binding)
        if state == liveness.UNKNOWN:
            return _view(agent_id, chosen, DISCONNECTED, "unverifiable",
                         "The selected conversation's process cannot be verified right now")
        return self._terminal_owned(agent_id, chosen, pane, state, live, relation)

    def _terminal_owned(self, agent_id, chosen, pane, state, live, relation) -> dict:
        """Claude Code: attached only when the selected process is the pane's foreground client."""
        client = chosen.provider
        if state == liveness.ALIVE:
            found = pane.get("client")
            if (relation == selection.HELD and live.transport == "terminal" and pane.get("kind") == "client" and found
                    and found["pid"] == live.pid and found["started"] == live.started):
                observed = {"client": client, "pane_id": pane["pane_id"], "pid": live.pid, "started": live.started,
                            "transport": "terminal"}
                return _view(agent_id, chosen, ATTACHED, "verified", f"{CLIENT_NAMES[client]} is showing the selected conversation",
                             observed=observed, token=self._token(agent_id, chosen, observed))
            return _view(agent_id, chosen, MISMATCH, "selected_not_in_terminal",
                         "The selected conversation is running but is not the foreground client of this agent's terminal")
        if relation == selection.PENDING:
            # The same conversation re-held by a new process after the old one is gone: its adoption is still due.
            return _view(agent_id, chosen, RECONNECTING, selection.REASON_PENDING, "The resumed conversation is registering")
        return self._stopped(agent_id, chosen, pane)

    def _stopped(self, agent_id, chosen, pane) -> dict:
        """The selected conversation's process is verified gone: exact resume is possible only into no pane or a pane whose
        registered shell is verifiably the idle foreground."""
        auto = chosen.desired_state == "running"
        if pane["state"] == "absent" or pane["kind"] == "shell":
            if pane["state"] == "present" and self._launching(agent_id, chosen, pane, SHELL_LAUNCH_SECONDS):
                return _view(agent_id, chosen, RECONNECTING, "launching", "Resuming the selected conversation")
            return _view(agent_id, chosen, DISCONNECTED, "not_running", "The selected conversation is not running",
                         action=ACTION_RECONNECT, auto_reconnect=auto)
        if pane["kind"] == "client":
            if self._launching(agent_id, chosen, pane, CLIENT_LAUNCH_SECONDS):
                return _view(agent_id, chosen, RECONNECTING, "launching", "Resuming the selected conversation")
            return _view(agent_id, chosen, MISMATCH, "unverified_client",
                         "A client is running here that cannot be verified as the selected conversation")
        return _view(agent_id, chosen, MISMATCH, "other_program", "Another program is running in this agent's terminal")

    @staticmethod
    def _token(agent_id, chosen, observed) -> str:
        """Identifies this attachment: it changes with the selected revision, the pane or the client process."""
        parts = (agent_id, chosen.revision, observed["pane_id"], str(observed["pid"]), observed["started"], observed["transport"])
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]

    # ----- explicit owner action -------------------------------------------------------------------------------------

    def reconnect(self, agent_id: str, expected_revision) -> dict:
        """Resume exactly the selected conversation into the agent's absent or idle-shell terminal. The caller has authorized the
        owner and made sure a shell exists. Serialized per agent on the runtime's shared lock: a concurrent call finds the
        first one's launch and returns it, so two requests never start two clients. Anything that cannot be proved safe is
        refused; nothing falls back to a fresh conversation."""
        self._require_terminals()
        with self.locks(agent_id):
            profile, chosen = self._load(agent_id)
            if profile is None:
                raise DomainError("not_found", "Select an active registered agent")
            if chosen is None:
                raise DomainError("selection_required", "No conversation is selected for this agent yet; start the agent in its terminal first")
            if chosen.revision != expected_revision:
                raise DomainError("revision_conflict", "The selected conversation changed; reload and try again")
            view = self._evaluate(profile, chosen)
            if view["state"] in (ATTACHED, RECONNECTING):
                return view
            if view["action"] != ACTION_RECONNECT:
                raise DomainError("invalid_transition", view["detail"] or "The terminal cannot be reconnected")
            self._launch(profile, chosen, expected_revision)
            return self._evaluate(profile, chosen)

    def resume_selected(self, agent_id: str, authorized) -> str | None:
        """Terminal control start/restart: resume exactly the selection the owner's action was authorized against, with no role
        bootstrap. ``authorized`` is the revision captured before the old pane was touched (None: nothing was selected, so the
        controller's own first-launch launcher applies and ``None`` is returned). A selection that is not the authorized one
        any more, or a launch that cannot be proved safe, raises; it is never replaced by a fresh conversation."""
        self._require_terminals()
        with self.locks(agent_id):
            profile, chosen = self._load(agent_id)
            current = chosen.revision if chosen is not None else None
            if current != authorized:
                raise DomainError("revision_conflict", "The selected conversation changed during the restart; nothing was started")
            if chosen is None:
                return None
            deadline = time.monotonic() + STOP_WAIT_SECONDS
            while True:
                profile, chosen = self._load(agent_id)
                if chosen is None or chosen.revision != authorized:
                    raise DomainError("revision_conflict", "The selected conversation changed during the restart; nothing was started")
                view = self._evaluate(profile, chosen)
                if view["action"] == ACTION_RECONNECT:
                    break
                if view["state"] == ATTACHED:
                    return f"{CLIENT_NAMES[chosen.provider]} is already showing the selected conversation"
                if view["reason"] == "selected_not_in_terminal" and time.monotonic() < deadline:
                    time.sleep(.2)
                    continue
                raise DomainError("invalid_transition", view["detail"] or "The selected conversation cannot be resumed")
            self._launch(profile, chosen, authorized)
            return (f"{CLIENT_NAMES[chosen.provider]} resume of the selected conversation submitted; "
                    "readiness is not confirmed and no role was bootstrapped")

    def _command(self, chosen, shell_pid: int) -> str:
        """The one exact resume command, after checking the capability of the executable the pane's shell will really run. The
        agent's stored model and effort are appended, as for every start Qorqbot types."""
        conversation = chosen.conversation_id
        if not UUID.fullmatch(conversation):
            raise DomainError("invalid_transition", "The selected conversation id is not resumable")
        executable = effective_executable("claude", shell_pid)
        if not features.probe(executable).get("resume"):
            raise DomainError("unsupported_runtime", "The installed Claude Code does not document --resume")
        return f"claude --resume {conversation}" + start_flags(self.runtime.paths, chosen.agent_id)

    def _launch(self, profile: dict, chosen, authorized: str) -> None:
        """Type the one exact resume command. Caller holds the agent's lock. The selection and the pane are re-read at the last
        moment before each side effect: before the line-clear and again before the submit, and only a verified idle foreground
        shell, the same one as first observed, is typed into. A mismatch refuses without typing anything."""
        agent_id, client = profile["agent_id"], chosen.provider
        backend = self._backend()
        if backend is None:
            raise DomainError("operation_unavailable", "Host terminal control is unavailable")
        broker, _, session, _, _ = backend.selected(profile)
        first = backend.pane_observation(profile, client)
        if first["state"] != "present" or first["kind"] != "shell":
            raise DomainError("invalid_transition", "The terminal is not an idle shell")
        command = self._command(chosen, first["pane_pid"])
        self._recheck(profile, authorized, first)
        code, _ = broker.tmux("send-keys", "-t", first["pane_id"], "C-u")
        if code:
            raise DomainError("operation_failed", "Could not prepare shell input")
        self._recheck(profile, authorized, first)
        self._launches[agent_id] = (chosen.revision, first["pane_id"], time.monotonic())
        # Address the observed pane itself. A session name may have been reused,
        # and the broker's generic submit helper intentionally targets by name.
        code, _ = broker.tmux("send-keys", "-t", first["pane_id"], "-l", command)
        if code:
            raise DomainError("operation_failed", "Could not enter the resume command")
        self._recheck(profile, authorized, first)
        code, _ = broker.tmux("send-keys", "-t", first["pane_id"], "Enter")
        if code:
            raise DomainError("operation_failed", "Resume submission could not be confirmed")

    def _recheck(self, profile: dict, authorized: str, expected: dict) -> None:
        """Right before a side effect: the selection is still the authorized one and the pane is still the same idle shell."""
        _, chosen = self._load(profile["agent_id"])
        if chosen is None or chosen.revision != authorized:
            raise DomainError("revision_conflict", "The selected conversation changed; nothing was typed")
        current = self._backend().pane_observation(profile, chosen.provider)
        if current["state"] != "present" or current["kind"] != "shell" or not _same_pane(expected, current):
            raise DomainError("invalid_transition", "The terminal changed; nothing was typed")
