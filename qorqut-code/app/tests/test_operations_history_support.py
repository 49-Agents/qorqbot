"""Synthetic composition for the host's operational-history checks (no tests live here).

One temporary instance per test: explicit ``InstancePaths`` with no component locations, a real Operations ``Database``
composed from the engine's own schema plus a tiny synthetic domain, registrations inserted as synthetic rows, a trusted
area provider reading them, the snapshot engine bound through the host's own composition, and a recording Git runner. No
real record, provider, source repository or system history ref is read or written, and the synthetic domain is not a
production export definition. The generic engine's own checks live in the Operations package tests.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
from types import SimpleNamespace

from bazarlyk.instance_paths import InstancePaths
from bazarlyk.operations_history.composition import history_over
from qorqut_operations.history_exports import (Boolean, BusinessAgentFiles, BusinessHeadquarters, ExportCatalog,
                                               GlobalAgentFiles, GlobalFiles, HistoryContribution, Integer, Obj,
                                               PRIVATE_EXCLUDED, StructuredField, TECHNICAL_EXCLUDED, TableClassification,
                                               TableExport, Text, declared_json)
from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution,
                                       StorageContribution, TableSpec)

SECRET = "TOPSECRET-synthetic-value"
OWNER = "00000000-0000-4000-8000-000000000001"
GLOBAL_AGENT = "00000000-0000-4000-8000-0000000000a1"
ACME_AGENT = "00000000-0000-4000-8000-0000000000b1"
BETA_AGENT = "00000000-0000-4000-8000-0000000000b2"
ACME, BETA = "10000000-0000-4000-8000-000000000001", "10000000-0000-4000-8000-000000000002"
REF = "refs/heads/history"
GIT_ENV = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@localhost",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@localhost"}
ALLOWED_GIT = {"init", "config", "rev-parse", "symbolic-ref", "ls-tree", "hash-object", "update-index", "write-tree",
               "commit-tree", "update-ref", "count-objects", "repack"}

SCHEMA = (
    """CREATE TABLE work_items (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL, secret_note TEXT,
        detail_json TEXT, revision INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE work_notes (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, channel TEXT NOT NULL, body TEXT NOT NULL)""",
    """CREATE TABLE event_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL)""",
)
TABLES = {"work_items": TableSpec(("id",)), "work_notes": TableSpec(("id",), revision_column=None),
          "event_log": TableSpec(("id",), revision_column=None)}


class _ReceiptHistory:
    def capture_receipt(self, uow, receipt, plan):
        pass


class _Hooks:
    tracked_tables = frozenset()

    def apply_effect(self, *arguments):
        return None

    def force_revision(self, plan, change):
        return False

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        pass


def assembly():
    return (AssemblyBuilder(target_name="history-host-test", target_version=1)
            .add(StorageContribution("history_test", TABLES, schema=SCHEMA))
            .add(FileEffectContribution("history_test", receipt_history=_ReceiptHistory()))
            .add(RecordEffectContribution("history_test", _Hooks())).freeze())


DETAIL = Obj(nullable=True, fields={"label": Text(), "pinned": Boolean()})


def domain_contribution():
    """Safe identity/lifecycle fields of items and their ordinary discussion; nothing else of the domain."""
    items = TableExport("work_items", key=("id",), shard_hex=2,
                        columns={"id": Text(), "scope": Text(), "title": Text(), "state": Text(), "revision": Integer()},
                        structured={"detail_json": StructuredField(DETAIL, declared_json(DETAIL))})
    notes = TableExport(
        "work_notes", key=("id",),
        columns={"id": Integer(), "item_id": Text(), "channel": Text(), "body": Text()},
        selector=lambda uow, request: request.select(uow, '"work_notes"."channel" = ?', ("item",)))
    return HistoryContribution("work", exports=(items, notes), classifications=(
        TableClassification("event_log", PRIVATE_EXCLUDED, "opaque event payloads can embed private channel data"),))


def rest_classified(database):
    """Contributions that also classify every other actual table (engine tables) as technical, for complete coverage."""
    with database.read() as uow:
        missing = ExportCatalog([domain_contribution()]).export(uow).missing
    return [domain_contribution(), HistoryContribution("rest", classifications=tuple(
        TableClassification(name, TECHNICAL_EXCLUDED, "synthetic: classified for the coverage-complete checks")
        for name in missing))]


def registered_areas(uow):
    """The trusted provider: current registrations read through the export's own unit."""
    areas = [GlobalFiles()]
    slugs = {row[0]: row[1] for row in uow.connection.execute("SELECT id, slug FROM businesses")}
    for agent_id, home in uow.connection.execute("SELECT agent_id, home_business_id FROM agent_profiles"):
        areas.append(GlobalAgentFiles(agent_id) if home is None else BusinessAgentFiles(slugs[home], agent_id))
    areas.extend(BusinessHeadquarters(slug) for slug in slugs.values())
    return areas


class Runner:
    """Records every Git call and lets a test intercept one before it runs."""

    def __init__(self):
        self.calls, self.intercepts = [], []

    @staticmethod
    def subcommand(command):
        index = 1
        while command[index] == "-c":
            index += 2
        return command[index]

    def __call__(self, command, **kwargs):
        name = self.subcommand(command)
        self.calls.append((name, command, kwargs))
        for intercept in list(self.intercepts):
            replacement = intercept(name, command, kwargs)
            if replacement is not None:
                return replacement
        return subprocess.run(command, **kwargs)


def failure(code=128, stderr=b"fatal: synthetic failure"):
    return subprocess.CompletedProcess([], code, b"", stderr)


def build(tmp_path, *, catalog=None, areas=registered_areas, seed=True):
    root = tmp_path / "instance"
    operations = root / "operations"
    operations.mkdir(parents=True)
    paths = InstancePaths(root, {})
    database = Database(paths.database, assembly=assembly()).initialize_target()
    clock = {"minute": 0}

    def tick():
        clock["minute"] += 1
        return "2026-10-11T09:%02d:00.000000Z" % clock["minute"]

    if seed:
        seed_registrations(database)
        seed_domain(database)
    runner = Runner()
    catalog = catalog or ExportCatalog([domain_contribution()])
    history = history_over(database, paths, catalog=catalog, areas=areas, clock=tick, git_runner=runner)
    environment = SimpleNamespace(root=root, operations=operations, runtime=operations / ".runtime", paths=paths,
                                  tmp=tmp_path, repository=operations / ".runtime" / "history.git", database=database,
                                  history=history, runner=runner, clock=tick, catalog=catalog, areas=areas)

    def another(**keywords):
        """A second engine over the same instance through the host composition; unnamed parts are this instance's own."""
        keywords = {"catalog": environment.catalog, "areas": environment.areas, "clock": tick, "git_runner": runner,
                    **keywords}
        return history_over(database, paths, **keywords)

    environment.another = another
    return environment


def seed_registrations(database):
    with database.transaction() as uow:
        connection = uow.connection
        connection.execute("INSERT INTO principals(id, kind, display_name, created_at, updated_at) "
                           "VALUES (?, 'human', 'Owner', 't', 't')", (OWNER,))
        for agent in (GLOBAL_AGENT, ACME_AGENT, BETA_AGENT):
            connection.execute("INSERT INTO principals(id, kind, display_name, created_at, updated_at) "
                               "VALUES (?, 'agent', 'Agent', 't', 't')", (agent,))
        for identity, slug in ((ACME, "acme"), (BETA, "beta")):
            connection.execute(
                "INSERT INTO businesses(id, slug, workspace, hq_path, name, created_at, updated_at) VALUES (?,?,?,?,?,'t','t')",
                (identity, slug, f"/synthetic/{slug}", f"/synthetic/{slug}/hq", slug.title()))
        for agent, home, namespace in ((GLOBAL_AGENT, None, "ecosystem"), (ACME_AGENT, ACME, ACME), (BETA_AGENT, BETA, BETA)):
            connection.execute(
                "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, "
                "profile_json, created_at, updated_at) VALUES (?,?,?,?,?,?,'{}','t','t')",
                (agent, agent, home, namespace, "agent", f"/synthetic/agents/{agent}"))


def seed_domain(database):
    with database.transaction() as uow:
        connection = uow.connection
        connection.executemany("INSERT INTO work_items(id, scope, title, state, secret_note, detail_json) VALUES (?,?,?,?,?,?)", [
            ("item-1", "acme", "Review the offer", "open", SECRET,
             json.dumps({"label": "pricing", "pinned": True, "token": SECRET, "nested": {"key": SECRET}})),
            ("item-2", "global", "Plan the quarter", "working", SECRET, None)])
        connection.executemany("INSERT INTO work_notes(id, item_id, channel, body) VALUES (?,?,?,?)", [
            (1, "item-1", "item", "ordinary item discussion"), (2, "item-1", "direct", f"private message {SECRET}"),
            (3, "item-2", "item", "second discussion")])
        connection.execute("INSERT INTO event_log(kind, payload_json) VALUES ('note', ?)", (json.dumps({"body": SECRET}),))


def git(env, *arguments, data=None):
    result = subprocess.run(["git", "--git-dir", str(env.repository), *arguments], capture_output=True, env=GIT_ENV,
                            input=data)
    assert result.returncode == 0, result.stderr
    return result.stdout


def tree(env, commit=REF):
    found = {}
    for record in git(env, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if record:
            meta, _, path = record.partition(b"\t")
            mode, _, oid = meta.decode().split()
            found[os.fsdecode(path)] = (mode, oid)
    return found


def blob(env, path, commit=REF):
    return git(env, "cat-file", "blob", f"{commit}:{path}")


def commits(env):
    return git(env, "rev-list", REF).split()


def everything(env):
    return git(env, "cat-file", "--batch-all-objects", "--batch")


def write(env, relative, content=b"", mode=None):
    path = env.operations / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if mode:
        path.chmod(mode)
    return path


@contextmanager
def tracked_reads(env, state):
    """Mark when an Operations read unit is open, and count how many are used."""
    original = env.database.read
    state.update(open=False, count=0)

    @contextmanager
    def tracked(*arguments, **keywords):
        state["open"], state["count"] = True, state["count"] + 1
        try:
            with original(*arguments, **keywords) as uow:
                yield uow
        finally:
            state["open"] = False

    env.database.read = tracked
    try:
        yield
    finally:
        env.database.read = original
