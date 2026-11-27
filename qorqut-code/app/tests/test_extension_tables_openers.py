"""Every place that opens the instance's store reads the store's record of extension tables first.

For each opener: a store without the record opens with today's composition (same digest); a store with a valid record
opens, where today's composition refuses it; and a recorded row that fails the statement check refuses the store before
any of its statements runs anywhere, and no file appears. The last test keeps a new opener from being added without a
record read."""
import ast
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import qorqut
from qorqut.bootstrap import agentic_migration
from qorqut.composition.assembly import host_assembly
from qorqut.composition.extension_tables import recorded
from qorqut.extensions import NO_EXTENSIONS, ExtensionSet
from qorqut.instance_paths import InstancePaths
from qorqut.migration import upgrade
from qorqut.service.changes import ChangeFeed
from qorqut.service.ownership import InstanceOwnership
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import Database

from test_extension_tables import ATTACHED, attaching, contributions, edit_row, store_with_record
from test_migration_upgrade import SyntheticExtension


class Stop(Exception):
    """Raised by a stand-in once the opener under test has handed over its database."""


class OpenerCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        root = self.folder / "instance"
        (root / "operations" / ".runtime").mkdir(parents=True, mode=0o700)
        self.app, self.dashboard, self.workspace = (self.folder / name for name in ("app", "dashboard", "workspace"))
        for folder in (self.app, self.dashboard, self.workspace):
            folder.mkdir()
        self.paths = InstancePaths(root, {"app": self.app, "dashboard": self.dashboard})

    def stores(self, extensions=NO_EXTENSIONS, compose=None):
        """``(name, expected assembly or None for a refusal)`` for each kind of store, made in turn at the instance path.

        ``compose(extensions, tables)`` gives the assembly a store is made for; by default the opener's own."""
        compose = compose or (lambda selection, tables: host_assembly(ChangeFeed(), selection, tables=tables))
        for name in ("without a record", "with a record", "with a refused row"):
            for path in self.paths.database.parent.glob("operations.sqlite3*"):
                path.unlink()
            if name == "without a record":
                Database(self.paths.database, assembly=compose(extensions, ())).initialize_target().close()
                expected = host_assembly(ChangeFeed(), extensions)
            else:
                store_with_record(self.paths.database, compose(extensions, contributions()))
                expected = host_assembly(ChangeFeed(), extensions, tables=contributions())
                if name == "with a refused row":
                    edit_row(self.paths.database, attaching(self.folder))
                    expected = None
                else:
                    # Without the record read the store is refused: today's composition is not the store's.
                    with self.assertRaises(DomainError):
                        Database(self.paths.database, assembly=host_assembly(ChangeFeed(), extensions)).open_existing()
            yield name, expected

    def refused(self, call):
        with self.assertRaises(DomainError) as raised:
            call()
        self.assertEqual(raised.exception.code, "invalid_database")
        self.assertIn("ATTACH", raised.exception.message)
        self.assertFalse((self.folder / ATTACHED).exists())


class ServiceOpenersTest(OpenerCase):
    def test_the_service_start_opens_the_store_with_its_record(self):
        from qorqut.service import main as service_main
        for name, expected in self.stores():
            with self.subTest(name):
                handed = {}

                def assemble(*args, **kwargs):
                    handed["database"] = kwargs["database"]
                    raise DomainError("invalid_configuration", "stopped by the test")

                with mock.patch("qorqut.bootstrap.extension_change.apply_pending"), \
                        mock.patch("qorqut.extensions.installed_selection", return_value=NO_EXTENSIONS), \
                        mock.patch.object(service_main, "assemble", assemble):
                    status = service_main.main(["--workspace", str(self.workspace), "--instance-root",
                                                str(self.paths.instance_root), "--component", f"app={self.app}",
                                                "--component", f"dashboard={self.dashboard}"])
                self.assertEqual(status, 1)
                if expected is None:
                    self.assertEqual(handed, {})
                    self.assertFalse((self.folder / ATTACHED).exists())
                else:
                    self.assertEqual(handed["database"].assembly.target, expected.target)
                    handed["database"].close()

    def build(self, database=None):
        from qorqut.service import runtime
        handed = {}

        def compose(*args, **kwargs):
            handed.update(kwargs)
            raise Stop

        with mock.patch.object(runtime, "compose_runtime", compose), \
                mock.patch("qorqut.agentic.installation.open_installation", return_value=(None, None)), \
                self.assertRaises(Stop):
            runtime.build_runtime(self.workspace, paths=self.paths, database=database, extensions=NO_EXTENSIONS)
        return handed["database"]

    def test_build_runtime_opens_or_validates_the_store_with_its_record(self):
        for name, expected in self.stores():
            with self.subTest(name):
                if expected is None:
                    self.refused(self.build)
                    continue
                self.assertEqual(self.build().assembly.target, expected.target)
                # Given a store opened as the service opens it, the selected storage is judged with the record.
                given = Database(self.paths.database, assembly=expected)
                self.assertIs(self.build(given), given)

    def test_the_package_adoption_opens_the_store_with_its_record(self):
        from qorqut.migration import package_adoption
        for name, expected in self.stores():
            with self.subTest(name):
                handed = {}

                def build_runtime(workspace, *, paths, database, **kwargs):
                    handed["database"] = database
                    raise Stop

                options = SimpleNamespace(extensions_config=None, workspace=self.workspace, command="plan")
                with mock.patch.object(package_adoption, "instance_paths_from_arguments", return_value=self.paths), \
                        mock.patch("qorqut.extensions.installed_selection", return_value=NO_EXTENSIONS), \
                        mock.patch("qorqut.service.runtime.build_runtime", build_runtime):
                    if expected is None:
                        self.refused(lambda: package_adoption.run(options))
                        continue
                    with self.assertRaises(Stop):
                        package_adoption.run(options)
                self.assertEqual(handed["database"].assembly.target, expected.target)

    def test_the_owner_preparation_opens_the_store_with_its_record(self):
        from qorqut.bootstrap import owner_prepare
        for name, expected in self.stores():
            with self.subTest(name):
                handed = {}

                def prepare(database, **kwargs):
                    handed["database"] = database
                    raise DomainError("invalid_configuration", "stopped by the test")

                with mock.patch.object(owner_prepare, "prepare", prepare):
                    status = owner_prepare.main(["prepare", "--instance-root", str(self.paths.instance_root),
                                                 "--project", "alpha", "--root", str(self.workspace),
                                                 "--endpoint", "http://127.0.0.1:1"])
                self.assertEqual(status, 1)
                if expected is None:
                    self.assertEqual(handed, {})
                    self.assertFalse((self.folder / ATTACHED).exists())
                else:
                    self.assertEqual(handed["database"].assembly.target, expected.target)
                    handed["database"].close()


class UpgradeOpenerTest(OpenerCase):
    def test_the_ordered_upgrade_composes_every_state_with_the_record(self):
        extensions = ExtensionSet((SyntheticExtension(),))
        for name, expected in self.stores(extensions, lambda selection, tables: upgrade.chain(selection, tables)[0][0]):
            with self.subTest(name), closing(sqlite3.connect(self.paths.database, isolation_level=None)) as connection:
                if expected is None:
                    identity = connection.execute("SELECT digest FROM target_identity").fetchall()
                    self.refused(lambda: upgrade.inspect(connection, extensions))
                    self.refused(lambda: upgrade.apply(connection, extensions))
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(connection.execute("SELECT digest FROM target_identity").fetchall(), identity)
                    continue
                report = upgrade.inspect(connection, extensions)
                self.assertEqual((report["store"], report["target_digest"]), (upgrade.BASE, expected.target.digest))
                result = upgrade.apply(connection, extensions)
                self.assertEqual(result["store"], upgrade.STATES[-1])
                self.assertEqual(connection.execute("SELECT digest FROM target_identity").fetchone()[0],
                                 expected.target.digest)
                self.assertEqual(upgrade.revert(connection, extensions, to=upgrade.BASE, discard_rows=True)["store"],
                                 upgrade.BASE)
            if expected is not None:
                Database(self.paths.database, assembly=upgrade.chain(extensions, recorded(self.paths.database))[0][0]
                         ).open_existing().close()


class AgenticMigrationOpenerTest(OpenerCase):
    def test_the_named_migration_recognises_and_applies_with_the_record(self):
        def predecessor(selection, tables):
            return agentic_migration._transition_for(selection, tables=tables).predecessor

        backups = self.folder / "backups"
        backups.mkdir(mode=0o700)
        for name, expected in self.stores(compose=predecessor):
            with self.subTest(name):
                if expected is None:
                    for call in (agentic_migration.inspect_migration, agentic_migration.prepare_migration):
                        self.refused(lambda: call(self.paths))
                    continue
                self.assertEqual(agentic_migration.inspect_migration(self.paths)["state"], "predecessor")
                plan = agentic_migration.prepare_migration(self.paths)
                self.assertEqual(plan["target"]["digest"], expected.target.digest)
                result = agentic_migration.apply_migration(self.paths, plan, backup=backups / f"{len(name)}.sqlite3")
                self.assertTrue(result["applied"])
                self.assertEqual(agentic_migration.inspect_migration(self.paths)["state"], "target")
                Database(self.paths.database, assembly=expected).open_existing().close()

    def test_the_extension_addition_keeps_the_record_on_both_sides(self):
        previous, selected = NO_EXTENSIONS, ExtensionSet((SyntheticExtension(),))
        backups = self.folder / "backups"
        backups.mkdir(mode=0o700)
        for name, expected in self.stores(previous):
            with self.subTest(name):
                if expected is None:
                    self.refused(lambda: agentic_migration.prepare_extension_storage(
                        self.paths, previous=previous, selected=selected))
                    continue
                plan = agentic_migration.prepare_extension_storage(self.paths, previous=previous, selected=selected)
                self.assertEqual(plan["predecessor"]["digest"], expected.target.digest)
                owner = InstanceOwnership(self.paths).acquire()
                try:
                    result = agentic_migration.apply_extension_storage(
                        self.paths, plan, previous=previous, selected=selected, backup=backups / f"{len(name)}.sqlite3",
                        ownership=owner)
                finally:
                    owner.release()
                self.assertTrue(result["applied"])
                Database(self.paths.database, assembly=host_assembly(
                    ChangeFeed(), selected, tables=recorded(self.paths.database))).open_existing().close()


# -- no new opener without a record read --------------------------------------------------------------------------

PACKAGE = Path(qorqut.__file__).resolve().parent
# Compositions that are never compared with an existing store, by file and enclosing function: the core-only reference
# of two checks, and the composition of a new store, which has no record.
WITHOUT_A_STORE = {("composition/assembly.py", "require_selected_storage"),
                   ("composition/assembly.py", "require_additive_registrations"),
                   ("bootstrap/initialize.py", "seed_instance")}
# Every ``Database(`` in the package, by file and enclosing function, with how many. Each one's assembly was composed
# with the store's record (or is the new store of ``initialize_empty_target``); a new one must be looked at and added.
DATABASES = {("composition/assembly.py", "open_database"): 1, ("composition/assembly.py", "initialize_empty_target"): 1,
             ("bootstrap/agentic_migration.py", "_apply_transition"): 1,
             ("bootstrap/agentic_migration.py", "_publish_backup"): 1,
             ("bootstrap/agentic_migration.py", "apply_extension_storage"): 1,
             ("bootstrap/extension_install.py", "install"): 2,
             # Already fails on every installation: it passes no assembly at all.
             ("storage/routine_cost_repair.py", "main"): 1}


def _calls(tree):
    """``(enclosing function name, call)`` for every call in a module."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                found.append((function, child))
            visit(child, function)

    visit(tree, None)
    return found


def _name(call):
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)


class EveryOpenerReadsTheRecordTest(unittest.TestCase):
    """Fails when a ``host_assembly(`` / ``host_contributions(`` call without ``tables=``, an ``open_database(`` whose
    assembly is not such a call, or a new ``Database(`` is added to the package. It does not catch an opener outside the
    package (the native updater, tools), one that passes a ``tables=`` value not read from the store it opens, a store read
    with ``sqlite3`` directly, or an assembly reused from an open ``Database``."""

    def test_no_composition_opens_a_store_without_its_record(self):
        databases = {}
        missing = []
        for path in sorted(PACKAGE.rglob("*.py")):
            relative = path.relative_to(PACKAGE).as_posix()
            for function, call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
                name, place = _name(call), f"{relative}:{call.lineno} in {function}"
                keywords = {keyword.arg for keyword in call.keywords}
                if name in ("host_assembly", "host_contributions") and "tables" not in keywords \
                        and (relative, function) not in WITHOUT_A_STORE:
                    missing.append(place)
                if name == "open_database":
                    assembly = call.args[1] if len(call.args) > 1 else None
                    if not (isinstance(assembly, ast.Call) and _name(assembly) == "host_assembly"):
                        missing.append(place)
                if name == "Database":
                    databases[(relative, function)] = databases.get((relative, function), 0) + 1
        self.assertEqual(missing, [], "compose with tables=recorded(...) of the store being opened")
        self.assertEqual(databases, DATABASES)


if __name__ == "__main__":
    unittest.main()
