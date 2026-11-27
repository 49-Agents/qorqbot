"""The registry of installed extensions: finding their folders, recording them in the store, and copying their plugin
parts into a real agentic layer (docs/EXTENSION-CONTRACT.md)."""
from collections import OrderedDict
import json
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from qorqut import extension_package, extension_registry as registry
from qorqut.agentic.installation import IDENTITY
from qorqut.agentic.view import HostAgenticView
from qorqut.agents.resolver import AgentConfigResolver
from qorqut.composition.assembly import host_assembly
from qorqut.instance_paths import InstancePaths
from qorqut.operations_history.catalog import catalog
from qorqut.service.changes import ChangeFeed
from qorqut_agentic import AgenticLayer
from qorqut_agentic.errors import AgenticError, NotFoundError
from qorqut_operations.contracts import DomainError, Principal
from qorqut_operations.history_exports import TECHNICAL_EXCLUDED
from qorqut_operations.storage.database import Database

EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"
VERSION = "1.7.0"
ORDUBASHY, APPLIER = "agent-ordubashy", "agent-applier"


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        app = self.root / "source" / "qorqut-code" / "app"
        app.mkdir(parents=True)
        self.paths = InstancePaths(self.root / "instance", {"app": app})
        self.built_in = app.parent / "extensions"
        self.downloaded = self.paths.installed_extensions

    def place(self, where=None, name="network"):
        target = (where or self.downloaded) / name
        shutil.copytree(EXAMPLE, target)
        return target

    def manifest(self, folder, mutate):
        path = folder / "extension.yaml"
        data = yaml.safe_load(path.read_text())
        mutate(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def discover(self, **arguments):
        arguments = {"selected": (), "app_version": VERSION, "platform": "linux", **arguments}
        return registry.discover(self.paths, **arguments)

    def one(self, **arguments):
        found = self.discover(**arguments)
        self.assertEqual(len(found), 1, found)
        return found[0]


class DiscoverTest(Case):
    def test_the_example_is_found_and_fine(self):
        folder = self.place()
        item = self.one()
        self.assertEqual((item.id, item.folder, item.delivery, item.findings, item.unsupported),
                         ("network", folder, "downloadable", (), None))
        self.assertEqual(item.manifest.name, "Network")
        self.assertTrue(item.fine)

    def test_discovery_reads_only(self):
        self.place()
        self.place(self.built_in, "other")

        def tree():
            return sorted((str(path), path.stat().st_mtime_ns) for path in self.root.rglob("*"))
        before = tree()
        self.discover()
        self.assertEqual(tree(), before)
        self.assertFalse(self.paths.operations.exists())

    def test_no_folders_and_folders_without_a_manifest_are_nothing(self):
        self.assertEqual(self.discover(), ())
        (self.built_in / "telegram").mkdir(parents=True)
        (self.built_in / "telegram" / "README.md").write_text("Today's extension.")
        (self.built_in / "notes.txt").write_text("not a folder")
        self.assertEqual(self.discover(), ())

    def test_the_installation_gives_the_skill_names_it_ships_with(self):
        asked = []

        def shipped():
            asked.append(True)
            return frozenset({"qorqut-plugin-network"})
        # Nothing is asked where no folder was found.
        self.assertEqual(self.discover(shipped=shipped), ())
        self.assertEqual(asked, [])
        self.place()
        item = self.one(shipped=shipped)
        self.assertEqual(asked, [True])
        self.assertTrue(item.refused)
        self.assertIn("ships with Qorqbot already uses", item.findings[0])
        self.assertTrue(self.one(shipped=frozenset).fine)

    def test_a_folder_in_the_wrong_place_is_refused(self):
        self.place(self.built_in)
        item = self.one()
        self.assertEqual(item.delivery, "built-in")
        self.assertTrue(item.refused)
        self.assertIn("with the built-in extensions, but its manifest says the delivery is downloadable",
                      item.findings[0])

    def test_a_checker_finding_refuses_the_folder(self):
        folder = self.place()
        (folder / "README.md").unlink()
        item = self.one()
        self.assertEqual(item.findings, ("README.md is missing at the top of the folder.",))
        self.assertIsNotNone(item.manifest)
        self.manifest(folder, lambda data: data.pop("summary"))
        item = self.one()
        self.assertIsNone(item.manifest)
        self.assertEqual(item.id, "network")

    def test_the_same_id_in_both_places_refuses_both(self):
        self.place()
        built_in = self.place(self.built_in)
        self.manifest(built_in, lambda data: data.update(delivery="built-in"))
        found = self.discover()
        self.assertEqual([item.delivery for item in found], ["built-in", "downloadable"])
        for item in found:
            self.assertTrue(any("is also used by the folder" in finding for finding in item.findings), item.findings)

    def test_an_id_also_selected_the_old_way_is_refused(self):
        self.place()
        item = self.one(selected=("whatsapp", "network"))
        self.assertEqual(item.findings, ("The id 'network' is also selected in the installation's extension list.",))

    def test_platform_and_app_version_make_it_unsupported(self):
        folder = self.place()
        self.manifest(folder, lambda data: data.update(platforms=["linux", "mac"]))
        self.assertEqual(self.one(platform="windows").unsupported, "It runs only on linux and mac, not on windows.")
        self.assertEqual(self.one(app_version="1.6.1").unsupported,
                         "It needs Qorqbot >=1.7.0, and this is Qorqbot 1.6.1.")
        # A build without a version number does not compare the versions.
        self.assertTrue(self.one(app_version=None).fine)
        self.assertTrue(self.one(app_version="dev").fine)
        self.assertTrue(self.one(app_version="1.7.0-check.t20261126120000.gabcdef123456").fine)
        item = self.one(app_version="1.6.1")
        self.assertFalse(item.refused)
        self.assertFalse(item.fine)

    def test_a_required_extension_must_be_installed_and_able_to_run(self):
        folder = self.place()
        self.manifest(folder, lambda data: data["requires"].update(extensions=["helper"]))
        self.assertEqual(self.one().unsupported, "It needs the extension 'helper', which is not installed.")
        helper = self.place(name="helper")
        self.manifest(helper, lambda data: data.update(id="helper"))
        shutil.move(helper / "plugin" / "skills" / "network", helper / "plugin" / "skills" / "helper")
        skill = helper / "plugin" / "skills" / "helper" / "SKILL.md"
        skill.write_text(skill.read_text().replace("qorqut-plugin-network", "qorqut-plugin-helper"))
        (helper / "plugin" / "records.yaml").unlink()
        found = {item.id: item for item in self.discover()}
        self.assertTrue(found["helper"].fine, found["helper"])
        self.assertTrue(found["network"].fine)
        self.manifest(helper, lambda data: data.update(platforms=["mac"]))
        found = {item.id: item for item in self.discover()}
        self.assertEqual(found["network"].unsupported, "It needs the extension 'helper', which cannot run on this computer.")

    def test_version_comparison(self):
        for version, spec, expected in (("1.7.0", ">=1.7.0", True), ("1.6.9", ">=1.7.0", False),
                                        ("1.10.0", ">=1.7.0,<2.0.0", True), ("2.0.0", ">=1.7.0,<2.0.0", False),
                                        ("1.7.0", "== 1.7.0", True), ("1.7.0", "!=1.7.0", False),
                                        ("1.7.1", ">1.7.0", True), ("1.7.0", "<=1.6.0", False),
                                        ("1.3.3-local.t20261104193012.gfad178051f1a", "<1.3.3", False)):
            self.assertIs(registry.admits(version, spec), expected, (version, spec))
        for version, spec in (("dev", ">=1.0.0"), (None, ">=1.0.0"), ("1.0.0", "~1.0.0"), ("1.0.0", ">=1.0")):
            with self.assertRaises(ValueError):
                registry.admits(version, spec)

    def test_the_two_places(self):
        self.assertEqual(registry.extension_roots(self.paths), ((self.built_in, "built-in"),
                                                                (self.root / "instance" / "extensions", "downloadable")))


class StoreCase(Case):
    """A real store and a real agentic layer, both in the temporary folder."""

    def setUp(self):
        super().setUp()
        self.feed = ChangeFeed()
        self.database = Database(self.root / "operations.sqlite3", assembly=host_assembly(self.feed))
        self.database.initialize_target()
        self.addCleanup(self.database.close)
        self.layer = AgenticLayer.initialize(self.root / "agentic", self.root / "agentic-state", git_user=IDENTITY,
                                             attribution_source="host_authenticated")
        with self.database.transaction() as uow:
            for agent, role in ((ORDUBASHY, "ordubashy"), (APPLIER, "learning-applier")):
                uow.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) "
                                       "VALUES(?,'agent',?,'now','now')", (agent, role))
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,profile_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'now','now')", (agent, agent, role, f"/w/{agent}", json.dumps({"role": {"id": role}})))

    def start(self, **arguments):
        """One start: find the folders, then record them."""
        found = registry.ExtensionRegistry(self.discover(**arguments), changes=self.feed)
        found.sync(self.database, self.layer)
        return found

    def row(self, ext_id="network"):
        with self.database.read() as uow:
            return registry.ExtensionRegistry._rows(uow.connection).get(ext_id)

    def read(self, call):
        with self.database.read() as uow:
            return call(uow.connection)

    def write(self, call):
        with self.database.transaction() as uow:
            return call(uow)

    def keyed(self, folder):
        """The example with a set-up step, switched on for Ordubashy once set up."""
        self.manifest(folder, lambda data: data.update(
            setup={"kind": "key", "fields": [{"id": "token", "label": "Token", "secret": True}]},
            agents={"allowed": ["ordubashy"], "on_after_setup": ["ordubashy"]}))


class RegistryTest(StoreCase):
    def test_a_fresh_store_has_the_table_and_it_is_classified(self):
        self.assertEqual(self.read(lambda connection: connection.execute("SELECT count(*) FROM extensions").fetchone()[0]), 0)
        self.assertEqual(catalog().classifications["extensions"][1].kind, TECHNICAL_EXCLUDED)

    def test_an_extension_without_set_up_is_ready_once_installed_and_on_for_nobody(self):
        self.place()
        found = self.start()
        row = self.row()
        self.assertEqual((row["status"], row["switches_json"]), ("ready", {}))
        [entry] = found.list()
        self.assertEqual((entry.id, entry.delivery, entry.version, entry.status, entry.agents, entry.package_id),
                         ("network", "downloadable", "1.0.0", "ready", (), "network"))
        self.assertEqual(found.get("network"), entry)
        with self.assertRaises(DomainError) as refusal:
            found.get("missing")
        self.assertEqual(refusal.exception.code, "not_found")
        self.assertEqual(self.read(lambda connection: found.on_for(ORDUBASHY, connection=connection)), ())
        self.assertFalse(found.active("network"))

    def test_switch_checks_the_role_and_on_for_needs_ready(self):
        self.keyed(self.place())
        self.manifest(self.downloaded / "network", lambda data: data["agents"].pop("on_after_setup"))
        found = self.start()
        self.assertEqual((self.row()["status"], self.row()["switches_json"]), ("not_set_up", {}))
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: found.switch("network", APPLIER, True, uow=uow))
        self.assertEqual(refusal.exception.code, "forbidden")
        self.assertEqual(refusal.exception.message,
                         "The extension 'network' can be switched on only for agents of the role ordubashy.")
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: found.switch("network", "nobody", True, uow=uow))
        self.assertEqual(refusal.exception.code, "not_found")
        # Switched on, but not set up: in effect for nobody, and listed as the agent it will be on for.
        self.assertFalse(self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow)))
        self.assertEqual(self.row()["switches_json"], {ORDUBASHY: True})
        self.assertEqual(found.get("network").agents, (ORDUBASHY,))
        self.assertEqual(self.read(lambda c: found.on_for(ORDUBASHY, connection=c)), ())
        self.assertFalse(self.read(lambda c: found.is_on("network", ORDUBASHY, connection=c)))
        self.assertFalse(self.read(lambda c: found.active("network", connection=c)))
        self.write(lambda uow: found.set_status("network", "ready", uow=uow))
        self.assertEqual(self.read(lambda c: found.on_for(ORDUBASHY, connection=c)), ("network",))
        self.assertEqual(found.on_for(ORDUBASHY, role="ordubashy"), ("network",))
        self.assertEqual(found.in_effect(ORDUBASHY), (("network", "1.0.0", self.row()["package_revision"]),))
        self.assertTrue(self.read(lambda c: found.is_on("network", ORDUBASHY, connection=c)))
        self.assertTrue(self.read(lambda c: found.active("network", connection=c)))
        self.assertFalse(self.write(lambda uow: found.switch("network", ORDUBASHY, False, uow=uow)))
        self.assertFalse(self.read(lambda c: found.active("network", connection=c)))
        self.assertEqual(found.get("network").agents, ())
        # Switching off needs no allowed role.
        self.assertFalse(self.write(lambda uow: found.switch("network", APPLIER, False, uow=uow)))
        self.assertEqual(self.row()["switches_json"], {ORDUBASHY: False, APPLIER: False})

    def test_with_no_switch_the_roles_for_after_set_up_have_it_once_ready(self):
        self.keyed(self.place())
        found = self.start()
        # Before it is ready it is in effect for nobody, but listed for Ordubashy, whose role has it after set-up.
        self.assertEqual((found.get("network").agents, found.on_for(ORDUBASHY)), ((ORDUBASHY,), ()))
        self.write(lambda uow: found.set_status("network", "setting_up", "Waiting for the token.", uow=uow))
        self.assertEqual((found.get("network").status, found.get("network").note), ("setting_up", "Waiting for the token."))
        self.write(lambda uow: found.set_status("network", "ready", uow=uow))
        self.assertEqual(self.row()["switches_json"], {})
        self.assertEqual((found.on_for(ORDUBASHY), found.on_for(APPLIER)), (("network",), ()))
        # A switch somebody made wins over the role, through a set-up that fails and succeeds again.
        self.write(lambda uow: found.switch("network", ORDUBASHY, False, uow=uow))
        self.write(lambda uow: found.set_status("network", "needs_attention", "The token was refused.", uow=uow))
        self.assertEqual(found.on_for(ORDUBASHY), ())
        self.write(lambda uow: found.set_status("network", "ready", uow=uow))
        self.assertEqual(found.on_for(ORDUBASHY), ())
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: found.set_status("network", "unsupported", uow=uow))
        self.assertEqual(refusal.exception.code, "validation_error")
        with self.assertRaises(DomainError):
            self.write(lambda uow: found.set_status("missing", "ready", uow=uow))

    def test_only_agents_that_exist_are_listed(self):
        self.keyed(self.place())
        found = self.start()
        self.write(lambda uow: found.set_status("network", "ready", uow=uow))
        self.assertTrue(found.active("network"))
        self.write(lambda uow: uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?",
                                                      (ORDUBASHY,)))
        self.assertEqual(found.get("network").agents, ())
        self.assertFalse(found.active("network"))
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        self.assertEqual(refusal.exception.code, "not_found")

    def test_the_revision_changes_with_every_change(self):
        self.keyed(self.place())
        found = self.start()
        seen = [self.read(lambda c: found.revision(connection=c))]
        for change in (lambda uow: found.switch("network", ORDUBASHY, True, uow=uow),
                       lambda uow: found.set_status("network", "setting_up", uow=uow),
                       lambda uow: found.set_status("network", "setting_up", "A note.", uow=uow),
                       lambda uow: found.set_status("network", "ready", "A note.", uow=uow)):
            self.write(change)
            seen.append(self.read(lambda c: found.revision(connection=c)))
        self.assertEqual(len(set(seen)), len(seen), seen)
        self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        self.assertEqual(self.read(lambda c: found.revision(connection=c)), seen[-1])

    def test_subscribers_hear_after_the_commit_which_agents_changed(self):
        self.keyed(self.place())
        found = self.start()
        heard = []
        found.subscribe(lambda changes: heard.extend(changes))

        def switch(uow):
            found.switch("network", ORDUBASHY, True, uow=uow)
            self.assertEqual(heard, [])
        self.write(switch)
        # Not ready yet: the switch is recorded, but what Ordubashy has in effect is the same.
        self.assertEqual(heard, [registry.Change("network", "switch", ORDUBASHY, ())])
        self.write(lambda uow: found.set_status("network", "ready", uow=uow))
        self.assertEqual(heard[1:], [registry.Change("network", "status", None, (ORDUBASHY,))])
        self.write(lambda uow: found.set_status("network", "ready", "A note.", uow=uow))
        self.assertEqual(heard[2:], [registry.Change("network", "status", None, ())])
        self.write(lambda uow: found.switch("network", ORDUBASHY, False, uow=uow))
        self.assertEqual(heard[3:], [registry.Change("network", "switch", ORDUBASHY, (ORDUBASHY,))])

    def test_a_refused_or_unsupported_extension_cannot_be_switched_and_is_in_effect_for_nobody(self):
        folder = self.place()
        found = self.start()
        self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        # The next start runs on a build the extension does not fit; its row and switch stay.
        later = self.start(app_version="1.6.1")
        [entry] = later.list()
        self.assertEqual((entry.status, entry.note, entry.agents), ("unsupported", "It needs Qorqbot >=1.7.0, and this is "
                                                                    "Qorqbot 1.6.1.", ()))
        self.assertEqual(self.read(lambda c: later.on_for(ORDUBASHY, connection=c)), ())
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: later.switch("network", ORDUBASHY, False, uow=uow))
        self.assertEqual(refusal.exception.code, "invalid_transition")
        (folder / "README.md").unlink()
        broken = self.start()
        [entry] = broken.list()
        self.assertEqual((entry.status, entry.note), ("needs_attention", "README.md is missing at the top of the folder."))
        self.assertIsNone(broken.plugin_file("network", "hooks.yaml"))
        self.assertEqual(self.row()["switches_json"], {ORDUBASHY: True})


class PluginPartTest(StoreCase):
    def layer_file(self, relative):
        return (self.layer.root / "plugins" / "network" / relative)

    def subjects(self, path="plugins/network"):
        return [commit["subject"] for commit in self.layer.history.log(paths=[path])["commits"]]

    def bump(self, folder, version, skill=None):
        self.manifest(folder, lambda data: data.update(version=version))
        if skill is not None:
            path = folder / "plugin" / "skills" / "network" / "SKILL.md"
            path.write_text(path.read_text() + skill)

    def test_install_copies_the_plugin_part_as_a_package(self):
        self.place()
        found = self.start()
        self.assertTrue(self.layer.catalog.validate()["valid"])
        entry = self.layer.catalog.read_entry("network/network")
        self.assertEqual(entry["entry"]["kind"], "skill")
        listed = {item["ref"] for item in self.layer.catalog.list(package_ids=["network"])["entries"]}
        self.assertEqual(listed, {"network/network"})
        hooks = self.layer.catalog.read_resource("network", "hooks.yaml")
        self.assertEqual(hooks["content"], (EXAMPLE / "plugin" / "hooks.yaml").read_text())
        self.assertEqual(found.plugin_file("network", "records.yaml"), (EXAMPLE / "plugin" / "records.yaml").read_bytes())
        self.assertEqual(found.plugin_file("network", "instructions/weekly-review.md"),
                         (EXAMPLE / "plugin" / "instructions" / "weekly-review.md").read_bytes())
        self.assertIsNone(found.plugin_file("network", "missing.md"))
        self.assertEqual(found.package_ids(), frozenset({"network"}))
        self.assertEqual(self.subjects("plugins/network/hooks.yaml"), ["Install extension network 1.0.0"])
        self.assertTrue(self.layer.history.status()["clean"])
        # The stored revision is the layer's own: removing at it is accepted without a conflict.
        row = self.row()
        self.layer.authoring.remove_package({"actor_id": "test", "actor_kind": "system", "reason": "check"}, "network",
                                            row["package_revision"], dry_run=True)
        snap = extension_package.snapshot(EXAMPLE, found.get("network").manifest)
        self.assertEqual(row["source_digest"], extension_package.digest(snap))

    def test_the_same_folder_leaves_edits_made_in_the_layer(self):
        self.place()
        self.start()
        self.layer_file("skills/network/SKILL.md").write_text(self.layer_file("skills/network/SKILL.md").read_text()
                                                              + "\nAn approved change.\n")
        before = self.row()
        self.start()
        self.assertIn("An approved change.", self.layer_file("skills/network/SKILL.md").read_text())
        self.assertEqual(self.subjects(), ["Install extension network 1.0.0"])
        self.assertEqual(self.row(), before)

    def test_an_update_commits_layer_edits_and_then_replaces_them(self):
        folder = self.place()
        self.start()
        self.layer_file("skills/network/SKILL.md").write_text("---\nname: qorqut-plugin-network\ndescription: Edited.\n"
                                                              "read_for: Edited\n---\nAn approved change.\n")
        self.bump(folder, "1.0.1", "\nNew text of 1.0.1.\n")
        (folder / "plugin" / "instructions" / "weekly-review.md").unlink()
        self.yaml_drop_weekly(folder)
        self.start()
        self.assertEqual(self.subjects(), ["Update extension network to 1.0.1", "Text of extension network before update "
                                           "to 1.0.1", "Install extension network 1.0.0"])
        self.assertEqual(self.layer_file("skills/network/SKILL.md").read_text(),
                         (folder / "plugin" / "skills" / "network" / "SKILL.md").read_text())
        self.assertFalse(self.layer_file("instructions/weekly-review.md").exists())
        kept = self.layer.history.log(paths=["plugins/network/skills/network/SKILL.md"])["commits"][1]["commit"]
        self.assertIn("An approved change.", self.layer.history.git.text(
            ["show", f"{kept}:plugins/network/skills/network/SKILL.md"]))
        self.assertEqual(self.row()["source_digest"], extension_package.digest(
            extension_package.snapshot(folder, self.discover()[0].manifest)))
        self.assertTrue(self.layer.history.status()["clean"])

    def yaml_drop_weekly(self, folder):
        path = folder / "plugin" / "hooks.yaml"
        data = [entry for entry in yaml.safe_load(path.read_text()) if entry["id"] != "weekly-review"]
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def test_an_update_over_a_committed_layer_edit_replaces_it(self):
        folder = self.place()
        self.start()
        self.layer_file("records.yaml").write_text(self.layer_file("records.yaml").read_text() + "\n")
        self.layer.history.commit("An approved change", paths=["plugins/network"], author=IDENTITY)
        self.bump(folder, "1.0.1")
        self.start()
        self.assertEqual(self.subjects()[:2], ["Update extension network to 1.0.1", "An approved change"])
        self.assertEqual(self.layer_file("records.yaml").read_bytes(), (folder / "plugin" / "records.yaml").read_bytes())

    def test_removal_commits_and_removes_and_forgets_the_row(self):
        folder = self.place()
        found = self.start()
        self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        self.layer_file("hooks.yaml").write_text("[]\n")
        shutil.rmtree(folder)
        later = self.start()
        self.assertEqual(later.list(), [])
        self.assertIsNone(self.row())
        self.assertEqual(later.package_ids(), frozenset())
        with self.assertRaises(NotFoundError):
            self.layer.catalog.read_resource("network", "hooks.yaml")
        self.assertEqual(self.subjects()[:2], ["Remove extension network", "Text of extension network before removal"])
        self.assertTrue(self.layer.history.status()["clean"])
        # Installed again, it starts with no switches.
        self.place()
        self.start()
        self.assertEqual((self.row()["status"], self.row()["switches_json"]), ("ready", {}))

    def crash(self, found):
        """A start that stops after the layer was written and before the store is."""
        with mock.patch.object(self.database, "transaction", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                found.sync(self.database, self.layer)

    def test_a_start_stopped_between_the_layer_copy_and_the_row_is_put_right_by_the_next(self):
        self.place()
        self.crash(registry.ExtensionRegistry(self.discover(), changes=self.feed))
        self.assertIsNone(self.row())
        self.assertEqual(self.subjects("plugins/network/hooks.yaml"), ["Install extension network 1.0.0"])
        found = self.start()
        self.assertEqual(found.get("network").status, "ready")
        self.assertEqual(self.row()["source_digest"], extension_package.digest(
            extension_package.snapshot(EXAMPLE, found.get("network").manifest)))
        self.assertTrue(self.layer.history.status()["clean"])
        self.layer.authoring.remove_package({"actor_id": "test", "actor_kind": "system", "reason": "check"}, "network",
                                            self.row()["package_revision"], dry_run=True)

    def test_a_start_stopped_between_the_removal_and_the_row_delete_is_put_right_by_the_next(self):
        folder = self.place()
        self.start()
        shutil.rmtree(folder)
        self.crash(registry.ExtensionRegistry(self.discover(), changes=self.feed))
        self.assertIsNotNone(self.row())
        with self.assertRaises(NotFoundError):
            self.layer.catalog.read_resource("network", "hooks.yaml")
        later = self.start()
        self.assertIsNone(self.row())
        self.assertEqual(later.list(), [])

    def test_a_removal_the_layer_refuses_keeps_the_row_and_the_start_goes_on(self):
        folder = self.place()
        self.start()
        shutil.rmtree(folder)
        refusal = AgenticError("the layer refused it", code="invalid_request")
        with mock.patch.object(self.layer.authoring, "remove_package", side_effect=refusal):
            found = self.start()
        [entry] = found.list()
        self.assertEqual((entry.id, entry.status, entry.note, entry.folder),
                         ("network", "needs_attention", "Its folder is gone, but its plugin part could not be removed "
                                                        "from the agentic layer: the layer refused it", None))
        self.assertEqual(found.get("network").status, "needs_attention")
        self.assertIsNotNone(self.row())
        self.assertEqual(found.package_ids(), frozenset({"network"}))
        self.assertEqual(found.on_for(ORDUBASHY), ())
        # The next start removes it.
        self.assertEqual(self.start().list(), [])
        self.assertIsNone(self.row())

    def test_with_no_extension_a_start_writes_nothing(self):
        history = self.layer.history.log()["commits"]
        with mock.patch.object(self.database, "transaction", side_effect=AssertionError("a write")):
            found = self.start()
        self.assertEqual((found.list(), found.package_ids(), found.in_effect(ORDUBASHY)), ([], frozenset(), ()))
        self.assertEqual(self.layer.history.log()["commits"], history)

    def test_a_start_refuses_a_layer_copy_of_hooks_or_records_the_readers_refuse(self):
        folder = self.place()
        found = self.start()
        self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        for name in ("hooks.yaml", "records.yaml"):
            original = self.layer_file(name).read_text()
            self.layer_file(name).write_text("schema_version: 2\n")
            later = self.start()
            _, findings = (registry.contract.read_hooks if name == "hooks.yaml" else registry.contract.read_record_types)(
                lambda path: self.layer_file(path).read_bytes() if self.layer_file(path).is_file() else None,
                extension_id="network")
            self.assertTrue(findings)
            entry = later.get("network")
            self.assertEqual((entry.status, entry.note), (
                "needs_attention", f"Its plugin part in the agentic layer cannot be used: {findings[0]}"))
            self.assertEqual((later.on_for(ORDUBASHY), later.on_for(APPLIER)), ((), ()))
            self.layer_file(name).write_text(original)
            self.assertEqual(self.start().on_for(ORDUBASHY), ("network",))
        self.assertTrue(folder.is_dir())

    def test_a_package_id_taken_by_something_else_is_refused(self):
        self.layer.authoring.import_package(
            {"actor_id": "test", "actor_kind": "system", "reason": "another package"}, "network",
            {"files": [{"path": "agentic.yaml", "content": "schema_version: 1\nid: network\nname: Other\n"
                                                           "description: Not an extension.\nentries: []\n"}]})
        self.place()
        found = self.start()
        [entry] = found.list()
        self.assertEqual(entry.status, "needs_attention")
        self.assertEqual(entry.note, "The agentic layer already has a package 'network' that was not made from this "
                                     "extension.")
        self.assertIsNone(self.row())
        self.assertEqual(found.package_ids(), frozenset())
        self.assertEqual(self.layer.catalog.read_resource("network", "agentic.yaml")["content"].split("\n")[2], "name: Other")
        with self.assertRaises(DomainError) as refusal:
            self.write(lambda uow: found.switch("network", ORDUBASHY, True, uow=uow))
        self.assertEqual(refusal.exception.code, "invalid_transition")

    def test_a_plugin_part_that_is_not_accepted_is_refused(self):
        folder = self.place()
        workflow = folder / "plugin" / "workflows" / "first-use.yaml"
        workflow.parent.mkdir()
        workflow.write_text(yaml.safe_dump({
            "schema_version": 1, "id": "other-name", "description": "Set up the first Connections.",
            "steps": [{"id": "start", "instructions": "Ask the owner who matters most."}]}))
        found = self.start()
        [entry] = found.list()
        self.assertEqual(entry.status, "needs_attention")
        # The checker or, failing it, the layer refuses it.
        self.assertIn("'other-name'", entry.note)
        self.assertIsNone(self.row())
        self.assertTrue(self.layer.catalog.validate()["valid"])
        # Named after its id, the workflow becomes an entry.
        workflow.write_text(workflow.read_text().replace("other-name", "first-use"))
        self.start()
        refs = {item["ref"] for item in self.layer.catalog.list(package_ids=["network"])["entries"]}
        self.assertEqual(refs, {"network/network", "network/first-use"})

    def test_a_fault_only_the_layer_finds_is_refused(self):
        # The checker loads the package in an empty layer itself, so no folder fault is left that only the real layer
        # finds; the layer's own refusal is stood in for.
        self.place()
        refused = AgenticError("the layer refused it", code="invalid_request")
        with mock.patch.object(self.layer.authoring, "import_package", side_effect=refused):
            found = self.start()
        [entry] = found.list()
        self.assertEqual((entry.status, entry.note), ("needs_attention", "The plugin part could not be copied into the "
                                                                         "agentic layer: the layer refused it"))
        self.assertIsNone(self.row())
        # Its package name stays out of every selection but those it is put in effect for.
        self.assertEqual(found.package_ids(), frozenset({"network"}))
        self.assertEqual(self.start().get("network").status, "ready")

    def test_a_symbolic_link_in_the_plugin_part_is_refused(self):
        folder = self.place()
        manifest = self.one().manifest
        (folder / "plugin" / "instructions" / "linked.md").symlink_to(folder / "README.md")
        with self.assertRaises(registry.contract.ContractError) as refusal:
            extension_package.snapshot(folder, manifest)
        self.assertEqual(str(refusal.exception), "The plugin part holds a symbolic link: plugin/instructions/linked.md.")
        (folder / "plugin" / "instructions" / "linked.md").unlink()
        shutil.move(folder / "plugin" / "instructions", folder / "elsewhere")
        (folder / "plugin" / "instructions").symlink_to(folder / "elsewhere")
        with self.assertRaises(registry.contract.ContractError):
            extension_package.snapshot(folder, manifest)

    def test_an_id_with_an_underscore_goes_through(self):
        folder = self.place(name="two_words")
        self.manifest(folder, lambda data: data.update(id="two_words"))
        shutil.move(folder / "plugin" / "skills" / "network", folder / "plugin" / "skills" / "two_words")
        skill = folder / "plugin" / "skills" / "two_words" / "SKILL.md"
        skill.write_text(skill.read_text().replace("qorqut-plugin-network", "qorqut-plugin-two_words"))
        self.assertTrue(self.one().fine)
        found = self.start()
        [entry] = found.list()
        self.assertEqual((entry.status, entry.note, entry.package_id), ("ready", None, "two_words"))
        listed = {item["ref"] for item in self.layer.catalog.list(package_ids=["two_words"])["entries"]}
        self.assertEqual(listed, {"two_words/two_words"})


class AgentTest(StoreCase):
    """What an agent gets from the extensions in effect for it: its catalog selection, its skills, its configuration's
    fingerprint and a restart of its open session."""

    def setUp(self):
        super().setUp()
        self.place()
        self.found = self.start()
        self.view = HostAgenticView.__new__(HostAgenticView)
        self.view._layer, self.view._packages = self.layer, {}
        self.view.extensions = self.found

    def on(self, agent=ORDUBASHY, on=True):
        return self.write(lambda uow: self.found.switch("network", agent, on, uow=uow))

    def profile(self, agent, role):
        return SimpleNamespace(agent_id=agent, data={"role": {"id": role}, "plugins": []},
                               role={"id": role}, scope=SimpleNamespace(global_capabilities=("read_global_context",)))

    def selected(self, agent, role):
        return "network" in self.view.selection({"agent_id": agent, "role": {"id": role}})["package_ids"]

    def skills(self, agent, role, taken=()):
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver.extensions = self.found
        in_effect = self.found.in_effect(agent, role=role)
        selection = self.view.selection({"agent_id": agent, "role": {"id": role}}, in_effect=in_effect)
        return resolver._extension_skills(self.view, selection, in_effect, taken=taken)

    def test_the_package_is_selected_only_for_an_agent_it_is_in_effect_for_and_for_the_learning_applier(self):
        self.assertFalse(self.selected(ORDUBASHY, "ordubashy"))
        self.assertTrue(self.selected(APPLIER, "learning-applier"))
        self.on()
        self.assertTrue(self.selected(ORDUBASHY, "ordubashy"))
        # The role is compared exactly with the profile's role id.
        self.assertFalse(self.selected(ORDUBASHY, "Ordubashy"))
        self.on(on=False)
        self.assertFalse(self.selected(ORDUBASHY, "ordubashy"))

    def test_the_skill_reaches_only_an_agent_it_is_in_effect_for(self):
        self.assertEqual(self.skills(ORDUBASHY, "ordubashy"), [])
        self.on()
        [(item, files)] = self.skills(ORDUBASHY, "ordubashy")
        self.assertEqual((item["name"], item["owner"], item["version"], item["digest"]),
                         ("qorqut-plugin-network", {"kind": "extension", "id": "network"}, "1.0.0",
                          self.row()["package_revision"]))
        self.assertEqual(item["generated_path"], ".claude/skills/qorqut-plugin-network/SKILL.md")
        self.assertEqual(files, {"SKILL.md": (EXAMPLE / "plugin" / "skills" / "network" / "SKILL.md").read_bytes()})
        # The Learning Applier reads the package but gets no skill from it.
        self.assertEqual(self.skills(APPLIER, "learning-applier"), [])

    def test_a_skill_edited_against_the_file_rules_is_refused(self):
        self.on()
        path = self.layer.root / "plugins" / "network" / "skills" / "network" / "SKILL.md"
        path.write_text(path.read_text().replace("name: qorqut-plugin-network", "name: other"))
        heard = []
        self.found.subscribe(heard.extend)
        before = self.row()
        # The composition is asked to retry; the extension alone is out, for every agent, and nothing is written.
        with self.assertRaises(DomainError) as refusal:
            self.skills(ORDUBASHY, "ordubashy")
        self.assertEqual((refusal.exception.code, refusal.exception.retryable), ("agentic_revision_conflict", True))
        entry = self.found.get("network")
        self.assertEqual(entry.status, "needs_attention")
        self.assertIn("Skill metadata must name qorqut-plugin-network", entry.note)
        self.assertEqual(self.skills(ORDUBASHY, "ordubashy"), [])
        self.assertFalse(self.selected(ORDUBASHY, "ordubashy"))
        self.assertFalse(self.selected(APPLIER, "learning-applier"))
        self.assertEqual(self.row(), before)
        self.assertEqual(heard, [registry.Change("network", "plugin", None, (ORDUBASHY,))])

    def test_the_fingerprint_changes_with_a_switch(self):
        stored = self.root / "role"
        stored.mkdir()
        with self.database.transaction() as uow:
            uow.connection.execute("INSERT INTO artifacts(kind,artifact_id,version,digest,stored_path,created_at) "
                                   "VALUES('role','ordubashy','1','sha256:x',?,'now')", (str(stored),))
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver.database, resolver.extensions = self.database, self.found
        resolver.registry = SimpleNamespace(installed=lambda: [], frozen=False)
        resolver._lock, resolver._fingerprints = threading.RLock(), OrderedDict()
        profile = self.profile(ORDUBASHY, "ordubashy")
        profile.data["role"] = {"id": "ordubashy", "version": "1", "digest": "sha256:x"}
        resolver.profile = lambda principal, connection=None: (profile, {"workspace": "/w", "profile_json": "{}"})

        def fingerprint():
            principal = Principal("6f1a1f6e-6a1b-4a1e-9a2e-2f4b1f0e0009", "agent")
            return resolver._snapshot(principal, None, agentic_revision="r1")["fingerprint"]
        before = fingerprint()
        self.on()
        switched = fingerprint()
        self.assertNotEqual(switched, before)
        self.on(on=False)
        self.assertEqual(fingerprint(), before)

    def test_changes_at_start_tell_whom_they_change_for(self):
        heard = []

        def start(**arguments):
            found = registry.ExtensionRegistry(self.discover(**arguments), changes=self.feed)
            found.subscribe(heard.extend)
            found.sync(self.database, self.layer)
            return found
        folder = self.downloaded / "network"
        self.on()
        # The same folder again: nothing changed for anybody.
        start()
        self.assertEqual(heard, [])
        self.bump(folder)
        start()
        self.assertEqual(heard, [registry.Change("network", "updated", None, (ORDUBASHY,))])
        start(app_version="1.6.1")
        self.assertEqual(heard[1:], [registry.Change("network", "plugin", None, (ORDUBASHY,))])
        start()
        self.assertEqual(heard[2:], [registry.Change("network", "plugin", None, (ORDUBASHY,))])
        shutil.rmtree(folder)
        start()
        self.assertEqual(heard[3:], [registry.Change("network", "plugin", None, (ORDUBASHY,))])
        # Installed again with Ordubashy's role on after set-up: on for him at once, in one change.
        self.place()
        self.manifest(folder, lambda data: data["agents"].update(on_after_setup=["ordubashy"]))
        start()
        self.assertEqual(heard[4:], [registry.Change("network", "plugin", None, (ORDUBASHY,))])

    def bump(self, folder):
        self.manifest(folder, lambda data: data.update(version="1.0.1"))
        path = folder / "plugin" / "skills" / "network" / "SKILL.md"
        path.write_text(path.read_text() + "\nNew text of 1.0.1.\n")

    def test_restarts_are_held_until_released_and_say_what_happens(self):
        from qorqut.native_chat import extension_restart
        queued = []
        managed = SimpleNamespace(handle=lambda agent_id: object() if agent_id == ORDUBASHY else None)
        chat = SimpleNamespace(managed=managed, opener=None, database=self.database, runtime=SimpleNamespace(paths=self.paths))
        restarts = extension_restart.ExtensionRestarts(chat, self.found, start=queued.append, held=True)
        self.on()
        self.assertEqual(queued, [])
        restarts.release()
        self.assertEqual(len(queued), 1)
        self.on(on=False)
        self.assertEqual(len(queued), 2)
        with mock.patch.object(extension_restart.mode, "is_terminal", return_value=False):
            self.assertEqual([restarts.expected(agent, True) for agent in (ORDUBASHY, APPLIER)], ["restarted", "not_open"])
            self.assertEqual(restarts.expected(ORDUBASHY, False), "unchanged")
        with mock.patch.object(extension_restart.mode, "is_terminal", return_value=True):
            self.assertEqual(restarts.expected(ORDUBASHY, True), "terminal")
            with self.assertLogs("qorqut.native_chat.extension_restart", "INFO") as logged:
                self.assertEqual(restarts.restart({ORDUBASHY: [("network", "status")]}), {ORDUBASHY: "terminal"})
        self.assertIn(extension_restart.SESSION["terminal"], logged.output[0])

    def test_a_committed_switch_queues_one_restart_for_an_agent_with_an_open_session_only(self):
        from qorqut.native_chat import extension_restart
        other = "agent-other"
        with self.database.transaction() as uow:
            uow.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) "
                                   "VALUES(?,'agent','other','now','now')", (other,))
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,profile_json,created_at,updated_at) "
                "VALUES(?,?,'other','/w/other',?,'now','now')", (other, other, json.dumps({"role": {"id": "ordubashy"}})))
        restarted, queued = [], []
        opener = SimpleNamespace(restart=lambda profile, text, key: restarted.append((profile["agent_id"], text)))
        managed = SimpleNamespace(handle=lambda agent_id: object() if agent_id == ORDUBASHY else None)
        chat = SimpleNamespace(managed=managed, opener=opener, database=self.database,
                               runtime=SimpleNamespace(paths=self.paths))
        extension_restart.ExtensionRestarts(chat, self.found, start=queued.append)
        with mock.patch.object(extension_restart.mode, "is_terminal", return_value=False), \
                mock.patch.object(extension_restart.store, "active_profile",
                                  side_effect=lambda connection, agent_id, paths: {"agent_id": agent_id}):
            def switch(uow):
                self.found.switch("network", ORDUBASHY, True, uow=uow)
                self.assertEqual(queued, [])
            self.write(switch)
            self.assertEqual((len(queued), restarted), (1, []))
            self.assertEqual(queued.pop()(), {ORDUBASHY: "restarted"})
            self.assertEqual(restarted, [(ORDUBASHY, extension_restart.NOTICE.format(
                changes="The extension Network was switched on for you."))])
            self.on(other)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued.pop()(), {other: "not_open"})
            self.assertEqual(len(restarted), 1)
            # A switch that changes nothing in effect queues nothing.
            self.on(other, on=True)
            self.assertEqual(queued, [])
            # A failed restart is logged and not tried again.
            opener.restart = mock.Mock(side_effect=DomainError("agent_off", "This agent is turned off"))
            self.on(on=False)
            with self.assertLogs("qorqut.native_chat.extension_restart", "INFO"):
                queued.pop()()
            self.assertEqual((opener.restart.call_count, queued), (1, []))


    def restarts(self, extensions, **arguments):
        """Restarts with every agent's session open and the work run at once; returns the notices sent, by agent."""
        from qorqut.native_chat import extension_restart
        sent = {}
        opener = SimpleNamespace(restart=lambda profile, text, key: sent.setdefault(profile["agent_id"], []).append(text))
        chat = SimpleNamespace(managed=SimpleNamespace(handle=lambda agent_id: object()), opener=opener,
                               database=self.database, runtime=SimpleNamespace(paths=self.paths))
        extension_restart.ExtensionRestarts(chat, extensions, start=lambda work: work(), **arguments)
        for patch in (mock.patch.object(extension_restart.mode, "is_terminal", return_value=False),
                      mock.patch.object(extension_restart.store, "active_profile",
                                        side_effect=lambda connection, agent_id, paths: {"agent_id": agent_id})):
            patch.start()
            self.addCleanup(patch.stop)
        return sent

    def notice(self, *sentences):
        from qorqut.native_chat.extension_restart import NOTICE
        return NOTICE.format(changes=" ".join(sentences))

    def test_a_status_change_into_or_out_of_ready_restarts_the_agents_it_is_on_for(self):
        sent = self.restarts(self.found)
        self.on()
        sent.clear()
        self.write(lambda uow: self.found.set_status("network", "not_set_up", uow=uow))
        self.assertEqual(sent, {ORDUBASHY: [self.notice("The extension Network is no longer in effect for you.")]})
        # A change that keeps it out of ready restarts nobody.
        self.write(lambda uow: self.found.set_status("network", "setting_up", uow=uow))
        self.assertEqual(len(sent[ORDUBASHY]), 1)
        self.write(lambda uow: self.found.set_status("network", "ready", uow=uow))
        self.assertEqual(sent, {ORDUBASHY: [self.notice("The extension Network is no longer in effect for you."),
                                            self.notice("The extension Network is now in effect for you.")]})

    def test_a_change_at_start_restarts_the_open_sessions_once_released(self):
        from qorqut.native_chat import extension_restart
        self.on()
        folder = self.downloaded / "network"

        def start(sentence, **arguments):
            """One start with restarts held until Chat has started; Ordubashy's session gets one notice."""
            found = registry.ExtensionRegistry(self.discover(**arguments), changes=self.feed)
            sent = self.restarts(found, held=True)
            found.sync(self.database, self.layer)
            self.assertEqual(sent, {})
            [restarts] = [callback.__self__ for callback in found._subscribers]
            self.assertIsInstance(restarts, extension_restart.ExtensionRestarts)
            restarts.release()
            self.assertEqual(sent, {ORDUBASHY: [self.notice(sentence)]} if sentence else {}, sentence)
        start("The extension Network was taken out of use because it cannot run on this computer.", app_version="1.6.1")
        start("The extension Network is now in effect for you.")
        self.bump(folder)
        start("The extension Network was updated.")
        self.manifest(folder, lambda data: data.update(version="1.0.2"))
        with mock.patch.object(self.layer.authoring, "import_package",
                               side_effect=AgenticError("the layer refused it", code="invalid_request")):
            start("The extension Network was taken out of use because it needs attention.")
        # The refused version is not recorded, so the next start copies it and has it in effect again.
        start("The extension Network is now in effect for you.")
        shutil.rmtree(folder)
        start("The extension network was removed.")
        start(None)

    def test_a_switch_in_a_unit_that_is_rolled_back_restarts_nobody(self):
        heard = []
        self.found.subscribe(heard.extend)
        with self.assertRaises(RuntimeError):
            with self.database.transaction() as uow:
                self.found.switch("network", ORDUBASHY, True, uow=uow)
                raise RuntimeError("rolled back")
        self.write(lambda uow: uow.connection.execute("UPDATE agent_profiles SET name=name"))
        self.write(lambda uow: self.found.set_status("network", "ready", uow=uow))
        self.assertEqual(heard, [])
        self.assertFalse(self.found.is_on("network", ORDUBASHY))

    def test_a_skill_name_taken_by_another_skill_takes_the_extension_out(self):
        self.on()
        heard = []
        self.found.subscribe(heard.extend)
        with self.assertRaises(DomainError) as refusal:
            self.skills(ORDUBASHY, "ordubashy", taken={"qorqut-plugin-network": {}})
        self.assertEqual((refusal.exception.code, refusal.exception.retryable), ("agentic_revision_conflict", True))
        self.assertEqual(self.found.get("network").note, "A skill of its plugin part in the agentic layer cannot be used: "
                                                         "another skill is already named qorqut-plugin-network")
        self.assertEqual(self.skills(ORDUBASHY, "ordubashy", taken={"qorqut-plugin-network": {}}), [])
        self.assertEqual(heard, [registry.Change("network", "plugin", None, (ORDUBASHY,))])

    def test_a_start_refuses_a_layer_copy_the_composer_would_refuse(self):
        self.on()
        path = self.layer.root / "plugins" / "network" / "skills" / "network" / "SKILL.md"
        path.write_text(path.read_text().replace("name: qorqut-plugin-network", "name: other"))
        heard = []
        found = registry.ExtensionRegistry(self.discover(), changes=self.feed)
        found.view = self.view
        found.subscribe(heard.extend)
        found.sync(self.database, self.layer)
        entry = found.get("network")
        self.assertEqual(entry.status, "needs_attention")
        self.assertIn("Skill metadata must name qorqut-plugin-network", entry.note)
        self.assertEqual(found.on_for(ORDUBASHY), ())
        self.assertEqual(heard, [registry.Change("network", "plugin", None, (ORDUBASHY,))])
        # The row keeps the switch: put right, the next start has it in effect again.
        path.write_text(path.read_text().replace("name: other", "name: qorqut-plugin-network"))
        found = registry.ExtensionRegistry(self.discover(), changes=self.feed)
        found.view = self.view
        found.sync(self.database, self.layer)
        self.assertEqual(found.on_for(ORDUBASHY), ("network",))


class OperationsTest(Case):
    """The owner's two operations, through the application on a small runtime."""

    def setUp(self):
        super().setUp()
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from private_thread_runtime import build_runtime, invoke
        self.invoke = invoke
        self.runtime = build_runtime(self.root)
        self.addCleanup(self.runtime.database.close)
        layer = AgenticLayer.initialize(self.root / "agentic", self.root / "agentic-state", git_user=IDENTITY,
                                        attribution_source="host_authenticated")
        self.place()
        self.found = registry.ExtensionRegistry(self.discover(), changes=self.runtime.database.assembly.change_publisher)
        self.found.sync(self.runtime.database, layer)
        from qorqut.extension_operations import register
        register(self.runtime.application.registry, self.found)

    def refused(self, operation, args, ctx=None):
        from qorqut_operations.contracts import InvocationOptions, new_id, plain
        result = self.runtime.application.invoke(ctx or self.runtime.owner, operation, {"scope": "global", **args},
                                                 InvocationOptions(idempotency_key=new_id()))
        self.assertNotEqual(result.status, "ok")
        return plain(result.error)

    def test_the_list_and_a_switch(self):
        coordinator = self.runtime.coordinator.principal_id
        listed = self.invoke(self.runtime, "extensions_list")
        self.assertEqual(listed, {"extensions": [{
            "id": "network", "name": "Network", "summary": "Ordubashy keeps your personal network, so he knows who you "
            "know, where you met and when to follow up.", "delivery": "downloadable", "version": "1.0.0",
            "status": "ready", "note": None, "agents": []}]})
        switched = self.invoke(self.runtime, "extensions_switch", {"extension": "network", "agent_id": coordinator,
                                                                   "on": True})
        self.assertEqual(switched["in_effect"], True)
        self.assertEqual(self.invoke(self.runtime, "extensions_list")["extensions"][0]["agents"], [coordinator])

    def test_switch_refusals(self):
        worker = self.runtime.worker.principal_id
        self.assertEqual(self.refused("extensions_switch", {"extension": "network", "agent_id": worker, "on": True})["code"],
                         "forbidden")
        self.assertEqual(self.refused("extensions_switch", {"extension": "missing", "agent_id": worker, "on": False})["code"],
                         "not_found")
        self.assertEqual(self.refused("extensions_switch", {"extension": "network", "agent_id": "nobody", "on": False})["code"],
                         "not_found")
        # The owner and Ordubashy in person, never another agent (the handler's own check; the policy is the stub's).
        from qorqut_operations.contracts import InvocationOptions, new_id
        listed = self.runtime.application.invoke(self.runtime.coordinator, "extensions_list", {"scope": "global"},
                                                 InvocationOptions(idempotency_key=new_id()))
        self.assertEqual(listed.status, "ok")
        for operation, args in (("extensions_list", {}),
                                ("extensions_setup", {"extension": "network", "action": "status"}),
                                ("extensions_switch", {"extension": "network", "agent_id": worker, "on": False})):
            self.assertEqual(self.refused(operation, args, ctx=self.runtime.worker),
                             {"code": "forbidden", "message": "Extensions are managed by the owner and Ordubashy.",
                              "retryable": False})
        # Setting up is Ordubashy's tool; the owner sets up on the card.
        self.assertEqual(self.refused("extensions_setup", {"extension": "network", "action": "start"})["code"], "forbidden")


    def test_another_person_is_refused_by_the_engine(self):
        from qorqut_operations.contracts import InvocationContext, new_id
        other = new_id()
        with self.runtime.database.transaction() as uow:
            uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "human"})
        ctx = InvocationContext(Principal(other, "human"), source="dashboard", global_capabilities={"read_global_context"})
        for operation, args in (("extensions_list", {}),
                                ("extensions_switch", {"extension": "network", "agent_id": self.runtime.worker.principal_id,
                                                       "on": False})):
            self.assertEqual(self.refused(operation, args, ctx=ctx)["code"], "forbidden")

    def test_the_owner_route_takes_both_operations(self):
        try:
            from starlette.applications import Starlette
            from starlette.routing import Route
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("Starlette is not installed")
        from qorqut.transport.http import HTTPAPI
        from qorqut_operations.contracts import new_id
        owner = self.runtime.owner.principal_id
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=self.runtime.database, require_authority=lambda: None,
                                      application=self.runtime.application, registry=self.runtime.application.registry,
                                      policy=SimpleNamespace(context_for=lambda *args, **options: self.runtime.owner))
        api.compatibility, api.guards = None, ()
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=owner, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)

        async def handler(request):
            return await api(request)
        client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(client.close)

        def post(operation, body):
            return client.post("/api/root/operations/" + operation, content=json.dumps({"scope": "global", **body}),
                               headers={"Content-Type": "application/json"}).json()
        coordinator = self.runtime.coordinator.principal_id
        listed = post("extensions_list", {})
        self.assertEqual([(item["id"], item["status"]) for item in listed["data"]["extensions"]], [("network", "ready")])
        switched = post("extensions_switch", {"extension": "network", "agent_id": coordinator, "on": True,
                                              "idempotency_key": new_id()})
        self.assertEqual(switched["data"]["in_effect"], True, switched)
        self.assertEqual(post("extensions_list", {})["data"]["extensions"][0]["agents"], [coordinator])


if __name__ == "__main__":
    unittest.main()
