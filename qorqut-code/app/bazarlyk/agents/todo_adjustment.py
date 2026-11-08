"""One daily Todo adjustment invitation on the first eligible inactivity pass.

The native input ledger already persists immutable attempts across sessions and
restarts. It is also this invitation's clock and deduplication store: no second
scheduler, schema, model runner, or automatic Task mutation is needed.
"""
from datetime import datetime, timezone

from qorqut_agentic.behavior.todo_adjustment import due, instant, IDLE
from bazarlyk.management.identity import configured_ordubashy
from bazarlyk.native_chat import store
from bazarlyk.native_chat.arbiter import StaleSession
from bazarlyk.native_chat.contracts import InputRequest, TYPE_NUDGE

PREFIX = 'todo-adjustment:'


class TodoAdjustment:
    def __init__(self, runtime, *, clock=None):
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def latest(self, uow, agent):
        # Known-not-typed failures don't spend the daily allowance. Unknown
        # outcomes do: never replay a possibly delivered invitation in a loop.
        row = uow.connection.execute(
            "SELECT * FROM native_inputs WHERE agent_id=? AND origin='system' AND input_type=? "
            "AND source_key GLOB 'todo-adjustment:*' AND state<>'undelivered' "
            "AND NOT (state='failed' AND coalesce(reason,'') LIKE 'not_typed:%') "
            "ORDER BY sequence DESC LIMIT 1", (agent, TYPE_NUDGE)).fetchone()
        return dict(row) if row else None

    def candidate(self, uow, agent, session, now):
        attention = self.runtime.application.ordubashy_attention
        owner = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (agent,)).fetchone()
        if (not owner or session['conversation_id'] != owner['consumer_id']
                or store.waiting_inputs(uow.connection, agent)):
            return None
        state = attention.state(uow, agent)
        if not attention.reminder_eligible(uow, agent, consumer_id=owner['consumer_id'],
                binding_generation=owner['generation'], focus_generation=state['focus_generation'],
                kind='todo_adjustment'):
            return None
        # Done-only boards still need cleanup; a truly empty board does not.
        if not uow.connection.execute('SELECT 1 FROM attention_memberships WHERE agent_id=? LIMIT 1', (agent,)).fetchone():
            return None
        last = self.latest(uow, agent)
        if last and last['state'] in ('waiting', 'submitting'):
            return None  # Native recovery owns an in-flight attempt.
        last_at = (last['settled_at'] or last['created_at']) if last else None
        if not due(now, session_started_at=session['observed_at'],
                last_activity_at=state['last_agent_activity_at'], last_owner_message_at=state['last_owner_message_at'],
                last_nudge_at=last_at):
            return None
        # A new session can retry a definitely-not-typed occurrence without an
        # idempotency conflict with the old session's expected binding.
        return PREFIX + (last['id'] if last else 'first') + ':' + session['id']

    def idle_session(self, profile):
        chat = self.runtime.native_chat
        observed = chat.probe.observe(profile)
        reconciled = chat.registry.reconcile(profile, observed)
        if (not reconciled.live or not reconciled.session or observed.observation is None
                or observed.observation.working is not False):
            return None
        return reconciled.session

    def send_due(self, profile):
        """True when this review takes the current inactivity slot, including a safe deferral."""
        agent = profile['agent_id']
        chat = self.runtime.native_chat
        with chat.arbiter.locks(agent):
            with self.runtime.database.read() as uow:
                if agent != configured_ordubashy(uow):
                    return False
                last = self.latest(uow, agent)
                # Don't follow the daily invitation with an ordinary progress
                # nudge on the very next polling pass.
                if (last and last['state'] in ('submitted', 'uncertain')
                        and instant(self.clock()) < instant(last['settled_at'] or last['created_at']) + IDLE):
                    return True
            session = self.idle_session(profile)
            if session is None:
                return False
            with self.runtime.database.read() as uow:
                key = self.candidate(uow, agent, session, self.clock())
                if key is None:
                    return False
                previous = store.input_by_key(uow.connection, agent, key)
                if previous:
                    # Preserve the exact payload across a known-not-typed retry,
                    # even if authored instructions changed in the meantime.
                    text = previous['body']
                else:
                    instructions = self.runtime.application.policy.agent_resolver.require_agentic_view().runtime_instructions()
                    try:
                        text = instructions.render('todo-adjustment')
                    except KeyError:
                        return False  # Authored instruction adoption is separate; ordinary nudges keep working.

            def checkpoint():
                current = self.idle_session(profile)
                if current is None or current['id'] != session['id']:
                    raise StaleSession()
                with self.runtime.database.read() as uow:
                    if self.candidate(uow, agent, current, self.clock()) != key:
                        raise StaleSession()

            # steer=False keeps the transport idle-only, even if work starts
            # between our observation and the adapter's final composer checks.
            chat.arbiter.submit(InputRequest(agent, TYPE_NUDGE, key, text,
                expected_binding=session['id'], metadata={'todo_adjustment': True}), on_checkpoint=checkpoint)
            return True
