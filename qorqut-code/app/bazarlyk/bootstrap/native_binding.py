"""Trusted native hook observations plus bounded target-local process verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
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


# Live helper consumers of one registration, on the target that runs them. A helper started through
# the stable launcher has a lifetime pin and is a fenced-protocol process; every other match is a
# pre-fencing consumer whose exit must be observed before ownership is upgraded. Standard library only.
#
# Absence of evidence is not an exit. A process that matches the registration, or that an earlier
# observation recorded (PID and start ticks, passed as the fourth argument), is reported with an
# ``unknown`` reason when its state cannot be read. Only a missing PID, a zombie, or a readable different
# start identity (a reused PID) counts as exited. An unreadable unrelated process is no evidence at all.
LEGACY_PROBE = '''import json,os,sys
from pathlib import Path
registration,pins,hostname=sys.argv[1:4]
known={}
for item in (json.loads(sys.argv[4]) if len(sys.argv)>4 else []):
 known[int(item[0])]=(str(item[1]),item[2])
MODULES=('bazarlyk.bootstrap.mcp_reconnect','bazarlyk.inbox.watcher','bazarlyk.bootstrap.codex_session','bazarlyk.bootstrap.supervisor')
found=[]
def gone(error):
 return isinstance(error,(FileNotFoundError,ProcessLookupError))
def doubt(pid,kind,reason):
 found.append({'pid':pid,'started':None,'kind':kind,'stable':False,'unknown':reason})
for entry in Path('/proc').iterdir():
 if not entry.name.isdigit() or int(entry.name)==os.getpid(): continue
 pid=int(entry.name)
 recorded=known.get(pid)
 try:
  text=' '.join(entry.joinpath('cmdline').read_bytes().decode(errors='replace').split('\\0'))
 except OSError as error:
  if gone(error) or recorded is None: continue
  text=None
 if text is not None and registration not in text and recorded is None: continue
 kind=None
 if text is not None and registration in text:
  kind=('mcp_bridge' if 'bazarlyk.bootstrap.mcp_reconnect' in text else
   'watcher' if ('bazarlyk.inbox.watcher' in text or ('--watch' in text and any(m in text for m in MODULES[2:]))) else None)
 if kind is None and recorded is None: continue
 label=kind or (recorded[1] if recorded else None)
 try:
  fields=entry.joinpath('stat').read_text().rsplit(')',1)[1].split()
  started=fields[19]
 except OSError as error:
  if not gone(error): doubt(pid,label,'stat_unreadable')
  continue
 except (IndexError,ValueError):
  doubt(pid,label,'stat_unparsable')
  continue
 if fields[0] in ('Z','X'): continue
 if kind is None:
  if recorded[0]==started:
   doubt(pid,label,'argv_unreadable' if text is None else 'argv_unmatched')
  continue
 try:
  stable=bool(pins) and Path(pins,hostname+'-'+entry.name+'-'+started+'.json').is_file()
 except OSError:
  stable=False
 found.append({'pid':pid,'started':started,'kind':kind,'stable':stable})
print(json.dumps(found))
'''

# The native session of one existing generation-0 owner, from either hook observation. It never writes
# an observation and never trusts a marker (or its freshness) by itself: the process is verified here.
LEGACY_NATIVE_PROBE = '''import json,re,sys
from pathlib import Path
workspace,session,pane,agent,registration=sys.argv[1:6]
def fields(pid):
 return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
def marker(name):
 path=Path(workspace)/'.bazarlyk'/name
 if path.is_symlink() or any(p.is_symlink() for p in path.parents): raise ValueError('unsafe observation')
 value=json.loads(path.read_text())
 assert isinstance(value,dict)
 assert value['session_id']==session and value['agent_id']==agent and value['workspace']==workspace
 assert value.get('registration_id',registration)==registration
 assert value['runtime'] in ('claude-code','codex') and value.get('event')!='SessionEnd'
 if name=='terminal-chat.json':
  source=Path(value['transcript_path']); assert source.is_absolute() and '..' not in source.parts
  assert not any(p.is_symlink() for p in (source,*source.parents))
  if value['runtime']=='claude-code': assert source.name==session+'.jsonl'
  else: assert re.fullmatch(r'rollout-\\d{4}-\\d\\d-\\d\\dT\\d\\d-\\d\\d-\\d\\d-'+re.escape(session)+r'(?:_[0-9a-fA-F-]{36})?\\.jsonl',source.name)
 return value
chosen=None
for name in ('native-binding.json','terminal-chat.json'):
 try: chosen=(name,marker(name)); break
 except (OSError,ValueError,KeyError,AssertionError): continue
if chosen is None: raise ValueError('no valid hook observation for this session')
name,v=chosen
pid=int(v['pid']);started=str(v['started']);native=fields(pid)
assert native[0] not in ('Z','X') and native[19]==started
comm=Path('/proc/'+str(pid)+'/comm').read_text().strip()
assert comm in ({'claude','claude.exe'} if v['runtime']=='claude-code' else {'codex'})
argv=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
assert not any(a in argv for a in (b'exec',b'queue',b'--print',b'-p',b'--agent-id'))
if b'app-server' in argv:
 assert v['runtime']=='codex'
 owner=None
 for descriptor in Path('/proc/'+str(pid)+'/fd').iterdir():
  try:
   if descriptor.readlink().name!=session+'.lock': continue
   info=descriptor.stat()
   lines=Path('/proc/'+str(pid)+'/fdinfo/'+descriptor.name).read_text().splitlines()
   if any(line.split()[2:6]==['FLOCK','ADVISORY','WRITE',str(pid)] for line in lines if line.startswith('lock:')):
    owner=str(pid)+':'+started+':'+descriptor.name+':'+str(info.st_dev)+':'+str(info.st_ino); break
  except OSError: continue
 assert owner and v.get('owner',owner)==owner
else:
 assert pane and native[2]==fields(pane)[5]
 ancestor=pid
 for _ in range(24):
  if str(ancestor)==pane: break
  ancestor=int(fields(ancestor)[1])
 else: raise ValueError('not registered pane ancestry')
 owner=str(pid)+':'+started
 assert v.get('owner',owner)==owner
print(json.dumps({'agent_id':agent,'registration_id':registration,'session_id':session,'runtime':v['runtime'],'workspace':workspace,
 'pid':pid,'started':started,'owner':owner,'observation':name}))
'''


INVENTORY_LIMIT = 64


def inventory_path(agent_id, root=None):
    if root is None:
        from bazarlyk.installation import installation_config
        config = installation_config()
        root = config['root'] if config else None
    if root is None or not isinstance(agent_id, str) or not agent_id or '/' in agent_id:
        return None
    return Path(root)/'state/legacy-helpers'/(agent_id + '.json')


def helper_inventory(agent_id, root=None):
    """Bounded PID/start/kind observations of an agent's earlier pre-fencing helpers (local target only)."""
    path = inventory_path(agent_id, root)
    try:
        values = json.loads(path.read_text()) if path else []
        return [[int(pid), str(started), str(kind)] for pid, started, kind in values[:INVENTORY_LIMIT]]
    except (OSError, ValueError, TypeError):
        return []


def record_helpers(agent_id, found, previous, root=None):
    """Remember observed helpers; keep one whose state is unknown now, drop one confirmed exited."""
    path = inventory_path(agent_id, root)
    if path is None:
        return
    kept = {item[0]: item for item in previous}
    current = {}
    for item in found:
        if item.get('stable'):
            continue
        if item.get('unknown'):
            if item['pid'] in kept:
                current[item['pid']] = kept[item['pid']]
        elif item.get('started') is not None and item.get('kind'):
            current[item['pid']] = [item['pid'], item['started'], item['kind']]
    values = sorted(current.values())[:INVENTORY_LIMIT]
    if values != sorted(previous):
        from bazarlyk.release_worker.lifetime import atomic_json
        atomic_json(path, values)


def observe_helpers(agent_id, registration, pins, probe, root=None):
    """The pre-fencing helper consumers of one registration on the probed target, unknown entries included.

    ``probe(script, arguments)`` runs the standard-library probe where the helpers live and returns its output;
    an unobservable probe raises. A pinned (stable-launcher) helper is not a pre-fencing consumer.
    """
    known = helper_inventory(agent_id, root) if pins else []
    found = json.loads(probe(LEGACY_PROBE, [registration, pins, socket.gethostname(), json.dumps(known)]))
    if not isinstance(found, list):
        raise ValueError('invalid helper observation')
    if pins:
        record_helpers(agent_id, found, known, root)
    return [item for item in found if item.get('unknown') or not item.get('stable')] if pins else found


class NativeBindingAdapter:
    def __init__(self, runtime): self.runtime = runtime

    def legacy_state(self, agent_id, registration_id, session_id):
        """Check the exact older hook's PID/start on its selected execution target.

        Generation-zero rows lack a native binding. A later prompt may recover
        them only when that older session's recorded process is demonstrably dead.
        Missing observations never count as death.
        """
        try:
            profile, execution, _ = self.selected(agent_id)
            script = '''import json,sys,os
from pathlib import Path
workspace,session,agent,registration=sys.argv[1:]
p=Path(workspace)/'.bazarlyk/activity'/(session+'.json')
assert not any(x.is_symlink() for x in (p,*p.parents))
v=json.loads(p.read_text())
assert v['session_id']==session and v['agent_id']==agent and v['registration_id']==registration
assert v['pid_namespace']==str(Path('/proc/self/ns/pid').readlink())
assert v['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip()
owner=v['owner'];parts=owner.split(':')
assert len(parts) in (2,5) and all(s.isdigit() for s in parts)
sys.argv=['probe',parts[0],parts[1],owner]
''' + DEAD_PROBE
            return self.run(execution, script, [profile['workspace'], session_id, agent_id, registration_id]).strip()
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError, DomainError):
            return 'unknown'

    @staticmethod
    def process_pins(execution):
        """Lifetime pins of the stable launcher; a target without a local pin directory has none to consult."""
        from bazarlyk.installation import installation_config
        config = installation_config()
        return str(Path(config['root'])/'state/process-pins') if execution.target_identity is None and config else ''

    def legacy_consumers(self, agent_id):
        """Observe the live pre-fencing helper consumers of this agent's registration.

        ``None`` is unknown (a target without a local process table or an unreadable observation); the
        caller then keeps the existing legacy protocol rather than infer an exit. A matched or previously
        recorded helper whose state cannot be read is an ``unknown`` entry: it is never an exit.
        """
        try:
            profile, execution, _ = self.selected(agent_id)
            registration = str(Path(profile['workspace'])/'.bazarlyk/registration.json')
            return observe_helpers(agent_id, registration, self.process_pins(execution),
                lambda script, arguments: self.run(execution, script, arguments))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError, DomainError):
            return None

    def verify_legacy(self, agent_id, registration_id, session_id):
        """The verified native process of an existing generation-0 owner, from a hook observation.

        The observation is ``native-binding.json`` (a current hook) or ``terminal-chat.json`` (a historical
        hook), for this purpose only. Neither is proof: the native PID/start, interactive argv, the registered
        pane ancestry and foreground group (or, for a shared Codex server, the exact session writer lock)
        are verified on the execution target. Nothing is written and no ownership changes.
        """
        try:
            profile, execution, terminal = self.selected(agent_id)
            code, pane = execution.broker.tmux('list-panes','-t','='+terminal,'-F','#{pane_pid}')
            if code or len(pane.strip().splitlines()) != 1:
                pane = ''
            value = json.loads(self.run(execution, LEGACY_NATIVE_PROBE, [profile['workspace'],session_id,pane.strip(),agent_id,registration_id]))
            target = str(execution.target_identity or 'local')
            value['execution_target'] = value['target'] = target
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError, DomainError) as exc:
            raise DomainError('inbox_binding_ambiguous', 'Existing native session cannot be verified on its execution target', retryable=True) from exc

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
