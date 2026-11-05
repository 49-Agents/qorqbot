"""Owner-only reads of shared document history and business activity.

Scope always comes from the selected business/Global context, never from a namespace in the request. Each source is first
resolved through its owning domain's own access rules; only then is shared history read. A host passes the resolvers of the
sources it owns and the kinds whose history is private to their own reader, so the readers here know neither. The shared
Context's history is its published Git graph and is read with ``context_object`` and ``context_changes``, not here."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_business_docs_versioning import HistoryStore, NotFound

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError
from .text import namespace

VERSION = {"type": "integer", "minimum": 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}


def version_item(value, *, content):
    item = {"history_version": value.version, "content_sha256": value.content_sha256, "author_id": value.author_id,
            "created_at": value.created_at, "reason": value.reason,
            "restored_from_version": value.restored_from_version}
    if content:
        item["content"] = value.content
    return item


def event_item(value):
    return {"sequence": value.sequence, "event_id": value.event_id, "kind": value.kind, "actor_id": value.actor_id,
            "summary": value.summary, "subject_type": value.subject_type, "subject_id": value.subject_id,
            "data": value.data, "occurred_at": value.occurred_at, "recorded_at": value.recorded_at}


def selection(args):
    """Exactly one of: one version, a from/to diff, or metadata pagination (the default)."""
    version = "version" in args
    diff = "from_version" in args or "to_version" in args
    page = "before_version" in args or "limit" in args
    if version + diff + page > 1:
        raise DomainError("validation_error", "Choose one of version, from_version/to_version, or before_version/limit")
    if diff and not ("from_version" in args and "to_version" in args):
        raise DomainError("validation_error", "A diff needs both from_version and to_version")
    return "version" if version else "diff" if diff else "page"


class DocumentHistory:
    """``store`` is the document store whose access rules resolve a ``document`` source. ``source_resolvers`` maps each further
    source kind, in its public enum order after ``document``, to ``resolver(ctx, source_id, uow) -> (namespace, history
    document ID, current revision)``. ``private_kinds`` maps a document kind whose history another reader serves to the message
    that points to that reader."""

    def __init__(self, store, *, source_resolvers, private_kinds):
        resolvers, private = dict(source_resolvers), dict(private_kinds)
        if "document" in resolvers or any(not callable(resolver) for resolver in resolvers.values()):
            raise TypeError("Further history sources are callable resolvers; the document source is built in")
        if any(not isinstance(kind, str) or not isinstance(message, str) for kind, message in private.items()):
            raise TypeError("A private kind maps to the message naming its own reader")
        self.store, self.resolvers, self.private_kinds = store, resolvers, private
        self.source_kinds = ("document", *resolvers)

    def register(self, registry):
        common = dict(effect_kind="read", supports_signoff=False, owner_only=True, exposure=frozenset({"dashboard"}))
        registry.register(OperationDescriptor("document_history_get",
            schema({"source_kind": {"enum": list(self.source_kinds)},
                    "source_id": {"type": "string", "minLength": 1, "maxLength": 600},
                    "version": VERSION, "from_version": VERSION, "to_version": VERSION,
                    "before_version": VERSION, "limit": LIMIT}, ("source_kind", "source_id")),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=lambda c, _a: "business:" + c.business_id if c.business_id else "global",
            description=instruction_text('reads-owner-shared-content-history-of-one-document-in'),
            read=self.get, **common))
        registry.register(OperationDescriptor("business_history_list",
            schema({"source": {"enum": ["core"]}, "before_sequence": VERSION, "limit": LIMIT}),
            scope_kinds=frozenset({"business"}), resolve_namespace=lambda c, _a: "business:" + c.business_id,
            description=instruction_text('reads-owner-the-selected-project-s-recorded-activity-facts'),
            read=self.events, **common))
        return self

    @staticmethod
    def owner(ctx, uow):
        installation = uow.repository("installation").get("service")
        if ctx.principal_kind != "human" or not installation or installation["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Only the installation owner can read document history")

    # document_history_get

    def get(self, ctx, args, options, uow):
        self.owner(ctx, uow)
        mode = selection(args)
        kind, source_id = args["source_kind"], args["source_id"]
        space, document_id, revision = self._core_source(ctx, kind, source_id, uow)
        store = HistoryStore(uow.connection)
        try:
            result = self._read(store, space, document_id, mode, args)
        except NotFound as exc:
            raise DomainError("not_found", "No recorded history for this document version") from exc
        return {"source_kind": kind, "source_id": source_id, "current_revision": revision, **result}

    def _core_source(self, ctx, kind, source_id, uow):
        """(namespace, history document ID, current domain revision) after the owning domain's own checks."""
        if kind == "document":
            saved = uow.connection.execute("SELECT kind FROM documents WHERE id=?", (source_id,)).fetchone()
            if saved and saved["kind"] in self.private_kinds:
                raise DomainError("forbidden", self.private_kinds[saved["kind"]])
            row = self.store.get(ctx, source_id, uow)
            return namespace(row["business_id"]), row["id"], row["revision"]
        return self.resolvers[kind](ctx, source_id, uow)

    @staticmethod
    def _read(store, space, document_id, mode, args):
        if mode == "version":
            return {"history_document_id": document_id,
                    "version": version_item(store.get_document(space, document_id, version=args["version"]), content=True)}
        if mode == "diff":
            return {"history_document_id": document_id, "from_version": args["from_version"],
                    "to_version": args["to_version"],
                    "diff": store.diff_document(space, document_id, args["from_version"], args["to_version"])}
        page = store.list_versions(space, document_id, before_version=args.get("before_version"),
                                   limit=args.get("limit", 50))
        return {"history_document_id": document_id, "versions": [version_item(item, content=False) for item in page.versions],
                "next_before_version": page.next_before_version}

    # business_history_list

    def events(self, ctx, args, options, uow):
        self.owner(ctx, uow)
        source = args.get("source", "core")
        page = HistoryStore(uow.connection).list_events(ctx.business_id, before_sequence=args.get("before_sequence"),
                                                        limit=args.get("limit", 50))
        return {"source": source, "events": [event_item(item) for item in page.events],
                "next_before_sequence": page.next_before_sequence}
