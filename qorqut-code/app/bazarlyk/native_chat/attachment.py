"""One account of whether an agent's registered Terminal shows its selected native conversation.

Chat and Terminal both consume this. The selected conversation is ``selection.py`` (the owner row); the terminal is only a
*view* of it. A tmux session existing says nothing about which conversation its pane shows, so nothing here infers
``attached`` from that. A state is reported only from evidence that is read, never assumed:

* ``attached``      the selected conversation is the *foreground client* of this agent's one registered pane: the hook marker
                    names it, the live process has the owner row's pid and start ticks, descends from the pane shell on the
                    pane's terminal and is the pane's foreground process group (Claude Code and a foreground Codex). A shared
                    Codex viewer also publishes its current session ID in its terminal title. Its displayed ID must resolve
                    uniquely to the selection on the verified server; the writer lock alone is insufficient.
* ``reconnecting``  a resume was just submitted, or the resumed client has not registered yet. Bounded by a short window.
* ``disconnected``  the selected conversation is not shown: stopped, no client attached, or its liveness cannot be verified
                    (an unverifiable process is never treated as dead).
* ``mismatch``      the pane runs something that is not the selected conversation (another conversation, a client that cannot
                    be tied to it, another program, the selected client suspended behind the shell). It is left untouched;
                    nothing is killed or typed into it.
* ``unsupported``   no verified way to attach this agent (remote execution target, other runtime, missing client capability, a
                    shared-server viewer whose attachment cannot be proved: reason ``viewer_unverified``).

``allowed action`` is what the owner may do next: ``reconnect`` (resume exactly the selected conversation, only into an absent
pane or a pane whose registered shell is verifiably the idle foreground, never a fresh conversation) or ``select_candidate``
(explicitly select the other conversation that is running). A new conversation is never created here: the owner uses the real
client's own controls in a clearly separate diagnostic terminal and then selects it.

Every side effect (the launch, the selected raw input) runs under the agent's one shared lock, after re-reading the selection
and the pane right before it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from qorqut_operations.contracts import DomainError
from bazarlyk.bootstrap import codex_session, features
from bazarlyk.transport.agent_terminal_control import NATIVE_ONLY, runtime_client

from . import codex_control, codex_viewer, liveness, selection, store
from .probe import REASON_LIVE, REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME

LOG = logging.getLogger(__name__)

ATTACHED, RECONNECTING, DISCONNECTED, MISMATCH, UNSUPPORTED = (
    "attached", "reconnecting", "disconnected", "mismatch", "unsupported")
ACTION_RECONNECT, ACTION_SELECT = "reconnect", "select_candidate"
MODE_SELECTED, MODE_FIRST_LAUNCH, MODE_UNMANAGED = "selected", "first_launch", "unmanaged"
REASON_VIEWER_UNVERIFIED = "viewer_unverified"

# A submitted resume keeps the view ``reconnecting`` for this long: while its client (or, for a shell prompt that has not yet run
# the command, the shell) has not shown the selected conversation. A launch that never registers then reads as a failure.
SHELL_LAUNCH_SECONDS = 6
CLIENT_LAUNCH_SECONDS = 90
STOP_WAIT_SECONDS = 5
LAUNCHER_LOOKUP_SECONDS = 5
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CLIENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def viewer_tail(conversation_id: str, endpoint: str) -> list[str]:
    """The exact trailing arguments of the terminal client this host launches to show one shared-server Codex thread."""
    return ["resume", conversation_id, "--remote", endpoint]


def _view(agent_id, chosen, state, reason, detail, *, action=None, observed=None, candidate=None, token=None,
          mode=None, auto_reconnect=False) -> dict:
    return {"agent_id": agent_id, "state": state, "reason": reason, "detail": detail, "action": action,
            "mode": mode or (MODE_SELECTED if chosen is not None else MODE_FIRST_LAUNCH),
            "selection": chosen.public() if chosen is not None else None,
            "observed": observed, "candidate": candidate, "token": token, "auto_reconnect": auto_reconnect}


def _same_pane(first: dict, second: dict) -> bool:
    """The same registered pane and the same shell process in it: tmux session id, pane id, shell pid and its start ticks."""
    return (first["state"] == second["state"] == "present"
            and all(first[key] == second[key] for key in ("session_id", "pane_id", "pane_pid", "shell_started")))


@lru_cache(maxsize=8)
def _codex_remote_resume(identity) -> bool:
    """Whether the Codex build at ``identity`` documents resuming over a ``unix://`` address (``--remote``)."""
    try:
        text = subprocess.run([identity[0], "resume", "--help"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "--remote" in text and "unix://" in text


def effective_executable(client: str, shell_pid: int) -> str:
    """The executable the registered pane's shell will actually run for ``client``, resolved the way that shell resolves it.

    The pane's shell is the prepared agent shell (``bash --rcfile .bazarlyk/agent-shell.bash -i``, whose ``claude`` function
    runs ``command claude`` with the launch settings). Its PATH is whatever its startup files make it, which can differ from
    this service's. So the lookup runs that same shell, with that shell's own environment, working directory and arguments, and
    asks it for the executable (``type -P`` skips functions and aliases, exactly what ``command claude`` runs). Anything that
    cannot be established this way is ``unsupported_runtime``; no other binary is ever substituted."""
    root = codex_session.PROC_ROOT / str(shell_pid)
    unsupported = DomainError("unsupported_runtime", "The terminal's launcher for this client cannot be established")
    try:
        argv = [item.decode("utf-8") for item in (root / "cmdline").read_bytes().split(b"\0") if item]
        environment = dict(item.split("=", 1) for item in (root / "environ").read_bytes().decode("utf-8").split("\0") if "=" in item)
        executable, cwd = os.readlink(root / "exe"), os.readlink(root / "cwd")
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

    def _load(self, agent_id: str):
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
            chosen = selection.selected(uow.connection, agent_id) if profile is not None else None
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

    @staticmethod
    def _endpoint(chosen) -> str | None:
        """The selected server's already-verified local control socket as a ``--remote`` address."""
        try:
            return "unix://" + codex_control.control_socket(chosen.owner)
        except (OSError, TypeError, AttributeError):
            return None

    def _launching(self, agent_id: str, chosen, pane: dict, seconds: float) -> bool:
        record = self._launches.get(agent_id)
        return (record is not None and record[0] == chosen.revision and record[1] == pane.get("pane_id")
                and time.monotonic() - record[2] < seconds)

    def _evaluate(self, profile: dict, chosen) -> dict:
        agent_id = profile["agent_id"]
        observed = self.probe.observe(profile)
        live = observed.observation if observed.reason == REASON_LIVE else None
        relation = selection.relation(chosen, live)
        other = observed.candidate or (live if relation == selection.CANDIDATE else None)
        candidate = ({"provider": other.provider, "conversation_id": other.conversation_id, "transport": other.transport}
                     if other is not None else None)
        if observed.reason in (REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME):
            return _view(agent_id, chosen, UNSUPPORTED, observed.reason, "This agent's native session cannot be attached here")
        if chosen is None:
            return _view(agent_id, None, DISCONNECTED, selection.REASON_NO_SELECTION,
                         "No conversation is selected for this agent yet", action=ACTION_SELECT if candidate else None,
                         candidate=candidate)
        client = chosen.provider
        if client is None or runtime_client(profile) != client:
            return _view(agent_id, chosen, UNSUPPORTED, "runtime_changed",
                         "The agent's runtime no longer matches its selected conversation")
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
        if candidate is not None and not chosen.shared_server:
            return _view(agent_id, chosen, MISMATCH, selection.REASON_CANDIDATE,
                         "Another conversation is running in the terminal; select it to use it, or reconnect the selected one "
                         "after it stops", action=ACTION_SELECT, candidate=candidate)
        state = liveness.of_binding(chosen.binding)
        if state == liveness.UNKNOWN:
            return _view(agent_id, chosen, DISCONNECTED, "unverifiable",
                         "The selected conversation's process cannot be verified right now")
        if chosen.shared_server:
            return self._shared_server(profile, chosen, pane, state, relation, candidate)
        return self._terminal_owned(agent_id, chosen, pane, state, live, relation)

    def _terminal_owned(self, agent_id, chosen, pane, state, live, relation) -> dict:
        """Claude Code and a foreground Codex: attached only when the selected process is the pane's foreground client."""
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

    def _shared_server(self, profile, chosen, pane, state, relation, candidate) -> dict:
        """Compare the native TUI's current displayed ID with the durable selection.

        The title survives narrow panes and approval dialogs. It is recomputed by
        Codex on thread navigation, whereas argv only records the initial resume.
        A shortened title is resolved against all loaded server IDs, with ambiguity
        refused. No transcript scraping, extra agent, prompt, or thread creation.
        """
        agent_id = profile["agent_id"]
        if state == liveness.DEAD:
            if relation == selection.PENDING:
                return _view(agent_id, chosen, RECONNECTING, selection.REASON_PENDING, "The resumed conversation is registering")
            return self._stopped(agent_id, chosen, pane)
        endpoint = self._endpoint(chosen)
        if relation != selection.HELD or endpoint is None:
            return _view(agent_id, chosen, DISCONNECTED, "unverifiable", "The Codex server and its thread cannot be verified right now")
        if pane["state"] == "absent" or pane["kind"] == "shell":
            if pane["state"] == "present" and self._launching(agent_id, chosen, pane, SHELL_LAUNCH_SECONDS):
                return _view(agent_id, chosen, RECONNECTING, "launching", "Opening the selected conversation")
            return _view(agent_id, chosen, DISCONNECTED, "viewer_not_attached",
                         "The conversation is running; reconnect to open it in Terminal", action=ACTION_RECONNECT,
                         auto_reconnect=chosen.desired_state == "running")
        if pane["kind"] == "client":
            found = pane["client"]
            configured = codex_viewer.configured(found["argv"], endpoint, profile["workspace"])
            displayed = codex_viewer.displayed_thread(chosen.owner, pane.get("title")) if configured else None
            if displayed == chosen.conversation_id:
                # The bounded server read must not authorize a pane that changed while it ran.
                current = self._backend().pane_observation(profile, "codex")
                if (not _same_pane(pane, current) or current.get("kind") != "client"
                        or current.get("client") != found or current.get("title") != pane.get("title")):
                    return _view(agent_id, chosen, DISCONNECTED, "terminal_changed", "The terminal changed; reconnect")
                observed = {"client": "codex", "pane_id": pane["pane_id"], "pid": found["pid"], "started": found["started"],
                            "transport": "codex_queue", "conversation_id": displayed}
                return _view(agent_id, chosen, ATTACHED, "verified", "Codex is showing the selected conversation",
                             observed=observed, token=self._token(agent_id, chosen, observed))
            if displayed is not None or candidate is not None:
                selectable = candidate if candidate and candidate["conversation_id"] == displayed else None
                return _view(agent_id, chosen, MISMATCH, selection.REASON_CANDIDATE,
                             "The terminal is showing another conversation",
                             candidate=selectable, action=ACTION_SELECT if selectable else None)
            if configured and self._launching(agent_id, chosen, pane, CLIENT_LAUNCH_SECONDS):
                return _view(agent_id, chosen, RECONNECTING, "launching",
                             "Waiting for Codex to display the selected conversation")
            return _view(agent_id, chosen, UNSUPPORTED, REASON_VIEWER_UNVERIFIED,
                         "This terminal has no readable conversation identity. Exit its client in Diagnostic terminal, then reconnect")
        return _view(agent_id, chosen, MISMATCH, "other_program", "Another program is running in this agent's terminal")

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
        refused; nothing falls back to a fresh conversation. A shared Codex viewer must publish its current native identity
        before its output or input is attached."""
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
        """The one exact resume command, after checking the capability of the executable the pane's shell will really run."""
        client, conversation = chosen.provider, chosen.conversation_id
        if not UUID.fullmatch(conversation):
            raise DomainError("invalid_transition", "The selected conversation id is not resumable")
        executable = effective_executable(client, shell_pid)
        if client == "claude":
            if not features.probe(executable).get("resume"):
                raise DomainError("unsupported_runtime", "The installed Claude Code does not document --resume")
            return f"claude --resume {conversation}"
        if chosen.shared_server and liveness.of_binding(chosen.binding) == liveness.ALIVE:
            endpoint = self._endpoint(chosen)
            if endpoint is None:
                raise DomainError("invalid_transition", "The Codex server endpoint cannot be verified right now")
            if not _codex_remote_resume(features.executable_identity(executable)):
                raise DomainError("unsupported_runtime", "The installed Codex does not document resuming over a unix:// endpoint")
            return "codex --config " + shlex.quote(codex_viewer.TITLE_CONFIG) + " " + " ".join(
                shlex.quote(item) for item in viewer_tail(conversation, endpoint))
        # A stopped Codex conversation resumes by its exact id through the normal shared background server.
        return f"codex --config {shlex.quote(codex_viewer.TITLE_CONFIG)} resume {conversation}"

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
