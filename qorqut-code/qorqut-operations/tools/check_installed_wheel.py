"""Build the wheel, install it into a disposable virtual environment and import it with no source tree in sight.

    python tools/check_installed_wheel.py [workdir [first-party-wheel-directory]]

Nothing global or live is touched: the wheel is built from this checkout into ``workdir`` (a temporary directory by
default), a fresh venv is created there, the wheel is installed with its runtime dependencies, and a short synthetic
script is run from an unrelated working directory with a clean ``sys.path``. The script proves that

* the package imports from the installed wheel (not from the checkout),
* no module named ``bazarlyk`` is importable or has been imported,
* a trusted assembly freezes, a store initializes in a temporary path and an operation applies with its idempotent
  replay, using only the installed package,
* a second script runs a small actual Signoff slice (a restricted agent's proposal is approved by the owner and
  applied once, through typed ``SignoffPorts``) and a registered-program slice (a manual routine run executes a
  synthetic program whose frozen output is applied under its worker identity), again with no ``bazarlyk``,
* a third script runs one neutral-item/relationship slice over two synthetic item kinds: an authorized link versus
  a denied endpoint, a read that acknowledges nothing, and an explicit review committed with a same-item edit,
* a fourth script runs the ``create_directory_tree`` file effect through the installed ``Application`` and prepared
  file executor: an empty layout is published once under its receipt, the same key replays, and an existing
  directory, even an identical empty tree, is never adopted.

The Signoff slice acts as an active global agent (a supported role, an ecosystem scope, no home project): its project
access is the active project directory, never a membership row, exactly as the engine requires of every agent.

The build needs ``setuptools`` and ``wheel`` available to ``pip`` (pip fetches them into the isolated build
environment) and a package index for ``jsonschema``, ``markdown-it-py`` and ``PyYAML``. The fourth runtime dependency,
the first-party ``qorqut-business-docs-versioning==0.2.0`` document-history library, is not on an index: give a directory
holding its built wheel as the second argument (or ``QORQUT_FIRST_PARTY_WHEELS``) and the install uses it as a find-links
location. The first script also proves the installed wheel brings both document libraries and still loads no host,
Context, Paper or content module.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import importlib.util, json, sys, tempfile
from pathlib import Path

assert importlib.util.find_spec("bazarlyk") is None, "bazarlyk must not be importable"
import qorqut_operations
from qorqut_operations.application import Application, CatalogContribution, OperationDescriptor, compose_catalog
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.contracts import InvocationContext, Principal, PolicyDecision
from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution,
    StorageContribution, TableSpec)

location = Path(qorqut_operations.__file__).resolve()
assert "site-packages" in location.parts, location
assert not any(name == "bazarlyk" or name.startswith("bazarlyk.") for name in sys.modules)
# The source-document and exact-history packages import their two document libraries; both come with the wheel.
import markdown_it, qorqut_business_docs_versioning
import qorqut_operations.document_history.reads, qorqut_operations.documents.library
assert "site-packages" in Path(qorqut_business_docs_versioning.__file__).resolve().parts
assert not any(name.partition(".")[0] in {"bazarlyk", "qorqut_context", "qorqut_paper", "qorqut_content"} for name in sys.modules)

class History:
    def capture_receipt(self, uow, receipt, plan): pass
class Hooks:
    tracked_tables = frozenset()
    def apply_effect(self, *args): return None
    def force_revision(self, plan, change): return False
    def record_applied(self, uow, plan, table, before, after, *, actor_id=None): pass

assembly = (AssemblyBuilder(target_name="wheel-probe", target_version=1)
    .add(StorageContribution("probe", {"things": TableSpec(("business_id", "id"), "business_id")}, schema=(
        "CREATE TABLE things (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, title TEXT NOT NULL, "
        "revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))",)))
    .add(FileEffectContribution("probe", receipt_history=History()))
    .add(RecordEffectContribution("probe", Hooks())).freeze())

HUMAN, BUSINESS = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
with tempfile.TemporaryDirectory(prefix="qops-wheel-probe-") as directory:
    database = Database(Path(directory) / "probe.sqlite3", assembly=assembly).initialize_target()
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "p", "name": "p", "workspace": directory + "/w",
                                             "hq_path": directory + "/w/hq"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": HUMAN, "is_owner": True})
    def prepare(ctx, args, options, uow):
        builder = PlanBuilder(ctx, "things_create", args, "Create")
        builder.record("things", args["id"], None, {"id": args["id"], "title": args["title"]})
        return builder.build()
    descriptor = OperationDescriptor("things_create", {"type": "object", "required": ["id", "title"],
        "properties": {"id": {"type": "string"}, "title": {"type": "string"}}}, prepare=prepare,
        apply=lambda ctx, plan, uow: {"rows": apply_records(ctx, plan, uow)})
    class Allow:
        def resolve(self, ctx, operation, targets, uow): return PolicyDecision("allow", "probe")
    app = Application(database, compose_catalog([CatalogContribution("probe", descriptors=(descriptor,))]).registry,
                      policy=Allow())
    context = InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}),
                                source="dashboard")
    first = app.invoke(context, "things_create", {"id": "a", "title": "A"}, {"idempotency_key": "k"})
    again = app.invoke(context, "things_create", {"id": "a", "title": "A"}, {"idempotency_key": "k"})
    changed = app.invoke(context, "things_create", {"id": "a", "title": "B"}, {"idempotency_key": "k"})
    assert first.status == again.status == "ok" and first.data == again.data, (first, again)
    assert changed.error["code"] == "idempotency_conflict", changed
    database.close()
    reopened = Database(Path(directory) / "probe.sqlite3", assembly=assembly).open_existing()
    with reopened.read(business_id=BUSINESS) as uow:
        assert [row["id"] for row in uow.repository("things").list()] == ["a"]
    reopened.close()
assert not any(name == "bazarlyk" or name.startswith("bazarlyk.") for name in sys.modules)
print(json.dumps({"version": qorqut_operations.__version__, "location": str(location)}))
'''


SLICE = r'''
import importlib.util, json, sys, tempfile
from dataclasses import replace
from pathlib import Path

assert importlib.util.find_spec("bazarlyk") is None, "bazarlyk must not be importable"
from qorqut_operations import background, signoffs
from qorqut_operations.application import (Application, CatalogContribution, CatalogEntry, OperationDescriptor,
                                           compose_catalog)
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.background import BackgroundAuthority, BackgroundService, Program, ProgramRegistry, ProgramResult
from qorqut_operations.contracts import InvocationContext, Principal
from qorqut_operations.global_agents import effective_business_ids
from qorqut_operations.policy import AgentPolicyBasis, PolicyResolver, PolicyRules
from qorqut_operations.roles import GLOBAL_WORKER_ROLE
from qorqut_operations.routines import RoutineOperations
from qorqut_operations.signoffs import ABSENT, ReferenceTarget, SignoffPorts, SignoffService
from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, LockPaths,
                                       RecordEffectContribution, StorageContribution, TableSpec)
from qorqut_operations.storage.admission import RecoveryAdmission

HUMAN, BUSINESS, AGENT = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 4))
INSTALLATION = "11111111-1111-4111-8111-111111111111"

class History:
    def capture_receipt(self, uow, receipt, plan): pass
class Hooks:
    tracked_tables = frozenset()
    def apply_effect(self, *args): return None
    def force_revision(self, plan, change): return False
    def record_applied(self, uow, plan, table, before, after, *, actor_id=None): pass
class Execution:
    execution_states = ("not_started", "applied")
    def execution_state(self, row, uow=None): return "applied" if row["state"] == "applied" else "not_started"
    def execution_sql(self, alias): return "'not_started'"
class Peers:
    def configured_ordubashy(self, uow, *, require_codex): return None
class Tasks:
    def open_assigned_count(self, uow, business_id, principal_id, now): return 0
class References:
    targets = {"thing": ReferenceTarget("things_get", "thing_id")}
    def task_namespace(self, ctx, args): return "business:" + (ctx.business_id or "")
class Agents:
    # A current global agent: no home project, and every project active in this transaction is in its scope.
    def refresh_invocation_context(self, ctx, uow):
        row = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (ctx.principal_id,)).fetchone()
        return replace(ctx, home_business_id=None, profile_revision=row[0],
                       allowed_business_ids=effective_business_ids(uow.connection, ctx.principal_id))
    def policy_basis(self, ctx, uow):
        return AgentPolicyBasis({"things_create": "signoff"}, {}, {"id": GLOBAL_WORKER_ROLE, "version": "1"}, 1)
    def plugin_declares(self, ctx, basis, descriptor, uow): return True

THINGS = ("CREATE TABLE things (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, "
          "title TEXT NOT NULL, created_by TEXT, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
          "updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))")
DOCUMENTS = ("CREATE TABLE documents (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, "
             "kind TEXT NOT NULL, path TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
             "updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))")
INBOX = ("CREATE TABLE inbox_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL, "
         "action TEXT NOT NULL, actor_id TEXT, before_json TEXT, after_json TEXT)")

def prepare(ctx, args, options, uow):
    builder = PlanBuilder(ctx, "things_create", args, "Create thing")
    builder.record("things", args["id"], None, {"id": args["id"], "title": args["title"], "created_by": ctx.principal_id})
    return builder.build()

def run_program(control, arguments):
    return ProgramResult(outputs=({"operation": "things_create",
                                   "arguments": {"id": "from-program", "title": arguments["label"]}},))

with tempfile.TemporaryDirectory(prefix="qops-wheel-slice-") as directory:
    root = Path(directory)
    assembly = (AssemblyBuilder(target_name="wheel-slice", target_version=1)
        .add(StorageContribution("probe", {"things": TableSpec(("business_id", "id"), "business_id"),
            "documents": TableSpec(("business_id", "id"), "business_id"),
            "inbox_changes": TableSpec(("id",), mutable=False, revision_column=None)},
            schema=(THINGS, DOCUMENTS, INBOX)))
        .add(StorageContribution("signoffs-needs", required_tables=signoffs.SIGNOFF_REQUIRED_TABLES))
        .add(background.storage_contribution())
        .add(FileEffectContribution("probe", receipt_history=History()))
        .add(RecordEffectContribution("probe", Hooks())).freeze())
    database = Database(root / "slice.sqlite3", assembly=assembly).initialize_target()
    locks = LockPaths.under(root / "locks")
    ports = SignoffPorts(execution=Execution(), peers=Peers(), tasks=Tasks(), references=References(),
                         platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
    service = SignoffService(database, ports, locks=locks)
    programs = ProgramRegistry()
    program = programs.register(Program("probe.report", 1, {"type": "object", "properties": {"label": {"type": "string"}},
        "required": ["label"], "additionalProperties": False}, run=run_program, permissions={"things_create": "allow"},
        outputs=("things_create",), resource_types=("things",)))
    # The definition digest is a function of the definition alone: this is the deployed v1 module's value.
    golden = Program("demo.report", 3, {"type": "object", "properties": {"count": {"type": "integer"},
        "label": {"type": "string"}}, "additionalProperties": False}, run=lambda control, arguments: None,
        permissions={"items_create": "allow"}, outputs=("items_create",), resource_types=("items",),
        discretionary=("items_touch",), global_capabilities=("manage_global_routines",), timeout_seconds=120,
        scope_kinds=("business", "global"))
    assert golden.definition["digest"] == "7c69b0cfb7766190ec67617e02bee23ad15e71e6ab8b488184942862ad8f56d5"
    authority = BackgroundAuthority(database, programs)
    programs.freeze()
    operations = RoutineOperations(programs, authority)
    entry = CatalogEntry("things_create", True, "records")
    descriptor = OperationDescriptor("things_create", {"type": "object", "required": ["id", "title"], "properties": {
        "id": {"type": "string"}, "title": {"type": "string"}}}, prepare=prepare,
        apply=lambda ctx, plan, uow: {"rows": apply_records(ctx, plan, uow)})
    catalog = compose_catalog([CatalogContribution("probe", (entry,), (descriptor,)), service.contribution(),
                               operations.contribution()], include_batch=False)
    policy = PolicyResolver(catalog.registry, Agents(), rules=PolicyRules(), worker_profiles=authority.worker_profiles,
        job_consequence_verifier=authority.verify_consequence, job_plan_validator=authority.validate_plan)
    app = Application(database, catalog.registry, policy=policy, signoffs=service, writer_admission=RecoveryAdmission())
    service.bind(app)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
        uow.repository("principals").insert({"id": AGENT, "kind": "agent", "display_name": "Proposer"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "p", "name": "p", "workspace": directory + "/w",
                                             "hq_path": directory + "/w/hq"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": HUMAN, "is_owner": True})
        uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": HUMAN})
        uow.connection.execute("INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, "
            "workspace, profile_json, created_at, updated_at) VALUES (?,?,NULL,'ecosystem',?,?,?, 'now', 'now')",
            (AGENT, AGENT, "proposer", directory + "/agent",
             json.dumps({"status": "active", "role": {"id": GLOBAL_WORKER_ROLE}, "scope": {"kind": "ecosystem"}})))
        workers = authority.register_workers(uow, [BUSINESS])

    # Signoff slice: a restricted global agent selects the project and stages, the owner approves the reviewed binding,
    # the engine applies once.
    agent = InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS,
        allowed_business_ids=frozenset({BUSINESS}), profile_revision=1, source="mcp")
    owner = InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS,
        allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
    staged = app.invoke(agent, "things_create", {"id": "a", "title": "A"}, {"idempotency_key": "stage"})
    assert staged.status == "signoff_required", staged
    reference = staged.signoff
    refused = service.review(agent, reference["id"], decision="approve", proposal_revision=1,
        payload_hash=reference["payload_hash"], expected_status_revision=1, idempotency_key="agent")
    assert refused.error["code"] == "forbidden", refused
    stale = service.review(owner, reference["id"], decision="approve", proposal_revision=1, payload_hash="0" * 64,
        expected_status_revision=1, idempotency_key="stale")
    assert stale.error["code"] == "stale_signoff", stale
    approved = service.review(owner, reference["id"], decision="approve", proposal_revision=1,
        payload_hash=reference["payload_hash"], expected_status_revision=1, idempotency_key="approve")
    assert approved.status == "accepted", approved
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("things").list() == []         # approval alone applies nothing
    assert service.run_one(reference["id"])["state"] == "applied" and service.run_one(reference["id"]) is None
    replay = service.review(owner, reference["id"], decision="approve", proposal_revision=1,
        payload_hash=reference["payload_hash"], expected_status_revision=1, idempotency_key="approve")
    assert replay.data["replayed"] is True

    # Registered-program slice: a manual routine run executes the program; its frozen output is applied by its worker.
    created = app.invoke(owner, "routines_create", {"definition": {"name": "Probe", "schedule": "0 3 * * *",
        "timezone": "UTC", "status": "active", "program": "probe.report", "arguments": {"label": "L"}, "target": "business"}},
        {"idempotency_key": "routine"})
    assert created.status == "ok", created
    routine = next(row for row in created.data["records"] if "job_kind" in row)
    ran = app.invoke(owner, "routines_run", {"routine_id": routine["id"]},
                     {"idempotency_key": "run", "expected_revision": routine["revision"]})
    assert ran.status == "ok", ran
    background_service = BackgroundService(app, programs, authority, locks=locks)
    outcome = background_service.run_one()
    assert outcome["state"] == "succeeded" and background_service.run_one() is None, outcome
    with database.read(business_id=BUSINESS) as uow:
        things = {row["id"]: row for row in uow.repository("things").list()}
    assert sorted(things) == ["a", "from-program"], things
    assert things["a"]["created_by"] == AGENT and things["from-program"]["created_by"] in workers
    database.close()
assert not any(name == "bazarlyk" or name.startswith("bazarlyk.") for name in sys.modules)
print(json.dumps({"signoff": "applied-once", "program": "succeeded", "digest": golden.definition["digest"][:12]}))
'''


NEUTRAL = r'''
import importlib.util, json, sqlite3, sys, tempfile
from pathlib import Path

assert importlib.util.find_spec("bazarlyk") is None, "bazarlyk must not be importable"
from qorqut_operations.application import Application, CatalogContribution, CatalogEntry, OperationDescriptor, OperationRegistry
from qorqut_operations.application.plans import PlanBuilder, apply_records, ref, require_revision
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal, canonical_hash, plain
from qorqut_operations.items import ItemContribution, ItemResolver, authorize_item_read, item_revision
from qorqut_operations.relationships import RelationshipOperations, RelationshipPorts, ReviewEdit
from qorqut_operations.storage import AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution, StorageContribution, TableSpec

HUMAN, BUSINESS = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
TABLE = ("CREATE TABLE {t} (business_id TEXT NOT NULL REFERENCES businesses(id), id TEXT NOT NULL, title TEXT NOT NULL, "
         "body TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, "
         "updated_at TEXT NOT NULL, PRIMARY KEY (business_id, id))")

class History:
    def capture_receipt(self, uow, receipt, plan): pass
class Hooks:
    tracked_tables = frozenset()
    def apply_effect(self, *args): return None
    def force_revision(self, plan, change): return False
    def record_applied(self, uow, plan, table, before, after, *, actor_id=None): pass
class Policy:
    denied = set()
    def resolve(self, ctx, operation, targets, uow):
        return PolicyDecision("deny" if operation.name in self.denied else "allow", "probe")

class Lookup:
    # One scoped lookup per kind over a synthetic table; reading is authorized through the descriptor's own policy.
    def __init__(self, application, kind, table, read):
        self.application, self.kind, self.table, self.read = application, kind, table, read
    def resource(self, ctx, item, uow):
        row = uow.repository(self.table, business_id=ctx.business_id).get(item["id"]) if item["type"] == self.kind else None
        if row is None:
            raise DomainError("not_found", "Context item does not exist in this business")
        return self.table, row
    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        authorize_item_read(self.application, ctx, self.read, uow)
        if builder:
            builder.expect(ref(table, item["id"], ctx.business_id), row)
        return {"label": row["title"], **item, "revision": item_revision(row), "resource_type": table,
                "resource_id": item["id"], "subtype": self.kind, "material_type": None, "tags": [], "archived": False,
                "raw_source": False, "summary": row["body"][:40], "created_at": row["created_at"], "updated_at": row["updated_at"]}
    def relationship_item(self, ctx, item, uow):
        self.resource(ctx, item, uow)
        return item
    def fingerprint(self, ctx, item, uow, builder=None):
        self.resolve(ctx, item, uow, builder)
        _, row = self.resource(ctx, item, uow)
        return canonical_hash({"title": row["title"], "body": row["body"]})

class Candidates:
    def candidates(self, ctx, uow, kind=None):
        return [{"type": k, "id": row["id"]} for k, table in (("note", "notes"), ("task", "tasks")) if not kind or kind == k
                for row in uow.repository(table, business_id=ctx.business_id).list()]

class Unused:
    # Required ports this slice never reaches: they refuse rather than assume a default.
    filters, material_types, tags = {}, ("note",), {"type": "array", "items": {"type": "string"}}
    @staticmethod
    def matches(value, args): return True
    @staticmethod
    def prepare(ctx, args, options, uow): raise DomainError("validation_error", "not used in this slice")
    @staticmethod
    def apply(ctx, plan, uow): raise DomainError("validation_error", "not used in this slice")
    @staticmethod
    def read(ctx, args, uow): return {"counts": {}}

def note_update(ctx, args, options, uow):
    row = uow.repository("notes", business_id=ctx.business_id).get(args["note_id"], required=True)
    require_revision(row, options.expected_revision)
    builder = PlanBuilder(ctx, "notes_update", args, "Update note")
    builder.record("notes", row["id"], row, {**plain(args["changes"]), "updated_at": builder.at})
    return builder.build()

with tempfile.TemporaryDirectory(prefix="qops-wheel-neutral-") as directory:
    assembly = (AssemblyBuilder(target_name="wheel-neutral", target_version=1)
        .add(StorageContribution("probe", {"notes": TableSpec(("business_id", "id"), "business_id"),
                                           "tasks": TableSpec(("business_id", "id"), "business_id")},
                                 schema=(TABLE.format(t="notes"), TABLE.format(t="tasks"))))
        .add(FileEffectContribution("probe", receipt_history=History()))
        .add(RecordEffectContribution("probe", Hooks())).freeze())
    database = Database(Path(directory) / "neutral.sqlite3", assembly=assembly).initialize_target()
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "p", "name": "p", "workspace": directory + "/w",
                                             "hq_path": directory + "/w/hq"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": HUMAN, "is_owner": True})
        for table, identity in (("notes", "n1"), ("tasks", "k1")):
            uow.repository(table, business_id=BUSINESS).insert({"id": identity, "title": identity.upper(), "body": "body"})

    # The Application owns the one open registry; the resolver and operations are built over it, then it is frozen.
    registry, policy = OperationRegistry(), Policy()
    app = Application(database, registry, policy=policy)
    items = ItemResolver(app, (ItemContribution("notes", ("note",), Lookup(app, "note", "notes", "notes_get")),
                               ItemContribution("work", ("task",), Lookup(app, "task", "tasks", "tasks_get"))),
                         candidates=Candidates())
    unused = Unused()
    ports = RelationshipPorts(roles={"related_to": ("Related to", "Related from")}, workflow_roles={}, materials=unused,
        archive=unused, navigation=unused, review_edits={"notes_update": ReviewEdit("note", "note_id")})
    relationships = RelationshipOperations(items, ports)
    get = lambda name, key: OperationDescriptor(name, {"type": "object", "properties": {key: {"type": "string"}},
        "required": [key], "additionalProperties": False}, effect_kind="read", supports_signoff=False,
        read=lambda ctx, args, options, uow: {})
    update = OperationDescriptor("notes_update", {"type": "object", "required": ["note_id", "changes"],
        "properties": {"note_id": {"type": "string"}, "changes": {"type": "object"}}, "additionalProperties": False},
        prepare=note_update, apply=lambda ctx, plan, uow: {"rows": len(apply_records(ctx, plan, uow))})
    domain = CatalogContribution("probe", (CatalogEntry("notes_get", False, "read"), CatalogEntry("tasks_get", False, "read"),
                                           CatalogEntry("notes_update", True, "records")),
                                 (get("notes_get", "note_id"), get("tasks_get", "task_id"), update))
    for contribution in (domain, relationships.contribution()):
        for entry in contribution.entries:
            registry.declare(entry)
    for contribution in (domain, relationships.contribution()):
        for descriptor in contribution.descriptors:
            registry.register(descriptor)
    registry.freeze()

    owner = InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}),
                              source="dashboard")
    note, task = {"type": "note", "id": "n1"}, {"type": "task", "id": "k1"}
    link = {"source": note, "target": task, "role": "related_to", "context": "evidence"}
    assert items.kinds == ("note", "task")
    # A link needs a readable endpoint; a denied endpoint is refused and nothing is written.
    policy.denied = {"tasks_get"}
    refused = app.invoke(owner, "relationships_create", link, {"idempotency_key": "denied"})
    assert refused.error["code"] == "forbidden", refused
    policy.denied = set()
    created = app.invoke(owner, "relationships_create", link, {"idempotency_key": "link"})
    assert created.status == "ok", created
    edge = plain(created.data)["relationship"]
    assert edge["source"] == note and edge["target"] == task and edge["revision"] == 1 and edge["predefined"] is True
    listed = plain(app.invoke(owner, "relationships_list", {"item": note, "direction": "outgoing"}, {}).data)
    assert [row["id"] for row in listed["items"]] == [edge["id"]]

    # Reading acknowledges nothing; the explicit review plus a same-item edit commit as one transaction.
    waiting = plain(app.invoke(owner, "context_reviews_list", {"item": note}, {}).data)["items"][0]
    assert waiting["pending"] and waiting["available"]
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM context_reviews").fetchone()[0] == 0
    decision = {"relationship_id": edge["id"], "relationship_revision": 1, "review_revision": 0,
                "source_token": waiting["source_token"], "result": "incorporated", "explanation": "Folded in"}
    review = {"item": note, "items": [decision], "edit": {"operation": "notes_update",
                                                         "arguments": {"note_id": "n1", "changes": {"body": "Edited"}}}}
    reviewed = app.invoke(owner, "context_review", review, {"idempotency_key": "review", "expected_revision": 1})
    assert reviewed.status == "ok", reviewed
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("notes").get("n1")["body"] == "Edited" and uow.repository("notes").get("n1")["revision"] == 2
        (stored,) = uow.repository("context_reviews").list()
        assert stored["created_at"] and stored["result"] == "incorporated" and stored["actor_id"] == HUMAN
        assert len(uow.repository("context_review_states").list()) == 1
    # A second decision from the old observation is stale; decisions are immutable.
    stale = app.invoke(owner, "context_review", {"item": note, "items": [decision]},
                       {"idempotency_key": "again", "expected_revision": 2})
    assert stale.error["code"] == "revision_conflict", stale
    with database.transaction() as uow:
        try:
            uow.connection.execute("UPDATE context_reviews SET explanation='x'")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("review decisions must be immutable")
    database.close()
assert not any(name == "bazarlyk" or name.startswith("bazarlyk.") for name in sys.modules)
print(json.dumps({"items": ["note", "task"], "link": "authorized-vs-denied", "review": "atomic-with-edit"}))
'''


DIRECTORY = r'''
import importlib.util, json, sys, tempfile
from pathlib import Path

assert importlib.util.find_spec("bazarlyk") is None, "bazarlyk must not be importable"
import qorqut_operations.storage.directories as directories
from qorqut_operations.application import Application, OperationDescriptor, OperationRegistry
from qorqut_operations.contracts import (ChangePreview, ChangeSet, ExpectedTarget, FieldChange, InvocationContext, PolicyDecision,
                                         PreparedChange, Principal, PreviewResource, ResourceRef, StructuredPreview)
from qorqut_operations.storage import (AssemblyBuilder, Database, DIRECTORY_ACTION, FileApplier, FileEffectContribution, LockPaths,
                                       RecordEffectContribution, RegisteredDirectoryTarget, directory_values, observe_directory)
from qorqut_operations.storage.executor import PreparedFileExecutor

assert "site-packages" in Path(directories.__file__).resolve().parts, directories.__file__
HUMAN, TYPE, LAYOUT = "00000000-0000-4000-8000-000000000001", "project_directories", ("code", "hq")

class History:
    def capture_receipt(self, uow, receipt, plan): pass
class Hooks:
    tracked_tables = frozenset()
    def apply_effect(self, *args): return None
    def force_revision(self, plan, change): return False
    def record_applied(self, uow, plan, table, before, after, *, actor_id=None): pass
class Allow:
    def resolve(self, ctx, operation, targets, uow): return PolicyDecision("allow", "probe")

with tempfile.TemporaryDirectory(prefix="qops-wheel-directory-") as directory:
    root = Path(directory)
    operations, staging = root / "operations", root / "runtime" / "staging"
    operations.mkdir()
    assembly = (AssemblyBuilder(target_name="wheel-directory", target_version=1)
        .add(FileEffectContribution("probe", receipt_history=History()))
        .add(RecordEffectContribution("probe", Hooks())).freeze())
    database = Database(root / "directory.sqlite3", assembly=assembly).initialize_target()
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human"})

    def target(resource):
        return RegisteredDirectoryTarget(operations, "projects/" + resource.resource_id, staging)
    def observed(ctx, resource, uow):
        digest = observe_directory(target(resource))
        return {"content_hash": digest} if digest else None
    def prepare(ctx, args, options, uow):
        resource, label = ResourceRef(TYPE, args["slug"], None, "global"), PreviewResource(TYPE, args["slug"], args["slug"])
        return ChangeSet("projects_make", 1, ctx.principal_id, "global", args, {"kind": "global"},
            (ExpectedTarget(resource, exists=False),),
            (PreparedChange("file", resource, DIRECTORY_ACTION, directory_values(LAYOUT)),),
            ChangePreview("Make project folder", "", {"kind": "global"}, (label,),
                          (StructuredPreview(label, (FieldChange("directories", None, list(LAYOUT)),)),)))

    registry = OperationRegistry()
    registry.register(OperationDescriptor("projects_make", {"type": "object", "required": ["slug"],
        "properties": {"slug": {"type": "string"}}}, effect_kind="files", prepare=prepare,
        apply=lambda ctx, plan, uow: {"slug": plan.arguments["slug"]}, scope_kinds=frozenset({"global"}),
        global_capability="coordinate_businesses"))
    caller = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({"coordinate_businesses"}))
    applier = FileApplier(database, target, locks=LockPaths.under(root / "locks"))
    app = Application(database, registry, policy=Allow(),
                      file_executor=PreparedFileExecutor(database, applier, context_loader=lambda plan, uow: caller))
    app.register_target_resolver(TYPE, observed)

    def tree(path):
        return sorted(item.relative_to(path).as_posix() for item in path.rglob("*")) if path.is_dir() else None
    made = app.invoke(caller, "projects_make", {"slug": "alpha"}, {"idempotency_key": "one"})
    assert made.status == "ok", made
    alpha = operations / "projects" / "alpha"
    assert tree(alpha) == ["code", "hq"] and not any(staging.iterdir()), tree(alpha)
    identity = alpha.stat().st_ino
    # The same key replays the saved result; another key for the now occupied name conflicts and adopts nothing.
    assert app.invoke(caller, "projects_make", {"slug": "alpha"}, {"idempotency_key": "one"}).data == made.data
    occupied = app.invoke(caller, "projects_make", {"slug": "alpha"}, {"idempotency_key": "two"})
    assert occupied.status == "error" and occupied.error["code"] == "revision_conflict", occupied
    assert alpha.stat().st_ino == identity
    # An identical empty tree made by someone else carries the same layout hash and is still never adopted.
    (operations / "projects" / "beta" / "code").mkdir(parents=True)
    (operations / "projects" / "beta" / "hq").mkdir()
    theirs = app.invoke(caller, "projects_make", {"slug": "beta"}, {"idempotency_key": "three"})
    assert theirs.status == "error" and theirs.error["code"] == "revision_conflict", theirs
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM resource_claims").fetchone()[0] == 0
        assert [row[0] for row in uow.connection.execute(
            "SELECT state FROM apply_receipts WHERE operation_name='projects_make' ORDER BY created_at")].count("applied") == 1
    database.close()
assert not any(name == "bazarlyk" or name.startswith("bazarlyk.") for name in sys.modules)
print(json.dumps({"directory": "published-once", "replay": "same-key", "foreign-tree": "never-adopted"}))
'''


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise SystemExit(f"{' '.join(map(str, command))}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main(argv: list[str]) -> None:
    workdir = Path(argv[1]) if len(argv) > 1 else Path(tempfile.mkdtemp(prefix="qorqut-operations-wheel-"))
    workdir.mkdir(parents=True, exist_ok=True)
    wheelhouse, environment, elsewhere = workdir / "wheelhouse", workdir / "venv", workdir / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(ROOT)])
    wheels = sorted(wheelhouse.glob("qorqut_operations-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one wheel, found {wheels}")
    run([sys.executable, "-m", "venv", str(environment)])
    python = environment / "bin" / "python"
    first_party = argv[2] if len(argv) > 2 else os.environ.get("QORQUT_FIRST_PARTY_WHEELS")
    find_links = ["--find-links", str(Path(first_party).resolve())] if first_party else []
    run([str(python), "-m", "pip", "install", "--quiet", *find_links, str(wheels[0])])
    # -I: ignore PYTHON* variables and the user site; the cwd is an empty directory, not the checkout.
    for script in (PROBE, SLICE, NEUTRAL, DIRECTORY):
        output = run([str(python), "-I", "-c", script], cwd=elsewhere)
        print(output.strip())


if __name__ == "__main__":
    main(sys.argv)
