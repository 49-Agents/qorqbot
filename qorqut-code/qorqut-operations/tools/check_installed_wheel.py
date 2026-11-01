"""Build the wheel, install it into a disposable virtual environment and import it with no source tree in sight.

    python tools/check_installed_wheel.py [workdir]

Nothing global or live is touched: the wheel is built from this checkout into ``workdir`` (a temporary directory by
default), a fresh venv is created there, the wheel is installed with its one runtime dependency, and a short synthetic
script is run from an unrelated working directory with a clean ``sys.path``. The script proves that

* the package imports from the installed wheel (not from the checkout),
* no module named ``bazarlyk`` is importable or has been imported,
* a trusted assembly freezes, a store initializes in a temporary path and an operation applies with its idempotent
  replay, using only the installed package,
* a second script runs a small actual Signoff slice (a restricted agent's proposal is approved by the owner and
  applied once, through typed ``SignoffPorts``) and a registered-program slice (a manual routine run executes a
  synthetic program whose frozen output is applied under its worker identity), again with no ``bazarlyk``.

The build needs ``setuptools`` and ``wheel`` available to ``pip`` (pip fetches them into the isolated build
environment) and a package index for ``jsonschema``.
"""

from __future__ import annotations

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
from qorqut_operations.policy import AgentPolicyBasis, PolicyResolver, PolicyRules
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
    def marketing_peer(self, uow, principal_id): return False
class Tasks:
    def open_assigned_count(self, uow, business_id, principal_id, now): return 0
class References:
    targets = {"thing": ReferenceTarget("things_get", "thing_id")}
    def task_namespace(self, ctx, args): return "business:" + (ctx.business_id or "")
class Agents:
    def refresh_invocation_context(self, ctx, uow):
        row = uow.connection.execute("SELECT home_business_id, revision FROM agent_profiles WHERE agent_id=?",
                                     (ctx.principal_id,)).fetchone()
        return replace(ctx, home_business_id=row[0], profile_revision=row[1], allowed_business_ids=frozenset({row[0]}))
    def policy_basis(self, ctx, uow):
        return AgentPolicyBasis({"things_create": "signoff"}, {}, {"id": "role-a", "version": "1"}, 1)
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
                         platform=ABSENT, email=ABSENT, campaigns=ABSENT, management=ABSENT, focus=ABSENT)
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
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": AGENT})
        uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": HUMAN})
        uow.connection.execute("INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, "
            "workspace, profile_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
            (AGENT, AGENT, BUSINESS, BUSINESS, "proposer", directory + "/agent",
             json.dumps({"role": {"id": "role-a"}, "scope": {"kind": "business"}})))
        workers = authority.register_workers(uow, [BUSINESS])

    # Signoff slice: a restricted agent stages, the owner approves the reviewed binding, the engine applies once.
    agent = InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS, home_business_id=BUSINESS,
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
    run([str(python), "-m", "pip", "install", "--quiet", str(wheels[0])])
    # -I: ignore PYTHON* variables and the user site; the cwd is an empty directory, not the checkout.
    for script in (PROBE, SLICE):
        output = run([str(python), "-I", "-c", script], cwd=elsewhere)
        print(output.strip())


if __name__ == "__main__":
    main(sys.argv)
