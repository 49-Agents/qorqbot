"""Small authoritative completion checks; never execute the agent's business work."""
import hashlib
import json
import uuid

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks import CLOSED_STATES
from qorqut_operations.application.plans import timestamp, utc_clock
from bazarlyk.bootstrap.activity_hook import safe_path
from bazarlyk.composition.locations import agent_workspace_of

RUNTIMES = frozenset({'claude-code', 'codex'})
REVIEW = 'work-review'
REMINDER = ('Before reporting verified completion, persist the task\'s actual state and one short evidence/result note; '
            'use tasks_complete(note=...) or tasks_update(note=...) when appropriate instead of a duplicate comment. '
            'Do not record proposals as approved. Update the related parent or group Task only if the outcome or '
            'blocker changed. Global peers with Focus rights archive or reframe the card for remaining owner action, '
            'preserving owner questions and required review. Clear finished current-work items and obsolete blockers; '
            'save only meaningful working-memory changes. Read receipts and reread on revision conflict. For recurring '
            'work, note this dated occurrence and keep its standing task open. Keep bookkeeping silent: no unchanged '
            'replies or sync-only notes. Record each atomic completed action once with actions_record (at most 20 words); '
            'if none exists, record nothing and never repeat a receipt. Guidance, not proof of completion.')


def native_checkpoint(runtime, agent_id, session_id, registration_id=None):
    """Read only the registered session's protected native observation."""
    with runtime.database.read() as uow:
        profile = uow.connection.execute('SELECT p.home_business_id,b.slug FROM agent_profiles p '
            'LEFT JOIN businesses b ON b.id=p.home_business_id WHERE p.agent_id=?',(agent_id,)).fetchone()
        if not profile:
            return None
    workspace = agent_workspace_of(runtime.paths, agent_id, profile['home_business_id'], profile['slug'])
    path = workspace / '.bazarlyk/activity' / (str(uuid.UUID(session_id)) + '.json')
    try:
        if not safe_path(path) or not path.is_file() or path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text())
        if (value.get('agent_id') != agent_id or value.get('session_id') != session_id
                or value.get('runtime') not in RUNTIMES
                or registration_id and value.get('registration_id') != registration_id):
            return None
        state = value.get('checkpoint', {})
        if state.get('protocol') != 1:
            return None
        for key in ('turn_id', 'root_turn_id'):
            if str(uuid.UUID(state[key])) != state[key]:
                return None
        if type(state.get('dirty_sequence')) is not int or not 0 <= state['dirty_sequence'] < 2**63:
            return None
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return None


class AgentCheckpoints:
    def __init__(self, runtime, *, clock=utc_clock):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock

    def _context(self, ctx, registration, session, uow):
        ctx = self.app.current_context(ctx, uow)
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'Native checkpoints require an agent')
        machine = uow.repository('machine_registrations').get(registration, required=True)
        row = uow.repository('agent_activity').get(
            {'agent_id': ctx.principal_id, 'session_id': session}, required=True)
        if (machine['principal_id'] != ctx.principal_id or row['registration_id'] != registration
                or row['runtime'] not in RUNTIMES):
            raise DomainError('forbidden', 'Checkpoint session is not bound to this registration')
        watcher = uow.connection.execute('SELECT consumer_id,generation FROM inbox_watchers WHERE agent_id=?',
                                          (ctx.principal_id,)).fetchone()
        if watcher and (watcher['consumer_id'] != session or watcher['generation'] > 0 and (
                type(ctx.inbox_generation) is not int or watcher['generation'] != ctx.inbox_generation)):
            raise DomainError('inbox_session_superseded', 'A newer session owns this agent inbox')
        return ctx, row

    def _findings(self, ctx, row, native, uow):
        items = row['items_json']  # Repository rows already decode JSON columns.
        issues = {}
        for item in items[:10]:
            task_id, business = item.get('task_id'), item['business_id']
            if not task_id:
                continue
            key = 'task:' + hashlib.sha256(json.dumps([business, task_id]).encode()).hexdigest()
            code, message = None, None
            try:
                selected = self.runtime.activity.allowed(ctx, business, 'tasks_get', uow)
                task = uow.repository('tasks', business_id=business).get(task_id, required=True)
                if task['archived'] or task['status'] in CLOSED_STATES:
                    code = 'finished_work_listed'
                    message = f'Remove completed or archived task {task_id} from current work, or record its actual follow-up.'
            except DomainError as exc:
                if exc.code not in {'forbidden', 'not_found', 'policy_denied', 'operation_unavailable'}:
                    raise
                code = 'unavailable_work_reference'
                message = 'Remove a current-work task reference whose source is no longer accessible.'
            if code:
                issues[key] = {'code': code, 'message': message, 'business_id': business,
                               'task_id': task_id}
        reviewed = (row['work_reviewed_turn'] == native['root_turn_id']
                    and row['work_reviewed_sequence'] >= native['dirty_sequence'])
        if native['dirty_sequence'] and not reviewed:
            # One reminder episode per substantive root turn, never per Stop or continuation.
            issues[REVIEW + ':' + native['root_turn_id']] = {
                'code': 'work_review_missing', 'business_id': ctx.business_id, 'task_id': None,
                'message': REMINDER}
        inbox = getattr(self.app, 'inbox', None)
        if inbox is not None and inbox.pending_owner_conversations(ctx, row['session_id'], uow):
            # Keep this issue session-bound and deliberately generic. The source
            # may be private even when its notification is old or was delivered.
            issues['owner-inbox'] = {
                'code': 'pending_owner_messages', 'business_id': None, 'task_id': None,
                'message': 'There are pending owner messages; reconcile and handle or explicitly defer them.'}
        return issues, reviewed

    def check(self, ctx, registration_id, session_id, turn_id, *, allow_correction=False, claim_id=None, binding_generation=None):
        if binding_generation is not None:
            from dataclasses import replace
            ctx = replace(ctx,inbox_session_id=session_id,inbox_generation=binding_generation)
        if type(allow_correction) is not bool:
            raise DomainError('validation_error', 'Invalid correction allowance')
        try:
            session_id, turn_id = str(uuid.UUID(session_id)), str(uuid.UUID(turn_id))
            registration_id = str(uuid.UUID(registration_id))
            if allow_correction:
                claim_id = str(uuid.UUID(claim_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise DomainError('validation_error', 'Invalid checkpoint identity') from exc
        native = native_checkpoint(self.runtime, ctx.principal_id, session_id, registration_id)
        if not native or native['turn_id'] != turn_id:
            return {'state': 'unobserved', 'issues': [], 'correction': None}
        with self.app.database.read() as uow:
            ctx, row = self._context(ctx, registration_id, session_id, uow)
            issues, reviewed = self._findings(ctx, row, native, uow)
            saved = {r['check_key']: dict(r) for r in uow.connection.execute(
                "SELECT * FROM agent_checkpoint_findings WHERE agent_id=? AND state='open'",
                (ctx.principal_id,))}
            changed = any(k not in saved for k in issues) or any(
                self._closes(k, issues, reviewed) for k in saved)
            candidate = any(k not in saved or not saved[k]['claimed_at'] for k in issues)
            if not changed and not (allow_correction and candidate):
                return self._result(issues, saved, [], 'checked')
        now = timestamp(self.clock)
        with self.runtime.activity.writer(), self.app.database.transaction() as uow:
            self.app.ensure_writes_open(uow)
            ctx, row = self._context(ctx, registration_id, session_id, uow)
            # Current source state is rechecked inside the single claim transaction.
            issues, reviewed = self._findings(ctx, row, native, uow)
            saved = {r['check_key']: dict(r) for r in uow.connection.execute(
                'SELECT * FROM agent_checkpoint_findings WHERE agent_id=?', (ctx.principal_id,))}
            if len(set(issues) | {k for k,r in saved.items() if r['state']=='open'}) > 100:
                return {'state': 'capacity', 'issues': [], 'correction': None}
            # Only resolved findings expire. Never free capacity by resetting
            # suppression for an unresolved issue.
            uow.connection.execute("DELETE FROM agent_checkpoint_findings WHERE agent_id=? AND state='resolved' "
                                   "AND resolved_at < datetime(?, '-7 days')", (ctx.principal_id, now))
            for key, before in saved.items():
                if before['state'] == 'open' and self._closes(key, issues, reviewed):
                    uow.connection.execute("UPDATE agent_checkpoint_findings SET state='resolved',resolved_at=?,updated_at=? "
                                           'WHERE agent_id=? AND check_key=?', (now, now, ctx.principal_id, key))
            claimed = []
            for key, issue in issues.items():
                before = saved.get(key)
                if before is None or before['state'] == 'resolved':
                    episode = before['episode'] + 1 if before else 1
                    uow.connection.execute('INSERT INTO agent_checkpoint_findings'
                        '(agent_id,check_key,code,business_id,task_id,session_id,episode,state,message,created_at,updated_at) '
                        "VALUES(?,?,?,?,?,?,?,'open',?,?,?) ON CONFLICT(agent_id,check_key) DO UPDATE SET "
                        "code=excluded.code,business_id=excluded.business_id,task_id=excluded.task_id,session_id=excluded.session_id,"
                        "episode=excluded.episode,state='open',message=excluded.message,created_at=excluded.created_at,"
                        'updated_at=excluded.updated_at,resolved_at=NULL,claimed_at=NULL,claim_id=NULL',
                        (ctx.principal_id, key, issue['code'], issue['business_id'], issue['task_id'],
                         session_id, episode, issue['message'], now, now))
                if allow_correction:
                    result = uow.connection.execute('UPDATE agent_checkpoint_findings SET claimed_at=?,claim_id=? '
                        "WHERE agent_id=? AND check_key=? AND state='open' AND claimed_at IS NULL",
                        (now, claim_id, ctx.principal_id, key))
                    if result.rowcount:
                        claimed.append(issue['message'])
            current = {r['check_key']: dict(r) for r in uow.connection.execute(
                "SELECT * FROM agent_checkpoint_findings WHERE agent_id=? AND state='open'", (ctx.principal_id,))}
            return self._result(issues, current, claimed, 'checked')

    @staticmethod
    def _closes(key, issues, reviewed):
        """A review reminder stays open until reviewed or replaced by a later turn's reminder."""
        if key in issues:
            return False
        return not key.startswith(REVIEW) or reviewed or any(k.startswith(REVIEW) for k in issues)

    @staticmethod
    def _result(issues, saved, claimed, state):
        return {'state': state, 'outstanding_count': len(saved),
                'issues': [{'code': issue['code'], 'message': issue['message'],
                            'correction_attempted': bool(saved.get(key, {}).get('claimed_at'))}
                           for key, issue in issues.items()],
                'correction': '\n'.join(dict.fromkeys(claimed))[:2500] if claimed else None}

    def summary(self, ctx, agent_id, session, uow):
        visible = []
        scopes = self.runtime.activity.scopes(ctx, uow)
        for row in uow.connection.execute(
                "SELECT * FROM agent_checkpoint_findings WHERE agent_id=? AND state='open' LIMIT 101", (agent_id,)):
            # The exact native session gets this finding through check(). Only
            # the installation owner may also see it in the existing activity
            # view; it is excluded from every other agent's summary.
            if row['check_key'] == 'owner-inbox':
                from bazarlyk.management.identity import installation_owner
                if not (ctx.principal_kind == 'human'
                        and ctx.principal_id == installation_owner(uow)):
                    continue
            if row['business_id'] not in scopes:
                continue
            try:
                self.runtime.activity.allowed(ctx, row['business_id'], 'agents_activity_list', uow)
                if row['task_id']:
                    self.runtime.activity.allowed(ctx, row['business_id'], 'tasks_get', uow)
            except DomainError:
                continue
            visible.append({'code': row['code'], 'message': row['message'],
                            'correction_attempted': bool(row['claimed_at'])})
        return {'state': session['checkpoint_state'], 'last_checked_at': session['checkpoint_checked_at'],
                'outstanding_count': len(visible), 'issues': visible[:10]}
