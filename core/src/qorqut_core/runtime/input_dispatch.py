"""D46 local input composition. Observation is specific to one saved attempt."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import shlex
import sqlite3
import stat
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from ..adapters.input_layout import classify_input, matches_input
from ..adapters.managed_io import ManagedTree
from ..adapters.manual_hooks import ManualBinding, binding_relative, read_hook_configuration
from ..adapters.preparation import EVENTS
from ..contracts import CoreError, Counter, Model, Revision, canonical
from ..messaging.notifications import notification_text
from ..runner.models import AttemptReceipt, ProcessFingerprint, RunBinding, RunnerError, TmuxBinding
from ..runner.shell import ShellManager, _program
from ..runner.shell_models import ShellSpec
from ..runner.storage import input_lock
from ..runner.terminal import TerminalRunner
from ..runner.tmux import Tmux
from ..storage.database import assert_external_io_allowed
from ..transcripts.spool import RunBinding as CaptureBinding
from .native_hooks import _native_process
from .owners import _active

InputReason = Literal[
    "runtime_unavailable", "unsupported_target", "session_unavailable", "hook_unavailable",
    "native_busy", "native_waiting_user", "human_control", "capture_unavailable",
    "composer_busy", "composer_blocked", "composer_unknown", "target_changed",
]


class InputUnavailable(CoreError):
    def __init__(self, reason: InputReason):
        self.reason = reason
        super().__init__("not_ready", "Message input is unavailable", retryable=True)


class InputTarget(Model):
    installation_id: UUID
    conversation_id: UUID
    terminal_id: UUID
    native_session_id: UUID
    provider: Literal["claude", "codex"]
    cli_version: Annotated[str, Field(min_length=1, max_length=100)]
    idle_event_seq: Revision
    control_epoch: Counter
    binding: RunBinding


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class RuntimeInput:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _host(self, agent_id, *, historical=False):
        with self.db.read() as tx:
            selected, agent, installation = self.app.terminals._target(tx, agent_id)
            job = tx.connection.execute("SELECT recovery_epoch,wait_reason FROM jobs WHERE job_id=?",
                                         (agent["provisioning_job_id"],)).fetchone()
        if installation["installation_id"] != str(self.app.settings.installation_id):
            raise InputUnavailable("target_changed")
        settings = self.app.settings.local_terminal
        if settings is None:
            raise InputUnavailable("runtime_unavailable")
        if agent["kind"] != "host":
            raise InputUnavailable("unsupported_target")
        if not historical:
            if not settings.native_programs:
                raise InputUnavailable("runtime_unavailable")
            if (agent["workspace_initialized_at"] is None or agent["lifecycle"] not in {"active", "provisioning"}
                    or job is None or job["recovery_epoch"] != installation["recovery_epoch"]
                    or job["wait_reason"] == "recovery_review"):
                raise InputUnavailable("session_unavailable")
        return selected, agent, installation

    @staticmethod
    def _file_identity(database):
        info = database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or database.resolve() != database):
            raise InputUnavailable("target_changed")
        return info.st_dev, info.st_ino

    @staticmethod
    @contextmanager
    def _read_store(database):
        # No create-if-missing path, including receipt absence checks.
        db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _owned(self, agent_id, *, saved=None, historical=False):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            selected, agent, installation = self._host(agent_id, historical=historical)
            configuration = self.app.terminals._configuration(selected)
            database = configuration["database"]
            with self.app.workspaces.owned(agent, installation) as (_, workspace_guard):
                identity = self._file_identity(database)

                def guard():
                    workspace_guard()
                    if self._file_identity(database) != identity:
                        raise InputUnavailable("target_changed")
                    current, current_agent, installed = self._host(agent_id, historical=historical)
                    if (current != selected or current_agent["conversation_id"] != agent["conversation_id"]
                            or installed != installation):
                        raise InputUnavailable("target_changed")

                guard()
                with self._read_store(database) as local:
                    rows = local.execute("SELECT * FROM runner_shell_terminals").fetchall()
                if len(rows) != 1:
                    raise InputUnavailable("session_unavailable")
                row = dict(rows[0])
                spec = ShellSpec.model_validate_json(row["spec_json"])
                root = self.app.settings.data_root / "agents" / agent["id"]
                epoch = saved.binding.recovery_epoch if historical else selected.recovery_epoch
                expected = ShellSpec(installation_id=installation["installation_id"], agent_id=agent_id,
                    recovery_epoch=epoch, environment_generation=1, home=root / "runtime/home",
                    workspace=root / "workspace", path=self.app.settings.local_terminal.path)
                if (spec != expected or row["spec_hash"] != _hash(spec.model_dump_json())
                        or json.loads(row["launch_json"]) != {k: str(v) for k, v in configuration.items()}
                        or row["installation_id"] != str(spec.installation_id) or row["agent_id"] != agent["id"]
                        or row["socket_path"] != str(configuration["socket_path"])
                        or row["runtime_root"] != str(configuration["runtime_root"])
                        or not row["tmux_json"] or not row["shell_json"]):
                    raise InputUnavailable("target_changed")
                if saved is not None:
                    if (saved.installation_id != spec.installation_id
                            or str(saved.conversation_id) != agent["conversation_id"]
                            or str(saved.terminal_id) != row["terminal_id"]
                            or str(saved.binding.boot_id) != row["boot_id"]
                            or saved.binding.environment_generation != spec.environment_generation
                            or saved.binding.recovery_epoch != spec.recovery_epoch
                            or TmuxBinding.model_validate_json(row["tmux_json"]) != saved.binding.tmux):
                        raise InputUnavailable("target_changed")
                yield configuration, row, agent, guard
                guard()

    def _owner(self, agent, row):
        with self.db.read() as tx:
            owner = _active(tx, agent["id"])
        spec = ShellSpec.model_validate_json(row["spec_json"])
        if (owner is None or owner["observation"] != "live"
                or owner["installation_id"] != str(spec.installation_id)
                or owner["recovery_epoch"] != str(spec.recovery_epoch)
                or owner["environment_generation"] != spec.environment_generation
                or owner["conversation_id"] != agent["conversation_id"]
                or owner["terminal_id"] != row["terminal_id"] or owner["boot_id"] != row["boot_id"]
                or owner["connected_by"] != agent["actor_id"]):
            raise InputUnavailable("session_unavailable")
        return owner

    def _configuration(self, shell, owner):
        """Read and compare owned bindings AND their installed native hook entries."""
        expected = ManualBinding(agent_id=shell.spec.agent_id, conversation_id=owner["conversation_id"],
            terminal_id=shell.terminal_id, recovery_epoch=shell.spec.recovery_epoch,
            environment_generation=shell.spec.environment_generation)
        settings = self.app.settings.local_terminal
        programs = settings.native_programs
        selected = next((p for p in programs if p.provider == owner["provider"]), None)
        if selected is None or selected.cli_version != owner["cli_version"]:
            raise InputUnavailable("hook_unavailable")
        try:
            with ManagedTree(shell.spec.workspace) as workspace, ManagedTree(shell.spec.home) as home:
                manifest = json.loads(workspace.read(".qorqut/manual-hooks/preparation.json"))
                identity = {"agent_id": str(shell.spec.agent_id), "conversation_id": owner["conversation_id"],
                            "roots": {"home": str(shell.spec.home), "workspace": str(shell.spec.workspace)}}
                if (manifest.get("schema_version") != 1 or manifest.get("identity") != identity
                        or set(manifest.get("providers", {})) != {p.provider for p in programs}):
                    raise ValueError("binding")
                for program in programs:
                    provider = program.provider
                    relative = binding_relative(provider)
                    path = shell.spec.workspace / relative
                    config = read_hook_configuration(path)
                    if (config.binding != expected or config.program != program
                            or config.core.url != f"http://127.0.0.1:{self.app.settings.listen_port}/mcp"
                            or config.roots.home != shell.spec.home or config.roots.workspace != shell.spec.workspace):
                        raise ValueError("configuration")
                    saved = manifest["providers"][provider]
                    if saved["binding_sha256"] != hashlib.sha256(workspace.read(relative)).hexdigest():
                        raise ValueError("manifest")
                    command = shlex.join((str(settings.python_executable), "-I", "-m",
                        "qorqut_core.runtime.hook_client", "--binding", str(path), "--provider", provider))
                    events = (*EVENTS, "PostToolUseFailure" if provider == "claude" else "Interrupt")
                    hooks = {event: {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
                             for event in events}
                    raw = (workspace.read(".claude/settings.local.json") if provider == "claude"
                           else home.read(".codex/hooks.json"))
                    installed = json.loads(raw)["hooks"]
                    if saved["hooks"] != hooks or any(installed.get(event, []).count(group) != 1
                                                      for event, group in hooks.items()):
                        raise ValueError("hooks")
        except Exception:
            raise InputUnavailable("hook_unavailable") from None
        return selected, programs

    def _idle(self, owner, agent):
        with self.db.read() as tx:
            # Rejected rival hooks do not replace the incumbent's observation.
            # Select its latest accepted event first, then validate ALL provenance;
            # never fall back to an older idle event after a mismatched/busy one.
            row = tx.connection.execute("""SELECT * FROM events WHERE kind='native.hook'
                AND json_extract(payload_json,'$.owner_id')=?
                AND json_extract(payload_json,'$.outcome') IN ('connected','reconnected','observed')
                ORDER BY event_seq DESC LIMIT 1""", (owner["owner_id"],)).fetchone()
            if row is None and (owner["provider"], owner["cli_version"]) == ("codex", "0.160.0"):
                row = tx.connection.execute("""SELECT * FROM events WHERE kind='native.startup_observed'
                    AND json_extract(payload_json,'$.owner_id')=?
                    ORDER BY event_seq DESC LIMIT 1""", (owner["owner_id"],)).fetchone()
        if (row is None or row["actor_id"] != agent["actor_id"] or row["agent_id"] != agent["id"]
                or row["origin_installation_id"] != owner["installation_id"]):
            raise InputUnavailable("hook_unavailable")
        data = json.loads(row["payload_json"])
        if (any(data.get(k) != owner[k] for k in ("terminal_id", "run_generation", "provider", "cli_version",
                "native_session_id", "recovery_epoch", "environment_generation"))
                or canonical(data.get("native_process")) != owner["process_json"]
                or row["recorded_at"] < owner["connected_at"]):
            raise InputUnavailable("hook_unavailable")
        if row["kind"] == "native.startup_observed":
            if data.get("evidence_kind") != "codex_writer_lock" or data.get("agent_id") != agent["id"]:
                raise InputUnavailable("hook_unavailable")
            # Binding evidence only. The fresh classifier below proves empty.
            return row["event_seq"], True
        if data.get("activity") == "working":
            raise InputUnavailable("native_busy")
        if data.get("activity") == "waiting_user":
            raise InputUnavailable("native_waiting_user")
        if data.get("activity") != "idle" or data.get("event") not in {"SessionStart", "Stop", "Interrupt"}:
            raise InputUnavailable("hook_unavailable")
        return row["event_seq"], data["event"] == "SessionStart"

    def _capture(self, shell, owner, database, *, initial):
        saved = shell.saved_capture
        if (not shell.capture.verified or not shell.capture.terminal_active or saved is None
                or saved.health != "healthy" or saved.recorder_finished_at is not None):
            raise InputUnavailable("capture_unavailable")
        with self._read_store(database) as local:
            epoch = local.execute("SELECT * FROM capture_epochs WHERE source_id=? AND source_epoch=?",
                                 (str(shell.source_id), str(shell.source_epoch))).fetchone()
            if (epoch is None or epoch["health"] != "healthy" or epoch["last_error"] is not None
                    or epoch["divergence_reason"] is not None or epoch["recorder_finished_at"] is not None
                    or epoch["recorder_run_id"] != str(shell.terminal_id)
                    or local.execute("SELECT 1 FROM capture_conflicts WHERE source_id=? AND source_epoch=?",
                        (str(shell.source_id), str(shell.source_epoch))).fetchone()):
                raise InputUnavailable("capture_unavailable")
        # D48 owns the memory-only, monotonically fresh raw-source observation.
        # Aggregate parser/coverage warnings remain public and cannot grant or
        # deny this proof. Until that private method exists, refuse input.
        observe = getattr(self.app.native_collector, "input_observation", None)
        view = observe(owner["owner_id"]) if callable(observe) else None
        binding = CaptureBinding(owner["installation_id"], owner["agent_id"], owner["recovery_epoch"],
            owner["environment_generation"], owner["boot_id"], owner["owner_id"], owner["run_generation"])
        if (view is None or view.binding != binding
                or datetime.fromisoformat(view.observed_at) < datetime.fromisoformat(owner["connected_at"])):
            raise InputUnavailable("capture_unavailable")
        if (initial and view.awaiting_primary is True and view.healthy is False and view.source_count == 0):
            return
        if view.healthy is not True:
            raise InputUnavailable("capture_unavailable")
        with self.db.read() as tx:
            sources = tx.connection.execute("""SELECT e.conflicted FROM archive_run_sources l
                JOIN archive_runs r USING(run_id) JOIN archive_sources s USING(source_id)
                JOIN archive_epochs e USING(source_id,source_epoch)
                WHERE r.run_id=? AND r.installation_id=? AND r.agent_id=? AND r.recovery_epoch=?
                    AND r.environment_generation=? AND r.boot_id=? AND r.run_generation=?
                    AND s.installation_id=r.installation_id AND s.agent_id=r.agent_id
                    AND s.source_type='native' AND s.parent_source_id IS NULL AND s.native_session_id=?
                    AND e.registered_recovery_epoch=r.recovery_epoch""",
                tuple(owner[k] for k in ("owner_id", "installation_id", "agent_id", "recovery_epoch",
                    "environment_generation", "boot_id", "run_generation", "native_session_id"))).fetchall()
        if not sources or any(row["conflicted"] for row in sources):
            raise InputUnavailable("capture_unavailable")

    def _observe(self, manager, row, agent, guard):
        # Caller owns the terminal gate. Public inspect/native_context would
        # recursively acquire flock; only this internal observation seam is safe.
        guard()
        current = manager._row(UUID(row["terminal_id"]))
        if current != row:
            raise InputUnavailable("target_changed")
        shell = manager._inspect(current)
        if shell.state != "running" or shell.shell is None or shell.tmux is None or shell.recorder is None:
            raise InputUnavailable("session_unavailable")
        owner = self._owner(agent, row)
        program, programs = self._configuration(shell, owner)
        native = ProcessFingerprint.model_validate_json(owner["process_json"])
        try:
            if _native_process(native, shell.shell, program, programs, include_start=True) != native:
                raise ValueError("native owner")
        except Exception:
            raise InputUnavailable("session_unavailable") from None
        idle = self._idle(owner, agent)
        self._capture(shell, owner, manager.database, initial=idle[1])
        if shell.control.mode != "unclaimed" or shell.control.viewer_id is not None:
            raise InputUnavailable("human_control")
        guard()
        if self._owner(agent, row) != owner or self._idle(owner, agent) != idle:
            raise InputUnavailable("target_changed")
        return InputTarget(installation_id=shell.spec.installation_id, conversation_id=owner["conversation_id"],
            terminal_id=shell.terminal_id, native_session_id=owner["native_session_id"], provider=owner["provider"],
            cli_version=owner["cli_version"], idle_event_seq=idle[0], control_epoch=shell.control.epoch,
            binding=RunBinding(agent_id=shell.spec.agent_id, recovery_epoch=shell.spec.recovery_epoch,
                environment_generation=shell.spec.environment_generation, boot_id=native.boot_id,
                run_id=owner["owner_id"], run_generation=owner["run_generation"], tmux=shell.tmux,
                recorder=shell.recorder, native=native))

    def prepare(self, agent_id) -> InputTarget:
        try:
            with self._owned(agent_id) as (config, row, agent, guard):
                manager = ShellManager(**config, existing=True)
                with input_lock(config["database"], UUID(row["terminal_id"])):
                    target = self._observe(manager, row, agent, guard)
                    verdict = classify_input(target.provider, target.cli_version,
                        Tmux(str(self.app.settings.local_terminal.tmux_executable)).inspect(target.binding))
                    if verdict.state != "empty":
                        raise InputUnavailable({"busy": "composer_busy", "blocked": "composer_blocked"}.get(
                            verdict.state, "composer_unknown"))
                    guard()
                    return target
        except InputUnavailable:
            raise
        except Exception:
            raise InputUnavailable("session_unavailable") from None

    def attention_snapshot(self, agent_id):
        """Read the exact saved shell/owner under its gate, without input proof.

        Do not call _inspect: its recovery path can save/adopt bindings. These
        checks require the already-saved shell and terminal and only inspect.
        Human control, hook activity and composer readiness are immaterial.
        """
        try:
            with self._owned(agent_id) as (config, row, agent, guard):
                manager = ShellManager(**config, existing=True)
                with input_lock(config["database"], UUID(row["terminal_id"])):
                    spec = ShellSpec.model_validate_json(row["spec_json"])
                    shell = ProcessFingerprint.model_validate_json(row["shell_json"])
                    owner = self._owner(agent, row)
                    programs = self.app.settings.local_terminal.native_programs
                    program = next((p for p in programs if p.provider == owner["provider"]
                                    and p.cli_version == owner["cli_version"]), None)
                    if program is None or row["stopped"]:
                        raise InputUnavailable("session_unavailable")
                    native = ProcessFingerprint.model_validate_json(owner["process_json"])

                    def verified():
                        guard()
                        if manager._row(UUID(row["terminal_id"])) != row or self._owner(agent, row) != owner:
                            raise InputUnavailable("target_changed")
                        target, _ = manager._target(row)
                        _program(shell, manager.shell_executable,
                                 [str(manager.shell_executable), "--noprofile", "--norc", "-i"],
                                 parent=target.pane)
                        if _native_process(native, shell, program, programs, include_start=True) != native:
                            raise InputUnavailable("target_changed")
                        return target

                    target = verified()
                    binding = RunBinding(agent_id=spec.agent_id, recovery_epoch=spec.recovery_epoch,
                        environment_generation=spec.environment_generation, boot_id=native.boot_id,
                        run_id=owner["owner_id"], run_generation=owner["run_generation"], tmux=target,
                        recorder=target.pane, native=native)
                    snapshot = Tmux(str(manager.tmux_executable)).inspect(binding)
                    if verified() != target:
                        raise InputUnavailable("target_changed")
                return owner, snapshot
        except InputUnavailable:
            raise
        except Exception:
            raise InputUnavailable("session_unavailable") from None

    def submit(self, target: InputTarget, attempt_id: UUID, message_id: UUID, *, still_current) -> AttemptReceipt:
        target = InputTarget.model_validate(target)
        text = notification_text(message_id)
        digest = hashlib.sha256(text.encode("ascii")).hexdigest()
        with self._owned(target.binding.agent_id, saved=target) as (config, row, agent, guard):
            manager = ShellManager(**config, existing=True)

            def ready(binding):
                assert_external_io_allowed()
                # Mandatory exact boolean; no truthy lease/claim surrogate.
                if binding != target.binding or still_current() is not True:
                    raise RunnerError("target_changed", "Host attempt is no longer current")
                try:
                    current = self._observe(manager, row, agent, guard)
                    if current != target:
                        raise InputUnavailable("target_changed")
                except InputUnavailable as exc:
                    raise RunnerError(exc.reason, "Input proof is unavailable") from None
                return True

            tmux = Tmux(str(self.app.settings.local_terminal.tmux_executable))
            runner = TerminalRunner(config["database"], tmux=tmux, capture_ready=ready,
                classify_composer=lambda binding, snapshot: classify_input(
                    target.provider, target.cli_version, snapshot).state,
                verify_paste=lambda binding, snapshot, supplied: binding == target.binding and supplied == digest
                    and matches_input(target.provider, target.cli_version, snapshot, text),
                # Local-only D46 has no machine takeover. Any future machine
                # hold must join this same gate before it can acknowledge input.
                machine_hold=lambda _: False, paste_settle_timeout=0.5)
            with input_lock(config["database"], target.terminal_id):
                ready(target.binding)
            runner.register_binding(target.binding, terminal_id=target.terminal_id)
            guard()
            return runner.attempt(target.binding, attempt_id, text, expected_epoch=target.control_epoch)

    def inspect(self, target: InputTarget, attempt_id: UUID) -> AttemptReceipt | None:
        target = InputTarget.model_validate(target)
        # No live native/shell/collector proof: historical receipts outlive them.
        with self._owned(target.binding.agent_id, saved=target, historical=True) as (config, row, _, guard):
            runner = TerminalRunner(config["database"],
                tmux=Tmux(str(self.app.settings.local_terminal.tmux_executable)), existing=True)
            with runner._gate(target.binding.run_id, observation=True) as (local, saved):
                guard()
                if (saved is None or saved["terminal_id"] != str(target.terminal_id)
                        or saved["binding_digest"] != target.binding.digest
                        or RunBinding.model_validate_json(saved["binding_json"]) != target.binding):
                    raise InputUnavailable("target_changed")
                receipt = local.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
                if receipt is None:
                    return None
                if (receipt["run_id"] != str(target.binding.run_id)
                        or receipt["binding_digest"] != target.binding.digest):
                    raise InputUnavailable("target_changed")
                result = runner._recover(local, target.binding, receipt)
                guard()
                return result
