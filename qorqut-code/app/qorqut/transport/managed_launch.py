"""What a managed session needs from the service before its host starts, and the broadcast port into it.

``prepare_managed`` prepares an agent's workspace and builds its launch environment (the managed runtime calls it on every open).
``input_existing`` is the trusted broadcast port: one delivery through the shared input arbiter, never an open."""

from contextlib import nullcontext

from qorqut import agent_cli_launch
from qorqut.agents.global_scope import active_global
from qorqut.bootstrap.preparation import installed_versions, prepare_agent
from qorqut.composition.locations import agent_workspace
from qorqut.management.identity import require_protected_control
from qorqut_operations.contracts import DomainError, Principal
from qorqut_operations.project_lifecycle import owner_may_act


class ManagedLaunch:
    def __init__(self, runtime, *, service_url, environment=None, version_provider=None, preparation=prepare_agent,
                 capture_transcripts=True, global_environment=None, agent_environment=None):
        self.runtime, self.service_url = runtime, service_url
        self.environment = environment or (lambda project: {})
        self.global_environment = global_environment or (lambda: {})
        self.agent_environment = agent_environment
        self.version_provider = version_provider or installed_versions
        self.preparation = preparation
        if type(capture_transcripts) is not bool:
            raise ValueError("Transcript capture selection must be a boolean")
        self.capture_transcripts = capture_transcripts

    def _environment(self, profile, project):
        if profile is not None and self.agent_environment:
            return self.agent_environment(project, profile)
        return self.environment(project) if project else self.global_environment()

    def agent(self, context, selector):
        """The registered active agent ``selector`` (id or name) of this context's workspace, for the installation owner."""
        self.runtime.require_authority()
        with self.runtime.database.read() as uow:
            current = self.runtime.application.current_context(context, uow)
            if current.project_id is None:
                installation = uow.repository("installation").get("service")
                if current.principal_kind != "human" or not installation or installation["owner_principal_id"] != current.principal_id:
                    raise DomainError("forbidden", "Preparing a global agent requires the installation owner")
            else:
                if current.principal_kind != "human" or not owner_may_act(uow.connection, current.principal_id, current.project_id):
                    raise DomainError("forbidden", "Preparing an agent requires the installation owner in an active project")
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_project_id IS ? AND (agent_id=? OR name=?) AND status='active'",
                                         (context.project_id, selector, selector)).fetchone()
            if row is None:
                raise DomainError("not_found", "Registered active agent was not found in this workspace")
            require_protected_control(uow, current.principal_id, current.principal_kind, [row["agent_id"]])
            profile, indexed = self.runtime.resolver.profile(Principal(row["agent_id"], "agent"), connection=uow.connection)
            return profile, indexed

    def prepare(self, agent_id, versions=None):
        """Write the agent's prepared workspace (instructions, settings, hooks, Codex configuration) for the installed CLIs."""
        versions = versions or self.version_provider()
        if isinstance(versions, str):
            versions = {"claude-code": versions}
        return self.preparation(self.runtime.resolver, Principal(agent_id, "agent"), endpoint=self.service_url, versions=versions,
                                capture_transcripts=self.capture_transcripts)

    def prepare_managed(self, agent_id):
        """The prepared workspace and launch environment of this global agent for a managed (headless) session:
        ``{"workspace", "env"}``. The environment carries the agent's credentials; the caller passes it to the host through
        its private launch file and never through an argument or a log."""
        admission = self.runtime.application.writer_admission
        with admission.writer() if admission is not None else nullcontext():
            with self.runtime.database.read() as uow:
                row = uow.connection.execute(
                    "SELECT * FROM agent_profiles WHERE agent_id=? AND home_project_id IS NULL AND status='active'", (agent_id,)).fetchone()
                if row is None or active_global(uow.connection, agent_id) is None:
                    raise DomainError("not_found", "Registered active global agent was not found")
                profile, _ = self.runtime.resolver.profile(Principal(agent_id, "agent"), connection=uow.connection)
            self.prepare(agent_id)
            env = agent_cli_launch.session_environment(dict(self._environment(profile, None)))
            return {"workspace": str(agent_workspace(self.runtime.paths, agent_id, None)), "env": env}

    def input_existing(self, project_id, agent_id, data, key=None):
        """Trusted broadcast port: it has no owner check because the dispatcher has already persisted an authorized broadcast.
        Only a session the shared arbiter can reach receives it (``key`` is the delivery's idempotency key)."""
        if not isinstance(data, str) or len(data) > 65536:
            raise DomainError("validation_error", "Broadcast input requires bounded text")
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=? AND home_project_id IS ? AND status='active'",
                (agent_id, project_id)).fetchone()
            # A project-home identity is never dispatched to, whatever its stored status.
            if row is None or row["home_project_id"] is not None or active_global(uow.connection, agent_id) is None:
                return {"state": "unavailable", "detail": "Agent is unavailable"}
        if key is None:
            return {"state": "unavailable", "detail": "An agent is reached through its Chat session only"}
        return self._native_input(agent_id, data, key)

    def _native_input(self, agent_id, data, key):
        """One broadcast delivery through the arbiter, reported in the dispatcher's vocabulary."""
        from qorqut.native_chat.contracts import InputRequest, NativeChatError, STATE_FAILED, STATE_SUBMITTED, STATE_UNCERTAIN, TYPE_BROADCAST
        try:
            result = self.runtime.native_chat.arbiter.submit(InputRequest(agent_id, TYPE_BROADCAST, key, data))
        except NativeChatError as error:
            return {"state": "failed", "detail": error.message}
        if result.state == STATE_SUBMITTED:
            return {"state": "delivered", "evidence": result.evidence}
        if result.state == STATE_UNCERTAIN:
            return {"state": "uncertain", "detail": "The delivery outcome is uncertain"}
        if result.state == STATE_FAILED:
            return {"state": "failed", "detail": result.reason or "The delivery failed"}
        # Rejected or deferred: nothing was delivered.
        return {"state": "unavailable", "detail": result.reason or "No verified session is ready"}
