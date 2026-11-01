"""Owner-session MCP caller policy: fixed catalog, one bound business.

Requests act with the installation owner's own human identity; the business
comes only from trusted credential metadata. This adapter never creates an
agent profile, inbox, watcher, or task discovery. A specific owner instruction
in the CLI conversation authorizes the requested write; this adapter enforces
the catalog, business binding and shared-discussion boundary.
"""

from dataclasses import replace

from bazarlyk import owner_sessions
from bazarlyk.application.batches import NAME as BATCH_NAME
from bazarlyk.contracts import DomainError


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
        business = uow.repository("businesses").get(actor.business_id)
        if business is None or business["archived_at"]:
            raise DomainError("forbidden", "The owner-session business is unavailable or archived")
        if not owner_sessions.enabled(business["config_json"]):
            raise DomainError("forbidden", "Owner sessions are not enabled for this business")
        member = uow.repository("business_memberships", business_id=actor.business_id).get(actor.principal.id)
        if not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "Owner-session business membership is unavailable")

    def identity(self, actor):
        return (actor.principal.id, actor.credential_id, actor.business_id)

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
        # when their initial context is business-bound.
        if args.get("scope") == "global":
            raise DomainError("forbidden", "Owner sessions cannot select Global operational scope")
        if canonical_name == "agents_message_recipients" and args.get("all_businesses"):
            raise DomainError("forbidden", "Owner-session recipients are limited to the bound business")
        if canonical_name == "tasks_create" and "focus" in args:
            def in_business(value):
                if isinstance(value, dict):
                    if value.get("scope") == "global":
                        return False
                    if "business_id" in value and value["business_id"] not in (None, actor.business_id):
                        return False
                    return all(in_business(item) for item in value.values())
                if isinstance(value, list):
                    return all(in_business(item) for item in value)
                return True
            if not in_business(args["focus"]):
                raise DomainError("forbidden", "Task focus must remain inside the owner-session business")
        # Story note/summary fields ride on shared timeline events. The domain
        # deliberately keeps state-change reasons shared, so this route refuses
        # them the same way stories_note_add refuses the owner: freeform
        # commentary belongs to the private Ordubashy conversation.
        if canonical_name in {"stories_update", "stories_close", "stories_reopen"} and (
                args.get("note") or args.get("summary")):
            from bazarlyk.domains.team_channel import channel_required
            raise channel_required(canonical_name)

    def validate_options(self, actor, canonical_name, options):
        if canonical_name in owner_sessions.GLOBAL_CONTEXT_OPERATIONS:
            if options.business_id is not None or options.review_business_id is not None:
                raise DomainError("forbidden", "Personal context is Global; owner sessions must not select a business for it")
            return
        for value in (options.business_id, options.review_business_id):
            if value is not None and value != actor.business_id:
                raise DomainError("forbidden", "Owner sessions are bound to their configured business")

    def invocation_context(self, actor, *, canonical_name, options):
        business_id = (None if canonical_name in owner_sessions.GLOBAL_CONTEXT_OPERATIONS
                       else actor.business_id)
        with self.runtime.database.read() as uow:
            self._require_enabled(actor, uow)
            context = self.runtime.policy.context_for(actor.principal.id, business_id=business_id,
                review_business_id=business_id, uow=uow, source="mcp")
            # Keep nested references and prepared targets within this profile,
            # even though the human owner belongs to other businesses too.
            return replace(context, allowed_business_ids=frozenset({actor.business_id}),
                owner_session_business_id=actor.business_id)

    def check_batch(self, actor):
        # authenticate() already re-ran the credential and profile checks for
        # this batch entry; the catalog decides batch availability.
        if BATCH_NAME not in owner_sessions.CATALOG:
            raise DomainError("policy_denied", "Batch execution is not allowed for owner sessions")
        return actor
