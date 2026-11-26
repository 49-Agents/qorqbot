"""Every installation whose runtime carries the WhatsApp connector has it selected, once, without a manual step.

    python3.12 -m unittest discover -s tools/distribution/tests -v

A new installation selects it when initialized; one initialized without it adopts it through the offline extension
installer (a stand-in here: the real one is exercised against a real instance). A runtime with no helper selects nothing.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402
from native_launch import OPENROUTER_FACTORY, TREG_FACTORY, WHATSAPP_FACTORY, select_whatsapp  # noqa: E402

WHEEL = {'key': 'qorqut-whatsapp', 'name': 'qorqut-whatsapp', 'version': '0.1.0', 'sha256': 'a' * 64,
         'file': 'wheels/qorqut_whatsapp-0.1.0-py3-none-linux_x86_64.whl'}
WHATSAPP = {'factory': WHATSAPP_FACTORY, 'config': {}}
TREG = {'factory': TREG_FACTORY, 'config': {}}
OPENROUTER = {'factory': OPENROUTER_FACTORY, 'config': {}}


class FakeInstaller:
    """Stands in for ``qorqut.bootstrap.extension_install``: records its command line, then completes, refuses or stops."""

    def __init__(self, config, outcome='complete'):
        self.config, self.outcome, self.calls = config, outcome, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        journal = self.config / 'extension-install.json'
        request = {'entry': WHATSAPP, 'bundle_sha256': argv[argv.index('--bundle-sha256') + 1]}
        if self.outcome == 'refused':
            return subprocess.CompletedProcess(argv, 1, '', 'Extension installation refused or interrupted: conflicts\n')
        if self.outcome == 'interrupted':
            journal.write_text(json.dumps({'phase': 'backed-up', 'request': request}))
            return subprocess.CompletedProcess(argv, 1, '', 'Extension installation refused or interrupted: stopped\n')
        selected = json.loads((self.config / 'extensions.json').read_text())
        (self.config / 'extensions.json').write_text(json.dumps({'extensions': [*selected['extensions'], WHATSAPP]}))
        journal.write_text(json.dumps({'phase': 'complete', 'request': request}))
        return subprocess.CompletedProcess(argv, 0, '{"state": "installed"}\n', '')


class WhatsAppSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'runtime').mkdir()
        (self.root / '.run').mkdir()
        self.config = self.root / 'instance/config'

    def install(self, extensions, selection=None, wheels=(WHEEL,)):
        (self.root / 'runtime/PREPARATION-RECEIPT.json').write_text(json.dumps(
            {'selection': {'treg': True, 'openrouter': True, 'whatsapp': True} if selection is None else selection,
             'wheels': list(wheels)}))
        if extensions is not None:  # None: a new installation, its instance folder not yet created
            self.config.mkdir(parents=True)
            (self.config / 'extensions.json').write_text(json.dumps({'extensions': extensions}))

    def selected(self):
        return json.loads((self.config / 'extensions.json').read_text())['extensions']

    def select(self, installer):
        with mock.patch('subprocess.run', installer), mock.patch('builtins.print'):
            return select_whatsapp(self.root, {})

    def test_a_new_installation_is_initialized_with_whatsapp(self):
        self.install(None)
        commands = []
        with mock.patch.object(native_launch, 'run', lambda argv, **kwargs: commands.append(argv)):
            with self.assertRaises(FileNotFoundError):  # the stand-in initializer writes no receipt
                native_launch.initialize(self.root, {'QORQUT_TIMEZONE': 'UTC'})
        argv = [str(part) for part in commands[0]]
        initial = Path(argv[argv.index('--extensions-config') + 1])
        self.assertEqual(json.loads(initial.read_text()), {'extensions': [WHATSAPP]})
        self.assertEqual(initial.parent, self.root / '.run')

    def test_a_runtime_without_the_helper_initializes_and_selects_nothing(self):
        self.install(None, selection={'treg': True, 'openrouter': True}, wheels=())
        commands = []
        with mock.patch.object(native_launch, 'run', lambda argv, **kwargs: commands.append(argv)):
            with self.assertRaises(FileNotFoundError):
                native_launch.initialize(self.root, {'QORQUT_TIMEZONE': 'UTC'})
        self.assertNotIn('--extensions-config', [str(part) for part in commands[0]])
        self.config.mkdir(parents=True)
        (self.config / 'extensions.json').write_text(json.dumps({'extensions': []}))
        installer = FakeInstaller(self.config)
        self.assertFalse(self.select(installer))
        self.assertEqual((installer.calls, self.selected()), ([], []))

    def test_an_installation_that_has_it_is_left_alone(self):
        self.install([WHATSAPP, TREG, OPENROUTER])
        installer = FakeInstaller(self.config)
        self.assertFalse(self.select(installer))
        self.assertEqual((installer.calls, self.selected()), ([], [WHATSAPP, TREG, OPENROUTER]))

    def test_an_updated_installation_adopts_it_once_through_the_extension_installer(self):
        self.install([TREG, OPENROUTER])
        installer = FakeInstaller(self.config)
        self.assertTrue(self.select(installer))
        argv = [str(part) for part in installer.calls[0]]
        self.assertEqual(argv[1:5], ['-I', '-B', '-m', 'qorqut.bootstrap.extension_install'])
        self.assertEqual(argv[argv.index('--factory') + 1], WHATSAPP_FACTORY)
        self.assertEqual(argv[argv.index('--bundle-sha256') + 1], WHEEL['sha256'])
        self.assertEqual(argv[argv.index('--prepared-root') + 1], str(self.root / 'runtime'))
        self.assertNotIn('--resume', argv)
        self.assertEqual(self.selected(), [TREG, OPENROUTER, WHATSAPP])
        self.assertFalse(self.select(installer))
        self.assertEqual(len(installer.calls), 1)

    def test_a_refusal_changes_nothing_and_the_installation_still_starts(self):
        self.install([TREG])
        self.assertFalse(self.select(FakeInstaller(self.config, 'refused')))
        self.assertEqual(self.selected(), [TREG])

    def test_an_interrupted_adoption_stops_the_start_and_is_resumed_next_time(self):
        self.install([TREG])
        with self.assertRaisesRegex(RuntimeError, 'start Qorqbot again'):
            self.select(FakeInstaller(self.config, 'interrupted'))
        installer = FakeInstaller(self.config)
        self.assertTrue(self.select(installer))
        self.assertIn('--resume', [str(part) for part in installer.calls[0]])
        self.assertEqual(self.selected(), [TREG, WHATSAPP])

    def test_another_connectors_unfinished_installation_is_not_touched(self):
        self.install([TREG])
        (self.config / 'extension-install.json').write_text(json.dumps(
            {'phase': 'prepared', 'request': {'entry': {'factory': 'qorqut_telegram.extension:create', 'config': {}}}}))
        installer = FakeInstaller(self.config)
        self.assertFalse(self.select(installer))
        self.assertEqual(installer.calls, [])


if __name__ == '__main__':
    unittest.main()
