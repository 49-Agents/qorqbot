"""Bounded, model-free technical incident observation and ingestion.

This module is deliberately independent of the application dispatcher.  A
boundary may call :meth:`IncidentReporter.observe` from an exception path; the
method only writes a small private spool when no writable unit of work is
available.  A service worker later calls :meth:`drain` with the registered
System Dev principal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Mapping
import fcntl
from threading import Event, Thread
import time
import tempfile
from bazarlyk.agents.global_scope import active_global, effective_business_ids, reaches_business
from bazarlyk.bootstrap.activity_hook import safe_path

from qorqut_operations.contracts import canonical_bytes, utc_now
from qorqut_operations.application import OperationDescriptor


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}

MAX_OBSERVATION_BYTES = 4096
MAX_SPOOL_BYTES = 16 * 1024 * 1024
MAX_DRAIN = 100
SCHEMA_VERSION = 1
INCIDENT_PROGRAM_ID = "system.incident_reporting"

_DYNAMIC = re.compile(r"(?:[0-9a-f]{8,}|\b\d{2,}\b)", re.I)
_NORMAL = frozenset({
    "validation_error", "forbidden", "permission_denied", "approval_pending",
    "revision_conflict", "login_required", "account_missing", "setup_incomplete",
    "pending", "hold", "not_found", "policy_denied", "invalid_credential",
    "missing_credential", "inactive_agent", "inbox_session_superseded", "inbox_consumer_busy",
    "context_refresh_required", "stale_proposal", "idempotency_conflict", "configuration_missing",
    "operation_unavailable", "scope_required", "invalid_transition", "maintenance",
    "cancelled", "service_stopping", "management_unavailable", "unsupported_runtime",
    "channel_required",
})
_SOFTWARE = {'internal_error','invalid_response','invalid_result','invalid_output','invariant_violation'}
_TECHNICAL_RETRY = {'busy','storage_busy','bootstrap_unavailable','reprepare_required','inbox_delivery_unavailable',
                    'provider_unavailable','timeout','connection_error','service_unavailable'}
# An approved account email send that ended without a confirmed submission. Reportable on its own: it is neither a
# software fault nor a normal workflow result, and only a bound account-mail send job's terminal outcome is given it.
DELIVERY_FAILURE = "external_delivery_failure"
_REPORTABLE = frozenset({"software_fault", "infrastructure_fault", "setup_contract", DELIVERY_FAILURE})
RECONCILE_BATCH = 10
_HOOK_COMPONENTS = frozenset({'bootstrap', 'inbox', 'activity_hook'})
_HOOK_OPERATIONS = frozenset({'prepare', 'receipt', 'review', 'watch', 'queue', 'record', 'checkpoint', 'release'})
_SAFE_SYMBOL = re.compile(r'^[a-z][a-z0-9_.-]{0,79}$')
_CALL_SITES = frozenset({'bootstrap.prepare', 'bootstrap.handle_hook', 'bootstrap.receipt', 'bootstrap.review', 'inbox.watcher.watch',
    'inbox.watcher.queue', 'inbox.watcher.release', 'activity_hook.record',
    'activity_hook.checkpoint'})


@dataclass(frozen=True)
class Observation:
    observation_id: str
    observed_at: str
    component: str
    operation: str
    error_code: str
    classification: str
    retryable: bool
    attempts: int
    call_site: str
    business_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    job_id: str | None = None
    receipt_id: str | None = None
    release: str = ""
    message: str = ""
    recovered: bool = False

    @property
    def fingerprint(self) -> str:
        stable = {
            "v": SCHEMA_VERSION, "component": self.component,
            "operation": self.operation, "error_code": self.error_code,
            "classification": self.classification, "call_site": self.call_site,
            "business_id": self.business_id,
        }
        return hashlib.sha256(canonical_bytes(stable)).hexdigest()

    def to_dict(self) -> dict:
        return self.__dict__ | {"fingerprint": self.fingerprint}


def _text(value, limit: int) -> str:
    value = "" if value is None else str(value)
    value = " ".join(value.split())
    return value[:limit]


def _stable_call_site(value) -> str:
    return _text(_DYNAMIC.sub("#", value or "unknown"), 160)


def classify(*, error_code="", classification=None, retryable=False, attempts=1,
             terminal=False, trusted_result=False, principal_missing=False,
             storage_failure=False, consecutive_failures=0, span_seconds=0) -> str:
    """Return a versioned deterministic class; normal workflow results are excluded."""
    code = _text(error_code, 80).lower()
    if classification:
        classification = _text(classification, 40).lower()
    if code in _NORMAL or classification in _NORMAL:
        return "normal_workflow"
    if classification == DELIVERY_FAILURE:
        return DELIVERY_FAILURE
    if principal_missing:
        return "setup_contract"
    if trusted_result or code in _SOFTWARE:
        return "software_fault"
    if terminal and (attempts > 1 or not retryable):
        return "infrastructure_fault" if storage_failure else "software_fault"
    if storage_failure and consecutive_failures >= 3 and span_seconds >= 600:
        return "infrastructure_fault"
    if retryable and (storage_failure or code in _TECHNICAL_RETRY):
        return "transient_observation"
    return classification or "operational_observation"


class IncidentReporter:
    """A bounded reporter with no provider/model calls and no task completion authority."""

    def __init__(self, spool_dir, *, clock=utc_now, release=""):
        self.spool_dir = Path(spool_dir)
        self.clock = clock
        self.release = _text(release, 80)
        self._lock = threading.Lock()
        self._active = set()
        self._success_pending = set()
        self.dropped_observations = 0
        self.last_drain = {'state': 'unobserved', 'at': None}
        self.last_reconcile = {'state': 'unobserved', 'at': None}

    def health(self):
        """Bounded local health; never report a reporting failure as a new incident."""
        dropped = self.dropped_observations
        try:
            path = self.spool_dir / 'system-incidents.health'
            if safe_path(path) and path.is_file() and path.stat().st_size < 1024:
                dropped = max(dropped, int(json.loads(path.read_text()).get('dropped_observations', 0)))
            spool = self.spool_dir / 'system-incidents.jsonl'
            size = spool.stat().st_size if safe_path(spool) and spool.is_file() else 0
        except (OSError, ValueError, TypeError):
            return {**self.last_drain, 'state': 'unavailable', 'dropped_observations': dropped}
        return {**self.last_drain, 'spool_bytes': size, 'dropped_observations': dropped}

    def boundary(self, context, component, operation, call, *, readiness=False):
        """Observe a trusted service boundary without changing its return/exception."""
        from qorqut_operations.contracts import DomainError
        def record(error=None):
            try:
                code = error.code if error else 'success'
                return self.observe(self.observation(component=component, operation=operation,
                    call_site=component, error_code=code,
                    retryable=bool(error and (error.retryable or readiness and code == 'reprepare_required')),
                    principal_missing=bool(error and (error.details or {}).get('incident_classification') == 'setup_contract'),
                    business_id=getattr(context, 'business_id', None),
                    agent_id=context.principal_id if context.principal_kind == 'agent' else None,
                    request_id=getattr(context, 'request_id', None), recovered=error is None))
            except Exception:
                return {'status': 'unavailable'}
        try:
            result = call()
        except DomainError as exc:
            outcome = record(exc)
            if isinstance(outcome, dict) and outcome.get('status') != 'unavailable':
                try:
                    setattr(exc, '_qorqut_incident_reported', True)
                except Exception:
                    pass
            raise
        except sqlite3.OperationalError as exc:
            outcome = record(DomainError('storage_busy', 'Operational storage unavailable', retryable=True))
            if isinstance(outcome, dict) and outcome.get('status') != 'unavailable':
                try:
                    setattr(exc, '_qorqut_incident_reported', True)
                except Exception:
                    pass
            raise
        except Exception as exc:
            outcome = record(DomainError('internal_error', 'Trusted service boundary failed'))
            if isinstance(outcome, dict) and outcome.get('status') != 'unavailable':
                try:
                    setattr(exc, '_qorqut_incident_reported', True)
                except Exception:
                    pass
            raise
        if readiness:
            if isinstance(result, Mapping):
                ready = result.get('ready') is True
                code = (result.get('error') or {}).get('code', 'bootstrap_unavailable')
            else:
                data = getattr(result, 'data', None)
                preparation = data.get('preparation', {}) if isinstance(data, Mapping) else {}
                ready = preparation.get('ready') is True
                code = 'bootstrap_unavailable'
            if not ready:
                record(DomainError(code, 'Preparation unavailable', retryable=True))
            else:
                record()
        else:
            record()
        return result

    def transport_failure(self, *, component, operation, request_id=None, context=None,
                          agent_id=None, session_id=None, business_id=None):
        """Record a trusted transport crash with bounded correlation data."""
        if context is not None:
            business_id = getattr(context, 'business_id', business_id)
            request_id = getattr(context, 'request_id', request_id)
            if getattr(context, 'principal_kind', None) == 'agent':
                agent_id = getattr(context, 'principal_id', agent_id)
        observation = self.observation(component=component, operation=operation,
            error_code='internal_error', trusted_result=True, call_site=component,
            business_id=business_id, agent_id=agent_id, session_id=session_id,
            request_id=request_id)
        return self.observe(observation)

    def transport_success(self, *, component, operation, request_id=None, context=None,
                          agent_id=None, session_id=None, business_id=None):
        """Queue a matching success probe for a previously reported crash."""
        if context is not None:
            business_id = getattr(context, 'business_id', business_id)
            request_id = getattr(context, 'request_id', request_id)
            if getattr(context, 'principal_kind', None) == 'agent':
                agent_id = getattr(context, 'principal_id', agent_id)
        observation = self.observation(component=component, operation=operation,
            error_code='success', call_site=component, business_id=business_id,
            agent_id=agent_id, session_id=session_id, request_id=request_id,
            recovered=True)
        return self.observe(observation)

    def job_result(self, row, error=None):
        """Called after a job attempt, outside its transaction; no new provider retry."""
        self.send_outcome(row)
        code = error.get('code', '') if error else 'success'
        known_technical = code in _TECHNICAL_RETRY | _SOFTWARE or code == 'background_failed'
        observation = self.observation(component='background', operation=row['kind'],
            call_site='background.run_one', error_code=code,
            business_id=row['business_id'], job_id=row['id'],
            principal_missing=bool(error and (error.get('details') or {}).get('incident_classification') == 'setup_contract'),
            attempts=row['attempts'], retryable=bool(error and error.get('retryable')),
            terminal=bool(error and known_technical and code != 'busy'),
            trusted_result=code == 'background_failed', recovered=error is None)
        return self.observe(observation)

    def send_outcome(self, row):
        """Queue the one stable outcome observation of a terminally failed account-mail send, from its committed job row.

        Only the observation is queued; ingestion re-reads the job. A lost or unspooled one is found again by
        reconciliation, so nothing here is relied on for correctness and no failure escapes."""
        try:
            from bazarlyk.domains import account_email_outcome as outcome
            if row['state'] != 'failed' or outcome.bound_send(row) is None or outcome.stage(row) not in outcome.REPORTABLE:
                return None
            return self.observe(self.send_observation(row))
        except Exception:
            return None

    def send_observation(self, row):
        from bazarlyk.domains import account_email_outcome as outcome
        code = 'external_unconfirmed' if outcome.stage(row) == 'unconfirmed' else 'external_failed'
        observation = self.observation(component='background', operation=outcome.JOB_KIND,
            call_site='background.account_email_send', error_code=code, classification=DELIVERY_FAILURE,
            agent_id=row['initiated_by_principal_id'], job_id=row['id'], attempts=row['attempts'])
        return replace(observation, observation_id=outcome.observation_id(row['id']))

    def unreported_account_sends(self, uow, limit=RECONCILE_BATCH):
        """Ids of terminally failed account-mail send jobs with no retained delivery impact, oldest first (bounded).

        A job the provider accepted before its own record failed is no failed send and is never listed."""
        from bazarlyk.domains import account_email_outcome as outcome
        operations = sorted(outcome.SEND_OPERATIONS)
        return [row[0] for row in uow.connection.execute(
            'SELECT j.id FROM jobs j WHERE j.business_id IS NULL AND j.kind=? AND j.state=\'failed\' '
            "AND json_extract(j.definition_ref_json,'$.account_email_operation') IN (" + ','.join('?' for _ in operations) + ') '
            "AND COALESCE(" + outcome.settled_receipt_sql() + ",'') <> 'completed' "
            'AND NOT EXISTS (SELECT 1 FROM system_incident_impacts x JOIN system_incidents i ON i.id=x.incident_id '
            'WHERE i.classification=? AND x.job_id=j.id) ORDER BY j.updated_at,j.id LIMIT ?',
            (outcome.JOB_KIND, *operations, DELIVERY_FAILURE, max(1, min(int(limit), 100))))]

    def report_account_send(self, uow, job_id, *, reporter_id):
        """Ingest one job's outcome through the same idempotent path as a spooled observation (job row authoritative)."""
        from bazarlyk.domains import account_email_outcome as outcome
        from qorqut_operations.storage.repositories import decode_row
        found = uow.connection.execute('SELECT * FROM jobs WHERE id=? AND business_id IS NULL AND kind=?',
                                       (job_id, outcome.JOB_KIND)).fetchone()
        if found is None:
            return {'created': False, 'updated': False, 'recovered': False, 'skipped': True, 'reason': 'job_missing'}
        return self._ingest(uow, self.send_observation(decode_row(found)), reporter_id)

    def load_active(self, database):
        """Warm the bounded success-probe index at startup and after each drain."""
        with database.read() as uow:
            rows = uow.connection.execute("SELECT DISTINCT json_extract(o.payload_json,'$.component'),x.operation,"
                "json_extract(o.payload_json,'$.call_site'),x.business_id,x.agent_id,x.session_id,x.task_id,x.job_id,x.receipt_id FROM system_incident_impacts x "
                'JOIN system_incidents i ON i.id=x.incident_id JOIN system_incident_observations o '
                "ON o.id=i.representative_observation_id WHERE x.recovered_at IS NULL AND x.operation IS NOT NULL AND i.status<>'closed' LIMIT 4096").fetchall()
            pending = {tuple(row) for row in rows}
            pending.update(tuple(row) for row in uow.connection.execute(
                "SELECT component,operation,call_site,business_id,nullif(agent_key,''),nullif(session_id,''),NULL,NULL,NULL FROM system_incident_fault_runs LIMIT 4096"))
        with self._lock:
            self._active.difference_update(self._success_pending - pending)
            self._active.update(sorted(pending,key=str)[:max(0,4096-len(self._active))])
            self._success_pending.intersection_update(pending)

    def observation(self, *, component, operation, error_code="", message="",
                    classification=None, retryable=False, attempts=1,
                    terminal=False, trusted_result=False, principal_missing=False,
                    storage_failure=False, consecutive_failures=0, span_seconds=0,
                    call_site="unknown", **refs) -> Observation:
        category = classify(error_code=error_code, classification=classification,
                            retryable=retryable, attempts=attempts, terminal=terminal,
                            trusted_result=trusted_result, principal_missing=principal_missing,
                            storage_failure=storage_failure,
                            consecutive_failures=consecutive_failures, span_seconds=span_seconds)
        return Observation(
            observation_id=str(uuid.uuid4()), observed_at=self.clock(),
            component=_text(component, 80), operation=_text(operation, 120),
            error_code=_text(error_code, 80), classification=category,
            retryable=bool(retryable), attempts=max(0, min(int(attempts), 999)),
            call_site=_stable_call_site(call_site), release=self.release,
            message=_text(message, 500), **{key: _text(refs.get(key), 120) or None
                for key in ("business_id", "agent_id", "session_id", "task_id",
                            "request_id", "job_id", "receipt_id")},
            recovered=bool(refs.get('recovered',False)))

    def external_observation(self, data: Mapping, *, agent_id: str, business_id: str | None,
                             observation_namespace: str | None = None) -> Observation:
        """Validate the small hook outbox contract and bind server identity."""
        allowed = {'observation_id', 'observed_at', 'component', 'operation',
                   'error_code', 'retryable', 'terminal', 'attempts',
                   'call_site', 'session_id', 'recovered'}
        if not isinstance(data, Mapping) or set(data) - allowed:
            raise ValueError('invalid technical observation fields')
        try:
            client_observation_id = str(uuid.UUID(data['observation_id']))
            observation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"qorqut/technical-observation/{observation_namespace}/{client_observation_id}")) \
                if observation_namespace else client_observation_id
            session_id = data['session_id']
            if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 200
                    or any(ord(char) < 33 or ord(char) == 127 for char in session_id)):
                raise ValueError
            observed_at = datetime.fromisoformat(data['observed_at'])
            if observed_at.tzinfo is None:
                raise ValueError
            component, operation, error_code = (data[key] for key in ('component', 'operation', 'error_code'))
            if any(not isinstance(value, str) or not value.strip() for value in (component, operation, error_code)):
                raise ValueError
            if (component not in _HOOK_COMPONENTS or operation not in _HOOK_OPERATIONS
                    or not _SAFE_SYMBOL.fullmatch(error_code)):
                raise ValueError
            retryable = data.get('retryable', False)
            terminal = data.get('terminal', False)
            recovered = data.get('recovered', False)
            attempts = data.get('attempts', 1)
            if type(retryable) is not bool or type(terminal) is not bool or type(recovered) is not bool:
                raise ValueError
            if type(attempts) is not int or not 0 <= attempts <= 999:
                raise ValueError
            call_site = data.get('call_site', component)
            if not isinstance(call_site, str) or call_site not in _CALL_SITES:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError('invalid technical observation') from None
        # Offline replay can be delayed, but arbitrary future/backdated events
        # would let a caller manipulate incident ordering and recovery.
        now = datetime.fromisoformat(utc_now())
        age = (now - observed_at.astimezone(now.tzinfo)).total_seconds()
        if age < -300 or age > 31 * 86400:
            raise ValueError('technical observation timestamp is outside its bound')
        observed_at = observed_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        return Observation(observation_id=observation_id, observed_at=observed_at,
            component=_text(component, 80), operation=_text(operation, 120),
            error_code=_text(error_code, 80), classification=classify(error_code=error_code,
                retryable=retryable, attempts=attempts, terminal=terminal), retryable=retryable,
            attempts=attempts, call_site=_stable_call_site(call_site),
            agent_id=agent_id, business_id=business_id, session_id=session_id,
            recovered=recovered)

    def observe_result(self, context, operation, result):
        """Model-free application result port; safe to call when reporting is unset."""
        if result is None or operation.startswith('system_incidents_'):
            return None
        error = getattr(result, "error", None)
        if not error:
            return self.observe_success(context,operation) if getattr(result,'status',None)=='ok' else None
        if not isinstance(error,Mapping):
            return None
        code, retryable = error.get('code','operation_failed'), error.get('retryable',False)
        observation = self.observation(component="application", operation=operation,
            error_code=code, retryable=retryable,
            call_site="application.invoke",
            business_id=getattr(context, "business_id", None),
            agent_id=context.principal_id if context.principal_kind=='agent' else None,
            request_id=getattr(context,'request_id',None),job_id=getattr(context,'job_id',None))
        if (error.get('details') or {}).get('incident_classification') == 'setup_contract':
            observation = replace(observation,classification='setup_contract')
        return self.observe(observation)

    def observe_success(self, context, operation):
        """Queue a scoped success probe; drain matches it to persisted impacts."""
        observation = self.observation(component="application", operation=operation,
            error_code="success", call_site="application.invoke",
            business_id=getattr(context, "business_id", None),
            agent_id=context.principal_id if context.principal_kind=='agent' else None, recovered=True)
        return self.observe(observation)

    def observe(self, observation: Observation, *, uow=None, reporter_id=None) -> dict:
        """Ingest immediately when a UOW is supplied, otherwise append to the spool."""
        # Error bodies can contain credentials, tool input or private text.
        # Persist typed codes/references only, even for future boundary callers.
        observation = replace(observation, message='')
        # Background jobs sharing a kind and business are separate recovery
        # identities. Legacy observations without a job_id retain NULL here.
        key = (observation.component,observation.operation,observation.call_site,
               observation.business_id,observation.agent_id,observation.session_id,
               observation.task_id,observation.job_id,observation.receipt_id)
        if not observation.recovered and observation.classification not in _REPORTABLE | {'transient_observation'}:
            return {'status':'ignored'}
        if observation.recovered:
            with self._lock:
                # An authenticated replay may arrive after a service restart,
                # before the in-memory success index has warmed.  The durable
                # impact query remains the authority for exact recovery.
                if uow is None and (key not in self._active or key in self._success_pending):
                    return {'status':'ignored'}
                if uow is None:
                    self._success_pending.add(key)
        else:
            with self._lock:
                if len(self._active) < 4096:
                    self._active.add(key)
                self._success_pending.discard(key)
        payload = observation.to_dict()
        encoded = canonical_bytes(payload)
        if len(encoded) > MAX_OBSERVATION_BYTES:
            payload["message"] = payload["message"][:128]
            encoded = canonical_bytes(payload)
        if uow is not None and reporter_id:
            return self._ingest(uow, Observation(**{k: payload[k] for k in Observation.__dataclass_fields__ if k in payload}), reporter_id)
        try:
            spooled = self._spool(encoded)
        except (OSError,ValueError,TypeError):
            spooled = False
        if not spooled:
            with self._lock:
                self._success_pending.discard(key)
            return {'status':'unavailable'}
        return {"status": "queued", "observation_id": observation.observation_id,
                "fingerprint": observation.fingerprint}

    def _spool(self, encoded: bytes):
        if not safe_path(self.spool_dir) or len(encoded)>MAX_OBSERVATION_BYTES:
            return False
        self.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try: self.spool_dir.chmod(0o700)
        except OSError: pass
        path = self.spool_dir / "system-incidents.jsonl"
        if not safe_path(path):
            return False
        line = encoded + b"\n"
        with self._lock, self._file_lock(path):
            try:
                current = path.stat().st_size if path.exists() else 0
                if current + len(line) > MAX_SPOOL_BYTES:
                    self.dropped_observations += 1
                    from bazarlyk.bootstrap.activity_hook import write_state
                    health = self.spool_dir / 'system-incidents.health'
                    prior = json.loads(health.read_text()) if safe_path(health) and health.is_file() and health.stat().st_size<1024 else {}
                    write_state(health,{'dropped_observations':prior.get('dropped_observations',0)+1},durable=True)
                    return False
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'ab') as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                return True
            except OSError:
                self.dropped_observations += 1
                return False

    def drain(self, uow, *, reporter_id, limit=MAX_DRAIN) -> dict:
        path = self.spool_dir / "system-incidents.jsonl"
        if not safe_path(path) or (path.is_file() and path.stat().st_size > MAX_SPOOL_BYTES):
            raise ValueError('Unsafe incident spool')
        if not path.exists():
            return {"processed": 0, "created": 0, "updated": 0, "recovered": 0, "remaining": 0}
        with self._lock, self._file_lock(path):
            lines = path.read_bytes().splitlines()
            count = min(MAX_DRAIN, max(1, int(limit)))
            take, rest = lines[:count], lines[count:]
        # Do not hold the spool lock while writing SQLite. Exact record receipts
        # make simultaneous snapshots and appends safe across processes.
        counters = self._drain_lines(uow, take, reporter_id)
        counters["remaining"] = len(rest)
        counters["_drained_ids"] = [hashlib.sha256(line).hexdigest() for line in take]
        return counters

    def acknowledge(self, observation_ids):
        """Remove only records successfully committed by a drain transaction."""
        ids = set(observation_ids)
        if not ids:
            return
        path = self.spool_dir / "system-incidents.jsonl"
        if not path.exists():
            return
        with self._lock, self._file_lock(path):
            lines = path.read_bytes().splitlines()
            rest = [line for line in lines if hashlib.sha256(line).hexdigest() not in ids]
            fd, temporary = tempfile.mkstemp(prefix='.incident-',dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as handle:
                    if rest: handle.write(b'\n'.join(rest)+b'\n')
                    handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary,path)
                directory = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)

    @contextmanager
    def _file_lock(self, path):
        lock = path.with_suffix(path.suffix + ".lock")
        if not safe_path(path) or not safe_path(lock):
            raise ValueError('Unsafe incident spool')
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _drain_lines(self, uow, take, reporter_id):
        counters = {"processed": 0, "created": 0, "updated": 0, "recovered": 0}
        for line in take:
            try:
                data = json.loads(line)
                obs = Observation(**{k: data[k] for k in Observation.__dataclass_fields__})
                uuid.UUID(obs.observation_id)
                datetime.fromisoformat(obs.observed_at)
                if len(line) > MAX_OBSERVATION_BYTES:
                    raise ValueError('Oversized observation')
            except (ValueError, TypeError, KeyError):
                # A partial final append must not poison all later records.
                self.dropped_observations += 1
                counters["processed"] += 1
                continue
            if not uow.connection.execute('INSERT OR IGNORE INTO system_incident_ingested'
                    '(observation_id,created_at) VALUES(?,?)',(obs.observation_id,utc_now())).rowcount:
                counters['processed'] += 1
                continue
            # Ingestion/programming failures roll back the transaction and keep
            # the original spool. They are not malformed-input acknowledgements.
            result = self._recover_success(uow, obs, reporter_id) if obs.recovered else self._ingest(uow, obs, reporter_id)
            counters["processed"] += 1
            counters["created"] += int(result.get("created", False))
            counters["updated"] += int(result.get("updated", False))
            counters["recovered"] += int(result.get("recovered", False))
        return counters

    def _recover_success(self, uow, obs, reporter_id):
        conn = uow.connection
        rows = conn.execute(
            "SELECT DISTINCT i.* FROM system_incidents i JOIN system_incident_impacts x ON x.incident_id=i.id "
            "JOIN system_incident_observations o ON o.id=i.representative_observation_id "
            "WHERE i.status<>'closed' AND x.operation=? AND x.agent_id IS ? AND x.business_id IS ? AND x.session_id IS ? "
            "AND x.task_id IS ? AND x.recovered_at IS NULL "
            "AND x.last_seen<=? AND json_extract(o.payload_json,'$.component')=? "
            "AND json_extract(o.payload_json,'$.call_site')=? AND x.job_id IS ? AND x.receipt_id IS ? LIMIT 100",
            (obs.operation,obs.agent_id,obs.business_id,obs.session_id,obs.task_id,obs.observed_at,obs.component,obs.call_site,obs.job_id,obs.receipt_id)).fetchall()
        recovered = 0
        for row in rows:
            incident_id = row['id']
            episode = conn.execute('SELECT coalesce(max(episode),0)+1 FROM system_incident_recoveries '
                'WHERE incident_id=? AND agent_id IS ?', (incident_id,obs.agent_id)).fetchone()[0]
            recovery_id = str(uuid.uuid5(uuid.UUID(obs.observation_id),incident_id))
            conn.execute("INSERT INTO system_incident_observations(id,fingerprint,observed_at,payload_json) VALUES(?,?,?,?)",
                (recovery_id,row['fingerprint'],obs.observed_at,canonical_bytes(obs.to_dict()).decode()))
            conn.execute("INSERT INTO system_incident_recoveries(incident_id,observation_id,observed_at,evidence,agent_id,episode) VALUES(?,?,?,?,?,?)",
                (incident_id,recovery_id,obs.observed_at,'A matching operation succeeded; affected work still needs verification.',obs.agent_id,episode))
            conn.execute("UPDATE system_incident_impacts SET recovered_at=? WHERE incident_id=? AND agent_id IS ? "
                'AND business_id IS ? AND session_id IS ? AND task_id IS ? AND job_id IS ? AND receipt_id IS ? AND recovered_at IS NULL AND last_seen<=?',
                (obs.observed_at,incident_id,obs.agent_id,obs.business_id,obs.session_id,obs.task_id,obs.job_id,obs.receipt_id,obs.observed_at))
            conn.execute("INSERT INTO system_incident_history(incident_id,event_kind,observed_at,observation_id,details) VALUES(?,?,?,?,?)",
                (incident_id,'recovered',obs.observed_at,recovery_id,'Matching scoped operation succeeded; ticket remains open for verification.'))
            pending = conn.execute('SELECT 1 FROM system_incident_impacts WHERE incident_id=? AND operation IS NOT NULL AND recovered_at IS NULL LIMIT 1',(incident_id,)).fetchone()
            conn.execute("UPDATE system_incidents SET recovery_count=recovery_count+1,status=? WHERE id=?",('open' if pending else 'recovering',incident_id))
            recipients = self.task_recipients(conn, incident_id, obs.business_id)
            if obs.agent_id:
                recipients.add(obs.agent_id)
            recipients = {identity for identity in recipients if self.recipient_current(conn,identity,obs.business_id)}
            if recipients:
                event_id = conn.execute("INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (obs.business_id,'system_incidents',incident_id,episode,reporter_id,'system_incident_recovered',
                     'A matching operation succeeded. Verify affected work before resuming or claiming completion.',obs.observed_at)).lastrowid
                conn.executemany('INSERT OR IGNORE INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)',
                    [(identity,event_id) for identity in sorted(recipients)])
            self._trim(conn, incident_id)
            recovered += 1
        conn.execute('DELETE FROM system_incident_fault_runs WHERE agent_key=? AND session_id=? AND business_id IS ? '
            'AND operation=? AND component=? AND call_site=? AND last_seen<=?',
            (obs.agent_id or '',obs.session_id or '',obs.business_id,obs.operation,obs.component,obs.call_site,obs.observed_at))
        return {"created": False, "updated": False, "recovered": recovered > 0}

    def _ingest(self, uow, obs: Observation, reporter_id: str) -> dict:
        conn = uow.connection
        if obs.classification == 'transient_observation':
            # SQLite NULLs are not equal in a composite primary key. Keep a
            # private empty sentinel for legacy/session-less transient runs.
            key = (obs.fingerprint,obs.agent_id or '',obs.session_id or '')
            if conn.execute('SELECT count(*) FROM system_incident_fault_runs').fetchone()[0] >= 4096 and not conn.execute(
                    'SELECT 1 FROM system_incident_fault_runs WHERE fingerprint=? AND agent_key=? AND session_id=?',key).fetchone():
                return {'skipped':True,'reason':'capacity'}
            attempts = max(1, obs.attempts)
            conn.execute('INSERT INTO system_incident_fault_runs(fingerprint,agent_key,session_id,business_id,operation,component,call_site,first_seen,last_seen,failures) '
                'VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint,agent_key,session_id) DO UPDATE SET '
                'last_seen=max(last_seen,excluded.last_seen),failures=failures+excluded.failures',
                (*key,obs.business_id,obs.operation,obs.component,obs.call_site,obs.observed_at,obs.observed_at,attempts))
            run = conn.execute('SELECT * FROM system_incident_fault_runs WHERE fingerprint=? AND agent_key=? AND session_id=?',key).fetchone()
            span = (datetime.fromisoformat(run['last_seen'])-datetime.fromisoformat(run['first_seen'])).total_seconds()
            if run['failures'] >= 3 and span >= 600:
                obs = replace(obs,classification='infrastructure_fault')
        if obs.classification not in _REPORTABLE:
            # Normal workflow and transient observations are intentionally
            # retained only as bounded telemetry by callers; they never open
            # a System Dev ticket.
            return {"created": False, "updated": False, "recovered": False,
                    "skipped": True, "reason": obs.classification}
        delivery = None
        if obs.classification == DELIVERY_FAILURE:
            prepared = self._prepare_delivery(conn, obs)
            if isinstance(prepared, dict):
                return prepared
            obs, delivery = prepared
        existing = conn.execute("SELECT id,status,occurrence_count,recovery_count,ticket_id,last_seen FROM system_incidents WHERE fingerprint=?", (obs.fingerprint,)).fetchone()
        now = obs.observed_at
        reopened = False
        if existing is None:
            incident_id, ticket_id = str(uuid.uuid4()), str(uuid.uuid4())
            title = f"System incident: {obs.component}/{obs.operation} ({obs.error_code})"[:200]
            details, kind = f'{obs.error_code} at {obs.call_site}. Automatically observed; inspect the incident evidence.', 'bug'
            if delivery is not None:
                title, details, kind = delivery['title'], delivery['details'], 'tooling'
            conn.execute("INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (ticket_id,title,details,kind,'open',reporter_id,obs.business_id,now,now))
            conn.execute("INSERT INTO system_incidents(id,fingerprint,ticket_id,title,status,classification,first_seen,last_seen,occurrence_count,recovery_count,business_id,representative_observation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (incident_id, obs.fingerprint, ticket_id, title, "open", obs.classification, now, now, 1, 0, obs.business_id, obs.observation_id))
            event = "opened"
            created, updated = True, False
        else:
            incident_id = existing[0]
            inserted = conn.execute("INSERT OR IGNORE INTO system_incident_observations(id,fingerprint,observed_at,payload_json) VALUES(?,?,?,?)",
                                   (obs.observation_id, obs.fingerprint, now, canonical_bytes(obs.to_dict()).decode())).rowcount
            if not inserted:
                return {"incident_id": incident_id, "ticket_id": existing[4],
                        "created": False, "updated": False, "recovered": False, "duplicate": True}
            conn.execute("UPDATE system_incidents SET last_seen=max(last_seen,?),occurrence_count=occurrence_count+1 WHERE id=?", (now, incident_id))
            recovered_at = conn.execute('SELECT max(observed_at) FROM system_incident_recoveries WHERE incident_id=?', (incident_id,)).fetchone()[0]
            ticket_closed_at = conn.execute('SELECT resolved_at FROM dev_tickets WHERE id=?',(existing['ticket_id'],)).fetchone()[0]
            if existing[1] in {'recovering','closed'} and now > max(existing['last_seen'], recovered_at or '', ticket_closed_at or ''):
                reopened = existing['status'] == 'closed'
                conn.execute("UPDATE system_incidents SET status='open' WHERE id=?", (incident_id,))
                conn.execute("UPDATE dev_tickets SET status='open',resolved_at=NULL,resolved_by=NULL,resolution='',updated_at=?,revision=revision+1 WHERE id=?", (now, existing[4]))
                from bazarlyk.domains.dev_tickets import DevTickets
                DevTickets.insert_event(uow, existing['ticket_id'], 'status_changed', reporter_id,
                    details='A later technical failure reopened this incident; prior resolution evidence is retained.', created_at=now)
            event, created, updated = "observed", False, True
        if existing is None:
            conn.execute("INSERT OR IGNORE INTO system_incident_observations(id,fingerprint,observed_at,payload_json) VALUES(?,?,?,?)",
                         (obs.observation_id, obs.fingerprint, now, canonical_bytes(obs.to_dict()).decode()))
        conn.execute("INSERT OR IGNORE INTO system_incident_impacts(incident_id,business_id,agent_id,session_id,task_id,operation,job_id,receipt_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (incident_id, obs.business_id, obs.agent_id, obs.session_id, obs.task_id, obs.operation, obs.job_id, obs.receipt_id, now, now))
        conn.execute("UPDATE system_incident_impacts SET last_seen=max(last_seen,?),episode=episode+(recovered_at IS NOT NULL),recovered_at=NULL "
                     "WHERE incident_id=? AND business_id IS ? AND agent_id IS ? AND session_id IS ? AND task_id IS ? AND operation IS ? AND job_id IS ? AND receipt_id IS ? AND (recovered_at IS NULL OR recovered_at<?)",
                     (now, incident_id, obs.business_id, obs.agent_id, obs.session_id, obs.task_id, obs.operation, obs.job_id, obs.receipt_id, now))
        conn.execute("INSERT INTO system_incident_history(incident_id,event_kind,observed_at,observation_id,details) VALUES(?,?,?,?,?)",
                     (incident_id, event, now, obs.observation_id, delivery['note'] if delivery is not None else obs.message))
        notified = False
        if delivery is not None:
            from bazarlyk.domains.dev_tickets import DevTickets
            DevTickets.insert_event(uow, existing[4] if existing else ticket_id, 'follow_up', reporter_id,
                                    details=delivery['note'], created_at=now)
            notified = self._notify_initiator(conn, incident_id, delivery, reporter_id, now)
        elif created or reopened:
            coordinator = conn.execute("SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()
            coordinator = coordinator[0] if coordinator else None
            if coordinator and coordinator != obs.agent_id and self.recipient_current(conn,coordinator,obs.business_id):
                event_id = conn.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) '
                    'VALUES(?,?,?,?,?,?,?)',(obs.business_id,'system_incidents',incident_id,reporter_id,
                    'system_incident_opened','A technical fault was automatically filed in System Dev. Inspect affected work and arrange repair.',now)).lastrowid
                conn.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)',(coordinator,event_id))
        if not existing or now >= existing['last_seen']:
            conn.execute('UPDATE system_incidents SET representative_observation_id=? WHERE id=?',(obs.observation_id,incident_id))
        self._trim(conn, incident_id)
        return {"incident_id": incident_id, "ticket_id": existing[4] if existing else ticket_id,
                "created": created, "updated": updated, "recovered": obs.recovered, **({"notified": notified} if delivery is not None else {})}

    def _prepare_delivery(self, conn, obs):
        """Rebind a delivery-failure observation to its job row (the authority) or say why it is not reportable.

        Returns the rebound observation and the safe text to file, or a result dict for a job that is missing, not a
        terminally failed bound send, or already has its one retained impact (a replay, restart or reconciliation)."""
        from bazarlyk.domains import account_email_outcome as outcome
        from qorqut_operations.storage.repositories import decode_row
        found = conn.execute('SELECT * FROM jobs WHERE id=? AND business_id IS NULL AND kind=?',
                             (obs.job_id, outcome.JOB_KIND)).fetchone()
        job = decode_row(found) if found is not None else None
        if job is None or job['state'] != 'failed' or outcome.bound_send(job) is None or outcome.stage(job) not in outcome.REPORTABLE:
            return {'created': False, 'updated': False, 'recovered': False, 'skipped': True, 'reason': 'not_a_failed_account_send'}
        if conn.execute('SELECT 1 FROM system_incident_impacts x JOIN system_incidents i ON i.id=x.incident_id '
                        'WHERE i.classification=? AND x.job_id=? LIMIT 1', (DELIVERY_FAILURE, job['id'])).fetchone():
            return {'created': False, 'updated': False, 'recovered': False, 'duplicate': True}
        unconfirmed = outcome.stage(job) == 'unconfirmed'
        signoff = outcome.signoff_for_job(conn, job)
        reference = f"job {job['id']}" + (f" for Signoff {signoff}" if signoff else '')
        obs = replace(obs, component='background', operation=outcome.JOB_KIND, call_site='background.account_email_send',
                      error_code='external_unconfirmed' if unconfirmed else 'external_failed', business_id=None,
                      agent_id=job['initiated_by_principal_id'], session_id=None, task_id=None, request_id=None,
                      job_id=job['id'], receipt_id=None, attempts=job['attempts'], message='')
        if unconfirmed:
            title = 'Email send unconfirmed'
            summary = (f'Account email send unconfirmed: the approved send may or may not have gone out ({reference}). '
                       'Check the mailbox before any replacement send, and tell the owner.')
            body = ('An approved private-account email send could not be confirmed after recovery, so it may or may not have '
                    'been submitted to the mail provider. Nothing was sent again. Check the mailbox before any replacement '
                    'send. Affected jobs and Signoffs are listed in the incident history; no message content or provider '
                    'text is recorded here.')
        else:
            title = 'Email send failed'
            summary = (f'Account email send failed: the approved send was not sent ({reference}). Nothing was sent again. '
                       'Tell the owner and ask before preparing a replacement send.')
            body = ('An approved private-account email send ended without being submitted: the mail provider refused it or it '
                    'stopped before dispatch. Nothing was sent again. A replacement send needs a new owner decision. Affected '
                    'jobs and Signoffs are listed in the incident history; no message content or provider text is recorded here.')
        return obs, {'title': title, 'details': body, 'note': reference[0].upper() + reference[1:] + (': unconfirmed.' if unconfirmed else ': not sent.'),
                     'summary': summary, 'event_type': 'system_incident_email_send_' + ('unconfirmed' if unconfirmed else 'failed'),
                     'initiator': job['initiated_by_principal_id']}

    def _notify_initiator(self, conn, incident_id, delivery, reporter_id, now):
        """One inbox notice for the initiating agent, in the ingestion transaction, if it still holds account authority.

        The initiator being the coordinator never suppresses it; a revoked or inactive initiator gets none (the owner's
        incident record exists regardless) and, read later, no private access either: the event is shown only to a
        currently authorized reader of this incident."""
        if not self.account_recipient_current(conn, delivery['initiator']):
            return False
        event_id = conn.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) '
            'VALUES(?,?,?,?,?,?,?)', (None, 'system_incidents', incident_id, reporter_id, delivery['event_type'],
                                      delivery['summary'], now)).lastrowid
        conn.execute('INSERT OR IGNORE INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (delivery['initiator'], event_id))
        return True

    @staticmethod
    def account_recipient_current(conn, agent_id):
        """The agent is an active supported global that still holds the account grant as the configured Ordubashy or a worker."""
        from bazarlyk.agents.worker_access import GLOBAL_WORKER_ROLE
        profile = active_global(conn, agent_id)
        if profile is None:
            return False
        held = set(profile['scope'].get('global_capabilities', []))
        if not {'manage_fastmail', 'read_global_context'} <= held:
            return False
        coordinator = conn.execute("SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()
        role = profile['role']['id']
        return role == GLOBAL_WORKER_ROLE or (role == 'ordubashy' and bool(coordinator) and coordinator[0] == agent_id)

    @staticmethod
    def recipient_current(conn, agent_id, business_id):
        # Only an active supported global receives current incident work: a business incident needs its derived reach to
        # that active business (no membership row), a Global one its read_global_context grant.
        profile = active_global(conn, agent_id)
        if profile is None:
            return False
        if business_id:
            return reaches_business(conn, agent_id, business_id)
        return 'read_global_context' in profile['scope'].get('global_capabilities', [])

    @staticmethod
    def task_recipients(conn, incident_id, business_id):
        return {row[0] for row in conn.execute(
            "SELECT DISTINCT r.to_id FROM system_incident_impacts x JOIN tasks t ON t.business_id IS x.business_id AND t.id=x.task_id "
            "JOIN relationships r ON r.business_id IS t.business_id AND r.from_type='task' AND r.from_id=t.id "
            "WHERE x.incident_id=? AND x.business_id IS ? AND x.operation IS NULL AND t.archived=0 "
            "AND r.role='assigned_to' AND r.to_type='agent'", (incident_id,business_id))}

    @staticmethod
    def link_task(uow, ticket_id, business_id, task_id, actor_id, at):
        """An authorized authored task note is the explicit association/evidence."""
        incident = uow.connection.execute('SELECT id,business_id FROM system_incidents WHERE ticket_id=?', (ticket_id,)).fetchone()
        if not incident or incident['business_id'] != business_id:
            return  # Cross-business task associations never follow incidental agent scope.
        uow.connection.execute('INSERT OR IGNORE INTO system_incident_impacts'
            '(incident_id,business_id,agent_id,session_id,task_id,operation,job_id,receipt_id,first_seen,last_seen) VALUES(?,?,NULL,NULL,?,NULL,NULL,NULL,?,?)',
            (incident['id'],business_id,task_id,at,at))

    @classmethod
    def ticket_status(cls, uow, ticket, actor_id, *, verified=False):
        """The existing authorized ticket lifecycle is the only closure authority."""
        conn = uow.connection
        incident = conn.execute('SELECT id,status FROM system_incidents WHERE ticket_id=?',(ticket['id'],)).fetchone()
        if not incident:
            return
        closed = ticket['status'] == 'closed'
        conn.execute('UPDATE system_incidents SET status=? WHERE id=?', ('closed' if closed else 'open',incident['id']))
        if not closed or not verified or incident['status'] == 'closed':
            return
        conn.execute('UPDATE system_incident_impacts SET recovered_at=coalesce(recovered_at,?) WHERE incident_id=?',
            (ticket['updated_at'],incident['id']))
        recipients = {row[0] for row in conn.execute('SELECT DISTINCT agent_id FROM system_incident_impacts '
            'WHERE incident_id=? AND agent_id IS NOT NULL',(incident['id'],))}
        recipients.update(cls.task_recipients(conn,incident['id'],ticket['business_id']))
        recipients = {identity for identity in recipients if cls.recipient_current(conn,identity,ticket['business_id'])}
        if recipients:
            event = conn.execute('INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,created_at) '
                'VALUES(?,?,?,?,?,?,?,?)',(ticket['business_id'],'system_incidents',incident['id'],ticket['revision'],actor_id,
                'system_incident_resolved','Repair evidence recorded. Inspect the resolution and your task prerequisites before resuming.',ticket['updated_at'])).lastrowid
            conn.executemany('INSERT OR IGNORE INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)',[(identity,event) for identity in sorted(recipients)])

    @staticmethod
    def _trim(conn, incident_id):
        # Aggregates retain counts; diagnostics are representative samples.
        conn.execute('DELETE FROM system_incident_history WHERE incident_id=? AND id NOT IN '
            '(SELECT id FROM system_incident_history WHERE incident_id=? ORDER BY id DESC LIMIT 100)',(incident_id,incident_id))
        # Keep the newest recovery per agent (its episode is the dedupe watermark)
        # plus the last 100 samples, without dropping live recovery watermarks.
        conn.execute('DELETE FROM system_incident_recoveries WHERE incident_id=? AND observation_id NOT IN '
            '(SELECT observation_id FROM system_incident_recoveries WHERE incident_id=? ORDER BY observed_at DESC LIMIT 100) '
            'AND episode < (SELECT max(r.episode) FROM system_incident_recoveries r '
            'WHERE r.incident_id=system_incident_recoveries.incident_id AND r.agent_id IS system_incident_recoveries.agent_id)', (incident_id,incident_id))
        conn.execute('DELETE FROM system_incident_observations WHERE fingerprint=(SELECT fingerprint FROM system_incidents WHERE id=?) '
            'AND id NOT IN (SELECT representative_observation_id FROM system_incidents WHERE id=?) '
            'AND id NOT IN (SELECT observation_id FROM system_incident_recoveries WHERE incident_id=?) '
            'AND id NOT IN (SELECT observation_id FROM system_incident_history WHERE incident_id=?)', (incident_id,)*4)

    def readiness(self, uow, *, reporter_id: str) -> dict:
        row = uow.connection.execute("SELECT id,kind,active FROM principals WHERE id=?", (reporter_id,)).fetchone()
        return {"ready": bool(row and row[1] == "system" and row[2]), "reporter_id": reporter_id}


class IncidentOperations:
    """Read-only owner and agent projections for persisted incident DTOs."""
    def __init__(self, application=None):
        self.app=application

    def register(self, registry):
        from qorqut_operations.tasks.scope import task_namespace
        registry.register(OperationDescriptor("system_incidents_list", schema({
            "status": {"enum": ["active", "open", "recovering", "closed", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "offset": {"type": "integer", "minimum": 0}},),
            scope_kinds=frozenset({"global"}), owner_only=True, supports_signoff=False,
            global_capability="read_global_context", exposure=frozenset({"dashboard"}),
            effect_kind="read", read=self.owner_list,
            description="Owner: list bounded System Dev incident summaries."))
        registry.register(OperationDescriptor("system_incidents_get", schema({
            "incident_id": {"type": "string", "format": "uuid"}}, ("incident_id",)),
            scope_kinds=frozenset({"global"}), owner_only=True, supports_signoff=False,
            global_capability="read_global_context", exposure=frozenset({"dashboard"}),
            effect_kind="read", read=self.owner_get,
            description="Owner: inspect one System Dev incident and bounded evidence."))
        common = dict(scope_kinds=frozenset({"business", "global"}),
                      supports_signoff=False,
                      global_capability="read_global_context", exposure=frozenset({"mcp"}),
                      effect_kind="read",resolve_namespace=task_namespace)
        registry.register(OperationDescriptor("system_incidents_agent_list", schema({
            'scope':{'enum':['business','global']},
            "status": {"enum": ["active", "open", "recovering", "closed", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0}}),
            **common, read=self.agent_list,
            description="Read System Dev incidents impacting your authorized scope."))
        registry.register(OperationDescriptor("system_incidents_agent_get", schema({
            'scope':{'enum':['business','global']},
            "incident_id": {"type": "string", "format": "uuid"}}, ("incident_id",)),
            **common, read=self.agent_get,
            description="Read one impacted System Dev incident in your authorized scope."))

    @staticmethod
    def _owner(ctx, uow):
        row = uow.repository("installation").get("service")
        if ctx.principal_kind != "human" or not row or row["owner_principal_id"] != ctx.principal_id:
            from qorqut_operations.contracts import DomainError
            raise DomainError("forbidden", "Only the installation owner can review incidents")

    def _dto(self, uow, row, *, details=True, ctx=None):
        incident = dict(row)
        ticket = uow.connection.execute('SELECT revision,status,resolution FROM dev_tickets WHERE id=?',(row['ticket_id'],)).fetchone()
        incident.update(revision=ticket['revision'],ticket_status=ticket['status'],resolution=ticket['resolution'])
        sample=uow.connection.execute('SELECT payload_json FROM system_incident_observations WHERE id=?',
                                      (row['representative_observation_id'],)).fetchone()
        sample=json.loads(sample[0]) if sample else {}
        incident['representative_error']=str(sample.get('error_code',''))
        query = "SELECT incident_id,business_id,agent_id,session_id,task_id,operation,job_id,receipt_id,first_seen,last_seen,episode,recovered_at FROM system_incident_impacts x WHERE incident_id=?"
        params = [row["id"]]
        if ctx is not None and ctx.principal_kind == "agent":
            clause,scope_params=IncidentOperations.impact_scope(ctx,uow)
            query += ' AND ('+clause+')'; params.extend(scope_params)
        impacts = [dict(x) for x in uow.connection.execute(query + " ORDER BY first_seen LIMIT 101", params)]
        if ctx is not None and ctx.principal_kind == 'agent':
            from qorqut_operations.contracts import DomainError
            for index, impact in enumerate(impacts):
                if not impact['task_id']:
                    continue
                selected = replace(ctx, business_id=impact['business_id'], review_business_id=impact['business_id'])
                try:
                    self.app.authorize(selected, self.app.registry.get('tasks_get'), (), uow,
                        namespace='business:'+selected.business_id if selected.business_id else 'global')
                    uow.repository('tasks', business_id=selected.business_id).get(impact['task_id'], required=True)
                except DomainError as error:
                    if error.code not in {'forbidden', 'policy_denied', 'not_found', 'operation_unavailable'}:
                        raise
                    impacts[index] = {'unavailable': True}
        result = {"incident": incident, "impacts": impacts[:100],'impacts_truncated':len(impacts)>100}
        if details:
            # Resolution/verification belong to the existing ticket event, not a
            # competing incident lifecycle record.
            resolved = uow.connection.execute("SELECT resolution,verification,deployment_ref,actor_id,created_at FROM dev_ticket_events "
                "WHERE ticket_id=? AND event_kind='resolved' ORDER BY created_at DESC,id DESC LIMIT 1",(row['ticket_id'],)).fetchone()
            result['resolution'] = dict(resolved) if resolved else None
            recovery_sql = "SELECT incident_id,observed_at,evidence,observation_id FROM system_incident_recoveries WHERE incident_id=?"
            recovery_params = [row["id"]]
            if (ctx is not None and ctx.principal_kind == "agent" and row['business_id'] is None
                    and getattr(getattr(ctx, 'role_artifact_ref', None), 'id', None) != 'ordubashy'):
                recovery_sql += " AND agent_id=?"; recovery_params.append(ctx.principal_id)
            result["recoveries"] = [dict(x) for x in uow.connection.execute(recovery_sql + " ORDER BY observed_at DESC LIMIT 100", recovery_params)]
            # Sanitized history contains no foreign business payloads. Global
            # agents other than Ordubashy get only their own observations.
            history='SELECT h.incident_id,h.event_kind,h.observed_at,h.observation_id,h.details FROM system_incident_history h '
            history+='LEFT JOIN system_incident_observations o ON o.id=h.observation_id WHERE h.incident_id=?'
            params=[row['id']]
            if ctx is not None and ctx.principal_kind=='agent' and row['business_id'] is None and getattr(getattr(ctx,'role_artifact_ref',None),'id',None)!='ordubashy':
                history+=" AND json_extract(o.payload_json,'$.agent_id')=?"; params.append(ctx.principal_id)
            result['history']=[dict(x) for x in uow.connection.execute(history+' ORDER BY h.id DESC LIMIT 100',params)]
        return result

    def owner_list(self, ctx, args, opts, uow):
        self._owner(ctx, uow)
        status, limit, offset = args.get("status", "open"), args.get("limit", 50), args.get("offset", 0)
        where, params = ("", []) if status == "all" else (" WHERE status=?", [status])
        if status == 'active':
            where, params = " WHERE status IN ('open','recovering')", []
        rows = [dict(x) for x in uow.connection.execute("SELECT *,(SELECT revision FROM dev_tickets t WHERE t.id=system_incidents.ticket_id) AS revision,"
            "(SELECT status FROM dev_tickets t WHERE t.id=system_incidents.ticket_id) AS ticket_status FROM system_incidents" + where + " ORDER BY first_seen,id LIMIT ? OFFSET ?", [*params, limit + 1, offset])]
        return {"items": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None,
                'reporting_health': self.health()}

    def health(self):
        reporter = getattr(self.app, 'incident_reporter', None)
        return reporter.health() if reporter else {'state': 'unavailable'}

    def owner_get(self, ctx, args, opts, uow):
        self._owner(ctx, uow)
        row = uow.connection.execute("SELECT * FROM system_incidents WHERE id=?", (args["incident_id"],)).fetchone()
        if row is None:
            from qorqut_operations.contracts import DomainError
            raise DomainError("not_found", "Incident was not found")
        return self._dto(uow, row, ctx=ctx)

    @staticmethod
    def impact_scope(ctx, uow):
        if ctx.principal_kind != "agent":
            from qorqut_operations.contracts import DomainError
            raise DomainError("forbidden", "Incident reads require an authenticated agent")
        # Business reach is the active global's derived set, never a membership; any other identity reads nothing.
        current=active_global(uow.connection,ctx.principal_id) is not None
        scopes=sorted(b for b in effective_business_ids(uow.connection,ctx.principal_id)
            if b in ctx.allowed_business_ids and (ctx.business_id is None or b==ctx.business_id))
        clause='x.business_id IN ('+','.join('?' for _ in scopes)+')' if scopes else '0'
        params=list(scopes)
        if current and ctx.business_id is None and 'read_global_context' in ctx.global_capabilities:
            if getattr(getattr(ctx,'role_artifact_ref',None),'id',None)=='ordubashy':
                clause+=' OR x.business_id IS NULL'
            else:
                clause+=' OR (x.business_id IS NULL AND x.agent_id=?)'; params.append(ctx.principal_id)
        return '('+clause+')',params

    @classmethod
    def visible(cls,ctx,identity,uow):
        clause,params=cls.impact_scope(ctx,uow)
        return bool(uow.connection.execute('SELECT 1 FROM system_incident_impacts x WHERE x.incident_id=? AND '+clause+' LIMIT 1',
            [identity,*params]).fetchone())

    def agent_list(self, ctx, args, opts, uow):
        clause,params = self.impact_scope(ctx,uow)
        status, limit, offset = args.get("status", "open"), args.get("limit", 50), args.get("offset", 0)
        clauses = ['EXISTS (SELECT 1 FROM system_incident_impacts x WHERE x.incident_id=system_incidents.id AND '+clause+')']
        if status == 'active':
            clauses.append("status IN ('open','recovering')")
        elif status != "all":
            clauses.append("status=?"); params.append(status)
        rows = [dict(x) for x in uow.connection.execute("SELECT *,(SELECT revision FROM dev_tickets t WHERE t.id=system_incidents.ticket_id) AS revision,"
            "(SELECT status FROM dev_tickets t WHERE t.id=system_incidents.ticket_id) AS ticket_status FROM system_incidents WHERE " + " AND ".join(clauses) + " ORDER BY first_seen,id LIMIT ? OFFSET ?", [*params, limit + 1, offset])]
        return {"items": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None,
                'reporting_health': self.health()}

    def agent_get(self, ctx, args, opts, uow):
        if not self.visible(ctx,args['incident_id'],uow):
            from qorqut_operations.contracts import DomainError
            raise DomainError("forbidden", "Incident is outside your authorized scope")
        row = uow.connection.execute("SELECT * FROM system_incidents WHERE id=?", (args["incident_id"],)).fetchone()
        if row is None:
            from qorqut_operations.contracts import DomainError
            raise DomainError("not_found", "Incident was not found")
        return self._dto(uow, row, ctx=ctx)


def technical_session_binding(connection, session_id, principal_id, registration_id) -> str:
    """Classify a native session for one authenticated technical report.

    A native UUID alone is not an identity. The stored identities are composite:
    ``session_registrations`` is unique by (registration, session) and
    ``agent_activity`` by (agent, session). A report is ``owned`` when either
    table binds this session to the caller's principal AND exact registration
    together; a foreign row or a matching UUID alone grants nothing. With no row
    anywhere the session is ``unregistered`` (a first bootstrap can fail before
    registration succeeds). Otherwise it is ``foreign``.
    """
    exact = connection.execute(
        "SELECT 1 FROM session_registrations WHERE session_id=? AND principal_id=? AND registration_id=? "
        "UNION ALL SELECT 1 FROM agent_activity WHERE session_id=? AND agent_id=? AND registration_id=? LIMIT 1",
        (session_id, principal_id, registration_id, session_id, principal_id, registration_id)).fetchone()
    if exact:
        return "owned"
    known = connection.execute(
        "SELECT 1 FROM session_registrations WHERE session_id=? "
        "UNION ALL SELECT 1 FROM agent_activity WHERE session_id=? LIMIT 1",
        (session_id, session_id)).fetchone()
    return "foreign" if known else "unregistered"


def ensure_system_reporter(uow, authority, *, program_id=INCIDENT_PROGRAM_ID,
                           business_ids=()):
    """Provision the reporter using the same deterministic worker mechanism as jobs."""
    identity = authority.worker_id(program_id, uow)
    actor = uow.repository("principals").get(identity)
    if actor is None:
        uow.repository("principals").insert({"id": identity, "kind": "system",
                                               "display_name": "System Dev incident reporter"})
    elif actor["kind"] != "system":
        raise ValueError("Incident reporter principal identity is occupied")
    for business_id in business_ids:
        membership = uow.repository("business_memberships", business_id=business_id)
        if membership.get(identity) is None:
            membership.insert({"principal_id": identity})
    return identity


def drain_spool(reporter: IncidentReporter, database, *, reporter_id: str,
                limit=MAX_DRAIN):
    """Flush one bounded batch behind the normal storage write-admission barrier."""
    with database.transaction(operation="system_incident_drain") as uow:
        from qorqut_operations.application.engine import Application
        from qorqut_operations.background.authority import BackgroundAuthority
        Application.ensure_writes_open(uow)
        BackgroundAuthority.require_worker(reporter_id,None,uow)
        result = reporter.drain(uow, reporter_id=reporter_id, limit=limit)
        uow.connection.execute("DELETE FROM system_incident_ingested WHERE created_at < datetime('now','-7 days')")
    reporter.acknowledge(result.pop('_drained_ids',[]))
    reporter.load_active(database)
    return result


class IncidentDrain:
    """Small optional lifecycle worker; it never blocks application requests."""
    def __init__(self, reporter, database, *, reporter_id=None, authority=None, writer=None, interval=60, clock=time.monotonic):
        self.reporter, self.database, self.reporter_id = reporter, database, reporter_id
        self.authority,self.writer = authority,writer
        self.interval, self.clock = max(60, interval), clock
        self._stop, self._thread = Event(), None

    def drain_once(self):
        result = self._drain_spool()
        reconciled = self._reconcile()
        return {**result, 'reconciled': reconciled} if reconciled else result

    def _reporter_identity(self):
        """The registered System Dev reporter, provisioned on first use (call inside the writer admission)."""
        if self.reporter_id is None:
            from qorqut_operations.application.engine import Application
            with self.database.transaction(operation='system_incident_reporter_setup') as uow:
                Application.ensure_writes_open(uow)
                identity = ensure_system_reporter(uow,self.authority)
            self.reporter_id = identity
        return self.reporter_id

    def _drain_spool(self):
        try:
            from contextlib import nullcontext
            path = self.reporter.spool_dir / 'system-incidents.jsonl'
            if self.reporter_id is not None and safe_path(path) and (not path.exists() or path.stat().st_size == 0):
                # Healthy idle reporting needs no periodic SQLite writer slot.
                return {'processed': 0, 'created': 0, 'updated': 0, 'recovered': 0, 'remaining': 0}
            with self.writer() if self.writer else nullcontext():
                result = drain_spool(self.reporter,self.database,reporter_id=self._reporter_identity())
                self.reporter.last_drain = {'state': 'ready', 'at': utc_now()}
                return result
        except Exception:
            # The spool remains intact; reporting its own failure would recurse.
            self.reporter.last_drain = {'state': 'unavailable', 'at': utc_now()}
            return {"processed": 0, "created": 0, "updated": 0, "recovered": 0, "remaining": None, "degraded": True}

    def _reconcile(self):
        """Report terminal account-mail send failures whose observation never reached an incident (a lost spool line, a
        failed drain, a restart, a failure that predates reporting). The job row is authoritative: this only reads
        terminal jobs, files through the same idempotent ingestion as the spool, never resends or touches the job, and
        does nothing while writes are closed. One bounded batch per drain interval; an idle system takes no writer slot."""
        try:
            from contextlib import nullcontext
            from qorqut_operations.application.engine import Application
            from qorqut_operations.background.authority import BackgroundAuthority
            with self.database.read() as uow:
                pending = self.reporter.unreported_account_sends(uow)
            reported = 0
            for job_id in pending:
                try:
                    with self.writer() if self.writer else nullcontext():
                        reporter_id = self._reporter_identity()
                        with self.database.transaction(operation='system_incident_reconcile') as uow:
                            Application.ensure_writes_open(uow)
                            BackgroundAuthority.require_worker(reporter_id,None,uow)
                            result = self.reporter.report_account_send(uow, job_id, reporter_id=reporter_id)
                    reported += int(bool(result.get('created') or result.get('updated')))
                except Exception as error:
                    if getattr(error, 'code', None) == 'maintenance':
                        break   # Writes are closed; the same jobs are found again once they reopen.
                    continue    # This job stays unreported and is retried on the next pass; the others proceed.
            self.reporter.last_reconcile = {'state': 'ready', 'at': utc_now()}
            return reported
        except Exception:
            self.reporter.last_reconcile = {'state': 'unavailable', 'at': utc_now()}
            return 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        try:
            self.reporter.load_active(self.database)
        except Exception:
            pass  # Database health is reflected in the drain's observable result.
        self._thread = Thread(target=self._run, name="system-incident-drain", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.drain_once()
            self._stop.wait(self.interval)

    def stop(self, timeout=5):
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=max(0, timeout))
        if thread.is_alive():
            raise TimeoutError("System incident drain is still running")
        self._thread = None
