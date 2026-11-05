"""Isolated launcher inputs, no service/database/model turn."""
import os
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'start.sh'


def test_external_inputs_and_foreground_pid(tmp_path):
    launcher = tmp_path / 'app'
    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"; printf "PID=%s\\n" "$$"; env\n')
    launcher.chmod(0o700)
    conf = tmp_path / 'private.env'
    conf.write_text('HOST=127.0.0.1\nPORT=1771\nFIXTURE_FLAG=kept\nINSTANCE_ROOT=/fixture/instance\n'
                    'COMPONENT_APP=/fixture/code/app\nCOMPONENT_DASHBOARD=/fixture/code/dashboard\n')
    env = {**os.environ, 'BIZ_DASH_CONF': str(conf), 'BIZ_DASH_STATE': str(tmp_path/'state'),
           'BIZ_DASH_LAUNCHER': str(launcher), 'BIZ_DASH_WORKSPACE': '/fixture/install',
           'CLAUDE_CODE_SESSION_ID': 'must-not-inherit'}
    child = subprocess.Popen(['bash', str(SCRIPT), 'foreground'], env=env, stdout=subprocess.PIPE, text=True)
    output, _ = child.communicate(timeout=5)
    assert child.returncode == 0
    assert f'PID={child.pid}' in output
    assert '--workspace\n/fixture/install\n' in output and '--port\n1771\n' in output
    assert '--instance-root\n/fixture/instance\n' in output and '--component\napp=/fixture/code/app\n' in output
    assert 'FIXTURE_FLAG=kept' in output and 'CLAUDE_CODE_SESSION_ID' not in output
    assert 'INSTANCE_ROOT=' not in output and 'COMPONENT_APP=' not in output  # launch arguments, not environment
    assert f'BIZ_DASH_STATE={tmp_path}/state' in output
    assert not (tmp_path/'state/dashboard.pid').exists()


def test_broker_uses_external_state_and_explicit_isolated_socket(tmp_path):
    env={**os.environ,'BIZ_DASH_STATE':str(tmp_path/'persistent'),'BIZ_TMUX_SOCKET':'isolated-contract'}
    result=subprocess.run([os.environ.get('PYTHON','/usr/bin/python3'),'-I','-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import pty_broker;print(pty_broker.TMUX_CONFIG_DIR);print(pty_broker.TMUX_SOCK)',
        str(SCRIPT.parent)],env=env,capture_output=True,text=True,check=True)
    assert result.stdout.splitlines()==[str(tmp_path/'persistent'),'isolated-contract']
