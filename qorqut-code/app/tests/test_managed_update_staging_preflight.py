"""macOS staging devices are checked before a managed update has any effect.

Run from qorqut-code/app:  python -m unittest tests.test_managed_update_staging_preflight

macOS is simulated (``posix_files._darwin`` patched) and a second file system is simulated by reporting another st_dev
for one directory; every effect of ``apply`` is replaced by a recorder, so nothing here is a native macOS run.
"""
import contextlib
import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from bazarlyk import posix_files
from bazarlyk.agentic import managed_update as module

PD = "a" * 64
SHA = "b" * 64
ATTRIBUTION = {"actor": "host"}


class Reached(Exception):
    """Raised by the first effect or check that follows the preflight."""


@contextlib.contextmanager
def foreign_device(*directories):
    marked = {os.stat(directory).st_ino for directory in directories}
    real = os.fstat

    def fstat(fd):
        result = real(fd)
        if result.st_ino in marked:
            return os.stat_result((result.st_mode, result.st_ino, result.st_dev + 1, *result[3:]))
        return result
    with mock.patch.object(os, "fstat", fstat):
        yield


class Harness(unittest.TestCase):
    """A managed instance on one real file system with every effect of ``apply`` replaced by a recorder."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.previous = dict(posix_files._staging)
        self.addCleanup(self.restore_staging)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.agentic_root = self.root / "agentic"
        (self.agentic_root / ".git").mkdir(parents=True)
        self.instance_root = self.root / "instance"
        self.config = self.instance_root / "config"
        self.config.mkdir(parents=True)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.paths = types.SimpleNamespace(agentic_root=self.agentic_root, agentic_runtime=self.runtime,
                                           instance_root=self.instance_root)
        self.history = module.history_directory(self.paths)
        self.plan = {"plan_digest": PD, "acknowledge_grants": False, "changes": [{"path": "a.md"}],
                     "binding": {"record_sha256": SHA}, "git": {"head": "h"},
                     "evidence": {"intent": str(self.history / ("managed-intent-" + PD + ".json")),
                                  "journal": str(self.history / ("managed-journal-" + PD + ".json"))}}
        self.patch(module.managed_plan, "validate_plan")
        self.patch(module.managed_intent, "_retained_plan", return_value=self.plan)
        self.patch(module, "normalize_attribution", return_value=ATTRIBUTION)
        self.patch(module, "Store")
        self.effects = {name: self.patch(owner, name) for owner, name in (
            (module, "read_binding_bytes"), (module, "_provision_before"), (module, "retain_prior_binding"),
            (module, "_publish_intent"), (module.managed_journal, "write"), (module, "_new_journal"),
            (module, "_provision"), (module.managed_files, "publish"), (module.managed_git, "prepare"),
            (module, "_initial_after_intent"), (module, "_common"))}
        self.effects["read_binding_bytes"].return_value = b"{}"
        self.effects["retain_prior_binding"].side_effect = Reached
        self.effects["_common"].side_effect = Reached
        self.intent = {"prior_binding_sha256": SHA, "plan": self.plan, "attribution": ATTRIBUTION}
        self.patch(module.managed_intent, "capture", return_value=self.intent)

    def patch(self, owner, name, **kwargs):
        return self.stack.enter_context(mock.patch.object(owner, name, **kwargs))

    def restore_staging(self):
        current = posix_files._staging.get("fd")
        if current is not None and current != self.previous.get("fd"):
            os.close(current)
        posix_files._staging.clear()
        posix_files._staging.update(self.previous)

    def apply(self):
        return module.apply(self.paths, self.plan, attribution=ATTRIBUTION)

    def tree(self):
        staging = self.runtime / "publish-staging"
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p != staging and staging not in p.parents)

    def assertNoEffects(self):
        for name, effect in self.effects.items():
            self.assertFalse(effect.called, name + " ran before the device preflight")

    def resume(self, phase, *, journal=True):
        pair = {"intent": Path("intent")}
        if journal:
            pair["journal"] = Path("journal")
        self.patch(module.managed_journal, "discover", return_value={PD: pair})
        self.patch(module.managed_journal, "read", return_value={"phase": phase})
        self.patch(module, "read_private_file", return_value=b"{}")
        self.patch(module.managed_intent, "_read_retained", return_value=self.intent)
        self.patch(module, "_check_source_if_present")
        self.patch(module, "_layout_proof", return_value=({}, {}))
        self.patch(module.managed_ancestry, "_verify_prior_for_resume")


class SimulatedDarwinTest(Harness):
    def setUp(self):
        super().setUp()
        self.patch(posix_files, "_darwin", return_value=True)

    # ---- new application ---------------------------------------------------------------------------------------
    def test_matching_devices_proceed_to_the_first_effect(self):
        with self.assertRaises(Reached):
            self.apply()
        self.assertTrue(self.effects["retain_prior_binding"].called)

    def test_a_git_directory_on_another_device_fails_before_any_effect(self):
        before = self.tree()
        with foreign_device(self.agentic_root / ".git"):
            with self.assertRaises(OSError) as raised:
                self.apply()
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertIn(str(self.agentic_root / ".git"), str(raised.exception))
        self.assertNoEffects()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.history.exists())

    def test_a_config_parent_on_another_device_fails_before_any_effect_while_history_is_absent(self):
        before = self.tree()
        with foreign_device(self.config):
            with self.assertRaises(OSError) as raised:
                self.apply()
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertIn(str(self.config), str(raised.exception))
        self.assertNoEffects()
        self.assertEqual(self.tree(), before)

    def test_an_existing_history_directory_on_another_device_fails_before_any_effect(self):
        self.history.mkdir(mode=0o700)
        before = self.tree()
        with foreign_device(self.history):
            with self.assertRaises(OSError) as raised:
                self.apply()
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertIn(str(self.history), str(raised.exception))
        self.assertNoEffects()
        self.assertEqual(self.tree(), before)

    def test_an_existing_history_directory_is_checked_instead_of_its_parent(self):
        self.history.mkdir(mode=0o700)
        with foreign_device(self.config):
            with self.assertRaises(Reached):
                self.apply()

    def test_a_symlinked_git_directory_fails_closed_before_any_effect(self):
        shutil.rmtree(self.agentic_root / ".git")
        (self.agentic_root / ".git").symlink_to(self.root)
        with self.assertRaises(OSError):
            self.apply()
        self.assertNoEffects()

    def test_descriptors_opened_by_the_preflight_are_closed_on_every_path(self):
        live, real_open, real_close = set(), os.open, os.close

        def opening(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            live.add(fd)
            return fd

        def closing(fd):
            real_close(fd)
            live.discard(fd)
        with mock.patch.object(os, "open", opening), mock.patch.object(os, "close", closing):
            with foreign_device(self.agentic_root / ".git"):
                with self.assertRaises(OSError):
                    self.apply()
            # Only the retained staging descriptor stays open.
            self.assertEqual(live, {posix_files._staging["fd"]})
            with self.assertRaises(Reached):
                self.apply()
            self.assertEqual(live, {posix_files._staging["fd"]})

    def test_an_update_without_a_git_change_does_not_need_the_git_device(self):
        self.plan["changes"] = []
        with foreign_device(self.agentic_root / ".git"):
            with self.assertRaises(Reached):
                self.apply()

    # ---- replays and no-change updates that publish nothing -----------------------------------------------------------
    def test_a_no_change_update_is_not_rejected_for_a_device_mismatch(self):
        self.patch(module.managed_intent, "capture", return_value=None)
        self.plan["binding"]["record_sha256"] = SHA
        with foreign_device(self.agentic_root / ".git", self.config):
            result = self.apply()
        self.assertTrue(result["no_change"])
        self.assertNoEffects()

    def test_an_already_complete_replay_is_not_rejected_for_a_device_mismatch(self):
        self.patch(module.managed_journal, "discover", return_value={PD: {"intent": Path("i"), "journal": Path("j")}})
        self.patch(module.managed_journal, "read", return_value={
            "phase": "complete", "attribution": ATTRIBUTION, "prior_binding_sha256": SHA, "result": {"done": True}})
        self.patch(module, "read_private_file", return_value=b"{}")
        self.patch(module.managed_intent, "_read_retained", return_value=self.intent)
        self.patch(module, "_proof", return_value={"proof": True})
        record = {"managed_upgrade": {"plan_digest": PD, "prior_binding_sha256": SHA}}
        installed = mock.MagicMock()
        installed.__enter__.return_value = record
        self.patch(sys.modules["bazarlyk.agentic.installation"], "_validated_installed_binding", return_value=installed)
        with foreign_device(self.agentic_root / ".git", self.config):
            result = self.apply()
        self.assertTrue(result["replayed"])
        self.assertNoEffects()

    # ---- resumption of a partial journal ------------------------------------------------------------------------------
    def test_a_partial_journal_is_refused_before_it_resumes_and_resumes_once_the_device_is_corrected(self):
        self.resume("files_published")
        with foreign_device(self.agentic_root / ".git"):
            with self.assertRaises(OSError) as raised:
                self.apply()
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertNoEffects()
        with self.assertRaises(Reached):
            self.apply()
        self.assertTrue(self.effects["_common"].called)

    def test_a_resumed_intent_does_not_need_the_history_device(self):
        self.resume("prepared")
        self.history.mkdir(mode=0o700)
        with foreign_device(self.config, self.history):
            with self.assertRaises(Reached):
                self.apply()

    def test_an_intent_without_a_journal_is_refused_before_a_journal_is_written(self):
        self.resume("prepared", journal=False)
        with foreign_device(self.agentic_root / ".git"):
            with self.assertRaises(OSError) as raised:
                self.apply()
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertNoEffects()

    def test_a_journal_past_index_publication_does_not_need_the_git_device(self):
        for phase in ("git_published", "feed_published", "binding_published"):
            self.resume(phase)
            with foreign_device(self.agentic_root / ".git"):
                with self.assertRaises(Reached):
                    self.apply()

    def test_every_phase_that_can_still_stage_the_index_needs_the_git_device(self):
        for phase in ("prepared", "files_published", "git_prepared", "ref_published", "index_publishing"):
            self.resume(phase)
            with foreign_device(self.agentic_root / ".git"):
                with self.assertRaises(OSError, msg=phase) as raised:
                    self.apply()
            self.assertEqual(raised.exception.errno, errno.EXDEV, phase)
        self.assertNoEffects()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux publication path")
class LinuxUnchangedTest(Harness):
    """The platform is not simulated: no staging is prepared and no device is compared."""

    def test_a_device_mismatch_is_ignored_and_no_staging_is_prepared(self):
        posix_files._staging.pop("fd", None)
        with foreign_device(self.agentic_root / ".git", self.config):
            with self.assertRaises(Reached):
                self.apply()
        self.assertFalse((self.runtime / "publish-staging").exists())
        self.assertNotIn("fd", posix_files._staging)

    def test_a_resumed_journal_is_not_checked_for_devices(self):
        self.resume("files_published")
        with foreign_device(self.agentic_root / ".git"):
            with self.assertRaises(Reached):
                self.apply()
        self.assertTrue(self.effects["_common"].called)


if __name__ == "__main__":
    unittest.main()
