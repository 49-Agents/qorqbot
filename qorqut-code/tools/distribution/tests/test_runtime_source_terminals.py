"""The installed runtime must carry the legacy Terminals mode's files.

    python3.12 -m unittest discover -s tools/distribution/tests -p 'test_runtime_source_terminals.py' -v
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_source  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[4]


class RequiredAssets(unittest.TestCase):
    def test_the_dashboard_broker_is_a_required_native_helper(self):
        self.assertIn('qorqut-code/dashboard/pty_broker.py', runtime_source.REQUIRED_RUNTIME_ASSETS['native-helpers'])

    def test_the_terminal_view_its_switch_and_the_emulator_licence_are_required(self):
        required = set(runtime_source.REQUIRED_RUNTIME_ASSETS['legacy-terminals'])
        for name in ('js/terminal.js', 'js/xterm.js', 'css/xterm.css', 'js/agent-start.js', 'js/agent-mode.js', 'css/agent-mode.css', 'js/agent-terminal-view.js',
                     'js/agent-trust-row.js', 'css/legacy-terminals.css', 'LICENSES/xterm-MIT.txt', 'THIRD-PARTY-NOTICES.md'):
            self.assertIn('qorqut-code/dashboard/' + name, required)

    def test_every_required_file_exists_in_the_checkout(self):
        missing = [path for paths in runtime_source.REQUIRED_RUNTIME_ASSETS.values() for path in paths
                   if not (REPOSITORY / path).exists()]
        self.assertEqual(missing, [])

    def test_none_of_them_is_excluded_from_the_runtime(self):
        for path in runtime_source.REQUIRED_RUNTIME_ASSETS['legacy-terminals']:
            local = path.split('/', 2)[2]
            self.assertIsNone(runtime_source.exclusion('dashboard', local), path)


if __name__ == '__main__':
    unittest.main()
