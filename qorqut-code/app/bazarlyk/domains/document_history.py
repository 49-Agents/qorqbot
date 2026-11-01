"""Owner-only reads of shared document history and business activity.

Scope always comes from the selected business/Global context, never from a
namespace in the request. Each source is first resolved through its owning
domain's own access rules; only then is shared history read. Working memory is
not served here: working_memory_get alone keeps its source-visibility checks.
"""

from __future__ import annotations

from qorqut_business_docs_versioning import HistoryStore, NotFound

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from bazarlyk.storage import history

from qorqut_operations.application.plans import schema

SOURCE_KINDS = ("document", "story", "story_draft", "learning_candidate", "learning_decision",
                "context_object", "context_brief")
VERSION = {"type": "integer", "minimum": 1}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 200}
SQL_SOURCES = {"story": "stories", "story_draft": "story_drafts", "learning_candidate": "learning_candidates",
               "learning_decision": "learning_decisions"}


def _version(value, *, content):
    item = {"history_version": value.version, "content_sha256": value.content_sha256, "author_id": value.author_id,
            "created_at": value.created_at, "reason": value.reason,
            "restored_from_version": value.restored_from_version}
    if content:
        item["content"] = value.content
    return item


def _event(value):
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
    def __init__(self, documents):
        self.documents = documents

    def register(self, registry):
        common = dict(effect_kind="read", supports_signoff=False, owner_only=True, exposure=frozenset({"dashboard"}))
        registry.register(OperationDescriptor("document_history_get",
            schema({"source_kind": {"enum": list(SOURCE_KINDS)},
                    "source_id": {"type": "string", "minLength": 1, "maxLength": 600},
                    "version": VERSION, "from_version": VERSION, "to_version": VERSION,
                    "before_version": VERSION, "limit": LIMIT}, ("source_kind", "source_id")),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=lambda c, _a: "business:" + c.business_id if c.business_id else "global",
            description="Owner: shared content history of one document in the selected scope: one version, a diff "
                        "between two versions, or a page of version metadata (no contents).",
            read=self.get, **common))
        registry.register(OperationDescriptor("business_history_list",
            schema({"source": {"enum": ["core", "context"]}, "before_sequence": VERSION, "limit": LIMIT}),
            scope_kinds=frozenset({"business"}), resolve_namespace=lambda c, _a: "business:" + c.business_id,
            description="Owner: the selected business's recorded activity facts, newest first, from one source "
                        "(core or context); each source has its own sequence and cursor.",
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
        if kind in {"context_object", "context_brief"}:
            return self._context(ctx, kind, source_id, mode, args)
        space, document_id, revision = self._core_source(ctx, kind, source_id, uow)
        store = HistoryStore(uow.connection)
        try:
            result = self._read(store, space, document_id, mode, args)
        except NotFound as exc:
            raise DomainError("not_found", "No recorded history for this document version") from exc
        return {"source_kind": kind, "source_id": source_id, "current_revision": revision, **result}

    def _core_source(self, ctx, kind, source_id, uow):
        """(namespace, history document ID, current domain revision) after the owning domain's own checks."""
        space = history.namespace(ctx.business_id)
        if kind == "document":
            saved = uow.connection.execute("SELECT kind FROM documents WHERE id=?", (source_id,)).fetchone()
            if saved and saved["kind"] == history.WORKING_MEMORY:
                raise DomainError("forbidden", "Working-memory history is read with working_memory_get")
            row = self.documents.store.get(ctx, source_id, uow)
            return history.namespace(row["business_id"]), row["id"], row["revision"]
        table = SQL_SOURCES[kind]
        row = uow.repository(table, business_id=ctx.business_id).get(source_id)
        prefix = history.SQL_DOCUMENTS[table][0]
        document_id = f"{prefix}:{source_id}"
        if row is None:
            # A deleted row keeps its history only in the scope that owned it.
            if history.latest(uow.connection, space, document_id) is None:
                raise DomainError("not_found", "Document not found")
            return space, document_id, None
        return space, history.sql_document_id(table, row), row.get("revision")

    @staticmethod
    def _read(store, space, document_id, mode, args):
        if mode == "version":
            return {"history_document_id": document_id,
                    "version": _version(store.get_document(space, document_id, version=args["version"]), content=True)}
        if mode == "diff":
            return {"history_document_id": document_id, "from_version": args["from_version"],
                    "to_version": args["to_version"],
                    "diff": store.diff_document(space, document_id, args["from_version"], args["to_version"])}
        page = store.list_versions(space, document_id, before_version=args.get("before_version"),
                                   limit=args.get("limit", 50))
        return {"history_document_id": document_id, "versions": [_version(item, content=False) for item in page.versions],
                "next_before_version": page.next_before_version}

    def _context(self, ctx, kind, source_id, mode, args):
        service = self._context_service()
        return {"source_kind": kind, "source_id": source_id,
                **service.history(kind, source_id, business_id=ctx.business_id, mode=mode, args=args,
                                  read=self._read)}

    def _context_service(self):
        operations = getattr(self.documents, "context_base", None)
        if operations is None:
            raise DomainError("operation_unavailable", "The context base is not configured on this installation")
        return operations.require()

    # business_history_list

    def events(self, ctx, args, options, uow):
        self.owner(ctx, uow)
        source = args.get("source", "core")
        if source == "context":
            page = self._context_service().history_events(ctx.business_id, before_sequence=args.get("before_sequence"),
                                                           limit=args.get("limit", 50))
        else:
            page = HistoryStore(uow.connection).list_events(ctx.business_id, before_sequence=args.get("before_sequence"),
                                                            limit=args.get("limit", 50))
        return {"source": source, "events": [_event(item) for item in page.events],
                "next_before_sequence": page.next_before_sequence}
