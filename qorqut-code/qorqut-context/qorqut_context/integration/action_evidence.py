"""Bounded evidence observed at Action admission, under the recording caller's read authority.

Only explicit task/document references are resolved. No network, model, comments, private notes or journals are read.
The queue freezes these observations alongside the exact Action revision; they are supporting context, not accepted
actions or claims about the state of a linked record at the Action's occurrence time.
"""
from dataclasses import replace

from qorqut_operations.contracts import DomainError, InvocationOptions, canonical_hash, utc_now


EXCERPT_BYTES = 4000
TITLE_BYTES = 1000
READS = {"task": ("tasks", "tasks_get"), "document": ("documents", "context_get")}


def clipped(value, limit):
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


class ActionEvidence:
    def __init__(self, application):
        self.application = application

    def __call__(self, ctx, revision, uow):
        refs = revision["snapshot_json"].get("refs", {})
        observed_at = utc_now()
        result = []
        for kind in READS:
            if not refs.get(kind):
                continue
            item = {"kind": kind, "id": refs[kind], "status": "unavailable"}
            try:
                item = self.read(ctx, revision, uow, kind, refs[kind])
            except (DomainError, OSError):
                # Missing, ambiguous, inaccessible and stale sources disclose the same generic result.
                # An optional reference must not prevent recording the accepted Action.
                pass
            result.append({**item, "observed_at": observed_at})
        return result

    @staticmethod
    def scope(ctx, revision, uow, table, identity):
        owning = revision["business_id"]
        candidates = {owning, None, *revision["snapshot_json"].get("business_ids", [])}
        candidates &= {None, *ctx.allowed_business_ids}
        found = [row["business_id"] for row in uow.connection.execute(
            f"SELECT business_id FROM {table} WHERE id=?", (identity,)) if row["business_id"] in candidates]
        # Task IDs are namespace-local. Prefer the Action's owning scope; never guess between other matches.
        if owning in found:
            return owning
        if len(found) != 1:
            raise DomainError("not_found", "Evidence reference is unavailable")
        return found[0]

    def read(self, ctx, revision, uow, kind, identity):
        app = self.application
        table, operation = READS[kind]
        business = self.scope(ctx, revision, uow, table, identity)
        selected = (ctx.select_business(business) if business is not None
                    else replace(ctx, business_id=None, review_business_id=None))
        arguments = ({"task_id": identity, "scope": "business" if business else "global", "notes_limit": 0}
                     if kind == "task" else
                     {"document_id": identity, "scope": "business" if business else "shared", "max_chars": EXCERPT_BYTES})
        descriptor = app.registry.get(operation)
        arguments = app.registry.bind_arguments(operation, arguments)
        namespace = app.namespace(selected, descriptor, arguments)
        decision = app.authorize(selected, descriptor, (), uow, namespace=namespace, arguments=arguments)
        if decision.mode != "allow":
            raise DomainError("forbidden", "Evidence read is not allowed")
        # Reuse the registered reader in the writer's existing transaction, not a nested Application.invoke.
        result = descriptor.read(selected, arguments, InvocationOptions(), uow)
        row = result[kind]
        if kind == "document" and (row["metadata_stale"] or row["archived"]):
            raise DomainError("revision_conflict", "Evidence source is unavailable")
        title = clipped(row["title"], TITLE_BYTES)
        body = row.get("overview", "") if kind == "task" else row["body"]
        excerpt = clipped(body, EXCERPT_BYTES)
        source = ({"title": row["title"], "overview": body, "status": row["status"]} if kind == "task" else None)
        return {"kind": kind, "id": identity, "business_id": business, "status": "available",
                "revision": row["revision"], "title": title, "excerpt": excerpt,
                "content_hash": canonical_hash(source) if source is not None else row["content_hash"],
                "hash_of": "task_fields" if kind == "task" else "document_bytes",
                **({"task_status": row["status"]} if kind == "task" else {}),
                "truncated": title != row["title"] or excerpt != body or (kind == "document" and not result["complete"])}
