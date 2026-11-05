"""Synthetic composition for the focused checks: a tiny fake domain, never a copy of the host.

Everything here is built in temporary directories from the package's own source-derived core schema. The ``items``
and ``documents`` tables, the hooks and the policy stand in for the retained domains a trusted host would contribute.
"""

from __future__ import annotations

from pathlib import Path

from qorqut_operations.contracts import (ChangePreview, ChangeSet, ExpectedTarget, FieldChange, InvocationContext,
    PolicyDecision, PreparedChange, PreviewResource, Principal, ResourceRef, StructuredPreview)
from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution,
    StorageContribution, TableSpec, TransactionContribution)

HUMAN = "00000000-0000-4000-8000-000000000001"
BUSINESS = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
AGENT = "00000000-0000-4000-8000-000000000004"
WORKER = "00000000-0000-4000-8000-000000000005"

DEMO_SCHEMA = (
    """CREATE TABLE items (
        business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'todo', created_by TEXT REFERENCES principals(id),
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (business_id, id))""",
    """CREATE TABLE documents (
        business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, kind TEXT NOT NULL,
        path TEXT NOT NULL, content_hash TEXT NOT NULL, availability TEXT NOT NULL DEFAULT 'ready',
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (business_id, id))""",
    """CREATE TABLE demo_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')""",
)


class DemoLifecycle:
    """Document availability, as v1 kept it beside the receipts."""

    def __init__(self):
        self.events = []

    def _set(self, uow, resource, state):
        self.events.append((state, resource.resource_id))
        uow.connection.execute("UPDATE documents SET availability=? WHERE business_id IS ? AND id=?",
                               (state, resource.business_id, resource.resource_id))

    def applying(self, uow, resource): self._set(uow, resource, "applying")
    def conflicted(self, uow, resource): self._set(uow, resource, "conflicted")
    def applied(self, uow, resource): self._set(uow, resource, "ready")
    def released(self, uow, resource): self._set(uow, resource, "ready")


class DemoReceiptHistory:
    def __init__(self):
        self.captured = []

    def capture_receipt(self, uow, receipt, plan):
        self.captured.append(receipt["id"])
        uow.connection.execute("INSERT INTO demo_history(kind, detail) VALUES ('receipt', ?)", (receipt["id"],))


class DemoRecordHooks:
    tracked_tables = frozenset({"items"})

    def __init__(self):
        self.offered, self.applied = [], []

    def apply_effect(self, context, plan, change, uow, repository, key, values):
        self.offered.append((change.resource.resource_type, change.action))
        if change.action == "stamp":  # a domain-owned effect: the engine does not interpret it
            return [repository.insert({**values, "title": values["title"].upper()})]
        return None

    def force_revision(self, plan, change):
        return plan.operation_name == "items_touch"

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        self.applied.append((table, before and before["revision"], after and after["revision"]))
        uow.connection.execute("INSERT INTO demo_history(kind, detail) VALUES ('record', ?)", (table,))


class RecordingPublisher:
    def __init__(self):
        self.log, self.fail_publish = [], False

    def capture(self, connection):
        self.log.append("capture")
        return connection.execute("SELECT count(*) FROM demo_history").fetchone()[0]

    def publish_commit(self, *, captured, operation, business_id, wrote=True):
        self.log.append(("publish", operation, wrote))
        if self.fail_publish:
            raise RuntimeError("subscriber failed")


def finalizer(uow):
    uow.connection.execute("INSERT INTO demo_history(kind) VALUES ('finalizer')")


class Demo:
    """One synthetic composition and the hook objects the tests inspect."""

    def __init__(self):
        self.lifecycle, self.history = DemoLifecycle(), DemoReceiptHistory()
        self.hooks, self.publisher = DemoRecordHooks(), RecordingPublisher()

    def builder(self, *, version=1, with_publisher=True, finalize=True, extra=(), with_lifecycle=True):
        builder = (AssemblyBuilder(target_name="synthetic", target_version=version)
            .add(StorageContribution("demo", {
                "items": TableSpec(("business_id", "id"), "business_id"),
                "documents": TableSpec(("business_id", "id"), "business_id"),
                "demo_history": TableSpec(("id",), mutable=False, revision_column=None)}, schema=DEMO_SCHEMA,
                required_lifecycles=frozenset({"documents"})))  # the synthetic domain cannot track documents without it
            .add(TransactionContribution("demo", (finalizer,) if finalize else (),
                                         self.publisher if with_publisher else None))
            .add(FileEffectContribution("demo", {"documents": self.lifecycle} if with_lifecycle else {}, self.history))
            .add(RecordEffectContribution("demo", self.hooks)))
        for contribution in extra:
            builder.add(contribution)
        return builder

    def assembly(self, **kwargs):
        return self.builder(**kwargs).freeze()


def database_at(tmp_path, *, demo=None, seed=True, **assembly):
    demo = demo or Demo()
    database = Database(Path(tmp_path) / "state.sqlite3", assembly=demo.assembly(**assembly)).initialize_target()
    database.demo = demo
    if seed:
        with database.transaction() as uow:
            uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
            for identity, name in ((BUSINESS, "first"), (OTHER, "second")):
                uow.repository("businesses").insert({"id": identity, "slug": name, "name": name,
                    "workspace": str(Path(tmp_path) / name), "hq_path": str(Path(tmp_path) / name / "hq")})
                uow.repository("business_memberships", business_id=identity).insert({"principal_id": HUMAN, "is_owner": True})
        demo.publisher.log.clear()
    return database


def context(**kwargs):
    return InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS,
                             allowed_business_ids=frozenset({BUSINESS}), source="dashboard", **kwargs)


class FixedPolicy:
    def __init__(self, mode="allow", overrides=None):
        self.mode, self.overrides = mode, overrides or {}
        self.calls = []

    def resolve(self, ctx, operation, targets, uow):
        self.calls.append(operation.name)
        return PolicyDecision(self.overrides.get(operation.name, self.mode), "test")


def item_plan(ctx, arguments, *, exists=False, revision=None, operation="items_create", extra=()):
    resource = ResourceRef("items", arguments["id"], ctx.business_id)
    label = PreviewResource("items", arguments["id"], arguments.get("title", "Item"))
    return ChangeSet(operation, 1, ctx.principal_id, "business:" + ctx.business_id, arguments,
        {"kind": "business", "business_id": ctx.business_id},
        (ExpectedTarget(resource, revision=revision, exists=exists),),
        (PreparedChange("record", resource, "insert", {"id": arguments["id"], "title": arguments.get("title", "Item"),
             "status": "todo", "created_by": ctx.principal_id}), *extra),
        ChangePreview("Create item", "", {"kind": "business", "business_id": ctx.business_id}, (label,),
            (StructuredPreview(label, (FieldChange("title", None, arguments.get("title", "Item")),)),)))
