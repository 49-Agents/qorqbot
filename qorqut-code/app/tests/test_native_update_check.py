"""A new build's provenance-only catalog refresh is admitted without writes; any real managed change is still refused.

The app update and the record of extension tables: the updater (``tools/distribution/native_update.py``, run from the
installed runtime) judges a store without the record exactly as before; a store with the record is opened by the new
runtime's check with the instance's selection and the record, and a refusal there restores the previous runtime."""
import base64
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

import yaml

from qorqut.bootstrap import native_update_check as module
from qorqut.composition.assembly import host_assembly
from qorqut.extensions import NO_EXTENSIONS
from qorqut.instance_paths import InstancePaths
from qorqut.service.changes import ChangeFeed
from qorqut_operations.storage.database import Database

from test_extension_tables import ATTACHED, attaching, contributions, edit_row, store_with_record

UPDATER = Path(__file__).resolve().parents[1].parent / "tools" / "distribution" / "native_update.py"

CATALOG = {
    "id": "imported-terminal-control", "schema_version": 1,
    "entries": [{"id": "asset-1", "kind": "template", "path": "native/app/instructions/a.md"}],
    "source": {"import_adapter": "v1.3-offline", "sources": [{
        "id": "host", "commit": "766d0449ee3c17684fb666c7237281a2a95f2df5", "tree": "6d08bec6",
        "root": "/Users/x/Qorqbot/qorqbot/runtime/src/qorqut-code", "snapshot_digest": "c8f0",
        "snapshot_sha256": "b9aa", "source_manifest_sha256": "afab",
        "files": {"app/qorqut/agentic/assets/backlog.md": "c795"}}]},
}


def refreshed(**source_changes):
    document = yaml.safe_load(yaml.safe_dump(CATALOG))
    document["source"]["sources"][0].update(source_changes)
    return document


def edit(path, document):
    return {"op": "edit", "path": path, "expected_sha256": "0" * 64, "mode": 384,
            "content_base64": base64.b64encode(yaml.safe_dump(document).encode()).decode()}


class ProvenanceOnlyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.path = "plugins/imported-terminal-control/agentic.yaml"
        (self.root / self.path).parent.mkdir(parents=True)
        (self.root / self.path).write_text(yaml.safe_dump(CATALOG))

    def test_new_commit_and_hashes_are_provenance_only(self):
        change = edit(self.path, refreshed(commit="6895969d", tree="aa11", snapshot_sha256="ec27",
                                           source_manifest_sha256="32dc", root="/elsewhere/runtime/src/qorqut-code"))
        self.assertTrue(module.provenance_only(change, self.root))

    def test_changed_file_digest_is_content(self):
        change = edit(self.path, refreshed(commit="6895969d", files={"app/qorqut/agentic/assets/backlog.md": "ffff"}))
        self.assertFalse(module.provenance_only(change, self.root))
        self.assertFalse(module.provenance_only(edit(self.path, refreshed(snapshot_digest="dddd")), self.root))

    def test_changed_entries_are_content(self):
        document = refreshed(commit="6895969d")
        document["entries"][0]["path"] = "native/app/instructions/b.md"
        self.assertFalse(module.provenance_only(edit(self.path, document), self.root))

    def test_identical_create_delete_other_files_and_escapes_are_refused(self):
        self.assertFalse(module.provenance_only(edit(self.path, CATALOG), self.root))
        self.assertFalse(module.provenance_only({**edit(self.path, refreshed(commit="1")), "op": "create"}, self.root))
        self.assertFalse(module.provenance_only({"op": "delete", "path": self.path}, self.root))
        self.assertFalse(module.provenance_only(edit("plugins/x/SKILL.md", refreshed(commit="1")), self.root))
        self.assertFalse(module.provenance_only(edit("../agentic.yaml", refreshed(commit="1")), self.root))
        self.assertFalse(module.provenance_only(edit("missing/agentic.yaml", refreshed(commit="1")), self.root))
        self.assertFalse(module.provenance_only({**edit(self.path, refreshed(commit="1")), "content_base64": "!!"}, self.root))


class CheckTests(unittest.TestCase):
    def run_check(self, plan, root):
        applied = []
        paths = mock.Mock(agentic_root=root)
        with mock.patch.object(module, "load_prepared", return_value=(None, None, {})), \
                mock.patch.object(module, "InstancePaths", return_value=paths), \
                mock.patch.object(module, "prepare_upgrade", return_value=plan), \
                mock.patch("qorqut.agentic.managed_update.apply", side_effect=AssertionError("Admission must not write")):
            result = module.check(Path("/prepared"), Path("/instance"))
        return result, applied

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.path = "plugins/imported-terminal-control/agentic.yaml"
        (self.root / self.path).parent.mkdir(parents=True)
        (self.root / self.path).write_text(yaml.safe_dump(CATALOG))
        self.plan = {"kind": "agentic_managed_update_v2", "complete": True, "state": {"migration_required": False},
                     "provisioning": {"items": [{"state": "present"}]}, "changes": []}

    def test_no_changes_is_admitted_without_applying(self):
        result, applied = self.run_check(self.plan, self.root)
        self.assertEqual(result, {"applied": False})
        self.assertEqual(applied, [])

    def test_provenance_refresh_is_read_only_on_linux_and_mac(self):
        plan = {**self.plan, "changes": [edit(self.path, refreshed(commit="6895969d"))]}
        before = (self.root / self.path).read_bytes()
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), mock.patch("sys.platform", platform):
                result, applied = self.run_check(plan, self.root)
                self.assertEqual(result, {"applied": False, "retained_provenance": [self.path]})
                self.assertEqual(applied, [])
                self.assertEqual((self.root / self.path).read_bytes(), before)

    def test_any_content_change_refuses_and_applies_nothing(self):
        plan = {**self.plan, "changes": [edit(self.path, refreshed(commit="6895969d")),
                                         edit(self.path, refreshed(files={"a": "b"}))]}
        with self.assertRaisesRegex(RuntimeError, "qualified managed update"):
            self.run_check(plan, self.root)

    def test_incomplete_migration_or_missing_provisioning_still_refuses(self):
        for plan in ({**self.plan, "complete": False}, {**self.plan, "state": {"migration_required": True}},
                     {**self.plan, "provisioning": {"items": [{"state": "missing"}]}},
                     {**self.plan, "kind": "agentic_layout_v2"}):
            with self.subTest(plan=plan), self.assertRaisesRegex(RuntimeError, "qualified managed update"):
                self.run_check(plan, self.root)



def load_updater(path=UPDATER):
    """The updater module from its file, as the installed runtime runs it; never placed in ``sys.modules``."""
    spec = importlib.util.spec_from_file_location("native_update_under_test", path)
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    return updater


def judged_before(db, schema_target):
    """The updater's store judgment before the record existed, line for line."""
    with sqlite3.connect('file:' + quote(str(db)) + '?mode=ro', uri=True) as connection:
        observed = connection.execute("SELECT name, version, digest FROM target_identity WHERE id='target'").fetchone()
    if list(observed or ()) != schema_target:
        raise ValueError('The existing database does not match the update’s supported schema')


def files(folder):
    """The store and its side files, by name, with their bytes."""
    return {path.name: path.read_bytes() for path in sorted(Path(folder).glob("operations.sqlite3*")) if path.is_file()}


def read_as_the_updater_does(db):
    """The updater's own read-only read just before the check (it leaves the side files a WAL store gets)."""
    connection = sqlite3.connect('file:' + quote(str(db)) + '?mode=ro', uri=True)
    try:
        connection.execute("SELECT 1 FROM sqlite_master").fetchall()
    finally:
        connection.close()


def outcome(call):
    try:
        call()
    except Exception as exc:  # the kind and text of a refusal are what is compared
        return type(exc), str(exc)
    return None


CORE_TARGET = list((lambda target: [target.name, target.version, target.digest])(host_assembly(ChangeFeed()).target))
ADMITTED = {"kind": "agentic_managed_update_v2", "complete": True, "state": {"migration_required": False},
            "provisioning": {"items": []}, "changes": []}


class StoreCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        self.instance = self.folder / "root" / "instance"
        (self.instance / "operations" / ".runtime").mkdir(parents=True, mode=0o700)
        (self.instance / "config").mkdir()
        (self.instance / "config" / "extensions.json").write_text('{"extensions": []}')
        self.components = {"app": self.folder / "app", "dashboard": self.folder / "dashboard"}
        for folder in self.components.values():
            folder.mkdir()
        self.paths = InstancePaths(self.instance, self.components)
        self.db = self.paths.database

    def core_store(self):
        Database(self.db, assembly=host_assembly(ChangeFeed())).initialize_target().close()

    def recorded_store(self):
        store_with_record(self.db, host_assembly(ChangeFeed(), NO_EXTENSIONS, tables=contributions()))

    def check(self, **options):
        with mock.patch.object(module, "load_prepared", return_value=(None, None, self.components)), \
                mock.patch.object(module, "prepare_upgrade", return_value=ADMITTED):
            return module.check(Path("/prepared"), self.instance, **options)


class UpdateStoreTests(StoreCase):
    def test_a_store_without_a_record_is_judged_as_before_and_is_not_read_by_the_check(self):
        updater = load_updater()
        wrong = [*CORE_TARGET[:2], "0" * 64]
        shapes = {
            "the core store": lambda: self.core_store(),
            "a store in WAL with both side files": lambda: self.wal_store(),
            "a store without an identity table": lambda: self.plain_store(),
            "an empty file": lambda: self.db.write_bytes(b""),
        }
        made = self.folder / "made"
        for name, make in shapes.items():
            for path in self.db.parent.glob("operations.sqlite3*"):
                path.unlink()
            make()
            shutil.rmtree(made, ignore_errors=True)
            made.mkdir()
            for path in self.db.parent.glob("operations.sqlite3*"):
                shutil.copy2(path, made / path.name)
            for target in (CORE_TARGET, wrong):
                with self.subTest(name, target=target[2][:6]):
                    copies = {}
                    for judge, call in (("before", judged_before), ("now", updater.require_supported_store)):
                        for path in self.db.parent.glob("operations.sqlite3*"):
                            path.unlink()
                        for path in made.iterdir():
                            shutil.copy2(path, self.db.parent / path.name)
                        copies[judge] = outcome(lambda: call(self.db, target)), files(self.db.parent)
                    self.assertEqual(copies["now"], copies["before"])
        # The check of the new runtime does not read a store the updater found without the record.
        self.core_store()
        before = files(self.db.parent)
        with mock.patch("qorqut.composition.extension_tables.recorded", side_effect=AssertionError("read")), \
                mock.patch("qorqut.composition.assembly.open_database", side_effect=AssertionError("opened")):
            self.assertEqual(self.check(), {"applied": False})
        self.assertEqual(files(self.db.parent), before)
        self.assertIs(updater.require_supported_store(self.db, CORE_TARGET), False)

    def plain_store(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("CREATE TABLE other (id)")
            connection.commit()

    def wal_store(self):
        self.core_store()
        writer = sqlite3.connect(self.db)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("UPDATE target_identity SET created_at = created_at || ' '")
        writer.commit()
        # Copied while the writer is open, so the pending WAL content is part of the shape.
        self.addCleanup(writer.close)

    def test_a_store_with_a_valid_record_opens_in_the_new_composition(self):
        self.recorded_store()
        # Before the record, the updater refused it: its identity is not the core-only one.
        with self.assertRaisesRegex(ValueError, "does not match"):
            judged_before(self.db, CORE_TARGET)
        self.assertIs(load_updater().require_supported_store(self.db, CORE_TARGET), True)
        opened = []
        from qorqut.composition.assembly import open_database as opener

        def open_database(paths, composed):
            opened.append(composed.target)
            return opener(paths, composed)

        read_as_the_updater_does(self.db)
        before = files(self.db.parent)
        with mock.patch("qorqut.composition.assembly.open_database", open_database):
            self.assertEqual(self.check(recorded_store=True), {"applied": False})
        self.assertEqual(opened, [host_assembly(ChangeFeed(), NO_EXTENSIONS, tables=contributions()).target])
        self.assertEqual(files(self.db.parent), before)

    def test_a_refused_record_row_refuses_the_update_before_any_of_its_statements_runs(self):
        rows = {"a row whose statements fail the check": attaching(self.folder),
                "a row that is not what this version writes": None}
        for name, contribution in rows.items():
            with self.subTest(name):
                for path in self.db.parent.glob("operations.sqlite3*"):
                    path.unlink()
                self.recorded_store()
                if contribution is None:
                    with closing(sqlite3.connect(self.db)) as connection:
                        text = connection.execute("SELECT contribution FROM extensiontables").fetchone()[0]
                    contribution = json.dumps(json.loads(text), indent=1)
                edit_row(self.db, contribution)
                read_as_the_updater_does(self.db)
                before = files(self.db.parent)
                with mock.patch("qorqut.composition.assembly.open_database", side_effect=AssertionError("opened")), \
                        mock.patch("qorqut_operations.storage.inventory.compile_target",
                                   side_effect=AssertionError("compiled")), \
                        self.assertRaisesRegex(RuntimeError, "previous runtime will be restored"):
                    self.check(recorded_store=True)
                self.assertEqual(files(self.db.parent), before)
                self.assertFalse((self.folder / ATTACHED).exists())

    def test_a_record_row_that_does_not_match_the_store_refuses_the_update(self):
        # A valid row for tables the store does not have: the store is not what its record composes.
        Database(self.db, assembly=host_assembly(ChangeFeed(), NO_EXTENSIONS, tables=contributions(rows=()))
                 ).initialize_target().close()
        from test_extension_tables import ROW, write_rows
        write_rows(self.db, (ROW,))
        read_as_the_updater_does(self.db)
        before = files(self.db.parent)
        with self.assertRaisesRegex(RuntimeError, "does not match this composition"):
            self.check(recorded_store=True)
        self.assertEqual(files(self.db.parent), before)


RUNNER = """
import json, os, sys
sys.path[:0] = json.loads(os.environ["CHECK_PATH"])
from pathlib import Path
from unittest import mock
arguments = sys.argv[1:]
with open(os.environ["CHECK_ARGUMENTS"], "a") as stream:
    stream.write(json.dumps(arguments) + "\\n")
assert arguments[:4] == ["-I", "-B", "-m", "qorqut.bootstrap.native_update_check"], arguments
if os.environ.get("CHECK_BEFORE_RECORD"):
    # A new runtime from before the record: its check takes only the two roots.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared-root", required=True)
    parser.add_argument("--instance-root", required=True)
    parser.parse_args(arguments[4:])
    sys.exit(0)
from qorqut.bootstrap import native_update_check as module
components = {name: Path(value) for name, value in json.loads(os.environ["CHECK_COMPONENTS"]).items()}
plan = {"kind": "agentic_managed_update_v2", "complete": True, "state": {"migration_required": False},
        "provisioning": {"items": []}, "changes": []}
try:
    with mock.patch.object(module, "load_prepared", return_value=(None, None, components)), \\
            mock.patch.object(module, "prepare_upgrade", return_value=plan):
        module.main(arguments[4:])
except RuntimeError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
"""

LAUNCHER = """
import json, os
from pathlib import Path
TREG_FACTORY, OPENROUTER_FACTORY, WHATSAPP_FACTORY = "treg:create", "openrouter:create", "whatsapp:create"
def read_json(path): return json.loads(Path(path).read_text())
def save_json(path, value): Path(path).write_text(json.dumps(value))
def installation_processes(root): return []
def require_completed_extension_installs(runtime): pass
def environment(root, home): return dict(os.environ)
def require_no_terminal_agents(root, env): pass
def stop_managed_hosts(root, env): pass
def stop_agent_browsers(root): pass
def verify(runtime): return read_json(Path(runtime) / "PAYLOAD.json")
def prepare(root, env): pass
def initialize(root, env): pass
"""


class UpdaterTests(StoreCase):
    """The whole update with stand-ins for the launcher and the new runtime's interpreter; the check itself is real."""

    def setUp(self):
        super().setUp()
        self.root = self.instance.parent
        runtime = self.root / "runtime"
        runtime.mkdir()
        shutil.copy2(UPDATER, runtime / "update.py")
        (runtime / "launch.py").write_text(LAUNCHER)
        (runtime / "PAYLOAD.json").write_text(json.dumps({"version": "old", "schema_target": CORE_TARGET}))
        (self.root / "qorqbot").write_text("old launcher\n")
        (self.instance / "home").mkdir()
        self.arguments = self.folder / "arguments.jsonl"
        runner = self.folder / "runner.py"
        runner.write_text(RUNNER)
        self.archive = self.folder / "update.tar.gz"
        python = "#!/bin/sh\nexec " + json.dumps(sys.executable) + " " + json.dumps(str(runner)) + ' "$@"\n'
        with tarfile.open(self.archive, "w:gz") as tar:
            for name, text, mode in (("qorqbot/runtime/PAYLOAD.json", json.dumps({"version": "new", "schema_target": CORE_TARGET}), 0o600),
                                     ("qorqbot/runtime/launch.py", LAUNCHER, 0o600),
                                     ("qorqbot/runtime/venv/bin/python", python, 0o700),
                                     ("qorqbot/qorqbot", "new launcher\n", 0o700)):
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
        self.environment = {"CHECK_PATH": json.dumps(sys.path), "CHECK_ARGUMENTS": str(self.arguments),
                            "CHECK_COMPONENTS": json.dumps({key: str(value) for key, value in self.components.items()})}

    def update(self, **environment):
        updater = load_updater(self.root / "runtime" / "update.py")
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with mock.patch.dict(os.environ, {**self.environment, **environment}):
            return updater.main([str(self.archive), "--sha256", digest])

    def state(self):
        journal = json.loads((self.root / ".run" / "update.json").read_text())
        return (journal["phase"], json.loads((self.root / "runtime" / "PAYLOAD.json").read_text())["version"],
                (self.root / "qorqbot").read_text())

    def passed(self):
        return [json.loads(line)[4:] for line in self.arguments.read_text().splitlines()]

    def test_a_store_with_a_valid_record_is_admitted_by_the_new_runtime(self):
        self.recorded_store()
        before = self.db.read_bytes()
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.state(), ("complete", "new", "new launcher\n"))
        self.assertIn("--recorded-store", self.passed()[0])
        self.assertEqual(self.db.read_bytes(), before)

    def test_a_store_without_a_record_is_admitted_without_the_store_being_read_by_the_check(self):
        self.core_store()
        self.assertEqual(self.update(), 0)
        self.assertEqual(self.state(), ("complete", "new", "new launcher\n"))
        self.assertNotIn("--recorded-store", self.passed()[0])

    def test_a_refused_record_row_keeps_the_previous_runtime(self):
        self.recorded_store()
        edit_row(self.db, attaching(self.folder))
        before = self.db.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            self.update()
        self.assertEqual(self.state(), ("restored-before-start", "old", "old launcher\n"))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.folder / ATTACHED).exists())

    def test_a_new_runtime_from_before_the_record_does_not_admit_a_store_with_one(self):
        self.recorded_store()
        with self.assertRaises(subprocess.CalledProcessError):
            self.update(CHECK_BEFORE_RECORD="1")
        self.assertEqual(self.state(), ("restored-before-start", "old", "old launcher\n"))


if __name__ == "__main__":
    unittest.main()
