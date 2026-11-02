"""Project inventory: the authorized, deduplicated view of one Project's owned and explicitly linked records.

Every family is read through its own canonical reader (or its domain's authorized neutral item read), never through
an arbitrary content reader: Tasks through ``tasks_get``/``tasks_list``, Signoffs through ``signoffs_get``, Actions
through ``actions_get``, documents/people/topics through the neutral resolver, tables through ``tables_get``, routines
through ``routines_get`` (Global included), System Dev reports through the owner queue or the scoped agent reader, and
stored reminders through ``owner_reminders_list``.

The page is a real bounded keyset window over one SQL union of *identities and sort metadata only*: each family
selects ``(updated_at, type, true owning scope, canonical id)``, owned and explicitly linked rows are merged by that
scoped identity, and every kept row is resolved and authorized through the canonical reader before its content is
read. SQL never serves a label, title, count or body, and authorization is never bypassed. The opaque cursor carries
the last examined total-order key ``(timestamp, type, scope, id)``, bound to the operation, Project, scan scope and
filters, so later pages continue past everything already examined with no maximum-total cap. A denied or filtered
candidate is silently skipped; the window keeps advancing until a page is full or every reader is exhausted.

The selected section only enumerates its own families (and, for ``other``, unknown extension kinds). Ownership is the
record's own scope (or an Action's existing membership), never a label; a link keeps its target's own scope and adds
no content. Reads create no message, no card and no source write.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timezone

from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, InvocationOptions, canonical_bytes
from qorqut_operations.documents.journal_access import JOURNAL_KINDS
from qorqut_operations.items import UNAVAILABLE as ITEM_UNAVAILABLE
from qorqut_operations.projects.links import ProjectTarget
from qorqut_operations.tasks.schema import CLOSED_STATES

from . import access

SECTIONS = ("all", "work", "signoffs", "actions", "documents", "tables", "automation", "other")
DOCUMENT_TYPES = frozenset({"material", "person", "topic"})
DOCUMENT_KINDS = {"context_topic": "topic", "context_person": "person"}
# Documents whose registered kind has an existing authorized read of that record. ``context_upload`` is a mutation
# staging an upload and runtime records such as working memory are never inventory documents.
SKIPPED_DOCUMENTS = frozenset({"context_upload", "context_map", "context_index", "working_memory",
                               "shared_instruction", "shared_instructions", "legacy_request", "email_message",
                               *JOURNAL_KINDS})
SECTION_OF = {"task": "work", "signoff": "signoffs", "action": "actions", "table": "tables", "routine": "automation",
              "project": "other", "dev_ticket": "other", "reminder": "other", "entity": "other"}
LINK_SECTION_TYPES = {"work": ("task",), "signoffs": ("signoff",), "actions": ("action",),
                      "documents": (*DOCUMENT_TYPES, "entity"), "tables": ("table",), "automation": ("routine",),
                      "other": ("project", "dev_ticket", "reminder")}
KNOWN_TYPES = frozenset(SECTION_OF) | DOCUMENT_TYPES
# Project link targets are the neutral kinds plus the host's own canonical record readers.
TARGET_TYPES = frozenset({"task", "signoff", "action", "routine", "reminder", "dev_ticket", "table", "project",
                          "material", "person", "topic", "entity"})
DEFAULT_LIMIT, MAX_LIMIT = 25, 100
# A page may examine more identities than it returns (denied/closed/filtered rows); the window keeps advancing
# through them until a page is full or every reader is exhausted. There is no maximum-total cap.
PAGE_EXAMINE = 4
# A single page may authorize at most this many identities while carrying hidden/closed/denied rows. It is a
# per-request work budget, never a total cap: an empty visible page still returns a cursor so the next request
# continues at the same reader frontier.
AUTHORIZATION_BUDGET = 500
# Extensions have no bounded sort-metadata hook; resolve at most this many of their candidates per request and let
# the ordinary cursor boundary continue. Unrelated sections never enumerate extension families at all.
EXTENSION_BUDGET = 100
# The reason a candidate cannot be shown is never disclosed: only these mean "skip it silently".
SKIP = frozenset(access.UNAVAILABLE) | ITEM_UNAVAILABLE | {"business_archived"}
# A whole family the caller may not read is omitted, never disclosed and never fatal to the rest of the page.
FAMILY_SKIP = SKIP | {"forbidden", "scope_required"}


def normalized(**values) -> dict:
    """One normalized item with exactly the documented fields (absent optional fields are omitted)."""
    return {key: value for key, value in values.items() if value is not None or key in {"business_id"}}


def association(owned, link=None) -> dict:
    value = {"owned": bool(owned), "linked": link is not None}
    if link is not None:
        value.update(link_id=link["id"], link_revision=link["revision"])
    return value


def owned(uow, item, project_id):
    """Derived ownership: the record's own scope, or an Action's existing business membership. Never a label."""
    if item["type"] == access.PROJECT_TYPE:
        return False
    if item["type"] == "action":
        from qorqut_operations.actions import links as action_links
        return project_id in action_links(uow, item["id"])
    return item.get("business_id") == project_id


def target_key(item) -> tuple:
    """Deduplication identity: the target's own scope is part of its identity, never its bare ID."""
    return item["type"], item["id"], item.get("business_id")


def item_key(item) -> tuple:
    """The total inventory order key: timestamp desc, then type, true owning scope and canonical id ascending."""
    ts = item.get("updated_at") or item.get("created_at") or ""
    return ts, item["type"], "" if item.get("business_id") is None else item["business_id"], item["id"]


def _instant(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def order_key(key) -> tuple:
    """A sortable form of the total order: newest first, then type/scope/id ascending."""
    ts, kind, scope, identity = key
    return -_instant(ts), kind, scope, identity


def is_after(key, after) -> bool:
    return after is None or order_key(tuple(key)) > order_key(tuple(after))


def section_of(item) -> str:
    if item["type"] in DOCUMENT_TYPES:
        return "documents"
    return SECTION_OF.get(item["type"], "other")


def _scope(value) -> str:
    return "" if value is None else value


def _unscope(value):
    return value or None


class Inventory:
    """The one Project inventory/detail reader. ``items`` is the frozen neutral item resolver of this host."""

    def __init__(self, application, items):
        self.application, self.items = application, items

    # -- canonical reads -------------------------------------------------------------------------------------------

    def read(self, ctx, operation, arguments, uow):
        """Run one registered read for the caller, authorized in exactly that scope."""
        descriptor = self.application.registry.get(operation)
        # The exact same target-namespace resolution an ordinary invocation uses: a global-only reader stays global.
        namespace = self.application.namespace(ctx, descriptor, arguments)
        self.application.authorize(ctx, descriptor, (), uow, namespace=namespace)
        return descriptor.read(ctx, arguments, InvocationOptions(), uow)

    @staticmethod
    def scope_context(ctx, scope):
        return replace(ctx, business_id=scope, review_business_id=scope)

    # -- normalized items -------------------------------------------------------------------------------------------

    @staticmethod
    def scope_names(uow, business_id):
        return access.scope_name(uow, business_id)

    def base(self, uow, *, type, id, business_id, label, revision, resource_type, state=None, summary=None,
             task_type=None, parent_id=None, created_at=None, updated_at=None, at=None, archived=False):
        slug, name = self.scope_names(uow, business_id)
        item = normalized(type=type, id=id, business_id=business_id, business_slug=slug, business_name=name,
                          label=label, revision=revision, state=state, task_type=task_type, parent_id=parent_id,
                          summary=summary, resource_type=resource_type, can_thread=type in access.THREAD_KINDS,
                          created_at=created_at, updated_at=updated_at, at=at)
        item["_archived"] = bool(archived)
        return item

    def from_neutral(self, ctx, item, view, uow):
        return self.base(uow, type=item["type"], id=item["id"], business_id=view.get("business_id", ctx.business_id),
                         label=view.get("label") or item["id"], revision=view.get("revision") or 1,
                         resource_type=view.get("resource_type") or item["type"], state=view.get("state"),
                         summary=view.get("summary"), task_type=view.get("task_type"),
                         created_at=view.get("created_at"), updated_at=view.get("updated_at"),
                         archived=view.get("archived", False))

    def from_task(self, uow, row):
        """One ``tasks_get`` row, keeping its Task type, place in that Task hierarchy and archive state."""
        return self.base(uow, type="task", id=row["id"], business_id=row["business_id"], label=row["title"],
                         revision=row["revision"], resource_type="tasks", state=row.get("status"),
                         summary=row.get("overview"), task_type=row.get("task_type") or "task",
                         parent_id=row.get("parent_id"), created_at=row.get("created_at"),
                         updated_at=row.get("updated_at"), archived=row.get("archived"))

    def from_signoff(self, uow, row):
        return self.base(uow, type="signoff", id=row["id"], business_id=row.get("review_business_id"),
                         label=row.get("title") or "Signoff", revision=row.get("status_revision") or 1,
                         resource_type="signoffs", state=row.get("state"), summary=row.get("category"),
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"))

    def from_action(self, uow, row):
        legacy = row.get("legacy") or {}
        return self.base(uow, type="action", id=row["action_id"], business_id=legacy.get("business_id"),
                         label=row.get("summary") or row.get("text") or row["action_id"],
                         revision=row.get("revision") or 1, resource_type="action_logs",
                         state="withdrawn" if row.get("withdrawn") else None, summary=row.get("text"),
                         created_at=row.get("recorded_at"), updated_at=row.get("updated_at"))

    def from_routine(self, uow, row):
        return self.base(uow, type="routine", id=row["id"], business_id=row.get("business_id"),
                         label=row.get("name") or row["id"], revision=row.get("revision") or 1,
                         resource_type="routines", state=row.get("status"), summary=row.get("schedule"),
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"),
                         archived=bool((row.get("metadata_json") or {}).get("retired")))

    def from_ticket(self, uow, row, business_id):
        return self.base(uow, type="dev_ticket", id=row["id"], business_id=business_id,
                         label=row.get("title") or row["id"], revision=row.get("revision") or 1,
                         resource_type="dev_tickets", state=row.get("status"), summary=row.get("kind"),
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"))

    def from_reminder(self, uow, row, business_id):
        return self.base(uow, type="reminder", id=row["reminder_id"], business_id=business_id,
                         label=row.get("text") or "Reminder", revision=row.get("revision") or 1,
                         resource_type="owner_reminders", state=row.get("state"), summary=row.get("text"),
                         at=row.get("at"))

    def from_project(self, row, uow):
        view = access.project_view(row)
        return self.base(uow, type="project", id=row["id"], business_id=row["id"], label=row["name"],
                         revision=row["revision"], resource_type="businesses", state=None, summary=view["summary"],
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"),
                         archived=row.get("archived_at") is not None)

    # -- identity/sort enumeration (no content) --------------------------------------------------------------------

    def _selects(self, ctx, scan, args, *, include_links=False, projects=False):
        """The identity/sort rows of the selected families; values are fixed SQL, every scope is a parameter."""
        section = args.get("section", "all")
        wanted = args.get("type")
        if wanted:
            section = section_of({"type": wanted})
        selects, params = [], []

        def add(sql, values=()):
            selects.append(sql)
            params.extend(values)

        def enabled(name):
            return section == "all" or section == name

        if enabled("work") and (not wanted or wanted == "task"):
            add("SELECT updated_at ts,'task' type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                "NULL link_revision FROM tasks WHERE business_id IS ?", (scan,))
        if enabled("signoffs") and (not wanted or wanted == "signoff"):
            scope_kind = "business" if scan is not None else "global"
            add("SELECT updated_at ts,'signoff' type,COALESCE(review_business_id,'') scope,id,1 owned,0 linked,"
                "NULL link_id,NULL link_revision FROM signoffs WHERE review_business_id IS ? AND review_scope_kind=?",
                (scan, scope_kind))
        if enabled("actions") and (not wanted or wanted == "action"):
            if scan is None:
                add("SELECT updated_at ts,'action' type,COALESCE(business_id,'') scope,action_key id,1 owned,0 linked,"
                    "NULL link_id,NULL link_revision FROM action_logs WHERE business_id IS NULL")
            else:
                add("SELECT a.updated_at ts,'action' type,COALESCE(a.business_id,'') scope,a.action_key id,1 owned,0 linked,"
                    "NULL link_id,NULL link_revision FROM action_logs a WHERE EXISTS "
                    "(SELECT 1 FROM action_businesses ab WHERE ab.action_key=a.action_key AND ab.business_id=?)", (scan,))
        if enabled("documents") and (not wanted or wanted in DOCUMENT_TYPES | {"entity"}):
            placeholders = ",".join("?" for _ in SKIPPED_DOCUMENTS)
            add("SELECT updated_at ts,CASE kind WHEN 'context_topic' THEN 'topic' WHEN 'context_person' THEN 'person' "
                "ELSE 'material' END type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                f"NULL link_revision FROM documents WHERE business_id IS ? AND kind NOT IN ({placeholders})",
                (scan, *sorted(SKIPPED_DOCUMENTS)))
            add("SELECT updated_at ts,'entity' type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                "NULL link_revision FROM context_entities WHERE business_id IS ?", (scan,))
        if enabled("tables") and (not wanted or wanted == "table"):
            add("SELECT updated_at ts,'table' type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                "NULL link_revision FROM data_tables WHERE business_id IS ?", (scan,))
        if enabled("automation") and (not wanted or wanted == "routine"):
            add("SELECT updated_at ts,'routine' type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                "NULL link_revision FROM routines WHERE business_id IS ?", (scan,))
        if enabled("other"):
            if not wanted or wanted == "dev_ticket":
                add("SELECT updated_at ts,'dev_ticket' type,COALESCE(business_id,'') scope,id,1 owned,0 linked,NULL link_id,"
                    "NULL link_revision FROM dev_tickets WHERE business_id IS ?", (scan,))
            if not wanted or wanted == "reminder":
                namespace = "business:" + scan if scan else "global"
                clause, values = "source_kind='owner_reminder' AND source_key LIKE ?", [namespace + "/%"]
                if ctx.principal_kind != "human" or ctx.delegation_assignment_id is not None:
                    clause += " AND agent_id=?"
                    values.append(ctx.principal_id)
                add("SELECT created_at ts,'reminder' type,? scope,id,1 owned,0 linked,NULL link_id,NULL link_revision "
                    "FROM telegram_outbox WHERE " + clause, [_scope(scan), *values])
        if projects and (not wanted or wanted == access.PROJECT_TYPE):
            add("SELECT updated_at ts,'project' type,id scope,id,0 owned,0 linked,NULL link_id,NULL link_revision "
                "FROM businesses WHERE archived_at IS NULL")
        if include_links and (section == "all" or section in LINK_SECTION_TYPES):
            clause, values = "", []
            if section != "all":
                allowed = LINK_SECTION_TYPES[section]
                clause = " AND (target_type IN (" + ",".join("?" for _ in allowed) + ")"
                values = list(allowed)
                if section == "other":
                    clause += " OR target_type NOT IN (" + ",".join("?" for _ in KNOWN_TYPES) + ")"
                    values += sorted(KNOWN_TYPES)
                clause += ")"
            add("SELECT COALESCE((" + self._link_ts_sql() + "),l.updated_at) ts,l.target_type type,"
                "COALESCE(l.target_business_id,'') scope,l.target_id id,0 owned,1 linked,l.id link_id,l.revision "
                "link_revision FROM project_item_links l WHERE l.business_id=?" + clause,
                [scan, *values])
        return selects, params

    @staticmethod
    def _link_ts_sql():
        """The linked target's real source change time, never the association's creation time.

        These are correlated keyset metadata lookups only: no title, body, label or authorization state is selected.
        A type without a sort metadata contract (an installed extension) falls back to the link row's own timestamp.
        """
        return """CASE l.target_type
            WHEN 'task' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM tasks x
                              WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'signoff' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM signoffs x
                                 WHERE x.review_business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'action' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM action_logs x
                                WHERE x.action_key=l.target_id)
            WHEN 'material' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM documents x
                                  WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'person' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM documents x
                                WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'topic' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM documents x
                               WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'entity' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM context_entities x
                                WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'table' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM data_tables x
                               WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'routine' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM routines x
                                 WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'dev_ticket' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM dev_tickets x
                                    WHERE x.business_id IS l.target_business_id AND x.id=l.target_id)
            WHEN 'reminder' THEN (SELECT x.created_at FROM telegram_outbox x WHERE x.id=l.target_id)
            WHEN 'project' THEN (SELECT COALESCE(x.updated_at,x.created_at) FROM businesses x WHERE x.id=l.target_id)
            ELSE NULL END"""

    @staticmethod
    def _grouped(selects):
        inner = " UNION ALL ".join(selects)
        return ("SELECT type,scope,id,MAX(ts) ts,MAX(owned) owned,MAX(linked) linked,MAX(link_id) link_id,"
                "MAX(link_revision) link_revision FROM (" + inner + ") GROUP BY type,scope,id")

    def _window(self, uow, selects, params, after, limit, *, probe=False):
        """One bounded key window after ``after`` in the total order (timestamp desc, type/scope/id asc)."""
        grouped = self._grouped(selects)
        values = list(params)
        where = ""
        if after is not None:
            ts, kind, scope, identity = after
            scope = _scope(scope)
            where = " WHERE (ts<? OR (ts=? AND (type>? OR (type=? AND (scope>? OR (scope=? AND id>?))))))"
            values += [ts, ts, kind, kind, scope, scope, identity]
        sql = f"SELECT * FROM ({grouped})" + where + " ORDER BY ts DESC,type ASC,scope ASC,id ASC LIMIT ?"
        values.append(1 if probe else limit)
        return [dict(row) for row in uow.connection.execute(sql, values)]

    # -- target resolution -----------------------------------------------------------------------------------------

    def resolve_target(self, ctx, project_id, kind, identity, scope, uow, *, reader_ctx=None, allow_self=False):
        """Resolve one candidate identity through its canonical reader in its true scope, or raise ``not_found``.

        SQL supplied only the identity and sort metadata; the item, its label, lifecycle and archive state all come
        from the authorized domain read here.
        """
        reader_ctx = reader_ctx or ctx
        if kind == access.PROJECT_TYPE:
            access.validate_public_reference({"type": kind, "id": identity, "business_id": identity})
            _, row = access.guard(reader_ctx, self.application, uow, project_id=identity)
            if identity == project_id and not allow_self:
                raise DomainError("validation_error", "A project cannot be linked to itself")
            return self.from_project(row, uow)
        owned_ctx = self.scope_context(reader_ctx, scope)
        if kind == "task":
            arguments = {"task_id": identity}
            if scope is None:
                arguments["scope"] = "global"
            row = self.read(owned_ctx, "tasks_get", arguments, uow)["task"]
            if row["business_id"] != scope:
                raise DomainError("not_found", "Task does not exist in this scope")
            return self.from_task(uow, row)
        if kind == "signoff":
            arguments = {"signoff_id": identity}
            if scope is None:
                arguments["scope"] = "global"
            row = self.read(owned_ctx, "signoffs_get", arguments, uow)["signoff"]
            if row["review_business_id"] != scope:
                raise DomainError("not_found", "Signoff does not exist in this scope")
            return self.from_signoff(uow, row)
        if kind == "action":
            # Resolve in the Action's true source scope, not the destination Project. The neutral action resolver
            # accepts the canonical action_id or its legacy bare row id and authorizes that source read; a caller who
            # can read a Global/shared Action may then explicitly link it without gaining membership in the target.
            source_ctx, stored = self.action_storage(reader_ctx, identity, uow)
            canonical, own_scope = stored["action_key"], stored["business_id"]
            if scope != own_scope:
                raise DomainError("not_found", "Action does not exist in this scope")
            row = self.read(source_ctx, "actions_get", {"action_id": canonical}, uow)["action"]
            legacy = row.get("legacy") or {}
            if legacy.get("business_id") != own_scope:
                raise DomainError("not_found", "Action does not exist in this scope")
            return self.from_action(uow, row)
        if kind == "routine":
            arguments = {"routine_id": identity}
            if scope is None:
                arguments["scope"] = "global"
            row = self.read(owned_ctx, "routines_get", arguments, uow)["routine"]
            if row.get("business_id") != scope or row.get("namespace") != ("global" if scope is None else scope):
                raise DomainError("not_found", "Routine does not exist in this scope")
            return self.from_routine(uow, row)
        if kind == "dev_ticket":
            row = self._ticket(reader_ctx, identity, scope, uow)
            return self.from_ticket(uow, row, scope)
        if kind == "reminder":
            row = self._reminder(owned_ctx, identity, scope, uow)
            return self.from_reminder(uow, row, scope)
        if kind == "table":
            arguments = {"table_id": identity}
            if scope is None:
                arguments["scope"] = "global"
            view = self.read(owned_ctx, "tables_get", arguments, uow)["table"]
            if view.get("business_id") != scope:
                raise DomainError("not_found", "Table does not exist in this scope")
            return self.base(uow, type="table", id=view["id"], business_id=scope, label=view.get("title") or view["id"],
                             revision=view.get("revision") or 1, resource_type="data_tables", state=None,
                             summary=view.get("description"), created_at=view.get("created_at"),
                             updated_at=view.get("updated_at"), archived=view.get("archived", False))
        if kind in DOCUMENT_TYPES or kind == "entity":
            table, stored = self.items.resource(owned_ctx, {"type": kind, "id": identity}, uow)
            if table == "documents" and stored.get("kind") in JOURNAL_KINDS:
                # The owner may read the private journal directly, but it is never promoted into generic Project
                # inventory or an explicit link. Genuine agent notes and business_owned_note stay available.
                raise DomainError("not_found", "Item does not exist in this scope")
            view = self.items.resolve(owned_ctx, {"type": kind, "id": identity}, uow, allow_busy=True)
            canonical = view.get("business_id", scope)
            if canonical != scope:
                raise DomainError("not_found", "Item does not exist in this scope")
            return self.from_neutral(owned_ctx, {"type": kind, "id": identity}, view, uow)
        # An installed extension's typed item: its own authorized neutral read is the canonical reader.
        view = self.items.resolve(owned_ctx, {"type": kind, "id": identity}, uow, allow_busy=True)
        canonical = view.get("business_id", scope)
        if canonical != scope:
            raise DomainError("not_found", "Item does not exist in this scope")
        return self.from_neutral(owned_ctx, {"type": kind, "id": identity}, view, uow)

    def action_context(self, ctx, identity, uow):
        """The caller refreshed into the Action's canonical source scope (Global or its stored business)."""
        if identity.startswith("global/"):
            scope = None
        elif identity.startswith("business/"):
            parts = identity.split("/")
            if len(parts) != 3 or not parts[1]:
                raise DomainError("not_found", "Action does not exist")
            scope = parts[1]
        else:
            # A legacy bare row ID exists only inside the caller's own selected business.
            scope = ctx.business_id
        source = self.scope_context(ctx, scope)
        current = self.application.current_context(source, uow)
        if scope is not None and scope not in current.allowed_business_ids:
            raise DomainError("not_found", "Action does not exist in this scope")
        return current

    def action_storage(self, ctx, identity, uow):
        """The true Action row and the refreshed source scope that authorized reading it."""
        source_ctx = self.action_context(ctx, identity, uow)
        table, stored = self.items.resource(source_ctx, {"type": "action", "id": identity}, uow)
        if table != "action_logs":
            raise DomainError("not_found", "Action does not exist")
        return source_ctx, stored

    def _ticket(self, ctx, identity, scope, uow):
        if ctx.principal_kind == "human":
            listing = self.read(self.scope_context(ctx, None), "dev_tickets_list",
                                {"status": "all", "details": True, "ticket_id": identity}, uow)["items"]
            row = next((value for value in listing if value["id"] == identity), None)
            if row is None or row.get("business_id") != scope:
                raise DomainError("not_found", "System Dev report does not exist in this scope")
            return row
        arguments = {"ticket_id": identity}
        if scope is None:
            arguments["scope"] = "global"
        source_ctx = self.application.current_context(self.scope_context(ctx, scope), uow)
        row = self.read(source_ctx, "dev_tickets_get", arguments, uow)["ticket"]
        if row.get("business_id") != scope:
            raise DomainError("not_found", "System Dev report does not exist in this scope")
        return row

    def _reminder(self, ctx, identity, scope, uow):
        result = self.read(ctx, "owner_reminders_list",
                           {"scope": "business" if scope else "global", "reminder_id": identity}, uow)
        row = next((value for value in result.get("reminders", []) if value["reminder_id"] == identity), None)
        if row is None:
            raise DomainError("not_found", "Reminder does not exist in this scope")
        return row

    # -- visibility, sections, hierarchy ---------------------------------------------------------------------------

    @staticmethod
    def visible(item, args):
        if item.get("_archived") and not args.get("include_archived", False):
            return False
        if (item["type"] == "task" and not args.get("include_closed", False)
                and item.get("state") in CLOSED_STATES and item.get("task_type") not in {"story", "customer"}):
            # Story/Customer are containers with their own established presentation semantics; ordinary actionable
            # Tasks (including Email Tasks) use lifecycle completion. Their children still follow represented parents.
            return False
        if item["type"] == "signoff" and not args.get("include_closed", False):
            return item.get("state") in {"pending", "answered"}
        if item["type"] == "action" and not args.get("include_closed", False) and item.get("state") == "withdrawn":
            return False
        if item["type"] == "dev_ticket" and not args.get("include_closed", False) and item.get("state") != "open":
            return False
        return True

    def represented_parent(self, ctx, project_id, item, args, uow):
        """Whether this Task's parent is itself represented in the same inventory after lifecycle/archive filters."""
        parent_id = item.get("parent_id")
        if not parent_id:
            return False
        parent_scope = item.get("business_id")
        if parent_scope != project_id:
            target = ProjectTarget("task", parent_id, parent_scope)
            if self.store(ctx, project_id, uow).find(ctx, project_id, target, uow) is None:
                return False
        try:
            parent = self.resolve_target(ctx, project_id, "task", parent_id, parent_scope, uow)
        except DomainError as error:
            if error.code in SKIP:
                return False
            raise
        return self.visible(parent, args)

    def store(self, ctx, project_id, uow):
        store = getattr(self.application, "project_links", None)
        if store is None:                                            # pragma: no cover - composition error
            raise DomainError("operation_unavailable", "Project links are not installed")
        return store

    # -- extension kinds -------------------------------------------------------------------------------------------

    def extensions(self, ctx, scan, args, uow, *, budget=EXTENSION_BUDGET):
        """The selected extension kinds, bounded by one per-request budget; returns ``(items, more, resolved)``.

        Installed extension hooks expose candidate identities but no sort-metadata cursor. We therefore enumerate only
        a bounded batch per request, resolve each candidate through its own authorized read, and report whether the
        candidate stream has more. Unselected families are never enumerated, and a section that cannot contain an
        extension kind never scans it.
        """
        wanted = args.get("type")
        section = args.get("section", "all")
        if section not in ("all", "other"):
            return [], False, 0
        if wanted and wanted in KNOWN_TYPES:
            return [], False, 0
        kinds = {kind for contribution in self.items.contributions
                 for kind in contribution.kinds if kind not in KNOWN_TYPES}
        if not kinds:
            return [], False, 0
        read_ctx = self.scope_context(ctx, scan)
        seen, found, resolved = set(), [], 0
        iterator = iter(self.items.candidates(read_ctx, uow, kind=wanted))
        more = False
        while True:
            if resolved >= budget:
                more = next(iterator, None) is not None
                break
            try:
                candidate = next(iterator)
            except StopIteration:
                break
            if candidate["type"] in KNOWN_TYPES or (wanted and candidate["type"] != wanted):
                continue
            if section != "all" and section_of(candidate) != section:
                continue
            key = (candidate["type"], candidate["id"])
            if key in seen:
                continue
            seen.add(key)
            try:
                view = self.items.resolve(read_ctx, {"type": candidate["type"], "id": candidate["id"]}, uow,
                                          allow_busy=True)
            except DomainError as error:
                if error.code in SKIP:
                    continue
                raise
            resolved += 1
            scope = view.get("business_id", scan)
            if scope != scan:
                continue
            item = self.from_neutral(read_ctx, candidate, view, uow)
            if not self.visible(item, args):
                continue
            if section != "all" and section_of(item) != section:
                continue
            found.append(item)
        return found, more, resolved

    # -- one page --------------------------------------------------------------------------------------------------

    def page(self, ctx, project_id, args, uow, *, scan, include_links, projects=False):
        """One bounded page of the selected inventory scope, owned and explicitly linked, deduplicated by identity.

        A SQL window carries only identity/sort metadata. Extension candidates are a separate bounded stream; an
        older extra is never allowed to advance the global frontier past an unexamined SQL window. Canonical-read work
        is capped per request, but the cursor always continues at the real frontier, so a page may legitimately be
        empty with a non-null cursor while a reader still has hidden rows.
        """
        limit = args.get("limit", DEFAULT_LIMIT)
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise DomainError("validation_error", "limit must be between 1 and 100")
        sections = args.get("section", "all")
        binding = {"operation": "project_items_list" if include_links else "project_items_candidates",
                   "project_id": project_id, "scan": scan, "section": sections,
                   "type": args.get("type"), "query": args.get("query"),
                   "root_tasks_only": bool(args.get("root_tasks_only")),
                   "include_closed": bool(args.get("include_closed")),
                   "include_archived": bool(args.get("include_archived")),
                   "projects": projects}
        after = self._decode(args.get("cursor"), binding)
        selects, params = self._selects(ctx, scan, args, include_links=include_links, projects=projects)
        budget = AUTHORIZATION_BUDGET
        extras, extras_more, extension_reads = self.extensions(ctx, scan, args, uow,
                                                              budget=min(EXTENSION_BUDGET, budget))
        budget -= extension_reads
        extras = sorted((item for item in extras if is_after(item_key(item), after)),
                        key=lambda item: order_key(item_key(item)))
        seen = set()
        items = []
        window = max(limit * PAGE_EXAMINE, 50)
        probe = False
        while len(items) < limit and budget > 0:
            rows = self._window(uow, selects, params, after, window) if selects else []
            exhausted = len(rows) < window
            last_sql = (rows[-1]["ts"], rows[-1]["type"], _scope(rows[-1]["scope"]), rows[-1]["id"]) if rows else None
            entries = []
            for row in rows:
                entries.append(("row", row, (row["ts"], row["type"], _scope(row["scope"]), row["id"])))
            for item in extras:
                key = item_key(item)
                if not is_after(key, after):
                    continue
                # With more SQL rows behind this window, only extras up to its last key belong to the consumed
                # frontier. An older extra waits; it must never jump over unexamined SQL rows.
                if last_sql is not None and order_key(key) > order_key(last_sql):
                    continue
                entries.append(("item", item, key))
            entries.sort(key=lambda entry: order_key(entry[2]))
            if not entries:
                if extras_more:
                    probe = True
                break
            consumed_extras = set()
            for kind, value, key in entries:
                if budget <= 0:
                    break
                if not is_after(key, after):
                    continue
                after = list(key)
                budget -= 1
                if kind == "item":
                    consumed_extras.add(target_key(value))
                    candidate = self._clean(value)
                    candidate["association"] = self.association_for(ctx, uow, candidate, project_id)
                    if args.get("query") and not self._matches(candidate, args["query"]):
                        continue
                    if sections != "all" and section_of(candidate) != sections:
                        continue
                    identity = target_key(candidate)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    items.append(candidate)
                    if len(items) >= limit:
                        break
                    continue
                row = value
                try:
                    item = self.resolve_target(ctx, project_id, row["type"], row["id"], _unscope(row["scope"]), uow,
                                               allow_self=True)
                except DomainError as error:
                    if error.code in SKIP:
                        continue
                    raise
                key_identity = target_key(item)
                if key_identity in seen:
                    continue
                seen.add(key_identity)
                if include_links:
                    item["association"] = association(row["owned"], {"id": row["link_id"], "revision": row["link_revision"]}
                                                      if row["linked"] and row["link_id"] else None)
                else:
                    item["association"] = self.association_for(ctx, uow, item, project_id)
                if not self.visible(item, args):
                    continue
                if sections != "all" and section_of(item) != sections:
                    continue
                if args.get("type") and item["type"] != args["type"]:
                    continue
                if args.get("query") and not self._matches(item, args["query"]):
                    continue
                if args.get("root_tasks_only") and item["type"] == "task" \
                        and self.represented_parent(ctx, project_id, item, args, uow):
                    continue
                items.append(self._clean(item))
                if len(items) >= limit:
                    break
            extras = [item for item in extras if target_key(item) not in consumed_extras]
            if len(items) >= limit:
                break
            if not exhausted:
                continue
            if extras:
                # SQL is exhausted; process the remaining extras in the next loop with an empty window.
                if budget <= 0:
                    break
                continue
            break
        more_rows = bool(self._window(uow, selects, params, after, 1, probe=True)) if selects else False
        more_extras = extras_more or any(is_after(item_key(item), after) for item in extras)
        probe = more_rows or more_extras
        cursor = self._encode(binding, after) if probe and after is not None else None
        return items, cursor

    def associate(self, ctx, uow, item, project_id):
        item["association"] = self.association_for(ctx, uow, item, project_id)
        return item

    def association_for(self, ctx, uow, item, project_id):
        store = self.store(ctx, project_id, uow)
        link = store.find(ctx, project_id, ProjectTarget(item["type"], item["id"], item.get("business_id")), uow)
        return association(owned(uow, item, project_id), link)

    @staticmethod
    def _matches(item, query):
        needle = query.casefold()
        return needle in (str(item.get("label")) + " " + str(item.get("id")) + " " +
                          str(item.get("business_name"))).casefold()

    @staticmethod
    def _clean(item):
        item = dict(item)
        item.pop("_archived", None)
        return item

    @staticmethod
    def _encode(binding, after):
        payload = canonical_bytes({"version": 1, "binding": binding, "after": list(after)})
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode(cursor, binding):
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2000:
            raise DomainError("validation_error", "Invalid project inventory cursor")
        try:
            raw = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        except (ValueError, UnicodeError) as exc:
            raise DomainError("validation_error", "Invalid project inventory cursor") from exc
        if (not isinstance(value, dict) or value.get("version") != 1 or value.get("binding") != binding
                or not isinstance(value.get("after"), list) or len(value["after"]) != 4
                or not all(isinstance(part, str) for part in value["after"])):
            raise DomainError("validation_error", "The cursor belongs to another project or filter")
        after = value["after"]
        # A stored/NULL Global scope is one canonical empty-string sort component, never None.
        return [after[0], after[1], _scope(after[2]), after[3]]

    # -- exact item and association --------------------------------------------------------------------------------

    def target_spec(self, ctx, project_id, spec, uow, *, allow_self=False):
        """One public target spec (exact canonical scope included) resolved through the canonical reader."""
        spec = access.validate_public_reference(dict(spec))
        if spec["type"] not in TARGET_TYPES and spec["type"] not in self.items.kinds:
            raise DomainError("validation_error", "This item kind does not exist")
        item = self.resolve_target(ctx, project_id, spec["type"], spec["id"], spec.get("business_id"), uow,
                                   allow_self=allow_self)
        if spec["type"] == access.PROJECT_TYPE:
            return item
        if item.get("business_id") != spec.get("business_id"):
            raise DomainError("not_found", "Choose the item's actual scope")
        return item

    # -- records ---------------------------------------------------------------------------------------------------

    def record(self, ctx, item_row, uow):
        """The appropriate existing domain read of one authorized item, never a raw database dump."""
        kind, identity, scope = item_row["type"], item_row["id"], item_row["business_id"]
        own = self.scope_context(ctx, scope)
        if kind == "task":
            return self.read(own, "tasks_get", {"task_id": identity, **({"scope": "global"} if scope is None else {})}, uow)
        if kind == "signoff":
            return self.read(own, "signoffs_get", {"signoff_id": identity, **({"scope": "global"} if scope is None else {})}, uow)
        if kind == "action":
            source_ctx = self.action_context(ctx, identity, uow)
            return self.read(source_ctx, "actions_get", {"action_id": identity}, uow)
        if kind == "table":
            return self.read(own, "tables_get", {"table_id": identity, **({"scope": "global"} if scope is None else {})}, uow)
        if kind == "routine":
            return self.read(own, "routines_get", {"routine_id": identity, **({"scope": "global"} if scope is None else {})}, uow)
        if kind == "reminder":
            result = self.read(own, "owner_reminders_list",
                               {"scope": "business" if scope else "global", "reminder_id": identity}, uow)
            stored = next((value for value in result.get("reminders", []) if value["reminder_id"] == identity), None)
            if stored is None:
                raise DomainError("not_found", "Reminder does not exist in this scope")
            return {"reminder": stored}
        if kind == "dev_ticket":
            if ctx.principal_kind == "human":
                return {"ticket": self._ticket(ctx, identity, scope, uow)}
            return self.read(self.scope_context(ctx, scope), "dev_tickets_get", {"ticket_id": identity}, uow)
        if kind == access.PROJECT_TYPE:
            _, row = access.guard(ctx, self.application, uow, project_id=identity)
            return {"project": access.project_view(row)}
        if kind in DOCUMENT_TYPES or kind == "entity":
            return self.document_record(own, {"type": kind, "id": identity}, uow)
        # An installed extension's typed item: its authorized neutral read is the existing domain read.
        return {"item": self.items.resolve(own, {"type": kind, "id": identity}, uow)}

    def document_record(self, ctx, item, uow):
        """The owning read of one authorized document; no mutation and no reader without a real read handler."""
        table, row = self.items.resource(ctx, item, uow)
        if table == "documents" and row.get("kind") in JOURNAL_KINDS:
            raise DomainError("not_found", "This private journal entry is not Project inventory")
        if table == "principals":
            listing = self.read(ctx, "assignees_list", {}, uow)
            person = next((value for value in listing.get("assignees", []) if value.get("id") == item["id"]), None)
            if person is None:
                raise DomainError("not_found", "Person is not available")
            return {"person": person}
        kind = row.get("kind")
        if kind == "asset":
            return self.read(ctx, "assets_get", {"asset_id": item["id"]}, uow)
        if kind == "data_upload":
            return self.read(ctx, "data_uploads_get", {"upload_id": item["id"]}, uow)
        if kind == "plan":
            if not row.get("owning_agent_id"):
                raise DomainError("not_found", "Plan is not available")
            return self.read(ctx, "plans_get", {"agent_id": row["owning_agent_id"]}, uow)
        if table == "context_entities":
            return self.read(ctx, "context_items_get", {"item": item}, uow)
        if kind == "context_person":
            return self.read(ctx, "context_people_get", {"document_id": item["id"]}, uow)
        if kind in {"context_topic", "context_note", "context", "pillar", "context_dump"}:
            return self.read(ctx, "context_get", {"document_id": item["id"]}, uow)
        if kind in {"business_note", "agent_note", "journal", "global_note", "business_owned_note", "legacy_agent_document"}:
            return self.read(ctx, "notes_get", {"document_id": item["id"]}, uow)
        if kind == "content":
            return self.read(ctx, "content_get", {"document_id": item["id"]}, uow)
        # A registered document without a read handler is never passed to ``descriptor.read``.
        raise DomainError("operation_unavailable", "This document has no read operation")


def list_schema():
    return schema({"project_id": {"type": "string", "format": "uuid"},
                   "scope": {"enum": ["business", "global"]},
                   "section": {"enum": list(SECTIONS)}, "root_tasks_only": {"type": "boolean"},
                   "include_closed": {"type": "boolean"}, "include_archived": {"type": "boolean"},
                   "cursor": {"type": "string", "maxLength": 2000},
                   "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, ("project_id",))


__all__ = ["DOCUMENT_TYPES", "Inventory", "MAX_LIMIT", "SECTIONS", "association", "list_schema", "normalized",
           "owned", "section_of", "target_key"]
