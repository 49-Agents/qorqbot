"""Fresh instruction evidence with bounded parsing and filesystem work."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from qorqut.agentic.view import HostAgenticView
from qorqut_operations.contracts import DomainError
from qorqut_agentic import catalog_api
from qorqut_agentic.errors import AgenticError, UnsafePathError
from qorqut_agentic import files as files_module
from qorqut_agentic.files import SafeTree

import test_mcp_catalog_cache as fixtures
from scripts.measure_toolcall_latency import fixture

SKILL = fixtures.SKILL


class InstructionReadTests(unittest.TestCase):
    setUp = fixtures.CatalogSnapshotTest.setUp
    write_manifest = fixtures.CatalogSnapshotTest.write_manifest
    write_skill = fixtures.CatalogSnapshotTest.write_skill
    inventory = fixtures.CatalogSnapshotTest.inventory
    counting = fixtures.CatalogSnapshotTest.counting

    def view(self):
        view = HostAgenticView.__new__(HostAgenticView)
        view._layer = self.layer
        return view

    def body(self):
        return self.layer.catalog.read_resource('demo', 'skills/greeting/SKILL.md')['content']

    def test_nested_projections_share_one_verified_snapshot(self):
        view = self.view()
        self.inventory()
        with self.counting() as parse, mock.patch.object(
                catalog_api, 'DiskSource', wraps=catalog_api.DiskSource) as source:
            result = view.consistent(lambda: view.consistent(self.body))
        self.assertEqual(result, SKILL)
        self.assertEqual(parse.call_count, 0)
        self.assertEqual(source.call_count, 2)  # entry and exit, including nested readers

    def test_standalone_resource_reuses_parse_but_verifies_fresh_bytes(self):
        self.inventory()
        with self.counting() as parse:
            self.assertEqual(self.body(), SKILL)
            self.assertEqual(parse.call_count, 0)
            path = self.tree / 'roles/demo/skills/greeting/SKILL.md'
            before = path.stat()
            changed = SKILL.replace('hello', 'HELLO')
            path.write_text(changed)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.body(), changed)
            self.assertEqual(parse.call_count, 1)

    def test_change_during_projection_retries_without_mixing_revisions(self):
        view, calls = self.view(), []
        changed = SKILL + '\nNew instruction.\n'
        def read():
            result = self.body()
            calls.append(result)
            if len(calls) == 1:
                self.write_skill(changed)
            return result
        self.assertEqual(view.consistent(read), changed)
        self.assertEqual(calls, [SKILL, changed])

    def test_explicit_revision_cannot_switch_to_newer_instructions(self):
        view = self.view()
        revision = self.inventory()[0]
        self.write_skill(SKILL + '\nChanged.\n')
        reader = mock.Mock()
        with self.assertRaises(DomainError) as caught:
            view.consistent(reader, revision=revision)
        self.assertEqual(caught.exception.code, 'agentic_revision_conflict')
        reader.assert_not_called()

    def test_non_revision_errors_are_not_retried_or_hidden(self):
        reader = mock.Mock(side_effect=DomainError('forbidden', 'Denied'))
        with self.assertRaises(DomainError) as caught:
            self.view().consistent(reader)
        self.assertEqual(caught.exception.code, 'forbidden')
        reader.assert_called_once()

    def test_inventory_matches_independent_walk_and_rejects_file_swap_to_symlink(self):
        tree = SafeTree(self.tree)
        nested = self.tree / 'roles/demo/deep/nested'
        nested.mkdir(parents=True)
        for index in range(12):
            (nested / f'{index}.txt').write_bytes(b'x' * index)
        (nested / 'link').symlink_to(self.tree / 'roles/demo/agentic.yaml')
        os.mkfifo(nested / 'pipe')
        files = {rel: tree.digest(rel) for rel, kind in tree.walk('roles') if kind == 'file'}
        special = {rel: kind for rel, kind in tree.walk('roles') if kind != 'file'}
        self.assertEqual(tree.inventory('roles'), (files, special))
        self.assertEqual(tree.inventory('absent'), ({}, {}))
        (self.root / 'outside').write_text('Must never be read')
        # A file replaced by a link after the directory was listed is refused, whether its digest is being read
        # for the first time or was remembered from an earlier inventory.
        for remembered in (False, True):
            with self.subTest(remembered=remembered):
                target = self.tree / 'roles/demo/deep/nested/1.txt'
                if target.is_symlink():
                    target.unlink()
                    target.write_bytes(b'x')
                clock = time.time_ns() + (10 * files_module.DIGEST_SETTLE_NS if remembered else 0)
                with mock.patch.object(files_module.time, 'time_ns', return_value=clock):
                    tree.inventory('roles')
                self.assertEqual('roles/demo/deep/nested/1.txt' in tree._digests, remembered)
                original = tree._digest_at
                def swap(parent, name, rel):
                    if rel.endswith('/1.txt') and not target.is_symlink():
                        target.unlink()
                        target.symlink_to(self.root / 'outside')
                    return original(parent, name, rel)
                with mock.patch.object(tree, '_digest_at', side_effect=swap):
                    with self.assertRaises(UnsafePathError):
                        tree.inventory('roles')

    def test_regular_reads_still_enforce_limits_and_no_symlinks(self):
        tree = SafeTree(self.tree)
        self.assertEqual(tree.read('roles/demo/skills/greeting/SKILL.md'), SKILL.encode())
        self.assertIsNone(tree.read('roles/demo/missing'))
        with self.assertRaises(AgenticError) as caught:
            tree.read('roles/demo/skills/greeting/SKILL.md', max_bytes=1)
        self.assertEqual(caught.exception.code, 'limit_exceeded')
        (self.tree / 'roles/demo/link').symlink_to(self.root)
        with self.assertRaises(UnsafePathError):
            tree.read('roles/demo/link/outside')


class RuntimeInstructionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root, self.layer, self.view = fixture(Path(directory.name))
        self.key = 'attention-board-transition'
        self.path = self.root / 'operations/runtime-instructions/messages' / (self.key + '.md')

    def test_verified_hit_skips_projection_and_returns_private_documents(self):
        first = self.view.runtime_instructions()
        original = first.render(self.key)
        first.document['messages'][self.key]['body'] = 'caller-local edit'
        with mock.patch.object(self.view, 'resource', side_effect=AssertionError('reprojected')):
            second = self.view.runtime_instructions()
        self.assertEqual(second.render(self.key), original)
        self.assertIsNot(first, second)

    def test_cached_prose_cannot_hide_same_size_same_mtime_edits(self):
        before = self.view.runtime_instructions().render(self.key)
        stat = self.path.stat()
        changed = before.swapcase()
        self.path.write_text(changed)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.view.runtime_instructions().render(self.key), changed)

    def test_invalid_current_instructions_never_fall_back_to_cached_prose(self):
        self.view.runtime_instructions()
        self.path.write_text('')
        with self.assertRaises(ValueError):
            self.view.runtime_instructions()
        self.path.unlink()
        with self.assertRaises(DomainError):
            self.view.runtime_instructions()

    def test_failed_exit_does_not_publish_new_prose(self):
        self.view.runtime_instructions()
        cached = self.view._runtime_instruction_bytes
        self.path.write_text('New instruction.\n')
        original = self.view.resource
        def change_during_read(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[1].endswith('/' + self.key + '.md'):
                self.path.write_text(self.path.read_text() + 'Changed again.\n')
            return value
        with mock.patch.object(self.view, 'resource', side_effect=change_during_read):
            with self.assertRaises(DomainError) as caught:
                self.view.runtime_instructions()
        self.assertEqual(caught.exception.code, 'agentic_revision_conflict')
        self.assertEqual(self.view._runtime_instruction_bytes, cached)
        self.assertEqual(self.view.runtime_instructions().render(self.key), self.path.read_text())

    def test_verified_hit_does_not_bypass_snapshot_recovery_failure(self):
        self.view.runtime_instructions()
        with mock.patch.object(self.layer.catalog, 'read_snapshot',
                               side_effect=AgenticError('Recovery required', code='recovery_required')):
            with self.assertRaises(DomainError) as caught:
                self.view.runtime_instructions()
        self.assertEqual(caught.exception.code, 'agentic_unavailable')

    def test_nested_read_is_still_subject_to_outer_snapshot_verification(self):
        with self.assertRaises(DomainError) as caught:
            with self.view.read_snapshot():
                self.view.runtime_instructions()
                self.path.write_text('Changed before the outer snapshot exits.\n')
        self.assertEqual(caught.exception.code, 'agentic_revision_conflict')
        self.assertEqual(self.view.runtime_instructions().render(self.key), self.path.read_text())
