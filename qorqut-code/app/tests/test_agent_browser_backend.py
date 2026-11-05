"""New backend acceptance using a disposable Chrome profile, never owner Chrome."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from bazarlyk.bootstrap import browser, agent_browser


def test_backend_rejects_target_overrides_and_removes_ambient_config(tmp_path):
    with pytest.raises(ValueError):
        agent_browser.command('cli', 'worker', 'http://localhost:1234', tmp_path,
                              ['click', 'e1', '--cdp=9999'])
    assert agent_browser.environment({'AGENT_BROWSER_PROFILE': 'private', 'PATH': '/bin'}) == {'PATH': '/bin'}
    command = agent_browser.command('cli', 'worker', 'http://localhost:1234', tmp_path, ['click', 'e1'])
    assert command[-2:] == ['click', '@e1']


@pytest.mark.parametrize('args, expected', [
    (['dialog', 'dismiss'], ['dialog', 'dismiss']),
    (['dialog-dismiss'], ['dialog', 'dismiss']),
    (['dialog', 'accept'], ['dialog', 'accept']),
    (['dialog-accept'], ['dialog', 'accept']),
    (['dialog', 'accept', 'https://example.test/a?b=1&c=2'],
     ['dialog', 'accept', 'https://example.test/a?b=1&c=2']),
    (['dialog-accept', ''], ['dialog', 'accept', '']),
])
def test_dialog_commands_reach_the_backend(tmp_path, monkeypatch, args, expected):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout='resolved', stderr='')

    monkeypatch.setattr(subprocess, 'run', execute)
    original = args.copy()
    assert browser.run(tmp_path, args, target=('http://127.0.0.1:1', Path('/not-executed')),
                       capture=True) == (0, 'resolved', '')
    assert calls[0][7:] == expected
    assert args == original  # Preserve the caller's recorded command.


@pytest.mark.parametrize('args', [
    ['dialog'], ['dialog', 'close'], ['dialog', 'dismiss', '--session', 'other'],
    ['dialog', 'accept', '--cdp=http://127.0.0.1:9999'],
    ['dialog-dismiss', '--config=other.json'], ['dialog', 'accept', '--headed'],
])
def test_invalid_dialog_commands_never_execute(tmp_path, monkeypatch, args):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not execute'))
    with pytest.raises(browser.BrowserError):
        browser.run(tmp_path, args, target=('http://127.0.0.1:1', Path('/not-executed')))


def test_agent_browser_external_chrome_form_observation_and_detach(tmp_path, capfd):
    chrome = shutil.which('google-chrome')
    runtime = Path(os.environ.get('QORQUT_TEST_BROWSER_RUNTIME',
                                 Path(__file__).resolve().parents[1] / 'tools/browser'))
    if not chrome or not (runtime / 'node_modules/.bin/agent-browser').exists() or not (runtime / 'node/bin/node').exists():
        pytest.skip('Requires Chrome and the built local backend (tools/browser/build_runtime.py)')
    profile = tmp_path / 'profile'
    profile.mkdir()
    process = subprocess.Popen([chrome, '--headless=new', '--no-sandbox', '--disable-gpu',
        '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
        '--user-data-dir=' + str(profile), 'about:blank'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    try:
        port_file = profile / 'DevToolsActivePort'
        for _ in range(100):
            if port_file.exists():
                break
            time.sleep(.1)
        endpoint = 'http://127.0.0.1:' + port_file.read_text().splitlines()[0]
        target = (endpoint, browser.runtime_executable(str(runtime)))
        def run(*args):
            assert browser.run(workspace, list(args), target=target) == 0
        run('attach')
        from urllib.parse import quote
        run('goto', 'data:text/html,' + quote('<title>Trial</title><label>Name<input aria-label="Name"></label><button onclick="document.querySelector(\'output\').textContent=document.querySelector(\'input\').value">Save</button><output></output>'))
        run('fill', 'input', 'Luna trial')
        run('click', 'button')
        capfd.readouterr()
        run('observe', 'overview')
        result = json.loads(capfd.readouterr().out)
        assert result['status'] == 'ok'
        assert 'Luna trial' in json.dumps(result)
        # Reproduce a native prompt over an unsaved form, then follow the runtime's
        # own recovery command. Both public spellings must preserve the form.
        run('goto', 'data:text/html,' + quote('''<title>Trial</title>
            <input id="draft" value="unsaved draft">
            <button id="paste" onclick="document.querySelector('output').textContent=JSON.stringify(prompt('Paste URL'))">Paste URL</button>
            <output></output>'''))
        for resolution, expected in [
                (['dialog', 'dismiss'], 'null'), (['dialog-dismiss'], 'null'),
                (['dialog', 'accept', 'https://example.test'], '"https://example.test"'),
                (['dialog-accept', ''], '""')]:
            run('click', '#paste')
            code, _, errors = browser.run(workspace, ['press', 'Escape'], target=target, capture=True)
            assert code != 0 and 'Paste URL' in errors and 'dialog dismiss' in errors
            run(*resolution)
            code, output, errors = browser.run(workspace, ['eval',
                '() => ({draft: document.querySelector("input").value, result: document.querySelector("output").textContent})'],
                target=target, capture=True)
            assert code == 0, errors
            assert json.loads(output) == {'draft': 'unsaved draft', 'result': expected}
        run('tab-list')
        run('screenshot', str(tmp_path / 'trial.png'))
        assert (tmp_path / 'trial.png').exists()
        run('detach')
        assert process.poll() is None
        with urlopen(endpoint + '/json/list', timeout=5) as response:
            assert any(page.get('title') == 'Trial' for page in json.load(response))
    finally:
        process.terminate()
        process.wait(timeout=15)
