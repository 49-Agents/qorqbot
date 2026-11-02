"""Owner-requested, visibility-gated direct journal reads for the configured Global coordinator."""

from __future__ import annotations

import hashlib

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PAGE, page, schema
from qorqut_operations.contracts import DomainError
from .journal_access import JOURNAL_KINDS, entries


class JournalOperations:
    """The direct journal reads. ``coordinator_id(uow)`` is the host's binding of the configured Global coordinator's identity;
    the caller, home, scope and delegation restrictions below are unchanged by where that identity comes from."""

    def __init__(self, store, coordinator_id):
        if not callable(coordinator_id):
            raise TypeError("The journal operations need the host's coordinator_id(uow)")
        self.store, self._coordinator_id = store, coordinator_id

    def register(self, registry):
        shared = dict(scope_kinds={"global"}, global_capability="read_global_context",
                      resolve_namespace=lambda _ctx, _args: "global", exposure={"mcp"})
        registry.register(OperationDescriptor(
            "journal_entries_list", schema(PAGE), effect_kind="read", supports_signoff=False,
            description="When the owner asks, list visible journal entry titles and dates so the named entry can be read. Does not change review or filing state.",
            read=lambda c, a, o, u: self.read_list(c, a, u), **shared))
        registry.register(OperationDescriptor(
            "journal_entry_get", schema({"document_id": {"type": "string", "format": "uuid"}}, ("document_id",)),
            effect_kind="read", supports_signoff=False,
            description="When the owner asks, return the complete current visible journal entry with its title, date, checklist state, revision and content hash. Never return a diff or excerpt.",
            read=lambda c, a, o, u: self.read_entry(c, a, u), **shared))
        return self

    def _coordinator(self, ctx, uow):
        identity = self._coordinator_id(uow)
        if (ctx.principal_kind != "agent" or ctx.principal_id != identity
                or ctx.home_business_id is not None or ctx.business_id is not None
                or ctx.delegation_assignment_id):
            raise DomainError("forbidden", "Direct journal reads belong to the configured Global Ordubaşı")

    def _entry(self, ctx, document_id, uow):
        saved = uow.connection.execute("SELECT business_id FROM documents WHERE id=?", (document_id,)).fetchone()
        row = (uow.repository("documents", business_id=saved["business_id"]).get(document_id)
               if saved else None)
        if (not row or row["kind"] not in JOURNAL_KINDS or row["archived"]
                or row["metadata_json"].get("ordubashy_visible", True) is not True
                or row["business_id"] and row["business_id"] not in ctx.allowed_business_ids):
            # Do not reveal whether an inaccessible or stale identifier exists.
            raise DomainError("not_found", "Journal entry is unavailable")
        raw = self.store.read_bytes(row, uow)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row["content_hash"].removeprefix("sha256:"):
            raise DomainError("revision_conflict", "Journal entry is unavailable because its registered revision is stale")
        return row

    def read_list(self, ctx, args, uow):
        self._coordinator(ctx, uow)
        found = [row for row in entries(uow)
                 if row["metadata_json"].get("ordubashy_visible", True) is True
                 and (not row["business_id"] or row["business_id"] in ctx.allowed_business_ids)]
        found.sort(key=lambda row: (str(row["metadata_json"].get("date") or row["created_at"]), row["id"]), reverse=True)
        items = [{"document_id": row["id"], "title": row["metadata_json"].get("title") or "Daily",
                  "date": row["metadata_json"].get("date") or row["created_at"][:10]}
                 for row in found]
        selected, cursor = page(items, args, None, "journal_entries_list",
                                key=lambda item: (item["date"], item["document_id"]))
        return {"items": selected, "next_cursor": cursor,
                "instruction": "Read an entry only when the owner asks. This listing is not a review or filing marker."}

    def read_entry(self, ctx, args, uow):
        self._coordinator(ctx, uow)
        row = self._entry(ctx, args["document_id"], uow)
        view = self.store.view(row, uow)
        metadata = view["metadata"]
        return {"entry": {"document_id": row["id"], "title": metadata.get("title") or "Daily",
                           "date": metadata.get("date") or row["created_at"][:10],
                           "body": view.get("body", ""), "statuses": metadata.get("statuses", {}),
                           "document_revision": row["revision"], "content_hash": view["content_hash"]}}
