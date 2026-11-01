"""Small owner-only dashboard projections; never load task bodies or routine history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from bazarlyk import local_time
from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError
from .common import schema, timestamp
from .tasks.operations import TaskOperations
from .tasks.snooze import ACTIVE_SQL


def register(registry, clock):
    def read(ctx, args, opts, uow):
        installation = uow.repository('installation').get('service')
        if ctx.principal_kind != 'human' or not installation or installation['owner_principal_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Dashboard summary requires the installation owner')
        now = clock()
        if isinstance(now, str): now = datetime.fromisoformat(now.replace('Z', '+00:00'))
        # Operational days are San Juan calendar days (bazarlyk.local_time).
        dates = [(local_time.today(lambda: now)-timedelta(days=i)).isoformat() for i in reversed(range(14))]
        businesses = uow.connection.execute('SELECT b.id,b.slug FROM businesses b JOIN business_memberships m ON m.business_id=b.id '
            'WHERE m.principal_id=? AND m.active=1 AND m.is_owner=1 AND b.archived_at IS NULL ORDER BY b.slug', (ctx.principal_id,)).fetchall()
        items = []
        for business in businesses:
            bid = business['id']
            config, _ = TaskOperations.configuration(replace(ctx, business_id=bid), uow)
            open_states = [s['name'] for s in config['states'] if s['kind'] != 'closed']
            count = 0
            if open_states:
                count = uow.connection.execute('SELECT COUNT(*) FROM tasks t WHERE t.business_id=? AND t.archived=0 AND '
                    + ACTIVE_SQL + ' AND t.status IN ('+','.join('?' for _ in open_states)+') AND EXISTS '
                    '(SELECT 1 FROM relationships r WHERE r.business_id=t.business_id AND r.from_type=\'task\' AND r.from_id=t.id '
                    'AND r.role=\'assigned_to\' AND r.to_id=?)', (bid, timestamp(lambda: now), *open_states, ctx.principal_id)).fetchone()[0]
            signoffs = uow.connection.execute("SELECT COUNT(*),COALESCE(SUM(category='platform_action'),0) FROM signoffs "
                "WHERE review_business_id=? AND state='pending' AND superseded_by_id IS NULL", (bid,)).fetchone()
            # A shared action counts once in each linked business, on its San Juan occurrence day.
            activity = dict(uow.connection.execute("SELECT a.occurred_date,COUNT(DISTINCT a.action_key) FROM action_logs a "
                "JOIN action_businesses m ON m.action_key=a.action_key WHERE m.business_id=? AND a.occurred_date>=? "
                "AND a.occurred_date<=? AND a.occurred_at<=? GROUP BY a.occurred_date", (bid, dates[0], dates[-1], timestamp(lambda: now))).fetchall())
            items.append({'business_id':bid,'slug':business['slug'],'tasks':count,'signoffs':signoffs[0],
                'requests':signoffs[1],'activity':[{'date':date,'count':activity.get(date,0)} for date in dates]})
        return {'items':items,'as_of':timestamp(lambda: now),'timezone':local_time.TIMEZONE}
    registry.register(OperationDescriptor('dashboard_summary_get', schema({}), scope_kinds=frozenset({'global'}),
        global_capability='read_global_context', effect_kind='read', supports_signoff=False,
        exposure=frozenset({'dashboard'}), description='Owner business counts and fourteen daily San Juan (America/Puerto_Rico) activity totals.', read=read))
