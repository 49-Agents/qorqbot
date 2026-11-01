"""Build the wheel, install it into a disposable virtual environment and import it with no source tree in sight.

    python tools/check_installed_wheel.py [workdir]

Nothing global or live is touched: the wheel is built from this checkout into ``workdir`` (a temporary directory by
default), a fresh venv is created there, the wheel is installed with its one runtime dependency, and a short synthetic
script is run from an unrelated working directory with a clean ``sys.path``. The script proves that

* the package imports from the installed wheel (not from the checkout),
* no module named ``bazarlyk`` is importable or has been imported,
* a trusted assembly freezes, a store initializes in a temporary path and an operation applies with its idempotent
  replay, using only the installed package.

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
    output = run([str(python), "-I", "-c", PROBE], cwd=elsewhere)
    print(output.strip())


if __name__ == "__main__":
    main(sys.argv)
