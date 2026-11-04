"""A new build's provenance-only catalog refresh is admitted without writes; any real managed change is still refused."""
import base64
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from bazarlyk.bootstrap import native_update_check as module

CATALOG = {
    "id": "imported-terminal-control", "schema_version": 1,
    "entries": [{"id": "asset-1", "kind": "template", "path": "native/app/instructions/a.md"}],
    "source": {"import_adapter": "v1.3-offline", "sources": [{
        "id": "host", "commit": "766d0449ee3c17684fb666c7237281a2a95f2df5", "tree": "6d08bec6",
        "root": "/Users/x/Qorqbot/qorqbot/runtime/src/qorqut-code", "snapshot_digest": "c8f0",
        "snapshot_sha256": "b9aa", "source_manifest_sha256": "afab",
        "files": {"app/bazarlyk/agentic/assets/backlog.md": "c795"}}]},
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
        change = edit(self.path, refreshed(commit="6895969d", files={"app/bazarlyk/agentic/assets/backlog.md": "ffff"}))
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
                mock.patch("bazarlyk.agentic.managed_update.apply", side_effect=AssertionError("Admission must not write")):
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


if __name__ == "__main__":
    unittest.main()
