"""Every installation whose runtime carries the OpenRouter extension has it selected, once, beside the other selections.

    python3.12 -m unittest discover -s tools/distribution/tests -v
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_launch import OPENROUTER_FACTORY, TREG_FACTORY, select_openrouter, select_treg  # noqa: E402

CALENDAR = {'factory': 'qorqut_google_calendar.extension:create', 'config': {}}
OPENROUTER = {'factory': OPENROUTER_FACTORY, 'config': {}}
TREG = {'factory': TREG_FACTORY, 'config': {}}


class OpenRouterSelectionTests(unittest.TestCase):
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

    def test_a_new_or_updated_installation_gets_openrouter_once_beside_treg(self):
        self.install({'google_calendar': True, 'treg': True, 'openrouter': True}, [CALENDAR])
        self.assertTrue(select_treg(self.root))
        self.assertTrue(select_openrouter(self.root))
        self.assertEqual(self.selected(), [CALENDAR, TREG, OPENROUTER])
        self.assertFalse(select_openrouter(self.root))
        self.assertFalse(select_treg(self.root))
        self.assertEqual(len(self.selected()), 3)

    def test_an_owners_own_openrouter_configuration_is_left_alone(self):
        own = {'factory': OPENROUTER_FACTORY, 'config': {'transcription_model': 'openai/whisper-1'}}
        self.install({'openrouter': True}, [own])
        self.assertFalse(select_openrouter(self.root))
        self.assertEqual(self.selected(), [own])

    def test_a_runtime_without_the_openrouter_wheel_selects_nothing(self):
        self.install({'google_calendar': True, 'treg': True}, [])
        self.assertFalse(select_openrouter(self.root))
        self.assertEqual(self.selected(), [])
        (self.root / 'runtime/PREPARATION-RECEIPT.json').unlink()
        self.assertFalse(select_openrouter(self.root))


if __name__ == '__main__':
    unittest.main()
