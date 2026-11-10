"""Durable transport claims for attention reminders.

The two-phase hook claim separates a recoverable reservation from an emission
whose result may be uncertain. All methods below run in the caller's admitted
write transaction; eligibility is checked again in that same transaction.
"""

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4


def _time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must have a timezone')
    return parsed.astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


_CLOCK_FIELDS = frozenset({'board_mode', 'board_due_at', 'focus_due_at',
    'last_agent_activity_at', 'last_owner_message_at', 'reminder_sequence'})


def _focus(value):
    return json.loads(value) if isinstance(value, str) else value


def _age(started_at, now):
    """Whole elapsed time since the focus began; unavailable (None) when it cannot be derived."""
    try:
        minutes = int((_time(now) - _time(started_at)).total_seconds()) // 60
    except (AttributeError, TypeError, ValueError):
        return None
    if minutes < 0:
        return None
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return ' '.join(part for part in (days and '%dd' % days, (days or hours) and '%dh' % hours, '%dm' % minutes) if part)


def record_attention_activity(uow, agent_id, kind, at, event_key, *, attention):
    """Record a genuine source timestamp, using that timestamp as watermark."""
    if kind not in ('agent_activity', 'owner_message') or not isinstance(event_key, str) or not 0 < len(event_key) <= 200:
        raise ValueError('invalid activity provenance')
    at = _stamp(_time(at))
    state = attention.state(uow, agent_id)
    field = 'last_agent_activity_at' if kind == 'agent_activity' else 'last_owner_message_at'
    if state.get(field) and _time(state[field]) >= _time(at):
        return False
    clock_advice(uow, agent_id, attention, _stamp(datetime.now(timezone.utc)), event=kind, event_at=at)
    return True


def _blocked_episode(uow, agent_id, state):
    focus = _focus(state.get('focus_json'))
    if not isinstance(focus, dict) or focus.get('type') != 'workflow':
        return None
    row = uow.connection.execute('SELECT blocked_since FROM attention_workflows WHERE agent_id=? AND run_id=? '
        'AND detached_at IS NULL AND paused=0', (agent_id, focus.get('id'))).fetchone()
    return (focus['id'], row['blocked_since']) if row and row['blocked_since'] else None


def clock_advice(uow, agent_id, attention, now, *, event=None, event_at=None, delivered_kind=None,
                 ignore_episode_receipt=False):
    """Advance only the shared clock columns; never replace focus or intent."""
    from qorqut.agents.attention_clock import advance
    state = dict(attention.state(uow, agent_id))
    stored = {key: state.get(key) for key in _CLOCK_FIELDS}
    focus = _focus(state.get('focus_json'))
    episode = _blocked_episode(uow, agent_id, state)
    blocked = episode[1] if episode else None
    if episode and not ignore_episode_receipt:
        receipt = uow.connection.execute("SELECT 1 FROM attention_reminders WHERE agent_id=? AND kind='workflow_detach' "
            "AND workflow_run_id=? AND blocked_since=? AND (state IN ('submitted','uncertain') "
            "OR (state='claimed' AND focus_generation<>?)) LIMIT 1",
            (agent_id, episode[0], episode[1], state['focus_generation'])).fetchone()
        if receipt:
            state['blocked_reminded_since'] = blocked
    if delivered_kind:
        state['delivered_kind'] = delivered_kind
    result = advance(state, now=now, mode=attention.board_mode(uow, agent_id), focused=bool(focus),
        focus_generation=state['focus_generation'], focus_started_at=state.get('focus_started_at'),
        blocked_since=blocked, event=event, event_at=event_at)
    attention.update_clock(uow, agent_id, {key: result[key] for key in _CLOCK_FIELDS
                                         if key in result and result[key] != stored[key]})
    return result


class AttentionRuntime:
    """Attention scheduling shares the existing nudge pass and input arbiter."""

    def __init__(self, runtime):
        from qorqut.agents.todo_adjustment import TodoAdjustment
        self.runtime = runtime
        self.attention = runtime.application.ordubashy_attention
        self.hooks = HookClaims(self._hook_eligible, self._delivered)
        self.todo_adjustment = TodoAdjustment(runtime)

    def _now(self):
        return _stamp(datetime.now(timezone.utc))

    def _text(self, uow, agent_id, kind):
        """The one reminder text shared by the hook and chat routes; reads only, never changes Attention state."""
        from qorqut.agents import attention_prompt
        view = self.runtime.application.policy.agent_resolver.require_agentic_view()
        with view.read_snapshot():
            instructions = view.runtime_instructions()
        state = self.attention.state(uow, agent_id)
        focus = _focus(state.get('focus_json'))
        focus = focus if isinstance(focus, dict) else None
        if not focus and not kind.startswith('board_'):
            return attention_prompt.render(kind, None, instructions=instructions)
        snapshot = self._snapshot(uow, agent_id, state, focus)
        if focus and focus.get('type') == 'workflow':
            workflow = uow.connection.execute('SELECT workflow_name,run_id FROM attention_workflows '
                'WHERE agent_id=? AND run_id=?', (agent_id, focus.get('id'))).fetchone()
            if workflow:
                snapshot['workflow'] = {'name': workflow['workflow_name'], 'run_id': workflow['run_id']}
        return attention_prompt.render(kind, snapshot, instructions=instructions)

    def _snapshot(self, uow, agent_id, state, focus):
        """Bounded projection through scoped, lifecycle-only Task reads.

        Whatever cannot be read as the coordinator is left unavailable (None) rather than reported as empty. The
        board's default full read, Task threads, relationships and unchecked source labels are never touched."""
        from qorqut_operations.contracts import DomainError
        from qorqut_operations.tasks.types import attributes
        from qorqut_operations.tasks.workers import project_workers
        from qorqut.agents import attention_prompt as prompt
        from qorqut.agents.attention import UNREADABLE
        work_ref = state.get('work_task_json')
        snapshot = {'focus': {key: focus.get(key) for key in ('type', 'id', 'business_id')} if focus else None,
                    'focused_for': _age(state.get('focus_started_at'), self._now()) if focus else None,
                    'work': None, 'intent': None, 'candidates': None, 'more': False}
        try:
            ctx = self.runtime.application.policy.context_for(agent_id, business_id=None, review_business_id=None, uow=uow)
        except DomainError:
            return snapshot

        def member(ref, created_at):
            """The lifecycle record of a still-authorized, unarchived ordinary Task with this membership identity."""
            try:
                task = self.attention.task(ctx, ref, uow, lifecycle_only=True)
            except DomainError as error:
                if error.code not in UNREADABLE:
                    raise
                return None
            if task.get('created_at') != created_at or task.get('archived') or attributes(task)['task_type'] != 'task':
                return None
            return task

        if focus and isinstance(work_ref, dict):
            membership = self.attention.task_membership(uow, agent_id, work_ref)
            task = member(work_ref, membership['task_created_at']) if membership else None
            if task is not None:
                snapshot['work'] = prompt.describe(work_ref, task, project_workers(uow, task))
                snapshot['intent'] = state.get('intent')
        candidates = []
        for row in uow.connection.execute('SELECT * FROM attention_memberships WHERE agent_id=? ORDER BY created_at,id', (agent_id,)):
            ref = {'type': 'task', 'id': row['task_id'], 'business_id': row['task_business_id']}
            if isinstance(work_ref, dict) and (ref['id'], ref['business_id']) == (work_ref.get('id'), work_ref.get('business_id')):
                continue
            task = member(ref, row['task_created_at'])
            # Filter on lifecycle first; Workers are projected only for tasks that will be shown.
            if task is None or task['status'] not in ('open', 'working'):
                continue
            if len(candidates) == prompt.CANDIDATES:
                snapshot['more'] = True
                break
            candidates.append(prompt.describe(ref, task, project_workers(uow, task)))
        snapshot['candidates'] = candidates
        return snapshot

    def _verified_consumer(self, agent_id, consumer_id):
        from qorqut.native_chat import store
        with self.runtime.database.read() as uow:
            profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if not profile:
            return False
        chat = self.runtime.native_chat
        observed = chat.probe.observe(profile)
        reconciled = chat.registry.reconcile(profile, observed)
        return bool(reconciled.live and reconciled.session
                    and reconciled.session['conversation_id'] == consumer_id)

    def hook_claim(self, agent_id, registration_id, consumer_id):
        with self.runtime.native_chat.arbiter.locks(agent_id), self.runtime.application.writer_admission.writer():
            if not self._verified_consumer(agent_id, consumer_id):
                return None
            with self.runtime.database.transaction(operation='attention.hook_claim') as uow:
                self.runtime.application.ensure_writes_open(uow)
                from qorqut.agents.attention import enrolled_board
                if not enrolled_board(uow, agent_id):
                    return None
                binding = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (agent_id,)).fetchone()
                registered = uow.connection.execute('SELECT 1 FROM agent_activity WHERE agent_id=? AND session_id=? '
                    'AND registration_id=?', (agent_id, consumer_id, registration_id)).fetchone()
                if not binding or not registered or binding['consumer_id'] != consumer_id:
                    return None
                now = self._now()
                self.hooks.recover_expired(uow, now, agent_id=agent_id)
                self.reconcile_board(uow, agent_id, now)
                advice = clock_advice(uow, agent_id, self.attention, now)
                state = self.attention.state(uow, agent_id)
                rows = uow.connection.execute("SELECT * FROM attention_reminders WHERE agent_id=? AND state='pending' "
                    "AND kind IN ('focus','workflow_detach') AND kind=? AND focus_generation=? AND sequence=? AND due_at=? LIMIT 1",
                    (agent_id, advice.get('kind'), state['focus_generation'], advice.get('reminder_sequence'),
                     advice.get('due_at'))).fetchall()
                for row in rows:
                    claim = self.hooks.claim(uow, row['id'], consumer_id, binding['generation'], now)
                    if claim:
                        break
                else:
                    return None
            # Rendering loads managed instructions and reads the task board.
            # The claim is durable; hook_begin still checks its generation and
            # eligibility before anything can be emitted.
            with self.runtime.database.read() as uow:
                text = self._text(uow, agent_id, row['kind'])
            return {**claim, 'registration_id': registration_id, 'kind': row['kind'], 'text': text}

    def hook_begin(self, agent_id, registration_id, claim):
        with self.runtime.native_chat.arbiter.locks(agent_id), self.runtime.application.writer_admission.writer():
            if not self._verified_consumer(agent_id, claim['claim_consumer_id']):
                return False
            with self.runtime.database.transaction(operation='attention.hook_begin') as uow:
                self.runtime.application.ensure_writes_open(uow)
                registered = uow.connection.execute('SELECT 1 FROM agent_activity WHERE agent_id=? AND session_id=? '
                    'AND registration_id=?', (agent_id, claim['claim_consumer_id'], registration_id)).fetchone()
                row = uow.connection.execute('SELECT agent_id FROM attention_reminders WHERE id=?', (claim['id'],)).fetchone()
                return bool(registered and row and row['agent_id'] == agent_id and self.hooks.begin(uow, claim, self._now()))

    def hook_settle(self, agent_id, registration_id, claim):
        from pathlib import Path
        from qorqut.bootstrap.activity_hook import safe_path
        from qorqut.composition.locations import agent_workspace_of
        with self.runtime.native_chat.arbiter.locks(agent_id), self.runtime.application.writer_admission.writer():
            with self.runtime.database.transaction(operation='attention.hook_settle') as uow:
                self.runtime.application.ensure_writes_open(uow)
                registered = uow.connection.execute('SELECT 1 FROM agent_activity WHERE agent_id=? AND session_id=? '
                    'AND registration_id=?', (agent_id, claim['claim_consumer_id'], registration_id)).fetchone()
                profile = uow.connection.execute('SELECT p.home_business_id,b.slug FROM agent_profiles p '
                    'LEFT JOIN businesses b ON b.id=p.home_business_id WHERE p.agent_id=?', (agent_id,)).fetchone()
                row = uow.connection.execute('SELECT agent_id FROM attention_reminders WHERE id=?', (claim['id'],)).fetchone()
                if not registered or not profile or not row or row['agent_id'] != agent_id:
                    return False
                # Both components are canonical UUIDs, so the client cannot choose a path.
                from uuid import UUID
                occurrence, token = str(UUID(claim['id'])), str(UUID(claim['claim_token']))
                workspace = agent_workspace_of(self.runtime.paths, agent_id, profile['home_business_id'], profile['slug'])
                path = Path(workspace) / '.qorqut' / 'attention-receipts' / (occurrence + '-' + token + '.json')
                if not safe_path(path) or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
                    return False
                receipt = json.loads(path.read_text())
                if receipt.get('registration_id') != registration_id or any(receipt.get(key) != claim.get(key) for key in
                    ('id', 'claim_token', 'claim_consumer_id', 'claim_binding_generation', 'focus_generation')):
                    return False
                return self.hooks.settle(uow, receipt, self._now())

    def _hook_eligible(self, uow, row, consumer_id, binding_generation, now):
        if not self.attention.reminder_eligible(uow, row['agent_id'], consumer_id=consumer_id,
                binding_generation=binding_generation, focus_generation=row['focus_generation'], kind=row['kind']):
            return False
        advice = clock_advice(uow, row['agent_id'], self.attention, now)
        return (advice.get('kind') == row['kind'] and advice.get('due_at') == row['due_at']
                and advice.get('reminder_sequence') == row['sequence'] and advice.get('hook_eligible') is True)

    def _delivered(self, uow, row, at):
        state = self.attention.state(uow, row['agent_id'])
        if (state['focus_generation'] != row['focus_generation']
                or state['reminder_sequence'] != row['sequence']):
            return
        now = self._now()
        focused = bool(_focus(state.get('focus_json')))
        if focused != (row['kind'] in ('focus', 'workflow_detach')):
            self.attention.update_clock(uow, row['agent_id'], {'reminder_sequence': row['sequence'] + 1})
            return
        if (row['kind'] == 'workflow_detach' and
                _blocked_episode(uow, row['agent_id'], state) != (row['workflow_run_id'], row['blocked_since'])):
            self.attention.update_clock(uow, row['agent_id'], {'reminder_sequence': row['sequence'] + 1})
            return
        before = clock_advice(uow, row['agent_id'], self.attention, now,
                              ignore_episode_receipt=row['kind'] == 'workflow_detach')
        after = clock_advice(uow, row['agent_id'], self.attention, now, event='delivered', event_at=at,
                             delivered_kind=row['kind'], ignore_episode_receipt=row['kind'] == 'workflow_detach')
        # The receipt may arrive after a newer owner message or activity moved
        # the deadline. Consume the occurrence without shortening that deadline.
        field = 'focus_due_at' if focused else 'board_due_at'
        prior, current = before.get(field), after.get(field)
        if prior and current and _time(prior) > _time(current):
            self.attention.update_clock(uow, row['agent_id'], {field: prior})

    def reconcile(self, uow):
        """One bounded pass over every enrolled board; none of them can postpone another's occurrence."""
        from qorqut.agents.attention import enrolled_boards
        now = self._now()
        self.hooks.recover_expired(uow, now)
        for agent_id in enrolled_boards(uow):
            self.reconcile_board(uow, agent_id, now)

    def reconcile_board(self, uow, agent_id, now):
        advice = clock_advice(uow, agent_id, self.attention, now)
        kind, due = advice.get('kind'), advice.get('due_at')
        if not kind or not due:
            return
        state = self.attention.state(uow, agent_id)
        episode = _blocked_episode(uow, agent_id, state) if kind == 'workflow_detach' else None
        if kind == 'workflow_detach' and not episode:
            return
        previous = uow.connection.execute('SELECT * FROM attention_reminders WHERE agent_id=? AND kind=? '
            'AND focus_generation=? AND sequence=?', (agent_id, kind, state['focus_generation'],
                                                        advice['reminder_sequence'])).fetchone()
        if (previous and episode and previous['state'] in ('claimed', 'uncertain', 'submitted')
                and (previous['workflow_run_id'], previous['blocked_since']) != episode):
            # A distinct blocked episode is independent; retain the old
            # uncertain receipt while assigning the new episode a new key.
            self.attention.update_clock(uow, agent_id, {'reminder_sequence': advice['reminder_sequence'] + 1})
            advice = clock_advice(uow, agent_id, self.attention, now)
            state = self.attention.state(uow, agent_id)
            previous = None
        if previous and previous['state'] == 'cancelled':
            if previous['route'] == 'chat':
                from qorqut.native_chat import store
                native = store.input_by_key(uow.connection, agent_id, 'attention:' + previous['id'])
                self._settle_chat(uow, previous, native, now)
            elif previous['emission_started_at'] is None:
                uow.connection.execute("UPDATE attention_reminders SET state='pending',route=NULL,claim_token=NULL,"
                    'claim_consumer_id=NULL,claim_binding_generation=NULL,claim_expires_at=NULL,updated_at=?,revision=revision+1 '
                    "WHERE id=? AND state='cancelled' AND emission_started_at IS NULL", (now, previous['id']))
            # Settling a proven delivery advances the sequence, whereas an
            # uncertain native input remains quarantined under its old key.
            advice = clock_advice(uow, agent_id, self.attention, now)
            kind, due = advice.get('kind'), advice.get('due_at')
            if not kind or not due:
                return
            state = self.attention.state(uow, agent_id)
        not_before = advice.get('chat_not_before') or due
        episode = _blocked_episode(uow, agent_id, state) if kind == 'workflow_detach' else None
        if kind == 'workflow_detach' and not episode:
            return
        uow.connection.execute('INSERT INTO attention_reminders '
            '(id,agent_id,kind,focus_generation,sequence,due_at,not_before,workflow_run_id,blocked_since,state,created_at,updated_at,revision) '
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,1) ON CONFLICT DO UPDATE SET due_at=excluded.due_at,"
            'not_before=excluded.not_before,workflow_run_id=excluded.workflow_run_id,blocked_since=excluded.blocked_since,'
            'updated_at=excluded.updated_at,revision=attention_reminders.revision+1 '
            "WHERE attention_reminders.state='pending' AND (attention_reminders.due_at<>excluded.due_at "
            'OR attention_reminders.not_before<>excluded.not_before '
            'OR attention_reminders.workflow_run_id IS NOT excluded.workflow_run_id '
            'OR attention_reminders.blocked_since IS NOT excluded.blocked_since)',
            (str(uuid4()), agent_id, kind, state['focus_generation'], advice['reminder_sequence'],
             due, not_before, episode[0] if episode else None, episode[1] if episode else None, now, now))

    def _settle_chat(self, uow, row, native, now):
        state = native['state'] if native else 'pending'
        if state == 'submitting':
            return
        # Keep the transport's uncertainty as history. Exact marked transcript
        # evidence proves arrival, not processing, and consumes this occurrence
        # without typing it again. Ordinary uncertain inputs stay quarantined.
        evidence = json.loads(native.get('evidence_json') or '{}') if native else {}
        observed_at = (evidence.get('native_input_observed_at') if state == 'uncertain'
            and native.get('reason') == 'transcript_confirmed'
            and evidence.get('native_input_observed') is True else None)
        if state == 'submitted' or observed_at:
            delivered_at = observed_at or native['settled_at'] or native['updated_at']
            changed = uow.connection.execute("UPDATE attention_reminders SET state='submitted',native_input_id=?,"
                "delivered_at=?,updated_at=?,revision=revision+1 WHERE id=? AND state IN ('claimed','cancelled','uncertain') AND route='chat'",
                (native['id'], delivered_at, now, row['id']))
            if changed.rowcount:
                self._delivered(uow, row, delivered_at)
        elif state == 'uncertain':
            uow.connection.execute("UPDATE attention_reminders SET state='uncertain',native_input_id=?,updated_at=?,"
                "revision=revision+1 WHERE id=? AND state IN ('claimed','cancelled') AND route='chat'", (native['id'], now, row['id']))
        elif native is None or (state == 'failed' and (native['reason'] or '').startswith('not_typed:')):
            uow.connection.execute("UPDATE attention_reminders SET state='pending',route=NULL,claim_token=NULL,"
                "claim_consumer_id=NULL,claim_binding_generation=NULL,claim_expires_at=NULL,updated_at=?,revision=revision+1 "
                "WHERE id=? AND state IN ('claimed','cancelled') AND route='chat'", (now, row['id']))
        else:
            # An unrecognized input state cannot prove the occurrence was not typed.
            uow.connection.execute("UPDATE attention_reminders SET state='uncertain',native_input_id=?,updated_at=?,"
                "revision=revision+1 WHERE id=? AND state IN ('claimed','cancelled') AND route='chat'", (native['id'], now, row['id']))

    def send_due(self, profile):
        """At most one chat attempt, under the arbiter's existing agent lock."""
        if self.todo_adjustment.send_due(profile):
            return
        from qorqut.native_chat import store
        from qorqut.native_chat.arbiter import StaleSession
        agent = profile['agent_id']
        chat = self.runtime.native_chat
        with chat.arbiter.locks(agent):
            with self.runtime.database.read() as uow:
                receipts = uow.connection.execute("SELECT r.*,a.registration_id FROM attention_reminders r "
                    'JOIN agent_activity a ON a.agent_id=r.agent_id AND a.session_id=r.claim_consumer_id '
                    "WHERE r.agent_id=? AND r.route='hook' AND r.state IN ('claimed','uncertain') "
                    'AND r.emission_started_at IS NOT NULL ORDER BY r.updated_at LIMIT 10', (agent,)).fetchall()
            for receipt in receipts:
                self.hook_settle(agent, receipt['registration_id'], dict(receipt))
            with self.runtime.database.read() as uow:
                from qorqut.agents.attention import enrolled_board
                if not enrolled_board(uow, agent):
                    return
            with self.runtime.database.transaction(operation='attention.reconcile_chat') as uow:
                self.runtime.application.ensure_writes_open(uow)
                if not enrolled_board(uow, agent):
                    return
                now = self._now()
                # Search for definitive receipts in SQL before applying the
                # bound, so old unresolved inputs cannot hide newer evidence.
                known = uow.connection.execute("SELECT r.* FROM attention_reminders r WHERE r.agent_id=? "
                    "AND r.state='uncertain' AND r.route='chat' AND EXISTS (SELECT 1 FROM native_inputs n "
                    "WHERE n.agent_id=r.agent_id AND n.source_key='attention:'||r.id AND (n.state='submitted' "
                    "OR (n.state='uncertain' AND n.reason='transcript_confirmed' "
                    "AND json_extract(n.evidence_json,'$.native_input_observed')=1 "
                    "AND json_extract(n.evidence_json,'$.native_input_observed_at') IS NOT NULL)) "
                    "AND NOT EXISTS (SELECT 1 FROM native_inputs newer WHERE newer.agent_id=n.agent_id "
                    "AND newer.source_key=n.source_key AND newer.attempt>n.attempt)) ORDER BY r.updated_at,r.id LIMIT 10",
                    (agent,)).fetchall()
                for uncertain in known:
                    self._settle_chat(uow, uncertain, store.input_by_key(uow.connection, agent, 'attention:' + uncertain['id']), now)
                for claimed in uow.connection.execute("SELECT * FROM attention_reminders WHERE agent_id=? "
                        "AND state='claimed' AND route='chat' LIMIT 10", (agent,)).fetchall():
                    self._settle_chat(uow, claimed, store.input_by_key(uow.connection, agent, 'attention:' + claimed['id']), now)
                owner = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (agent,)).fetchone()
                if not owner or owner['desired_state'] in ('paused', 'stopped') or store.waiting_inputs(uow.connection, agent):
                    return
                session = store.live_session(uow.connection, agent)
                if not session or session['conversation_id'] != owner['consumer_id']:
                    return
                advice = clock_advice(uow, agent, self.attention, now)
                current = self.attention.state(uow, agent)
                if not advice.get('chat_eligible'):
                    return
                row = uow.connection.execute("SELECT * FROM attention_reminders WHERE agent_id=? AND state='pending' "
                    'AND kind=? AND due_at=? AND sequence=? AND focus_generation=? LIMIT 1',
                    (agent, advice.get('kind'), advice.get('due_at'), advice.get('reminder_sequence'),
                     current['focus_generation'])).fetchone()
                if not row or not self.attention.reminder_eligible(uow, agent, consumer_id=owner['consumer_id'],
                        binding_generation=owner['generation'], focus_generation=row['focus_generation'], kind=row['kind']):
                    return
                token = str(uuid4())
                expires = _stamp(_time(now) + timedelta(seconds=60))
                changed = uow.connection.execute("UPDATE attention_reminders SET state='claimed',route='chat',claim_token=?,"
                    'claim_consumer_id=?,claim_binding_generation=?,claim_expires_at=?,updated_at=?,revision=revision+1 '
                    "WHERE id=? AND state='pending' AND revision=?", (token, owner['consumer_id'], owner['generation'], expires, now,
                    row['id'], row['revision']))
                if not changed.rowcount:
                    return
                occurrence_id, generation, kind = row['id'], row['focus_generation'], row['kind']
                consumer, binding_generation = owner['consumer_id'], owner['generation']
            # Keep catalog loading and board projection out of SQLite's sole
            # writer. The existing checkpoint revalidates this exact claim
            # immediately before the native input is submitted.
            with self.runtime.database.read() as uow:
                text = self._text(uow, agent, kind)
            observed = chat.probe.observe(profile)
            reconciled = chat.registry.reconcile(profile, observed)
            if not reconciled.live or not reconciled.session or reconciled.session['id'] != session['id']:
                with self.runtime.database.transaction(operation='attention.release_chat') as uow:
                    row = uow.connection.execute('SELECT * FROM attention_reminders WHERE id=?', (occurrence_id,)).fetchone()
                    self._settle_chat(uow, row, None, self._now())
                return

            def checkpoint():
                with self.runtime.database.transaction(operation='attention.chat_checkpoint') as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    current = uow.connection.execute('SELECT * FROM attention_reminders WHERE id=?', (occurrence_id,)).fetchone()
                    now = self._now()
                    advice = clock_advice(uow, agent, self.attention, now)
                    if (not current or current['state'] != 'claimed' or current['claim_token'] != token
                            or advice.get('kind') != kind or advice.get('due_at') != current['due_at']
                            or advice.get('reminder_sequence') != current['sequence'] or not advice.get('chat_eligible')
                            or not self.attention.reminder_eligible(uow, agent, consumer_id=consumer,
                                binding_generation=binding_generation, focus_generation=generation, kind=kind)):
                        raise StaleSession()
            result = chat.arbiter.admit_nudge(agent, 'attention:' + occurrence_id, text,
                expected_binding=session['id'], metadata={'attention_reminder': occurrence_id}, on_checkpoint=checkpoint)
            with self.runtime.database.transaction(operation='attention.settle_chat') as uow:
                self.runtime.application.ensure_writes_open(uow)
                row = uow.connection.execute('SELECT * FROM attention_reminders WHERE id=?', (occurrence_id,)).fetchone()
                native = store.input_by_key(uow.connection, agent, 'attention:' + occurrence_id)
                self._settle_chat(uow, row, native, self._now())


class HookClaims:
    """CAS protocol shared by the authenticated hook and reconciliation pass.

    ``eligible`` must verify the current agent, binding, focus generation and
    hook deadline in the supplied transaction. ``delivered`` rearms only when
    the current focus and occurrence are still applicable.
    """

    def __init__(self, eligible, delivered):
        self.eligible = eligible
        self.delivered = delivered

    def claim(self, uow, occurrence_id, consumer_id, binding_generation, now):
        now = _stamp(_time(now))
        conn = uow.connection
        row = conn.execute('SELECT * FROM attention_reminders WHERE id=?', (occurrence_id,)).fetchone()
        if not row or row['state'] != 'pending' or not self.eligible(uow, row, consumer_id, binding_generation, now):
            return None
        token = str(uuid4())
        expires = _stamp(_time(now) + timedelta(seconds=60))
        changed = conn.execute("UPDATE attention_reminders SET state='claimed',route='hook',claim_token=?,"
            'claim_consumer_id=?,claim_binding_generation=?,claim_expires_at=?,emission_started_at=NULL,'
            'updated_at=?,revision=revision+1 WHERE id=? AND state=\'pending\' AND revision=?',
            (token, consumer_id, binding_generation, expires, now, occurrence_id, row['revision']))
        if changed.rowcount != 1:
            return None
        return {'id': occurrence_id, 'claim_token': token, 'claim_consumer_id': consumer_id,
                'claim_binding_generation': binding_generation, 'focus_generation': row['focus_generation'],
                'revision': row['revision'] + 1, 'claim_expires_at': expires}

    def begin(self, uow, claim, now):
        now = _stamp(_time(now))
        conn = uow.connection
        row = conn.execute('SELECT * FROM attention_reminders WHERE id=?', (claim['id'],)).fetchone()
        if (not row or row['state'] != 'claimed' or row['route'] != 'hook'
                or row['claim_token'] != claim['claim_token']
                or row['claim_consumer_id'] != claim['claim_consumer_id']
                or row['claim_binding_generation'] != claim['claim_binding_generation']
                or row['focus_generation'] != claim['focus_generation']
                or row['revision'] != claim['revision'] or row['emission_started_at'] is not None
                or _time(now) >= _time(row['claim_expires_at'])
                or not self.eligible(uow, row, claim['claim_consumer_id'], claim['claim_binding_generation'], now)):
            return False
        changed = conn.execute('UPDATE attention_reminders SET emission_started_at=?,updated_at=?,revision=revision+1 '
            "WHERE id=? AND state='claimed' AND revision=? AND claim_token=? AND emission_started_at IS NULL",
            (now, now, claim['id'], claim['revision'], claim['claim_token']))
        return changed.rowcount == 1

    def recover_expired(self, uow, now, *, agent_id=None):
        # A begun emission has crossed the uncertainty boundary. In particular,
        # a lost begin response cannot be treated as an unstarted emission.
        now = _stamp(_time(now))
        conn = uow.connection
        scope = ' AND agent_id=?' if agent_id is not None else ''
        parameters = (now, now, agent_id) if agent_id is not None else (now, now)
        uncertain = conn.execute("UPDATE attention_reminders SET state='uncertain',updated_at=?,revision=revision+1 "
            "WHERE state='claimed' AND route='hook' AND emission_started_at IS NOT NULL AND claim_expires_at<=?" + scope,
            parameters).rowcount
        pending = conn.execute("UPDATE attention_reminders SET state='pending',route=NULL,claim_token=NULL,"
            'claim_consumer_id=NULL,claim_binding_generation=NULL,claim_expires_at=NULL,updated_at=?,revision=revision+1 '
            "WHERE state='claimed' AND route='hook' AND emission_started_at IS NULL AND claim_expires_at<=?" + scope,
            parameters).rowcount
        return pending, uncertain

    def settle(self, uow, receipt, now):
        now = _stamp(_time(now))
        conn = uow.connection
        row = conn.execute('SELECT * FROM attention_reminders WHERE id=?', (receipt['id'],)).fetchone()
        if not row or row['route'] != 'hook' or row['claim_token'] != receipt['claim_token']:
            return False
        if (row['claim_consumer_id'] != receipt['claim_consumer_id']
                or row['claim_binding_generation'] != receipt['claim_binding_generation']
                or row['focus_generation'] != receipt['focus_generation'] or not row['emission_started_at']):
            return False
        flushed = receipt['delivered_at']
        if _time(flushed) < _time(row['emission_started_at']) or _time(flushed) > _time(now):
            return False
        if row['state'] == 'submitted':
            return row['delivered_at'] == flushed
        if row['state'] not in ('claimed', 'uncertain'):
            return False
        changed = conn.execute("UPDATE attention_reminders SET state='submitted',delivered_at=?,updated_at=?,revision=revision+1 "
            "WHERE id=? AND revision=? AND state IN ('claimed','uncertain') AND claim_token=?",
            (flushed, now, row['id'], row['revision'], receipt['claim_token']))
        if changed.rowcount != 1:
            return False
        self.delivered(uow, row, flushed)
        return True


def attach_attention_runtime(runtime):
    attention = AttentionRuntime(runtime)
    previous = runtime.inbox_nudges.reconcile

    def reconcile(uow):
        if previous:
            previous(uow)
        attention.reconcile(uow)

    runtime.inbox_nudges.reconcile = reconcile
    runtime.attention_runtime = attention
    return attention
