"""Verify the actual bytes reaching an isolated tmux pane, without any agent."""
import importlib.util
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.mark.skipif(not shutil.which('tmux'), reason='tmux is required')
def test_broadcast_enter_after_paste_reaches_existing_pane(tmp_path):
    spec = importlib.util.spec_from_file_location('broadcast_test_broker', Path(__file__).resolve().parents[1] / 'pty_broker.py')
    broker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = broker
    spec.loader.exec_module(broker)
    broker.TMUX_SOCK = 'broadcast-test-' + uuid4().hex
    broker.TMUX_CONFIG_DIR = str(tmp_path)
    reader = tmp_path / 'reader.py'
    reader.write_text('''import os,tty,time,select
from pathlib import Path
tty.setraw(0)
Path('ready').touch()
data=b''
end=time.monotonic()+10
while time.monotonic()<end:
    if select.select([0],[],[],.1)[0]: data+=os.read(0,65536)
    if data.endswith(b'\\r'): break
Path('received').write_bytes(data)
time.sleep(10)
''')
    def wait_for(path):
        end = time.monotonic() + 10
        while not path.exists() and time.monotonic() < end:
            time.sleep(.02)
        assert path.exists()
    try:
        broker.ensure_session('recipient', [sys.executable, str(reader)], str(tmp_path))
        wait_for(tmp_path / 'ready')
        message = 'Review café\nThen report back.'
        broker.submit_input('recipient', message)
        wait_for(tmp_path / 'received')
        assert (tmp_path / 'received').read_bytes() == message.encode() + b'\r'
        assert broker.session_alive('recipient')
    finally:
        broker.tmux('kill-server')
        sys.modules.pop(spec.name, None)


@pytest.mark.skipif(__import__('os').environ.get('QORQUT_TEST_CODEX_INPUT') != '1',
                    reason='opt-in isolated installed Codex input check')
def test_installed_codex_submits_local_status_command(tmp_path):
    import json
    codex = shutil.which('codex')
    if not codex or not shutil.which('tmux'):
        pytest.skip('Codex and tmux are required')
    spec = importlib.util.spec_from_file_location('codex_submit_broker', Path(__file__).resolve().parents[1] / 'pty_broker.py')
    broker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = broker
    spec.loader.exec_module(broker)
    broker.TMUX_SOCK = 'codex-submit-test-' + uuid4().hex
    broker.TMUX_CONFIG_DIR = str(tmp_path)
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'config.toml').write_text(
        'model="local-test"\nmodel_provider="local-test"\n'
        '[model_providers.local-test]\nname="Isolated test"\n'
        'base_url="http://127.0.0.1:9"\nwire_api="responses"\n'
        '[projects.' + json.dumps(str(tmp_path)) + ']\ntrust_level="trusted"\n')
    def screen():
        return broker.tmux('capture-pane', '-p', '-t', '=recipient:')[1]
    def wait_for(text):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if text in screen():
                return
            time.sleep(.1)
        pytest.fail('Codex did not display ' + text + ': ' + screen())
    try:
        broker.ensure_session('recipient', [codex, '--no-alt-screen', '--cd', str(tmp_path)], str(tmp_path),
                              env={'CODEX_HOME': str(config), 'CODEX_API_KEY': 'isolated-not-a-key'})
        wait_for('local-test default')
        broker.submit_input('recipient', '/status')
        wait_for('Token usage:')
        assert '0 total' in screen()
        assert 'Model provider:' in screen()
    finally:
        broker.tmux('kill-server')
        sys.modules.pop(spec.name, None)
