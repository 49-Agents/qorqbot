"""Local manual native admission; hooks are observations, never input readiness."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import JsonValue, model_validator

from ..adapters.capabilities import require_profile
from ..adapters.hooks import ACTIVITY
from ..adapters.manual_hooks import ManualBinding, NativeProgram, binding_relative, read_hook_configuration
from ..agents.registry import get_row
from ..app import _check_actor
from ..contracts import CoreError, Model, Revision, canonical
from ..runner.models import ProcessFingerprint
from ..runner.process import fingerprint, verify_process
from ..runner.shell_models import ShellView, absolute_path
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from .models import OwnerCandidate, SessionConflict
from .owners import _active, _binding

MAX_HOOK_BYTES = 1024 * 1024
CONFLICT_MESSAGE = str(SessionConflict())
UNAVAILABLE_MESSAGE = "Could not connect to this agent's inbox."
_RESULTS = {
    "connected": ("Connected to this agent's inbox.", "session_connected"),
    "reconnected": ("Reconnected to this agent's inbox.", "session_reconnected"),
    "observed": ("Native session observed.", "session_observed"),
    "conflict": (CONFLICT_MESSAGE, "session_conflict"),
    "unavailable": (UNAVAILABLE_MESSAGE, "connection_unavailable"),
}


class NativeHookRequest(Model):
    provider: Literal["claude", "codex"]
    agent_id: UUID
    terminal_id: UUID
    hook_process: ProcessFingerprint
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def bounded(self):
        if len(canonical(self).encode("utf-8")) > MAX_HOOK_BYTES:
            raise ValueError("Native hook request exceeds 1 MiB")
        return self


class NativeHookResult(Model):
    outcome: Literal["connected", "reconnected", "observed", "conflict", "unavailable"]
    message: str
    reason: str
    owner_id: UUID | None = None
    run_generation: Revision | None = None

    @model_validator(mode="after")
    def fixed(self):
        if (self.message, self.reason) != _RESULTS[self.outcome]:
            raise ValueError("Invalid native hook diagnostic")
        if (self.owner_id is None) != (self.run_generation is None):
            raise ValueError("Owner identity and generation must be paired")
        if self.outcome in {"connected", "reconnected", "observed"} and self.owner_id is None:
            raise ValueError("Accepted observations require an owner")
        return self

    @classmethod
    def result(cls, outcome, owner_id=None, run_generation=None):
        message, reason = _RESULTS[outcome]
        return cls(outcome=outcome, message=message, reason=reason,
                   owner_id=owner_id, run_generation=run_generation)


def _node(process):
    verify_process(process)
    root = Path("/proc") / str(process.pid)
    raw = (root / "stat").read_text()
    parent = int(raw[raw.rindex(")") + 2:].split()[1])
    executable = os.readlink(root / "exe")
    verify_process(process)
    return parent, executable


def _native_process(hook, shell, program, configured_programs):
    """At most 32 parents, with a second pass over exact start/parent/exe facts."""
    assert_external_io_allowed()
    expected_executable = str(program.executable.resolve(strict=True))
    configured_executables = {str(item.executable.resolve(strict=True)) for item in configured_programs}
    chain, selected, seen = [], [], set()
    process = hook
    for depth in range(33):
        if process.pid in seen:
            raise ValueError("Invalid native ancestry")
        seen.add(process.pid)
        parent, executable = _node(process)
        chain.append((process, parent, executable))
        if process == shell:
            break
        if process.boot_id != shell.boot_id or process.uid != shell.uid or process.pid_namespace != shell.pid_namespace:
            raise ValueError("Native process domain differs from shell")
        if executable in configured_executables and process != hook:
            selected.append((process, executable))
        if depth == 32:
            raise ValueError("Live shell is outside bounded ancestry")
        process = fingerprint(parent)
    else:
        raise ValueError("Live shell is outside bounded ancestry")
    if len(selected) != 1 or selected[0][1] != expected_executable:
        raise ValueError("No unique root native executable in shell ancestry")
    for process, parent, executable in reversed(chain):
        if _node(process) != (parent, executable):
            raise ValueError("Native ancestry changed")
    return selected[0][0]


def _hook_identity(payload, provider, home):
    if (any(payload.get(key) for key in ("agent_id", "parent_session_id", "parent_thread_id", "isSidechain"))
            or any(key in payload for key in ("run_id", "terminal_id", "conversation_id", "installation_id",
                                              "recovery_epoch", "environment_generation", "run_generation"))):
        raise ValueError("Native child or platform identity is not admitted")
    event = payload.get("hook_event_name")
    supported = set(ACTIVITY) - ({"Interrupt"} if provider == "claude" else {"PostToolUseFailure"})
    if event not in supported:
        raise ValueError("Unsupported native hook event")
    session = UUID(payload["session_id"])
    path = absolute_path(payload["transcript_path"])
    store = home / (".claude/projects" if provider == "claude" else ".codex/sessions")
    # Missing fresh Claude files are allowed. Existing symlink escapes are not.
    path.relative_to(store)
    path.resolve().relative_to(store.resolve())
    if store.resolve() != store or path == store:
        raise ValueError("Native transcript is outside managed HOME")
    return event, session, path


class NativeHooks:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _target(self, context, request):
        with self.db.read() as tx:
            _check_actor(tx, context)
            agent = dict(self.app.session_owners._agent(tx, request.agent_id, admission=True))
            installation = dict(tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone())
        return agent, installation

    @staticmethod
    def _same_target(before, after):
        agent, installation = before
        current, installed = after
        return (all(agent[k] == current[k] for k in ("id", "actor_id", "conversation_id", "lifecycle",
                    "lifecycle_revision", "creation_operation_id", "provisioning_job_id", "workspace_initialized_at"))
                and installation == installed)

    def _event(self, context, request, result, *, metadata=None):
        with self.db.transaction() as tx:
            _check_actor(tx, context)
            agent = get_row(tx, request.agent_id)
            actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?", (agent["actor_id"],)).fetchone()
            if actor is None or tuple(actor) != ("agent", agent["id"]):
                raise CoreError("target_changed", "Registered agent identity changed")
            append_event(tx, EventInput(kind="native.hook", actor_id=agent["actor_id"], agent_id=agent["id"],
                payload={"provider": request.provider, "terminal_id": str(request.terminal_id),
                         "hook_process": request.hook_process.model_dump(mode="json"),
                         "payload_sha256": hashlib.sha256(canonical(request.payload).encode()).hexdigest(),
                         **result.model_dump(mode="json"), **(metadata or {})}))

    def receive(self, context, request: NativeHookRequest) -> NativeHookResult:
        """Internal transport composition; no caller-selected config or program path."""
        try:
            request = NativeHookRequest.model_validate(request)
            assert_external_io_allowed()
            with self.db.lock.use(), self.db.lock.effects:
                before = self._target(context, request)
                agent, installation = before
                workspace = self.app.settings.data_root / "agents" / agent["id"] / "workspace"
                with self.app.workspaces.owned(agent, installation) as (_, guard):
                    config = read_hook_configuration(workspace / binding_relative(request.provider))
                    configurations = [(request.provider, config)]
                    peer_provider = "codex" if request.provider == "claude" else "claude"
                    try:
                        peer = read_hook_configuration(workspace / binding_relative(peer_provider))
                    except FileNotFoundError:
                        pass  # Explicit single-provider setup has no peer binding.
                    else:
                        configurations.append((peer_provider, peer))
                    terminal = self.app.terminals.native_context(request.agent_id, request.terminal_id)
                    expected = ManualBinding(agent_id=request.agent_id, conversation_id=agent["conversation_id"],
                        terminal_id=request.terminal_id, recovery_epoch=installation["recovery_epoch"],
                        environment_generation=terminal.spec.environment_generation)
                    for provider, configured in configurations:
                        if (configured.binding != expected or configured.program.provider != provider
                                or configured.roots.workspace != workspace
                                or configured.roots.workspace != terminal.spec.workspace
                                or configured.roots.home != terminal.spec.home):
                            raise ValueError("Manual hook configuration differs from current terminal")
                    guard()
                    if not self._same_target(before, self._target(context, request)):
                        raise ValueError("Native target changed")
                    return self.handle(context, request, terminal=terminal, program=config.program,
                                       configured_programs=tuple(item.program for _, item in configurations))
        except Exception:
            # Files/validation/observer errors may contain secrets. Never expose
            # their text or misclassify an unavailable bridge as a live conflict.
            return self._unavailable(context, request)

    def _unavailable(self, context, request):
        result = NativeHookResult.result("unavailable")
        if isinstance(request, NativeHookRequest):
            try:
                with self.db.lock.use():
                    self._event(context, request, result)
            except Exception:
                pass  # Storage failure is itself unavailable; never kill a CLI.
        return result

    def handle(self, context, request: NativeHookRequest, *, terminal: ShellView,
               program: NativeProgram, configured_programs: tuple[NativeProgram, ...]) -> NativeHookResult:
        try:
            request = NativeHookRequest.model_validate(request)
            terminal, program = ShellView.model_validate(terminal), NativeProgram.model_validate(program)
            configured_programs = tuple(NativeProgram.model_validate(item) for item in configured_programs)
            if program not in configured_programs:
                raise ValueError("Selected native program is not configured")
            assert_external_io_allowed()
            with self.db.lock.use(), self.db.lock.effects:
                before = self._target(context, request)
                agent, installation = before
                spec = terminal.spec
                root = self.app.settings.data_root / "agents" / agent["id"]
                if (request.provider != program.provider or terminal.terminal_id != request.terminal_id
                        or spec.agent_id != request.agent_id or terminal.state != "running" or terminal.shell is None
                        or str(spec.installation_id) != installation["installation_id"]
                        or str(spec.recovery_epoch) != installation["recovery_epoch"]
                        or spec.home != root / "runtime/home" or spec.workspace != root / "workspace"):
                    raise ValueError("Native shell binding differs from registered target")
                for configured in configured_programs:
                    require_profile(configured.provider, configured.cli_version)
                event, session, path = _hook_identity(request.payload, program.provider, spec.home)
                native = _native_process(request.hook_process, terminal.shell, program, configured_programs)
                candidate = OwnerCandidate(agent_id=request.agent_id, terminal_id=request.terminal_id,
                    recovery_epoch=spec.recovery_epoch, environment_generation=spec.environment_generation,
                    provider=program.provider, cli_version=program.cli_version, native_session_id=session, process=native)
                if not self._same_target(before, self._target(context, request)):
                    raise ValueError("Native target changed during inspection")
                verify_process(terminal.shell)
                verify_process(native)
                if event == "SessionStart":
                    try:
                        owner = self.app.session_owners.connect(candidate)
                        result = NativeHookResult.result(owner.outcome, owner.owner_id, owner.run_generation)
                    except SessionConflict:
                        result = NativeHookResult.result("conflict")
                else:
                    with self.db.read() as tx:
                        owner = _active(tx, request.agent_id)
                        if owner is None or any(owner[k] != v for k, v in _binding(candidate).items()):
                            raise ValueError("Hook has no matching incumbent")
                    result = NativeHookResult.result("observed", owner["owner_id"], owner["run_generation"])
                self._event(context, request, result, metadata={"event": event,
                    "activity": ACTIVITY[event] if result.outcome != "conflict" else "unknown",
                    "native_session_id": str(session), "transcript_path": str(path),
                    "native_process": native.model_dump(mode="json"), "cli_version": program.cli_version,
                    "recovery_epoch": str(spec.recovery_epoch), "environment_generation": spec.environment_generation})
                return result
        except Exception:
            return self._unavailable(context, request)
