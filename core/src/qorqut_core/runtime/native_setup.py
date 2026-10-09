"""Passive, monitor-driven preparation for the reserved agent's recorded shell."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from uuid import UUID

from ..adapters.managed_io import ManagedTree
from ..adapters.manual_core import prepare_manual_browser, prepare_manual_core
from ..adapters.manual_hooks import ManualBinding, prepare_manual_hooks
from ..adapters.types import AdapterError, ManagedRoots, MCPBinding
from ..agents.registry import reserved
from ..agents.workspace import child_directory, same_child
from ..contracts import CoreError, Invocation
from ..runner.models import RunnerError
from ..storage.database import assert_external_io_allowed
from .native_models import NativeSetupView
from .native_startup import NativeStartup
from .terminal_models import TerminalGet

def _private(fd):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AdapterError("Native setup requires private owned runtime files")


class NativeSetup:
    def __init__(self, application):
        self.app, self.db = application, application.db
        self._lock = threading.Lock()
        self._cache = None
        self._installed = None
        self._startup = NativeStartup(application)

    def _publish(self, agent_id, state, reason):
        view = NativeSetupView(state=state, reason=reason, observed_at=self.db.clock())
        with self._lock:
            self._cache = UUID(str(agent_id)), view, time.monotonic()

    def view(self, agent_id):
        # Called within runtime_get's transaction: memory and time only.
        with self._lock:
            cached = self._cache
        if cached is None:
            settings = self.app.settings.local_terminal
            if settings is None or not settings.native_programs:
                return NativeSetupView()
            return NativeSetupView(state="unknown", reason="observation_stale")
        host, view, observed = cached
        if UUID(str(agent_id)) != host:
            return NativeSetupView(state="disabled", reason="unsupported_target", observed_at=view.observed_at)
        if time.monotonic() - observed > 5:
            return view.model_copy(update={"state": "unknown", "reason": "observation_stale"})
        return view

    def tick(self):
        assert_external_io_allowed()
        agent_id = None
        try:
            with self.db.lock.use(), self.db.lock.effects:
                with self.db.read() as tx:
                    row = dict(reserved(tx))
                    agent_id = UUID(row["id"])
                    installation = dict(tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone())
                    job = tx.connection.execute("SELECT recovery_epoch,wait_reason FROM jobs WHERE job_id=?",
                                                (row["provisioning_job_id"],)).fetchone()
                    terminal = self.app.terminals.get(tx, TerminalGet(agent_id=agent_id), None, None).data
                    attachment = self.app.browsers.attachment(tx, agent_id)
                    if attachment and attachment["recovery_epoch"] != installation["recovery_epoch"]:
                        self._publish(agent_id, "blocked", "recovery_review")
                        return
                settings = self.app.settings.local_terminal
                if settings is None or not settings.native_programs:
                    self._publish(agent_id, "disabled", "not_configured")
                    return
                if installation["installation_id"] != str(self.app.settings.installation_id):
                    raise AdapterError("Native setup installation binding changed")
                if str(self.app.settings.listen_address) != "127.0.0.1":
                    raise AdapterError("Native setup requires the explicit loopback endpoint")
                if job is not None and (job["recovery_epoch"] != installation["recovery_epoch"]
                                        or job["wait_reason"] == "recovery_review"):
                    self._publish(agent_id, "blocked", "recovery_review")
                    return
                if job is None or row["lifecycle"] not in {"provisioning", "active"}:
                    raise AdapterError("Native setup agent binding is unavailable")
                if row["workspace_initialized_at"] is None or terminal.status != "available" or terminal.terminal_id is None:
                    self._publish(agent_id, "waiting", "waiting_terminal")
                    return
                shell = self.app.terminals.native_context(agent_id, terminal.terminal_id)
                binding = ManualBinding(agent_id=agent_id, conversation_id=row["conversation_id"],
                    terminal_id=shell.terminal_id, recovery_epoch=shell.spec.recovery_epoch,
                    environment_generation=shell.spec.environment_generation)
                roots = ManagedRoots(home=shell.spec.home, workspace=shell.spec.workspace)
                with self.app.workspaces.owned(row, installation) as (agent, guard), \
                        child_directory(agent, "runtime") as runtime, child_directory(runtime, "home") as home, \
                        child_directory(agent, "workspace") as workspace, ManagedTree(roots.home.parent) as tree:
                    _private(runtime)
                    _private(home)

                    def checked():
                        guard()
                        same_child(agent, "runtime", runtime)
                        same_child(runtime, "home", home)
                        same_child(agent, "workspace", workspace)
                        with ManagedTree(roots.home) as opened_home, ManagedTree(roots.workspace) as opened_workspace:
                            identities = tuple((os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                                               for fd in (runtime, home, workspace))
                            if (tree.identity, opened_home.identity, opened_workspace.identity) != identities:
                                raise AdapterError("Native setup directories changed")
                        with self.db.read() as tx:
                            current = reserved(tx)
                            current_job = tx.connection.execute("SELECT recovery_epoch,wait_reason FROM jobs WHERE job_id=?",
                                (current["provisioning_job_id"],)).fetchone()
                            if (current["actor_id"] != row["actor_id"] or current["conversation_id"] != row["conversation_id"]
                                    or current_job is None or tuple(current_job) != tuple(job)):
                                raise AdapterError("Native setup binding changed")
                            current_browser = self.app.browsers.attachment(tx, agent_id)
                            if (current_browser and current_browser["attachment_id"]) != (attachment and attachment["attachment_id"]):
                                raise AdapterError("Native browser attachment changed")
                        return identities

                    identities = checked()
                    key = (binding, row["actor_id"], settings, self.app.settings.listen_address,
                           self.app.settings.listen_port, identities, attachment and attachment["attachment_id"])
                    with self._lock:
                        installed = self._installed
                    if key != installed:
                        core = MCPBinding(url=f"http://127.0.0.1:{self.app.settings.listen_port}/mcp",
                            headers=(("X-Qorqut-Actor", row["actor_id"]),))
                        prepare_manual_core(roots, binding, core, settings.native_programs)
                        checked()
                        prepare_manual_hooks(roots, binding, core, settings.native_programs,
                            (str(settings.python_executable), "-I", "-m", "qorqut_core.runtime.hook_client"))
                        checked()
                        if attachment:
                            browser = MCPBinding(url=attachment["service_url"] + "/mcp",
                                headers=(("X-Qorqut-Actor", row["actor_id"]),))
                            prepare_manual_browser(roots, binding, browser, settings.native_programs)
                            checked()
                        with self._lock:
                            self._installed = key
                    self._startup.observe(Invocation(actor_id=row["actor_id"]), terminal=shell,
                                          programs=settings.native_programs, guard=checked)
                self._publish(agent_id, "ready", "configuration_installed")
        except (CoreError, RunnerError, OSError, ValueError, sqlite3.Error):
            # Fixed observations only: no tokens, native settings, paths or
            # arbitrary filesystem/validation diagnostics escape.
            if agent_id is not None:
                self._publish(agent_id, "blocked", "setup_unavailable")
            else:
                with self._lock:
                    self._cache = None

    def close(self):
        with self._lock:
            self._cache = self._installed = None
