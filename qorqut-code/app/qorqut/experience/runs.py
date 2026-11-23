"""The queue of the Learning Applier's workflow runs: what makes a run due, and what starts it.

Two workflows run on a schedule in the owner's time zone (``zone_name``): the experience extraction every day at 03:00 and
the review and application on Sunday at 06:00. Either time is changed, or the workflow switched off, with
``QORQUT_EXPERIENCE_EXTRACTION_AT`` and ``QORQUT_EXPERIENCE_REVIEW_AT`` (a five-field cron expression, or ``off``).

``Runs.due`` (the host's inbox reconcile pass, inside its write unit) queues a run when a scheduled time has passed. The
first run of a kind is queued only at its scheduled time, never for a time that passed before this store had the tables;
later, a time missed while the service was down is caught up. A run that is still waiting takes the newer time instead
of a second run being queued behind it.

``Runs.step`` (the native Chat backend's pass) starts the oldest waiting run when no run is running and the agent is not
in the middle of a turn. Every run starts in a fresh conversation: the agent's idle conversation is closed and a new one
is opened whose first message names the run. One run at a time: a run that is due while another is running waits until
that one is finished. A run ends when the agent calls ``experience_run_finish``. One whose conversation ended without
that is started once more in another fresh conversation, then given up as ``abandoned``; an abandoned extraction loses
nothing, because the next extraction reads from the end of the last finished one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import threading
import time
from zoneinfo import ZoneInfo

from qorqut_operations.background.schedule import RoutineError, parse_cron, prev_slot
from qorqut_operations.contracts import DomainError, new_id

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.managed_runtime import mode
from qorqut.native_chat import store

from .schema import RUNS

LOG = logging.getLogger(__name__)

EXTRACTION, REVIEW = 'extraction', 'review'
ENVIRONMENT = {EXTRACTION: 'QORQUT_EXPERIENCE_EXTRACTION_AT', REVIEW: 'QORQUT_EXPERIENCE_REVIEW_AT'}
DEFAULTS = {EXTRACTION: '0 3 * * *', REVIEW: '0 6 * * 0'}
TITLES = {EXTRACTION: 'experience extraction', REVIEW: 'review and application'}
ZONE_ENVIRONMENT = ('QORQUT_EXPERIENCE_TIMEZONE', 'QORQUT_TIMEZONE', 'TZ')
FIRST_WINDOW = {EXTRACTION: timedelta(days=1), REVIEW: timedelta(days=7)}
LONGEST_WINDOW = timedelta(days=7)
FIRST_RUN_GRACE = timedelta(hours=1)      # the first run of a kind is queued only this soon after its scheduled time
DUE_SECONDS = 60.0                        # how often the reconcile pass looks at the schedules
STEP_SECONDS = 20.0                       # how often the backend pass looks at the queue
RETRY_SECONDS = 120.0                     # after a start that failed
SEEN_SECONDS = 60.0                       # how often a running run's conversation is recorded as seen
STALL = timedelta(minutes=20)             # a running run whose conversation has been gone this long is no longer running
START_ATTEMPTS = 3                        # starts that fail before the run is given up
RUN_ATTEMPTS = 2                          # conversations a run gets before it is given up
TAG = 'qorqut-workflow'


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def system_zone() -> str | None:
    """The IANA name of this computer's own time zone, when the system says it."""
    try:
        with open('/etc/timezone', encoding='utf-8') as file:
            return file.readline().strip() or None
    except (OSError, UnicodeError):
        pass
    try:
        path = os.path.realpath('/etc/localtime')
    except OSError:
        return None
    return path.split('/zoneinfo/', 1)[1] if '/zoneinfo/' in path else None


def zone_name(environ=None, system=system_zone) -> str:
    """The time zone the two scheduled times are in: the owner's. The first of these that names a real zone:
    ``QORQUT_EXPERIENCE_TIMEZONE``, the installation's ``QORQUT_TIMEZONE``, this computer's own zone (``TZ``, then what the
    system says), and UTC when none does. The installation's zone alone would not do: it is UTC wherever it was never set,
    and 03:00 UTC is not the owner's night."""
    environ = os.environ if environ is None else environ
    for source, read in (*((name, environ.get) for name in ZONE_ENVIRONMENT), ('the system', lambda _: system())):
        value = (read(source) or '').strip().lstrip(':')
        if not value:
            continue
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError):       # ``ZoneInfoNotFoundError`` is a ``KeyError``
            LOG.warning('%s names no known time zone (%r); looking further for the workflow schedule', source, value)
        else:
            return value
    return 'UTC'


def schedules(environ=None) -> dict:
    """``{kind: cron}`` of the workflows that are switched on. A value that is not a cron expression switches nothing on
    and is logged: a typing mistake must not run a workflow at a time nobody chose."""
    environ = os.environ if environ is None else environ
    found = {}
    for kind, name in ENVIRONMENT.items():
        value = (environ.get(name) or '').strip() or DEFAULTS[kind]
        if value.lower() in ('off', '0', 'false', 'no'):
            continue
        try:
            parse_cron(value)
        except RoutineError:
            LOG.warning('%s is not a five-field cron expression; the %s is not scheduled', name, TITLES[kind])
            continue
        found[kind] = value
    return found


def kickoff(run, zone=None) -> str:
    """The first message of a run's fresh conversation. It starts with a tag, so Chat never shows or indexes it as something
    the owner wrote."""
    zone = zone or zone_name()
    lines = [f'Scheduled workflow: {TITLES[run["kind"]]}.', f'Run: {run["id"]}']
    if run['kind'] == EXTRACTION:
        lines.append(f'Period to read: {run["window_start"]} to {run["window_end"]} (UTC; the owner\'s time zone is {zone}).')
    lines.append('This is a fresh conversation started for this run alone. Follow your instructions for this workflow from '
                 'its first step: call experience_run_get, then do the work, then call experience_run_finish. Nobody is '
                 'waiting for a reply in this chat.')
    return f'<{TAG}>\n' + '\n'.join(lines) + f'\n</{TAG}>'


class Runs:
    def __init__(self, runtime, *, clock=None, monotonic=time.monotonic, spawn=None, environ=None, zone=None):
        self.runtime = runtime
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._spawn = spawn or self._thread
        self._environ, self._zone = environ, zone
        self._due_at = self._step_at = self._retry_at = float('-inf')
        self._seen: dict[str, float] = {}
        self._starting: dict | None = None

    @staticmethod
    def _thread(target, *args) -> None:
        threading.Thread(target=target, args=args, name='experience-run-start', daemon=True).start()

    @property
    def zone(self):
        if self._zone is None:
            self._zone = zone_name(self._environ)
        return self._zone

    def schedules(self) -> dict:
        return schedules(self._environ)

    @property
    def database(self):
        return self.runtime.database

    # ----- rows -------------------------------------------------------------------------------------------------------

    @staticmethod
    def running(uow):
        found = uow.connection.execute(f"SELECT id FROM {RUNS} WHERE state='running' ORDER BY started_at, id LIMIT 1").fetchone()
        return uow.repository(RUNS).get(found['id']) if found else None

    @staticmethod
    def next_queued(uow):
        found = uow.connection.execute(f"SELECT id FROM {RUNS} WHERE state='queued' ORDER BY slot, queued_at, id LIMIT 1").fetchone()
        return uow.repository(RUNS).get(found['id']) if found else None

    @staticmethod
    def last_done_end(uow, kind):
        found = uow.connection.execute(f"SELECT max(window_end) FROM {RUNS} WHERE kind=? AND state='done'", (kind,)).fetchone()
        return found[0] if found else None

    def window_start(self, uow, kind, end: str, current: str | None = None) -> str:
        """Where a run of this kind reads from: the end of the last finished one, at most a week back. When none has finished:
        a day back (a week for the review), or ``current``, the start a waiting run already has, when that is earlier, so
        a run that waited keeps the days it waited through."""
        latest = moment(end)
        done = self.last_done_end(uow, kind)
        if done:
            begin = moment(done)
        else:
            begin = latest - FIRST_WINDOW[kind]
            if current:
                begin = min(begin, moment(current))
        return stamp(min(max(begin, latest - LONGEST_WINDOW), latest - timedelta(minutes=1)))

    # ----- what is due ------------------------------------------------------------------------------------------------

    def due(self, uow, *, force: bool = False) -> list[str]:
        """Queue the runs whose scheduled time has passed. The caller owns the write transaction. Returns the ids it queued
        or moved to a newer time. Nothing is queued while no Learning Applier is registered. Paced like ``nudges.paced``:
        the time is kept once the look has finished or failed, so a unit that is abandoned at its first write and run
        again holding the writer looks again."""
        tick = self._monotonic()
        if not force and tick - self._due_at < DUE_SECONDS:
            return []
        try:
            changed = self._due(uow)
        except Exception:
            self._due_at = tick
            raise
        self._due_at = tick
        return changed

    def _due(self, uow) -> list[str]:
        found = self.schedules()
        if not found or active_role_holder(uow.connection, LEARNING_APPLIER_ROLE) is None:
            return []
        now, changed, runs = self._clock(), [], uow.repository(RUNS)
        for kind, cron in found.items():
            slot = prev_slot(cron, now, self.zone)
            text = stamp(slot)
            newest = uow.connection.execute(f'SELECT max(slot) FROM {RUNS} WHERE kind=?', (kind,)).fetchone()[0]
            if newest is None and now - slot > FIRST_RUN_GRACE:
                continue
            if newest is not None and newest >= text:
                continue
            waiting = uow.connection.execute(f"SELECT id FROM {RUNS} WHERE kind=? AND state='queued' ORDER BY slot LIMIT 1",
                                             (kind,)).fetchone()
            if waiting:
                before = runs.get(waiting['id'])
                runs.update(waiting['id'], {'slot': text, 'window_end': text,
                                            'window_start': self.window_start(uow, kind, text, before['window_start'])})
                changed.append(waiting['id'])
                continue
            identity = new_id()
            runs.insert({'id': identity, 'kind': kind, 'state': 'queued', 'slot': text,
                         'window_start': self.window_start(uow, kind, text), 'window_end': text, 'agent_id': None,
                         'conversation_id': None, 'attempts': 0, 'queued_at': stamp(now), 'started_at': None, 'seen_at': None,
                         'finished_at': None, 'result_json': None})
            changed.append(identity)
        return changed

    # ----- starting and watching ---------------------------------------------------------------------------------------

    def step(self) -> None:
        """One look at the queue. Never waits and never raises: it runs on the backend's pass."""
        tick = self._monotonic()
        if tick - self._step_at < STEP_SECONDS:
            return
        self._step_at = tick
        try:
            self._step(tick)
        except DomainError:
            LOG.debug('Experience run step deferred', exc_info=True)       # writes are closed, or the agent cannot act now
        except Exception:  # noqa: BLE001 - nothing here may stop the backend's pass
            LOG.warning('Experience run step failed', exc_info=True)

    def _opener(self):
        return getattr(getattr(self.runtime, 'native_chat', None), 'opener', None)

    def _step(self, tick) -> None:
        if self._starting is not None:
            self._started(self._starting)
            return
        opener = self._opener()
        if opener is None or opener.managed is None or mode.switching(getattr(self.runtime, 'paths', None)):
            return
        with self.database.read() as uow:
            running, queued = self.running(uow), self.next_queued(uow)
            agent_id = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
        if running is not None:
            self._watch(opener, running, tick)
        elif queued is not None and agent_id and tick >= self._retry_at:
            self._start(opener, queued, agent_id)

    def _conversation(self, opener, agent_id):
        """``(conversation id or None when no session runs, phase)``; raises when the session cannot be verified now."""
        host = opener.managed.handle(agent_id)
        if host is None:
            return None, None
        status = host.client.status() if host.client is not None else None
        if not isinstance(status, dict) or status.get('status') != 'ok':
            raise DomainError('host_unverified', "The agent's session could not be read", retryable=True)
        return str(status.get('session_id') or ''), status.get('phase')

    def _began(self, agent_id, conversation):
        """When this conversation was first observed, or ``None`` while it is not bound yet."""
        with self.database.read() as uow:
            found = uow.connection.execute('SELECT min(observed_at) FROM native_sessions WHERE agent_id=? AND conversation_id=?',
                                           (agent_id, conversation)).fetchone()[0]
        return moment(found) if found else None

    def _watch(self, opener, run, tick) -> None:
        """A running run stays running while its conversation runs. One whose conversation has been gone for ``STALL`` gets a
        second fresh conversation, then is given up."""
        try:
            conversation, _ = self._conversation(opener, run['agent_id'])
        except Exception:  # noqa: BLE001 - not known to be gone: looked at again on a later pass
            return
        now = self._clock()
        if run['conversation_id'] and conversation == run['conversation_id']:
            if tick - self._seen.get(run['id'], float('-inf')) >= SEEN_SECONDS:
                self._seen[run['id']] = tick
                self._write(run['id'], {'seen_at': stamp(now)}, state='running')
            return
        waited = True
        if not run['conversation_id'] and conversation:
            # The service stopped between starting this run and recording its conversation. A conversation that began after
            # the run was started is the fresh one; an older one never got the run's first message.
            began = self._began(run['agent_id'], conversation)
            if began is None:
                return
            if began >= moment(run['started_at']):
                self._write(run['id'], {'conversation_id': conversation, 'seen_at': stamp(now)}, state='running')
                return
            waited = False
        last = run['seen_at'] or run['started_at']
        if waited and last and now - moment(last) < STALL:
            return
        self._seen.pop(run['id'], None)
        if run['attempts'] < RUN_ATTEMPTS:
            self._write(run['id'], {'state': 'queued', 'conversation_id': None, 'seen_at': None}, state='running')
            LOG.info('Experience run %s lost its conversation before it was finished; it will be started again', run['id'])
        else:
            self._write(run['id'], {'state': 'abandoned', 'finished_at': stamp(now),
                                    'result_json': {'reason': 'The conversation ended before the run was finished'}}, state='running')
            LOG.warning('Experience run %s was abandoned: its conversation ended before it was finished', run['id'])

    def _start(self, opener, run, agent_id) -> None:
        if not opener.wakeable(agent_id):
            return                                   # off, paused, or an agent the product does not offer yet: the run waits
        try:
            conversation, phase = self._conversation(opener, agent_id)
        except Exception:  # noqa: BLE001 - a session that cannot be read now is read again on a later pass
            return
        if conversation is not None and (conversation == '' or phase != 'idle'):
            return                                   # in the middle of a turn, starting or closing: let it finish
        with self.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if profile is None:
            return
        now = stamp(self._clock())
        with self.database.transaction(operation='experience.run-start') as uow:
            self.runtime.application.ensure_writes_open(uow)
            current = uow.repository(RUNS).get(run['id'])
            if current is None or current['state'] != 'queued' or self.running(uow) is not None:
                return
            values = {'state': 'running', 'agent_id': agent_id, 'conversation_id': None, 'attempts': current['attempts'] + 1,
                      'started_at': now, 'seen_at': now,
                      'window_start': self.window_start(uow, current['kind'], current['window_end'], current['window_start'])}
            uow.repository(RUNS).update(run['id'], values)
            run = {**current, **values}
        flow = {'run': run, 'profile': profile, 'closing': conversation or '', 'outcome': None}
        self._starting = flow
        self._spawn(self._open, opener, flow)

    def _open(self, opener, flow) -> None:
        """On a thread of its own: close the idle conversation and open the fresh one with the run's first message."""
        run = flow['run']
        try:
            session = opener.replace(flow['profile'], flow['closing'], lambda: kickoff(run, self.zone),
                                     f'experience:{run["id"]}:{run["attempts"]}')
            flow['outcome'] = ('opened', session)
        except Exception as error:  # noqa: BLE001 - reported to the pass, which decides whether to try again
            LOG.info('Starting experience run %s failed', run['id'], exc_info=True)
            flow['outcome'] = ('failed', str(getattr(error, 'code', None) or type(error).__name__))

    def _started(self, flow) -> None:
        """Record how a start ended. The flow is let go only once that is written: a write refused now is made on a later pass."""
        outcome, run = flow['outcome'], flow['run']
        if outcome is None:
            return
        if outcome[0] == 'opened':
            self._write(run['id'], {'conversation_id': outcome[1], 'seen_at': stamp(self._clock())}, state='running')
            LOG.info('Experience run %s (%s) started in conversation %s', run['id'], run['kind'], outcome[1])
        elif run['attempts'] >= START_ATTEMPTS:
            self._write(run['id'], {'state': 'abandoned', 'finished_at': stamp(self._clock()),
                                    'result_json': {'reason': 'A fresh conversation could not be started', 'code': outcome[1]}},
                        state='running')
            LOG.warning('Experience run %s was abandoned: no fresh conversation could be started (%s)', run['id'], outcome[1])
        else:
            self._write(run['id'], {'state': 'queued', 'conversation_id': None}, state='running')
        if outcome[0] != 'opened':
            self._retry_at = self._monotonic() + RETRY_SECONDS
        self._starting = None

    def _write(self, identity, values, *, state) -> bool:
        """Change one run that is still in ``state``. False when it is not: the agent finished it meanwhile."""
        with self.database.transaction(operation='experience.run') as uow:
            self.runtime.application.ensure_writes_open(uow)
            current = uow.repository(RUNS).get(identity)
            if current is None or current['state'] != state:
                return False
            uow.repository(RUNS).update(identity, values)
            return True


__all__ = ['EXTRACTION', 'REVIEW', 'Runs', 'TITLES', 'kickoff', 'moment', 'schedules', 'stamp', 'zone_name']
