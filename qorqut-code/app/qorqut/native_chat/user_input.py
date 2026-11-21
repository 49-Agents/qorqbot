"""``request_user_input``: an authorized agent's presentation of existing records in Chat.

The tool is a read that returns at once. It validates the message and each reference through the existing read authorization and
returns the normalized, identity-only presentation; that successful tool result in the agent's native transcript is the one source Chat
projects (``projection.py``). It creates no request, queue, answer, lease or Done state, and it changes no Task, Signoff, Focus
card, chat input or browser: a Signoff is decided through the Signoff API, a browser is used through ``browser_turn``, and a Task
or a browser tab is only shown. A tab reference names one open page of an agent's managed Chrome; it grants no control and no
authorization, and the owner's live view of it stays the owner's alone.

The owner's Chat resolves the references again at read time (``owner_references``) from current records and the trusted browser
configuration, so a label, state or viewer URL is never taken from the model and a missing target never breaks the transcript."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from dataclasses import replace
import re
from urllib.parse import urlsplit

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError
from qorqut.errors import domain_error_from_agentic
from qorqut_operations.items import UNAVAILABLE, authorize_item_read

from qorqut.management.identity import configured_ordubashy

from . import references as refs
from . import store

NAME = "request_user_input"
MAX_TAB_LABEL = 200
DESCRIPTION = (
    instruction_text('user-input-ordubashy-or-the-current-overseer-show-the'))
SCHEMA = schema({
    "message": {"type": "string", "minLength": 1, "maxLength": refs.MAX_MESSAGE_CHARS},
    "references": {"type": "array", "maxItems": refs.MAX_REFERENCES, "items": schema({
        "type": {"enum": list(refs.TYPES)},
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
        "project_id": {"type": ["string", "null"], "format": "uuid"}}, ("type", "id"))},
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
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        if ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "request_user_input is limited to the configured Ordubashy agent")
        if configured_ordubashy(uow) != ctx.principal_id:
            from qorqut.overseer.authority import current_overseer
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
        listed = {}      # each agent's open tabs, listed once however many of its tabs are named
        for position, reference in enumerate(shown["references"], 1):
            try:
                self.resolve(ctx, reference, uow, tabs=listed)
            except DomainError as error:
                if error.code not in UNAVAILABLE:
                    raise
                raise DomainError("not_found", f"Reference {position} ({reference['type']}) is not available; read it first and "
                                  "use its exact id and project_id") from None
        return {"user_input": shown}

    # ----- resolution ----------------------------------------------------------------------------------------------------

    def resolve(self, ctx, reference, uow, *, viewer=False, tabs=None):
        """The current record a reference names, through the caller's existing read authorization, or an ``UNAVAILABLE`` error.
        Returns the display facts: ``label`` and, for a Task, ``state``; for a browser also its viewer when ``viewer`` asks for
        a ready one. ``tabs`` is the caller's memo of the tabs already listed, by agent (see ``tab``)."""
        kind, identity, project = reference["type"], reference["id"], reference["project_id"]
        if kind == refs.TYPE_TAB:
            return self.tab(ctx, identity, uow, tabs)
        if kind == refs.TYPE_BROWSER:
            return self.runtime.browser_access.targets.resolve(uow, ctx.principal_id, identity, project, viewer=viewer)
        if kind == refs.TYPE_DELTA:
            from qorqut_agentic.errors import AgenticError
            from qorqut.overseer.access import EvidenceAccess, OwnerEvidenceAccess
            gate = OwnerEvidenceAccess(self.runtime, ctx) if ctx.principal_kind == 'human' else EvidenceAccess(self.runtime, ctx)
            try:
                delta = self.runtime.agentic_layer.overseer.deltas.get(identity)
                for proposal_id in delta['revision']['proposal_ids']:
                    proposal = self.runtime.agentic_layer.overseer.proposals.get(proposal_id)
                    for source in proposal['sources']:
                        gate.authorize_source(source['ref'])
                return {'label': delta['revision']['summary'], 'project_slug': None}
            except AgenticError as error:
                raise domain_error_from_agentic(error) from error
        card = self.runtime.focus.resolve(ctx, {"type": kind, "id": identity, "project_id": project}, uow)
        found = {"label": card["label"], "project_slug": card["project_slug"]}
        if kind == refs.TYPE_TASK:
            found["state"] = card["state"]
        return found

    def agent(self, ctx, identity, uow):
        """A registered agent whose browser tab Chat shows, through the existing ``agents_get`` read authorization: the caller must be allowed
        that read, and an agent needs the Global capabilities. An unknown id,
        a human principal or a profile the caller may not read is an ``UNAVAILABLE`` error."""
        authorize_item_read(self.app, ctx, "agents_get", uow)
        self.runtime.agents._access(ctx, uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone())
        found = store.agent_identity(uow.connection, identity)
        if found is None:
            raise DomainError("not_found", "Agent profile does not exist")
        return found

    def tab(self, ctx, identity, uow, listed=None):
        """One open tab of a registered agent's managed Chrome: the agent is read as ``agent`` reads it, then the tab must be in
        the list Chrome gives now. Returns the agent's facts as ``agent`` and the tab's ``title`` and ``url`` as the page
        reports them, which are untrusted text. A browser that is stopped, not set up, not answering or not the agent's, and a
        tab that is closed, are all one ``UNAVAILABLE`` error: nothing here may fail a read of Chat or hold it up.

        ``listed`` maps an agent to its tabs as listed in this call (``None`` for a browser that did not answer), so one page of
        Chat naming several tabs of one agent asks that browser once. Chrome answers within a few seconds at most
        (``sign_in._get_json``) and refuses at once when it is not running."""
        agent_id, tab_id = refs.tab_parts(identity)
        found = self.agent(ctx, agent_id, uow)
        if listed is None:
            listed = {}
        if agent_id not in listed:
            listed[agent_id] = self._open_tabs(agent_id)
        tab = (listed[agent_id] or {}).get(tab_id)
        if tab is None:
            raise DomainError("not_found", "Tab is not open")
        return {"agent": found, "title": tab["title"], "url": tab["url"]}

    def _open_tabs(self, agent_id):
        """``{tab id: tab}`` of the browser the agent uses, or ``None`` when it cannot be listed, whatever the reason."""
        from qorqut.browser import live
        try:
            return {tab["id"]: tab for tab in live.tabs(live.port(self.runtime, agent_id))}
        except Exception:
            return None

    @staticmethod
    def tab_label(title, url):
        """What the owner's card for a tab says: the page's title, else the address's host, else ``Tab``. Both come from the
        page, so this is text only, short and on one line; nothing of the address but its host is ever used."""
        label = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", title or "").split())
        if not label:
            try:
                label = urlsplit(url or "").hostname or ""
            except ValueError:
                label = ""
        return label[:MAX_TAB_LABEL] or "Tab"

    def owner_references(self, principal_id, found, uow):
        """The owner's view of the references a page of Chat names, by ``(type, id, project_id)``: current records resolved with
        the installation owner's own reach, a viewer URL only from the trusted configuration, and an unavailable placeholder
        (identity only, no target detail) for anything missing, deleted or not readable by the owner."""
        ctx = self.app.policy.context_for(principal_id, project_id=None, review_project_id=None, uow=uow)
        views, listed = {}, {}       # listed: each agent's open tabs, asked of its browser once for the whole page
        for key in dict.fromkeys(found):
            kind, identity, project = key
            if kind in refs.RETIRED_TYPES:
                continue
            try:
                facts = self.resolve(ctx, {"type": kind, "id": identity, "project_id": project}, uow, viewer=True, tabs=listed)
            except DomainError as error:
                if error.code not in UNAVAILABLE and error.code != "invalid_configuration":
                    raise
                continue
            if kind == refs.TYPE_TAB:
                agent_id, tab_id = refs.tab_parts(identity)
                views[key] = {"type": kind, "id": identity, "project_id": None, "label": self.tab_label(facts["title"], facts["url"]),
                              "available": True, "agent_id": agent_id, "tab_id": tab_id, "agent_label": facts["agent"]["display_name"]}
                continue
            view = {"type": kind, "id": identity, "project_id": project, "label": facts["label"], "available": True}
            if kind == refs.TYPE_BROWSER:
                view["viewer_url"] = facts["viewer_url"]
                view.update({size: facts[size] for size in ("width", "height") if size in facts})
            else:
                view["project_slug"] = facts["project_slug"]
                if kind == refs.TYPE_TASK:
                    view["state"] = facts["state"]
            views[key] = view
        return views
