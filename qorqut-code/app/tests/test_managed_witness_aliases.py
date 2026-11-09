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


if __name__ == '__main__':
    unittest.main()
