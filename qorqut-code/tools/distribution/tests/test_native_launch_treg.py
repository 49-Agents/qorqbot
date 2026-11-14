"""Every installation whose runtime carries the treg toolkit has it selected, once, without touching other selections.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_launch import TREG_FACTORY, select_treg  # noqa: E402

CALENDAR = {'factory': 'qorqut_google_calendar.extension:create', 'config': {}}


class TregSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / 'runtime').mkdir()
        (self.root / 'instance/config').mkdir(parents=True)
        self.config = self.root / 'instance/config/extensions.json'

    def install(self, selection, extensions):
        (self.root / 'runtime/PREPARATION-RECEIPT.json').write_text(json.dumps({'selection': selection}))
        self.config.write_text(json.dumps({'extensions': extensions}))

    def selected(self):
        return json.loads(self.config.read_text())['extensions']

    def test_a_new_or_updated_installation_gets_treg_once_and_keeps_its_other_extensions(self):
        self.install({'google_calendar': True, 'treg': True}, [CALENDAR])
        self.assertTrue(select_treg(self.root))
        self.assertEqual(self.selected(), [CALENDAR, {'factory': TREG_FACTORY, 'config': {}}])
        self.assertFalse(select_treg(self.root))
        self.assertEqual(len(self.selected()), 2)

    def test_an_owners_own_treg_configuration_is_left_alone(self):
        own = {'factory': TREG_FACTORY, 'config': {'lookup_ceiling_usd': 0.5}}
        self.install({'treg': True}, [own])
        self.assertFalse(select_treg(self.root))
        self.assertEqual(self.selected(), [own])

    def test_a_runtime_without_the_treg_wheel_selects_nothing(self):
        self.install({'google_calendar': True}, [])
        self.assertFalse(select_treg(self.root))
        self.assertEqual(self.selected(), [])
        (self.root / 'runtime/PREPARATION-RECEIPT.json').unlink()
        self.assertFalse(select_treg(self.root))


if __name__ == '__main__':
    unittest.main()
