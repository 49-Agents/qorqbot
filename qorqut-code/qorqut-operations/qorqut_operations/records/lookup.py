"""The neutral item lookups: how a shared ``{"type", "id"}`` item is resolved, canonicalised, fingerprinted and enumerated,
with the private-source, availability and membership rules of the records every host has.

Registering these kinds with the ``ItemResolver`` grants nothing: ``resource`` keeps the owner-only journal, excluded
working-memory and active-member guards, ``resolve`` authorizes the domain's own read operation (``authorize_item_read``)
at the original read-check points, and ``fingerprint`` first performs that same authorized read. The dependencies are
explicit; nothing here constructs or reaches the Task service, and published shared Context readability grants none of
these private operational reads.

An ``agent`` item is a descriptive endpoint, not an assignment. A global's profile has no home and is read in Global and in
any active selected business; it is current only as an active supported global. A business-home profile is history, read
only in its original business. Retired, inactive, unsupported-role, malformed and business-home profiles resolve as
archived, so the ``include_archived`` contract keeps them out of normal lists.

The host contributes the records and rules of its own integrations through ``LookupHost``: every method is required and
is called at one fixed point of the flow below. Nothing is discovered by attribute or module."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from qorqut_operations.actions.writeback import linked_action, linked_action_items
from qorqut_operations.application.plans import ref, rows
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, ExpectedTarget, canonical_hash
from qorqut_operations.items import authorize_item_read, item_revision
from qorqut_operations.records.vocabulary import RAW_KINDS, document_type
from qorqut_operations.tags import item_labels

from qorqut_operations.documents.journal_access import JOURNAL_KINDS, is_owner
from qorqut_operations.documents.materials import classification
from qorqut_operations.documents.people import details

# Documents of these kinds are runtime or host-private records, never offered as relationship candidates.
NOT_CANDIDATES = {"working_memory", "email_message", "context_index", "context_map", "plan", "shared_instruction",
                  "shared_instructions", "legacy_agent_document", "legacy_request"}
# The resource tables whose labels live in the shared tag catalog, with the catalog's item type for each.
LABELLED = {"tasks": "task", "action_logs": "action"}


class LookupHost(Protocol):
    """The host's records and rules, each consulted at one fixed point of ``RecordItems`` and ``RecordCandidates``.

    ``material_resource`` is the host's own ``(table, row)`` for a ``material`` identity, or ``None``: called only for a
    material, after both document privacy checks and the document-kind match, and before the person-membership fallback.
    ``current_agent`` says whether a stored ``agent_profiles`` row is a current agent (only asked for that table).
    ``fallback_subtype`` is the subtype of a host row that names neither a material type nor a kind (asked lazily).
    ``relationship_item`` is the canonical relationship endpoint of the already resolved ``(table, row)``.
    ``fingerprint_content`` adds a host table's content to a fingerprint and returns whether it handled the table; an
    unhandled table reads nothing. ``include_candidate`` says whether a resource table is enumerated in this scope."""

    def material_resource(self, ctx, identity: str, uow) -> tuple[str, Mapping[str, Any]] | None: ...

    def current_agent(self, uow, row: Mapping[str, Any]) -> bool: ...

    def fallback_subtype(self, table: str, row: Mapping[str, Any]) -> str | None: ...

    def relationship_item(self, ctx, item: Mapping[str, Any], table: str, row: Mapping[str, Any], uow) -> Mapping[str, Any]: ...

    def fingerprint_content(self, ctx, table: str, row: Mapping[str, Any], content: dict[str, Any], uow) -> bool: ...

    def include_candidate(self, ctx, table: str) -> bool: ...


def agent_row(ctx, uow, identity):
    """The stored profile an ``agent`` item names in this scope: the business's own historical row, else a global's row
    (no home) in Global or an active selected business. A business-home row is never read outside its business."""
    if ctx.business_id is not None:
        row = uow.repository("agent_profiles", business_id=ctx.business_id).get(identity)
        if row is not None or ctx.business_id not in active_business_ids(uow.connection):
            return row
    return uow.repository("agent_profiles", business_id=None).get(identity)


def stored_profile(row):
    """The decoded stored profile, or an empty one when it is malformed (it then reads only as archived history)."""
    profile = row.get("profile_json")
    return profile if isinstance(profile, dict) else {}


class RecordItems:
    """``ItemLookup`` over the neutral records. ``related`` is the related-resource resolver, ``documents`` the document
    service (its store reads files and hashes), ``resource_types`` the ordered ``resource table -> item kind`` map of the
    host's record vocabulary and ``host`` its ``LookupHost``."""

    def __init__(self, application, documents, related, resource_types, host):
        if None in (application, documents, related, resource_types, host):
            raise ValueError("Record items need the Application, the document service, the related resources, "
                             "the resource types and the lookup host")
        self.application, self.documents, self.related = application, documents, related
        self.resource_types, self.host = resource_types, host

    def authorize(self, ctx, operation, uow):
        authorize_item_read(self.application, ctx, operation, uow)

    def resource(self, ctx, item, uow):
        kind, identity = item["type"], item["id"]
        if kind in {"person", "topic", "material"}:
            row = uow.repository("documents", business_id=ctx.business_id).get(identity)
            if row and row['kind'] in JOURNAL_KINDS and not is_owner(ctx, uow):
                raise DomainError('not_found', 'Context item does not exist in this project')
            if row and row['kind'] == 'working_memory':
                raise DomainError('not_found', 'Context item does not exist in this project')
            if row and document_type(row) == kind:
                return "documents", row
            if kind == "material":
                found = self.host.material_resource(ctx, identity, uow)
                if found: return found
            if kind == "person":
                member = uow.repository("business_memberships", business_id=ctx.business_id).get(identity)
                row = uow.repository("principals").get(identity)
                if member and row and row["kind"] == "human": return "principals", row
        else:
            table = next((key for key, value in self.resource_types.items() if value == kind), None)
            if table == "action_logs":
                # Canonical action_id or legacy bare ID, authorized by membership.
                row = linked_action(ctx, uow, identity)
                if row: return table, row
            elif table == "agent_profiles":
                row = agent_row(ctx, uow, identity)
                if row: return table, row
            elif table:
                row = uow.repository(table, business_id=ctx.business_id).get(identity)
                if row: return table, row
        raise DomainError("not_found", "Context item does not exist in this project")

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        if table == "context_entities":
            self.authorize(ctx, "context_items_get", uow)
            view = {"label": row["label"], "url": row["url"], "revision": row["revision"]}
        elif table in {"principals", "agent_profiles"}:
            self.authorize(ctx, "assignees_list" if table == "principals" else "agents_get", uow)
            view = {"label": row.get("display_name") or stored_profile(row).get("display_name") or row.get("name"), "revision": row["revision"]}
        else:
            view = self.related.get(ctx, uow, table, item["id"], builder, allow_busy=allow_busy)
        if builder:
            if table == "action_logs":
                # The action's true storage identity; a Global-stored action is a read-only Global reference.
                builder.expect(ref(table, row["id"], row["business_id"], None if row["business_id"] else "global"), row)
            elif table == "agent_profiles" and row["home_business_id"] is None:
                # A global's principal and profile; inside a business plan the profile is a read-only ecosystem pin.
                builder.expect(ref("principals", item["id"], None), uow.repository("principals").get(item["id"]))
                builder.expect(ref(table, item["id"], None, "ecosystem" if ctx.business_id else None), row)
            else:
                # A business-home profile keeps its original home pin.
                builder.expect(ref(table, item["id"], None if table == "principals" else ctx.business_id), row)
        metadata = row.get("metadata_json", {})
        material = classification(row, table)
        subtype = material.get("material_type") or row.get("kind") or self.host.fallback_subtype(table, row)
        archived = bool(row.get("archived") or metadata.get("archived") or
                        (table == "agent_profiles" and not self.host.current_agent(uow, row)) or
                        (table == "principals" and (not row["active"] or not uow.repository("business_memberships", business_id=ctx.business_id).get(item["id"])["active"])))
        person = {}
        revision = item_revision(row)
        if item["type"] == "person":
            person = details(row, uow, business_id=ctx.business_id)
            if table == "principals":
                member = uow.repository("business_memberships", business_id=ctx.business_id).get(item["id"])
                revision = member["revision"]
                if builder:
                    builder.expect(ref("business_memberships", item["id"], ctx.business_id), member)
            person.update({key: metadata.get(key, "") for key in ("name", "role", "org", "about")})
            person["aka"] = metadata.get("aka", [])
        return {**view, **item, **material, "revision": revision, "resource_type": table, "resource_id": item["id"], "subtype": subtype,
                "archived": archived, "raw_source": bool(metadata.get("raw_source") or row.get("kind") in RAW_KINDS),
                "summary": metadata.get("filer_summary") or metadata.get("summary", ""), "created_at": row.get("created_at"), "updated_at": row.get("updated_at"), **person}

    def project_inventory_window(self, ctx, uow, *, kind, project_id, source_business_id, include_links,
                                 after, limit):
        """Bounded identity/sort metadata for the core ``agent`` contribution of a Project inventory.

        Only identifiers, their actual home scope and the profile's source timestamp are selected. The ordinary
        authorized ``resolve`` remains the only way this identity becomes an item; selecting metadata grants nothing.
        """
        if kind != "agent":
            raise DomainError("operation_unavailable", f"Project inventory metadata is unavailable for {kind}")
        limit = max(1, min(int(limit), 100))
        scope = source_business_id
        if scope is None:
            owned = "SELECT agent_id id,home_business_id business_id,updated_at ts FROM agent_profiles " \
                    "WHERE home_business_id IS NULL"
            values = []
        else:
            # A Global agent is readable inside an active business by the ordinary agent lookup; retain that existing
            # canonical business scope while enumerating only this source's own and Global profiles.
            owned = "SELECT agent_id id,? business_id,updated_at ts FROM agent_profiles " \
                    "WHERE home_business_id IS ? OR home_business_id IS NULL"
            values = [scope, scope]
        sql = owned
        if include_links:
            sql += (" UNION ALL SELECT l.target_id id,l.target_business_id business_id,"
                    "COALESCE(a.updated_at,a.created_at) ts FROM project_item_links l "
                    "JOIN agent_profiles a ON a.agent_id=l.target_id "
                    "WHERE l.business_id=? AND l.target_type='agent'")
            values += [project_id]
        grouped = ("SELECT type,scope,id,MAX(ts) ts FROM ("
                   + "SELECT 'agent' type,COALESCE(business_id,'') scope,id,ts FROM (" + sql + ") metadata"
                   + ") GROUP BY type,scope,id")
        where = ""
        if after is not None:
            ts, kind_name, after_scope, identity = after
            after_scope = "" if after_scope is None else after_scope
            where = " WHERE (ts<? OR (ts=? AND (type>? OR (type=? AND (scope>? OR (scope=? AND id>?))))))"
            values += [ts, ts, kind_name, kind_name, after_scope, after_scope, identity]
        rows = [dict(row) for row in uow.connection.execute(
            f"SELECT * FROM ({grouped}){where} ORDER BY ts DESC,type ASC,scope ASC,id ASC LIMIT ?",
            [*values, limit + 1])]
        result = [{"type": "agent", "id": row["id"],
                   "business_id": None if row["scope"] in (None, "") else row["scope"],
                   "updated_at": row["ts"]} for row in rows[:limit]]
        return {"rows": result, "has_more": len(rows) > limit}

    def relationship_item(self, ctx, item, uow):
        table, row = self.resource(ctx, item, uow)
        return self.host.relationship_item(ctx, item, table, row, uow)

    def fingerprint(self, ctx, item, uow, builder=None):
        self.resolve(ctx, item, uow, builder)
        table, row = self.resource(ctx, item, uow)
        if table == "documents":
            store = self.documents.store
            document = store.view(row, uow)
            metadata = document["metadata"]
            content = {"content": document.get("body", document["content_hash"]),
                "title": metadata.get("title"), "source": metadata.get("source"),
                "notes": metadata.get("notes"), "topic": metadata.get("topic")}
            if row["kind"] not in RAW_KINDS:
                content["description"] = {key: metadata.get(key) for key in ("name", "summary", "filer_summary", "filer_evidence", "about", "aka", "org", "role", "material_type", "affiliation", "contact")}
            if builder:
                file = store.file_ref(row["id"], row["path"], ctx.business_id)
                builder.targets[file] = ExpectedTarget(file, content_hash=document["content_hash"], exists=True)
            return canonical_hash(content)
        # Review decisions and their counters are intentionally excluded from content.
        content = {key: value for key, value in row.items() if key not in {"revision", "created_at", "updated_at", "sequence", "assignees_json", "pending_signoff_id"}}
        if table == "signoffs":
            # payload_hash already identifies the immutable binary prepared proposal.
            content.pop("change_set", None)
        if self.host.fingerprint_content(ctx, table, row, content, uow):
            pass
        elif table == "principals":
            content["person"] = details(row, uow, business_id=ctx.business_id)
        elif table == "tasks":
            content["notes"] = rows(uow, "thread_messages", ctx.business_id, where="item_type='task' AND item_id=?", parameters=(row["id"],))
            content["responsibility"] = sorted((edge["role"], edge["to_type"], edge["to_id"]) for edge in rows(uow, "relationships", ctx.business_id,
                where="from_type='task' AND from_id=? AND role IN ('assigned_to','blocked_by')", parameters=(row["id"],)))
        elif table == "data_tables":
            content["rows"] = rows(uow, "data_rows", ctx.business_id, where="table_id=?", parameters=(row["id"],))
        if table in LABELLED:
            content["tags"] = item_labels(uow, row["business_id"], LABELLED[table], row["id"])
        return canonical_hash(content)


class RecordCandidates:
    """``ItemCandidates``: enumeration in v1's order (resource tables, then documents, then human members). Listing a
    candidate grants nothing; the relationship service resolves each one before it exposes any detail. A selected extension's
    lookup that enumerates its own kinds (``candidates(ctx, uow, kind)``) is listed after the resource tables.
    ``resource_types`` is the same ordered map as the lookup's and ``host`` its ``LookupHost`` (``include_candidate``)."""

    def __init__(self, resource_types, host, extensions=()):
        if None in (resource_types, host):
            raise ValueError("Record candidates need the resource types and the lookup host")
        self.resource_types, self.host, self.extensions = resource_types, host, tuple(extensions)

    def candidates(self, ctx, uow, kind=None) -> Iterator[dict[str, str]]:
        for table, item_type in self.resource_types.items():
            if not self.host.include_candidate(ctx, table): continue
            if kind and kind != item_type: continue
            if table == "action_logs":
                for identity in linked_action_items(ctx, uow):
                    yield {"type": item_type, "id": identity}
                continue
            if table == "agent_profiles":
                # A business's own historical rows, then every global's; resolution says which are current.
                for business in ([ctx.business_id] if ctx.business_id is None else [ctx.business_id, None]):
                    for row in rows(uow, table, business):
                        yield {"type": item_type, "id": row["agent_id"]}
                continue
            for row in rows(uow, table, ctx.business_id):
                yield {"type": item_type, "id": row.get("id", row.get("agent_id"))}
        for contribution in self.extensions:
            if (not kind or kind in contribution.kinds) and callable(getattr(contribution.lookup, "candidates", None)):
                yield from contribution.lookup.candidates(ctx, uow, kind)
        if not kind or kind in {"person", "topic", "material"}:
            for row in rows(uow, "documents", ctx.business_id):
                if row["kind"] in NOT_CANDIDATES: continue
                item_type = document_type(row)
                if not kind or kind == item_type: yield {"type": item_type, "id": row["id"]}
        if not kind or kind == "person":
            for member in rows(uow, "business_memberships", ctx.business_id):
                principal = uow.repository("principals").get(member["principal_id"])
                if principal and principal["kind"] == "human": yield {"type": "person", "id": principal["id"]}
