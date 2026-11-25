"""A project's files sit directly in its folder, and a new project's folder holds an empty code folder.

Locations, the permissions prepared for an agent, the code a preparation leaves alone, the shape of a created project
and full recovery of a project without a code folder."""
import isolated_cli_environment

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from qorqut.bootstrap import preparation
from qorqut.composition import locations
from qorqut.instance_paths import InstancePaths
from qorqut.storage import full_recovery, recovery
from qorqut_operations.contracts import DomainError
from qorqut_operations.projects.creation import creation as project_creation
from qorqut_operations.storage.directories import DirectoryEffects
from qorqut_operations.storage.directory_layout import checked_values, directory_values

from test_preparation_modes import Fixture as PreparationFixture

ROW = {"id": "p1", "slug": "alpha"}


class Locations(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="project layout ")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = InstancePaths(self.root, {})

    def test_a_project_is_its_own_folder(self):
        folder = self.root / "operations" / "projects" / "alpha"
        self.assertEqual(locations.project_root(self.paths, ROW), folder)
        self.assertEqual(locations.document_root(self.paths, ROW, "notes/a.md"), folder)
        self.assertEqual(locations.project_code(self.paths, ROW), folder / "code")
        self.assertFalse(hasattr(self.paths, "project_headquarters"))

    def test_a_document_path_is_the_location_below_the_project_folder(self):
        for path in ("notes/a.md", "context/product.md", "agents/plan.md"):
            location = locations.document_location(self.paths, ROW, path)
            self.assertEqual(location, "projects/alpha/" + path)
            self.assertEqual(locations.document_path_at(self.paths, ROW, location), path)
        self.assertIsNone(locations.document_path_at(self.paths, ROW, "projects/beta/notes/a.md"))
        self.assertIsNone(locations.document_path_at(self.paths, ROW, "projects/alpha"))


class Permissions(PreparationFixture):
    """What an agent may write and read in a project, with and without a code folder."""

    def setUp(self):
        super().setUp()
        self.resolver.paths = InstancePaths(self.root, {})
        self.connection.execute("INSERT INTO projects VALUES ('p1','alpha',NULL)")
        self.folder = self.root / "operations" / "projects" / "alpha"

    def prepared(self):
        projection = preparation.build_projection(
            self.resolver, self.config, registration_id="reg-1", endpoint="http://127.0.0.1:1096",
            interpreter=sys.executable, codex_permission_mode="scoped")
        claude = json.loads(projection.files[".claude/settings.local.json"])["permissions"]
        codex = tomllib.loads(projection.files[".codex/config.toml"].decode())["permissions"]["qorqut-agent"]["filesystem"]
        return projection, claude, codex

    def project_rules(self, claude, codex):
        mine = [rule for rule in claude["allow"] if "/projects/" in rule]
        return mine, {path: mode for path, mode in codex.items() if "/projects/" in path}

    def test_a_missing_code_folder_grants_nothing(self):
        projection, claude, codex = self.prepared()
        allowed, filesystem = self.project_rules(claude, codex)
        self.assertEqual(allowed, [f"Read(/{self.folder}/{name}/**)" for name in ("context", "analytics", "assets")])
        self.assertNotIn("additionalDirectories", claude)
        self.assertNotIn("write", filesystem.values())
        self.assertEqual(projection.metadata["project_code_roots"], [])
        self.assertFalse((self.folder / "code").exists())

    def test_an_existing_code_folder_is_the_only_project_write_root(self):
        (self.folder / "code").mkdir(parents=True)
        projection, claude, codex = self.prepared()
        allowed, filesystem = self.project_rules(claude, codex)
        self.assertIn(f"Edit(/{self.folder}/code/**)", allowed)
        self.assertEqual([rule for rule in allowed if rule.startswith("Edit(")], [f"Edit(/{self.folder}/code/**)"])
        self.assertEqual(claude["additionalDirectories"], [str(self.folder / "code")])
        self.assertEqual({path for path, mode in filesystem.items() if mode == "write"}, {str(self.folder / "code")})
        self.assertEqual(projection.metadata["project_code_roots"], [str(self.folder / "code")])

    def test_protected_entries_are_read_only_names_in_the_project_folder(self):
        for code in (False, True):
            if code:
                (self.folder / "code").mkdir(parents=True)
            _, _, codex = self.prepared()
            self.assertEqual({path: mode for path, mode in codex.items() if mode == "read" and "/projects/" in path},
                             {str(self.folder / name): "read" for name in ("CLAUDE.md", "context", "tasks", "log")})
            self.assertNotIn(str(self.folder), codex)

    def test_the_instruction_text_names_code_roots_only_when_there_are_some(self):
        with mock.patch("qorqut_agentic.behavior.workspace.documents", return_value={"CLAUDE.md": b"c", "AGENTS.md": b"a"}) as documents:
            self.prepared()
            self.assertEqual(documents.call_args.kwargs["code_roots"], [])
            (self.folder / "code").mkdir(parents=True)
            self.prepared()
            self.assertEqual(documents.call_args.kwargs["code_roots"], [self.folder / "code"])

    def test_a_code_folder_that_is_a_link_is_refused(self):
        self.folder.mkdir(parents=True)
        (self.root / "elsewhere").mkdir()
        os.symlink(self.root / "elsewhere", self.folder / "code")
        with self.assertRaises(DomainError):
            self.prepared()


class PreparationLeavesCodeAlone(PreparationFixture):
    def test_applying_a_preparation_creates_no_code_folder(self):
        self.resolver.paths = InstancePaths(self.root, {})
        self.connection.execute("INSERT INTO projects VALUES ('p1','alpha',NULL)")
        folder = self.root / "operations" / "projects" / "alpha"
        folder.mkdir(parents=True)
        projection = preparation.build_projection(
            self.resolver, self.config, registration_id="reg-1", endpoint="http://127.0.0.1:1096", interpreter=sys.executable)
        projection.metadata["project_code_roots"] = [str(folder / "code")]
        repositories = {
            "machine_registrations": SimpleNamespace(list=lambda **kwargs: [], insert=lambda values: None),
            "credentials": SimpleNamespace(insert=lambda values: None)}

        @contextmanager
        def unit():
            yield SimpleNamespace(repository=repositories.__getitem__, connection=self.connection)
        self.resolver.database = SimpleNamespace(read=unit, transaction=unit)
        self.resolver.resolve_agent_config = lambda principal: SimpleNamespace(
            to_dict=lambda: {**self.data, "agentic_catalog_revision": 1, "preparation": {}}, effective_config_id="c")
        self.resolver.profile = lambda principal, connection=None: (SimpleNamespace(revision=1), {})
        self.resolver.locks = SimpleNamespace(recovery=self.root / "recovery.lock")
        principal = SimpleNamespace(id="agent-1")
        with (mock.patch.object(preparation, "build_projection", return_value=projection),
              mock.patch.object(preparation, "SUPPORTED_GLOBAL_ROLES", {"worker"}),
              mock.patch.object(preparation, "preparation_lock", lambda paths, principal_id: contextmanager(lambda: (yield))()),
              mock.patch("qorqut_operations.storage.admission.InstallationRecoveryLock",
                         lambda path: SimpleNamespace(writer=contextmanager(lambda: (yield)))),
              mock.patch("qorqut_operations.application.Application.ensure_writes_open", lambda uow: None),
              mock.patch.object(preparation, "seed_instance_guidance", lambda resolver, data: {}),
              mock.patch.object(preparation, "runtime_compatibility", lambda runtime, version: {"supported": True}),
              mock.patch.object(preparation, "checkpoint_protocols", lambda versions, reports: {}),
              mock.patch.object(preparation, "service_url", lambda endpoint: endpoint)):
            preparation._prepare_agent_locked(self.resolver, principal, endpoint="http://127.0.0.1:1096",
                                              versions={"claude-code": "2.1.0"}, interpreter=sys.executable)
        self.assertTrue((self.workspace / ".qorqut/credential").is_file())
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [])


class CreatedProject(unittest.TestCase):
    def test_creation_makes_the_project_folder_with_an_empty_code_folder(self):
        self.assertEqual(project_creation.LAYOUT, ("code",))
        self.assertEqual(checked_values(directory_values(project_creation.LAYOUT)), ("code",))

    def test_the_created_code_folder_exists_and_is_empty(self):
        root = Path(tempfile.mkdtemp(prefix="created project ")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            DirectoryEffects._build(descriptor, checked_values(directory_values(project_creation.LAYOUT)))
        finally:
            os.close(descriptor)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["code"])
        self.assertTrue((root / "code").is_dir())
        self.assertEqual(list((root / "code").iterdir()), [])

    def test_the_project_row_has_no_second_location(self):
        root = Path("/instance/operations/projects")
        operations = SimpleNamespace(project_root=lambda row: root / row["slug"], projects_directory="projects",
                                     relative_path=lambda path: Path(path).relative_to("/instance/operations").as_posix(),
                                     operations_root=Path("/instance/operations"), staging_root=Path("/instance/staging"))
        rows = {"installation": {"owner_principal_id": "h"}, "principals": {"kind": "human", "active": 1, "id": "h"}}
        uow = SimpleNamespace(repository=lambda name: SimpleNamespace(get=lambda *a, **k: rows[name]),
                              connection=SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None)))
        recorded = []
        builder = SimpleNamespace(
            expect=lambda *a: None, targets={}, changes=[], resources=[], previews=[], effects=[],
            record=lambda *a, **k: recorded.append(a), build=lambda: None)
        creation = project_creation.Creation(operations)
        creation.workers = lambda uow: []
        creation.occupied = lambda slug, uow, location=None: False
        with mock.patch.object(project_creation, "ref", lambda *parts: parts):
            creation.prepare(builder, {"name": "Alpha"}, uow)
        project = recorded[0][3]["project"]
        self.assertEqual(project["workspace"], str(root / "alpha"))
        self.assertEqual(set(project), {"id", "slug", "workspace", "name", "config_json"})
        self.assertEqual(recorded[0][3]["directories"], ["code"])
        self.assertEqual(checked_values(builder.changes[0].values), ("code",))
        self.assertEqual(builder.previews[0].fields[0].after, ("projects/alpha", "projects/alpha/code"))
        self.assertIn("an empty code folder", builder.effects[0])


class CodeIsOptionalInFullRecovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="recovery layout ")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.authority = self.root / "authority"
        (self.authority / "authored").mkdir(parents=True)
        (self.authority / "authored/manifest.json").write_text(json.dumps({"projects": [{"slug": "alpha", "root": "projects/alpha"}]}))
        self.operations = self.root / "operations"
        (self.operations / "projects/alpha").mkdir(parents=True)
        self.store = SimpleNamespace(root=self.operations)

    def sources(self):
        return full_recovery.FullRecoveryStore._runtime_sources(self.store, self.authority)

    def test_a_project_without_a_code_folder_has_no_extra_source(self):
        self.assertEqual(self.sources(), [self.authority])

    def test_an_existing_code_folder_is_carried(self):
        (self.operations / "projects/alpha/code").mkdir()
        self.assertEqual(self.sources(), [self.authority, self.operations / "projects/alpha/code"])

    def test_a_code_file_is_refused(self):
        (self.operations / "projects/alpha/code").write_text("x")
        with self.assertRaises(DomainError):
            self.sources()

    def test_a_standalone_repository_in_the_project_folder_is_carried(self):
        (self.operations / "projects/alpha/.git").mkdir()
        self.assertEqual(self.sources(), [self.authority, self.operations / "projects/alpha/.git"])

    def test_restoring_a_project_without_code_moves_nothing_for_it(self):
        raw, target = self.root / "raw", self.root / "target"
        (raw / "projects/alpha").mkdir(parents=True)
        (target / "projects/alpha").mkdir(parents=True)
        full_recovery.FullRecoveryStore._merge_runtime(self.store, raw, target, self.authority)
        self.assertEqual(list((target / "projects/alpha").iterdir()), [])

    def test_restoring_moves_an_existing_code_folder_into_place(self):
        raw, target = self.root / "raw", self.root / "target"
        (raw / "projects/alpha/code").mkdir(parents=True)
        (raw / "projects/alpha/code/main.py").write_text("print()")
        (target / "projects/alpha").mkdir(parents=True)
        full_recovery.FullRecoveryStore._merge_runtime(self.store, raw, target, self.authority)
        self.assertEqual((target / "projects/alpha/code/main.py").read_text(), "print()")


class BackupScope(unittest.TestCase):
    def test_the_manifest_names_the_project_folder_only(self):
        paths = SimpleNamespace(operations=Path("/instance/operations"))
        instance = InstancePaths(Path("/instance"), {})
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE projects (id TEXT, slug TEXT)")
        connection.execute("INSERT INTO projects VALUES ('p1','alpha')")
        self.assertEqual(recovery.project_entries(connection, instance), [{"slug": "alpha", "root": "projects/alpha"}])
        self.assertEqual(paths.operations, instance.operations)

    def test_a_code_folder_at_the_project_root_is_not_walked(self):
        root = Path(tempfile.mkdtemp(prefix="backup scope ")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("notes/a.md", "agents/x/b.md", "code/c.py", "browser/d", "notes/code/e.md"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text("x")
        walked = {path.relative_to(root).as_posix() for path in recovery._walk(root, top_skip=recovery.PRIVATE_PROJECT_DIRECTORIES)}
        self.assertEqual(walked, {"notes/a.md", "agents/x/b.md", "notes/code/e.md"})


if __name__ == "__main__":
    unittest.main()
