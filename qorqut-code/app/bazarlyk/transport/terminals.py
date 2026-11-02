"""Owner-only mirrors of registered Claude workspaces; shutdown never kills tmux."""

import base64
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time

import anyio
from starlette.responses import JSONResponse, StreamingResponse

from bazarlyk.bootstrap import prepare_agent
from bazarlyk.bootstrap.preparation import installed_versions
from bazarlyk.composition.locations import agent_workspace, business_hq
from qorqut_operations.contracts import DomainError, Principal
from bazarlyk.agents.global_scope import active_global
from bazarlyk.management.identity import require_protected_control
from .http import checked_fields, read_json

# Extra owner workspace shells beyond the original slot 1.
WORKSPACE_TERMINAL_SLOTS = ("2", "3")


@dataclass(frozen=True)
class TerminalExecution:
    """Immutable execution target selected for one business terminal request."""

    broker: object
    endpoint: str
    interpreter: str | None
    version_provider: object
    environment_resolver: object
    # Providers should keep this stable for the lifetime of one observed
    # container generation. It lets stream rechecks ignore fresh callables.
    target_identity: object | None = None
    # A container target may validate its interpreter inside the observed
    # environment. Host execution keeps the preparation default check.
    interpreter_validator: object | None = None


class TerminalService:
    def __init__(self, runtime, broker, *, service_url, shell="/bin/bash", environment=None,
                 version_provider=None, preparation=prepare_agent, capture_transcripts=True, global_environment=None,
                 agent_environment=None,
                 workspace=None, execution_provider=None):
        self.runtime, self.broker, self.service_url = runtime, broker, service_url
        self.execution_provider = execution_provider
        self.workspace = Path(workspace) if workspace is not None else Path(runtime.workspace)
        self.shell = [shell, "-i"]
        if Path(shell).name not in {"bash", "sh", "dash", "zsh", "fish"}:
            raise ValueError("Terminals start an interactive shell only")
        self.environment = environment or (lambda business: {})
        self.global_environment = global_environment or (lambda: {})
        self.agent_environment = agent_environment
        self.version_provider = version_provider or installed_versions
        self.preparation = preparation
        if type(capture_transcripts) is not bool:
            raise ValueError("Transcript capture selection must be a boolean")
        self.capture_transcripts = capture_transcripts

    def _host_environment(self, profile, business):
        if profile is not None and self.agent_environment:
            return self.agent_environment(business, profile)
        return self.environment(business) if business else self.global_environment()

    def _host_execution(self):
        return TerminalExecution(self.broker, self.service_url, None, self.version_provider, self._host_environment)

    def execution(self, business):
        """Select the business backend; an absent result retains legacy host execution."""
        if business is None or self.execution_provider is None:
            return self._host_execution()
        selected = self.execution_provider(business)
        if selected is None:
            return self._host_execution()
        if not isinstance(selected, TerminalExecution) or selected.broker is None:
            raise DomainError("operation_unavailable", "The business terminal execution backend is invalid")
        return selected

    @staticmethod
    def _same_execution(bound, selected):
        if bound.target_identity is not None or selected.target_identity is not None:
            return (bound.target_identity is not None
                    and bound.target_identity == selected.target_identity)
        return bound == selected

    @staticmethod
    def _version():
        try:
            return subprocess.run(["claude", "--version"], check=True, capture_output=True,
                text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise DomainError("unsupported_claude", "The installed Claude Code version could not be verified") from None

    def owner(self, context):
        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            current = self.runtime.application.current_context(context, uow)
            if current.business_id is None:
                installation = uow.repository("installation").get("service")
                if current.principal_kind != "human" or not installation or installation["owner_principal_id"] != current.principal_id:
                    raise DomainError("forbidden", "Global terminals require the installation owner")
                return
            membership = uow.repository("business_memberships", business_id=current.business_id).get(current.principal_id)
            if current.principal_kind != "human" or not membership or not membership["active"] or not membership["is_owner"]:
                raise DomainError("forbidden", "Terminal access requires the current business owner")

    def agent(self, context, selector):
        self.owner(context)
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id IS ? AND (agent_id=? OR name=?) AND status='active'", (context.business_id, selector, selector)).fetchone()
            if row is None:
                raise DomainError("not_found", "Registered active agent was not found in this workspace")
            current = self.runtime.application.current_context(context, uow)
            require_protected_control(uow, current.principal_id, current.principal_kind, [row["agent_id"]])
            profile, indexed = self.runtime.resolver.profile(Principal(row["agent_id"], "agent"), connection=uow.connection)
            return profile, indexed

    def binding(self, business, row, broker=None):
        broker = self.broker if broker is None else broker
        # The registered identity and home scope select the workspace; the stored column and the name never do.
        workspace = agent_workspace(self.runtime.paths, row["agent_id"], business)
        if business is None:
            if row["home_business_id"] is not None:
                raise DomainError("registration_conflict", "Global terminal workspace must belong to a Global agent")
            # An extra underscore cannot occur in a business agent's validated name.
            session = broker.session_name("global", "_" + row["agent_id"])
            return str(self.runtime.root) + "\0global\0" + row["agent_id"], session, str(workspace)
        if row["home_business_id"] != business["id"]:
            raise DomainError("registration_conflict", "Terminal workspace is not registered under this business")
        hq = business_hq(self.runtime.paths, business)
        # Session names stay derived from the registered agent name, as before.
        stable_name = row["name"]
        return str(hq) + "\0" + stable_name, broker.session_name(business["slug"], stable_name), str(workspace)

    def workspace_binding(self, context, slot=1):
        """Bind an owner utility shell to the Qorqut workspace; slot 1 keeps the original session."""
        self.owner(context)
        workspace = self.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise DomainError("invalid_configuration", "The Qorqut workspace is unavailable")
        suffix = "" if slot == 1 else f"-{slot}"
        return (str(workspace) + "\0workspace-terminal" + suffix,
                self.broker.session_name("global", "workspace" + suffix), str(workspace))

    @staticmethod
    def size(body):
        cols, rows = body.get("cols", 120), body.get("rows", 32)
        if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 250:
            raise DomainError("validation_error", "Terminal dimensions must be 20-500 columns and 5-250 rows")
        return cols, rows

    def open(self, context, business, selector, *, cols=120, rows=32, execution=None, reconnect=False):
        from contextlib import nullcontext
        admission = self.runtime.application.writer_admission
        with admission.writer() if admission is not None else nullcontext():
            return self._open_admitted(context, business, selector, cols=cols, rows=rows, execution=execution, reconnect=reconnect)

    def _open_admitted(self, context, business, selector, *, cols, rows, execution, reconnect):
        profile, row = self.agent(context, selector)
        execution = execution or self.execution(business)
        broker = execution.broker
        if broker is None or not broker.tmux_available():
            raise DomainError("operation_unavailable", "Registered agent terminals require the tmux mirror backend")
        key, session, cwd = self.binding(business, row, broker)
        # Viewing an existing session is an attach, not a fresh agent launch.
        # Authorization, registration binding and execution selection above still
        # run on every request; preparation remains mandatory for new sessions.
        if reconnect and broker.session_alive(session):
            return broker.get_or_start(key, broker.attach_cmd(session), cwd, cols=cols, rows=rows), profile, row
        self._prepare(execution, profile.agent_id)
        env = dict(execution.environment_resolver(profile, business))
        if execution.target_identity is not None:
            # Bind once to the prepared agent, independently of later code cwd.
            # Settings cannot substitute another agent's registration path.
            env["QORQUT_REGISTRATION"] = str(Path(cwd) / ".bazarlyk/registration.json")
        shell = self.shell
        prepared_rc = Path(cwd) / ".bazarlyk/agent-shell.bash"
        if Path(shell[0]).name == "bash" and prepared_rc.is_file():
            shell = [shell[0], "--rcfile", str(prepared_rc), "-i"]
        broker.ensure_session(session, shell, cwd, env=env, cols=cols, rows=rows)
        return broker.get_or_start(key, broker.attach_cmd(session), cwd, cols=cols, rows=rows), profile, row

    def open_workspace(self, context, *, slot=1, cols=120, rows=32):
        from contextlib import nullcontext
        admission = self.runtime.application.writer_admission
        with admission.writer() if admission is not None else nullcontext():
            return self._open_workspace_admitted(context, slot=slot, cols=cols, rows=rows)

    def _open_workspace_admitted(self, context, *, slot, cols, rows):
        if self.broker is None or not self.broker.tmux_available():
            raise DomainError("operation_unavailable", "The workspace terminal requires the tmux mirror backend")
        key, session, cwd = self.workspace_binding(context, slot)
        # This is deliberately just a shell. It has no agent profile, preparation,
        # credentials, inbox, or transcript capture hook.
        self.broker.ensure_session(session, self.shell, cwd, env=self.global_environment(), cols=cols, rows=rows)
        return self.broker.get_or_start(key, self.broker.attach_cmd(session), cwd, cols=cols, rows=rows)

    def listing(self, context, business):
        self.owner(context)
        execution = self.execution(business)
        broker = execution.broker
        if broker is None or not broker.tmux_available():
            return []
        with self.runtime.database.read() as uow:
            # Only active supported globals have terminals; a business-home identity is historical and never listed.
            agents = [row for row in uow.connection.execute(
                "SELECT * FROM agent_profiles WHERE home_business_id IS NULL AND status='active' ORDER BY name").fetchall()
                if context.business_id is None and active_global(uow.connection, row["agent_id"]) is not None]
        sessions = set(broker.list_sessions(broker.session_prefix(business["slug"] if business else "global")))
        return [{"agent": row["name"], "agent_id": row["agent_id"], "done": False}
                for row in agents if self.binding(business, row, broker)[1] in sessions]

    def input_existing(self, business_id, agent_id, data, key=None):
        """Trusted broadcast port: only an already-attached mirror is writable.

        It intentionally has no owner check because the dispatcher has already
        persisted an authorized broadcast.  It never calls open/ensure_session.  A host-native agent's input goes through the
        shared native input arbiter (``key`` is the broadcast delivery's idempotency key); a sandbox or remote target keeps
        its broker transport.
        """
        if not isinstance(data, str) or len(data) > 65536:
            raise DomainError("validation_error", "Terminal input requires bounded text")
        with self.runtime.database.read() as uow:
            business = uow.repository("businesses").get(business_id) if business_id else None
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND home_business_id IS ? AND status='active'",
                (agent_id, business_id)).fetchone()
            # A business-home identity is never dispatched to, whatever its stored status.
            if row is None or row["home_business_id"] is not None or active_global(uow.connection, agent_id) is None:
                return {"state": "unavailable", "detail": "Agent is unavailable"}
        execution = self.execution(business)
        if execution.target_identity is None and execution.broker is self.broker and key is not None:
            return self._native_input(agent_id, data, key)
        broker = execution.broker
        if broker is None or not broker.tmux_available():
            return {"state": "unavailable", "detail": "Terminal backend is unavailable"}
        key, session, _ = self.binding(business, row, broker)
        terminal = broker.get(key)
        if terminal is None or getattr(terminal, "done", False) or not broker.session_alive(session):
            return {"state": "unavailable", "detail": "No live terminal mirror is attached"}
        try:
            # Keep paste and submit separate through the owning pane.
            # The broker waits for client paste handling before sending Enter.
            broker.submit_input(session, data)
        except Exception:
            # The broker cannot prove whether a failed write reached the PTY.
            return {"state": "uncertain", "detail": "Terminal write outcome is uncertain"}
        return {"state": "delivered"}

    def _native_input(self, agent_id, data, key):
        """One broadcast delivery to a host-native session through the arbiter, reported in the dispatcher's vocabulary."""
        from bazarlyk.native_chat.contracts import InputRequest, NativeChatError, STATE_FAILED, STATE_SUBMITTED, STATE_UNCERTAIN, TYPE_BROADCAST
        try:
            result = self.runtime.native_chat.arbiter.submit(InputRequest(agent_id, TYPE_BROADCAST, key, data))
        except NativeChatError as error:
            return {"state": "failed", "detail": error.message}
        if result.state == STATE_SUBMITTED:
            return {"state": "delivered", "evidence": result.evidence}
        if result.state == STATE_UNCERTAIN:
            return {"state": "uncertain", "detail": "Terminal write outcome is uncertain"}
        if result.state == STATE_FAILED:
            return {"state": "failed", "detail": result.reason or "Terminal input failed"}
        # Rejected or deferred: nothing was typed.
        return {"state": "unavailable", "detail": result.reason or "No verified native session is ready"}

    def _prepare(self, execution, agent_id):
        versions = execution.version_provider()
        if isinstance(versions, str):
            versions = {"claude-code": versions}
        kwargs = {"endpoint": execution.endpoint, "versions": versions, "capture_transcripts": self.capture_transcripts}
        if execution.interpreter is not None:
            kwargs["interpreter"] = execution.interpreter
        if execution.interpreter_validator is not None:
            kwargs["interpreter_validator"] = execution.interpreter_validator
        self.preparation(self.runtime.resolver, Principal(agent_id, "agent"), **kwargs)

    def reset_business_for_archive(self, business, agents):
        """Trusted approved archive effect: clear only this business's sessions.

        Unlike opening a terminal, this never prepares an agent or launches a
        replacement model. Missing sessions are already reset, including retries.
        """
        broker = self.execution(business).broker
        if broker is None or not broker.tmux_available():
            raise DomainError("operation_unavailable", "Business terminal reset is unavailable")
        sessions = set(broker.list_sessions(broker.session_prefix(business["slug"])))
        for row in agents:
            key, session, _ = self.binding(business, row, broker)
            if session in sessions and not broker.kill_session(session):
                # Another client may have closed it concurrently. Confirm absence.
                if session in broker.list_sessions(broker.session_prefix(business["slug"])):
                    raise DomainError("operation_failed", "A business terminal could not be reset", retryable=True)
            broker.stop(key)

    async def route(self, request, context, business, tail):
        if tail == ["terminals"] and request.method == "GET":
            return JSONResponse(await anyio.to_thread.run_sync(lambda: self.listing(context, business)))
        # Owner shells: workspace-terminal/<action> is slot 1; slots 2 and 3 add their number.
        workspace_terminal = business is None and tail[:1] == ["workspace-terminal"] and (
            len(tail) == 2 or len(tail) == 3 and tail[1] in WORKSPACE_TERMINAL_SLOTS)
        agent_terminal = len(tail) == 3 and tail[0] == "terminal"
        if not workspace_terminal and not agent_terminal:
            raise DomainError("not_found", "Terminal endpoint is not registered")
        slot = int(tail[1]) if workspace_terminal and len(tail) == 3 else 1
        selector, action = (None, tail[-1]) if workspace_terminal else tail[1:]
        check_target = (lambda: self.owner(context)) if workspace_terminal else (lambda: self.agent(context, selector))
        if workspace_terminal:
            execution = self._host_execution()
            broker = self.broker
        else:
            profile, row = await anyio.to_thread.run_sync(check_target)
            execution = await anyio.to_thread.run_sync(lambda: self.execution(business))
            broker = execution.broker
        open_target = (lambda: (self.open_workspace(context, slot=slot), None, None)) if workspace_terminal else (lambda: self.open(context, business, selector, execution=execution, reconnect=action == "stream"))
        binding_target = (lambda: self.workspace_binding(context, slot)) if workspace_terminal else (lambda: self.binding(business, row, broker))
        if action == "stream" and request.method == "GET":
            if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
                raise DomainError("csrf_required", "Terminal streams must originate in this dashboard")
            terminal, profile, row = await anyio.to_thread.run_sync(open_target)
            async def events():
                current = terminal
                checked_at = time.monotonic()
                # Idle waits block a worker thread on the broker's output condition, so
                # give each stream its own limiter instead of draining the shared pool.
                idle_limiter = anyio.CapacityLimiter(1)
                blob, cursor = current.snapshot()
                if blob:
                    # Keep the data frame compatible with existing mirrors. New clients
                    # avoid replying to historical device queries during scrollback replay.
                    yield b"event: replay\ndata: {}\n\n"
                    yield b"data: " + base64.b64encode(blob) + b"\n\n"
                while not await request.is_disconnected():
                    if time.monotonic() - checked_at >= 5:
                        try:
                            await anyio.to_thread.run_sync(check_target)
                            if not workspace_terminal and self.execution_provider is not None:
                                selected = await anyio.to_thread.run_sync(lambda: self.execution(business))
                                if not self._same_execution(execution, selected):
                                    yield b"event: done\ndata: {}\n\n"
                                    return
                        except DomainError:
                            yield b"event: done\ndata: {}\n\n"
                            return
                        checked_at = time.monotonic()
                    chunk, cursor = current.tail_from(cursor)
                    if chunk:
                        yield b"data: " + base64.b64encode(chunk) + b"\n\n"
                    if current.done:
                        key, session, cwd = binding_target()
                        if broker.session_alive(session):
                            current = broker.get_or_start(key, broker.attach_cmd(session), cwd,
                                cols=current.cols, rows=current.rows)
                            cursor = 0
                        else:
                            yield b"event: done\ndata: {}\n\n"
                            return
                    if not chunk:
                        # New PTY bytes stream immediately; the timeout keeps the
                        # disconnect and authorization rechecks above alive.
                        wait = getattr(current, "wait_output", None)
                        if wait is None:
                            await anyio.sleep(0.03)
                        else:
                            await anyio.to_thread.run_sync(wait, cursor, 0.25, limiter=idle_limiter)
            return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        if request.method != "POST":
            raise DomainError("not_found", "Terminal endpoint is not registered")
        body = await read_json(request, limit=128 * 1024)
        allowed = {"start": {"cols", "rows"}, "resize": {"cols", "rows"}, "input": {"data"}, "stop": set()}
        if action not in allowed:
            raise DomainError("not_found", "Terminal action is not registered")
        checked_fields(body, allowed[action])
        if workspace_terminal:
            await anyio.to_thread.run_sync(lambda: self.owner(context))
            profile, row = None, None
        # For business actions, authorization and execution selection were
        # completed above so every action in this request shares one identity.
        if broker is None:
            raise DomainError("operation_unavailable", "Terminal backend is unavailable")
        key, session, cwd = binding_target()
        if action == "start":
            cols, rows = self.size(body)
            if workspace_terminal:
                terminal = await anyio.to_thread.run_sync(lambda: self.open_workspace(context, slot=slot, cols=cols, rows=rows))
                target = {"workspace": cwd}
            else:
                terminal, _, _ = await anyio.to_thread.run_sync(lambda: self.open(context, business, selector,
                    cols=cols, rows=rows, execution=execution))
                target = {"agent": profile.data["name"]}
            return JSONResponse({"ok": True, **target, "cols": terminal.cols, "rows": terminal.rows, "done": False})
        if action == "stop":
            stopped = False
            try:
                stopped = await anyio.to_thread.run_sync(lambda: broker.kill_session(session))
            finally:
                stopped = broker.stop(key) or stopped
            return JSONResponse({"ok": bool(stopped)})
        terminal = broker.get(key)
        if terminal is None:
            raise DomainError("not_found", "No live terminal mirror is attached")
        if action == "input":
            data = body.get("data")
            if not isinstance(data, str) or len(data) > 65536:
                raise DomainError("validation_error", "Terminal input requires bounded text")
            terminal.write(data.encode("utf-8"))
        else:
            cols, rows = self.size(body)
            terminal.resize(rows, cols)
            await anyio.to_thread.run_sync(lambda: broker.resize_session(session, rows, cols))
        return JSONResponse({"ok": True})
