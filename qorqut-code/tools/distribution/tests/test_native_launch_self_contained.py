"""A self-contained package ships its Python environment, tmux and git; nothing is built or installed on the user's computer.

    python3.12 -m unittest discover -s tools/distribution/tests -v

No real Python environment or program is used: the shipped trees are small stand-ins.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_build  # noqa: E402
import native_launch  # noqa: E402


def write(path, text='', mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


class Folder(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / 'qorqbot'
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(parents=True)

    def ship(self, bundled=True):
        """A package folder as unpacked: one listed file, and (when self-contained) the two shipped trees."""
        write(self.runtime / 'launch.py', '# launcher\n')
        manifest = {'format': 1, 'version': '9.9.9', 'platform': 'macos14-arm64',
                    'files': {'launch.py': native_launch.hashlib.sha256(b'# launcher\n').hexdigest()}}
        if bundled:
            write(self.runtime / 'venv/bin/python3.12', '#!/bin/sh\n', 0o755)
            (self.runtime / 'venv/bin/python').symlink_to('python3.12')
            write(self.runtime / 'venv/lib/python3.12/site-packages/qorqut/__init__.py', 'VALUE = 1\n')
            write(self.runtime / 'native/bin/git', '#!/bin/sh\n', 0o755)
            write(self.runtime / 'native/bin/tmux', '#!/bin/sh\n', 0o755)
            manifest['bundled'] = {name: native_launch.tree_digest(self.runtime / name) for name in ('native', 'venv')}
        write(self.runtime / 'PAYLOAD.json', json.dumps(manifest))
        return manifest


class TreeDigest(Folder):
    def test_content_program_bit_and_link_target_each_change_the_digest(self):
        self.ship()
        tree = self.runtime / 'native'
        first = native_launch.tree_digest(tree)
        self.assertEqual(first, native_launch.tree_digest(tree))
        (tree / 'bin/git').write_text('#!/bin/sh\nexit 1\n')
        changed = native_launch.tree_digest(tree)
        self.assertNotEqual(first, changed)
        (tree / 'bin/git').chmod(0o644)
        without_program_bit = native_launch.tree_digest(tree)
        self.assertNotEqual(changed, without_program_bit)
        (tree / 'bin/git-upload-pack').symlink_to('git')
        linked = native_launch.tree_digest(tree)
        self.assertNotEqual(without_program_bit, linked)
        (tree / 'bin/git-upload-pack').unlink()
        (tree / 'bin/git-upload-pack').symlink_to('tmux')
        self.assertNotEqual(linked, native_launch.tree_digest(tree))

    def test_group_and_other_permission_bits_do_not_matter(self):
        # Unpacking applies the user's umask, so only the owner's program bit is part of the identity.
        self.ship()
        tree = self.runtime / 'native'
        first = native_launch.tree_digest(tree)
        (tree / 'bin/git').chmod(0o700)
        self.assertEqual(first, native_launch.tree_digest(tree))


class Verify(Folder):
    def test_a_package_without_shipped_trees_is_checked_as_before(self):
        self.ship(bundled=False)
        self.assertEqual(native_launch.bundled(self.runtime), {})
        self.assertEqual(native_launch.verify(self.runtime)['version'], '9.9.9')

    def test_changed_shipped_tools_are_refused(self):
        self.ship()
        native_launch.verify(self.runtime)
        (self.runtime / 'native/bin/tmux').write_text('#!/bin/sh\necho other\n')
        with self.assertRaisesRegex(RuntimeError, 'Package checksum failed: native'):
            native_launch.verify(self.runtime)

    def test_the_environment_is_checked_as_shipped_but_not_once_in_use(self):
        self.ship()
        addition = self.runtime / 'venv/lib/python3.12/site-packages/extension.py'
        addition.write_text('ADDED = True\n')
        with self.assertRaisesRegex(RuntimeError, 'Package checksum failed: venv'):
            native_launch.verify(self.runtime)
        # After first use an optional extension may be installed into it; the tools are still checked.
        write(self.runtime / 'ENVIRONMENT.json', json.dumps({'format': 1, 'root': str(self.root)}))
        native_launch.verify(self.runtime)
        (self.runtime / 'native/bin/git').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'Package checksum failed: native'):
            native_launch.verify(self.runtime)

    def test_an_unknown_shipped_tree_name_is_refused(self):
        manifest = self.ship()
        manifest['bundled']['instance'] = 'x'
        write(self.runtime / 'PAYLOAD.json', json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, 'Unsupported package manifest'):
            native_launch.verify(self.runtime)


class Prepare(Folder):
    def test_a_ready_made_environment_is_used_as_it_is(self):
        self.ship()
        with mock.patch.object(native_launch, 'run') as run, mock.patch('builtins.print'):
            native_launch.prepare(self.root, {})
        run.assert_not_called()  # no venv creation, no pip
        self.assertEqual(json.loads((self.runtime / 'ENVIRONMENT.json').read_text()), {'format': 1, 'root': str(self.root)})
        with mock.patch.object(native_launch, 'verify') as verify:
            native_launch.prepare(self.root, {})  # later starts only confirm the folder has not moved
        verify.assert_not_called()

    def test_a_changed_ready_made_environment_is_never_started(self):
        self.ship()
        (self.runtime / 'venv/lib/python3.12/site-packages/qorqut/__init__.py').write_text('VALUE = 2\n')
        with mock.patch('builtins.print'), self.assertRaisesRegex(RuntimeError, 'Package checksum failed: venv'):
            native_launch.prepare(self.root, {})
        self.assertFalse((self.runtime / 'ENVIRONMENT.json').exists())

    def test_prerequisites_ask_for_nothing_when_the_package_ships_its_own(self):
        with mock.patch.object(native_launch.shutil, 'which', return_value=None):
            native_launch.require_prerequisites({'PATH': '/nowhere'}, self_contained=True)
            with self.assertRaisesRegex(RuntimeError, 'Git is required'):
                native_launch.require_prerequisites({'PATH': '/nowhere'})


class Environment(Folder):
    def environment(self):
        home = self.root / 'instance/home'
        with mock.patch.dict(os.environ, {'HOME': str(self.root.parent / 'user'), 'PATH': '/usr/bin:/bin', 'QORQUT_TIMEZONE': 'UTC'}, clear=True), \
                mock.patch.object(native_launch.sys, 'platform', 'linux'):
            return native_launch.environment(self.root, home)

    def test_shipped_tools_come_before_the_computers_own(self):
        self.ship()
        write(self.runtime / 'native/bin/agent-browser', '#!/bin/sh\n', 0o755)
        env = self.environment()
        self.assertEqual(env['PATH'].split(os.pathsep),
                         [str(self.runtime / 'venv/bin'), str(self.runtime / 'native/bin'), '/usr/bin', '/bin'])
        self.assertEqual(env['QORQUT_AGENT_BROWSER_EXECUTABLE'], str(self.runtime / 'native/bin/agent-browser'))

    def test_a_package_without_shipped_tools_keeps_its_path(self):
        self.ship(bundled=False)
        env = self.environment()
        self.assertEqual(env['PATH'].split(os.pathsep), [str(self.runtime / 'venv/bin'), '/usr/bin', '/bin'])
        self.assertNotIn('QORQUT_AGENT_BROWSER_EXECUTABLE', env)

    def test_the_shipped_browser_tool_is_never_downloaded_again(self):
        self.ship()
        tool = write(self.runtime / 'native/bin/agent-browser', '#!/bin/sh\n', 0o755)
        download = mock.Mock(side_effect=AssertionError('no download expected'))
        self.assertEqual(native_launch.ensure_agent_browser(self.root, download=download), (tool, None))


class Platform(Folder):
    def allowed(self, target, system, machine, mac='15.1'):
        write(self.runtime / 'PAYLOAD.json', json.dumps({'format': 1, 'platform': target, 'files': {}}))
        with mock.patch.object(native_launch.platform, 'system', return_value=system), \
                mock.patch.object(native_launch.platform, 'machine', return_value=machine), \
                mock.patch.object(native_launch.platform, 'mac_ver', return_value=(mac, ('', '', ''), machine)):
            try:
                native_launch.require_platform(self.runtime)
            except RuntimeError:
                return False
            return True

    def test_each_mac_package_runs_only_on_its_own_chip(self):
        self.assertTrue(self.allowed('macos14-arm64', 'Darwin', 'arm64'))
        self.assertTrue(self.allowed('macos14-x86_64', 'Darwin', 'x86_64'))
        self.assertFalse(self.allowed('macos14-arm64', 'Darwin', 'x86_64'))
        self.assertFalse(self.allowed('macos14-x86_64', 'Darwin', 'arm64'))
        self.assertFalse(self.allowed('macos14-x86_64', 'Darwin', 'x86_64', mac='13.6'))
        self.assertFalse(self.allowed('macos14-x86_64', 'Linux', 'x86_64'))

    def test_the_browser_tool_exists_for_both_mac_chips(self):
        self.assertEqual(native_launch.agent_browser_binary_name('Darwin', 'arm64'), 'agent-browser-darwin-arm64')
        self.assertEqual(native_launch.agent_browser_binary_name('Darwin', 'x86_64'), 'agent-browser-darwin-x64')


class StartCommand(Folder):
    """The ./qorqbot shell command chooses the Python that runs the launcher."""

    def command(self, env_python=None):
        write(self.root / 'qorqbot', native_build.LAUNCHER, 0o755)
        write(self.runtime / 'launch.py', '')
        env = {'PATH': '/usr/bin:/bin'}
        if env_python:
            env['QORQUT_PYTHON'] = str(env_python)
        return subprocess.run([str(self.root / 'qorqbot'), 'status'], env=env, capture_output=True, text=True)

    def python(self, path, label):
        return write(path, f'#!/bin/sh\necho "{label} $*"\n', 0o755)

    def test_a_self_contained_folder_runs_on_its_own_python(self):
        self.python(self.runtime / 'venv/bin/python3.12', 'own')
        (self.runtime / 'native').mkdir()
        done = self.command()
        self.assertEqual(done.stdout.strip(), f'own -I -B {self.runtime}/launch.py status')

    def test_an_interrupted_update_still_finds_the_previous_python(self):
        previous = self.root / '.previous-runtime'
        self.python(previous / 'venv/bin/python3.12', 'previous')
        (previous / 'native').mkdir()
        self.runtime.rename(self.root / 'runtime-aside')
        write(self.root / 'qorqbot', native_build.LAUNCHER, 0o755)
        done = subprocess.run([str(self.root / 'qorqbot'), 'update', '--recover'], env={'PATH': '/usr/bin:/bin'},
                              capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), f'previous -I -B {previous}/update.py --recover')

    def test_an_explicit_python_wins(self):
        self.python(self.runtime / 'venv/bin/python3.12', 'own')
        (self.runtime / 'native').mkdir()
        chosen = self.python(self.root.parent / 'chosen-python', 'chosen')
        self.assertTrue(self.command(env_python=chosen).stdout.startswith('chosen '))

    def test_a_folder_without_shipped_tools_uses_the_computers_python(self):
        # A Linux package builds its environment in runtime/venv from the computer's Python 3.12.
        self.python(self.runtime / 'venv/bin/python3.12', 'venv')
        done = self.command()
        self.assertFalse(done.stdout.startswith('venv '))


class RelocatedCommands(unittest.TestCase):
    def test_commands_run_the_python_beside_them_wherever_the_folder_is(self):
        with tempfile.TemporaryDirectory() as temporary:
            venv = Path(temporary).resolve() / 'build/runtime/venv'
            body = 'import sys\nprint("ran", sys.argv[1])\n'
            write(venv / 'bin/short', f'#!{venv}/bin/python3.12\n{body}', 0o755)
            write(venv / 'bin/long', f"#!/bin/sh\n'''exec' \"{venv}/bin/python3.12\" \"$0\" \"$@\"\n' '''\n{body}", 0o755)
            own = "#!/bin/sh\n'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/python3.12\" \"$0\" \"$@\"\n' '''\n" + body
            write(venv / 'bin/pip3', own, 0o755)
            write(venv / 'bin/activate', 'VIRTUAL_ENV=x\n')
            (venv / 'bin/python3.12').symlink_to(sys.executable)
            self.assertEqual(native_build.relocate_scripts(venv), ['long', 'short'])
            self.assertEqual((venv / 'bin/pip3').read_text(), own)
            moved = Path(temporary).resolve() / 'elsewhere/venv'
            moved.parent.mkdir()
            venv.rename(moved)
            for name in ('short', 'long'):
                text = (moved / 'bin' / name).read_text()
                self.assertNotIn(str(venv), text)
                done = subprocess.run([str(moved / 'bin' / name), 'here'], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
                self.assertEqual((done.returncode, done.stdout.strip()), (0, 'ran here'), done.stderr)

    def test_an_unrecognized_command_start_stops_the_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            venv = Path(temporary).resolve() / 'venv'
            write(venv / 'bin/odd', f'#!/usr/bin/env -S {venv}/bin/python3.12 -u\nprint(1)\n'.replace('#!/usr/bin/env', '#!/bin/sh\nexec'), 0o755)
            with self.assertRaisesRegex(ValueError, 'Unrecognized command start in odd'):
                native_build.relocate_scripts(venv)


if __name__ == '__main__':
    unittest.main()
