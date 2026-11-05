"""The host launcher's argv construction and refusals; the product is never started.

`start.sh argv` and `start.sh foreground` (with a stub interpreter that only prints what it receives) are the
only things executed. A decoy sibling checkout proves nothing is searched for: if the launcher ever fell back to
`../biz-core/.venv/bin/python` the decoy would run and mark a file."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'start.sh'
SERVER = SCRIPT.with_name('server.py')

SELECTION = {
    'INSTANCE_ROOT': '/fixture/instance',
    'WORKSPACE': '/fixture/workspace',
    'COMPONENT_APP': '/fixture/code/app',
    'COMPONENT_DASHBOARD': '/fixture/code/biz-dashboard',
}


class Launch:
    """A copy of start.sh inside a throw-away tree that also holds a decoy sibling `biz-core` interpreter."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.dashboard = tmp_path / 'biz-dashboard'
        self.dashboard.mkdir()
        self.script = self.dashboard / 'start.sh'
        shutil.copy(SCRIPT, self.script)
        self.marker = tmp_path / 'decoy-ran'
        decoy = tmp_path / 'biz-core' / '.venv' / 'bin' / 'python'
        decoy.parent.mkdir(parents=True)
        decoy.write_text(f'#!/bin/sh\ntouch {self.marker}\n')
        decoy.chmod(0o700)
        self.interpreter = tmp_path / 'host-python'
        self.interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$@"; env\n')
        self.interpreter.chmod(0o700)

    def conf(self, *, drop=(), extra=None, interpreter=True, **overrides):
        values = {'HOST': '127.0.0.1', 'PORT': '1771', **SELECTION}
        if interpreter:
            values['INTERPRETER'] = str(self.interpreter)
        values.update(overrides)
        for key in drop:
            values.pop(key)
        lines = [f'{key}={value}' for key, value in values.items()] + list(extra or ())
        path = self.root / 'private.env'
        path.write_text('\n'.join(lines) + '\n')
        return path

    def run(self, mode, conf, **env):
        merged = {**os.environ, 'BIZ_DASH_CONF': str(conf), 'BIZ_DASH_STATE': str(self.root / 'state'), **env}
        for key in ('BIZ_DASH_PYTHON', 'BIZ_DASH_WORKSPACE', 'BIZ_DASH_LAUNCHER'):
            merged.pop(key, None) if key not in env else None
        return subprocess.run(['bash', str(self.script), mode], env=merged, capture_output=True, text=True, timeout=10)


@pytest.fixture
def launch(tmp_path):
    return Launch(tmp_path)


def argv_of(result):
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_argv_is_the_explicit_interpreter_workspace_instance_root_and_components_in_a_fixed_order(launch):
    conf = launch.conf(COMPONENT_OPERATIONS_ENGINE='/fixture/code/qorqut-operations',
                       COMPONENT_ROLES='/fixture/qorqut-roles/agent-roles', extra=['QORQUT_TERMINAL_WORKSPACE=/fixture/term'])
    assert argv_of(launch.run('argv', conf)) == [
        str(launch.interpreter), '-I', '-u', '-m', 'bazarlyk.service',
        '--workspace', '/fixture/workspace', '--instance-root', '/fixture/instance',
        '--component', 'app=/fixture/code/app',
        '--component', 'operations_engine=/fixture/code/qorqut-operations',
        '--component', 'dashboard=/fixture/code/biz-dashboard',
        '--component', 'roles=/fixture/qorqut-roles/agent-roles',
        '--host', '127.0.0.1', '--port', '1771']
    assert not launch.marker.exists()


def test_the_environment_override_names_still_select_interpreter_and_workspace(launch):
    conf = launch.conf(interpreter=False, WORKSPACE='/fixture/ignored')
    other = launch.root / 'other-python'
    other.write_text('#!/bin/sh\n')
    argv = argv_of(launch.run('argv', conf, BIZ_DASH_PYTHON=str(other), BIZ_DASH_WORKSPACE='/fixture/override'))
    assert argv[0] == str(other) and argv[argv.index('--workspace') + 1] == '/fixture/override'


def test_a_stable_application_launcher_receives_the_same_selection_without_an_interpreter(launch):
    stable = launch.root / 'stable-launcher'
    stable.write_text('#!/bin/sh\n')
    argv = argv_of(launch.run('argv', launch.conf(interpreter=False), BIZ_DASH_LAUNCHER=str(stable)))
    assert argv[:3] == [str(stable), '--workspace', '/fixture/workspace']
    assert '-m' not in argv and argv[-4:] == ['--host', '127.0.0.1', '--port', '1771']


def test_foreground_runs_exactly_the_constructed_argv_with_a_clean_environment(launch):
    conf = launch.conf(extra=['FIXTURE_FLAG=kept'])
    expected = argv_of(launch.run('argv', conf))
    result = launch.run('foreground', conf, CLAUDE_CODE_SESSION_ID='must-not-inherit')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:len(expected) - 1] == expected[1:]  # the stub prints its arguments, then env
    assert 'FIXTURE_FLAG=kept' in result.stdout and 'CLAUDE_CODE_SESSION_ID' not in result.stdout
    assert 'INSTANCE_ROOT=' not in result.stdout and 'COMPONENT_APP=' not in result.stdout and 'INTERPRETER=' not in result.stdout
    assert not launch.marker.exists()


@pytest.mark.parametrize('drop, message', [
    ('INSTANCE_ROOT', 'no instance root selected'),
    ('WORKSPACE', 'no workspace selected'),
    ('COMPONENT_APP', 'no app component selected'),
    ('COMPONENT_DASHBOARD', 'no dashboard component selected'),
])
def test_a_missing_selection_is_refused(launch, drop, message):
    for mode in ('argv', 'foreground'):
        result = launch.run(mode, launch.conf(drop=[drop]))
        assert result.returncode != 0 and message in result.stderr and result.stdout == ''
    assert not launch.marker.exists() and not (launch.root / 'state').exists()


def test_no_interpreter_is_never_replaced_by_a_sibling_checkout(launch):
    for mode in ('argv', 'foreground', 'start'):
        result = launch.run(mode, launch.conf(interpreter=False))
        assert result.returncode != 0 and 'no host interpreter selected' in result.stderr
    assert not launch.marker.exists()
    assert not (launch.dashboard / '.state').exists() and not (launch.root / 'state').exists()


@pytest.mark.parametrize('overrides, message', [
    ({'INSTANCE_ROOT': 'instance'}, 'INSTANCE_ROOT must be an absolute path'),
    ({'WORKSPACE': '../workspace'}, 'WORKSPACE must be an absolute path'),
    ({'COMPONENT_APP': 'app'}, 'COMPONENT_APP must be an absolute path'),
    ({'COMPONENT_DASHBOARD': '/fixture/../biz-dashboard'}, 'COMPONENT_DASHBOARD must be a normalized absolute path'),
    ({'INSTANCE_ROOT': '/fixture//instance'}, 'INSTANCE_ROOT must be a normalized absolute path'),
    ({'INSTANCE_ROOT': '/'}, 'INSTANCE_ROOT must not be the filesystem root'),
    ({'INTERPRETER': 'python3'}, 'INTERPRETER must be an absolute path'),
    ({'PORT': 'http'}, 'PORT must be a number'),
])
def test_a_relative_or_malformed_selection_is_refused(launch, overrides, message):
    result = launch.run('argv', launch.conf(**overrides))
    assert result.returncode != 0 and message in result.stderr and result.stdout == ''


@pytest.mark.parametrize('extra, message', [
    (['COMPONENT_BOGUS=/fixture/x'], 'unknown component'),
    (['COMPONENT_APP=/fixture/other/app'], 'component app is selected more than once'),
    (['BIZ_ROOT=/fixture/businesses'], 'BIZ_ROOT is retired'),
    (['BIZ_DIRS=/fixture/one'], 'BIZ_DIRS is retired'),
])
def test_unknown_duplicate_and_retired_selections_are_refused(launch, extra, message):
    result = launch.run('argv', launch.conf(extra=extra))
    assert result.returncode != 0 and message in result.stderr and result.stdout == ''


def test_server_py_does_not_look_for_a_sibling_checkout(launch):
    shutil.copy(SERVER, launch.dashboard / 'server.py')
    decoy = launch.root / 'biz-core' / 'bazarlyk' / 'service'
    decoy.mkdir(parents=True)
    (decoy.parent / '__init__.py').write_text('')
    (decoy / '__init__.py').write_text('')
    (decoy / 'main.py').write_text(f'from pathlib import Path\nPath({str(launch.marker)!r}).touch()\ndef main(): return 0\n')
    result = subprocess.run([sys.executable, '-I', str(launch.dashboard / 'server.py'), '--port', '1'],
                            capture_output=True, text=True, timeout=30, cwd=launch.root)
    assert result.returncode != 0 and 'not importable by this interpreter' in result.stderr
    assert not launch.marker.exists()


def test_server_py_forwards_its_arguments_unchanged_to_an_importable_host(launch):
    shutil.copy(SERVER, launch.dashboard / 'server.py')
    host = launch.root / 'installed' / 'bazarlyk' / 'service'
    host.mkdir(parents=True)
    (host.parent / '__init__.py').write_text('')
    (host / '__init__.py').write_text('')
    (host / 'main.py').write_text('import json, sys\n'
                                  'def main(argv=None):\n'
                                  '    print(json.dumps({"argv": sys.argv[1:], "sibling": any("biz-core" in p for p in sys.path)}))\n'
                                  '    return 0\n')
    arguments = ['--workspace', '/fixture/workspace', '--instance-root', '/fixture/instance', '--component', 'app=/fixture/app']
    result = subprocess.run([sys.executable, str(launch.dashboard / 'server.py'), *arguments], capture_output=True, text=True,
                            timeout=30, cwd=launch.root, env={**os.environ, 'PYTHONPATH': str(launch.root / 'installed')})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '{"argv": %s, "sibling": false}' % str(arguments).replace("'", '"')
