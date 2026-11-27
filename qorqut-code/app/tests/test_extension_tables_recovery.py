"""Recovery and the record of extension tables: each saved generation is verified and restored against the composition
of its own record and the running selection, not against the running store's.

A generation saved before the tables were made verifies and restores after they were made; one saved with them restores
after the extension was removed; ``prune`` counts both; a generation whose recorded row is refused is not verified; and
with no record anywhere, verify, restore and prune give what they gave before, with no more files.

The parts of a generation that are not the operational store (the authored archive and the Context pair) are stand-ins
here; the store part is real. ``snapshot_database`` patched to hand back the running database is the code before."""
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.extensions import ExtensionSet
from qorqut.instance_paths import InstancePaths
from qorqut.service.changes import ChangeFeed
from qorqut.storage import full_recovery, recovery
from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.storage.database import Database

from test_extension_tables import ATTACHED, attaching, contributions, edit_row, store_with_record
from test_migration_upgrade import SyntheticExtension

SELECTION = ExtensionSet((SyntheticExtension(),))


def composed(tables):
    return host_assembly(ChangeFeed(), SELECTION, tables=tables)


def before_the_record(database, extensions, snapshot):
    """``snapshot_database`` as the code was before the record: the running database judges every generation."""
    return database


def tree(folder):
    """Every file under ``folder``, by relative path, with its bytes. The engine's temporary snapshot name is random, so
    it is shown without its random part."""
    return {re.sub(r"\.snapshot-[^.]+\.", ".snapshot-.", path.relative_to(folder).as_posix()): path.read_bytes()
            for path in sorted(Path(folder).rglob("*")) if path.is_file()}


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name).resolve()
        root = self.folder / "instance"
        (root / "operations" / ".runtime").mkdir(parents=True, mode=0o700)
        components = {name: self.folder / name for name in ("app", "dashboard")}
        for folder in components.values():
            folder.mkdir()
        self.paths = InstancePaths(root, components)
        self.saved = self.folder / "saved"
        self.saved.mkdir()
        context = mock.MagicMock()
        context.mark_incomplete.side_effect = lambda target, generation: Path(target) / ".runtime/restore-incomplete.json"
        context.restore.return_value = {}
        for target, value in ((recovery, "ContextRecovery"), (recovery, "require_target_projects"),
                              (full_recovery, "require_target_projects")):
            patcher = mock.patch.object(target, value, return_value=context if value == "ContextRecovery" else None)
            patcher.start()
            self.addCleanup(patcher.stop)
        for module in (recovery, full_recovery):
            for name, stand_in in (("verify_snapshot", lambda directory: SimpleNamespace(fingerprint="authored",
                                                                                         root=self.paths.operations)),
                                   ("restore_snapshot", lambda directory, target: Path(target).mkdir(exist_ok=True))):
                patcher = mock.patch.object(module, name, stand_in)
                patcher.start()
                self.addCleanup(patcher.stop)

    def store(self, path, with_tables):
        """A store at ``path``: before the tables (no record), or with the record and its tables."""
        if with_tables:
            store_with_record(path, composed(contributions()))
        else:
            Database(path, assembly=composed(())).initialize_target().close()
        return Database(path, assembly=composed(contributions() if with_tables else ())).open_existing()

    def running(self, with_tables):
        for path in self.paths.database.parent.glob("operations.sqlite3*"):
            path.unlink()
        database = self.store(self.paths.database, with_tables)
        self.addCleanup(database.close)
        return database

    def backups(self, database):
        return recovery.BackupStore(database, self.paths, extensions=SELECTION)

    def generation(self, backups, with_tables, created_at="2026-11-20T10:00:00Z", row=None):
        """Save one generation of a store before or with the tables, as ``BackupStore.create`` lays it out."""
        source = self.saved / uuid4().hex
        database = self.store(source, with_tables)
        if row is not None:
            database.close()
            edit_row(source, row)
            database = Database(source, assembly=composed(contributions()))
        identity = str(uuid4())
        directory = backups.destination / identity
        (directory / "authored").mkdir(parents=True)
        (directory / "authored" / "sources.tar").write_bytes(b"authored")
        if row is None:
            saved = database.backup(directory / "state.sqlite3")
            database.close()
        else:
            shutil.copyfile(source, directory / "state.sqlite3")
            saved = {"checksum": recovery._digest(directory / "state.sqlite3"), "schema_version": 0, "record_counts": {}}
        metadata = {"schema_version": 2, "generation_id": identity, "state": "complete", "created_at": created_at,
                    "source_root": str(backups.root), "database_relative_path": backups.database_relative,
                    "database": {key: value for key, value in saved.items() if key != "path"},
                    "authored": {"fingerprint": "authored", "archive_sha256": recovery._digest(directory / "authored/sources.tar"),
                                 "files": 0, "artifact_digests": []},
                    "context": {}}
        (directory / "generation.json").write_bytes(canonical_bytes(metadata) + b"\n")
        return identity


class GenerationTest(RecoveryCase):
    def test_a_generation_saved_before_the_tables_verifies_and_restores_after_they_were_made(self):
        backups = self.backups(self.running(with_tables=True))
        identity = self.generation(backups, with_tables=False)
        with mock.patch.object(recovery, "snapshot_database", before_the_record), \
                self.assertRaises(DomainError) as raised:
            backups.verify(identity)
        self.assertEqual(raised.exception.code, "invalid_backup")
        self.assertEqual(backups.verify(identity)["generation_id"], identity)
        result = backups.restore(identity, self.folder / "rehearsal")
        restored = Database(Path(result["database"]), assembly=composed(())).open_existing()
        restored.close()

    def test_a_generation_saved_with_the_tables_restores_after_the_extension_was_removed(self):
        for running_tables in (False, True):
            with self.subTest(running_store_keeps_the_tables=running_tables):
                backups = self.backups(self.running(with_tables=running_tables))
                identity = self.generation(backups, with_tables=True)
                if not running_tables:
                    with mock.patch.object(recovery, "snapshot_database", before_the_record), \
                            self.assertRaises(DomainError):
                        backups.verify(identity)
                result = backups.restore(identity, self.folder / f"rehearsal-{running_tables}")
                self.assertIn("tablenote_items", result["record_counts"])
                Database(Path(result["database"]), assembly=composed(contributions())).open_existing().close()

    def test_prune_counts_generations_saved_before_and_with_the_tables(self):
        for running_tables in (True, False):
            with self.subTest(running_store_has_the_tables=running_tables):
                backups = self.backups(self.running(with_tables=running_tables))
                shutil.rmtree(backups.destination, ignore_errors=True)
                older = self.generation(backups, with_tables=not running_tables, created_at="2026-11-18T10:00:00Z")
                newer = self.generation(backups, with_tables=running_tables, created_at="2026-11-19T10:00:00Z")
                # Before, the generation that is not the running composition was skipped: neither counted nor removed.
                copy = self.folder / "copy"
                shutil.rmtree(copy, ignore_errors=True)
                shutil.copytree(backups.destination, copy)
                with mock.patch.object(recovery, "snapshot_database", before_the_record):
                    self.assertEqual(backups.prune(daily=1, weekly=1), [])
                shutil.rmtree(backups.destination)
                shutil.copytree(copy, backups.destination)
                self.assertEqual(backups.prune(daily=1, weekly=1), [older])
                self.assertEqual(sorted(path.name for path in backups.destination.iterdir()), [newer])

    def test_a_generation_whose_record_row_is_refused_is_not_verified_and_prune_keeps_it(self):
        backups = self.backups(self.running(with_tables=True))
        identity = self.generation(backups, with_tables=True, row=attaching(self.folder))
        with mock.patch.object(recovery.Database, "validate_snapshot", side_effect=AssertionError("validated")), \
                self.assertRaises(DomainError) as raised:
            backups.verify(identity)
        self.assertEqual(raised.exception.code, "invalid_backup")
        self.assertIn("ATTACH", raised.exception.message)
        self.assertEqual(backups.prune(daily=1, weekly=1), [])
        self.assertTrue((backups.destination / identity).is_dir())
        self.assertFalse((self.folder / ATTACHED).exists())


class NoRecordTest(RecoveryCase):
    def test_without_a_record_anywhere_verify_restore_and_prune_give_what_they_gave_before(self):
        backups = self.backups(self.running(with_tables=False))
        identities = [self.generation(backups, with_tables=False, created_at=f"2026-11-{day}T10:00:00Z")
                      for day in (17, 18, 19)]
        copy = self.folder / "copy"
        shutil.copytree(backups.destination, copy)
        results = {}
        handed = []
        real = recovery.snapshot_database

        def spy(database, extensions, snapshot):
            chosen = real(database, extensions, snapshot)
            handed.append(chosen is database)
            return chosen

        for code, choose in (("before", before_the_record), ("now", spy)):
            shutil.rmtree(backups.destination)
            shutil.copytree(copy, backups.destination)
            with mock.patch.object(recovery, "snapshot_database", choose):
                verified = [backups.verify(identity) for identity in identities]
                restored = backups.restore(identities[0], self.folder / f"rehearsal-{code}")
                rehearsal = tree(self.folder / f"rehearsal-{code}")
                pruned = backups.prune(daily=1, weekly=1)
            results[code] = (verified, {key: value for key, value in restored.items() if key not in ("destination", "database")},
                             sorted(rehearsal), pruned, tree(backups.destination))
        self.assertEqual(results["now"], results["before"])
        self.assertTrue(handed and all(handed))


class FullRecoveryTest(RecoveryCase):
    """The full restore checks the saved store as ``BackupStore.restore`` does."""

    def restore_authority(self, database, identity, backups):
        store = SimpleNamespace(database=database, extensions=SELECTION, paths=self.paths, root=self.paths.operations,
                                database_relative=Path(backups.database_relative), context=backups.context)
        target = self.folder / uuid4().hex
        full_recovery.FullRecoveryStore._restore_authority(store, backups.destination / identity, identity, target)
        return target / backups.database_relative

    def test_a_full_restore_uses_the_generation_s_own_record(self):
        for running_tables, saved_tables in ((True, False), (False, True), (False, False)):
            with self.subTest(running_tables=running_tables, saved_tables=saved_tables):
                database = self.running(with_tables=running_tables)
                backups = self.backups(database)
                identity = self.generation(backups, with_tables=saved_tables)
                if running_tables != saved_tables:
                    with mock.patch.object(full_recovery, "snapshot_database", before_the_record), \
                            self.assertRaises(DomainError):
                        self.restore_authority(database, identity, backups)
                restored = self.restore_authority(database, identity, backups)
                Database(restored, assembly=composed(contributions() if saved_tables else ())).open_existing().close()


if __name__ == "__main__":
    unittest.main()
