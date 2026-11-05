"""Delivery of due broadcasts: the bounded engine behind the host's dispatcher thread.

One call to ``tick`` performs one durable step, in order: deliver a pending delivery left by an earlier step, else claim the
next due or manually requested occurrence and write its ``pending`` delivery rows with the message frozen in each payload,
then deliver them. Every delivery runs in its own transaction and is authorized again when it is made (a cancelled broadcast,
a sender that lost permission or a recipient that left scope is ``denied``). An ``inbox`` delivery writes the message, its event
and its delivery row and marks itself ``delivered`` in that one transaction. An ``input`` delivery commits ``uncertain``
BEFORE the external write and records the result in a second transaction that only completes a row still ``uncertain``: a
write whose outcome is unknown is never replayed, so only ``pending`` rows are ever selected for delivery.

The engine starts no thread and owns no schedule or queue. The host supplies the application and database it writes through,
the authority and protected-control checks, the terminal input call, the stop signal and the clock; it may build the engine
afresh for every call, because the engine keeps nothing between calls."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.background.schedule import next_slot, zone
from .broadcasts import _stamp


class BroadcastDispatch:
    def __init__(self, application, database, broadcasts, *, require_authority, require_protected_control, input_existing,
                 stopping, clock=lambda: datetime.now(timezone.utc)):
        self.application, self.database, self.broadcasts = application, database, broadcasts
        self.require_authority, self.require_protected_control = require_authority, require_protected_control
        self.input_existing, self.stopping, self.clock = input_existing, stopping, clock

    @staticmethod
    def _next(timing, now):
        if timing["kind"] == "once":
            return None
        if timing["kind"] == "interval":
            anchor = datetime.fromisoformat(timing["at"].replace("Z", "+00:00"))
            period = timing["every_seconds"]
            elapsed = max(0, (now - anchor).total_seconds())
            return _stamp(anchor + timedelta(seconds=(int(elapsed // period) + 1) * period))
        following = next_slot(timing["schedule"], now, timing["timezone"])
        # Preserve existing routines' suppression of the repeated DST wall hour.
        while following.astimezone(zone(timing["timezone"])).fold:
            following = next_slot(timing["schedule"], following, timing["timezone"])
        return _stamp(following)

    def tick(self):
        admission = self.application.writer_admission
        with admission.writer() if admission is not None else nullcontext():
            self.require_authority()
            return self._tick()

    def _tick(self):
        with self.database.read() as uow:
            pending = uow.connection.execute("SELECT broadcast_id,occurrence FROM broadcast_deliveries "
                "WHERE state='pending' ORDER BY created_at,broadcast_id,occurrence LIMIT 1").fetchone()
        if pending:
            self._deliver(pending["broadcast_id"], pending["occurrence"])
            return pending["broadcast_id"]
        now = self.clock()
        with self.database.transaction() as uow:
            self.application.ensure_writes_open(uow)
            found = uow.connection.execute("SELECT * FROM broadcasts WHERE status!='cancelled' AND "
                "(manual_count>0 OR (status='active' AND next_due_at IS NOT NULL AND next_due_at<=?)) "
                "ORDER BY CASE WHEN manual_count>0 THEN 0 ELSE 1 END,next_due_at,id LIMIT 1", (_stamp(now),)).fetchone()
            if not found:
                return None
            row = dict(found)
            manual = row['manual_count'] > 0
            if not manual and row['max_occurrences'] is not None and row['occurrence_count'] >= row['max_occurrences']:
                uow.connection.execute("UPDATE broadcasts SET status='paused',revision=revision+1,updated_at=? WHERE id=?",
                                       (_stamp(now), row['id']))
                return row['id']
            occurrence = uow.connection.execute("SELECT coalesce(max(occurrence),0)+1 FROM broadcast_deliveries "
                                                "WHERE broadcast_id=?", (row['id'],)).fetchone()[0]
            payload = json.dumps({"sender_id": row['sender_id'], "delivery": row['delivery'], "message": row['message'],
                                  "scope_business_id": row['business_id'], "trigger": 'manual' if manual else 'schedule',
                                  "scheduled_at": _stamp(now) if manual else row['next_due_at']}, sort_keys=True)
            for recipient in json.loads(row['recipients_json']):
                uow.connection.execute("INSERT INTO broadcast_deliveries(broadcast_id,occurrence,recipient_id,business_id,"
                    "state,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (row['id'], occurrence, recipient['agent_id'],
                    recipient['business_id'], 'pending', payload, _stamp(now)))
            if manual:
                uow.connection.execute("UPDATE broadcasts SET manual_count=manual_count-1,revision=revision+1,updated_at=? WHERE id=?",
                                       (_stamp(now), row['id']))
            else:
                count = row['occurrence_count'] + 1
                following = self._next(json.loads(row['timing_json']), now)
                finished = following is None or row['max_occurrences'] is not None and count >= row['max_occurrences']
                uow.connection.execute("UPDATE broadcasts SET occurrence_count=?,next_due_at=?,status=?,revision=revision+1,"
                    "updated_at=? WHERE id=?", (count, following, 'paused' if finished else 'active', _stamp(now), row['id']))
        self._deliver(row['id'], occurrence)
        return row['id']

    def _delivery_context(self, payload, delivery, uow):
        app = self.application
        ctx = app.policy.context_for(payload['sender_id'], business_id=payload['scope_business_id'],
                                     review_business_id=None, uow=uow)
        descriptor = app.registry.get('broadcasts_create')
        namespace = 'global' if payload['scope_business_id'] is None else 'business:' + payload['scope_business_id']
        decision = app.authorize(ctx, descriptor, [], uow, namespace=namespace)
        if decision.mode != 'allow':
            raise DomainError('forbidden', 'Sender no longer has permission to deliver this broadcast')
        self.broadcasts._current_sender(ctx, uow)
        self.broadcasts._authorized_recipient(ctx, delivery['recipient_id'], delivery['business_id'], uow)
        if payload['delivery'] == 'input' and not self.broadcasts._input_allowed(ctx, uow):
            raise DomainError('forbidden', 'Sender no longer has Input permission')
        if payload['delivery'] == 'input':
            self.require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [delivery['recipient_id']])
        return ctx

    @staticmethod
    def _finish(uow, delivery, state, now, *, message_id=None, detail=None, prior='pending'):
        uow.connection.execute("UPDATE broadcast_deliveries SET state=?,message_id=?,detail=?,finished_at=? "
            "WHERE broadcast_id=? AND occurrence=? AND recipient_id=? AND state=?", (state, message_id, detail, now,
            delivery['broadcast_id'], delivery['occurrence'], delivery['recipient_id'], prior))

    def _deliver(self, broadcast_id, occurrence):
        with self.database.read() as uow:
            pending = [dict(x) for x in uow.connection.execute("SELECT * FROM broadcast_deliveries WHERE "
                "broadcast_id=? AND occurrence=? AND state='pending' ORDER BY recipient_id", (broadcast_id, occurrence))]
        for delivery in pending:
            if self.stopping():
                return
            payload = json.loads(delivery['payload_json'])
            input_reserved = False
            with self.database.transaction() as uow:
                self.application.ensure_writes_open(uow)
                current = uow.connection.execute("SELECT state FROM broadcast_deliveries WHERE broadcast_id=? "
                    "AND occurrence=? AND recipient_id=?", (broadcast_id, occurrence, delivery['recipient_id'])).fetchone()
                if not current or current['state'] != 'pending':
                    continue
                broadcast = uow.connection.execute("SELECT status FROM broadcasts WHERE id=?", (broadcast_id,)).fetchone()
                if not broadcast or broadcast['status'] == 'cancelled':
                    self._finish(uow, delivery, 'denied', _stamp(self.clock()), detail='Cancelled before delivery')
                    continue
                try:
                    ctx = self._delivery_context(payload, delivery, uow)
                except DomainError as exc:
                    self._finish(uow, delivery, 'denied', _stamp(self.clock()), detail=exc.message)
                    continue
                if payload['delivery'] == 'inbox':
                    self._inbox(payload, delivery, uow)
                else:
                    # Persist the uncertain boundary BEFORE the external write. A restart
                    # must never replay a write whose outcome it cannot establish.
                    self._finish(uow, delivery, 'uncertain', _stamp(self.clock()), detail='Terminal write outcome is not confirmed')
                    input_reserved = True
            if input_reserved:
                try:
                    # A global's native session is its Global one; the delivery's business stays its authorization and history scope.
                    result = self.input_existing(None, delivery['recipient_id'], payload['message'],
                        key=f"broadcast:{delivery['broadcast_id']}:{delivery['occurrence']}:{delivery['recipient_id']}")
                    state = result.get('state', 'uncertain')
                    if state not in {'delivered', 'unavailable', 'uncertain', 'failed'}:
                        state = 'uncertain'
                    detail = result.get('detail')
                except Exception:
                    state, detail = 'uncertain', 'Terminal write outcome is uncertain'
                with self.database.transaction() as uow:
                    self._finish(uow, delivery, state, _stamp(self.clock()), detail=detail, prior='uncertain')

    def _inbox(self, payload, delivery, uow):
        identity, now = new_id(), _stamp(self.clock())
        uow.actor_id = payload['sender_id']
        event = uow.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (delivery['business_id'], 'agent_messages', identity, payload['sender_id'],
            'direct_message', payload['message'][:160], now)).lastrowid
        uow.connection.execute("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,current_sender_id,current_recipient_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, event, delivery['business_id'], payload['sender_id'], delivery['recipient_id'],
            None, identity, payload['message'], now, payload['sender_id'], delivery['recipient_id']))
        uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (delivery['recipient_id'], event))
        self._finish(uow, delivery, 'delivered', now, message_id=identity)
