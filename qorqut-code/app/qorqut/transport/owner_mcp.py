"""Owner-session MCP caller policy: fixed catalog, one bound project.

Requests act with the installation owner's own human identity; the project
comes only from trusted credential metadata. This adapter never creates an
agent profile, inbox, watcher, or task discovery. A specific owner instruction
in the CLI conversation authorizes the requested write; this adapter enforces
the catalog, project binding and shared-discussion boundary.
"""

from dataclasses import replace

from qorqut import owner_sessions
from qorqut_operations.application.batches import NAME as BATCH_NAME
from qorqut_operations.contracts import DomainError
from qorqut_operations.project_lifecycle import owner_may_act


class OwnerSessionCaller:
    scope = "owner-session"

    def __init__(self, runtime, authentication):
        self.runtime, self.authentication = runtime, authentication

    def authenticate(self, authorization):
        actor = self.authentication.owner_session(authorization)
        with self.runtime.database.read() as uow:
            self._require_enabled(actor, uow)
        return actor

    def _require_enabled(self, actor, uow):
        """Connection/scope validation on every request, including open sessions."""
        project = uow.repository("projects").get(actor.project_id)
        if project is None or project["archived_at"]:
            raise DomainError("forbidden", "The owner-session project is unavailable or archived")
        if not owner_sessions.enabled(project["config_json"]):
            raise DomainError("forbidden", "Owner sessions are not enabled for this project")
        if not owner_may_act(uow.connection, actor.principal.id, actor.project_id):
            raise DomainError("forbidden", "The installation owner cannot act in the owner-session project")

    def identity(self, actor):
        return (actor.principal.id, actor.credential_id, actor.project_id)

    @staticmethod
    def exposes(descriptor):
        return (getattr(descriptor, "owner_session", False)
                or (not descriptor.owner_only and "mcp" in descriptor.exposure))

    def inventory(self, actor):
        registry = self.runtime.registry
        rows = []
        for name in sorted(owner_sessions.CATALOG):
            try:
                descriptor = registry.get(name)
            except DomainError:
                continue
            if not self.exposes(descriptor):
                continue
            metadata = descriptor.metadata
            rows.append({"canonical_operation": name, "exposed_aliases": [],
                "input_schema_version": descriptor.version, "plugin_requirement": None,
                "effective_mode": "allow", "supports_signoff": metadata.supports_signoff,
                "source": {"kind": "owner_session"}, "available": True})
        return rows

    def check_operation(self, canonical_name):
        if canonical_name not in owner_sessions.CATALOG:
            raise DomainError("forbidden", "This operation is not in the owner-session catalog")

    def validate_arguments(self, actor, canonical_name, args):
        # Scope is a domain argument (not an invocation option). Human task,
        # Signoff and private-conversation readers otherwise accept Global even
        # when their initial context is project-bound.
        if args.get("scope") == "global":
            raise DomainError("forbidden", "Owner sessions cannot select Global operational scope")

    def validate_options(self, actor, canonical_name, options):
        for value in (options.project_id, options.review_project_id):
            if value is not None and value != actor.project_id:
                raise DomainError("forbidden", "Owner sessions are bound to their configured project")

    def invocation_context(self, actor, *, canonical_name, options):
        project_id = actor.project_id
        with self.runtime.database.read() as uow:
            self._require_enabled(actor, uow)
            context = self.runtime.policy.context_for(actor.principal.id, project_id=project_id,
                review_project_id=project_id, uow=uow, source="mcp")
            # Keep nested references and prepared targets within this profile,
            # even though the human owner belongs to other projects too.
            return replace(context, allowed_project_ids=frozenset({actor.project_id}),
                owner_session_project_id=actor.project_id)

    def check_batch(self, actor):
        # authenticate() already re-ran the credential and profile checks for
        # this batch entry; the catalog decides batch availability.
        if BATCH_NAME not in owner_sessions.CATALOG:
            raise DomainError("policy_denied", "Batch execution is not allowed for owner sessions")
        return actor
