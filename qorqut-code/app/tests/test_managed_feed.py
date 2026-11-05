import base64

from qorqut_agentic.store import Store
from bazarlyk.agentic import managed_feed, managed_git, managed_intent
from bazarlyk.agentic.managed_plan import prepare
from test_agentic_managed_plan import installed, refresh_export

ATTR = {'actor_id': 'synthetic-admin', 'actor_kind': 'human', 'reason': 'Offline managed update'}


def test_exact_managed_feed_reconciliation(installed):
    paths, source = installed
    guidance = source / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/overseer/overseer/instructions/review.md'
    guidance.write_bytes(guidance.read_bytes() + b'\nSynthetic supplier change.\n')
    refresh_export(source)
    plan = prepare(paths, source)
    intent = managed_intent.capture(paths, plan, attribution=ATTR)
    git = managed_git.prepare(paths, intent, paths.instance_root / 'config')
    for change in plan['changes']:
        target = paths.agentic_root / change['path']
        if change['op'] == 'delete':
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(change['content_base64']))
    event = managed_feed.apply(paths, intent, git['commit'])
    assert event == managed_feed.expected(intent, git['commit'])
    assert managed_feed.apply(paths, intent, git['commit']) == event
    with Store(paths.agentic_runtime).reader() as conn:
        assert conn.execute('SELECT count(*) FROM changes WHERE change_id=?', (event['change_id'],)).fetchone()[0] == 1
        assert managed_feed.verify_event(conn, intent, git['commit']) == event
