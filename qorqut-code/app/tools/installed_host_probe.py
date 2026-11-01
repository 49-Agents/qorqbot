"""The installed host gate, run by ``check_installed_host.py`` with ``python -I`` from an unrelated empty directory.

It imports only installed packages (``bazarlyk`` and ``qorqut_operations`` from the disposable environment), reads the
checkout only as data (component and role-source locations) and builds a synthetic instance below its own work
directory. It exits non-zero on the first failed assertion and prints one JSON receipt on success. The last sections are a
package-composition check of the native archive and operational history libraries, not their activation or a final
export policy."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
checkout = Path(config["checkout"]).resolve()
evidence = {"engine_commit": config["engine_commit"], "host_commit": config["host_commit"], "dirty": config["dirty"],
            "python": sys.version.split()[0]}


def check(condition, message):
    if not condition:
        raise SystemExit("installed host gate failed: " + message)


# -- 1. The imports come from the installed wheels, with no source tree and no removed module ------------------------
for entry in sys.path:
    resolved = Path(entry or ".").resolve()
    check(resolved != checkout and checkout not in resolved.parents, f"the source tree is on sys.path: {entry!r}")
import bazarlyk  # noqa: E402
import qorqut_operations  # noqa: E402

site = Path(qorqut_operations.__file__).resolve().parents[1]
check(Path(bazarlyk.__file__).resolve().parents[1] == site, "host and Operations are not installed side by side")
check(checkout not in site.parents, "the installed packages live inside the checkout")
evidence["installed_in"] = "site-packages" if site.name == "site-packages" else str(site.name)
def importable(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:  # the removed module's package is gone as well
        return False


for module in config["removed_modules"]:
    check(not importable(module), f"removed host module is importable: {module}")
evidence["removed_modules_checked"] = len(config["removed_modules"])
check(importable("qorqut_operations.contracts"), "the engine contracts are not installed")
check(not importable("bazarlyk.contracts"), "the old core contracts module is importable")


def digests(package):
    root = Path(package.__file__).resolve().parent
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts}


for package in (qorqut_operations, bazarlyk):
    installed, expected = digests(package), config["expected"][package.__name__]
    check(installed == expected, f"{package.__name__}: installed files differ from the recorded commit "
          f"(missing {sorted(set(expected) - set(installed))[:3]}, extra {sorted(set(installed) - set(expected))[:3]}, "
          f"changed {sorted(k for k in set(installed) & set(expected) if installed[k] != expected[k])[:3]})")
    evidence[f"{package.__name__}_files"] = len(installed)

# -- 2. A synthetic instance built only from the installed packages -------------------------------------------------
work = Path(config["work"])
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)


def ensure_contextbase():
    """The optional private ContextBase package is not part of the disposable environment; supply only the request model
    the retained operations import (the same stand-in the focused acceptance cases use)."""
    if "contextbase" in sys.modules or importlib.util.find_spec("contextbase") is not None:
        return
    from pydantic import BaseModel
    package, schema = types.ModuleType("contextbase"), types.ModuleType("contextbase.schema")

    class DirectEditRequest(BaseModel):
        instruction: str = ""

    schema.DirectEditRequest, package.schema = DirectEditRequest, schema
    sys.modules["contextbase"], sys.modules["contextbase.schema"] = package, schema


ensure_contextbase()
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain, utc_now  # noqa: E402
from bazarlyk.background.integration import attach_background  # noqa: E402
from bazarlyk.bootstrap.seed import apply_initial_plan, register_context_sources  # noqa: E402
from bazarlyk.composition.assembly import host_assembly, initialize_empty_target  # noqa: E402
from bazarlyk.instance_paths import InstancePaths  # noqa: E402
from bazarlyk.service.changes import ChangeFeed  # noqa: E402
from bazarlyk.service.lifetime import ServiceLifetime  # noqa: E402
from bazarlyk.service.runtime import build_runtime  # noqa: E402

workspace = work / "workspace"
shutil.copytree(Path(config["roles"]), workspace / "qorqut-roles" / "agent-roles", ignore=shutil.ignore_patterns(".git"))
components = {**config["components"], "app": str(checkout / "app"), "operations_engine": str(checkout / "qorqut-operations"),
              "roles": str(workspace / "qorqut-roles" / "agent-roles")}
root = work / "instance"
root.mkdir()
paths = InstancePaths(root, {name: Path(value) for name, value in components.items()})
feed = ChangeFeed()
database = initialize_empty_target(paths, host_assembly(feed))
owner, business = new_id(), new_id()
with database.transaction() as uow:
    now = utc_now()
    uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
    uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": owner,
                                           "created_at": now, "updated_at": now})
    uow.repository("businesses").insert({"id": business, "slug": "first", "name": "First", "workspace": str(paths.business_root("first")),
                                         "hq_path": str(paths.business_hq("first"))})
    uow.repository("business_memberships", business_id=business).insert({"principal_id": owner, "is_owner": True})
runtime = build_runtime(workspace, paths=paths, database=database)
template = paths.component("template")
register_context_sources(database, paths, template_root=template)
register_context_sources(database, paths, template_root=template, business_id=business, hq=paths.business_hq("first"))

from bazarlyk.agents.models import GLOBAL_CAPABILITIES  # noqa: E402

owner_global = InvocationContext(Principal(owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
with database.read() as uow:
    plan = runtime.publisher.prepare(owner_global, {"source_id": "business-agent"}, InvocationOptions(idempotency_key="seed-role"), uow, "role")
pin = apply_initial_plan(database, owner_global, plan, runtime.publisher, runtime.publisher.resolve_file_target, runtime.locks)["artifact"]
owner_context = InvocationContext(Principal(owner, "human"), business_id=business, allowed_business_ids=frozenset({business}), source="dashboard")


def invoke(context, operation, arguments):
    return runtime.application.invoke(context, operation, arguments, InvocationOptions(idempotency_key=new_id()))


created = invoke(InvocationContext(Principal(owner, "human"), business_id=business, allowed_business_ids=frozenset({business}),
                                   global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
                 "agents_create", {"role": pin, "scope": {"kind": "business", "business_id": business}, "plugins": []})
check(created.status == "ok", f"agent registration failed: {created.error}")
agent = created.data["agent_id"]
agent_context = runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=business)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Mail:
    """A bounded in-memory mail engine for the registered email program."""

    admin_token = "synthetic"

    def mailboxes(self):
        return {"mailboxes": [{"ref": "mail-a", "folder_id": "folder-1", "address": "hello@business.example", "aliases": [],
                               "business_id": business}], "sync": [{"mailbox": "mail-a", "state": "synced", "synced_at": "2026-10-11T12:00:00Z"}]}

    def messages(self, mailbox, after):
        return {"items": [], "cursor": 1}


runtime.domains.email.client = Mail()
background = attach_background(runtime, runtime.programs, clock=Clock())
runtime.freeze_registry()
with database.transaction() as uow:
    runtime.authority.register_workers(uow, [business])

# -- 3. The host's engine-owned types are the installed Operations types --------------------------------------------
engine_owned = {"database": runtime.database, "application": runtime.application, "registry": runtime.registry,
                "signoffs": runtime.signoffs, "programs": runtime.programs, "authority": runtime.authority,
                "background": background, "items": runtime.domains.items, "relationships": runtime.domains.relationships,
                "reviews": runtime.domains.reviews, "write_admission": runtime.application.writer_admission}
for name, value in engine_owned.items():
    module = type(value).__module__
    check(module.startswith("qorqut_operations."), f"{name} is {module}.{type(value).__name__}, not an Operations type")
    check(Path(sys.modules[module].__file__).resolve().is_relative_to(site), f"{name} does not load from the installed package")
check(type(runtime.domains.related).__module__.startswith("bazarlyk."), "the related-resource resolver is not the host's")
check(isinstance(runtime.lifetime(), ServiceLifetime), "the lifetime owner is not the host's ServiceLifetime")
evidence["engine_owned_types"] = sorted(f"{name}={type(value).__module__}.{type(value).__name__}" for name, value in engine_owned.items())

# -- 4. A protected proposal is reviewed by the owner and applied once ---------------------------------------------
proposal = invoke(agent_context, "products_create", {"name": "Widget"})
check(proposal.status == "signoff_required", f"products_create was not protected: {proposal.status} {proposal.error}")
signoff_id = plain(invoke(owner_context, "signoffs_list", {}).data)["items"][0]["id"]
signoff = plain(invoke(owner_context, "signoffs_get", {"signoff_id": signoff_id}).data)["signoff"]
decision = runtime.signoffs.review(owner_context, signoff_id, decision="approve", proposal_revision=signoff["proposal_revision"],
                                   payload_hash=signoff["payload_hash"], expected_status_revision=signoff["status_revision"],
                                   idempotency_key=new_id())
check(decision.status == "accepted", f"the owner decision was refused: {decision.error}")
check(runtime.signoffs.run_pending() == 1 and runtime.signoffs.run_pending() == 0, "the approved proposal was not applied exactly once")
with database.read() as uow:
    check([dict(row) for row in uow.connection.execute("SELECT name FROM products")] == [{"name": "Widget"}], "the product was not created once")

# -- 5. A registered non-Context program runs under its own worker authority ------------------------------------------
queued = invoke(owner_context, "business_email_sync", {})
check(queued.status == "ok", f"the sync was not queued: {queued.error}")
outcome = background.run_one(queued.data["job_id"])
check(outcome and outcome.get("state") == "succeeded", f"the registered program did not complete: {outcome}")
with database.read() as uow:
    worker = runtime.authority.worker_id("email.sync", uow)
    job = dict(uow.connection.execute("SELECT kind,state,principal_id FROM jobs").fetchone())
    check(job == {"kind": "email.sync", "state": "succeeded", "principal_id": worker}, f"unexpected job {job}")
    check(uow.connection.execute("SELECT kind FROM principals WHERE id=?", (worker,)).fetchone()[0] == "system", "the worker is not a system principal")

# -- 6. An ordinary retained-domain read, with no Story service involved --------------------------------------------
for holder in ("tasks", "documents", "tables", "data_uploads", "assets"):
    setattr(getattr(runtime.domains, holder), "stories", None)
task = plain(invoke(owner_context, "tasks_create", {"title": "Plan launch", "scope": "business"}).data)["task"]
read = plain(invoke(owner_context, "context_items_get", {"item": {"type": "task", "id": task["id"]}}).data)["item"]
check(read["label"] == "Plan launch" and read["resource_type"] == "tasks", "the neutral item read failed")

# -- 7. The committed outcome is available to an explicit, read-only row selection -----------------------------------
SELECTION = {"signoffs": ("id", "state", "proposal_revision", "payload_hash", "applied_at"),
             "inbox_events": ("id", "source_type", "source_id", "event_type", "summary"),
             "products": ("id", "business_id", "name", "revision")}
connection = sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
try:
    selected = {table: [dict(row) for row in connection.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id")]
                for table, columns in SELECTION.items()}
    try:
        connection.execute("DELETE FROM products")
        check(False, "the export connection could write")
    except sqlite3.OperationalError:
        pass
finally:
    connection.close()
check([row["state"] for row in selected["signoffs"]] == ["applied"], "the committed Signoff row is missing")
check([row["event_type"] for row in selected["inbox_events"]] == ["response_submitted", "action_outcome"], "the inbox outcome is missing")
evidence["selected_rows"] = {table: len(rows) for table, rows in selected.items()}


# -- 8. The services start under the one lifetime owner; neither new library is imported or activated by the host -------
class Controller:
    """Bounded stand-ins for the services that would reach native sessions, terminals or providers."""

    def __init__(self):
        self.calls = []

    def start(self, *_):
        self.calls.append("start")

    def stop(self, timeout=None):
        self.calls.append("stop")

    stop_observer = stop
    start_external_writer_ticker = start


for name in ("native_chat", "activity", "incidents", "terminal_control", "content_sessions", "telegram", "email_history", "changes"):
    setattr(runtime, name, Controller())
runtime.start()
threads = (runtime.signoffs._thread, background._thread)
check(all(thread.is_alive() for thread in threads), "the Signoff and background services did not start")

LIBRARIES = ("bazarlyk.native_archive", "bazarlyk.operations_history", "qorqut_operations.history_exports")
transcripts = paths.agent_transcripts(agent, business_folder="first")
check(not [name for name in sys.modules if name.startswith(LIBRARIES)],
      "host composition, the retained slice or start-up imported an inert library: "
      f"{sorted(name for name in sys.modules if name.startswith(LIBRARIES))}")
check(not paths.history.exists() and not transcripts.exists(), "start-up created a history repository or a transcript archive")


# -- 9. The native archive and the operational history coexist, inert, with the installed host and engine --------------
def schema_objects():
    connection = sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)
    try:
        return sorted(tuple(row) for row in connection.execute("SELECT type, name, sql FROM sqlite_master"))
    finally:
        connection.close()


schema_before = schema_objects()
real_tables = {row[1] for row in schema_before if row[0] == "table" and not row[1].startswith("sqlite_")}

import bazarlyk.native_archive as archive_package  # noqa: E402
import bazarlyk.operations_history as history_package  # noqa: E402
import qorqut_operations.history_exports as export_package  # noqa: E402
from bazarlyk.native_archive import CaptureBinding, TranscriptArchive  # noqa: E402
from bazarlyk.operations_history import GlobalFiles, History  # noqa: E402
from qorqut_operations.history_exports import ExportCatalog, HistoryContribution, Integer, TableExport, Text  # noqa: E402

for module in (archive_package, history_package, export_package):
    check(Path(module.__file__).resolve().is_relative_to(site), f"{module.__name__} does not load from the installed package")

# 9a. One synthetic, bound Claude transcript is archived losslessly and read back.
conversation = "33333333-3333-4333-8333-333333333333"
agent_workspace = paths.agent_workspace(agent, business_folder="first")
agent_workspace.mkdir(parents=True, exist_ok=True)  # the archive creates only its root's final directory
provider_home = work / "provider-home"
provider_home.mkdir()
source = provider_home / "session.jsonl"
line = json.dumps({"type": "user", "sessionId": conversation, "cwd": str(agent_workspace), "uuid": "m1",
                   "message": {"role": "user", "content": "synthetic installed-archive line"}}).encode() + b"\n"
source.write_bytes(line)
binding = CaptureBinding(agent, "claude", conversation, 1, str(agent_workspace), str(source))
archive = TranscriptArchive(transcripts)
receipt = archive.capture(binding)
check(receipt.sequence == 1 and receipt.length == len(line) and receipt.complete, f"unexpected archive receipt {receipt}")
check(archive.capture(binding) == receipt and archive.latest(binding) == receipt, "an unchanged capture published a new receipt")
raw = archive.read_raw(receipt, offset=0, limit=1 << 20)
check(raw.data == line and raw.next_offset is None, "the archived bytes differ from the source")
with archive.iter_records(receipt) as records:
    check([(item.offset, item.length, item.kind) for item in records] == [(0, len(line), "json")], "unexpected archived records")
evidence["archive"] = {"receipt_sequence": receipt.sequence, "bytes": receipt.length, "records": 1}

# 9b. A deliberately partial catalog over declared safe columns: one snapshot is committed and says its coverage is incomplete.
catalog = ExportCatalog([HistoryContribution("installed-probe", exports=(
    TableExport("products", {"id": Text(), "name": Text(), "revision": Integer()}, ("id",)),))])
target = database.assembly.target
with database.read() as uow:
    exported = catalog.export(uow)
check((exported.schema.name, exported.schema.version, exported.schema.digest) == (target.name, target.version, target.digest),
      "the export's schema identity is not the host assembly's")
check(not exported.coverage_complete and set(exported.missing) == real_tables - {"products"},
      "the partial catalog did not report exactly the unclassified tables as missing")
(paths.files / "notes").mkdir(parents=True)
(paths.files / "notes" / "plan.md").write_text("synthetic global note")
history = History(database, paths=paths, catalog=catalog, areas=lambda uow: [GlobalFiles()],
                  clock=lambda: "2026-10-11T12:00:00.000000Z")
history.initialize()
summary = history.snapshot()
check(summary["changed"] and summary["files_complete"] and summary["rows"] == {"products": 1}, f"unexpected snapshot {summary}")
check(summary["complete"] is False and summary["coverage_complete"] is False, "incomplete coverage was reported as complete")
check(summary["coverage"]["missing_total"] == len(real_tables) - 1, "the snapshot's missing-table count is wrong")
check(history.snapshot()["changed"] is False and history.status()["state"] == "incomplete", "unexpected history status")


def git(*arguments):
    done = subprocess.run(["git", "--git-dir", str(paths.history), *arguments], capture_output=True,
                          env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    check(done.returncode == 0, f"git {arguments[0]} failed: {done.stderr.decode()[:200]}")
    return done.stdout


tree = set(git("ls-tree", "-r", "--name-only", "refs/heads/history").decode().split())
check(tree == {"database/manifest.json", "database/products.jsonl", "files/notes/plan.md"}, f"unexpected history tree {sorted(tree)}")
objects = git("cat-file", "--batch-all-objects", "--batch")
exported_row = json.loads(git("show", "refs/heads/history:database/products.jsonl").decode().splitlines()[0])
check(set(exported_row) == {"id", "name", "revision"} and exported_row["name"] == "Widget", f"unexpected exported row {exported_row}")
check(business.encode() not in objects and agent.encode() not in objects, "an undeclared column value reached the history")
check(schema_objects() == schema_before, "the archive or history changed the database schema")
evidence["history"] = {"commit": summary["commit"], "tree": sorted(tree), "rows": summary["rows"], "complete": summary["complete"],
                       "missing_tables": summary["coverage"]["missing_total"], "schema_unchanged": True}

# -- 10. A clean, bounded shutdown ---------------------------------------------------------------------------------------
report = runtime.stop()
check(report.clean and report.database_closed and not any(thread.is_alive() for thread in threads), "the shutdown was not clean")
evidence["shutdown"] = {"clean": report.clean, "database_closed": report.database_closed, "stopped": list(report.stopped)}
evidence["result"] = "passed"
print(json.dumps(evidence, indent=2, sort_keys=True))
