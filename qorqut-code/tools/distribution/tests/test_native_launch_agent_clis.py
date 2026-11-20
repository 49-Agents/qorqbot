"""The launcher knows where Qorqbot's downloaded Claude Code and Codex live, but leaves the service environment alone.

    python3.12 -m unittest discover -s tools/distribution/tests -v

Nothing here contacts the network (the download itself is tested in app/tests/test_agent_cli_install.py). The pins are checked
against the repository's lockfile and against the table the running service uses (app/qorqut/agent_cli_install.py).
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'app'))
import native_launch  # noqa: E402
from qorqut import agent_cli_install  # noqa: E402

NPM = Path(__file__).resolve().parents[3] / 'app/tools/native_clis'


class PinTests(unittest.TestCase):
    def test_the_pins_match_package_json_and_the_lockfile(self):
        wanted = json.loads((NPM / 'package.json').read_text())['dependencies']
        pins = {'claude': wanted['@anthropic-ai/claude-code'], 'codex': wanted['@openai/codex']}
        self.assertEqual(native_launch.AGENT_CLI_VERSIONS, pins)
        self.assertEqual(agent_cli_install.VERSIONS, pins)
        locked = json.loads((NPM / 'package-lock.json').read_text())['packages']
        for provider, rows in agent_cli_install._TARGETS.items():
            for target, (path, integrity, size, unpacked, inner) in rows.items():
                entry = next(value for value in locked.values() if value.get('resolved') == agent_cli_install._REGISTRY + path)
                self.assertEqual(entry['integrity'], 'sha512-' + integrity, (provider, target))
                self.assertEqual(entry['version'].split('-')[0], pins[provider], (provider, target))
                self.assertIn(pins[provider], path)
                self.assertTrue(inner.startswith('package/') and inner.endswith('/'))

    def test_the_launcher_finds_installed_copies_where_the_service_puts_them(self):
        self.assertEqual(native_launch.AGENT_CLI_EXECUTABLE, agent_cli_install.EXECUTABLE_ENVIRONMENT)
        self.assertEqual(native_launch.AGENT_CLI_ENTRY, agent_cli_install._ENTRY)
        for provider in agent_cli_install.PROVIDERS:
            self.assertEqual(native_launch.agent_cli_path('/r', provider), agent_cli_install.managed_path(provider, '/r'))

    def test_downloading_lives_in_the_service_only(self):
        # One installer (app/qorqut/agent_cli_install.py); the launcher keeps no second copy of the pins or the extractor.
        for name in ('ensure_claude', 'ensure_codex', 'ensure_agent_cli', 'extract_agent_cli', 'AGENT_CLI_TARGETS'):
            self.assertFalse(hasattr(native_launch, name), name)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def install(self, provider):
        path = native_launch.agent_cli_path(self.root, provider)
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/sh\n')
        path.chmod(0o755)
        return path

    def environment(self, **extra):
        with mock.patch.dict(os.environ, {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'QORQUT_TIMEZONE': 'UTC', **extra}, clear=True):
            return native_launch.environment(self.root, self.root / 'home')

    def test_the_service_environment_is_the_same_with_or_without_downloaded_copies(self):
        # The service environment reaches every process Qorqbot starts, including those running the owner's own Claude Code
        # or Codex: no updater variable, no executable override and no versioned folder on PATH goes into it. Each launch site
        # finds the downloaded copy itself (app/qorqut/agent_cli_launch.py) and applies its settings to that copy only.
        before = self.environment()
        self.install('claude')
        self.install('codex')
        after = self.environment()
        self.assertEqual(before, after)
        self.assertEqual(after['PATH'].split(os.pathsep), [str(self.root / 'runtime/venv/bin'), '/usr/bin', '/bin'])
        for name in ('QORQUT_CLAUDE_EXECUTABLE', 'QORQUT_CODEX_EXECUTABLE', 'DISABLE_AUTOUPDATER', 'DISABLE_UPDATES'):
            self.assertNotIn(name, after)
        self.assertEqual(json.loads(after['QORQUT_NATIVE_USER_ENV'])['PATH'], '/usr/bin:/bin')

    def test_an_override_the_owner_set_is_passed_on_untouched(self):
        self.install('claude')
        env = self.environment(QORQUT_CLAUDE_EXECUTABLE='/opt/mine/claude', DISABLE_AUTOUPDATER='0')
        self.assertEqual(env['QORQUT_CLAUDE_EXECUTABLE'], '/opt/mine/claude')
        self.assertEqual(env['DISABLE_AUTOUPDATER'], '0')
        self.assertNotIn('DISABLE_UPDATES', env)


if __name__ == '__main__':
    unittest.main()
