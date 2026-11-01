"""The retained domains' item lookups: how a shared ``{"type", "id"}`` item is resolved, canonicalised, fingerprinted
and enumerated, with each domain's private-source, availability and membership rules.

Registering these kinds with the Operations ``ItemResolver`` grants nothing: ``resource`` keeps the owner-only journal,
excluded working-memory, Global-mailbox discussion, active-member and active-agent guards, ``resolve`` authorizes the
domain's own read operation (``authorize_item_read``) at the original read-check points, and ``fingerprint`` first
performs that same authorized read. The dependencies are explicit; nothing here constructs or reaches the Story
service, and published shared Context readability grants none of these private operational reads."""

from qorqut_operations.application.plans import ref, rows
from qorqut_operations.contracts import DomainError, ExpectedTarget, canonical_hash
from qorqut_operations.departments import ALIASES, normalize_departments, profile_department
from qorqut_operations.items import authorize_item_read, item_revision

from ..activity.operations import linked_action, linked_action_items
from ..documents.journal_access import JOURNAL_KINDS, is_owner
from ..documents.materials import classification
from ..documents.people import details
from .vocabulary import NOT_CANDIDATES, RAW_KINDS, RESOURCE_TYPES, document_type

class HostItems:
    """``ItemLookup`` over the retained domains. ``related`` is the host's related-resource resolver, ``documents``
    the document service (its store reads files and hashes) and ``email`` the email domain whose Global-mailbox
    discussion access guards mailbox rows."""

    def __init__(self, application, documents, email, related):
        self.application, self.documents, self.email, self.related = application, documents, email, related

    def authorize(self, ctx, operation, uow):
        authorize_item_read(self.application, ctx, operation, uow)

    def resource(self, ctx, item, uow):
        kind, identity = item["type"], item["id"]
        if kind in {"person", "topic", "material"}:
            row = uow.repository("documents", business_id=ctx.business_id).get(identity)
            if row and row['kind'] in JOURNAL_KINDS and not is_owner(ctx, uow):
                raise DomainError('not_found', 'Context item does not exist in this business')
            if row and row['kind'] == 'working_memory':
                raise DomainError('not_found', 'Context item does not exist in this business')
            if row and document_type(row) == kind:
                return "documents", row
            if kind == "material":
                for table in ("email_conversations", "email_messages"):
                    row = uow.repository(table, business_id=ctx.business_id).get(identity)
                    if row and ctx.business_id is None:
                        # Ordubashy's Global mailbox: only its readers resolve these rows.
                        self.email.discussion_access(ctx, uow)
                    if row: return table, row
            if kind == "person":
                member = uow.repository("business_memberships", business_id=ctx.business_id).get(identity)
                row = uow.repository("principals").get(identity)
                if member and row and row["kind"] == "human": return "principals", row
        else:
            table = next((key for key, value in RESOURCE_TYPES.items() if value == kind), None)
            if table == "action_logs":
                # Canonical action_id or legacy bare ID, authorized by membership.
                row = linked_action(ctx, uow, identity)
                if row: return table, row
            elif table:
                row = uow.repository(table, business_id=ctx.business_id).get(identity)
                if row: return table, row
        raise DomainError("not_found", "Context item does not exist in this business")

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        if table == "outreach_events":
            view = {"label": row["summary"], "revision": row["record_revision"]}
        elif table == "context_entities":
            self.authorize(ctx, "context_items_get", uow)
            view = {"label": row["label"], "url": row["url"], "revision": row["revision"]}
        elif table == "stories":
            self.authorize(ctx, "stories_get", uow)
            from ..stories.campaigns import responsible
            view = {"label": row["title"], "state": row["state"], "revision": row["revision"], "tags": row["tags_json"],
                "responsible_agent_id": responsible(row, uow)}
        elif table == "products":
            self.authorize(ctx, "products_get", uow)
            view = {"label": row["name"], "revision": row["revision"]}
        elif table in {"principals", "agent_profiles"}:
            self.authorize(ctx, "assignees_list" if table == "principals" else "agents_get", uow)
            view = {"label": row.get("display_name") or row.get("profile_json", {}).get("display_name") or row.get("name"), "revision": row["revision"]}
        else:
            view = self.related.get(ctx, uow, table, item["id"], builder, allow_busy=allow_busy)
        if builder:
            if table == "action_logs":
                # The action's true storage identity; a Global-stored action is a read-only Global reference.
                builder.expect(ref(table, row["id"], row["business_id"], None if row["business_id"] else "global"), row)
            else:
                builder.expect(ref(table, item["id"], None if table == "principals" else ctx.business_id), row)
        metadata = row.get("metadata_json", {})
        material = classification(row, table)
        subtype = material.get("material_type") or row.get("kind") or ("message" if table == "email_messages" else None)
        archived = bool(row.get("archived") or metadata.get("archived") or
                        (table == "agent_profiles" and row["status"] != "active") or
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
        tags = row.get("tags_json", [])
        departments = normalize_departments(tags.get("departments", [])) if isinstance(tags, dict) else normalize_departments([tag for tag in tags if tag in ALIASES])
        if table == "agent_profiles": departments = [profile_department(row["profile_json"])]
        if table == "action_logs" and row.get("tag") in ALIASES: departments = normalize_departments([row["tag"]])
        return {**view, **item, **material, "departments": departments, "revision": revision, "resource_type": table, "resource_id": item["id"], "subtype": subtype,
                "archived": archived, "raw_source": bool(metadata.get("raw_source") or row.get("kind") in RAW_KINDS),
                "summary": row["attributes_json"].get("one_liner", "") if table == "products" else metadata.get("filer_summary") or metadata.get("summary", ""), "created_at": row.get("created_at"), "updated_at": row.get("updated_at"), **person}

    def relationship_item(self, ctx, item, uow):
        table, row = self.resource(ctx, item, uow)
        if table == "email_messages":
            return {"type": "material", "id": row["conversation_id"]}
        if table == "documents" and row["kind"] == "email_message":
            return {"type": "material", "id": row["metadata_json"]["conversation_id"]}
        return item

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
        content = {key: value for key, value in row.items() if key not in {"revision", "created_at", "updated_at", "sequence", "assignees_json", "pending_request_id", "pending_signoff_id"}}
        if table == "signoffs":
            # payload_hash already identifies the immutable binary prepared proposal.
            content.pop("change_set", None)
        if table == "email_conversations":
            content["messages"] = [{"id": message["id"], "message": {key: value for key, value in message["message_json"].items() if key not in {"is_read", "url"}}, "deleted": message["deleted"]}
                for message in sorted(rows(uow, "email_messages", ctx.business_id, where="conversation_id=?", parameters=(row["id"],)), key=lambda value: value["id"])]
        elif table == "principals":
            content["person"] = details(row, uow, business_id=ctx.business_id)
        elif table == "tasks":
            content["notes"] = rows(uow, "task_notes", ctx.business_id, where="task_id=?", parameters=(row["id"],))
            content["responsibility"] = sorted((edge["role"], edge["to_type"], edge["to_id"]) for edge in rows(uow, "relationships", ctx.business_id,
                where="from_type='task' AND from_id=? AND role IN ('assigned_to','blocked_by')", parameters=(row["id"],)))
        elif table == "stories":
            content["contributions"] = [{key: event[key] for key in ("id", "kind", "title", "body", "statuses_json", "data_json")}
                for event in sorted(rows(uow, "story_events", ctx.business_id, where="story_id=?", parameters=(row["id"],)), key=lambda value: value["sequence"])]
        elif table == "data_tables":
            content["rows"] = rows(uow, "data_rows", ctx.business_id, where="table_id=?", parameters=(row["id"],))
        return canonical_hash(content)


class HostItemCandidates:
    """``ItemCandidates``: enumeration in v1's order (resource tables, then documents, then human members). Listing a
    candidate grants nothing; the relationship service resolves each one before it exposes any detail."""

    def candidates(self, ctx, uow, kind=None):
        for table, item_type in RESOURCE_TYPES.items():
            if table == "email_messages" or table == "email_conversations" and ctx.business_id is None: continue
            if kind and kind != item_type: continue
            if table == "action_logs":
                for identity in linked_action_items(ctx, uow):
                    yield {"type": item_type, "id": identity}
                continue
            for row in rows(uow, table, ctx.business_id):
                yield {"type": item_type, "id": row.get("id", row.get("agent_id"))}
        if not kind or kind in {"person", "topic", "material"}:
            for row in rows(uow, "documents", ctx.business_id):
                if row["kind"] in NOT_CANDIDATES: continue
                item_type = document_type(row)
                if not kind or kind == item_type: yield {"type": item_type, "id": row["id"]}
        if not kind or kind == "person":
            for member in rows(uow, "business_memberships", ctx.business_id):
                principal = uow.repository("principals").get(member["principal_id"])
                if principal and principal["kind"] == "human": yield {"type": "person", "id": principal["id"]}
