"""Lazy onbash composition of the accepted recorded shell and its input gates."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from uuid import UUID, uuid4

from ..agents.registry import get_row, reserved
from ..agents.workspace import child_directory, same_child
from ..app import Change, Operation
from ..contracts import CoreError
from ..runner.models import RunnerError
from ..runner.shell import ShellManager, ShellSpec
from ..runner.shell_models import absolute_path
from ..storage.database import assert_external_io_allowed
from ..storage.files import directory
from .terminal_models import Recording, TerminalGet, TerminalView

_LOG = logging.getLogger(__name__)
MAX_STREAMS = 16
MAX_AGENT_STREAMS = 4


@dataclass(frozen=True)
class _Target:
    agent_id: UUID
    recovery_epoch: UUID
    binding: tuple


def _owned(fd, *, private=True):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or (private and info.st_mode & 0o077):
        raise CoreError("target_changed", "Terminal directory ownership is unavailable")


def _private_tree(path):
    """Create only the explicit directory chain, rejecting symlink traversal.

    Sync parent entries even on retry: an existing directory can be the result
    of an earlier interrupted mkdir/fsync, not evidence of durable publication.
    """
    with directory(Path("/")) as root:
        fd = os.dup(root)
        try:
            for part in path.parts[1:]:
                with child_directory(fd, part, create=True) as child:
                    os.fsync(fd)
                    next_fd = os.dup(child)
                os.close(fd)
                fd = next_fd
            _owned(fd)
            os.fsync(fd)
        finally:
            os.close(fd)


class Terminals:
    def __init__(self, application):
        self.app, self.db = application, application.db
        self.settings = application.settings.local_terminal
        self._lock = threading.Lock()
        self._cache = None
        self._manager = None
        self._streams = {}
        self._closing = False
        self._stop = threading.Event()
        self._healthy = threading.Event()
        self._thread = None

    def operations(self):
        return (Operation("terminal_get", TerminalGet, False, self.get, TerminalView),)

    def _target(self, tx, agent_id=None):
        row = dict(reserved(tx) if agent_id is None else get_row(tx, agent_id))
        installation = dict(tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone())
        actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?", (row["actor_id"],)).fetchone()
        if actor is None or tuple(actor) != ("agent", row["id"]):
            raise CoreError("target_changed", "Registered agent identity changed")
        target = _Target(UUID(row["id"]), UUID(installation["recovery_epoch"]), tuple(
            row[key] for key in ("actor_id", "kind", "lifecycle", "lifecycle_revision",
                                "creation_operation_id", "provisioning_job_id", "workspace_initialized_at")
        ) + (installation["installation_id"], installation["onbash_agent_id"]))
        return target, row, installation

    def _base(self, target, row, installation):
        status, reason = "unavailable", "not_observed"
        if self.settings is None:
            status, reason = "disabled", "not_configured"
        elif row["kind"] != "host" or installation["onbash_agent_id"] != row["id"]:
            status, reason = "unsupported_target", "local_onbash_only"
        elif row["lifecycle"] not in {"provisioning", "active"}:
            reason = "lifecycle_unavailable"
        elif row["workspace_initialized_at"] is None:
            status, reason = "waiting_workspace", "workspace_not_initialized"
        with self._lock:
            known = self._cache[1].terminal_id if self._cache is not None and self._cache[0].agent_id == target.agent_id else None
        return TerminalView(agent_id=target.agent_id, recovery_epoch=target.recovery_epoch,
                            status=status, reason=reason, terminal_id=known)

    def _cached(self, target, row, installation):
        base = self._base(target, row, installation)
        with self._lock:
            cached = self._cache
        if base.reason != "not_observed" or cached is None:
            return base
        binding, view, observed = cached
        if target != binding:
            return base.model_copy(update={"reason": "target_changed", "terminal_id": view.terminal_id})
        if time.monotonic() - observed > 5:
            recording = view.recording.model_copy(update={"state": "unknown"}) if view.recording else None
            return view.model_copy(update={"status": "unavailable", "reason": "observation_stale", "recording": recording})
        return view

    def get(self, tx, args, context, operation_id):
        # SQL and immutable memory only; operation transactions never probe.
        return Change(self._cached(*self._target(tx, args.agent_id)))

    def _publish(self, target, view):
        with self._lock:
            self._cache = target, view, time.monotonic()
        return view

    def _presentation(self, target, shell):
        saved, probe = shell.saved_capture, shell.capture
        recording = None
        if saved is not None:
            state = saved.health
            if state == "healthy" and not (probe.verified and probe.terminal_active):
                state = "unknown"
            recording = Recording(state=state, local_captured_offset=saved.local_captured_offset,
                                  host_archived_offset=saved.host_archived_offset)
        available = shell.state == "running" and probe.verified and probe.terminal_active
        return TerminalView(agent_id=target.agent_id, recovery_epoch=target.recovery_epoch,
            terminal_id=shell.terminal_id, observed_at=self.db.clock(),
            status="available" if available else "stopped" if shell.state == "stopped" else "unavailable",
            reason="verified" if available else "shell_exited" if shell.state == "stopped" else "binding_unconfirmed",
            control=shell.control, recording=recording)

    def _paths(self, target):
        agent = self.app.settings.data_root / "agents" / str(target.agent_id)
        runtime = self.settings.runtime_root / target.agent_id.hex
        return agent, runtime, runtime / "tmux.sock"

    def _configuration(self, target):
        agent, runtime, socket = self._paths(target)
        return dict(database=agent / "runtime/terminal.sqlite3", runtime_root=runtime, socket_path=socket,
                    python_executable=self.settings.python_executable, tmux_executable=self.settings.tmux_executable,
                    shell_executable=self.settings.bash_executable)

    @staticmethod
    def _attempt(database):
        if not database.exists():
            return None
        info = database.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or database.resolve() != database):
            raise CoreError("target_changed", "Terminal store ownership changed")
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='runner_shell_terminals'").fetchone():
                return None
            rows = connection.execute("SELECT terminal_id,spec_json,launch_json FROM runner_shell_terminals").fetchall()
            if len(rows) > 1:
                raise CoreError("target_changed", "Terminal store binding is ambiguous")
            return dict(rows[0]) if rows else None
        finally:
            connection.close()

    def observe(self):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                target, row, installation = self._target(tx)
                job = tx.connection.execute("SELECT recovery_epoch,wait_reason FROM jobs WHERE job_id=?",
                                            (row["provisioning_job_id"],)).fetchone()
            base = self._base(target, row, installation)
            if base.reason != "not_observed":
                return self._publish(target, base)
            try:
                with self.app.workspaces.owned(row, installation) as (fd, guard):
                    configuration = self._configuration(target)
                    for path in configuration.values():
                        absolute_path(path)
                    agent, runtime, _ = self._paths(target)
                    spec = ShellSpec(installation_id=installation["installation_id"], agent_id=target.agent_id,
                        recovery_epoch=target.recovery_epoch, environment_generation=1, home=agent / "runtime/home",
                        workspace=agent / "workspace", path=self.settings.path)
                    # Preflight all paths/executables before any new resource.
                    for executable in (self.settings.python_executable, self.settings.tmux_executable,
                                       self.settings.bash_executable):
                        if not executable.is_file() or not os.access(executable, os.X_OK):
                            raise CoreError("not_ready", "Terminal executable is unavailable")
                    if self.settings.bash_executable.resolve().name != "bash":
                        raise CoreError("invalid_argument", "Terminal requires Bash")
                    with child_directory(fd, "workspace") as workspace, child_directory(fd, "runtime") as local:
                        _owned(workspace, private=False)
                        _owned(local)
                        prior = self._attempt(configuration["database"])
                        if prior is not None:
                            base = base.model_copy(update={"terminal_id": UUID(prior["terminal_id"])})
                            if (ShellSpec.model_validate_json(prior["spec_json"]) != spec or
                                    json.loads(prior["launch_json"]) != {k: str(v) for k, v in configuration.items()}):
                                raise CoreError("target_changed", "Terminal attempt is bound differently")
                        if job is None or job["recovery_epoch"] != installation["recovery_epoch"] or job["wait_reason"] == "recovery_review":
                            raise CoreError("target_changed", "Terminal setup is held for recovery")
                        with child_directory(local, "home", create=True) as home:
                            _owned(home)
                            os.fsync(home)
                            os.fsync(local)
                            _private_tree(self.settings.runtime_root)
                            _private_tree(runtime)
                            guard()
                            same_child(fd, "workspace", workspace)
                            same_child(fd, "runtime", local)
                            same_child(local, "home", home)
                            if self._manager is None:
                                self._manager = ShellManager(**configuration)
                            shell = self._manager.ensure(spec)
                            # Shell creates native/XDG directories. Sync their
                            # parent entries, also when recovering a prior attempt.
                            for suffix in (".local/share", ".local", ".codex", ".claude", ".config", ".cache"):
                                with directory(spec.home / suffix) as child:
                                    os.fsync(child)
                            os.fsync(home)
                            os.fsync(local)
                            guard()
                    return self._publish(target, self._presentation(target, shell))
            except (CoreError, RunnerError, OSError, ValueError, sqlite3.Error) as exc:
                code = exc.error.code if isinstance(exc, CoreError) else exc.code if isinstance(exc, RunnerError) else "unavailable"
                reason = "target_changed" if code in {"target_changed", "idempotency_conflict"} else "terminal_unavailable"
                return self._publish(target, base.model_copy(update={"observed_at": self.db.clock(), "reason": reason}))

    @property
    def ready(self):
        return self._thread is not None and self._thread.is_alive() and self._healthy.is_set() and not self._stop.is_set()

    def start(self):
        with self.db.lock.use(), self._lock:
            if self._closing or self._thread is not None:
                raise CoreError("invalid_transition", "Terminal observation cannot be restarted")
            self._thread = threading.Thread(target=self._run, name="qorqut-terminals", daemon=False)
            self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    self.observe()
                except CoreError:
                    # A domain/maintenance hold is not observer death. Existing
                    # cache expires and read/effect admission still revalidates.
                    pass
                self._healthy.set()
                self._stop.wait(1)
        except BaseException:
            _LOG.error("terminal_observation_stopped")
        finally:
            self._healthy.clear()

    @contextmanager
    def _binding(self, target, actor_id):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self._lock:
                if self._closing:
                    raise CoreError("unavailable", "Terminal service is stopping")
            with self.db.read() as tx:
                current, row, installation = self._target(tx, target.agent_id)
                if tx.connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (str(actor_id),)).fetchone() is None:
                    raise CoreError("not_found", "Calling identity is unavailable")
            if current != target or self._base(current, row, installation).reason != "not_observed":
                raise CoreError("target_changed", "Terminal target changed")
            with self.app.workspaces.owned(row, installation) as (_, guard):
                guard()
                yield

    def attach(self, agent_id, actor_id):
        # Reserve capacity before any attachment effect. This is also used by
        # trusted local callers; the transport cannot choose a viewer identity.
        with self.db.read() as tx:
            target, row, installation = self._target(tx, agent_id)
            view = self._cached(target, row, installation)
        with self._lock:
            if self._closing or len(self._streams) >= MAX_STREAMS or sum(
                    agent == target.agent_id for agent, _ in self._streams.values()) >= MAX_AGENT_STREAMS:
                raise CoreError("unavailable", "Terminal stream capacity unavailable")
            if view.status != "available" or self._manager is None:
                raise CoreError("not_ready", "Terminal is unavailable")
            viewer = uuid4()
            self._streams[viewer] = (target.agent_id, None)
        attachment = None
        try:
            with self._binding(target, actor_id):
                fresh = self._manager.inspect(view.terminal_id)
                if fresh.spec.agent_id != target.agent_id or fresh.spec.recovery_epoch != target.recovery_epoch or fresh.state != "running":
                    raise CoreError("target_changed", "Terminal binding is unavailable")
                attachment = self._manager.attach(view.terminal_id, viewer, read_only=False)
                stream = TerminalStream(self, target, actor_id, attachment)
                stream.initial = self._publish(target, self._presentation(target, fresh))
                with self._lock:
                    self._streams[viewer] = (target.agent_id, stream)
                return stream
        except BaseException:
            if attachment is not None:
                attachment.close()
            with self._lock:
                self._streams.pop(viewer, None)
            raise

    def close(self):
        with self._lock:
            self._closing = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        # Wait for admitted creation/effects before capturing the owned viewers.
        with self.db.lock.effects:
            with self._lock:
                streams = [stream for _, stream in self._streams.values() if stream is not None]
        for stream in streams:
            stream.close()


class TerminalStream:
    def __init__(self, service, target, actor_id, attachment):
        self.service, self.target, self.actor_id = service, target, actor_id
        self.attachment = attachment
        self.viewer_id, self.terminal_id = attachment.viewer_id, attachment.terminal_id
        self.initial = None
        self._lock = threading.RLock()

    def state(self):
        with self.service.db.read() as tx:
            target, row, installation = self.service._target(tx, self.target.agent_id)
            view = self.service._cached(target, row, installation)
            if tx.connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (str(self.actor_id),)).fetchone() is None:
                raise CoreError("target_changed", "Calling identity changed")
        if target != self.target or view.terminal_id != self.terminal_id:
            raise CoreError("target_changed", "Terminal target changed")
        return view

    def read(self):
        with self._lock:
            return self.attachment.read(), self.attachment.closed

    def control(self, action, expected_epoch):
        if action not in {"take", "release"}:
            raise CoreError("invalid_argument", "Unknown Terminal control action")
        with self._lock, self.service._binding(self.target, self.actor_id):
            if self.attachment.closed:
                raise CoreError("not_ready", "Terminal viewer is closed")
            manager = self.service._manager
            shell = manager.inspect(self.terminal_id)
            if shell.state != "running" or not shell.capture.verified or not shell.capture.terminal_active:
                raise CoreError("not_ready", "Exact live terminal is unavailable")
            method = manager.take_control if action == "take" else manager.release_control
            control = method(self.terminal_id, self.viewer_id, expected_epoch)
            self.service._publish(self.target, self.service._presentation(self.target, shell.model_copy(update={"control": control})))
            return control

    def input(self, data, *, epoch, sequence):
        with self._lock, self.service._binding(self.target, self.actor_id):
            return self.attachment.write(data, epoch=epoch, sequence=sequence)

    def resize(self, columns, rows, *, epoch, sequence):
        with self._lock, self.service._binding(self.target, self.actor_id):
            return self.attachment.resize(columns, rows, epoch=epoch, sequence=sequence)

    def close(self):
        with self._lock:
            self.attachment.close()
        with self.service._lock:
            self.service._streams.pop(self.viewer_id, None)
