"""Downloadable extensions end to end on a temporary instance with the ``network`` example: the index lists it, the job
stages its bundle and asks for a restart, the change is applied at the next start, and ``sync`` records it."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

import yaml

from qorqut import extension_index, extension_registry as registry
from qorqut.bootstrap import extension_change as change

from test_extension_registry import EXAMPLE, StoreCase

DISTRIBUTION = Path(__file__).resolve().parents[1].parent / "tools" / "distribution"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", DISTRIBUTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_tool("extension_bundle")
generator = load_tool("extension_index")


class Interrupted(Exception):
    pass


class ChangeCase(StoreCase):
    def setUp(self):
        super().setUp()
        self.paths.runtime.mkdir(parents=True)
        (self.paths.instance_root / "config").mkdir(mode=0o700)
        self.release = self.root / "release"
        self.release.mkdir()
        self.restarts = []
        self.source = self.release / "1.0.0" / "network"
        shutil.copytree(EXAMPLE, self.source)
        self.index = self.publish(self.source)

    def publish(self, folder):
        """Build the folder's bundle and an index of it, as a release does; the bundle's address is a file:// one."""
        out = self.release / ("out-" + yaml.safe_load((folder / "extension.yaml").read_text())["version"])
        out.mkdir()
        bundle = builder.build_folder(folder, "any", out / "network.tar.gz")["path"]
        value = generator.build_index([folder], [bundle], "1.7.0", out.as_uri())
        (out / extension_index.INDEX_FILE).write_text(json.dumps(value))
        return extension_index.load(out / extension_index.INDEX_FILE)

    def assertStaged(self, outcome):
        self.assertEqual(outcome, change.Outcome(True))

    def restart(self, reason):
        self.restarts.append(reason)
        return True

    def stage(self, index=None, bundle=None):
        entry, chosen = (index or self.index).entry_for("network", "linux-x86_64")
        return change.stage(self.paths, entry, bundle or chosen, requested_by="agent-ordubashy", restart=self.restart)

    def installed(self):
        return self.downloaded / "network"

    def update_source(self):
        newer = self.release / "1.0.1" / "network"
        shutil.copytree(self.source, newer)
        self.manifest(newer, lambda data: data.update(version="1.0.1"))
        skill = newer / "plugin" / "skills" / "network" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nNew text of 1.0.1.\n")
        return self.publish(newer)

    def subjects(self):
        return [commit["subject"] for commit in self.layer.history.log(paths=["plugins/network"])["commits"]]


class ListTest(ChangeCase):
    def offered(self, **arguments):
        arguments = {"app_version": "1.7.0", "platform": "linux", "machine": "x86_64", **arguments}
        found = self.discover()
        return registry.ExtensionRegistry(found, offered=extension_index.offered(self.index, found, **arguments)).list()

    def test_an_index_entry_without_a_folder_is_not_installed(self):
        listed = self.offered()
        self.assertEqual([(item.id, item.status, item.note, item.version, item.folder) for item in listed],
                         [("network", "not_installed", None, "1.0.0", None)])

    def test_an_entry_that_does_not_fit_is_unsupported_with_the_reason(self):
        self.assertEqual([(item.status, item.note) for item in self.offered(app_version="1.6.0")],
                         [("unsupported", "It needs Qorqbot >=1.7.0, and this is Qorqbot 1.6.0.")])
        # A build without a version number does not compare versions.
        self.assertEqual([item.status for item in self.offered(app_version=None)], ["not_installed"])

    def test_an_entry_for_another_computer_or_needing_another_extension_is_unsupported(self):
        value = json.loads((self.release / "out-1.0.0" / extension_index.INDEX_FILE).read_text())
        value["extensions"][0]["platforms"] = ["mac"]
        self.index = extension_index.parse(value)
        self.assertEqual([item.note for item in self.offered()], ["It runs only on mac, not on linux."])
        value["extensions"][0].update(platforms=["linux"], requires_extensions=["crm_extra"])
        self.index = extension_index.parse(value)
        self.assertEqual([item.note for item in self.offered()],
                         ["It needs the extension 'crm_extra', which cannot be installed here."])
        value["extensions"][0].update(requires_extensions=[])
        value["extensions"][0]["bundles"][0]["platform"] = "mac-arm64"
        self.index = extension_index.parse(value)
        self.assertEqual([item.note for item in self.offered()], ["No bundle of it is made for linux-x86_64."])

    def test_an_offered_entry_carries_its_name_and_summary(self):
        item = self.offered()[0]
        self.assertEqual((item.name, item.summary), ("Network", self.index.entry("network").summary))
        self.place()
        installed = self.offered()[0]
        self.assertEqual((installed.name, installed.folder is not None), ("Network", True))

    def test_reads_and_writes_of_installed_extensions_refuse_an_offered_id(self):
        found = self.discover()
        listed = registry.ExtensionRegistry(found, offered=extension_index.offered(
            self.index, found, app_version="1.7.0", platform="linux", machine="x86_64"))
        listed.sync(self.database, self.layer)
        refused = "No extension 'network' is installed."
        with self.assertRaisesRegex(Exception, refused):
            listed.get("network")
        with self.assertRaisesRegex(Exception, refused):
            self.write(lambda uow: listed.switch("network", "agent-ordubashy", True, uow=uow))
        with self.assertRaisesRegex(Exception, refused):
            self.write(lambda uow: listed.set_status("network", "ready", uow=uow))
        self.assertIsNone(listed.plugin_file("network", "hooks.yaml"))
        self.assertEqual((listed.on_for("agent-ordubashy"), listed.active("network")), ((), False))
        from qorqut.native_chat.extension_restart import ExtensionRestarts
        restarts = ExtensionRestarts(mock.Mock(), listed, start=lambda work: work())
        # An offered id is not an installed extension to the notice either.
        self.assertIn("The extension network was removed.", restarts.notice("agent-ordubashy", [("network", "switch")]))

    def test_an_installed_extension_is_listed_once(self):
        self.place()
        self.assertEqual([(item.id, item.delivery, item.folder is not None) for item in self.offered()],
                         [("network", "downloadable", True)])

    def test_the_bundle_platform_names(self):
        self.assertEqual(extension_index.bundle_platform("linux", "x86_64"), "linux-x86_64")
        self.assertEqual(extension_index.bundle_platform("linux", "aarch64"), "linux-arm64")
        self.assertEqual(extension_index.bundle_platform("mac", "arm64"), "mac-arm64")
        self.assertEqual(extension_index.bundle_platform("linux", "AMD64"), "linux-x86_64")

    def test_the_index_file_is_next_to_the_app_unless_the_trusted_configuration_names_one(self):
        with mock.patch.dict(os.environ, {"QORQUT_INSTALLATION_CONFIG": ""}):
            self.assertEqual(extension_index.index_path(self.paths), self.built_in.parent / "extensions-index.json")
            self.assertIsNone(extension_index.app_index(self.paths))
        config = self.root / "installation.json"
        test_index = self.release / "out-1.0.0" / extension_index.INDEX_FILE
        config.write_text(json.dumps({"format": 1, "root": str(self.root), "extensions_index": str(test_index)}))
        with mock.patch.dict(os.environ, {"QORQUT_INSTALLATION_CONFIG": str(config)}):
            self.assertEqual(extension_index.index_path(self.paths), test_index)
            self.assertEqual(extension_index.app_index(self.paths).entry("network").version, "1.0.0")
        test_index.write_text("{")
        with mock.patch.dict(os.environ, {"QORQUT_INSTALLATION_CONFIG": str(config)}), \
                self.assertLogs("qorqut.extension_index", "WARNING"):
            self.assertIsNone(extension_index.app_index(self.paths))


class InstallTest(ChangeCase):
    def test_install_update_and_remove(self):
        # Install: staged, a restart asked for, nothing installed yet.
        self.assertStaged(self.stage())
        self.assertEqual(self.restarts, ["install extension network 1.0.0"])
        pending = change.pending(self.paths)
        self.assertEqual((pending["action"], pending["id"], pending["version"], pending["requested_by"],
                          pending["result"]), ("install", "network", "1.0.0", "agent-ordubashy", None))
        self.assertFalse(self.installed().exists())
        self.assertEqual(os.listdir(change.staging(self.paths)), [self.index.entry("network").bundles[0].sha256])
        # The next start: applied before the selection, then found and recorded.
        change.apply_pending(self.paths)
        self.assertTrue((self.installed() / "extension.yaml").is_file())
        self.assertEqual(os.listdir(change.staging(self.paths)), [])
        found = self.start()
        self.assertEqual([(item.id, item.status) for item in found.list()], [("network", "ready")])
        self.assertEqual(found.package_ids(), frozenset({"network"}))
        self.assertEqual(change.finish(self.paths)["result"], {"state": "done", "note": None})
        self.assertIsNone(change.pending(self.paths))

        # Update: the folder is replaced, the old one kept as a backup, the layer copy updated by sync.
        newer = self.update_source()
        self.assertStaged(self.stage(newer))
        self.assertEqual(change.pending(self.paths)["action"], "update")
        change_id = change.pending(self.paths)["change_id"]
        change.apply_pending(self.paths)
        backup = self.paths.instance_root / "extension-backups" / change_id / "network"
        self.assertIn("version: 1.0.0", (backup / "extension.yaml").read_text())
        self.assertIn("version: 1.0.1", (self.installed() / "extension.yaml").read_text())
        found = self.start()
        change.finish(self.paths)
        self.assertEqual(found.get("network").version, "1.0.1")
        self.assertIn("New text of 1.0.1.", found.plugin_file("network", "skills/network/SKILL.md").decode())
        self.assertEqual(self.subjects()[0], "Update extension network to 1.0.1")

        # Removal: the folder goes to a backup, sync removes the row and the package.
        self.assertStaged(change.stage_removal(self.paths, "network", restart=self.restart))
        self.assertEqual(self.restarts[-1], "remove extension network")
        change_id = change.pending(self.paths)["change_id"]
        change.apply_pending(self.paths)
        self.assertFalse(self.installed().exists())
        self.assertTrue((self.paths.instance_root / "extension-backups" / change_id / "network").is_dir())
        found = self.start()
        change.finish(self.paths)
        self.assertEqual(found.list(), [])
        self.assertIsNone(self.row())
        self.assertEqual(found.package_ids(), frozenset())

    def test_an_installed_download_leaves_the_app_updates_gate_as_it_was(self):
        # The app's update (``tools/distribution/native_update.py``) refuses only when ``config/extensions.json`` selects a
        # factory its bundle does not carry. A downloaded extension is never written there, so the update goes ahead.
        selection = self.paths.instance_root / "config" / "extensions.json"
        selection.write_text(json.dumps({"extensions": []}))
        before = selection.read_bytes()
        self.assertStaged(self.stage())
        change.apply_pending(self.paths)
        change.finish(self.paths)
        self.assertTrue(self.installed().is_dir())
        self.assertEqual(selection.read_bytes(), before)
        update = (DISTRIBUTION / "native_update.py").read_text()
        self.assertEqual(update.count("config/extensions.json"), 1)

    def test_the_result_at_the_next_start_leaves_a_line_the_service_log_writes(self):
        # The service sets no log level of its own, so only warnings and above are written.
        self.assertStaged(self.stage())
        change.apply_pending(self.paths)
        with self.assertLogs("qorqut.bootstrap.extension_change", "WARNING") as written:
            change.finish(self.paths)
        self.assertEqual(len(written.records), 1)
        self.assertIn("extension_change_finished action=install extension=network state=done", written.output[0])

    def test_a_bad_checksum_changes_nothing(self):
        bundle = replace(self.index.entry("network").bundles[0], sha256="0" * 64)
        self.assertEqual(self.stage(bundle=bundle),
                         change.Outcome(False, "The downloaded bundle does not have the checksum the index gives."))
        self.assertEqual((self.restarts, change.pending(self.paths)), ([], None))
        self.assertEqual(os.listdir(change.staging(self.paths)), [])
        self.assertFalse(self.installed().exists())

    def test_a_bundle_larger_than_the_index_says_or_missing_changes_nothing(self):
        bundle = self.index.entry("network").bundles[0]
        self.assertIn("larger than the", self.stage(bundle=replace(bundle, size=10)).note)
        missing = replace(bundle, url=(self.release / "absent.tar.gz").as_uri())
        self.assertIn("could not be downloaded or unpacked", self.stage(bundle=missing).note)
        self.assertEqual((self.restarts, change.pending(self.paths)), ([], None))

    def test_only_one_change_waits_at_a_time(self):
        self.assertStaged(self.stage())
        self.assertEqual(self.stage(), change.Outcome(False, change.WAITING))
        self.assertEqual(change.stage_removal(self.paths, "network", restart=self.restart),
                         change.Outcome(False, change.WAITING))
        self.assertEqual(len(self.restarts), 1)

    def test_removing_what_is_not_installed_is_refused(self):
        self.assertEqual(change.stage_removal(self.paths, "network", restart=self.restart),
                         change.Outcome(False, "No downloaded extension 'network' is installed."))
        self.place()
        for bad in ("network/../../config", "../config", ".staging", "Network"):
            with self.subTest(bad):
                self.assertFalse(change.stage_removal(self.paths, bad, restart=self.restart).staged)
        self.assertIsNone(change.pending(self.paths))

    def test_a_staged_folder_that_breaks_the_contract_is_not_moved_in(self):
        self.assertStaged(self.stage())
        (Path(change.pending(self.paths)["staged"]) / "README.md").unlink()
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"]["state"], "failed")
        self.assertIn("README.md is missing", change.pending(self.paths)["result"]["note"])
        self.assertFalse(self.installed().exists())

    def test_nothing_pending_does_nothing(self):
        change.apply_pending(self.paths)
        self.assertIsNone(change.finish(self.paths))
        self.assertFalse(self.downloaded.exists())


class StageFailureTest(ChangeCase):
    def test_no_restart_to_ask_for_keeps_the_change_for_the_next_start(self):
        outcome = self.stage_with(lambda reason: False)
        self.assertEqual(outcome, change.Outcome(
            True, "Qorqbot could not be asked to restart, so the change is applied at its next start."))
        self.assertIsNotNone(change.pending(self.paths))
        change.apply_pending(self.paths)
        self.assertTrue(self.installed().is_dir())

    def stage_with(self, restart):
        entry, chosen = self.index.entry_for("network", "linux-x86_64")
        return change.stage(self.paths, entry, chosen, restart=restart)

    def assertNothingLeft(self, outcome):
        self.assertFalse(outcome.staged)
        self.assertIsNone(change.pending(self.paths))
        self.assertEqual(os.listdir(change.staging(self.paths)), [])

    def test_a_restart_request_that_fails_leaves_nothing(self):
        def boom(reason):
            raise RuntimeError("restart failed")
        outcome = self.stage_with(boom)
        self.assertNothingLeft(outcome)
        self.assertIn("restart failed", outcome.note)

    def test_a_change_file_that_cannot_be_written_leaves_nothing(self):
        with mock.patch.object(change, "_save", side_effect=OSError("disk full")):
            outcome = self.stage_with(self.restart)
        self.assertNothingLeft(outcome)
        self.assertEqual(self.restarts, [])
        self.assertIn("disk full", outcome.note)


class BrokenChangeFileTest(ChangeCase):
    """A change file that is not a valid change is set aside and the start goes on; nothing outside the three places a
    change may touch is moved."""

    def write_change(self, value):
        path = change.change_path(self.paths)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        os.chmod(path, 0o600)

    def set_aside(self):
        config = self.paths.instance_root / "config"
        return [name for name in os.listdir(config) if name.startswith(change.CHANGE + ".refused-")]

    def tree(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")
                      if "extension-change.json" not in path.name)

    def assertSetAside(self, value):
        self.write_change(value)
        before = self.tree()
        with self.assertLogs("qorqut.bootstrap.extension_change", "WARNING") as logged:
            change.apply_pending(self.paths)
        self.assertFalse(change.change_path(self.paths).exists())
        self.assertEqual(len(self.set_aside()), 1)
        self.assertEqual(self.tree(), before)
        self.assertNotIn("victim", " ".join(logged.output))
        self.assertIsNone(change.finish(self.paths))
        for name in self.set_aside():
            (self.paths.instance_root / "config" / name).unlink()

    def valid(self, **changes):
        value = {"format": 1, "change_id": "a" * 32, "action": "remove", "id": "network", "version": None,
                 "sha256": None, "staged": None, "requested_by": None, "signoff_id": None, "delete_data": False,
                 "result": None}
        value.update(changes)
        return value

    def test_malformed_files_are_set_aside(self):
        for value in ("{", "[]", {"result": None}, self.valid(format=2), self.valid(action="move"),
                      self.valid(result={"state": "maybe", "note": None}), self.valid(delete_data="no")):
            with self.subTest(value=value):
                self.assertSetAside(value)

    def test_paths_from_the_file_are_checked(self):
        victim = self.paths.instance_root / "config" / "victim"
        victim.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.place()
        sha = self.index.entry("network").bundles[0].sha256
        staged = str(change.staging(self.paths) / ".." / "network")
        for value in (self.valid(id="../config/victim"), self.valid(id=str(outside)), self.valid(id="network/../../config"),
                      self.valid(change_id="../../escaped"), self.valid(change_id="A" * 32),
                      self.valid(action="install", version="1.0.0", sha256=sha, staged=staged),
                      self.valid(action="install", version="1.0.0", sha256="../x", staged=staged),
                      self.valid(staged="/tmp/x")):
            with self.subTest(value=value):
                self.assertSetAside(value)
        self.assertTrue(victim.is_dir() and outside.is_dir() and self.installed().is_dir())
        self.assertFalse((self.paths.instance_root / "extension-backups").exists())

    def test_the_start_goes_on_after_a_broken_file(self):
        self.write_change("{")
        with self.assertLogs("qorqut.bootstrap.extension_change", "WARNING"):
            change.apply_pending(self.paths)
        self.assertStaged(self.stage())
        change.apply_pending(self.paths)
        self.assertTrue(self.installed().is_dir())


class InterruptionTest(ChangeCase):
    def interrupt(self, phase):
        def hook(reached):
            if reached == phase:
                raise Interrupted(phase)
        return mock.patch.object(change, "_fault_hook", hook)

    def installed_once(self):
        self.assertStaged(self.stage())
        change.apply_pending(self.paths)
        self.start()
        change.finish(self.paths)

    def test_an_interrupted_install_finishes_at_the_next_start(self):
        for phase in ("backed-up", "moved", "applied"):
            with self.subTest(phase):
                self.assertStaged(self.stage())
                with self.interrupt(phase), self.assertRaises(Interrupted):
                    change.apply_pending(self.paths)
                change.apply_pending(self.paths)
                self.assertEqual(change.pending(self.paths)["result"]["state"], "done")
                self.assertTrue((self.installed() / "extension.yaml").is_file())
                self.assertEqual([item.status for item in self.start().list()], ["ready"])
                change.finish(self.paths)
                shutil.rmtree(self.installed())
                self.start()

    def test_an_interrupted_update_finishes_at_the_next_start(self):
        self.installed_once()
        newer = self.update_source()
        for phase in ("backed-up", "moved", "applied"):
            with self.subTest(phase):
                self.assertStaged(self.stage(newer))
                with self.interrupt(phase), self.assertRaises(Interrupted):
                    change.apply_pending(self.paths)
                change.apply_pending(self.paths)
                self.assertEqual(change.pending(self.paths)["result"]["state"], "done")
                self.assertIn("version: 1.0.1", (self.installed() / "extension.yaml").read_text())
                change.finish(self.paths)

    def test_an_update_that_fails_after_the_backup_puts_the_old_folder_back(self):
        self.installed_once()
        newer = self.update_source()
        for broken in ("contract", "gone"):
            with self.subTest(broken):
                self.assertStaged(self.stage(newer))
                with self.interrupt("backed-up"), self.assertRaises(Interrupted):
                    change.apply_pending(self.paths)
                self.assertFalse(self.installed().exists())
                staged = Path(change.pending(self.paths)["staged"])
                if broken == "contract":
                    (staged / "README.md").unlink()
                else:
                    shutil.rmtree(staged.parent)
                change.apply_pending(self.paths)
                self.assertEqual(change.pending(self.paths)["result"]["state"], "failed")
                self.assertIn("version: 1.0.0", (self.installed() / "extension.yaml").read_text())
                change.finish(self.paths)

    def test_a_staged_folder_gone_before_the_backup_leaves_the_old_folder(self):
        self.installed_once()
        self.assertStaged(self.stage(self.update_source()))
        shutil.rmtree(Path(change.pending(self.paths)["staged"]).parent)
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"]["state"], "failed")
        self.assertIn("version: 1.0.0", (self.installed() / "extension.yaml").read_text())

    def test_an_interrupted_removal_finishes_at_the_next_start(self):
        self.installed_once()
        for phase in ("moved", "applied"):
            with self.subTest(phase):
                if not self.installed().exists():
                    self.installed_once()
                self.assertStaged(change.stage_removal(self.paths, "network", restart=self.restart))
                with self.interrupt(phase), self.assertRaises(Interrupted):
                    change.apply_pending(self.paths)
                change.apply_pending(self.paths)
                self.assertEqual(change.pending(self.paths)["result"]["state"], "done")
                self.assertFalse(self.installed().exists())
                change.finish(self.paths)
                self.start()


if __name__ == "__main__":
    unittest.main()
