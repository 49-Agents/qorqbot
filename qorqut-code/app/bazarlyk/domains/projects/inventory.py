"""Project inventory: the authorized, deduplicated view of one Project's owned and explicitly linked records.

Every family is read through its own canonical reader or its domain's authorized neutral item read, never through
an arbitrary table:

* work        Tasks of every Task type, through ``tasks_list`` (hierarchy included) plus explicitly linked foreign
              Tasks through ``tasks_get`` in the Task's own scope;
* signoffs    the Project's review scope through ``signoffs_list`` plus explicitly linked Signoffs;
* actions     Actions whose membership includes the Project, through ``actions_list(business=...)`` plus linked
              Actions from any scope (canonical ``action_key`` identity preserved);
* documents   Project notes, materials, people and topics through the neutral item resolver's authorized reads;
* tables      Project tables through the same neutral reads (``tables_get`` re-authorizes each item);
* automation  Project routines through ``routines_list``; executions stay in the existing ``routines_runs`` reader
              under the routine's exact id and scope;
* other       owner/dev tickets (``dev_tickets_list``), Project reminders (``owner_reminders_list``), external
              references and any installed extension item kinds through the neutral reads.

Nothing is inferred from a display label. Ownership is always the record's own scope. A link keeps its target's own
scope, adds no content and grants nothing: every candidate is resolved and authorized again here, and a denied
candidate is skipped before its label or count is emitted. Reads create no message, no card and no source write.

The merged page is bounded: each family is scanned to ``MAX_SCAN`` rows, then ordered deterministically by the
item's updated (or created) instant descending and its scoped canonical identity, with a keyset cursor bound to the
operation and its filters (the engine's ``page``/``page_position`` conventions).
"""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application.plans import page, schema
from qorqut_operations.contracts import InvocationOptions
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE as ITEM_UNAVAILABLE

from . import access

SECTIONS = ("all", "work", "signoffs", "actions", "documents", "tables", "automation", "other")
DOCUMENT_TYPES = frozenset({"material", "person", "topic"})
# Kinds whose own canonical list reader builds the family (they are not enumerated through the neutral resolver).
OWN_READERS = frozenset({"task", "signoff", "action", "agent"})
SECTION_OF = {"task": "work", "signoff": "signoffs", "action": "actions", "table": "tables", "routine": "automation",
              "project": "other", "dev_ticket": "other", "reminder": "other", "entity": "other"}
DEFAULT_LIMIT, MAX_LIMIT, MAX_SCAN = 25, 100, 2000
# The reason a candidate cannot be shown is never disclosed: only these mean "skip it silently".
SKIP = frozenset(access.UNAVAILABLE) | ITEM_UNAVAILABLE | {"business_archived"}
# A whole family the caller may not read is omitted, never disclosed and never fatal to the rest of the page.
FAMILY_SKIP = SKIP | {"forbidden", "scope_required"}
CANONICAL_LIMITS = {"tasks_list": 200, "signoffs_list": 100, "actions_list": 200, "routines_list": 200,
                    "dev_tickets_list": 200, "owner_reminders_list": 100}


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

    def attempted(self, ctx, operation, arguments, uow, default):
        """A family the caller may not read is empty and never disclosed; a real failure still propagates."""
        try:
            return self.read(ctx, operation, arguments, uow)
        except DomainError as error:
            if error.code in FAMILY_SKIP:
                return default
            raise

    def collect(self, ctx, operation, arguments, uow, *, key="items", cursor="next_cursor"):
        """Follow one canonical list reader's own pages up to the documented bounded scan."""
        found, next_cursor, scanned = [], arguments.get("cursor"), 0
        while True:
            page_args = {**arguments, "limit": max(1, min(CANONICAL_LIMITS.get(operation, 100), MAX_SCAN - scanned))}
            if next_cursor:
                page_args["offset" if operation == "dev_tickets_list" else "cursor"] = next_cursor
            try:
                result = self.read(ctx, operation, page_args, uow)
            except DomainError as error:
                if error.code in FAMILY_SKIP and not found:
                    return []
                raise
            rows = result.get(key) or []
            found.extend(rows)
            scanned += len(rows)
            next_cursor = result.get(cursor) or result.get("next_offset")
            if not next_cursor or not rows or scanned >= MAX_SCAN:
                return found

    # -- item construction -----------------------------------------------------------------------------------------

    @staticmethod
    def scope_names(uow, business_id):
        return access.scope_name(uow, business_id)

    def base(self, uow, *, type, id, business_id, label, revision, resource_type, state=None, summary=None,
             task_type=None, parent_id=None, created_at=None, updated_at=None, at=None):
        slug, name = self.scope_names(uow, business_id)
        item = normalized(type=type, id=id, business_id=business_id, business_slug=slug, business_name=name,
                          label=label, revision=revision, state=state, task_type=task_type, parent_id=parent_id,
                          summary=summary, resource_type=resource_type, can_thread=type in access.THREAD_KINDS,
                          created_at=created_at, updated_at=updated_at, at=at)
        return item

    def from_neutral(self, ctx, item, view, uow):
        """One resolved neutral item as a Project inventory row."""
        return self.base(uow, type=item["type"], id=item["id"], business_id=view.get("business_id", ctx.business_id),
                         label=view.get("label") or item["id"], revision=view.get("revision") or 1,
                         resource_type=view.get("resource_type") or item["type"], state=view.get("state"),
                         summary=view.get("summary"), task_type=view.get("task_type"),
                         created_at=view.get("created_at"), updated_at=view.get("updated_at"))

    def from_task(self, uow, row):
        """One ``tasks_list``/``tasks_get`` row, keeping its Task type and place in that Task hierarchy."""
        return self.base(uow, type="task", id=row["id"], business_id=row["business_id"], label=row["title"],
                         revision=row["revision"], resource_type="tasks", state=row.get("status"),
                         summary=row.get("overview"), task_type=row.get("task_type") or "task",
                         parent_id=row.get("parent_id"), created_at=row.get("created_at"),
                         updated_at=row.get("updated_at"))

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
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"))

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
                         created_at=row.get("created_at"), updated_at=row.get("updated_at"))

    # -- project records -------------------------------------------------------------------------------------------

    def project(self, ctx, project_id, args, uow, *, operation="project_items_list"):
        """The identity gate, the Project's current reach and the Project record read of this operation."""
        selected, row = access.allow(ctx, self.application, uow, project_id=project_id, operation=operation)
        return selected, row

    def association_of(self, uow, item, project_id, links):
        """Ownership union with the one explicit association of ``item`` (if any)."""
        link = links.get((item["type"], item["id"], item.get("business_id")))
        item["association"] = association(owned(uow, item, project_id), link)
        return item

    # -- families --------------------------------------------------------------------------------------------------

    def work(self, selected, args, uow):
        rows = self.collect(selected, "tasks_list", {
            "include_closed": args.get("include_closed", False),
            "include_archived": args.get("include_archived", False), "snooze_filter": "all"}, uow, key="tasks")
        return [self.from_task(uow, row) for row in rows]

    def signoffs(self, selected, args, uow):
        """Pending and answered proposals of this review scope; closed history only with ``include_closed``."""
        arguments = {} if selected.business_id is not None else {"scope": "global"}
        if not args.get("include_closed", False):
            arguments["states"] = ["pending", "answered"]
        rows = self.collect(selected, "signoffs_list", arguments, uow, key="items")
        return [self.from_signoff(uow, row) for row in rows]

    def actions(self, selected, args, uow):
        """Actions whose membership includes this scope; a global scan keeps the owner/ecosystem listing."""
        arguments = {"include_withdrawn": args.get("include_closed", False)}
        if selected.business_id is not None:
            arguments["business"] = selected.business_id
        rows = self.collect(selected, "actions_list", arguments, uow, key="actions")
        return [self.from_action(uow, row) for row in rows]

    def neutral(self, selected, args, uow):
        """Every other family the neutral resolver owns: documents, people/topics, tables, entities, extensions."""
        found, scanned = [], 0
        for candidate in self.items.candidates(selected, uow):
            if candidate["type"] in OWN_READERS or candidate["type"] == access.PROJECT_TYPE:
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                break
            try:
                view = self.items.resolve(selected, candidate, uow)
            except DomainError as error:
                if error.code in SKIP:
                    continue
                raise
            if view.get("archived") and not args.get("include_archived", False):
                continue
            found.append((candidate, view))
        return found

    def routines(self, selected, args, uow):
        """Project routines; Global routines have no business listing and are only reachable by an explicit link."""
        if selected.business_id is None:
            return []
        rows = self.collect(selected, "routines_list", {}, uow, key="items")
        return [self.from_routine(uow, row) for row in rows]

    def tickets(self, selected, project_id, args, uow):
        """Project-owned System Dev tickets through the owner queue reader (other reviewers keep their own)."""
        found = []
        for row in self.collect(selected, "dev_tickets_list", {"status": "all"}, uow, key="items"):
            if row.get("business_id") == project_id:
                found.append(self.from_ticket(uow, row, project_id))
        return found

    def reminders(self, selected, project_id, args, uow):
        rows = self.collect(selected, "owner_reminders_list",
                            {"scope": "business" if project_id else "global"}, uow, key="reminders")
        return [self.from_reminder(uow, row, project_id) for row in rows]

    # -- explicit links --------------------------------------------------------------------------------------------

    def linked(self, ctx, project_id, links, uow):
        """Every explicitly associated target whose source is still readable right now, in its own scope."""
        found = []
        for link in links.values():
            try:
                item = self.target(ctx, project_id, link["target_type"], link["target_id"], link["target_business_id"], uow)
            except DomainError as error:
                if error.code in SKIP:
                    continue
                raise
            found.append(item)
        return found

    # -- one page --------------------------------------------------------------------------------------------------

    def inventory(self, ctx, project_id, args, uow, *, links=None, projects=False):
        """All accessible entries of one scan scope, owned and explicitly linked, deduplicated by scoped identity.

        ``project_id`` is the scan scope (a Project UUID, or ``None`` for Global) in both ``project_items_list`` and
        ``project_items_candidates``; the caller's own context is never rewritten here."""
        selected = replace(ctx, business_id=project_id, review_business_id=project_id)
        index = {}
        for item in self.work(selected, args, uow) + self.signoffs(selected, args, uow) + self.actions(selected, args, uow) \
                + self.routines(selected, args, uow) + self.tickets(selected, project_id, args, uow) \
                + self.reminders(selected, project_id, args, uow):
            index.setdefault(target_key(item), item)
        for candidate, view in self.neutral(selected, args, uow):
            item = self.from_neutral(selected, candidate, view, uow)
            index.setdefault(target_key(item), item)
        if projects:
            for candidate in self.items.candidates(selected, uow, kind=access.PROJECT_TYPE):
                try:
                    self.items.resolve(selected, candidate, uow)
                except DomainError as error:
                    if error.code in SKIP:
                        continue
                    raise
                row = uow.repository("businesses").get(candidate["id"])
                if row is not None:
                    item = self.from_project(row, uow)
                    index.setdefault(target_key(item), item)
        links = {} if links is None else links
        for item in self.linked(ctx, project_id, links, uow):
            index.setdefault(target_key(item), item)
        entries = list(index.values())
        if args.get("root_tasks_only"):
            # Top-level Tasks of this scan scope only; a child never stands in for its parent.
            entries = [item for item in entries if item["type"] != "task" or not item.get("parent_id")]
        section = args.get("section", "all")
        if section != "all":
            entries = [item for item in entries if self.section_of(item) == section]
        entries.sort(key=lambda item: ((item.get("updated_at") or item.get("created_at") or ""), item["type"], item["id"]),
                     reverse=True)
        return entries

    @staticmethod
    def section_of(item):
        if item["type"] in DOCUMENT_TYPES:
            return "documents"
        return SECTION_OF.get(item["type"], "other")

    # -- link store access -----------------------------------------------------------------------------------------

    def links(self, ctx, project_id, uow):
        store = getattr(self.application, "project_links", None)
        if store is None:                                            # pragma: no cover - composition error
            raise DomainError("operation_unavailable", "Project links are not installed")
        return store.index(ctx, project_id, uow)

    # -- target resolution -----------------------------------------------------------------------------------------

    def target(self, ctx, project_id, kind, identity, business_id, uow, *, allow_self=False):
        """One link target resolved and authorized in its own scope, as a normalized item.

        The canonical identity is kept: an Action becomes its ``action_key``, a Project names itself, and every
        record keeps its own owning scope. The Project's scope is never substituted for the target's."""
        if kind == access.PROJECT_TYPE:
            return self.project_target(ctx, project_id, identity, business_id, uow, allow_self=allow_self)
        if kind == "routine":
            return self.routine_target(ctx, identity, business_id, uow)
        if kind == "reminder":
            return self.reminder_target(ctx, identity, business_id, uow)
        if kind == "dev_ticket":
            return self.ticket_target(ctx, identity, business_id, uow)
        if kind not in self.items.kinds:
            raise DomainError("validation_error", "This item kind does not exist")
        own = replace(ctx, business_id=business_id, review_business_id=business_id)
        item = {"type": kind, "id": identity}
        view = self.items.resolve(own, item, uow)
        table, row = self.items.resource(own, item, uow)
        canonical, scope = identity, business_id
        if table == "action_logs":
            canonical, scope = row["action_key"], row["business_id"]
        elif table == "signoffs":
            scope = row["review_business_id"]
        elif "business_id" in row:
            scope = row["business_id"]
        elif table == "principals":
            scope = business_id
        if business_id is not None and scope != business_id:
            raise DomainError("not_found", "Choose the item's actual scope")
        view = {**view, "business_id": scope}
        result = self.from_neutral(own, {"type": kind, "id": canonical, "business_id": scope}, view, uow)
        result["id"] = canonical
        return result

    def project_target(self, ctx, project_id, identity, business_id, uow, allow_self=False):
        if identity == project_id and not allow_self:
            raise DomainError("validation_error", "A project cannot be linked to itself")
        if business_id not in (None, identity):
            raise DomainError("validation_error", "A project reference must name the project as its own scope")
        own = replace(ctx, business_id=None, review_business_id=None)
        view = self.items.resolve(own, {"type": access.PROJECT_TYPE, "id": identity}, uow)
        row = uow.repository("businesses").get(identity, required=True)
        item = self.from_project(row, uow)
        item["revision"] = view.get("revision", row["revision"])
        return item

    def routine_target(self, ctx, identity, business_id, uow):
        own = self.scope_context(ctx, business_id)
        row = self.attempted(own, "routines_get", {"routine_id": identity}, uow, None)
        if row is None or row.get("routine", {}).get("business_id") != business_id:
            raise DomainError("not_found", "Routine does not exist in this scope")
        return self.from_routine(uow, row["routine"])

    def reminder_target(self, ctx, identity, business_id, uow):
        own = self.scope_context(ctx, business_id)
        result = self.attempted(own, "owner_reminders_list",
                                {"scope": "business" if business_id else "global", "reminder_id": identity}, uow, None)
        if not result:
            raise DomainError("not_found", "Reminder does not exist in this scope")
        row = next((item for item in result.get("reminders", []) if item["reminder_id"] == identity), None)
        if row is None:
            raise DomainError("not_found", "Reminder does not exist in this scope")
        return self.from_reminder(uow, row, business_id)

    def ticket_target(self, ctx, identity, business_id, uow):
        own = self.scope_context(ctx, business_id)
        if ctx.principal_kind == "human":
            for row in self.collect(own, "dev_tickets_list", {"status": "all"}, uow, key="items"):
                if row["id"] == identity:
                    return self.from_ticket(uow, row, business_id)
            raise DomainError("not_found", "System Dev report does not exist")
        view = self.attempted(own, "dev_tickets_get", {"ticket_id": identity}, uow, None)
        if view is None or view.get("ticket", {}).get("business_id") != business_id:
            raise DomainError("not_found", "System Dev report does not exist in this scope")
        return self.from_ticket(uow, view["ticket"], business_id)

    def scope_context(self, ctx, business_id):
        if business_id is not None and business_id not in ctx.allowed_business_ids and ctx.business_id != business_id:
            raise DomainError("not_found", "Item is outside your current access")
        return replace(ctx, business_id=business_id, review_business_id=business_id)

    # -- records ---------------------------------------------------------------------------------------------------

    def record(self, ctx, item_row, uow):
        """The appropriate existing domain read of one authorized item, never a raw database dump."""
        kind, identity, business_id = item_row["type"], item_row["id"], item_row["business_id"]
        own = self.scope_context(ctx, business_id)
        scope = {"scope": "global"} if business_id is None else {}
        if kind == "task":
            return self.read(own, "tasks_get", {"task_id": identity, **scope}, uow)
        if kind == "signoff":
            return self.read(own, "signoffs_get", {"signoff_id": identity, **scope}, uow)
        if kind == "action":
            return self.read(own, "actions_get", {"action_id": identity, **scope}, uow)
        if kind == "table":
            return self.read(own, "tables_get", {"table_id": identity, **scope}, uow)
        if kind == "routine":
            return self.read(own, "routines_get", {"routine_id": identity, **scope}, uow)
        if kind == "reminder":
            result = self.attempted(own, "owner_reminders_list",
                                    {"scope": "business" if business_id else "global", "reminder_id": identity}, uow, None)
            stored = next((value for value in (result or {}).get("reminders", []) if value["reminder_id"] == identity),
                          None)
            if stored is None:
                raise DomainError("not_found", "Reminder does not exist in this scope")
            return {"reminder": stored}
        if kind == "dev_ticket":
            if ctx.principal_kind == "human":
                for row in self.collect(own, "dev_tickets_list", {"status": "all"}, uow, key="items"):
                    if row["id"] == identity:
                        return {"ticket": row}
                raise DomainError("not_found", "System Dev report does not exist")
            return self.read(own, "dev_tickets_get", {"ticket_id": identity, **scope}, uow)
        if kind == access.PROJECT_TYPE:
            return {"project": access.project_view(uow.repository("businesses").get(identity, required=True))}
        if kind in DOCUMENT_TYPES or kind == "entity":
            return self.document_record(own, {"type": kind, "id": identity}, uow)
        # An installed extension's typed item: its authorized neutral read is the existing domain read.
        return {"item": self.items.resolve(own, {"type": kind, "id": identity}, uow)}

    def document_record(self, ctx, item, uow):
        """The owning document operation of one authorized person/topic/material/entity item."""
        table, row = self.items.resource(ctx, item, uow)
        if table == "principals":
            listing = self.read(ctx, "assignees_list", {}, uow)
            person = next((value for value in listing.get("assignees", []) if value.get("id") == item["id"]), None)
            if person is None:
                raise DomainError("not_found", "Person is not available")
            return {"person": person}
        kind = row.get("kind")
        operation = {"asset": "assets_get", "context_upload": "context_upload", "context_dump": "context_get",
                     "data_upload": "data_uploads_get", "content": "content_get", "context_person": "context_people_get"}.get(kind)
        if operation is None:
            operation = "notes_get" if kind in {"business_note", "agent_note", "journal", "global_note",
                                                "business_owned_note"} else "context_get"
        if table == "context_entities":
            return self.read(ctx, "context_items_get", {"item": item}, uow)
        return self.read(ctx, operation, {"document_id": item["id"]}, uow)


def page_items(entries, args, project_id):
    """The engine's bounded keyset page over the merged, deterministically ordered entries."""
    selected, cursor = page(entries, args, project_id, "project_items_list",
                            key=lambda item: ((item.get("updated_at") or item.get("created_at") or ""),
                                              item["type"], item["id"]))
    return selected, cursor


def list_schema():
    return schema({"project_id": {"type": "string", "format": "uuid"},
                   "scope": {"enum": ["business", "global"]},
                   "section": {"enum": list(SECTIONS)}, "root_tasks_only": {"type": "boolean"},
                   "include_closed": {"type": "boolean"}, "include_archived": {"type": "boolean"},
                   "cursor": {"type": "string", "maxLength": 2000},
                   "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}}, ("project_id",))


__all__ = ["DOCUMENT_TYPES", "Inventory", "MAX_LIMIT", "SECTIONS", "association", "list_schema", "normalized",
           "page_items", "target_key"]