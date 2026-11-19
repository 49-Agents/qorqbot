"""Trusted native hook observations plus bounded target-local process verification."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from qorqut_operations.contracts import DomainError
from .client import load_registration, path_has_symlink, write_json_private
from qorqut.release_worker.lifetime import local_instance

# A managed agent's client runs headless as a child of a runtime host the service launched: Claude Code as ``claude -p``, Codex as
# ``codex app-server --listen stdio://``. The comments below speak of claude; the Codex case is the same rule with the process name
# and argument vector of ``HEADLESS``. That client is the only headless one that may own inbox delivery. Who is that client is decided by the SERVICE (``NativeBindingAdapter.verify_managed``:
# the pid and start ticks the token-verified host reports). Everything below that runs in the agent's own processes
# (``managed_host``, ``attest``, the bridge) is a filter over facts the agent's shell can also produce; it only decides whether a
# claim is worth making, never whether it is true.
MANAGED_HOST_RECORD = '.qorqut/managed-host.json'
HOST_MODULE = b'qorqut.managed_runtime.host'
CLIENT_NAMES = frozenset({'claude', 'claude.exe'})
# The process names a managed host's client can have, by the runtime of the hook or session that claims it.
HEADLESS = {'claude-code': CLIENT_NAMES, 'codex': frozenset({'codex'})}
MAX_RECORD_BYTES = 4096
MAX_HOPS = 24
MAX_ARGUMENT = 4096
EXE_NAME = re.compile(r'python[0-9.]*(\.exe)?')
IDENTIFIER = re.compile(r'[A-Za-z0-9._:-]{1,64}')
TICKS = re.compile(r'[0-9]{1,20}')
LIVE_HOST_STATES = frozenset({'starting', 'ready', 'closing'})
# What a managed observation may carry from the hook's marker: only these, only short text. Process facts and the host come
# from the service-held identity, never from the workspace.
MARKER_KEYS = ('agent_id', 'registration_id', 'session_id', 'runtime', 'workspace', 'event', 'host', 'target', 'target_instance',
               'boot_id', 'pid_namespace')


class ProcessTable:
    """Process facts of this machine, the same ones the hooks read elsewhere: kernel fields (state, parent, start ticks), the
    short name and the argument vector."""

    @staticmethod
    def fields(pid):
        from .terminal_chat import process_fields
        return process_fields(pid)

    @staticmethod
    def comm(pid):
        if sys.platform == 'darwin':
            from qorqut import process_info
            return process_info.name(pid)
        return Path(f'/proc/{pid}/comm').read_text().strip()

    @staticmethod
    def argv(pid):
        if sys.platform == 'darwin':
            from qorqut import process_info
            return [arg.encode() for arg in process_info.cmdline(pid)]
        return Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')

    @staticmethod
    def exe(pid):
        """The file name of the executable running ``pid`` where the platform shows it, else ``None`` (never a guess)."""
        if sys.platform == 'darwin':
            return None
        try:
            return Path(os.readlink(f'/proc/{pid}/exe').removesuffix(' (deleted)')).name
        except OSError:
            return None


def runs_host_module(argv):
    """Whether ``argv`` is exactly what the launcher starts: ``<interpreter> [-flags] -m qorqut.managed_runtime.host --state
    <dir>``. A loose "contains these words" match is what a ``bash -c ... -m qorqut.managed_runtime.host`` imitation satisfies."""
    argv = list(argv)
    if argv and argv[-1] == b'':
        argv.pop()
    try:
        index = argv.index(b'-m')
    except ValueError:
        return False
    return (1 <= index <= 4 and argv[0] != b'' and all(flag.startswith(b'-') for flag in argv[1:index]) and len(argv) == index + 4
            and argv[index + 1] == HOST_MODULE and argv[index + 2] == b'--state' and 0 < len(argv[index + 3]) <= MAX_ARGUMENT)


def host_id_of(value, pid, ticks):
    """A host label that can only be short plain text; anything else is replaced by the one derived from the identity."""
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else f'host:{pid}:{ticks}'


def managed_host(workspace, pid, agent_id=None, *, processes=None, runtime='claude-code'):
    """A managed runtime host that plausibly drives the headless client ``pid``, or ``None``. A FILTER, not an authority.

    It runs inside the agent's own processes and reads only what the agent's shell can also write or fake: the workspace record
    ``<workspace>/.qorqut/managed-host.json``, ``/proc``, a process named ``claude``, a process with the launcher's argument
    vector. So a ``pid`` it accepts is merely a claim; ``NativeBindingAdapter.verify_managed`` binds it only if the service's own
    token-verified view of the agent's host names this very process. What the filter does buy: nothing is claimed for a plain
    ``claude -p`` (a delegated worker, a diagnostic run), including one an agent starts from inside its managed session.

    The claim holds while: the record is well formed; the host process has those start ticks, runs exactly
    ``python [-flags] -m qorqut.managed_runtime.host --state <dir>`` and, where the platform shows it, an executable named
    ``python*``; ``pid`` is a live ``claude``; and walking up from ``pid`` reaches the host without passing another ``claude``.
    Anything unreadable is a refusal.

    ``runtime='codex'`` is the same rule for the other client: ``pid`` is a live process named ``codex`` whose command line ends in
    exactly ``app-server --listen stdio://`` (what the Codex driver starts; never ``exec``, ``queue`` or a terminal client), and the walk
    up to the host passes no other ``codex``, so a ``codex`` an agent starts from its own shell is refused."""
    processes = processes or ProcessTable()
    names = HEADLESS.get(runtime)
    if names is None:
        return None
    try:
        path = Path(workspace) / MANAGED_HOST_RECORD
        if path_has_symlink(path) or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
            return None
        record = json.loads(path.read_text())
        host_pid, ticks = record['host_pid'], record['host_ticks']
        if (type(host_pid) is not int or host_pid <= 1 or type(pid) is not int or pid <= 1 or not isinstance(ticks, str)
                or not TICKS.fullmatch(ticks) or (agent_id is not None and record.get('agent_id', agent_id) != agent_id)):
            return None
        host = processes.fields(host_pid)
        if host[0] == 'Z' or host[19] != ticks or not runs_host_module(processes.argv(host_pid)):
            return None
        exe = getattr(processes, 'exe', lambda _: None)(host_pid)
        if exe is not None and not EXE_NAME.fullmatch(exe.lower()):
            return None
        client = processes.fields(pid)
        if client[0] == 'Z' or processes.comm(pid) not in names:
            return None
        if runtime == 'codex':
            from .codex_session import runs_app_server
            if not runs_app_server(processes.argv(pid)):
                return None
        parent = int(client[1])
        for _ in range(MAX_HOPS):
            if parent == host_pid:
                return {'host_id': host_id_of(record.get('host_id'), host_pid, ticks), 'host_pid': host_pid, 'host_ticks': ticks}
            if parent <= 1 or processes.comm(parent) in names:
                return None
            parent = int(processes.fields(parent)[1])
    except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError):
        return None
    return None


def attest(hook, registration_path, runtime):
    """Only a hook descending from a native interactive client creates this observation."""
    registration = load_registration(registration_path)
    host = None
    changed_cwd = Path(hook.get('cwd', '')).resolve() != Path(registration['workspace']).resolve()
    if hook.get('agent_id') or hook.get('parent_session_id'):
        raise DomainError('inbox_binding_required','A diagnostic or subagent hook cannot select the parent inbox')
    session = hook['session_id']
    if runtime == 'codex':
        from .codex_session import hosted_session_process, session_process
        # The app-server a managed host of this agent started, serving this very thread (writer lock held), is bound by its process
        # alone (a two-part owner, like the managed claude); the service then checks it against its own host identity. Any other
        # app-server keeps what it always had: its held-lock owner, which only a terminal that displays the thread can make current.
        hosted = hosted_session_process(session, registration['workspace'], registration['agent_id'])
        if hosted is not None:
            owner, host = hosted
        else:
            owner = session_process(session)
        pid, started = owner.split(':')[:2]
    else:
        from .terminal_chat import native_process
        process = native_process(runtime)
        if process is None:
            raise DomainError('inbox_binding_required', 'No interactive native ancestor is verified')
        pid, started = process['pid'], process['started']
        if sys.platform == 'darwin':
            from qorqut import process_info
            argv = [arg.encode() for arg in process_info.cmdline(pid)]
        else:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if any(arg in argv for arg in (b'-p', b'--print', b'--agent-id')):
            # Only a headless client under a plausible managed host is let through (a claim: the service verifies it against
            # its own host identity before anything binds); a subagent never is.
            host = None if b'--agent-id' in argv else managed_host(registration['workspace'], int(pid), registration['agent_id'])
            if host is None:
                raise DomainError('inbox_binding_required', 'Diagnostic and subagent clients cannot own inbox delivery')
        if sys.platform == 'darwin' and process_info.start_token(pid) != started:
            raise DomainError('inbox_binding_required', 'Native process changed during attestation')
        owner = f'{pid}:{started}'
    if changed_cwd:
        try:
            prior=json.loads(Path(registration['workspace']).joinpath('.qorqut/native-binding.json').read_text())
            if prior['session_id']!=session or prior['owner']!=owner or prior['registration_id']!=registration['registration_id']:
                raise ValueError('different native session')
        except (OSError,ValueError,KeyError):
            raise DomainError('registration_conflict','Changed working directory has no matching verified native session association')
    value = {'agent_id': registration['agent_id'], 'registration_id': registration['registration_id'],
        'session_id': session, 'runtime': runtime, 'workspace': registration['workspace'],
        'pid': int(pid), 'started': str(started), 'owner': owner,
        'event': hook.get('source', 'startup') if hook['hook_event_name'] == 'SessionStart' else 'prompt',
        **local_instance()}
    if host is not None:
        value['managed_host'] = host
    write_json_private(Path(registration['workspace'])/'.qorqut/native-binding.json', value)
    return value


# This exact standard-library probe is also executed on the configured target.
# It never inspects a target PID through the dashboard host's /proc.
PROBE = '''import json,sys
from pathlib import Path
workspace,session,pane,agent,registration=sys.argv[1:]
def fields(pid):
 if sys.platform=='darwin':
  from qorqut import process_info
  return process_info.fields(pid)
 return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
marker=Path(workspace)/'.qorqut/native-binding.json'
if marker.is_symlink() or any(p.is_symlink() for p in marker.parents): raise ValueError('unsafe observation')
v=json.loads(marker.read_text())
assert v['session_id']==session and v['agent_id']==agent and v['registration_id']==registration and v['workspace']==workspace
pid=int(v['pid']);native=fields(pid)
assert native[0]!='Z' and native[19]==v['started']
if sys.platform=='darwin':
 from qorqut import process_info
 name=process_info.name(pid)
else: name=Path('/proc/'+str(pid)+'/comm').read_text().strip()
assert name in ({'claude','claude.exe'} if v['runtime']=='claude-code' else {'codex'})
argv=([arg.encode() for arg in process_info.cmdline(pid)] if sys.platform=='darwin'
 else Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0'))
assert not any(a in argv for a in (b'exec',b'queue',b'--print',b'-p',b'--agent-id'))
if b'app-server' in argv:
 assert sys.platform!='darwin', 'Shared app-server writer-lock ownership is unavailable on macOS'
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
if sys.platform=='darwin': assert process_info.start_token(pid)==v['started']
print(json.dumps(v))
'''

# The consistency probe for a managed agent's headless client, run on the local target only (a managed host is always local). It
# does not decide who the client is: the SERVICE passes in the pid and start ticks of the claude and of the host that the
# token-verified host identity reports, and this script only refuses when the machine disagrees (a different process, the
# workspace record or the hook's marker naming another one, a host that does not look like the launcher's, a ``claude`` between
# them). ``-p`` is waived here and nowhere else. The optional ninth argument names the client the service's identity says the host
# runs (``claude-code``, the default, or ``codex``): a Codex client must be an ``app-server --listen stdio://`` that holds this
# conversation's thread-writer lock (where the platform shows it).
MANAGED_PROBE = '''import json,re,sys,uuid
from pathlib import Path
workspace,session,agent,registration,cpid,cstarted,hpid,hticks=sys.argv[1:9]
runtime=sys.argv[9] if len(sys.argv)>9 else 'claude-code'
assert runtime in ('claude-code','codex')
names=('codex',) if runtime=='codex' else ('claude','claude.exe')
if sys.platform=='darwin':
 from qorqut import process_info
def fields(pid):
 if sys.platform=='darwin': return process_info.fields(pid)
 return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()
def comm(pid):
 if sys.platform=='darwin': return process_info.name(pid)
 return Path('/proc/'+str(pid)+'/comm').read_text().strip()
def argv(pid):
 if sys.platform=='darwin': return [arg.encode() for arg in process_info.cmdline(pid)]
 return Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\\0')
def shape(a):
 a=list(a)
 if a and a[-1]==b'': a.pop()
 if b'-m' not in a: return False
 i=a.index(b'-m')
 return 1<=i<=4 and a[0]!=b'' and all(f.startswith(b'-') for f in a[1:i]) and len(a)==i+4 and a[i+1]==b'qorqut.managed_runtime.host' and a[i+2]==b'--state' and 0<len(a[i+3])<=4096
def safe(path):
 if path.is_symlink() or any(p.is_symlink() for p in path.parents): raise ValueError('unsafe file')
 return json.loads(path.read_text())
v=safe(Path(workspace)/'.qorqut/native-binding.json')
assert v['session_id']==session and v['agent_id']==agent and v['registration_id']==registration and v['workspace']==workspace
assert v['runtime']==runtime
assert v['pid']==int(cpid) and v['started']==cstarted
record=Path(workspace)/'.qorqut/managed-host.json'
assert record.stat().st_size<=4096
h=safe(record)
assert h.get('agent_id',agent)==agent and h['host_pid']==int(hpid) and h['host_ticks']==hticks
host=int(hpid);pid=int(cpid)
assert host>1 and pid>1 and host!=pid
hf=fields(host)
assert hf[0]!='Z' and hf[19]==hticks and shape(argv(host))
if sys.platform!='darwin':
 try: exe=Path('/proc/'+str(host)+'/exe').readlink().name
 except OSError: exe=None
 assert exe is None or re.fullmatch(r'python[0-9.]*(\\.exe)?',exe.lower().replace(' (deleted)',''))
native=fields(pid)
assert native[0]!='Z' and native[19]==cstarted and comm(pid) in names
assert not any(a in argv(pid) for a in (b'exec',b'queue',b'--agent-id'))
if runtime=='codex':
 a=list(argv(pid))
 if a and a[-1]==b'': a.pop()
 assert a[-3:]==[b'app-server',b'--listen',b'stdio://']
 if sys.platform!='darwin':
  lock=str(uuid.UUID(session))+'.lock';held=False
  for d in Path('/proc/'+str(pid)+'/fd').iterdir():
   try: t=d.readlink()
   except OSError: continue
   if t.name==lock and t.parent.name=='thread-writer-locks':
    held=held or any(l.split()[2:6]==['FLOCK','ADVISORY','WRITE',str(pid)] for l in Path('/proc/'+str(pid)+'/fdinfo/'+d.name).read_text().splitlines() if l.startswith('lock:'))
  assert held
parent=int(native[1])
for _ in range(24):
 if parent==host: break
 assert parent>1 and comm(parent) not in names
 parent=int(fields(parent)[1])
else: raise ValueError('not a managed host child')
if sys.platform=='darwin': assert process_info.start_token(pid)==cstarted
print(json.dumps(v))
'''

DEAD_PROBE = '''import sys
from pathlib import Path
pid,started,owner=sys.argv[1:]
try:
 if sys.platform=='darwin':
  from qorqut import process_info
  f=process_info.fields(pid)
 else: f=Path('/proc/'+pid+'/stat').read_text().rsplit(')',1)[1].split()
 dead=f[0]=='Z' or f[19]!=started
 parts=owner.split(':')
 if not dead and len(parts)==5:
  if sys.platform=='darwin':
   print('unknown');sys.exit(0)
  descriptor=Path('/proc/'+pid+'/fd/'+parts[2]);info=descriptor.stat()
  dead=(info.st_dev,info.st_ino)!=(int(parts[3]),int(parts[4]))
  if not dead:
   dead=not any(line.split()[2:6]==['FLOCK','ADVISORY','WRITE',pid] for line in Path('/proc/'+pid+'/fdinfo/'+parts[2]).read_text().splitlines() if line.startswith('lock:'))
 print('dead' if dead else 'alive')
except (FileNotFoundError,ProcessLookupError): print('dead')
except (OSError,IndexError,ValueError): print('unknown')
'''


class NativeBindingAdapter:
    def __init__(self, runtime): self.runtime = runtime

    def publish_local(self, profile, selected):
        """Publish the committed owner generation to its still-current local hook.

        Remote targets retain their own supervisor publication. This never
        invents a session or generation, or writes through a replaced process.
        """
        from . import handoff, supervisor
        from .codex_session import owner_alive
        binding = selected.binding
        if binding.get('execution_target') != 'local' or not owner_alive(binding['owner']):
            return False
        path = Path(profile['workspace']) / '.qorqut/registration.json'
        registration = load_registration(path)
        if (registration['agent_id'] != profile['agent_id']
                or registration['registration_id'] != binding['registration_id']):
            return False
        native = handoff.observation(registration)
        if not handoff.same(native, binding):
            return False
        return supervisor.register(path, native, {'inbox_generation': selected.generation,
            'inbox_desired_state': selected.desired_state})

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

    def managed_identity(self, agent_id, session_id):
        """The service-held, token-verified identity of this agent's running managed host, or ``None``.

        This is the anchor of the managed path. It needs: the agent's mode is ``managed`` (a ``managed-host.json`` in the workspace
        of a terminal agent has no effect), the managed runtime is reachable from this service runtime, and it reports a live host
        with the claude process it spawned (pid and start ticks) and the host's own pid and start ticks. A conversation the host
        already knows must be the one being bound. Anything else, including a runtime that cannot be reached, is ``None``: the
        managed path then refuses and the terminal probe judges the session as it always did."""
        try:
            from qorqut.managed_runtime.manager import managed_of
            from qorqut.managed_runtime.mode import MANAGED, mode_of
            managed = managed_of(self.runtime)
            if managed is None or mode_of(getattr(managed, 'paths', None), agent_id) != MANAGED:
                return None
            identity = managed.identity(agent_id)
        except Exception:  # noqa: BLE001 - a runtime that cannot answer is not an authority
            return None
        if not isinstance(identity, dict) or identity.get('agent_id', agent_id) != agent_id or identity.get('state') not in LIVE_HOST_STATES:
            return None
        pid, started, host_pid, host_started = (identity.get(key) for key in ('pid', 'started', 'host_pid', 'host_started'))
        if (type(pid) is not int or pid <= 1 or type(host_pid) is not int or host_pid <= 1 or pid == host_pid
                or not (isinstance(started, str) and TICKS.fullmatch(started))
                or not (isinstance(host_started, str) and TICKS.fullmatch(host_started))):
            return None
        if identity.get('conversation_id') not in (None, session_id):
            return None
        return {'pid': pid, 'started': started, 'host_pid': host_pid, 'host_started': host_started,
                'runtime': 'codex' if identity.get('provider') == 'codex' else 'claude-code',
                'host_id': host_id_of(identity.get('instance'), host_pid, host_started)}

    def verify_managed(self, execution, profile, agent_id, registration_id, session_id):
        """The observation of a managed agent's headless client, or ``None`` to judge the session as a terminal one.

        Only for a managed-mode agent on this machine whose service-held host identity (``managed_identity``) names exactly
        the claimed claude process and host. The machine is then asked once more (``MANAGED_PROBE``) whether the hook's marker,
        the workspace record and the process tree agree with that identity; they are consistency checks, not the anchor. The
        returned observation takes its process and host facts from the identity, not from the workspace: no value an agent can
        write reaches the binding except the short marker fields in ``MARKER_KEYS``. A terminal agent, a remote target, a
        missing runtime, or any disagreement is ``None``."""
        if execution.target_identity is not None:
            return None
        try:
            known = self.managed_identity(agent_id, session_id)
            if known is None:
                return None
            record = Path(profile['workspace']) / MANAGED_HOST_RECORD
            if path_has_symlink(record) or not record.is_file():
                return None
            marker = json.loads(self.run(execution, MANAGED_PROBE, [
                profile['workspace'], session_id, agent_id, registration_id,
                known['pid'], known['started'], known['host_pid'], known['host_started'], known['runtime']]))
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            return None
        value = {key: marker[key] for key in MARKER_KEYS if isinstance(marker.get(key), str) and len(marker[key]) <= 256}
        value.update(pid=known['pid'], started=known['started'], owner=f"{known['pid']}:{known['started']}",
                     managed_host={'host_id': known['host_id'], 'host_pid': known['host_pid'], 'host_ticks': known['host_started']})
        return value

    def verify(self, agent_id, registration_id, session_id, *, previous=None):
        try:
            profile, execution, terminal = self.selected(agent_id)
            target = str(execution.target_identity or 'local')
            value = self.verify_managed(execution, profile, agent_id, registration_id, session_id)
            if value is None:
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
