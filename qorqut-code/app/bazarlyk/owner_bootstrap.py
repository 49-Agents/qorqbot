"""Context-only orientation read for owner-directed code sessions.

One bounded, side-effect-free read that tells a freshly started owner CLI
conversation which business it is bound to, where that business's code and
engineering authority live, what the shared published Context says, and what
else can be fetched on demand (docs/plans/OWNER-DIRECTED-CODE-SESSIONS.md §4,
§5). It deliberately composes current business/document stores directly instead
of `AgentConfigResolver`: there is no managed profile, no role work loop, no
task/Signoff/inbox inventory, no watcher and no write. Identities (owner,
coordinator) come from current data, never from literals. Businesses own
resources only: the ``business_agent`` and ``dev`` entries stay in the shape and
always report the retired business peers as unavailable.

The one current Context brief is the shared published graph: the runtime supplies the published-Context ``home_text``
provider (the same one managed-agent bootstrap uses) and its bounded text of the one shared graph is the ``context_home``
brief with source ``context:published``. It is a derived graph projection, so it carries no document id, path or hash;
its text names the published revision and the node and relationship references it shows, and says explicitly when the
graph is not initialized, unavailable or empty. A provider or status that is not ready is reported in ``missing`` so it
is never mistaken for an empty graph. The authored Product, ICP and Brand documents are ordinary scoped sources: they
are neither read here nor a fallback when the graph cannot be read, and appear only in the registered-metadata
``context_index`` like any other business document.

Every expected source that cannot be read is reported in ``missing`` rather
than silently dropped, so a session never claims to be up to speed on a brief
it never saw.
"""

from bazarlyk.agents.global_scope import reaches_business
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.application.plans import schema
from qorqut_context.integration.reading.text import TRUNCATED

NAME = "owner_session_bootstrap"
MAX_CHARACTERS = 40000
MAX_INDEX_ITEMS = 50

GUIDANCE = (
    "Deeper context is fetched on demand: start with context_home, search context_find, then read"
    " context_object and context_expand. This orientation is an index, not the whole graph.",
    "Project direction belongs to the owner with Ordubashy, who delegates bounded work to the permanent workers;"
    " the project's notes are listed with notes_list (target_business_id) and read with notes_get. Projects have no"
    " resident agents: Ordubashy coordinates execution across projects and the global agents work here when the owner directs them.",
    "Direct coordination uses agents_message_recipients, agents_message_send and agents_messages_list"
    " within this project; create and assign same-business tasks when work needs durable follow-up. These"
    " messages are distinct from the owner_conversation private discussion attached to a task,"
    " Signoff or email object.",
    "No autonomous work loop is started and nothing is claimed, announced, handed off or reported"
    " automatically. Read a task or Signoff only when the owner's current request refers to it.",
    "A specific owner instruction (for example \"create these tasks\" or \"complete this task\") authorizes that"
    " Qorqut operation without a second confirmation; ask only for missing scope or a materially different"
    " action. Tool access alone never authorizes unrelated writes, and current revisions, idempotency keys,"
    " state transitions and excluded operations still apply.",
)
OWNER_FALLBACK = "the installation owner"


class OwnerSessionBootstrap:
    """Assembles the orientation from the current business and document stores."""

    def __init__(self, documents, context_home=None, context_status=None):
        self.documents = documents
        # Optional published-Context providers: ``context_home(business_id) -> Markdown or None`` (the graph is shared,
        # so the text does not depend on the business) and ``context_status() -> {"status", "revision", "reason"}``.
        self.context_home = context_home
        self.context_status = context_status

    def register(self, registry):
        registry.register(OperationDescriptor(NAME, schema(), effect_kind="read", supports_signoff=False,
            scope_kinds=frozenset({"business"}), exposure=frozenset({"mcp"}),
            read=lambda ctx, arguments, options, uow: self.read(ctx, uow),
            description="Orientation for an owner-directed code session: the bound project, its coordinator,"
                        " its engineering authority, registered repositories and code root, the shared"
                        " published Context home, and a bounded index of further context. Returns no tasks,"
                        " Signoffs or inbox data and changes nothing."))
        return self

    def read(self, ctx, uow):
        business = uow.repository("businesses").get(ctx.business_id, required=True)
        missing = []
        engineering = self._engineering(business, missing)
        home = self._home_brief(business, missing)
        briefs = home
        coordination = self._coordination(business, uow)
        principal = uow.repository("principals").get(ctx.principal_id)
        owner = (f"{principal['display_name']}, the installation owner"
                 if principal and principal["display_name"].strip() else OWNER_FALLBACK)
        return {"business": {"id": business["id"], "slug": business["slug"], "name": business["name"],
                             "stage": business["stage"], "timezone": business["timezone"],
                             "revision": business["revision"]},
                "engineering": engineering, "briefs": briefs,
                "context_index": self._index(ctx, uow, missing),
                "coordination": coordination,
                "guidance": [f"Selected business: {business['name']} ({business['slug']}, {business['id']})."
                             f" Every operation in this session is bound to it and acts as {owner}.",
                             self._peer_line(), *GUIDANCE],
                "missing": missing,
                "character_count": sum(len(brief["text"]) for brief in briefs),
                "character_limit": MAX_CHARACTERS}

    @staticmethod
    def _coordination(business, uow):
        from bazarlyk.management.identity import configured_ordubashy

        operations = {"recipients": "agents_message_recipients", "send": "agents_message_send",
                      "history": "agents_messages_list", "create_task": "tasks_create",
                      "assign_task": "tasks_assign"}
        identity = configured_ordubashy(uow, require_codex=False)
        principal = uow.repository("principals").get(identity) if identity else None
        # Global business access is derived: every active business, no membership row.
        available = bool(principal and principal["active"] and reaches_business(uow.connection, identity, business["id"]))
        if available:
            reason = None
        elif identity:
            reason = "inactive_or_out_of_scope"
            identity = None
            principal = None
        else:
            reason = "not_configured"
        return {"coordinator_id": identity, "coordinator_name": principal["display_name"] if principal else None,
                "available": available, "reason": reason, "operations": operations}

    @staticmethod
    def _peer_line():
        return ("Projects have no resident project agent or Dev; do not assign this project's work to any"
                " other agent without the owner's instruction. This session remains the owner's own code session.")

    def _engineering(self, business, missing):
        config = business["config_json"]
        config = plain(config) if isinstance(config, dict) else {}
        authority = plain(config.get("engineering") or {})
        repositories = plain(config.get("code") or [])
        if not authority:
            missing.append({"source": "engineering.authority", "reason": "unconfigured",
                            "detail": "No engineering authority is registered for this project"})
        if not repositories:
            missing.append({"source": "engineering.repositories", "reason": "none_registered",
                            "detail": "No code repositories are registered in the project configuration"})
        return {"code_root": business["workspace"].rstrip("/") + "/code", "workspace": business["workspace"],
                "authority": authority, "repositories": repositories,
                "note": "Owner/Ordubashy-managed; absent targets are unconfigured. No credential values are"
                        " included here; read Settings variable names through the ordinary operations."}

    def _home_brief(self, business, missing):
        """The shared published Context home as one derived brief; a state that is not a readable graph is reported."""
        if self.context_home is None:
            missing.append({"source": "context_home", "reason": "not_configured",
                            "detail": "No published Context provider is configured for this session"})
            return []
        state = self.context_status() if self.context_status is not None else None
        if state is not None and state.get("status") != "ready":
            reason = state.get("reason") or {}
            missing.append({"source": "context_home", "reason": state.get("status"),
                            "detail": reason.get("message") or "The shared published Context is not readable"})
        text = (self.context_home(business["id"]) or "").strip()
        if not text:
            missing.append({"source": "context_home", "reason": "unavailable",
                            "detail": "The published Context provider returned no text"})
            return []
        truncated = text.endswith(TRUNCATED.strip()) or len(text) > MAX_CHARACTERS
        text = text[:MAX_CHARACTERS]
        return [{"key": "context_home", "source": "context:published", "characters": len(text),
                 "truncated": truncated, "text": text}]

    def _index(self, ctx, uow, missing):
        try:
            selected = self.documents.library.select(ctx, {"scope": "business"}, uow)
        except DomainError as exc:
            missing.append({"source": "context_index", "reason": exc.code, "detail": exc.message})
            return {"items": [], "total": 0, "truncated": False, "projection": "registered_metadata"}
        items = []
        for row in selected[:MAX_INDEX_ITEMS]:
            summary = self.documents.library.summary(row)
            items.append({"id": summary["id"], "title": summary["title"], "kind": summary["kind"],
                          "revision": summary["revision"], "content_hash": summary["content_hash"],
                          "availability": summary["availability"], "updated_at": row["updated_at"]})
        if not selected:
            missing.append({"source": "context_index", "reason": "no_documents",
                            "detail": "This project has no authored context documents yet"})
        return {"items": items, "total": len(selected), "truncated": len(selected) > len(items),
                "projection": "registered_metadata"}
