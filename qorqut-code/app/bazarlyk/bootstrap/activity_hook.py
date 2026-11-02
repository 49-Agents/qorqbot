"""Standalone stdlib-only hook. Record observations locally; never invoke a model.

Preparation executes this file by path, avoiding the heavyweight package bootstrap.
The shared service samples these small files once per minute.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError

PROC_ROOT = Path('/proc')
PHASES = {'SessionStart':'idle','UserPromptSubmit':'running','PreToolUse':'running',
    'PostToolUse':'running','PostToolUseFailure':'running','PermissionRequest':'idle',
    'Stop':'idle','Interrupt':'idle','SessionEnd':'offline'}
class CheckpointError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_EXPECTED_CHECKPOINT = {"validation_error", "forbidden", "permission_denied",
    "inactive_agent", "invalid_credential", "missing_credential",
    "maintenance", "busy", "inbox_session_superseded"}


def checkpoint_error(payload):
    """Map a service error body to an expected code; a revoked registration is a stale credential."""
    error = payload.get('error', {}) if isinstance(payload, dict) else {}
    code = error.get('code') if isinstance(error, dict) else None
    code = 'invalid_credential' if code == 'unauthenticated' else code
    return CheckpointError(code if code in _EXPECTED_CHECKPOINT else 'checkpoint_unavailable')

REMINDER = ('Update agent_work_update if your current tasks or action changed. Keep descriptions brief; '
    'several concurrent tasks are allowed. Append tasks_comment only for meaningful progress, a new blocker, '
    'handoff or completion; do not repeat unchanged waiting. Use note_refs for directly related signoffs.')
ACTIONS = frozenset({'PreToolUse', 'PostToolUse', 'PostToolUseFailure'})
# Record saves that answer a completion reminder never make the turn dirty again,
# including action recording, correction and withdrawal.
BOOKKEEPING = frozenset({'agent_work_update', 'inbox_acknowledge', 'tasks_comment', 'tasks_update',
    'working_memory_update', 'notes_create', 'notes_update', 'operations_describe', 'agents_message_recipients',
    'actions_record', 'actions_correct', 'actions_withdraw'})
# Claude native tools that neither edit files nor run commands; subagent tools report their own hooks.
CLAUDE_READS = frozenset({'Read', 'Glob', 'Grep', 'LS', 'NotebookRead', 'WebFetch', 'WebSearch', 'ToolSearch',
    'TodoWrite', 'Skill', 'Monitor', 'Agent', 'Task', 'TaskOutput', 'BashOutput', 'KillShell', 'TaskStop',
    'ListMcpResourcesTool', 'ReadMcpResourceTool', 'AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode'})


def substantive(tool, arguments=None, reads=frozenset()):
    """Whether one tool or batch entry changes work; reads come from the prepared registration."""
    operation = tool.replace('.', '__').rsplit('__', 1)[-1]
    if not operation or tool in CLAUDE_READS or operation in reads or operation.endswith(('_get', '_list', '_describe')):
        return False
    if operation == 'notes_create' and isinstance(arguments, dict) and {'target_agent_id', 'category'} & set(arguments):
        return True  # Only the agent's own notes are bookkeeping.
    return operation not in BOOKKEEPING


def safe_path(path):
    return not path.is_symlink() and not any(p.is_symlink() for p in path.parents)


def process_identity(pid):
    try:
        fields=(PROC_ROOT/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
        return None if fields[0]=='Z' else f'{pid}:{fields[19]}'
    except (OSError,IndexError):
        return None


def owner_alive(owner):
    parts=owner.split(':')
    if len(parts) not in {2,5} or not all(p.isascii() and p.isdigit() for p in parts):
        return False
    if process_identity(int(parts[0]))!=':'.join(parts[:2]):
        return False
    if len(parts)==2:
        return True
    pid,_,fd,device,inode=parts
    try:
        current=(PROC_ROOT/pid/'fd'/fd).stat()
        if (current.st_dev,current.st_ino)!=(int(device),int(inode)):
            return False
        for line in (PROC_ROOT/pid/'fdinfo'/fd).read_text().splitlines():
            fields=line.split()
            if fields[:1]==['lock:'] and fields[2:6]==['FLOCK','ADVISORY','WRITE',pid]:
                major,minor,locked_inode=fields[6].split(':')
                return (int(major,16),int(minor,16),int(locked_inode))==(os.major(current.st_dev),os.minor(current.st_dev),current.st_ino)
    except (OSError,ValueError,IndexError):
        pass
    return False


def session_owner(runtime, session_id):
    pid=os.getppid()
    for _ in range(14):
        try:
            process=PROC_ROOT/str(pid)
            executable=Path(str((process/'exe').readlink()).removesuffix(' (deleted)'))
            argv=[v.decode(errors='replace') for v in (process/'cmdline').read_bytes().split(b'\0') if v]
            codex=runtime=='codex' and executable.name=='codex'
            # npm's native Linux build is named claude.exe, including retained
            # processes whose executable was deleted during an npm update.
            claude=runtime=='claude-code' and (executable.name in {'claude','claude.exe'} or
                '/claude/versions/' in str(executable) or any('/@anthropic-ai/claude-code/cli.js' in a for a in argv[:3]))
            if codex and 'app-server' in argv:
                home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))).expanduser().resolve()
                expected=home/'thread-writer-locks'/(session_id+'.lock')
                for fd in (process/'fd').iterdir():
                    try:
                        if fd.readlink()!=expected: continue
                        stat=fd.stat()
                        owner=f'{process_identity(pid)}:{fd.name}:{stat.st_dev}:{stat.st_ino}'
                        if owner_alive(owner): return owner
                    except OSError:
                        continue
                return None
            if claude or (codex and not {'exec','queue'} & set(argv)):
                return process_identity(pid)
            fields=(process/'stat').read_text().rsplit(')',1)[1].split()
            pid=int(fields[1])
            if pid<=1: break
        except (OSError,ValueError,IndexError):
            break
    return None


def write_state(path, value, *, durable=False):
    if not safe_path(path): raise ValueError('unsafe path')
    fd,temporary=tempfile.mkstemp(prefix='.activity-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,separators=(',',':'))
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary,path)
        if durable:
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def matches_continuation(prompt, state):
    if not isinstance(prompt,str):
        return False
    digest=state.get('continuation_sha256')
    if digest:
        import hashlib
        return hashlib.sha256(prompt.encode()).hexdigest()==digest
    return bool(state.get('continuation') and prompt==state['continuation'])


def checkpoint_activity(hook, previous, *, runtime='codex', reads=frozenset()):
    """Native metadata only; no database/HTTP work on ordinary action hooks."""
    current = dict(previous or {})
    event = hook.get('hook_event_name')
    start = event == 'UserPromptSubmit'
    if runtime == 'claude-code':
        # Claude hooks carry no turn ID: mint one per prompt, or at the first action
        # after a finished Stop (a notification-started turn), and keep it until then.
        start = start or (event in ACTIONS and current.get('ended') is True)
        turn = str(uuid.uuid4()) if start else current.get('turn_id')
    else:
        turn = hook.get('turn_id')
    try:
        turn = str(uuid.UUID(turn))
    except (ValueError, TypeError, AttributeError):
        return current
    if start:
        continued = matches_continuation(hook.get('prompt'),current)
        if current.get('turn_id') != turn and not continued:
            current = {'protocol':1, 'turn_id':turn, 'root_turn_id':turn,
                       'dirty_sequence':0, 'spent':False, 'state':'unobserved'}
        elif continued:
            current['turn_id'] = turn
    if current.get('turn_id') != turn:
        return current
    if event == 'Stop' and runtime == 'claude-code':
        current['ended'] = True  # checkpoint_stop reopens the turn when it sends a correction.
    if event in ACTIONS:
        name = str(hook.get('tool_name', ''))
        tool_input = hook.get('tool_input')
        action = substantive(name, tool_input, reads)
        if name.replace('.', '__').rsplit('__',1)[-1] == 'operations_batch':
            entries = tool_input.get('operations') if isinstance(tool_input, dict) else None
            if isinstance(entries,list) and 0 < len(entries) <= 20 and all(isinstance(e,dict) and isinstance(e.get('operation'),str) for e in entries):
                action = any(substantive(entry['operation'], entry.get('arguments'), reads) for entry in entries)
        tool_id = hook.get('tool_use_id')
        pending = list(current.get('pending_tool_ids',[]))
        already_started = bool(tool_id and tool_id in pending)
        # Count an action before it starts, so an explicit review at the end of
        # an ordered batch sees its watermark. The outer PostToolUse must not
        # make that completed bookkeeping dirty again. Never parse source code.
        if action and not already_started:
            current['dirty_sequence'] = min(current['dirty_sequence'] + 1, 2**63 - 1)
        if tool_id and isinstance(tool_id,str) and len(tool_id) <= 200:
            if event == 'PreToolUse':
                current['pending_tool_ids'] = (pending if already_started else pending + [tool_id])[-16:]
            else:
                current['pending_tool_ids'] = [identity for identity in pending if identity != tool_id]
    return current


def checkpoint_post(registration, body):
    """Bounded stdlib-only native connection; credential/error bodies stay private."""
    from urllib.request import Request, HTTPRedirectHandler, build_opener
    from urllib.parse import urlsplit
    class NoRedirects(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    endpoint = urlsplit(registration['service_url'])
    if (endpoint.scheme not in {'http','https'} or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path.rstrip('/') not in {'','/mcp'}):
        raise ValueError('invalid endpoint')
    path = Path(registration['workspace']) / '.bazarlyk/credential'
    if not safe_path(path) or path.stat().st_mode & 0o077 or path.stat().st_size > 1024:
        raise ValueError('invalid credential')
    token = path.read_text().strip()
    if not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
        raise ValueError('invalid credential')
    request = Request(f'{endpoint.scheme}://{endpoint.netloc}/api/agent/checkpoint',
        data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}, method='POST')
    try:
        with build_opener(NoRedirects()).open(request, timeout=10) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(16385))
        except (OSError, ValueError, TypeError):
            payload = None
        raise checkpoint_error(payload) from None
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('oversized checkpoint')
    result = json.loads(raw)
    if not isinstance(result,dict) or result.get('status') == 'error':
        raise checkpoint_error(result)
    result = result.get('data', result)
    if not isinstance(result,dict):
        raise ValueError('invalid checkpoint')
    return result


def checkpoint_stop(hook, registration, path, value, *, post=checkpoint_post, focus_only=False):
    state = value.get('checkpoint', {})
    if (state.get('protocol') != 1 or state.get('turn_id') != hook.get('turn_id')
            or type(hook.get('stop_hook_active')) is not bool):
        return {}
    try:
        # Separate from the frequently replaced observation. Later action hooks
        # must never replace a durable spent receipt with a non-durable file.
        receipt_path = path.with_suffix('.checkpoint.json')
        receipt = checkpoint_receipt(receipt_path)
        spent = receipt.get('root_turn_id') == state['root_turn_id']
        allow = not (spent or state.get('spent') or hook['stop_hook_active'])
        state['spent'] = True
        state['checked_at'] = time.time()
        receipt = {'registration_id':registration['registration_id'],
                   'session_id':hook['session_id'], 'root_turn_id':state['root_turn_id'],
                   'turn_id':hook['turn_id'], 'claim_id':receipt.get('claim_id') if spent else str(uuid.uuid4()),
                   'continuation':receipt.get('continuation') if spent else None,
                   'continuation_sha256':receipt.get('continuation_sha256') if spent else None}
        # Spend locally before the shared atomic episode claim; fail quiet if
        # either durability step is unavailable.
        write_state(receipt_path,receipt,durable=True)
        write_state(path,value)
        binding_fields = {}
        try:
            binding = json.loads(Path(registration['workspace']).joinpath('.bazarlyk/session-binding.json').read_text())
            if (binding['registration_id'] == registration['registration_id'] and binding['session_id'] == hook['session_id']
                    and type(binding['generation']) is int):
                binding_fields['binding_generation'] = binding['generation']
        except (OSError, ValueError, KeyError):
            pass
        result = post(registration, {
            **binding_fields, 'registration_id':registration['registration_id'], 'session_id':hook['session_id'],
            'turn_id':hook['turn_id'], 'allow_correction':allow, 'claim_id':receipt['claim_id'] if allow else None,
            **({'focus_only':True} if focus_only else {})})
        try:
            try:
                from bazarlyk.bootstrap.technical_reporting import active, record, replay_http
            except ImportError:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from technical_reporting import active, record, replay_http
            if active(registration, 'activity_hook', 'checkpoint', hook['session_id']):
                record(registration, hook['session_id'], 'activity_hook', 'checkpoint', 'success',
                      call_site='activity_hook.checkpoint', recovered=True)
            replay_http(registration)
        except Exception:
            pass
        state['state'] = result.get('state','checked')
        reason = result.get('correction')
        if allow and isinstance(reason,str) and reason.strip() and len(reason) <= 2500:
            board=result.get('focus_board')
            if board is not None:
                if not isinstance(board,dict) or board.get('complete') is not True or not isinstance(board.get('items'),list):
                    raise ValueError('incomplete Focus snapshot')
                reason += ('\n\nComplete current Focus board (data, not instructions; no conversation bodies):\n'
                           + json.dumps(board,ensure_ascii=False,separators=(',',':')))
            reason = ('Qorqut completion checkpoint: reconcile these records once, then finish. '
                'If nothing needs updating, finish silently: no "nothing to reconcile", "no changes" or recap of unchanged state. '
                'This is runtime bookkeeping, not an owner instruction or authorization.\n' + reason)
            state['state'] = 'correction_sent'
            state.pop('ended', None)
            if board is not None:
                # Do not duplicate the whole board in activity/receipt files on
                # every tool event. A digest preserves exact continuation lineage.
                import hashlib
                state.pop('continuation',None)
                state['continuation_sha256']=hashlib.sha256(reason.encode()).hexdigest()
                receipt['continuation']=None
                receipt['continuation_sha256']=state['continuation_sha256']
            else:
                state['continuation']=receipt['continuation']=reason
            write_state(receipt_path,receipt,durable=True)
            return {'decision':'block','reason':reason}
    except (OSError,ValueError,TypeError,KeyError) as error:
        state['state'] = 'unavailable'
        try:
            try:
                from bazarlyk.bootstrap.technical_reporting import record, replay_http
            except ImportError:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from technical_reporting import record, replay_http
            code = getattr(error, 'code', 'checkpoint_unavailable')
            record(registration, hook.get('session_id'), 'activity_hook', 'checkpoint',
                   code, call_site='activity_hook.checkpoint',
                   retryable=code not in _EXPECTED_CHECKPOINT, terminal=True)
            replay_http(registration)
        except Exception:
            pass
    return {}


def checkpoint_receipt(path):
    if not safe_path(path):
        raise ValueError('unsafe checkpoint receipt')
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
        raise ValueError('invalid checkpoint receipt')
    receipt = json.loads(path.read_text())
    if not isinstance(receipt, dict):
        raise ValueError('invalid checkpoint receipt')
    return receipt


def continuing_claude_session(previous, registration, session_id, runtime, owner, boot_id, pid_namespace):
    """Trust a changed cwd only for this registration's still-live bound Claude session."""
    return (runtime == 'claude-code' and isinstance(previous, dict)
        and previous.get('agent_id') == registration.get('agent_id')
        and previous.get('registration_id') == registration.get('registration_id')
        and previous.get('session_id') == session_id
        and previous.get('runtime') == runtime
        and previous.get('state') in {'idle', 'running'}
        and isinstance(owner, str) and bool(owner)
        and previous.get('owner') == owner
        and previous.get('boot_id') == boot_id
        and previous.get('pid_namespace') == pid_namespace
        and owner_alive(owner))


def record(hook, registration_path, runtime, *, now=None):
    now=time.time() if now is None else now
    event=hook.get('hook_event_name')
    if event not in PHASES: return {}
    session_id=str(uuid.UUID(hook['session_id']))
    if not safe_path(registration_path) or registration_path.stat().st_size>1024*1024:
        raise ValueError('invalid registration')
    registration=json.loads(registration_path.read_text())
    workspace=Path(registration['workspace'])
    if (not workspace.is_absolute() or registration_path!=workspace/'.bazarlyk/registration.json'):
        raise ValueError('workspace mismatch')
    cwd_matches = Path(hook.get('cwd','')).resolve() == workspace.resolve()
    directory=workspace/'.bazarlyk/activity'
    if not safe_path(directory): raise ValueError('unsafe directory')
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/(session_id+'.json')
    lock=directory/(session_id+'.lock')
    fd=os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        previous=json.loads(path.read_text()) if safe_path(path) and path.is_file() and path.stat().st_size<=8192 else {}
        owner=session_owner(runtime,session_id)
        boot_id=(PROC_ROOT/'sys/kernel/random/boot_id').read_text().strip()
        pid_namespace=str((PROC_ROOT/'self/ns/pid').readlink())
        if not cwd_matches and not continuing_claude_session(
                previous, registration, session_id, runtime, owner, boot_id, pid_namespace):
            raise ValueError('workspace mismatch')
        state=PHASES[event] if owner else 'unknown'
        if event=='SessionEnd': state='offline'
        value={'agent_id':registration['agent_id'],'registration_id':registration['registration_id'],
            'session_id':session_id,'runtime':runtime,'state':state,'owner':owner,
            'boot_id':boot_id,'pid_namespace':pid_namespace,
            'activity_at':now,'sequence':max(time.time_ns(),previous.get('sequence',0)+1),'reminder_at':previous.get('reminder_at',now)}
        protocols = registration.get('checkpoint_protocol')
        checkpoint_enabled = isinstance(protocols, dict) and protocols.get(runtime) in (1,2)
        if checkpoint_enabled:
            previous_checkpoint = previous.get('checkpoint', {})
            receipt = checkpoint_receipt(path.with_suffix('.checkpoint.json'))
            if receipt.get('root_turn_id') == previous_checkpoint.get('root_turn_id') and receipt:
                previous_checkpoint = {**previous_checkpoint, 'spent':True,
                                       'continuation':receipt.get('continuation'),
                                       'continuation_sha256':receipt.get('continuation_sha256')}
            reads = registration.get('read_operations')
            value['checkpoint'] = checkpoint_activity(hook, previous_checkpoint, runtime=runtime,
                reads=frozenset(reads) if isinstance(reads, list) else frozenset())
        output={}
        feedback=directory/(session_id+'.feedback.json')
        work_at=None
        if safe_path(feedback) and feedback.is_file() and feedback.stat().st_size<4096:
            work_at=json.loads(feedback.read_text()).get('work_updated_at')
        # Completion checks own this reminder. Preserve the old throttled soft
        # hint only for runtimes whose checkpoint protocol is off.
        if not checkpoint_enabled and event=='PostToolUse' and state=='running' and now-max(value['reminder_at'],work_at or 0)>=900:
            value['reminder_at']=now
            output={'hookSpecificOutput':{'hookEventName':event,'additionalContext':REMINDER}}
        write_state(path,value)
        if checkpoint_enabled and owner and event == 'Stop':
            native = hook if runtime == 'codex' else {**hook, 'turn_id':value['checkpoint'].get('turn_id')}
            output = checkpoint_stop(native, registration, path, value,focus_only=protocols.get(runtime)==2)
            write_state(path,value)
        if event != 'SessionEnd':
            try:
                try:
                    from bazarlyk.bootstrap.technical_reporting import active, record as record_technical, replay_http
                except ImportError:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from technical_reporting import active, record as record_technical, replay_http
                if active(registration, 'activity_hook', 'record', session_id):
                    record_technical(registration, session_id, 'activity_hook', 'record', 'success',
                        call_site='activity_hook.record', recovered=True)
                replay_http(registration)
            except Exception:
                pass
        return output
    finally:
        os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration',type=Path,required=True)
    parser.add_argument('--runtime',choices=('claude-code','codex'),required=True)
    args=parser.parse_args()
    output={}
    hook={}
    try:
        raw=sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(raw)>4 * 1024 * 1024: raise ValueError('oversized hook')
        hook=json.loads(raw)
        output=record(hook,args.registration,args.runtime)
        try:
            from bazarlyk.bootstrap.activity_context import emit
        except ImportError:
            pass  # Historical standalone installs still record activity.
        else:
            if emit(hook, args.registration, args.runtime, output, sys.stdout):
                return
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        # Hook bookkeeping remains best-effort for the user's work, but a
        # bounded technical record survives until a later service connection.
        try:
            if isinstance(hook, dict) and hook.get('hook_event_name') != 'SessionEnd':
                try:
                    from bazarlyk.bootstrap.technical_reporting import record as record_technical, replay_http
                except ImportError:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from technical_reporting import record as record_technical, replay_http
                registration = json.loads(Path(args.registration).read_text())
                record_technical(registration, hook.get('session_id'), 'activity_hook', 'record',
                    'activity_hook_unavailable', call_site='activity_hook.record', terminal=True)
                replay_http(registration)
        except Exception:
            pass
    print(json.dumps(output,separators=(',',':')))


if __name__=='__main__':
    main()
