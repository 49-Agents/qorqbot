from datetime import datetime, timedelta, timezone

from bazarlyk.transport.routine_views import hourly_runs, recent_runs


def test_hourly_completion_boundaries_and_namespace_isolation():
    now = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)
    start = now.replace(minute=0) - timedelta(hours=47)
    def run(end, state='succeeded', namespace='business:a'):
        return {'namespace': namespace, 'routine_id': 'same', 'end': end.isoformat(), 'state': state}
    data = hourly_runs([
        run(start), run(start - timedelta(seconds=1)), run(now),
        run(now, 'failed'), run(now, 'cancelled'), run(now, 'running'),
        run(now + timedelta(seconds=1)), run(now, namespace='business:b'),
        *[run(now) for _ in range(60)],
    ], now=now)
    slots = data['routines']['business:a/same']
    assert len(slots) == 48
    assert slots[0]['succeeded'] == 1
    assert slots[-1]['succeeded'] == 61
    assert slots[-1]['failed'] == 1
    assert sum(x['succeeded'] for x in data['routines']['business:b/same']) == 1


def test_global_fanout_counts_one_invocation_and_waits_for_completion():
    now = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)
    def member(identity, state, group):
        return {'id':identity, 'namespace':'global', 'routine_id':'sync', 'trigger':'manual',
            'state':state, 'started_at':now.isoformat(), 'finished_at':now.isoformat(),
            'result_json':{'run_group_id':group}}
    rows = [member('a','succeeded','one'), member('b','succeeded','one'),
        member('c','failed','two'), member('d','succeeded','two'),
        member('e','running','three'), member('f','succeeded','three')]
    slots = hourly_runs(recent_runs(rows, limit=None), now=now)['routines']['global/sync']
    assert sum(x['succeeded'] for x in slots) == 1
    assert sum(x['failed'] for x in slots) == 1
