"""Trusted native hook observations plus bounded target-local process verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys

from qorqut_operations.contracts import DomainError
from .client import load_registration, write_json_private
from bazarlyk.release_worker.lifetime import local_instance


def attest(hook, registration_path, runtime):
    """Only a hook descending from a native interactive client creates this observation."""
    registration = load_registration(registration_path)
    changed_cwd = Path(hook.get('cwd', '')).resolve() != Path(registration['workspace']).resolve()
    if hook.get('agent_id') or hook.get('parent_session_id'):
        raise DomainError('inbox_binding_required','A diagnostic or subagent hook cannot select the parent inbox')
    session = hook['session_id']
    if runtime == 'codex':
        from .codex_session import session_process
        owner = session_process(session)
        pid, started = owner.split(':')[:2]
    else:
        from .terminal_chat import native_process
        process = native_process(runtime)
        if process is None:
            raise DomainError('inbox_binding_required', 'No interactive native ancestor is verified')
        pid, started = process['pid'], process['started']
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if any(arg in argv for arg in (b'-p', b'--print', b'--agent-id')):
            raise DomainError('inbox_binding_required', 'Diagnostic and subagent clients cannot own inbox delivery')
        owner = f'{pid}:{started}'
    if changed_cwd:
        try:
            prior=json.loads(Path(registration['workspace']).joinpath('.bazarlyk/native-binding.json').read_text())
            if prior['session_id']!=session or prior['owner']!=owner or prior['registration_id']!=registration['registration_id']:
                raise ValueError('different native session')
        except (OSError,ValueError,KeyError):
            raise DomainError('registration_conflict','Changed working directory has no matching verified native session association')
    value = {'agent_id': registration['agent_id'], 'registration_id': registration['registration_id'],
        'session_id': session, 'runtime': runtime, 'workspace': registration['workspace'],
        'pid': int(pid), 'started': str(started), 'owner': owner,
        'event': hook.get('source', 'startup') if hook['hook_event_name'] == 'SessionStart' else 'prompt',
        **local_instance()}
    write_json_private(Path(registration['workspace'])/'.bazarlyk/native-binding.json', value)
    return value


# This exact standard-library probe is also executed on the configured target.
# It never inspects a target PID through the dashboard host's /proc.
PROBE = '''import json,sys
from pathlib import Path
workspace,session,pane,agent,registration=sys.argv[1:]
def fields(pid):
 return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
marker=Path(workspace)/'.bazarlyk/native-binding.json'
if marker.is_symlink() or any(p.is_symlink() for p in marker.parents): raise ValueError('unsafe observation')
v=json.loads(marker.read_text())
assert v['session_id']==session and v['agent_id']==agent and v['registration_id']==registration and v['workspace']==workspace
pid=int(v['pid']);native=fields(pid)
assert native[0]!='Z' and native[19]==v['started']
name=Path('/proc/'+str(pid)+'/comm').read_text().strip()
assert name in ({'claude','claude.exe'} if v['runtime']=='claude-code' else {'codex'})
argv=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
assert not any(a in argv for a in (b'exec',b'queue',b'--print',b'-p',b'--agent-id'))
if b'app-server' in argv:
 assert v['runtime']=='codex'
 parts=v['owner'].split(':'); assert len(parts)==5
 fd=Path('/proc/'+str(pid)+'/fd/'+parts[2]); info=fd.stat()
 assert (info.st_dev,info.st_ino)==(int(parts[3]),int(parts[4]))
 assert fd.readlink().name==session+'.lock'
 assert any(line.split()[2:6]==['FLOCK','ADVISORY','WRITE',str(pid)] for line in Path('/proc/'+str(pid)+'/fdinfo/'+parts[2]).read_text().splitlines() if line.startswith('lock:'))
else:
 assert pane and native[2]==fields(pane)[5]
 ancestor=pid
 for _ in range(24):
  if str(ancestor)==pane: break
  ancestor=int(fields(ancestor)[1])
 else: raise ValueError('not registered pane ancestry')
print(json.dumps(v))
'''

DEAD_PROBE = '''import sys
from pathlib import Path
pid,started,owner=sys.argv[1:]
try:
 f=Path('/proc/'+pid+'/stat').read_text().rsplit(')',1)[1].split()
 dead=f[0]=='Z' or f[19]!=started
 parts=owner.split(':')
 if not dead and len(parts)==5:
  descriptor=Path('/proc/'+pid+'/fd/'+parts[2]);info=descriptor.stat()
  dead=(info.st_dev,info.st_ino)!=(int(parts[3]),int(parts[4]))
  if not dead:
   dead=not any(line.split()[2:6]==['FLOCK','ADVISORY','WRITE',pid] for line in Path('/proc/'+pid+'/fdinfo/'+parts[2]).read_text().splitlines() if line.startswith('lock:'))
 print('dead' if dead else 'alive')
except FileNotFoundError: print('dead')
except (OSError,IndexError,ValueError): print('unknown')
'''


class NativeBindingAdapter:
    def __init__(self, runtime): self.runtime = runtime

    def selected(self, agent_id):
        with self.runtime.database.read() as uow:
            profile = dict(uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?',(agent_id,)).fetchone())
            business = uow.repository('businesses').get(profile['home_business_id']) if profile['home_business_id'] else None
        terminals = self.runtime.terminals
        execution = terminals.execution(business)
        _, session, _ = terminals.binding(business, profile, execution.broker)
        return profile, execution, session

    @staticmethod
    def run(execution, script, arguments):
        if execution.target_identity is not None:
            broker = execution.broker
            if not hasattr(broker, 'binding_probe'):
                raise DomainError('inbox_binding_ambiguous', 'Execution target has no trusted process probe')
            return broker.binding_probe(script, arguments)
        return subprocess.check_output([sys.executable,'-I','-c',script,*map(str,arguments)],
                                       text=True, timeout=1)

    def verify(self, agent_id, registration_id, session_id, *, previous=None):
        try:
            profile, execution, terminal = self.selected(agent_id)
            target = str(execution.target_identity or 'local')
            code, pane = execution.broker.tmux('list-panes','-t','='+terminal,'-F','#{pane_pid}')
            if code or len(pane.strip().splitlines()) != 1:
                pane = ''
            value = json.loads(self.run(execution, PROBE, [profile['workspace'],session_id,pane.strip(),agent_id,registration_id]))
            value['execution_target'] = target
            value['target'] = target
            if execution.target_identity is not None:
                # A replacement control host/daemon is not the original target.
                value['target_instance'] = local_instance()['target_instance']+':'+execution.target_identity[-1]
            old_state = 'unknown'
            if previous and previous.get('execution_target') == target:
                old_state = self.run(execution, DEAD_PROBE, [previous['pid'],previous['started'],previous['owner']]).strip()
            return value, old_state
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, DomainError) as exc:
            raise DomainError('inbox_binding_ambiguous', 'Native process/terminal binding cannot be verified on its execution target', retryable=True) from exc
