"""One model-free supervisor per execution host; native clients are never restarted.

It repairs a session's local binding after a failed hook, delivers queued hook receipts and the one context-recovery wake, and hosts
the delegated Codex runtime of a session that has one. It does not poll or notify: pending inbox work is announced by the backend's
nudge dispatcher, which needs nothing on the agent's side."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import socket
from concurrent.futures import ThreadPoolExecutor

from qorqut_operations.contracts import DomainError
from qorqut.installation import installation_config
from qorqut.release_worker.lifetime import atomic_json, exclusion, identity
from .client import (BOOTSTRAP_TIMEOUT, REQUEST_TIMEOUT, BootstrapClient,
                     load_registration, write_json_private)
from .codex_session import owner_alive

# Parent session runtimes whose delegated workers this host runs (see delegated_runtime.RUNTIME_EXECUTABLES).
DELEGATION_RUNTIMES = frozenset({'codex','claude-code'})
HELPER_MODULE = 'qorqut.bootstrap.supervisor'  # the stable launcher runs it as __main__, so __name__ is not this allowlisted name
PERMANENT = {'forbidden','invalid_principal','inactive_agent','invalid_credential',
             'missing_credential','missing_registration','invalid_registration',
             'registration_conflict','inbox_session_superseded','inbox_generation_stale','inbox_selection_required'}


def register_pending(registration_path,native):
    config = installation_config()
    if config is None: return False
    root = Path(config['root'])
    atomic_json(root/'state/agent-recovery'/f"{native['registration_id']}-{native['session_id']}.json",
        {**native,'registration_path':str(registration_path)})
    return True


def local_probe(script, arguments):
    """The standard-library probe, run on this host: the supervisor is the execution host's own process."""
    return subprocess.check_output([sys.executable, '-I', '-c', script, *map(str, arguments)], text=True, timeout=5)


def register(registration_path, native, response):
    if response.get('inbox_generation') is None:
        return False
    config = installation_config()
    registration = load_registration(registration_path)
    binding = {'registration_id':registration['registration_id'],'session_id':native['session_id'],
        'generation':response['inbox_generation'],'owner':native['owner'],'runtime':native['runtime'],
        'host':native.get('host',socket.gethostname()),'registration_path':str(registration_path),'desired_state':response.get('inbox_desired_state','running')}
    write_json_private(Path(registration['workspace'])/'.qorqut/session-binding.json',binding)
    # A folder or desktop installation has no installation configuration and no supervisor: the receipt in the agent's own
    # folder is its whole binding, and its MCP bridge reads it there. A server installation also records it for its supervisor.
    if config is None:
        return True
    root = Path(config['root'])
    # Preserve cached command paths against native process start, independent
    # of configuration rewrites. Observation-based migration releases them.
    cached = Path(registration['workspace'])/'.qorqut/cached-runtime-references.json'
    if cached.is_file():
        values = json.loads(cached.read_text())
        from .runtime_references import cache
        cache(root,native['owner'],values['references'],registration_id=registration['registration_id'],
              session_id=native['session_id'])
    with exclusion(root,shared=True):
        atomic_json(root/'state/agent-supervisor'/f"{registration['registration_id']}.json",binding)
        root.joinpath('state/helper-faults',f"{registration['registration_id']}.json").unlink(missing_ok=True)
        pending=root/'state/agent-recovery'/f"{native['registration_id']}-{native['session_id']}.json"
        if pending.is_file():
            try:
                if json.loads(pending.read_text()).get('owner')==native['owner']:pending.unlink()
            except (OSError,ValueError):pass
    return True


class ObservedHelper:
    """Adopt only a verified helper PID/start after a supervisor restart."""
    def __init__(self, pid, started): self.pid, self.started = pid, started
    def poll(self): return None if identity(self.pid) == self.started else 0
    def terminate(self):
        if self.poll() is None: os.kill(self.pid, signal.SIGTERM)


class Supervisor:
    def __init__(self, config, *, spawn=subprocess.Popen, alive=owner_alive, probe=local_probe):
        self.config, self.spawn, self.alive, self.probe = config, spawn, alive, probe
        self.root = Path(config['root'])
        self.children = {}
        self.recoveries = {}
        self.retry_at = {}
        self.followups = {}
        self.followup_retry = {}
        self.context_check_at = 0
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.reference_future = None
        self.reference_check_at = 0
        self.report_future = None
        self.report_check_at = 0

    def recover(self,item):
        registration = load_registration(Path(item['registration_path']))
        from . import handoff
        def matches():
            try:
                native=handoff.observation(registration)
                return all(native.get(name)==item[name] for name in ('registration_id','session_id','owner','runtime'))
            except (OSError,ValueError,KeyError):return False
        if not matches():raise DomainError('inbox_generation_stale','Pending repair no longer matches its native hook observation')
        client = BootstrapClient(registration,timeout=BOOTSTRAP_TIMEOUT)
        try:
            response = client.post('/api/agent/bootstrap', item.get('bootstrap_body') or {
                'registration_id':item['registration_id'], 'session_id':item['session_id'],
                'event':item['event'],'runtime':item['runtime'],'last_effective_config_id':None})
            if response.get('ready') is not True:
                error = response.get('error') or {}
                raise DomainError(error.get('code','reprepare_required'), error.get('message','Configuration is unavailable'),
                    retryable=error.get('retryable') is True)
        except DomainError as error:
            if item.get('request_id') and matches():
                from . import recovery
                warm = bool((item.get('bootstrap_body') or {}).get('last_effective_config_id'))
                if not warm or error.code not in {'bootstrap_unavailable', 'busy'}:
                    recovery.record(registration,item['session_id'],item['runtime'],error.code,owner=item['owner'],details=error.details)
                if error.code in PERMANENT or not error.retryable and error.code not in {'busy','bootstrap_unavailable'}:
                    handoff.publish(self.root,registration,item,error={
                        'code':error.code,'message':error.message,'retryable':error.retryable})
                    return True
            raise
        if not matches():raise DomainError('inbox_generation_stale','Native selection changed during background repair')
        def publish_context():
            if item.get('request_id'):
                from . import handoff
                # Publish only after the binding writes. The next hook may
                # consume this immediately and then use its matching MCP binding.
                handoff.publish(self.root,registration,item,response=response)
        if response.get('inbox_generation') is None:
            if item.get('request_id'):
                publish_context();return True
            raise DomainError(response.get('inbox_binding_error','bootstrap_unavailable'),'Binding recovery is unavailable')
        if not matches():raise DomainError('inbox_generation_stale','Native selection changed during background repair')
        binding = {'registration_id':item['registration_id'],'session_id':item['session_id'],
            'generation':response['inbox_generation'],'owner':item['owner'],'runtime':item['runtime'],
            'host':item.get('host',socket.gethostname()),'registration_path':item['registration_path'],'desired_state':response.get('inbox_desired_state','running')}
        write_json_private(Path(registration['workspace'])/'.qorqut/session-binding.json',binding)
        atomic_json(self.root/'state/agent-supervisor'/f"{item['registration_id']}.json",binding)
        publish_context()
        return True

    def followup(self,item):
        registration = load_registration(Path(item['registration_path']))
        from . import handoff
        native = handoff.observation(registration)
        if not handoff.same(native,item) or not self.alive(item['owner']):
            return True
        client = BootstrapClient(registration,timeout=REQUEST_TIMEOUT)
        try:
            result = client.post(item['route'],item['body'])
        except DomainError as error:
            if error.code in PERMANENT | {'stale_delivery'}:return True
            raise
        if item['route'].endswith('/bootstrap/receipt'):
            try:
                from .technical_reporting import active, record
                if active(registration,'bootstrap','receipt',item['session_id']):
                    record(registration,item['session_id'],'bootstrap','receipt','success',
                        call_site='bootstrap.receipt',recovered=True)
            except Exception:pass  # Incident bookkeeping cannot undo delivery.
        return result.get('state') != 'deferred'  # a wake that was not typed yet is retried; every other state is final

    def reconcile_followups(self):
        from . import handoff, recovery
        # A prepared handoff may wake the existing session once. Its durable
        # notification ID survives supervisor restarts and uncertain submissions.
        for path in self.root.joinpath('state/agent-context').glob('*.json'):
            try:
                item = handoff.read(path)
                response = item.get('response') or {}
                if item.get('consumed') or item.get('wake_queued') or not response.get('inbox_available'):
                    continue
                registration = load_registration(Path(item['registration_path']))
                episode = recovery.read(registration,item['session_id'])
                if not episode or episode.get('code') in recovery.DENIALS:continue
                if episode.get('owner') not in (None,item['owner']):continue
                handoff.queue(registration,item,'/api/agent/inbox/recovery',{
                    'registration_id':item['registration_id'],'consumer_id':item['session_id'],
                    'notification_id':episode['failure_id'],'runtime':item['runtime'],'owner':item['owner'],
                    'binding_generation':response.get('inbox_generation',0)})
                with recovery.locked(registration,item['session_id']):
                    current = handoff.read(path)
                    if current.get('request_id') == item['request_id']:
                        write_json_private(path,{**current,'wake_queued':True})
            except (OSError,ValueError,KeyError,DomainError):continue
        now = time.monotonic()
        for path,future in list(self.followups.items()):
            if not path.exists() and future.done():
                del self.followups[path]
                self.followup_retry.pop(path,None)
        for path in self.root.joinpath('state/agent-followups').glob('*.json'):
            try:
                item = handoff.read(path)
                future = self.followups.get(path)
                if future is not None and future.done():
                    done = False
                    try:done = future.result()
                    except Exception:pass
                    if done and item.get('request_id') == future.request_id:
                        registration = load_registration(Path(item['registration_path']))
                        with recovery.locked(registration,item['session_id']):
                            if handoff.read(path).get('request_id') == future.request_id:
                                path.unlink(missing_ok=True)
                        self.followup_retry.pop(path,None)
                    else:
                        failures = self.followup_retry.get(path,(0,0))[1]+1
                        self.followup_retry[path] = (now+min(30,2**min(failures,5)),failures)
                    del self.followups[path]
                elif future is None and now >= self.followup_retry.get(path,(0,0))[0]:
                    future = self.pool.submit(self.followup,item)
                    future.request_id = item['request_id']
                    self.followups[path] = future
            except (OSError,ValueError,KeyError):continue

    def seed_context_recovery(self):
        """Continue a failed native hook even when the helper was replaced while it was idle."""
        from . import handoff, recovery
        for path in self.root.joinpath('state/agent-supervisor').glob('*.json'):
            try:
                binding = handoff.read(path)
                if binding.get('host',socket.gethostname()) != socket.gethostname():continue
                if binding['desired_state'] != 'running' or not self.alive(binding['owner']):continue
                registration_path = Path(binding['registration_path'])
                registration = load_registration(registration_path)
                episode = recovery.read(registration,binding['session_id'])
                if not episode or episode['code'] in recovery.DENIALS:continue
                native = handoff.observation(registration)
                if not handoff.same(native,binding) or episode.get('owner') not in (None,native['owner']):continue
                handoff.offer(self.root,registration_path,registration,native,{
                    'registration_id':native['registration_id'],'session_id':native['session_id'],
                    'runtime':native['runtime'],'event':'prompt'})
            except (OSError,ValueError,KeyError,DomainError):continue

    def replay_reports(self):
        """Drain existing outboxes even after a native session goes idle or exits."""
        from .technical_reporting import replay_http
        seen = set()
        for directory in ('agent-supervisor', 'agent-recovery', 'agent-recovery-denied'):
            for path in self.root.joinpath('state', directory).glob('*.json'):
                try:
                    item = json.loads(path.read_text())
                    if not isinstance(item, dict) or item.get('host', socket.gethostname()) != socket.gethostname():
                        continue
                    registration_path = item['registration_path']
                    if registration_path in seen:
                        continue
                    seen.add(registration_path)
                    registration = load_registration(Path(registration_path))
                    pending = Path(registration['workspace']) / '.qorqut/technical-reports/pending.jsonl'
                    if pending.is_file() and pending.stat().st_size:
                        replay_http(registration)
                except (OSError, ValueError, TypeError, KeyError, DomainError):
                    continue

    def tick(self):
        if time.monotonic() >= self.context_check_at:
            self.seed_context_recovery()
            self.context_check_at = time.monotonic()+2
        # Reference probes run automatically and cannot stall session supervision.
        if (self.reference_future is None or self.reference_future.done()) and time.monotonic() >= self.reference_check_at:
            from qorqut.release_worker.reference_targets import reconcile
            self.reference_future = self.pool.submit(reconcile, self.config, retire_exited=True)
            self.reference_check_at = time.monotonic() + 30
        # One background pass; slow/offline services never block supervision or a hook.
        if (self.report_future is None or self.report_future.done()) and time.monotonic() >= self.report_check_at:
            self.report_future = self.pool.submit(self.replay_reports)
            self.report_check_at = time.monotonic() + 5
        for path,future in list(self.recoveries.items()):
            if not path.exists() and future.done():
                del self.recoveries[path];self.retry_at.pop(path,None)
        for path in sorted(self.root.joinpath('state/agent-recovery').glob('*.json'))[:1000]:
            try:
                from .handoff import discard_pending
                item = json.loads(path.read_text())
                if item.get('host',socket.gethostname())!=socket.gethostname():continue
                if not self.alive(item['owner']):
                    registration = load_registration(Path(item['registration_path']))
                    pending = Path(registration['workspace']) / '.qorqut/technical-reports/pending.jsonl'
                    if pending.is_file() and pending.stat().st_size:
                        # A failed first bootstrap may have no supervisor binding.
                        # Keep its discovery pointer until background replay drains it.
                        continue
                    discard_pending(path,item); continue
                if not item.get('request_id'):
                    from . import handoff
                    registration_path = Path(item['registration_path'])
                    registration = load_registration(registration_path)
                    if handoff.same(handoff.observation(registration), item):
                        handoff.offer(self.root, registration_path, registration, item, {
                            'registration_id': item['registration_id'], 'session_id': item['session_id'],
                            'runtime': item['runtime'], 'event': 'prompt', 'last_effective_config_id': None})
                        item = handoff.read(path)
                future = self.recoveries.get(path)
                if future is None and item.get('request_id'):
                    from .handoff import read, result_path
                    # A crash after publishing, before reaping the future, must
                    # not refetch or reset the saved notification state.
                    if read(result_path(self.root,item)).get('request_id') == item['request_id']:
                        discard_pending(path,item)
                        continue
                if future and future.done():
                    if item.get('request_id') != getattr(future,'request_id',None):
                        del self.recoveries[path]
                        self.retry_at.pop(path,None)
                        continue  # A later hook queued a new request in the same slot.
                    try:
                        future.result(); discard_pending(path,item); self.retry_at.pop(path,None)
                    except Exception as error:
                        if isinstance(error,DomainError) and error.code in PERMANENT:
                            atomic_json(self.root/'state/agent-recovery-denied'/path.name,
                                {**item,'denial':error.code})
                            discard_pending(path,item);self.retry_at.pop(path,None)
                            del self.recoveries[path]
                            continue
                        failures = self.retry_at.get(path,(0,0))[1]+1
                        delay = (1,2,5,10,30)[min(failures-1,4)]
                        self.retry_at[path] = (time.monotonic()+delay,failures)
                    del self.recoveries[path]
                elif future is None and time.monotonic() >= self.retry_at.get(path,(0,0))[0]:
                    self.recoveries[path] = self.pool.submit(self.recover,item)
                    self.recoveries[path].request_id = item.get('request_id')
            except (OSError,ValueError,KeyError,DomainError): continue
        self.reconcile_followups()
        # Only a session with delegated workers (Codex or Claude Code) needs a process of its own; nothing else is hosted.
        wanted = {}
        for path in sorted(self.root.joinpath('state/agent-supervisor').glob('*.json'))[:1000]:
            try:
                item = json.loads(path.read_text())
                if item.get('host',socket.gethostname())!=socket.gethostname() or item.get('mode') is not None:continue
                registration = load_registration(Path(item['registration_path']))
                if item['registration_id'] != registration['registration_id']:
                    continue
                if item['desired_state'] != 'running' or not self.alive(item['owner']):
                    continue
                if item['runtime'] not in DELEGATION_RUNTIMES or registration.get('durable_delegation_enabled') is not True:
                    continue
                fault = self.root/'state/helper-faults'/f"{item['registration_id']}.json"
                if fault.is_file():
                    denied = json.loads(fault.read_text())
                    if all(denied.get(name)==item[name] for name in ('owner','session_id','generation')):
                        continue  # a terminal denial of exactly this binding is never retried by respawning
                wanted[(item['registration_id'],item['session_id'],item['generation'])] = item
            except (OSError,ValueError,KeyError,DomainError):
                continue
        for key, process in list(self.children.items()):
            if key not in wanted:
                if process.poll() is None: process.terminate()
                del self.children[key]
        for key,item in wanted.items():
            process = self.children.get(key)
            if process is not None and process.poll() is None:
                continue
            observation = self.root/'state/watcher-processes'/f"{item['registration_id']}.json"
            try:
                seen = json.loads(observation.read_text())
                if (seen.get('host',socket.gethostname())==socket.gethostname() and seen['session_id'] == item['session_id'] and seen['generation'] == item['generation']
                        and seen['owner'] == item['owner'] and identity(seen['pid']) == seen['started']
                        and self._watcher_process(seen['pid'])):
                    self.children[key] = ObservedHelper(seen['pid'], seen['started'])
                    continue
            except (OSError, ValueError, KeyError): pass
            with exclusion(self.root,shared=True):
                self.children[key] = self.spawn([str(self.root/'bin/agent-helper'),HELPER_MODULE,
                    '--watch',item['registration_path'],'--session',item['session_id'],
                    '--generation',str(item['generation']),'--owner',item['owner'],'--runtime',item['runtime']],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)

    @staticmethod
    def _watcher_process(pid):
        if sys.platform == 'darwin':
            from qorqut.process_info import cmdline
            return 'qorqut.bootstrap.supervisor' in cmdline(pid)
        return b'qorqut.bootstrap.supervisor' in Path(f'/proc/{pid}/cmdline').read_bytes()


def run_watch(path, session, generation, owner, runtime):
    """One supervised delegated-runtime host. ``--watch`` names the process kind the release worker already recognizes; it polls
    nothing. It runs while its exact session binding and native process are current and stops the delegated runtime when they are
    not."""
    registration = load_registration(path)
    lock_path = Path(registration['workspace'])/'.qorqut/supervised-watcher.lock'
    descriptor = os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor); return 0
    config = installation_config()
    def terminal(code):
        if config:
            atomic_json(Path(config['root'])/'state/helper-faults'/f"{registration['registration_id']}.json",
                {'code':code,'owner':owner,'session_id':session,'generation':generation})
    if config:
        atomic_json(Path(config['root'])/'state/watcher-processes'/f"{registration['registration_id']}.json",
            {'pid':os.getpid(),'started':identity(os.getpid()),'session_id':session,'generation':generation,'owner':owner,'host':socket.gethostname()})
    def alive():
        try:
            current = json.loads(Path(registration['workspace']).joinpath('.qorqut/session-binding.json').read_text())
            return owner_alive(owner) and current['session_id']==session and current['generation']==generation and current['desired_state']=='running'
        except (OSError,ValueError,KeyError,TypeError): return False
    def stop(*args): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    delegated = None
    try:
        if not alive() or registration.get('durable_delegation_enabled') is not True or runtime not in DELEGATION_RUNTIMES: return 0
        from .delegated_runtime import DelegatedRuntime
        delegated = DelegatedRuntime(registration,session,alive=alive,runtime=runtime); delegated.start()
        while alive(): time.sleep(1)
        return 0
    except DomainError as error:
        if error.code in PERMANENT: terminal(error.code)
        return 0
    except (KeyboardInterrupt,OSError): return 0
    finally:
        if delegated: delegated.stop()
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--watch',type=Path)
    parser.add_argument('--session'); parser.add_argument('--generation',type=int)
    parser.add_argument('--owner'); parser.add_argument('--runtime')
    args = parser.parse_args()
    if args.watch: return run_watch(args.watch,args.session,args.generation,args.owner,args.runtime)
    config = json.loads(args.config.read_text())
    path = Path(config['root'])/'state'/f'supervisor-{socket.gethostname()}.lock'; path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        supervisor = Supervisor(config)
        signal.signal(signal.SIGTERM, lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            while True:
                supervisor.tick(); time.sleep(.25)
        except KeyboardInterrupt: return 0
        finally:
            supervisor.pool.shutdown(wait=False, cancel_futures=True)
            for process in supervisor.children.values():
                if process.poll() is None: process.terminate()


if __name__ == '__main__': raise SystemExit(main())
