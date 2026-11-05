"""TEST-ONLY contract double of the Operations Domain package ``qorqut_operations.crm``.

The Domain worker's package is not part of this source group, yet the host composes it. When ``qorqut_operations.crm`` is
absent, ``install()`` registers a synthetic module that follows ``CONTRACT.md`` (exports, schema deltas, activity shape),
and rewrites the Operations DDL text exactly as the contract says the Domain will (``data_tables.table_kind`` /
``crm_config_json`` and ``'table'`` discussion items). Nothing here ships in the product, and nothing here is evidence
that the real Domain behaves this way: tests that use it are *contract tests with a double*, not integration tests.

When the real package is importable, ``install()`` does nothing and the same tests run against it.

The double implements only what the host reads: ``local_activity`` and ``resolve_record`` (plus a real
``tables_thread_get`` read), the table/catalog declarations, and inert handlers for the Domain's writes."""

from __future__ import annotations

import importlib.util
import sys
import types

CONTRACT_DOUBLE = "qorqut_operations.crm.CONTRACT_DOUBLE"

DATA_TABLES_TAIL = "    PRIMARY KEY(business_id,id)\n)"
DATA_TABLES_COLUMNS = ("    table_kind TEXT NOT NULL DEFAULT 'general' CHECK(table_kind IN ('general','crm')),\n"
                       "    crm_config_json TEXT CHECK(crm_config_json IS NULL OR json_valid(crm_config_json)),\n")
CRM_SCHEMA = (
    """CREATE TABLE crm_entries (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    record_business_id TEXT REFERENCES businesses(id),
    record_task_id TEXT NOT NULL,
    stage_id TEXT,
    position INTEGER NOT NULL CHECK(position>=0),
    cells_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(cells_json)),
    removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    FOREIGN KEY(business_id,table_id) REFERENCES data_tables(business_id,id)
)""",
    """CREATE INDEX crm_entries_table ON crm_entries(business_id,table_id,position,id)""",
    """CREATE TABLE crm_activity_events (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('email','message','development')),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    summary TEXT NOT NULL,
    body TEXT,
    source_json TEXT NOT NULL CHECK(json_valid(source_json)),
    event_key TEXT NOT NULL,
    PRIMARY KEY(business_id,id)
)""",
    """CREATE UNIQUE INDEX crm_activity_events_key ON crm_activity_events(coalesce(business_id,''),task_id,event_key)""",
)


def installed() -> bool:
    return getattr(sys.modules.get("qorqut_operations.crm"), "CONTRACT_DOUBLE", False) is True


def install() -> bool:
    """Install the double unless the real Domain package exists. Returns whether the double is active."""
    if installed():
        return True
    if "qorqut_operations.crm" in sys.modules or importlib.util.find_spec("qorqut_operations.crm") is not None:
        return False
    _patch_ddl()
    _patch_tables()
    module = _module()
    sys.modules["qorqut_operations.crm"] = module
    import qorqut_operations
    qorqut_operations.crm = module
    return True


def _patch_ddl():
    from qorqut_operations import tables
    from qorqut_operations.discussion import follows, schema as discussion
    import qorqut_operations.discussion as discussion_package
    import qorqut_operations.tables.schema as tables_schema

    patched = []
    for statement in tables_schema.SCHEMA:
        if statement.startswith('CREATE TABLE "data_tables"'):
            if DATA_TABLES_TAIL not in statement:
                raise RuntimeError("Operations data_tables DDL changed; update the contract double")
            statement = statement.replace(DATA_TABLES_TAIL, DATA_TABLES_COLUMNS + DATA_TABLES_TAIL)
        patched.append(statement)
    tables_schema.SCHEMA = tables.SCHEMA = tuple(patched)

    needle = "item_type IN ('task','action')"
    threads = tuple(statement.replace(needle, "item_type IN ('task','action','table')") for statement in discussion.THREAD_SCHEMA)
    if sum(statement.count("'table')") for statement in threads) != 2:
        raise RuntimeError("Operations discussion DDL changed; update the contract double")
    discussion.THREAD_SCHEMA = threads
    discussion.ITEM_TYPES = follows.ITEM_TYPES = discussion_package.ITEM_TYPES = ("task", "action", "table")


def _patch_tables():
    from qorqut_operations.tables import TableOperations

    def bind_crm(self, crm):
        if getattr(self, "crm", None) is not None:
            raise RuntimeError("The Table service is already bound to a CRM")
        self.crm = crm
        return self

    TableOperations.bind_crm = bind_crm


def _module():
    from qorqut_operations.application import OperationDescriptor
    from qorqut_operations.application.plans import IDENTITY, PAGE, UUID, page, schema, timestamp, utc_clock
    from qorqut_operations.application.registry import CatalogEntry
    from qorqut_operations.contracts import DomainError
    from qorqut_operations.discussion import ThreadItem
    from qorqut_operations.discussion.threads import read_messages
    from qorqut_operations.history_exports import SEPARATE_HISTORY, TableClassification
    from qorqut_operations.storage.assembly import StorageContribution
    from qorqut_operations.storage.specs import TableSpec
    from qorqut_operations.tables.operations import table_context, table_namespace
    from qorqut_operations.tasks import links as task_links

    READ_NAMES = ("crm_entries_list", "tables_thread_get")
    MUTATION_NAMES = ("crm_entries_add", "crm_entries_update", "crm_entries_remove", "tables_comment", "crm_activity_record")
    FOLLOW = "tables_follow"
    TABLE_NAMES = ("crm_entries", "crm_activity_events")
    TABLES = {"crm_entries": TableSpec(("business_id", "id"), "business_id"),
              "crm_activity_events": TableSpec(("business_id", "id"), "business_id", False, None)}
    IMMUTABLE_FIELDS = {"crm_activity_events": frozenset({"business_id", "task_id", "kind", "occurred_at", "recorded_at",
                                                          "recorded_by", "summary", "body", "source_json", "event_key"})}
    CATALOG_ENTRIES = (*(CatalogEntry(name, False, "read") for name in READ_NAMES),
                       *(CatalogEntry(name, True, "records") for name in MUTATION_NAMES),
                       CatalogEntry(FOLLOW, False, "records"))
    CLASSIFICATIONS = tuple(TableClassification(name, SEPARATE_HISTORY, "Contract double classification.", "qorqut-tables")
                            for name in TABLE_NAMES)
    SCOPE = {"scope": {"enum": ["business", "global"]}}

    def unavailable(*_args, **_kwargs):
        raise DomainError("operation_unavailable", "The CRM Domain operation is a contract double")

    class CRMOperations:
        """Synthetic Domain: just enough surface for the host's composition and the aggregate activity read."""

        def __init__(self, application, tables, tasks, items, *, clock=utc_clock):
            self.application, self.tables, self.tasks, self.items, self.clock = application, tables, tasks, items, clock

        def register(self, registry):
            scoped = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                          resolve_namespace=table_namespace)
            registry.register(OperationDescriptor(
                "crm_entries_list", schema({**SCOPE, **PAGE, "table_id": UUID}, ("table_id",)), effect_kind="read",
                supports_signoff=False, description="Contract double of the CRM entries read.", **scoped,
                read=lambda ctx, args, options, uow: {"items": [], "next_cursor": None, "table_revision": 1}))
            registry.register(OperationDescriptor(
                "tables_thread_get", schema({**SCOPE, **PAGE, "table_id": UUID}, ("table_id",)), effect_kind="read",
                supports_signoff=False, description="Contract double of the Table discussion read.", **scoped,
                read=self.thread_get))
            writes = {"crm_entries_add": ({"table_id": UUID}, ("table_id",)),
                      "crm_entries_update": ({"table_id": UUID, "entry_id": IDENTITY}, ("table_id", "entry_id")),
                      "crm_entries_remove": ({"table_id": UUID, "entry_id": IDENTITY}, ("table_id", "entry_id")),
                      "tables_comment": ({"table_id": UUID, "text": {"type": "string", "minLength": 1, "maxLength": 20000}},
                                         ("table_id", "text")),
                      "crm_activity_record": ({"task_id": IDENTITY}, ("task_id",))}
            for name, (fields, required) in writes.items():
                options = dict(scoped, resolve_namespace=table_namespace if name != "crm_activity_record" else None)
                if name == "crm_activity_record":
                    from qorqut_operations.tasks.scope import task_namespace
                    options["resolve_namespace"] = task_namespace
                registry.register(OperationDescriptor(
                    name, schema({**SCOPE, **fields}, required), description="Contract double of a CRM write.",
                    prepare=unavailable, apply=unavailable, validate_targets=unavailable, **options))
            registry.register(OperationDescriptor(
                FOLLOW, schema({**SCOPE, "table_id": UUID, "following": {"type": "boolean"}}, ("table_id", "following")),
                effect_kind="records", supports_signoff=False, description="Contract double of the Table follow preference.",
                prepare=unavailable, apply=unavailable, validate_targets=unavailable, **scoped))
            return self

        def thread_get(self, ctx, args, options, uow):
            ctx = table_context(ctx, args, uow)
            uow.repository("data_tables", business_id=ctx.business_id).get(args["table_id"], required=True)
            history = read_messages(uow, ThreadItem(ctx.business_id, "table", args["table_id"]), args, "tables_thread_get",
                                    ctx.principal_id)
            return {"thread_id": history["thread_id"], "notes": history["messages"], "next_cursor": history["next_cursor"]}

        def resolve_record(self, ctx, ref, uow, builder=None):
            """The authorized Task row (the real Domain returns the public Task DTO)."""
            row = task_links.resolve(self.tasks, ctx, {"business_id": ref["business_id"], "task_id": ref["id"]}, uow, builder)
            return dict(row)

        def local_activity(self, ctx, task_id, args, uow):
            """Own discussion plus recorded observations of the Task, newest first, no later than ``as_of``."""
            as_of = args.get("as_of") or timestamp(self.clock)
            scope = ctx.business_id or "global"
            items = []
            for row in uow.connection.execute(
                    "SELECT * FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id=? AND created_at<=?",
                    (ctx.business_id, task_id, as_of)):
                comment = row["kind"] == "comment"
                items.append({"id": f"task:{scope}:{task_id}:own:{row['id']}", "kind": "comment" if comment else "development",
                              "occurred_at": row["created_at"], "actor": {"id": row["author_id"]} if row["author_id"] else None,
                              "summary": row["summary"] or row["body"][:500], "source": {"type": "task", "id": task_id, "business_id": ctx.business_id},
                              "origin": "own", **({"body": row["body"]} if comment else {})})
            import json
            for row in uow.connection.execute(
                    "SELECT * FROM crm_activity_events WHERE business_id IS ? AND task_id=? AND recorded_at<=?",
                    (ctx.business_id, task_id, as_of)):
                items.append({"id": f"recorded:{scope}:{task_id}:{row['id']}", "kind": row["kind"], "occurred_at": row["occurred_at"],
                              "recorded_at": row["recorded_at"], "summary": row["summary"], "body": row["body"],
                              "source": json.loads(row["source_json"]), "origin": "recorded"})
            selected, cursor = page(items, {"task_id": task_id, "as_of": as_of, "limit": min(args.get("limit", 50), 100),
                                            **({"cursor": args["cursor"]} if args.get("cursor") else {})},
                                    ctx.business_id, "crm_activity_local", key=lambda item: (item["occurred_at"], item["id"]))
            return {"items": selected, "next_cursor": cursor, "as_of": as_of}

    def storage_contribution():
        return StorageContribution("crm", TABLES, IMMUTABLE_FIELDS, CRM_SCHEMA,
                                   required_tables=frozenset({"data_tables", "thread_messages", "tasks"}))

    module = types.ModuleType("qorqut_operations.crm")
    module.__spec__ = importlib.util.spec_from_loader("qorqut_operations.crm", loader=None)
    module.__dict__.update(CONTRACT_DOUBLE=True, CRMOperations=CRMOperations, CATALOG_ENTRIES=CATALOG_ENTRIES,
                           READ_NAMES=READ_NAMES, MUTATION_NAMES=MUTATION_NAMES + (FOLLOW,), TABLE_NAMES=TABLE_NAMES,
                           TABLES=TABLES, IMMUTABLE_FIELDS=IMMUTABLE_FIELDS, SCHEMA=CRM_SCHEMA, CLASSIFICATIONS=CLASSIFICATIONS,
                           storage_contribution=storage_contribution)
    return module
