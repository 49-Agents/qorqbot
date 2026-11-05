from types import SimpleNamespace
import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend, dev_client


@pytest.mark.parametrize('action,snapshot',[
    ('start',{'state':'present','command':'codex'}),
    ('ping',{'state':'present','command':'bash'}),
    ('kill',{'state':'absent'}), ('restart',{'state':'absent'})])
def test_invalid_terminal_states_are_rejected(action,snapshot):
    with pytest.raises(DomainError): AgentTerminalBackend.validate(action,snapshot)


def restart_calls(profile=None, command='codex', client=None):
    """Every effect ``execute`` makes for one target, in order, against recording stand-ins (no tmux, session or model)."""
    calls=[]
    class Broker:
        def tmux(self,*args): calls.append(('tmux',args));return 0,''
        def stop(self,key): calls.append(('stop',key))
        def submit_input(self,session,text): calls.append(('input',text))
    class Read:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def repository(self,name):return self
        def get(self,key):return {'owner_principal_id':'owner'}
    runtime=SimpleNamespace(database=SimpleNamespace(read=Read),policy=SimpleNamespace(context_for=lambda *a,**k:'owner-context'))
    terminals=SimpleNamespace(runtime=runtime,open=lambda *a:calls.append(('open',)))
    backend=AgentTerminalBackend(terminals,sleep=lambda t:None)
    marked={'client':client} if client else {}
    expected={'state':'present','command':command,'session_id':'$1',**marked}
    snapshots=iter([expected,{'state':'present','command':'bash','pane_id':'%2'},{'state':'present','command':command,'pane_id':'%2'}])
    backend.snapshot=lambda row:next(snapshots)
    backend.selected=lambda row:(Broker(),'key','session','cwd',None)
    backend.wait_ready=lambda row,client='codex':calls.append(('ready',client))
    row={'agent_id':'target','home_business_id':None,**({'profile_json':profile} if profile else {})}
    backend.execute(row,'restart',None,expected)
    return calls


def profile(role,runtime):
    return {'role':{'id':role},'runtime':runtime}


def test_restart_orders_kill_launch_and_role_without_arbitrary_commands():
    # A global that is not a worker keeps the Codex launcher, the $role command and the picker's second Enter.
    assert restart_calls()==[('tmux',('kill-session','-t','$1')),('stop','key'),('open',),
        ('tmux',('send-keys','-t','%2','C-u')),('input','codex'),('ready','codex'),('input','$role'),
        ('tmux',('send-keys','-t','%2','Enter'))]


def test_a_codex_worker_restarts_through_codex_and_its_role_picker():
    assert restart_calls(profile('global-worker','codex'),client='codex')==[('tmux',('kill-session','-t','$1')),('stop','key'),
        ('open',),('tmux',('send-keys','-t','%2','C-u')),('input','codex'),('ready','codex'),('input','$role'),
        ('tmux',('send-keys','-t','%2','Enter'))]


def test_a_claude_worker_restarts_through_claude_code_and_its_slash_role():
    # Claude Code has no skill picker: the role command is one submitted slash command, and readiness is a verified composer.
    assert restart_calls(profile('global-worker','claude-code'),command='claude',client='claude')==[
        ('tmux',('kill-session','-t','$1')),('stop','key'),('open',),('tmux',('send-keys','-t','%2','C-u')),
        ('input','claude'),('ready','claude'),('input','/role')]


def test_claude_worker_readiness_requires_a_verified_idle_composer():
    from qorqut_operations.contracts import DomainError as EngineError
    from bazarlyk.transport.agent_terminal_control import OwnerInputDeferred
    backend=AgentTerminalBackend(SimpleNamespace(runtime=None),timeout=.01,sleep=lambda t:None)
    broker=SimpleNamespace(tmux=lambda *a:(0,'Welcome to Claude Code'))
    backend.selected=lambda row:(broker,None,'session',None,None)
    backend.snapshot=lambda row:{'state':'present','command':'claude','pane_id':'%2'}
    seen=[]
    def refuse(row,client,allow_busy=False):
        seen.append(client);raise OwnerInputDeferred('composer_unrecognized')
    backend._ready=refuse
    with pytest.raises(EngineError,match='Claude Code did not reach its input prompt'):backend.wait_ready({},'claude')
    assert seen and set(seen)=={'claude'}
    backend._ready=lambda row,client,allow_busy=False:(broker,{},None)
    backend.wait_ready({},'claude')


def test_a_claude_screen_is_never_taken_for_a_codex_prompt():
    from qorqut_operations.contracts import DomainError as EngineError
    backend=AgentTerminalBackend(SimpleNamespace(runtime=None),timeout=.01,sleep=lambda t:None)
    broker=SimpleNamespace(tmux=lambda *a:(0,'› looks like a codex prompt'))
    backend.selected=lambda row:(broker,None,'session',None,None)
    backend.snapshot=lambda row:{'state':'present','command':'claude','pane_id':'%2'}
    with pytest.raises(EngineError,match='Codex did not reach its input prompt'):backend.wait_ready({})


def test_preparation_failure_never_types_role():
    backend=AgentTerminalBackend(SimpleNamespace(runtime=None),timeout=.01,sleep=lambda t:None)
    calls=[]
    broker=SimpleNamespace(tmux=lambda *a:(0,'Blocked by hook: reprepare_required'))
    backend.selected=lambda row:(broker,None,'session',None,None)
    backend.snapshot=lambda row:{'state':'present','command':'codex','pane_id':'%2'}
    with pytest.raises(DomainError,match='preparation failed'):backend.wait_ready({})


def test_snapshot_identifies_codex_under_node_without_tty_ioctl(tmp_path, monkeypatch):
    import bazarlyk.transport.agent_terminal_control as module
    from pathlib import Path
    proc = tmp_path / 'proc'
    proc.mkdir()
    def process(pid, group, foreground, command):
        folder = proc / str(pid)
        folder.mkdir()
        fields = ['S', '1', str(group), '1', '44', str(foreground)] + ['0'] * 13 + ['123456']
        (folder / 'stat').write_text(f'{pid} ({command}) ' + ' '.join(fields))
        (folder / 'comm').write_text(command)
        (folder / 'cmdline').write_bytes(command.encode() + b'\0')
    process(10, 10, 20, 'bash')
    process(20, 20, 20, 'node')
    process(21, 20, 20, 'codex')
    monkeypatch.setattr(module, 'Path', lambda value: proc if value == '/proc' else proc / str(value).removeprefix('/proc/'))
    broker = SimpleNamespace(session_alive=lambda s: True,
        tmux=lambda *a: (0, '1|$1|%1|10|node|/dev/pts/44'))
    backend = AgentTerminalBackend(SimpleNamespace(runtime=None))
    backend.selected = lambda row: (broker, None, 'session', None, None)
    snapshot = backend.snapshot({})
    assert snapshot['command'] == 'codex'
    assert snapshot['foreground_pid'] == 20
    assert snapshot['foreground_started'] == '123456'
    (proc / '21' / 'cmdline').write_bytes(b'codex\0--remote\0unix://\0')
    with pytest.raises(DomainError, match='Remote app-server'):
        backend.snapshot({})


def test_start_submits_role_and_second_enter_through_real_tmux(tmp_path):
    """Exercise pane addressing on an isolated tmux server, without a model."""
    import shutil
    import subprocess
    import time
    if not shutil.which('tmux'):
        pytest.skip('tmux is required for terminal integration coverage')
    socket = str(tmp_path / 'tmux.sock')
    receipt = tmp_path / 'input.txt'
    launcher = tmp_path / 'shell.sh'
    launcher.write_text('''#!/bin/bash
codex() {
  printf '\\n› Ready\\n'
  IFS= read -r role
  IFS= read -r second
  printf '%s\\n%s\\n' "$role" "$second" > "$RECEIPT"
}
export -f codex
exec bash --noprofile --norc
''')
    class Broker:
        def tmux(self, *args):
            result = subprocess.run(['tmux', '-S', socket, *args], capture_output=True, text=True)
            return result.returncode, result.stdout
        def submit_input(self, session, text):
            assert self.tmux('send-keys', '-t', '='+session+':', '-l', text)[0] == 0
            assert self.tmux('send-keys', '-t', '='+session+':', 'Enter')[0] == 0
    broker = Broker()
    class Read:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def repository(self, name): return self
        def get(self, key): return {'owner_principal_id':'owner'}
    runtime = SimpleNamespace(database=SimpleNamespace(read=Read),
        policy=SimpleNamespace(context_for=lambda *a, **k: 'owner'))
    backend = AgentTerminalBackend(SimpleNamespace(runtime=runtime, open=lambda *a: None), timeout=3)
    session = 'integration__agent'
    try:
        import os
        subprocess.run(['tmux', '-S', socket, 'new-session', '-d', '-s', session,
                        'bash '+str(launcher)], env={**os.environ, 'RECEIPT':str(receipt)}, check=True)
        pane = broker.tmux('list-panes', '-t', '='+session, '-F', '#{pane_id}')[1].strip()
        # The fixture shell stands in for Codex; actual screen reads and input
        # delivery use tmux. No real model or business agent is launched.
        observations = 0
        def snapshot(row):
            nonlocal observations
            observations += 1
            return {'state':'present','command':'bash' if observations <= 2 else 'codex','pane_id':pane}
        backend.snapshot = snapshot
        backend.selected = lambda row: (broker, 'key', session, tmp_path, None)
        expected = {'state':'present','command':'bash','pane_id':pane}
        backend.execute({'agent_id':'target','home_business_id':None}, 'start', None, expected)
        until = time.monotonic()+3
        while not receipt.exists() and time.monotonic()<until:
            time.sleep(.02)
        assert receipt.read_text() == '$role\n\n'
    finally:
        broker.tmux('kill-server')


def dev_row(runtime='claude-code', role='dev'):
    import json
    return {'agent_id':'dev', 'home_business_id':None,
            'profile_json':json.dumps({'role':{'id':role}, 'runtime':runtime})}


def recording_backend(snapshots):
    calls=[]
    class Broker:
        def tmux(self,*args): calls.append(('tmux',args));return 0,''
        def stop(self,key): calls.append(('stop',key))
        def submit_input(self,session,text): calls.append(('input',text))
    class Read:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def repository(self,name):return self
        def get(self,key):return {'owner_principal_id':'owner'}
    runtime=SimpleNamespace(database=SimpleNamespace(read=Read),policy=SimpleNamespace(context_for=lambda *a,**k:'owner-context'))
    backend=AgentTerminalBackend(SimpleNamespace(runtime=runtime,open=lambda *a:calls.append(('open',))),sleep=lambda t:None)
    snapshots=iter(snapshots)
    backend.snapshot=lambda row:next(snapshots)
    backend.selected=lambda row:(Broker(),'key','session','cwd',None)
    backend.wait_ready=lambda row:calls.append(('ready',))
    return backend, calls


@pytest.mark.parametrize('runtime,launcher', [('claude-code','claude'), ('codex','codex')])
@pytest.mark.parametrize('action', ['start','restart'])
def test_dev_launches_its_runtimes_client_without_a_role_or_work_prompt(runtime, launcher, action):
    shell={'state':'present','command':'bash','pane_id':'%2','client':launcher}
    expected={'state':'present','command':launcher,'session_id':'$1','client':launcher} if action=='restart' else shell
    backend, calls = recording_backend([expected, shell])
    detail = backend.execute(dev_row(runtime), action, None, expected)
    assert detail == 'Startup command submitted; readiness/work is not confirmed'
    launch = [('open',), ('tmux',('send-keys','-t','%2','C-u')), ('input',launcher)]
    assert calls == ([('tmux',('kill-session','-t','$1')),('stop','key')] if action=='restart' else []) + launch
    # No $role, /role, readiness scraping or automatic project prompt follows.
    assert not any(call[0] == 'ready' or (call[0] == 'input' and call[1] != launcher) for call in calls)


def test_dev_runtime_change_after_approval_never_dispatches():
    expected={'state':'present','command':'bash','pane_id':'%2','client':'codex'}
    backend, calls = recording_backend([expected])
    with pytest.raises(DomainError) as error:
        backend.execute(dev_row('claude-code'), 'start', None, expected)
    assert error.value.code == 'revision_conflict' and calls == []


def test_dev_and_other_pings_require_their_verified_foreground_client():
    backend, calls = recording_backend([{'state':'present','command':'claude','client':'claude'}])
    assert 'Claude Code' in backend.execute(dev_row(), 'ping', 'Owner: status?', {'state':'present','command':'claude','client':'claude'})
    assert calls == [('input','Owner: status?')]
    for snapshot in ({'state':'present','command':'bash','client':'claude'},
                     {'state':'present','command':'unrecognized','client':'claude'},
                     {'state':'present','command':'claude','client':'codex'},
                     {'state':'present','command':'claude'}):
        with pytest.raises(DomainError): AgentTerminalBackend.validate('ping', snapshot)
    AgentTerminalBackend.validate('ping', {'state':'present','command':'codex','client':'codex'})
    AgentTerminalBackend.validate('ping', {'state':'present','command':'codex'})


def proc_tree(tmp_path, monkeypatch, foreground):
    import bazarlyk.transport.agent_terminal_control as module
    proc = tmp_path / 'proc'
    proc.mkdir()
    def process(pid, group, command):
        folder = proc / str(pid)
        folder.mkdir()
        fields = ['S', '1', str(group), '1', '44', '20'] + ['0'] * 13 + ['123456']
        (folder / 'stat').write_text(f'{pid} ({command}) ' + ' '.join(fields))
        (folder / 'comm').write_text(command)
        (folder / 'cmdline').write_bytes(command.encode() + b'\0')
    process(10, 10, 'bash')
    for offset, command in enumerate(foreground):
        process(20 + offset, 20, command)
    monkeypatch.setattr(module, 'Path', lambda value: proc if value == '/proc' else proc / str(value).removeprefix('/proc/'))


@pytest.mark.parametrize('row,tmux_command,group,command', [
    # Dev on Claude Code: native claude and claude.exe are verified from the process group.
    (dev_row(), 'claude', ['claude'], 'claude'),
    (dev_row(), 'node', ['node', 'claude.exe'], 'claude'),
    # tmux's name alone, or another foreground program, is not Dev's verified client.
    (dev_row(), 'claude', ['vim'], 'unrecognized'),
    (dev_row(), 'vim', ['vim'], 'unrecognized'),
    (dev_row(), 'node', ['node', 'codex'], 'unrecognized'),
    (dev_row('codex'), 'node', ['node', 'codex'], 'codex'),
    (dev_row('codex'), 'claude', ['claude'], 'unrecognized'),
    # Non-Dev targets keep their existing Codex-only observation.
    (dev_row(role='business-agent'), 'claude', ['claude'], 'claude'),
    (dev_row(role='business-agent'), 'node', ['node', 'codex'], 'codex'),
])
def test_snapshot_normalizes_only_dev_runtimes_client(tmp_path, monkeypatch, row, tmux_command, group, command):
    proc_tree(tmp_path, monkeypatch, group)
    broker = SimpleNamespace(session_alive=lambda s: True, tmux=lambda *a: (0, f'1|$1|%1|10|{tmux_command}|/dev/pts/44'))
    backend = AgentTerminalBackend(SimpleNamespace(runtime=None))
    backend.selected = lambda value: (broker, None, 'session', None, None)
    snapshot = backend.snapshot(row)
    assert snapshot['command'] == command
    # Only a Dev binding names its client, so a later runtime change invalidates it.
    assert snapshot.get('client') == dev_client(row)
    ping = (lambda: AgentTerminalBackend.validate('ping', snapshot))
    if command == snapshot.get('client', 'codex'):
        ping()
    else:
        with pytest.raises(DomainError): ping()


def test_dev_idle_shell_is_startable_but_never_pingable(tmp_path, monkeypatch):
    proc_tree(tmp_path, monkeypatch, ['bash'])
    broker = SimpleNamespace(session_alive=lambda s: True, tmux=lambda *a: (0, '1|$1|%1|10|bash|/dev/pts/44'))
    backend = AgentTerminalBackend(SimpleNamespace(runtime=None))
    backend.selected = lambda value: (broker, None, 'session', None, None)
    snapshot = backend.snapshot(dev_row())
    assert snapshot['command'] == 'bash'
    AgentTerminalBackend.validate('start', snapshot)
    with pytest.raises(DomainError): AgentTerminalBackend.validate('ping', snapshot)
