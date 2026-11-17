"""New runtime aliases must not change the instruction tree of a sealed intent."""
import unittest
from unittest.mock import patch

from qorqut.agentic.managed_witness import _WitnessInventory
from qorqut.agentic import inventory


class Snapshot:
    def __init__(self, identifier, files):
        self.id = identifier
        self.files = files
        self.classified = set()

    def exists(self, path):
        return path in self.files or any(name.startswith(path + '/') for name in self.files)

    def tree(self, root, **options):
        return [name for name in self.files if name.startswith(root + '/')]

    def read(self, path):
        return self.files[path]


class RetainedAliases(unittest.TestCase):
    root = 'app/qorqut/agentic/assets'
    prefix = 'operations/source-instructions/host/'

    def replay(self, *, witnessed_new=False, physical_new=False):
        old, new = self.root + '/old.md', self.root + '/new.md'
        aliases = {('host', path): self.prefix + path for path in (old, new)}
        host = Snapshot('host', {new: b'old physical text'} if physical_new else {})
        roles = Snapshot('roles', {self.prefix + old: b'old text',
                                  **({self.prefix + new: b'new text'} if witnessed_new else {})})
        replay = _WitnessInventory()
        replay.sources = {'host': host, 'roles': roles}
        return replay, host, aliases

    def test_new_library_alias_does_not_expand_old_witness(self):
        replay, host, aliases = self.replay()
        with patch.object(inventory.instruction_sources, 'locations', return_value=aliases):
            self.assertEqual(replay.source_paths(host, self.root), [self.root + '/old.md'])

    def test_present_unindexed_file_remains_visible_for_validation(self):
        replay, host, aliases = self.replay(witnessed_new=True)
        with patch.object(inventory.instruction_sources, 'locations', return_value=aliases):
            self.assertEqual(replay.source_paths(host, self.root),
                             [self.root + '/new.md', self.root + '/old.md'])

    def test_new_alias_does_not_replace_witnessed_physical_supplier(self):
        replay, host, aliases = self.replay(physical_new=True)
        with patch.object(inventory.instruction_sources, 'locations', return_value=aliases):
            self.assertEqual(replay.read(host, self.root + '/new.md'), b'old physical text')


class RetiredAliases(unittest.TestCase):
    """A retired alias keeps re-verifying the intents that used it, and adds nothing to a new inventory."""
    root = 'app/qorqut/agentic/assets'
    prefix = 'operations/source-instructions/host/'

    def setUp(self):
        self.kept, self.gone = self.root + '/kept.md', self.root + '/gone.md'
        aliases = {('host', path): self.prefix + path for path in (self.kept, self.gone)}
        patches = [patch.object(inventory.instruction_sources, 'locations', return_value=aliases),
                   patch.object(inventory.instruction_sources, 'retired', return_value=frozenset({('host', self.gone)}))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def sources(self, owner):
        host = Snapshot('host', {})
        roles = Snapshot('roles', {self.prefix + self.kept: b'kept text', self.prefix + self.gone: b'retained text'})
        owner.sources = {'host': host, 'roles': roles}
        return host

    def test_replay_resolves_a_retired_alias_from_its_witness(self):
        replay = _WitnessInventory()
        host = self.sources(replay)
        self.assertEqual(replay.source_paths(host, self.root), [self.gone, self.kept])
        self.assertEqual(replay.read(host, self.gone), b'retained text')

    def test_new_inventory_takes_nothing_from_a_retired_alias(self):
        current = inventory.Inventory()
        host = self.sources(current)
        self.assertEqual(current.source_paths(host, self.root), [self.kept])


class AliasIndex(unittest.TestCase):
    """The shipped alias index: entries are only ever retired, never deleted."""

    def test_retired_aliases_resolve_but_supply_nothing(self):
        from qorqut_agentic.behavior import sources
        assets = 'app/qorqut/agentic/assets'
        for name in ('working-memory.md', 'legacy-working-memory.md'):
            path = assets + '/' + name
            with self.subTest(path=path):
                self.assertIn(('host', path), sources.retired())
                self.assertEqual(sources.location('host', path), sources.ROOT + '/host/' + path)
                self.assertNotIn(path, sources.paths('host', assets))
                self.assertIn(path, sources.paths('host', assets, include_retired=True))
                with self.assertRaisesRegex(ValueError, 'Retired'):
                    sources.seed_bytes('host', path)

    def test_every_active_alias_has_its_text(self):
        from importlib.resources import files
        from qorqut_agentic.behavior import sources
        base = files('qorqut_agentic').joinpath('resources/defaults')
        for key, target in sources.locations().items():
            if key not in sources.retired():
                with self.subTest(alias=key):
                    self.assertTrue(base.joinpath(target).is_file())


if __name__ == '__main__':
    unittest.main()
