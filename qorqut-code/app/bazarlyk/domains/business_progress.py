"""Read-only counts from business-owned evidence, never estimates of missing history."""
from dataclasses import replace
from datetime import timedelta

from qorqut_operations import local_time
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.application.plans import schema, utc_clock
from .email.attribution import outgoing
from .email.purposes import NO_PURPOSES

KEYS = ("emails_sent", "tasks_done", "actions", "signoffs_approved")


def day(value):
    """San Juan calendar day of a stored UTC instant; naive or invalid values stay undated."""
    return local_time.local_date(value) if isinstance(value, str) else None


class BusinessProgress:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def register(self, registry):
        from .dashboard_summary import register
        register(registry, self.clock)
        registry.register(OperationDescriptor("business_progress_get", schema({"days": {"enum": [30, 90, 365]}}),
            effect_kind="read", supports_signoff=False,
            description="Owner read: all-time recorded totals and daily San Juan (America/Puerto_Rico) series for captured outgoing emails, distinct tasks first marked done, completed actions linked to this project (on their occurrence day) and approved signoffs. Missing dates are explicit, never guessed.",
            read=self.read))

        registry.register(OperationDescriptor('global_progress_get', schema({'days': {'enum': [30,120,365]}}),
            scope_kinds=frozenset({'global'}), global_capability='read_global_context', effect_kind='read',
            supports_signoff=False, description='Owner daily San Juan (America/Puerto_Rico) counts across authorized workspaces: task completions, captured sent emails, every inbox delivery, each learning first accepted and each distinct completed action once on its occurrence day.', read=self.read_global))

    def read_global(self, ctx, args, opts, uow):
        installation = uow.repository('installation').get('service')
        if ctx.principal_kind != 'human' or not installation or installation['owner_principal_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Global progress requires the owner')
        businesses = [r[0] for r in uow.connection.execute(
            'SELECT b.id FROM businesses b JOIN business_memberships m ON m.business_id=b.id '
            'WHERE m.principal_id=? AND m.active=1 AND m.is_owner=1 AND b.archived_at IS NULL', (ctx.principal_id,))]
        end = local_time.today(self.clock)
        keys = ('tasks_done', 'emails_sent', 'inbox_messages', 'learnings_accepted', 'new_tickets', 'actions')
        series = {(end-timedelta(days=i)).isoformat(): dict.fromkeys(keys, 0) for i in reversed(range(args.get('days',30)))}
        for business in businesses:
            data = self.read(replace(ctx, business_id=business), {'days': args.get('days',30)}, opts, uow)
            for row in data['series']:
                if row['date'] in series:
                    for key in ('tasks_done','emails_sent'):
                        series[row['date']][key] += row[key]
        def count(key, value):
            date = day(value)
            if date in series: series[date][key] += 1
        for row in uow.connection.execute('SELECT completed_at FROM task_completions WHERE business_id IS NULL'):
            count('tasks_done', row[0])
        scopes = set(businesses) | {None}
        # Count each recipient delivery, including system events and acknowledged messages.
        # Private owner conversations keep their own store; their deliveries are
        # never counted as business inbox traffic in any projection.
        for row in uow.connection.execute('SELECT a.home_business_id,e.business_id,e.created_at FROM inbox_deliveries d '
                'JOIN agent_profiles a ON a.agent_id=d.agent_id JOIN inbox_events e ON e.id=d.event_id '
                "WHERE e.source_type<>'owner_conversations'"):
            if row[0] in scopes and row[1] in scopes: count('inbox_messages', row[2])
        for row in uow.connection.execute("SELECT business_id,candidate_id,MIN(created_at) FROM learning_history "
                "WHERE json_extract(after_json,'$.state')='accepted' GROUP BY business_id,candidate_id"):
            if row[0] in scopes: count('learnings_accepted', row[2])
        # Each row is one ticket. Count its creation day regardless of kind or
        # current status; follow-up and resolution events are separate history.
        for row in uow.connection.execute('SELECT business_id,created_at FROM dev_tickets'):
            if row[0] in scopes: count('new_tickets', row[1])
        # Each canonical action once, on its San Juan occurrence day; never a sum of business counts.
        for row in uow.connection.execute('SELECT occurred_date FROM action_logs WHERE occurred_date>=?', (min(series),)):
            if row[0] in series: series[row[0]]['actions'] += 1
        return {'series':[{'date':date,**values} for date,values in series.items()], 'timezone':local_time.TIMEZONE, 'days':len(series)}

    def read(self, ctx, args, opts, uow):
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
        if ctx.principal_kind != "human" or not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "Project progress requires its owner")
        end = local_time.today(self.clock)
        days = args.get("days", 30)
        series = {(end-timedelta(days=i)).isoformat(): {key: 0 for key in KEYS} for i in reversed(range(days))}
        totals, undated = dict.fromkeys(KEYS, 0), dict.fromkeys(KEYS, 0)

        def count(key, value, n=1):
            totals[key] += n
            date = day(value)
            if not date: undated[key] += n
            elif date in series: series[date][key] += n

        # Mailbox/provider identity is the fallback only when an RFC message ID
        # is missing. Sent/BCC copies sharing that ID count once per business.
        seen = set()
        for raw in uow.connection.execute("SELECT * FROM email_messages WHERE business_id=? ORDER BY created_at,id", (ctx.business_id,)):
            message = decode_row(raw)
            if not outgoing(message, uow, getattr(uow.database, "mailbox_purposes", NO_PURPOSES)): continue
            data = message["message_json"]
            identity = (data.get("internet_message_id") or "").strip() or message["mailbox"]+":"+message["provider_id"]
            if identity in seen: continue
            seen.add(identity)
            count("emails_sent", data.get("sent_at"))
        for row in uow.connection.execute("SELECT completed_at FROM task_completions WHERE business_id=?", (ctx.business_id,)):
            count("tasks_done", row[0])
        # Actions linked to this business wherever stored, on their San Juan occurrence day (backfills included).
        for row in uow.connection.execute("SELECT a.occurred_date FROM action_logs a JOIN action_businesses m "
                                          "ON m.action_key=a.action_key WHERE m.business_id=?", (ctx.business_id,)):
            totals["actions"] += 1
            if not row[0]: undated["actions"] += 1
            elif row[0] in series: series[row[0]]["actions"] += 1
        for row in uow.connection.execute("SELECT json_extract(decision_json,'$.at') FROM signoffs WHERE review_business_id=? AND json_extract(decision_json,'$.kind')='approve'", (ctx.business_id,)):
            count("signoffs_approved", row[0])
        return {"totals": totals, "undated": undated, "series": [{"date": date, **values} for date, values in series.items()],
                "timezone": local_time.TIMEZONE, "days": days,
                "coverage": "Recorded history only. Emails include captured outgoing replies and follow-ups; unsynced mail is excluded. Tasks count once on first entering done, excluding dropped tasks. Older completion dates may be unknown."}
