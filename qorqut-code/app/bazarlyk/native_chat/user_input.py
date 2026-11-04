"""``request_user_input``: an authorized agent's presentation of existing records in Chat.

The tool is a read that returns at once. It validates the message and each reference through the existing read authorization and
returns the normalized, identity-only presentation; that successful tool result in the agent's native transcript is the one source Chat
projects (``projection.py``). It creates no request, queue, answer, lease or Done state, and it changes no Task, Signoff, Focus
card, chat input or browser: a Signoff is decided through the Signoff API, a browser is used through ``browser_turn``, and a Task
is only shown.

The owner's Chat resolves the references again at read time (``owner_references``) from current records and the trusted browser
configuration, so a label, state or viewer URL is never taken from the model and a missing target never breaks the transcript."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE, authorize_item_read

from bazarlyk.management.identity import configured_ordubashy

from . import references as refs
from . import store

NAME = "request_user_input"
DESCRIPTION = (
    "Ordubashy or the current Overseer. Show the owner a message in your Chat with existing records right below it: pending Signoffs (decided there by "
    "the owner), configured browsers (a live view the owner can use to log in or act), Tasks (title and state only) and registered agents "
    "(name only). Returns at once; it asks nothing, creates no request, waits for no answer and grants no approval. references are "
    "{type: task|signoff|browser|agent|project|delta, id, business_id}: a task, signoff, agent, project or delta by its UUID, a browser by the "
    "id from browser_turn action=list, a Project by its actual UUID with business_id equal to that same UUID, and business_id the "
    "record's owning business (null or omitted for Global and always for an agent and a browser; a project always "
    "needs its exact scope; a delta has no business_id). At most 8 references; the message is at most 4000 characters. Read the "
    "record first and reuse the exact ids; an unknown or unreadable one is refused.")
SCHEMA = schema({
    "message": {"type": "string", "minLength": 1, "maxLength": refs.MAX_MESSAGE_CHARS},
    "references": {"type": "array", "maxItems": refs.MAX_REFERENCES, "items": schema({
        "type": {"enum": list(refs.TYPES)},
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
        "business_id": {"type": ["string", "null"], "format": "uuid"}}, ("type", "id"))},
}, ("message",))


class UserInput:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        self.runtime.registry.register(OperationDescriptor(
            NAME, SCHEMA, supports_signoff=False, scope_kinds=frozenset({"global"}), global_capability="read_global_context",
            resolve_namespace=lambda ctx, args: "global", exposure=frozenset({"mcp"}), effect_kind="read",
            description=DESCRIPTION, read=self.read))
        self.runtime.user_input = self
        return self

    # ----- the tool ------------------------------------------------------------------------------------------------------

    def caller(self, ctx, uow):
        """The configured Ordubashy or currently registered and capable Overseer."""
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        if ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "request_user_input is limited to the configured Ordubashy agent")
        if configured_ordubashy(uow) != ctx.principal_id:
            from bazarlyk.overseer.authority import current_overseer
            ctx, _ = current_overseer(self.runtime, ctx, uow, capability='manage_agentic_layer')
            session = store.live_session(uow.connection, ctx.principal_id)
            if session is None or not session['provider'] or not session['conversation_id']:
                raise DomainError('forbidden', 'The current Overseer conversation is unavailable')
        return ctx

    def read(self, ctx, args, options, uow):
        ctx = self.caller(ctx, uow)
        try:
            shown = refs.normalize(args["message"], args.get("references"))
        except refs.InputError as error:
            raise DomainError("validation_error", str(error)) from None
        for position, reference in enumerate(shown["references"], 1):
            try:
                self.resolve(ctx, reference, uow)
            except DomainError as error:
                if error.code not in UNAVAILABLE:
                    raise
                raise DomainError("not_found", f"Reference {position} ({reference['type']}) is not available; read it first and "
                                  "use its exact id and business_id") from None
        return {"user_input": shown}

    # ----- resolution ----------------------------------------------------------------------------------------------------

    def resolve(self, ctx, reference, uow, *, viewer=False):
        """The current record a reference names, through the caller's existing read authorization, or an ``UNAVAILABLE`` error.
        Returns the display facts: ``label`` and, for a Task, ``state``; for a browser also its viewer when ``viewer`` asks for
        a ready one."""
        kind, identity, business = reference["type"], reference["id"], reference["business_id"]
        if kind == refs.TYPE_AGENT:
            return self.agent(ctx, identity, uow)
        if kind == refs.TYPE_BROWSER:
            return self.runtime.browser_access.targets.resolve(uow, ctx.principal_id, identity, business, viewer=viewer)
        if kind == refs.TYPE_DELTA:
            from qorqut_agentic.errors import AgenticError
            from bazarlyk.overseer.access import EvidenceAccess, OwnerEvidenceAccess
            gate = OwnerEvidenceAccess(self.runtime, ctx) if ctx.principal_kind == 'human' else EvidenceAccess(self.runtime, ctx)
            try:
                delta = self.runtime.agentic_layer.overseer.deltas.get(identity)
                for proposal_id in delta['revision']['proposal_ids']:
                    proposal = self.runtime.agentic_layer.overseer.proposals.get(proposal_id)
                    for source in proposal['sources']:
                        gate.authorize_source(source['ref'])
                return {'label': delta['revision']['summary'], 'business_slug': None}
            except AgenticError as error:
                raise DomainError(error.code, str(error), details=error.details) from error
        card = self.runtime.focus.resolve(ctx, {"type": kind, "id": identity, "business_id": business}, uow)
        found = {"label": card["label"], "business_slug": card["business_slug"]}
        if kind == refs.TYPE_TASK:
            found["state"] = card["state"]
        return found

    def agent(self, ctx, identity, uow):
        """A registered agent as Chat names it, through the existing ``agents_get`` read authorization: the caller must be allowed
        that read, and a Global agent needs the Global capabilities while a business-home agent is outside Global. An unknown id,
        a human principal or a profile the caller may not read is an ``UNAVAILABLE`` error."""
        authorize_item_read(self.app, ctx, "agents_get", uow)
        self.runtime.agents._access(ctx, uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone())
        found = store.agent_identity(uow.connection, identity)
        if found is None:
            raise DomainError("not_found", "Agent profile does not exist")
        return found

    def owner_references(self, principal_id, found, uow):
        """The owner's view of the references a page of Chat names, by ``(type, id, business_id)``: current records resolved with
        the installation owner's own reach, a viewer URL only from the trusted configuration, and an unavailable placeholder
        (identity only, no target detail) for anything missing, deleted or not readable by the owner."""
        ctx = self.app.policy.context_for(principal_id, business_id=None, review_business_id=None, uow=uow)
        views = {}
        for key in dict.fromkeys(found):
            kind, identity, business = key
            try:
                facts = self.resolve(ctx, {"type": kind, "id": identity, "business_id": business}, uow, viewer=True)
            except DomainError as error:
                if error.code not in UNAVAILABLE and error.code != "invalid_configuration":
                    raise
                continue
            if kind == refs.TYPE_AGENT:
                views[key] = {"type": kind, "id": identity, "business_id": None, "label": facts["display_name"], "available": True,
                              **{name: facts[name] for name in ("role", "presentation") if name in facts}}
                continue
            view = {"type": kind, "id": identity, "business_id": business, "label": facts["label"], "available": True}
            if kind == refs.TYPE_BROWSER:
                view["viewer_url"] = facts["viewer_url"]
                view.update({size: facts[size] for size in ("width", "height") if size in facts})
            else:
                view["business_slug"] = facts["business_slug"]
                if kind == refs.TYPE_TASK:
                    view["state"] = facts["state"]
            views[key] = view
        return views
