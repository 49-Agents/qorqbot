"""Opt-in installed-composition check: build both libraries from exact committed sources, install them together into a
disposable virtual environment and run one synthetic snapshot from an unrelated directory with a clean ``sys.path``.

    QORQUT_HISTORY_INSTALLED_CHECK=1 QORQUT_HISTORY_OPERATIONS_SOURCE=/path/to/qorqut-operations pytest \\
        app/tests/test_operations_history_installed.py

Nothing global or live is touched. The Operations repository and this repository are exported with ``git archive HEAD`` into
a temporary directory (so only committed source is built), the wheels are installed with ``--no-deps`` plus the engine's one
runtime dependency, and the probe runs with ``python -I``. The snapshot engine is ``qorqut_operations.history_exports``; the
host wheel here carries the whole ``bazarlyk`` package, but the probe imports only the host's explicit path contract and its
history composition. Building needs ``setuptools`` and ``wheel`` in the interpreter running pytest, and a package index for
``jsonschema``.
"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

CODE = Path(__file__).resolve().parents[2]

PROBE = textwrap.dedent(r'''
    import importlib.util, json, os, subprocess, sys, tempfile
    from pathlib import Path

    assert importlib.util.find_spec("qorqut_code") is None
    import qorqut_operations
    import bazarlyk.operations_history.composition as history_package
    from bazarlyk.instance_paths import InstancePaths
    from bazarlyk.operations_history.composition import history_over
    from qorqut_operations.history_exports import (BusinessAgentFiles, BusinessHeadquarters, ExportCatalog, GlobalFiles,
                                                   HistoryContribution, HistoryObserver, Integer, PRIVATE_EXCLUDED,
                                                   TECHNICAL_EXCLUDED, TableClassification, TableExport, Text)
    from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution,
                                           StorageContribution, TableSpec)

    for module in (qorqut_operations, history_package):
        assert "site-packages" in Path(module.__file__).resolve().parts, module.__file__
    cwd = Path.cwd().resolve()
    assert not any(Path(entry or ".").resolve() == cwd for entry in sys.path if entry), sys.path

    class Receipts:
        def capture_receipt(self, uow, receipt, plan): pass
    class Hooks:
        tracked_tables = frozenset()
        def apply_effect(self, *arguments): return None
        def force_revision(self, plan, change): return False
        def record_applied(self, uow, plan, table, before, after, *, actor_id=None): pass

    SECRET = "TOPSECRET-installed"
    schema = ("CREATE TABLE work_items (id TEXT PRIMARY KEY, title TEXT NOT NULL, secret_note TEXT, revision INTEGER NOT NULL DEFAULT 1)",)
    assembly = (AssemblyBuilder(target_name="history-installed-probe", target_version=1)
        .add(StorageContribution("probe", {"work_items": TableSpec(("id",))}, schema=schema))
        .add(FileEffectContribution("probe", receipt_history=Receipts()))
        .add(RecordEffectContribution("probe", Hooks())).freeze())

    with tempfile.TemporaryDirectory(prefix="history-installed-") as directory:
        root = Path(directory).resolve() / "instance"
        (root / "operations").mkdir(parents=True)
        paths = InstancePaths(root, {})
        database = Database(paths.database, assembly=assembly).initialize_target()
        with database.transaction() as uow:
            uow.connection.execute("INSERT INTO work_items(id, title, secret_note) VALUES ('a','Safe title',?)", (SECRET,))
            uow.connection.execute("INSERT INTO principals(id, kind, display_name, created_at, updated_at) VALUES ('p','agent','x','t','t')")
            uow.connection.execute("INSERT INTO businesses(id, slug, workspace, hq_path, name, created_at, updated_at) "
                                   "VALUES ('b','acme','/w','/w/hq','Acme','t','t')")
            uow.connection.execute("INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, "
                                   "profile_json, created_at, updated_at) VALUES ('p','p','b','b','agent','/w/agent','{}','t','t')")
        items = TableExport("work_items", {"id": Text(), "title": Text(), "revision": Integer()}, ("id",))

        def contribution(missing=()):
            return [HistoryContribution("probe", exports=(items,)),
                    *([HistoryContribution("rest", classifications=tuple(
                        TableClassification(name, TECHNICAL_EXCLUDED, "probe") for name in missing))] if missing else [])]

        with database.read() as uow:
            missing = ExportCatalog(contribution()).export(uow).missing
        catalog = ExportCatalog(contribution(missing))

        def areas(uow):
            return [GlobalFiles(), BusinessHeadquarters("acme"), BusinessAgentFiles("acme", "p")]

        counter = iter(range(1, 100))
        history = history_over(database, paths, catalog=catalog, areas=areas,
                               clock=lambda: "2026-10-11T10:%02d:00.000000Z" % next(counter))
        assert not (root / "operations" / ".runtime" / "history.git").exists()
        history.initialize()
        for relative, content in (("files/notes/plan.md", "global"), ("projects/acme/hq/h.md", "hq"),
                                  ("projects/acme/agents/p/files/n.md", "agent"), ("projects/acme/code/app.py", "code"),
                                  ("context/c.md", "context"), ("agents/p/transcripts/t.jsonl", "transcript")):
            target = root / "operations" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        summary = history.snapshot()
        repository = str(paths.history)

        def git(*arguments):
            result = subprocess.run(["git", "--git-dir", repository, *arguments], capture_output=True,
                                    env={"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
            assert result.returncode == 0, result.stderr
            return result.stdout

        files = set(git("ls-tree", "-r", "--name-only", "refs/heads/history").decode().split("\n")) - {""}
        assert files == {"database/work_items.jsonl", "database/manifest.json", "files/notes/plan.md", "projects/acme/hq/h.md",
                         "projects/acme/agents/p/files/n.md"}, files
        assert SECRET.encode() not in git("cat-file", "--batch-all-objects", "--batch")
        assert summary["complete"] is True and summary["changed"] is True, summary
        assert history.snapshot()["changed"] is False
        observer = HistoryObserver(history.snapshot, interval=30)
        observer.start(); observer.notify_commit()
        assert observer.stop(10).stopped is True
        database.close()
    assert not any(name.startswith("bazarlyk.") and name not in ("bazarlyk.instance_paths",) and
                   not name.startswith("bazarlyk.operations_history") for name in sys.modules), sorted(sys.modules)
    assert "qorqut_context" not in sys.modules
    print(json.dumps({"commit": summary["commit"], "files": sorted(files)}))
''')

OPERATIONS_ONLY = textwrap.dedent('''
    import sys
    import qorqut_operations.history_exports as exports
    assert "site-packages" in exports.__file__
    assert exports.History and exports.HistoryObserver and exports.ResolvedArea
    assert not any(name.split(".")[0] in ("bazarlyk", "qorqut_context") for name in sys.modules)
    print("operations-only import is independent of the host")
''')


def run(command, **keywords):
    result = subprocess.run(command, capture_output=True, text=True, timeout=900, **keywords)
    assert result.returncode == 0, f"{command[:4]}\n{result.stdout}\n{result.stderr}"
    return result.stdout


def export(repository, destination, *paths):
    destination.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(repository), "archive", "HEAD", *paths], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)


@pytest.mark.skipif(os.environ.get("QORQUT_HISTORY_INSTALLED_CHECK") != "1",
                    reason="opt-in: builds two wheels and installs them into a disposable environment")
def test_both_libraries_install_together_and_take_a_synthetic_snapshot_outside_the_source_trees(tmp_path):
    operations = Path(os.environ["QORQUT_HISTORY_OPERATIONS_SOURCE"])
    export(operations, tmp_path / "operations-src")
    export(CODE, tmp_path / "code-src", "app")
    wheels = tmp_path / "wheels"
    for source in (tmp_path / "operations-src", tmp_path / "code-src" / "app"):
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(wheels), str(source)])
    environment = tmp_path / "venv"
    run([sys.executable, "-m", "venv", str(environment)])
    python = str(environment / "bin" / "python")
    names = sorted(wheels.glob("*.whl"))
    assert {name.name.split("-")[0] for name in names} == {"qorqut_operations", "bazarlyk_core"}
    run([python, "-m", "pip", "install", "--no-deps", *map(str, names)])
    run([python, "-m", "pip", "install", "jsonschema>=4.18"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    environment_variables = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "GIT_"))}
    assert "independent of the host" in run([python, "-I", "-c", OPERATIONS_ONLY], cwd=elsewhere, env=environment_variables)
    output = run([python, "-I", "-c", PROBE], cwd=elsewhere, env=environment_variables)
    assert '"commit"' in output
