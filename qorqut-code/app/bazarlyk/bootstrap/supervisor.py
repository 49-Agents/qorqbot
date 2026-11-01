"""One model-free supervisor per execution host; native clients are never restarted."""
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

from bazarlyk.contracts import DomainError
from bazarlyk.installation import installation_config, helper_launcher
from bazarlyk.release_worker.lifetime import atomic_json, exclusion, pin, identity
from .client import (BOOTSTRAP_TIMEOUT, REQUEST_TIMEOUT, BootstrapClient,
                     load_registration, write_json_private)
from .codex_session import owner_alive
from .native_binding import observe_helpers

HELPER_MODULE = 'bazarlyk.bootstrap.supervisor'  # the stable launcher runs it as __main__, so __name__ is not this allowlisted name
LEGACY_PENDING = 'legacy_handoff_pending'
LEGACY_RETRY = 30
LEGACY = 'legacy'  # a migration-preserved generation-0 owner kept on its existing protocol
LEGACY_RECONCILE = 30  # an observed legacy owner is re-observed this often (owner, state, a resume after a pause)
LEGACY_CANDIDATE_RETRY = 10
LEGACY_GATE = 5  # between rechecks of the old watcher's exit and the owner, before a replacement may run
PERMANENT = {'forbidden','invalid_principal','inactive_agent','invalid_credential',
             'missing_credential','missing_registration','invalid_registration',
             'registration_conflict','inbox_session_superseded','inbox_generation_stale','inbox_watching_stopped'}


def register_pending(registration_path,native):
    config = installation_config()
    if config is None: return False
    root = Path(config['root'])
    atomic_json(root/'state/agent-recovery'/f"{native['registration_id']}-{native['session_id']}.json",
        {**native,'registration_path':str(registration_path)})
    return True


def maintained_legacy(native, response):
    """A migration-preserved watcher already belongs to the host supervisor.

    Do not enter the old hook-owned watcher lock protocol for this same owner.
    The supervisor holds that lock for its lifetime and does not watch its bytes.
    """
    from . import handoff
    config = installation_config()
    if not config or response.get('inbox_legacy_handoff') != 'pending':return False
    root = Path(config['root'])
    if not handoff.running(root):return False
    try:
        entry = handoff.read(root/'state/agent-supervisor'/f"{native['registration_id']}.json")
        return (handoff.same(entry,native) and entry.get('mode') == LEGACY and entry.get('generation') == 0
            and entry.get('desired_state') == 'running' and entry.get('host',socket.gethostname()) == socket.gethostname())
    except (OSError,ValueError):return False


def local_probe(script, arguments):
    """The standard-library probe, run on this host: the supervisor is the execution host's own process."""
    return subprocess.check_output([sys.executable, '-I', '-c', script, *map(str, arguments)], text=True, timeout=5)


def seed_legacy(root, registration_path, registration, session_id, *, source):
    """Offer an existing generation-0 owner to supervisor reconciliation.

    Nothing is verified or changed here: the backend and the target-local probe decide, later, whether this owner,
    registration and native process may be maintained. It is the same candidate the first activation writes.
    """
    with exclusion(Path(root), shared=True):
        atomic_json(Path(root)/'state/legacy-candidates'/f"{registration['registration_id']}.json",
            {'registration_id':registration['registration_id'],'agent_id':registration['agent_id'],'session_id':session_id,
             'registration_path':str(registration_path),'host':socket.gethostname(),'source':source})
    return True


def register(registration_path, native, response):
    config = installation_config()
    if config is None or response.get('inbox_generation') is None:
        return False
    registration = load_registration(registration_path)
    binding = {'registration_id':registration['registration_id'],'session_id':native['session_id'],
        'generation':response['inbox_generation'],'owner':native['owner'],'runtime':native['runtime'],
        'host':native.get('host',socket.gethostname()),'registration_path':str(registration_path),'desired_state':response.get('inbox_desired_state','running')}
    write_json_private(Path(registration['workspace'])/'.bazarlyk/session-binding.json',binding)
    root = Path(config['root'])
    # Preserve cached command paths against native process start, independent
    # of configuration rewrites. Observation-based migration releases them.
    cached = Path(registration['workspace'])/'.bazarlyk/cached-runtime-references.json'
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
        if native['runtime'] == 'codex':
            from .codex_session import state_path
            legacy = state_path(registration, native['session_id'])
            if legacy.exists():
                descriptor = os.open(legacy, os.O_WRONLY | os.O_NOFOLLOW)
                try: os.ftruncate(descriptor, 0)
                finally: os.close(descriptor)
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
        self.candidates = {}  # legacy candidate path -> in-flight observation and the next time to look
        self.gates = {}  # legacy child key -> in-flight recheck and the next time to look
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.reference_future = None
        self.reference_check_at = 0

    def recover(self,item):
        registration = load_registration(Path(item['registration_path']))
        from . import handoff
        observation = handoff.observation(registration)
        if observation.get('legacy_observation'):
            verified = self.legacy_observe(item)
            if verified['owner'] != item['owner']:
                raise DomainError('inbox_generation_stale', 'Legacy native owner changed')
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
                from . import handoff, recovery
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
        if response.get('inbox_generation') in (None, 0):
            if response.get('inbox_legacy_handoff')=='pending' or response.get('inbox_generation') == 0:
                # The upgrade waits for the old helpers' confirmed exit; meanwhile the same existing owner is maintained.
                seed_legacy(self.root,item['registration_path'],registration,item['session_id'],source='hook')
                if item.get('request_id'):
                    publish_context();return True
                raise DomainError(LEGACY_PENDING,'A pre-fencing helper consumer of this session has not exited',retryable=True)
            if item.get('request_id'):
                publish_context();return True
            raise DomainError(response.get('inbox_binding_error','bootstrap_unavailable'),'Binding recovery is unavailable')
        if not matches():raise DomainError('inbox_generation_stale','Native selection changed during background repair')
        binding = {'registration_id':item['registration_id'],'session_id':item['session_id'],
            'generation':response['inbox_generation'],'owner':item['owner'],'runtime':item['runtime'],
            'host':item.get('host',socket.gethostname()),'registration_path':item['registration_path'],'desired_state':response.get('inbox_desired_state','running')}
        write_json_private(Path(registration['workspace'])/'.bazarlyk/session-binding.json',binding)
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
            if error.code == 'inbox_watching_stopped':return False
            if error.code in PERMANENT | {'stale_delivery'}:return True
            raise
        if item['route'].endswith('/bootstrap/receipt'):
            try:
                from .technical_reporting import active, record
                if active(registration,'bootstrap','receipt',item['session_id']):
                    record(registration,item['session_id'],'bootstrap','receipt','success',
                        call_site='bootstrap.receipt',recovered=True)
            except Exception:pass  # Incident bookkeeping cannot undo delivery.
        return result.get('state') != 'queued'

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
                if response.get('inbox_desired_state','running') != 'running':continue
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

    def legacy_observe(self,item):
        """The backend's verdict on this existing owner: registration, state and the same verified native process."""
        registration = load_registration(Path(item['registration_path']))
        if registration['registration_id'] != item['registration_id']:
            raise DomainError('invalid_registration','Candidate does not match its prepared registration')
        value = BootstrapClient(registration,timeout=4).post('/api/agent/inbox/legacy',
            {'registration_id':item['registration_id'],'consumer_id':item['session_id']})
        if (not isinstance(value,dict) or value.get('generation') != 0 or value.get('session_id') != item['session_id']
                or not isinstance(value.get('owner'),str) or value.get('desired_state') not in {'running','paused','stopped'}):
            raise DomainError('inbox_binding_ambiguous','Legacy observation is invalid',retryable=True)
        return {**value,'agent_id':registration['agent_id']}

    def legacy_register(self,item,observed):
        """Keep this observed owner in the existing supervisor state, in explicit legacy mode.

        Only the explicit generation-0 mode is written; no fenced binding is published and no generation is claimed.
        A fenced registration of the same integration is never overwritten.
        """
        registration_id = item['registration_id']
        entry = self.root/'state/agent-supervisor'/f'{registration_id}.json'
        value = {'registration_id':registration_id,'session_id':item['session_id'],'generation':0,'owner':observed['owner'],
            'runtime':observed['runtime'],'host':socket.gethostname(),'registration_path':item['registration_path'],
            'desired_state':observed['desired_state'],'mode':LEGACY}
        with exclusion(self.root,shared=True):
            try: current = json.loads(entry.read_text())
            except (OSError,ValueError): current = None
            if current and current.get('mode') != LEGACY:
                raise DomainError('inbox_generation_stale','A fenced registration owns this integration')
            if current != value:
                atomic_json(entry,value)
            fault = self.root/'state/helper-faults'/f'{registration_id}.json'
            if value['desired_state'] == 'running' and fault.is_file():
                try: stopped = json.loads(fault.read_text()).get('code') == 'inbox_watching_stopped'
                except (OSError,ValueError): stopped = False
                if stopped: fault.unlink(missing_ok=True)

    def legacy_close(self,item,code,*,path=None):
        """This candidate can never be maintained (owner replaced, fenced, revoked, a different registration)."""
        registration_id = item['registration_id']
        atomic_json(self.root/'state/agent-recovery-denied'/f'legacy-{registration_id}.json',{**item,'denial':code})
        if path is not None: path.unlink(missing_ok=True)
        entry = self.root/'state/agent-supervisor'/f'{registration_id}.json'
        with exclusion(self.root,shared=True):
            try:
                if json.loads(entry.read_text()).get('mode') == LEGACY: entry.unlink(missing_ok=True)
            except (OSError,ValueError): pass

    def reconcile_legacy(self):
        """Verify offered legacy owners in the background and keep their supervisor state current."""
        now = time.monotonic()
        for path in sorted(self.root.joinpath('state/legacy-candidates').glob('*.json'))[:1000]:
            try:
                item = json.loads(path.read_text())
                if item.get('host',socket.gethostname()) != socket.gethostname(): continue
                slot = self.candidates.setdefault(path,{'future':None,'next':0})
                if slot['future'] is not None:
                    if not slot['future'].done(): continue
                    future, slot['future'] = slot['future'], None
                    try:
                        observed = future.result()
                        self.legacy_register(item,observed)
                        slot['next'] = now+LEGACY_RECONCILE
                    except DomainError as error:
                        if error.code in PERMANENT: self.legacy_close(item,error.code,path=path)
                        slot['next'] = now+LEGACY_CANDIDATE_RETRY
                    except Exception:
                        slot['next'] = now+LEGACY_CANDIDATE_RETRY  # Unobservable stays legacy/unknown.
                elif now >= slot['next']:
                    slot['future'] = self.pool.submit(self.legacy_observe,item)
            except (OSError,ValueError,KeyError): continue
        for path in [path for path in self.candidates if not path.exists()]:
            del self.candidates[path]

    def legacy_gate(self,item):
        """Recheck owner, registration, state and native identity, then that no pre-fencing watcher can still notify.

        An old watcher that is alive, or whose exit cannot be confirmed, keeps the integration on that watcher, and so
        does a helper whose kind is not known. A helper known to be an MCP bridge is not a notifier: it never blocks
        the replacement of a confirmed-exited watcher, even when its own state cannot be read (the protocol upgrade,
        unlike this recovery, still defers on it). Raises when the backend or the process table is unobservable.
        """
        observed = self.legacy_observe(item)
        if (observed['desired_state'] != 'running' or observed['owner'] != item['owner'] or observed['runtime'] != item['runtime']):
            self.legacy_register(item,observed)  # an explicit pause/stop or a changed native: nothing runs this round
            return False
        found = observe_helpers(observed['agent_id'],item['registration_path'],str(self.root/'state/process-pins'),self.probe,root=self.root)
        return all(entry.get('kind') == 'mcp_bridge' for entry in found)

    def legacy_ready(self,key,item):
        """Never blocks the tick: true only when a completed recheck allows exactly one replacement to start now."""
        gate = self.gates.setdefault(key,{'future':None,'next':0})
        now = time.monotonic()
        if gate['future'] is not None:
            if not gate['future'].done(): return False
            future, gate['future'] = gate['future'], None
            gate['next'] = now+LEGACY_GATE
            try: return bool(future.result())
            except DomainError as error:
                if error.code in PERMANENT: self.legacy_close({k:item[k] for k in ('registration_id','session_id','registration_path')},error.code)
            except Exception: pass
            return False
        if now >= gate['next']:
            gate['future'] = self.pool.submit(self.legacy_gate,item)
        return False

    def tick(self):
        if time.monotonic() >= self.context_check_at:
            self.seed_context_recovery()
            self.context_check_at = time.monotonic()+2
        # Reference probes run automatically and cannot stall watcher supervision.
        if (self.reference_future is None or self.reference_future.done()) and time.monotonic() >= self.reference_check_at:
            from bazarlyk.release_worker.reference_targets import reconcile
            self.reference_future = self.pool.submit(reconcile, self.config)
            self.reference_check_at = time.monotonic() + 30
        for path,future in list(self.recoveries.items()):
            if not path.exists() and future.done():
                del self.recoveries[path];self.retry_at.pop(path,None)
        for path in sorted(self.root.joinpath('state/agent-recovery').glob('*.json'))[:1000]:
            try:
                from .handoff import discard_pending
                item = json.loads(path.read_text())
                if item.get('host',socket.gethostname())!=socket.gethostname():continue
                if not self.alive(item['owner']):
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
                        # Ownership stays with the still-running legacy helpers; observing their exit is not urgent.
                        delay = LEGACY_RETRY if isinstance(error,DomainError) and error.code==LEGACY_PENDING else (1,2,5,10,30)[min(failures-1,4)]
                        self.retry_at[path] = (time.monotonic()+delay,failures)
                    del self.recoveries[path]
                elif future is None and time.monotonic() >= self.retry_at.get(path,(0,0))[0]:
                    self.recoveries[path] = self.pool.submit(self.recover,item)
                    self.recoveries[path].request_id = item.get('request_id')
            except (OSError,ValueError,KeyError): continue
        self.reconcile_followups()
        self.reconcile_legacy()
        wanted = {}
        for path in sorted(self.root.joinpath('state/agent-supervisor').glob('*.json'))[:1000]:
            try:
                item = json.loads(path.read_text())
                if item.get('host',socket.gethostname())!=socket.gethostname():continue
                registration = load_registration(Path(item['registration_path']))
                if item['registration_id'] != registration['registration_id']:
                    continue
                if item['desired_state'] != 'running' or not self.alive(item['owner']):
                    continue
                fault=self.root/'state/helper-faults'/f"{item['registration_id']}.json"
                if fault.is_file():
                    denied=json.loads(fault.read_text())
                    if all(denied.get(name)==item[name] for name in ('owner','session_id','generation')):
                        continue
                # A hook observation is not a separate ownership authority.
                if item.get('mode') == LEGACY:
                    if item['generation'] != 0: continue
                    wanted[(item['registration_id'],item['session_id'],0,LEGACY,item['owner'])] = item
                else:
                    wanted[(item['registration_id'],item['session_id'],item['generation'])] = item
            except (OSError,ValueError,KeyError,DomainError):
                continue
        for key, process in list(self.children.items()):
            if key not in wanted:
                if process.poll() is None: process.terminate()
                del self.children[key]
        for key in [key for key in self.gates if key not in wanted]:
            del self.gates[key]
        for key,item in wanted.items():
            process = self.children.get(key)
            if process is not None and process.poll() is None:
                continue
            observation = self.root/'state/watcher-processes'/f"{item['registration_id']}.json"
            try:
                seen = json.loads(observation.read_text())
                if (seen.get('host',socket.gethostname())==socket.gethostname() and seen['session_id'] == item['session_id'] and seen['generation'] == item['generation']
                        and seen['owner'] == item['owner'] and seen.get('mode') == item.get('mode') and identity(seen['pid']) == seen['started']
                        and b'bazarlyk.bootstrap.supervisor' in Path(f"/proc/{seen['pid']}/cmdline").read_bytes()):
                    self.children[key] = ObservedHelper(seen['pid'], seen['started'])
                    continue
            except (OSError, ValueError, KeyError): pass
            legacy = item.get('mode') == LEGACY
            if legacy and not self.legacy_ready(key,item):
                continue
            with exclusion(self.root,shared=True):
                self.children[key] = self.spawn([str(self.root/'bin/agent-helper'),HELPER_MODULE,
                    '--watch',item['registration_path'],'--session',item['session_id'],
                    '--generation',str(item['generation']),'--owner',item['owner'],'--runtime',item['runtime'],
                    *(['--legacy'] if legacy else [])],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)


def run_watch(path, session, generation, owner, runtime, legacy=False):
    """One supervised watcher. ``legacy`` maintains the existing generation-0 owner on its existing protocol.

    A legacy watcher runs only while the supervisor's explicit legacy entry still names this exact session and native
    owner as running; it names generation 0 to the service, so a fenced or replaced owner stops it. It never reads or
    publishes a fenced binding, and it never starts delegated work.
    """
    from bazarlyk.inbox.watcher import watch, notification_receipt
    registration = load_registration(path)
    client = BootstrapClient(registration,timeout=REQUEST_TIMEOUT)
    lock_path = Path(registration['workspace'])/'.bazarlyk/supervised-watcher.lock'
    descriptor = os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor); return 0
    legacy_descriptor = None
    if runtime == 'codex':
        from .codex_session import state_path
        legacy_descriptor = os.open(state_path(registration, session), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try: fcntl.flock(legacy_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(legacy_descriptor); os.close(descriptor); return 0
    config = installation_config()
    def terminal(code):
        if config:
            atomic_json(Path(config['root'])/'state/helper-faults'/f"{registration['registration_id']}.json",
                {'code':code,'owner':owner,'session_id':session,'generation':generation})
    if config:
        atomic_json(Path(config['root'])/'state/watcher-processes'/f"{registration['registration_id']}.json",
            {'pid':os.getpid(),'started':identity(os.getpid()),'session_id':session,'generation':generation,'owner':owner,'host':socket.gethostname(),
             **({'mode':LEGACY} if legacy else {})})
    def alive():
        try:
            if legacy:
                current = json.loads((Path(config['root'])/'state/agent-supervisor'/f"{registration['registration_id']}.json").read_text())
                return (owner_alive(owner) and current.get('mode')==LEGACY and current['session_id']==session and current['owner']==owner
                    and current['generation']==0 and current['desired_state']=='running')
            current = json.loads(Path(registration['workspace']).joinpath('.bazarlyk/session-binding.json').read_text())
            return owner_alive(owner) and current['session_id']==session and current['generation']==generation and current['desired_state']=='running'
        except (OSError,ValueError,KeyError,TypeError): return False
    backend_observation={'instance':None,'checked':0}
    def sleep(seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            if not alive(): raise KeyboardInterrupt
            if seconds>1 and time.monotonic()-backend_observation['checked']>=1:
                backend_observation['checked']=time.monotonic()
                try:
                    status=client.post('/api/agent/inbox/status',{'registration_id':registration['registration_id'],
                        'consumer_id':session,'binding_generation':generation})
                    if status['desired_state']!='running':raise KeyboardInterrupt
                    changed=backend_observation['instance'] is not None and status['instance']!=backend_observation['instance']
                    backend_observation['instance']=status['instance']
                    if changed:return  # A real poll follows backend replacement, separate from ordinary cadence.
                except DomainError as error:
                    if error.code in PERMANENT:
                        terminal(error.code);raise KeyboardInterrupt
                    return  # Watcher owns the one fast outage retry loop.
            time.sleep(min(.25,max(0,deadline-time.monotonic())))
    def emit(message):
        receipt = notification_receipt(message)
        if receipt is None:
            return  # Empty polling and repair never schedule maintenance model turns.
        result = client.post('/api/agent/inbox/deliver',{
            'registration_id':registration['registration_id'],'consumer_id':session,
            'binding_generation':generation,'notification_id':receipt['notification_id'],'runtime':runtime,
            **({'owner':owner} if legacy else {})})
        if result['state'] == 'queued':
            raise DomainError('inbox_delivery_waiting','Native composer is waiting',retryable=True)
        if result['state'] == 'uncertain':
            raise DomainError('inbox_delivery_uncertain','Native submission awaits reconciliation')
    def stop(*args): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    delegated = None
    try:
        if not alive(): return 0
        if registration.get('durable_delegation_enabled') is True and runtime=='codex' and not legacy:
            from .delegated_runtime import DelegatedRuntime
            delegated = DelegatedRuntime(registration,session,alive=alive); delegated.start()
        return watch(client,session,emit=emit,sleep=sleep,interval=registration.get('watcher_interval_seconds',300),
                     generation=generation,on_terminal=terminal)
    except (KeyboardInterrupt,DomainError,OSError): return 0
    finally:
        if delegated: delegated.stop()
        if legacy_descriptor is not None: os.close(legacy_descriptor)
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--watch',type=Path)
    parser.add_argument('--session'); parser.add_argument('--generation',type=int)
    parser.add_argument('--owner'); parser.add_argument('--runtime')
    parser.add_argument('--legacy',action='store_true')
    parser.add_argument('--set-state',type=Path)
    parser.add_argument('--desired-state',choices=('running','paused','stopped'))
    args = parser.parse_args()
    if args.set_state:
        registration=load_registration(args.set_state)
        config=installation_config()
        entry=Path(config['root'])/'state/agent-supervisor'/f"{registration['registration_id']}.json" if config else None
        state_path=Path(registration['workspace'])/'.bazarlyk/session-binding.json'
        try: binding=json.loads(entry.read_text()) if entry else None
        except (OSError,ValueError): binding=None
        # An existing generation-0 owner is changed through its explicit legacy entry; a fenced binding through its own file.
        legacy=bool(binding and binding.get('mode')==LEGACY)
        if not legacy: binding=json.loads(state_path.read_text())
        response=BootstrapClient(registration,timeout=REQUEST_TIMEOUT).post('/api/agent/inbox/state',{
            'registration_id':registration['registration_id'],'consumer_id':binding['session_id'],
            'binding_generation':binding['generation'],'desired_state':args.desired_state})
        binding['desired_state']=response['desired_state']
        if not legacy: write_json_private(state_path,binding)
        if config:atomic_json(entry,binding)
        return 0
    if args.watch: return run_watch(args.watch,args.session,args.generation,args.owner,args.runtime,args.legacy)
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
