"""A synthetic host for the neutral item and relationship extraction: small scoped fake domains, real engine parts.

Everything real here is the engine (Application, PolicyResolver, ItemResolver, RelationshipOperations, the real
relationship/review tables, the Signoff service that stages agent proposals). Everything fake is what a trusted host
contributes: two item lookups over synthetic ``topics``/``messages``/``tickets`` tables with a private-source rule,
a busy state and a file-content target, the candidates port, the Material/archive/navigation adapters, a
and three reviewable edit operations. No product code, provider or live data."""

from __future__ import annotations

import hashlib
import json

from qorqut_operations import signoffs
from qorqut_operations.application import (Application, CatalogContribution, CatalogEntry, OperationDescriptor,
                                           OperationRegistry)
from qorqut_operations.application.plans import PlanBuilder, apply_records, ref, require_revision, rows
from qorqut_operations.contracts import (DomainError, ExpectedTarget, InvocationContext, Principal, ResourceRef,
                                         canonical_hash, new_id, plain)
from qorqut_operations.items import UNAVAILABLE, ItemContribution, ItemResolver, authorize_item_read, item_revision
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.relationships import RelationshipOperations, RelationshipPorts, ReviewEdit
from qorqut_operations.signoffs import ABSENT, SignoffPorts, SignoffService
from qorqut_operations.storage import LockPaths, StorageContribution, TableSpec
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.repositories import decode_row
from signoff_support import FakeAgents, FakeExecution, FakePeers, FakeReferences, FakeTasks
from support import AGENT, BUSINESS, HUMAN, Demo, DemoRecordHooks, database_at

STRANGER = "00000000-0000-4000-8000-0000000000c3"     # an active member who owns nothing
INSTALLATION = "11111111-1111-4111-8111-111111111111"

SCHEMA = (
    """CREATE TABLE inbox_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
        action TEXT NOT NULL, actor_id TEXT, before_json TEXT, after_json TEXT)""",
    """CREATE TABLE topics (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '', visibility TEXT NOT NULL DEFAULT 'shared', owner_id TEXT,
        availability TEXT NOT NULL DEFAULT 'ready', archived INTEGER NOT NULL DEFAULT 0,
        material_type TEXT NOT NULL DEFAULT 'document', tags_json TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (business_id, id))""",
    """CREATE TABLE topic_files (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL,
        content_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (business_id, id))""",
    """CREATE TABLE messages (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL,
        topic_id TEXT NOT NULL, subject TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))""",
    """CREATE TABLE tickets (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))""",
)
TABLES = {"inbox_changes": TableSpec(("id",), mutable=False, revision_column=None),
          "topics": TableSpec(("business_id", "id"), "business_id"),
          "topic_files": TableSpec(("business_id", "id"), "business_id", revision_column=None),
          "messages": TableSpec(("business_id", "id"), "business_id"),
          "tickets": TableSpec(("business_id", "id"), "business_id")}

ROLES = {"related_to": ("Related to", "Related from"), "part_of": ("Part of", "Includes"),
         "assigned_to": ("Assigned to", "Assigned work"), "blocked_by": ("Blocked by", "Blocks"),
         "protected_membership": ("Member of", "Members")}
WORKFLOW_ROLES = {"assigned_to": "tickets_update or assignment tools", "blocked_by": "tickets_update"}
MATERIAL_TYPES = ("document", "note", "mixed")
TAGS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100}, "maxItems": 50, "uniqueItems": True}

READS = ("topics_get", "tickets_get")
MUTATIONS = ("topics_update", "docs_update", "tickets_update")
RELATIONSHIP_OPERATIONS = (
    "relationship_roles_list", "relationships_list", "relationships_get", "context_items_list", "context_items_get",
    "context_navigation_get", "context_graph_get", "context_reviews_list", "context_reviews_history",
    "relationships_create", "relationships_update", "relationships_remove", "context_items_archive",
    "materials_update", "context_review", "context_review_reopen")


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Hooks(DemoRecordHooks):
    """The demo record hooks, tolerant of the synthetic ``topic_files`` table that has no revision column."""

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        self.applied.append((table, (before or {}).get("revision"), (after or {}).get("revision")))
        uow.connection.execute("INSERT INTO demo_history(kind, detail) VALUES ('record', ?)", (table,))


# -- the synthetic item domains --------------------------------------------------------------------------------------

class NotesLookup:
    """Kinds ``topic`` and ``message``: a private topic resolves only for its owner; a topic being updated is busy
    unless the read is metadata-only; a message's canonical relationship endpoint is its topic."""

    def __init__(self, host):
        self.host = host

    def resource(self, ctx, item, uow):
        kind, identity = item["type"], item["id"]
        if kind == "topic":
            row = uow.repository("topics", business_id=ctx.business_id).get(identity)
            if row and row["visibility"] == "private" and row["owner_id"] != ctx.principal_id:
                raise DomainError("not_found", "Context item does not exist in this project")
            if row:
                return "topics", row
        elif kind == "message":
            row = uow.repository("messages", business_id=ctx.business_id).get(identity)
            if row:
                return "messages", row
        raise DomainError("not_found", "Context item does not exist in this project")

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        authorize_item_read(self.host.app, ctx, "topics_get", uow)
        if table == "topics" and row["availability"] != "ready" and not allow_busy:
            raise DomainError("busy", "The document has an unfinished application", retryable=True)
        if builder:
            builder.expect(ref(table, item["id"], ctx.business_id), row)
        topic = table == "topics"
        return {"label": row["title"] if topic else row["subject"], **item, "revision": item_revision(row),
                "resource_type": table, "resource_id": item["id"], "subtype": "note" if topic else "message",
                "material_type": row["material_type"] if topic else None, "origin": "authored",
                "tags": row["tags_json"] if topic else [], "archived": bool(row["archived"]) if topic else False,
                "raw_source": topic and row["visibility"] == "raw", "summary": row["body"][:40] if topic else "",
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def relationship_item(self, ctx, item, uow):
        table, row = self.resource(ctx, item, uow)
        return {"type": "topic", "id": row["topic_id"]} if table == "messages" else item

    def fingerprint(self, ctx, item, uow, builder=None):
        self.resolve(ctx, item, uow, builder)
        table, row = self.resource(ctx, item, uow)
        if table == "messages":
            return canonical_hash({"subject": row["subject"]})
        if builder:
            file = ResourceRef("topic_files", row["id"], ctx.business_id)
            current = uow.repository("topic_files", business_id=ctx.business_id).get(row["id"], required=True)
            builder.targets[file] = ExpectedTarget(file, content_hash=current["content_hash"], exists=True)
        return canonical_hash({"title": row["title"], "body": row["body"]})


class TicketLookup:
    def __init__(self, host):
        self.host = host

    def resource(self, ctx, item, uow):
        row = uow.repository("tickets", business_id=ctx.business_id).get(item["id"]) if item["type"] == "ticket" else None
        if row is None:
            raise DomainError("not_found", "Context item does not exist in this project")
        return "tickets", row

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        authorize_item_read(self.host.app, ctx, "tickets_get", uow)
        if builder:
            builder.expect(ref(table, item["id"], ctx.business_id), row)
        return {"label": row["title"], **item, "state": row["state"], "revision": item_revision(row),
                "resource_type": table, "resource_id": item["id"], "subtype": row["state"], "material_type": None,
                "tags": [], "archived": row["state"] == "closed", "raw_source": False, "summary": "",
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def relationship_item(self, ctx, item, uow):
        self.resource(ctx, item, uow)
        return item

    def fingerprint(self, ctx, item, uow, builder=None):
        self.resolve(ctx, item, uow, builder)
        _, row = self.resource(ctx, item, uow)
        return canonical_hash({key: value for key, value in row.items()
                               if key not in {"revision", "created_at", "updated_at"}})


class Candidates:
    def candidates(self, ctx, uow, kind=None):
        for item_kind, table in (("topic", "topics"), ("ticket", "tickets")):
            if not kind or kind == item_kind:
                for row in rows(uow, table, ctx.business_id):
                    yield {"type": item_kind, "id": row["id"]}


# -- the relationship ports --------------------------------------------------------------------------------------------

class Materials:
    filters = {"material_type": {"enum": list(MATERIAL_TYPES)}, "origin": {"type": "string", "maxLength": 100},
               "tag": {"type": "string", "minLength": 1, "maxLength": 100}}
    material_types = MATERIAL_TYPES
    tags = TAGS

    def __init__(self, host):
        self.host = host

    @staticmethod
    def matches(value, args):
        return (all(value.get(key) == args[key] for key in ("material_type", "origin") if key in args)
                and ("tag" not in args or args["tag"] in value.get("tags", [])))

    def prepare(self, ctx, args, options, uow):
        item = {"type": "topic", "id": args["material_id"]}
        self.host.items.resolve(ctx, item, uow)
        _, row = self.host.items.resource(ctx, item, uow)
        require_revision(row, options.expected_revision)
        changes = plain(args["changes"])
        builder = PlanBuilder(ctx, "materials_update", args, "Update Material classification")
        values = {"updated_at": builder.at}
        if "material_type" in changes:
            values["material_type"] = changes["material_type"]
        if "tags" in changes:
            values["tags_json"] = changes["tags"]
        builder.record("topics", row["id"], row, values)
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return {"item": self.host.items.resolve(ctx, {"type": "topic", "id": plan.arguments["material_id"]}, uow)}


class Archive:
    def __init__(self, host):
        self.host = host

    def prepare(self, ctx, args, options, uow):
        self.host.items.resolve(ctx, args["item"], uow)
        table, row = self.host.items.resource(ctx, args["item"], uow)
        require_revision(row, options.expected_revision)
        if table != "topics":
            raise DomainError("workflow_required", "Use the item's dedicated lifecycle tool")
        builder = PlanBuilder(ctx, "context_items_archive", args, "Archive context item")
        builder.record(table, row["id"], row, {"archived": int(args["archived"]), "updated_at": builder.at})
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return {"item": self.host.items.resolve(ctx, plain(plan.arguments["item"]), uow)}


class Navigation:
    def __init__(self, host):
        self.host = host

    def read(self, ctx, args, uow):
        counts = {}
        for kind in ("topic", "ticket"):
            count = 0
            for item in self.host.items.candidates(ctx, uow, kind=kind):
                try:
                    value = self.host.items.resolve(ctx, item, uow)
                except DomainError as error:
                    if error.code not in UNAVAILABLE:
                        raise
                    continue
                count += not value["raw_source"] and not value["archived"]
            counts[kind] = count
        return {"navigation_only": True, "counts": counts}


# -- the reviewable edit operations ------------------------------------------------------------------------------------

def update_prepare(table, key, operation):
    def prepare(ctx, args, options, uow):
        row = uow.repository(table, business_id=ctx.business_id).get(args[key], required=True)
        require_revision(row, options.expected_revision)
        builder = PlanBuilder(ctx, operation, args, "Update " + table)
        values = {**plain(args["changes"]), "updated_at": builder.at}
        builder.record(table, row["id"], row, values)
        if table == "topics":
            file = uow.repository("topic_files", business_id=ctx.business_id).get(row["id"], required=True)
            builder.record("topic_files", row["id"], file, {"content_hash": file_hash(values.get("body", row["body"]))})
        return builder.build()
    return prepare


CHANGES = {"type": "object", "minProperties": 1, "additionalProperties": False,
           "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "state": {"type": "string"}}}


class Domain:
    """The three reviewable operations; ``tickets_update`` applies through its own handler (a domain apply)."""

    def __init__(self):
        self.domain_applies, self.fail_domain_apply = [], False

    def entries(self):
        return (*(CatalogEntry(name, False, "read") for name in READS),
                *(CatalogEntry(name, True, "records") for name in MUTATIONS))

    def descriptors(self):
        def getter(table, key):
            return lambda ctx, args, options, uow: {"row": uow.repository(table, business_id=ctx.business_id).get(args[key], required=True)}
        return (
            OperationDescriptor("topics_get", schema_of({"topic_id": {"type": "string"}}, ("topic_id",)), effect_kind="read",
                                supports_signoff=False, read=getter("topics", "topic_id")),
            OperationDescriptor("tickets_get", schema_of({"ticket_id": {"type": "string"}}, ("ticket_id",)), effect_kind="read",
                                supports_signoff=False, read=getter("tickets", "ticket_id")),
            OperationDescriptor("topics_update", schema_of({"topic_id": {"type": "string"}, "changes": CHANGES}, ("topic_id", "changes")),
                                prepare=update_prepare("topics", "topic_id", "topics_update"), apply=self.apply_records),
            OperationDescriptor("docs_update", schema_of({"document_id": {"type": "string"}, "changes": CHANGES}, ("document_id", "changes")),
                                prepare=update_prepare("topics", "document_id", "docs_update"), apply=self.apply_records),
            OperationDescriptor("tickets_update", schema_of({"ticket_id": {"type": "string"}, "changes": CHANGES}, ("ticket_id", "changes")),
                                prepare=update_prepare("tickets", "ticket_id", "tickets_update"), apply=self.apply_ticket))

    @staticmethod
    def apply_records(ctx, plan, uow):
        return {"records": len(apply_records(ctx, plan, uow))}

    def apply_ticket(self, ctx, plan, uow):
        written = apply_records(ctx, plan, uow)
        self.domain_applies.append(plan.operation_name)
        if self.fail_domain_apply:
            raise DomainError("operation_failed", "synthetic domain failure after writing")
        return {"records": len(written)}


def schema_of(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


REVIEW_EDITS = {"topics_update": ReviewEdit("topic", "topic_id"),
                "docs_update": ReviewEdit(None, "document_id", resource_type="topics"),
                "tickets_update": ReviewEdit("ticket", "ticket_id", apply_through_domain=True)}


class Agents(FakeAgents):
    """The fake agent registry with every operation of this synthetic host allowed by default."""

    def __init__(self):
        super().__init__()
        self.defaults = {name: "allow" for name in (*RELATIONSHIP_OPERATIONS, *READS, *MUTATIONS)}


def install(registry, *contributions):
    """Trusted registration into one registry, in the order ``compose_catalog`` uses, then freeze."""
    for contribution in contributions:
        for entry in contribution.entries:
            registry.declare(entry)
    for contribution in contributions:
        for descriptor in contribution.descriptors:
            registry.register(descriptor)
    return registry.freeze()


class RelationshipHost:
    """One composed synthetic host. The ``Application`` is constructed first, over an open registry, because the
    ``ItemResolver`` takes it; the contributions are registered and the registry frozen before traffic."""

    def __init__(self, tmp_path, *, now=None, lookups=None, edits=None):
        self.agents, self.domain = Agents(), Domain()
        demo = Demo()
        demo.hooks = Hooks()
        self.database = database_at(tmp_path, demo=demo, extra=(
            StorageContribution("synthetic", TABLES, schema=SCHEMA), signoffs.storage_contribution()))
        self.locks = LockPaths.under(tmp_path / "locks")
        self.seed()
        ports = SignoffPorts(execution=FakeExecution(), peers=FakePeers(), tasks=FakeTasks(), references=FakeReferences(),
                             platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
        self.service = SignoffService(self.database, ports, locks=self.locks)
        self.registry = OperationRegistry()
        self.policy = PolicyResolver(self.registry, self.agents, rules=PolicyRules())
        self.app = Application(self.database, self.registry, policy=self.policy, signoffs=self.service,
                               writer_admission=RecoveryAdmission())
        self.notes, self.tickets = NotesLookup(self), TicketLookup(self)
        self.items = ItemResolver(self.app, lookups or (ItemContribution("notes", ("topic", "message"), self.notes),
                                                        ItemContribution("tracker", ("ticket",), self.tickets)),
                                  candidates=Candidates())
        self.ports = RelationshipPorts(roles=ROLES, workflow_roles=WORKFLOW_ROLES, materials=Materials(self),
                                       archive=Archive(self), navigation=Navigation(self),
                                       review_edits=edits or REVIEW_EDITS)
        self.relationships = RelationshipOperations(self.items, self.ports, **({"clock": now} if now else {}))
        install(self.registry, CatalogContribution("synthetic", self.domain.entries(), self.domain.descriptors()),
                self.service.contribution(), self.relationships.contribution())
        self.service.bind(self.app)

    # -- seeding ---------------------------------------------------------------------------------------------------

    def seed(self):
        with self.database.transaction() as uow:
            principals = uow.repository("principals")
            principals.insert({"id": AGENT, "kind": "agent", "display_name": "Proposer"})
            principals.insert({"id": STRANGER, "kind": "human", "display_name": "Stranger"})
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION,
                                                   "owner_principal_id": HUMAN})
            # The acting agent is global and holds no global capability or membership row.
            profile = {"status": "active", "role": {"id": "global-worker"},
                       "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []}}
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, "
                "profile_json, created_at, updated_at) VALUES (?,?,NULL,'ecosystem',?,?,?, 'now', 'now')",
                (AGENT, AGENT, "proposer", "/nonexistent/proposer", json.dumps(profile)))
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": STRANGER})

    def topic(self, identity, title="Topic", body="Body", *, business=BUSINESS, visibility="shared", owner=None,
              availability="ready", **extra):
        with self.database.transaction() as uow:
            uow.repository("topics", business_id=business).insert({
                "id": identity, "title": title, "body": body, "visibility": visibility, "owner_id": owner,
                "availability": availability, **extra})
            uow.repository("topic_files", business_id=business).insert({"id": identity, "content_hash": file_hash(body)})
        return {"type": "topic", "id": identity}

    def message(self, identity, topic, subject="Subject"):
        with self.database.transaction() as uow:
            uow.repository("messages", business_id=BUSINESS).insert({"id": identity, "topic_id": topic, "subject": subject})
        return {"type": "message", "id": identity}

    def ticket(self, identity, title="Ticket", state="open", *, business=BUSINESS):
        with self.database.transaction() as uow:
            uow.repository("tickets", business_id=business).insert({"id": identity, "title": title, "state": state})
        return {"type": "ticket", "id": identity}

    def edge(self, source, target, role="related_to", context="", *, identity=None, business=BUSINESS):
        identity = identity or new_id()
        with self.database.transaction() as uow:
            uow.repository("relationships", business_id=business).insert({
                "id": identity, "from_type": source["type"], "from_id": source["id"], "to_type": target["type"],
                "to_id": target["id"], "role": role, "context": context})
        return identity

    # -- contexts and calls ----------------------------------------------------------------------------------------

    @staticmethod
    def owner(business=BUSINESS, **kwargs):
        return InvocationContext(Principal(HUMAN, "human"), business_id=business,
                                 allowed_business_ids=frozenset({business}), source="dashboard", **kwargs)

    @staticmethod
    def stranger(**kwargs):
        return InvocationContext(Principal(STRANGER, "human"), business_id=BUSINESS,
                                 allowed_business_ids=frozenset({BUSINESS}), source="dashboard", **kwargs)

    @staticmethod
    def agent(**kwargs):
        return InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS,
                                 allowed_business_ids=frozenset({BUSINESS}), profile_revision=1, source="mcp", **kwargs)

    def invoke(self, operation, arguments=None, ctx=None, **options):
        options.setdefault("idempotency_key", new_id())
        return self.app.invoke(ctx or self.owner(), operation, arguments or {}, options)

    def read(self, operation, arguments=None, ctx=None):
        result = self.app.invoke(ctx or self.owner(), operation, arguments or {}, {})
        assert result.status == "ok", result.error
        return plain(result.data)

    def ok(self, operation, arguments=None, ctx=None, **options):
        result = self.invoke(operation, arguments, ctx, **options)
        assert result.status in {"ok", "signoff_required"}, result.error
        return result

    # -- reads of stored state -------------------------------------------------------------------------------------

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [decode_row(row) for row in uow.connection.execute(sql, parameters)]

    def edges(self, business=BUSINESS):
        return self.query("SELECT * FROM relationships WHERE business_id IS ? ORDER BY created_at, rowid", business)

    def reviews(self):
        return self.query("SELECT * FROM context_reviews ORDER BY created_at, rowid")

    def states(self):
        return self.query("SELECT * FROM context_review_states ORDER BY rowid")

    def topic_row(self, identity):
        return self.query("SELECT * FROM topics WHERE id=?", identity)[0]

    def count(self, table, where="1"):
        return self.query(f"SELECT count(*) AS n FROM {table} WHERE {where}")[0]["n"]

    # -- Signoff helpers (an agent's staged proposal, an owner's decision) -----------------------------------------

    def row(self, signoff_id):
        return self.query("SELECT * FROM signoffs WHERE id=?", signoff_id)[0]

    def approve(self, signoff_id, ctx=None):
        row = self.row(signoff_id)
        return self.service.review(ctx or self.owner(), signoff_id, decision="approve", proposal_revision=row["proposal_revision"],
                                   payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                                   idempotency_key=new_id(), feedback=None)

    # -- review helpers --------------------------------------------------------------------------------------------

    def pending(self, item, ctx=None, **filters):
        return self.read("context_reviews_list", {"item": item, **filters}, ctx)

    def decision(self, item, edge_id, result="incorporated", explanation="Reviewed", ctx=None):
        status = next(entry for entry in self.pending(item, ctx, include_reviewed=True)["items"] if entry["relationship_id"] == edge_id)
        return {"relationship_id": edge_id, "relationship_revision": status["relationship_revision"],
                "review_revision": status["review_revision"], "source_token": status["source_token"],
                "result": result, "explanation": explanation}

    def review(self, item, edge_id, *, ctx=None, edit=None, revision=None, **kwargs):
        arguments = {"item": item, "items": [self.decision(item, edge_id, ctx=ctx, **kwargs)]}
        if edit:
            arguments["edit"] = edit
        if revision is None:
            revision = self.read("context_items_get", {"item": item}, ctx)["item"]["revision"]
        return self.invoke("context_review", arguments, ctx, expected_revision=revision)
