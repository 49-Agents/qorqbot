"""Focused behavior for the bounded last-parsed-catalog cache in ``qorqut_agentic.catalog_api``.

The fixture is a real initialized authoring root on a temporary directory and a
synthetic package written into it; nothing is mocked except the parse counter.
"""
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest import mock

from qorqut_agentic import catalog_api
from qorqut_agentic.errors import RevisionConflictError
from qorqut_agentic.layer import AgenticLayer

DEADLINE = 20

MANIFEST = """schema_version: 1
id: demo
name: Demo package
description: Synthetic package used by the catalog snapshot cache test.
entries:
  - id: greeting
    kind: skill
    description: Synthetic greeting skill.
    when_to_use: Use only inside the synthetic fixture.
    path: skills/greeting/SKILL.md
"""

SKILL = """---
name: greeting
description: Synthetic greeting skill.
---
Say hello in one sentence.
"""


class CatalogSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='catalog cache '))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tree, self.state = self.root / 'authored', self.root / 'state'
        self.layer = AgenticLayer.initialize(self.tree, self.state)
        self.write_manifest()
        self.write_skill(SKILL)

    # -- fixture --------------------------------------------------------------
    def write_manifest(self, body=MANIFEST):
        directory = self.tree / 'roles' / 'demo'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'agentic.yaml').write_text(body)

    def write_skill(self, body=SKILL):
        path = self.tree / 'roles' / 'demo' / 'skills' / 'greeting' / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def inventory(self, reader=None):
        """One ordinary outer snapshot with two inventories and one resource body."""
        reader = reader or self.layer.catalog
        with reader.read_snapshot() as revision:
            listed = reader.list()
            entry = reader.read_entry('demo/greeting')
            resource = reader.read_resource('demo', 'skills/greeting/SKILL.md')
        return revision, listed, entry, resource

    def counting(self):
        return mock.patch.object(catalog_api, 'load_catalog', wraps=catalog_api.load_catalog)

    # -- evidence -------------------------------------------------------------
    def test_unchanged_snapshots_parse_once_and_keep_both_inventories(self):
        with self.counting() as parse:
            first = self.inventory()
            second = self.inventory()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(first[0], second[0])
        self.assertEqual([row['ref'] for row in first[1]['entries']], ['demo/greeting'])
        self.assertEqual([row['ref'] for row in second[1]['entries']], ['demo/greeting'])
        self.assertEqual(second[2]['entry']['ref'], 'demo/greeting')
        self.assertEqual(second[3]['content'], SKILL)

    def test_body_edit_add_remove_and_special_entries_rebuild_the_parse(self):
        with self.counting() as parse:
            self.inventory()
            self.write_skill(SKILL + '\nChanged.\n')
            edited = self.inventory()
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(edited[3]['content'], SKILL + '\nChanged.\n')

            # A new file joins the package's file set.
            extra = self.tree / 'roles' / 'demo' / 'skills' / 'greeting' / 'notes.md'
            extra.write_text('note\n')
            self.inventory()
            self.assertEqual(parse.call_count, 3)

            # Removing it changes the set again.
            extra.unlink()
            self.inventory()
            self.assertEqual(parse.call_count, 4)

            # A special entry is recorded separately from files; it must invalidate too.
            link = self.tree / 'roles' / 'demo' / 'linked'
            os.symlink('agentic.yaml', link)
            self.inventory()
            self.assertEqual(parse.call_count, 5)
            link.unlink()
            self.inventory()
            self.assertEqual(parse.call_count, 6)

    def test_a_legacy_layout_flag_alone_rebuilds_the_parse(self):
        real = catalog_api.DiskSource

        class ForcedLegacy:
            """The same bytes and entries, but the source reports legacy scaffolding."""

            def __init__(self, *args, **kwargs):
                self.source = real(*args, **kwargs)
                self.source.legacy_nonempty = True

            def __getattr__(self, name):
                return getattr(self.source, name)

        with self.counting() as parse:
            self.inventory()
            with mock.patch.object(catalog_api, 'DiskSource', ForcedLegacy):
                self.inventory()
            self.assertEqual(parse.call_count, 2)
            # The forced state published its own entry; the ordinary flag differs from it again.
            self.inventory()
            self.assertEqual(parse.call_count, 3)

    def test_a_change_during_a_snapshot_conflicts_without_poisoning_the_cache(self):
        with self.counting() as parse:
            self.inventory()
            self.assertEqual(parse.call_count, 1)
            with self.assertRaises(RevisionConflictError):
                with self.layer.catalog.read_snapshot():
                    self.write_skill(SKILL + '\nEdited during the snapshot.\n')
            # The conflict published nothing; the changed bytes are parsed next.
            changed = self.inventory()
            self.assertEqual(parse.call_count, 2)
            self.assertEqual(changed[3]['content'], SKILL + '\nEdited during the snapshot.\n')
            self.inventory()
            self.assertEqual(parse.call_count, 2)

    def test_nested_snapshots_reuse_the_active_catalog(self):
        with self.counting() as parse:
            with self.layer.catalog.read_snapshot() as outer:
                with self.layer.catalog.read_snapshot() as inner:
                    listed = self.layer.catalog.list()
                self.assertEqual(outer, inner)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual([row['ref'] for row in listed['entries']], ['demo/greeting'])

    def test_a_hit_attaches_its_own_source_and_never_reads_cached_bytes(self):
        self.inventory()  # publishes a parse and reads the body through that snapshot
        entered = threading.Event()
        observed = {}

        def pinned():
            try:
                with self.layer.catalog.read_snapshot():
                    observed['catalog'] = self.layer.catalog._active_catalog()
                    entered.set()
                    time.sleep(0.2)  # Long enough for the edit below to land.
                    observed['resource'] = self.layer.catalog.read_resource(
                        'demo', 'skills/greeting/SKILL.md')
            except RevisionConflictError as exc:
                observed['conflict'] = exc

        thread = threading.Thread(target=pinned)
        thread.start()
        self.assertTrue(entered.wait(DEADLINE))
        self.write_skill(SKILL + '\nEdited while pinned.\n')
        thread.join(DEADLINE)
        self.assertFalse(thread.is_alive())
        # A fresh source verifies the current bytes, so a shared old read cache cannot hide the edit.
        self.assertIn('conflict', observed)
        self.assertNotIn('resource', observed)

    def test_concurrent_snapshots_share_parsed_containers_but_not_sources(self):
        self.inventory()  # One published parse both concurrent snapshots can hit.
        barrier = threading.Barrier(2, timeout=DEADLINE)
        pinned = []

        def snapshot():
            with self.layer.catalog.read_snapshot():
                catalog = self.layer.catalog._active_catalog()
                barrier.wait()
                pinned.append(catalog)

        threads = [threading.Thread(target=snapshot) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(DEADLINE)
        self.assertEqual(len(pinned), 2)
        self.assertIsNot(pinned[0].source, pinned[1].source)
        self.assertIs(pinned[0].packages, pinned[1].packages)
        self.assertIs(pinned[0].entries, pinned[1].entries)
        for catalog in pinned:
            self.assertEqual(catalog.revision, pinned[0].revision)


if __name__ == '__main__':
    unittest.main()