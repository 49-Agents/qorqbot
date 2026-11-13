"""A read-and-acknowledge request: posted by the coordinator, acknowledged by the owner, never a decision or a blocker."""
from dataclasses import replace
import unittest

from qorqut_operations.contracts import Principal, new_id, plain

import test_side_chat_rename as chats
import test_signoff_sources as fixture
from qorqut.attention_queue import queue

REPORT = {'title': 'Daily report, 12 Nov', 'kind': 'acknowledge', 'selection_mode': 'none',
          'explanation': '## Forgotten\n\nNothing.\n\n## Done\n\n- Linden contract sent ([contract](/files/view?path=linden.pdf))'}
MORNING = '2026-11-12T15:00:00.000000Z'    # 10:00 in New York, after the daily summary hour
NEXT_DAY = '2026-11-13T15:00:00.000000Z'


class AcknowledgeTests(unittest.TestCase):
    setUp = fixture.SignoffSourceTests.setUp
    invoke = fixture.SignoffSourceTests.invoke
    task = fixture.SignoffSourceTests.task

    def agent(self, role):
        identity = new_id()
        profile = {'status': 'active', 'role': {'id': role},
                   'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': role})
            uow.repository('agent_profiles', business_id=None).insert({'agent_id': identity, 'principal_id': identity,
                'namespace': 'ecosystem', 'name': role, 'workspace': role, 'profile_json': profile})
        return replace(self.ctx, principal=Principal(identity, 'agent'), source='mcp', profile_revision=1)

    def coordinator(self):
        ctx = self.agent('ordubashy')
        self.policy.context_for = lambda principal_id, business_id=None, review_business_id=None, **_: replace(
            ctx if principal_id == ctx.principal_id else self.ctx, business_id=business_id, review_business_id=review_business_id)
        return ctx

    def post(self, ctx, key='daily-report', form=None, **extra):
        return self.app.invoke(ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': key,
            'form': {**REPORT, **(form or {})}, **extra}, {'idempotency_key': new_id()})

    def posted(self, ctx, **arguments):
        result = self.post(ctx, **arguments)
        self.assertEqual(result.status, 'ok', result.error)
        return plain(result.data)['signoff']

    def refused(self, result, code):
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.error['code'], code, result.error)

    def respond(self, signoff, **response):
        return self.signoffs.respond(self.ctx, signoff['id'], proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'],
            idempotency_key=new_id(), **response)

    def state(self, identity):
        with self.db.read() as uow:
            return uow.repository('signoffs', business_id=None).get(identity)['state']

    def test_only_the_coordinator_posts_one(self):
        signoff = self.posted(self.coordinator())
        self.assertEqual((signoff['category'], signoff['input_kind'], signoff['state']), ('input_request', 'acknowledge', 'pending'))
        # It is the owner's alone and announces itself without being asked to.
        self.assertEqual(signoff['input']['response_policy'], 'owner_only')
        self.assertEqual((signoff['attention']['notify'], signoff['attention']['urgent'], signoff['attention']['blocking']), (1, 0, 0))
        self.refused(self.post(self.agent('global-worker')), 'forbidden')
        self.refused(self.post(self.ctx), 'forbidden')

    def test_it_asks_nothing_and_blocks_nothing(self):
        ctx = self.coordinator()
        choices = [{'id': 'a', 'label': 'Yes'}, {'id': 'b', 'label': 'No'}]
        self.refused(self.post(ctx, form={'selection_mode': 'single', 'choices': choices}), 'validation_error')
        self.refused(self.post(ctx, form={'notes_required': True}), 'validation_error')
        self.refused(self.post(ctx, source=self.source), 'validation_error')
        for flag in ('urgent', 'blocking'):
            self.refused(self.post(ctx, attention={flag: True, 'reason': 'Read it now'}), 'validation_error')
        signoff = self.posted(ctx)
        linked = self.app.invoke(ctx, 'signoffs_source_link', {'scope': 'global', 'source': self.source,
            'signoff_id': signoff['id']}, {'idempotency_key': new_id()})
        self.refused(linked, 'validation_error')
        raised = self.app.invoke(ctx, 'signoffs_attention_set', {'scope': 'global', 'signoff_id': signoff['id'],
            'attention': {'notify': True, 'urgent': True, 'reason': 'Read it now'}}, {'idempotency_key': new_id()})
        self.refused(raised, 'validation_error')
        self.assertEqual(self.task()['status'], 'working')
        self.assertFalse(self.task()['input_needed'])

    def test_the_owner_acknowledges_without_an_answer(self):
        ctx = self.coordinator()
        signoff = self.posted(ctx)
        self.refused(self.respond(signoff, notes='Looks fine'), 'validation_error')
        self.assertEqual(self.state(signoff['id']), 'pending')
        result = self.respond(signoff)
        self.assertEqual(result.status, 'ok', result.error)
        acknowledged = plain(result.data)['signoff']
        self.assertEqual((acknowledged['state'], acknowledged['decision_state'], acknowledged['actionable']),
                         ('answered', 'acknowledged', False))
        self.assertEqual(acknowledged['decision']['by'], self.owner)
        self.assertIsNone(acknowledged['decision']['message_id'])
        with self.db.read() as uow:
            delivered = uow.connection.execute('SELECT e.event_type FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id '
                "WHERE d.agent_id=? AND e.source_type='signoffs' AND e.source_id=?", (ctx.principal_id, signoff['id'])).fetchall()
        self.assertEqual(sum('response_submitted' in row[0] for row in delivered), 1)

    def test_a_new_report_replaces_the_unacknowledged_one(self):
        ctx = self.coordinator()
        first = self.posted(ctx)
        # The same report again is the same request.
        self.assertEqual(self.posted(ctx)['id'], first['id'])
        second = self.posted(ctx, form={'title': 'Daily report, 13 Nov'})
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual((self.state(first['id']), self.state(second['id'])), ('cancelled', 'pending'))
        self.assertEqual(second['preview']['replaces_id'], first['id'])
        # An acknowledged report is left alone: the next one is simply new.
        self.assertEqual(self.respond(second).status, 'ok')
        third = self.posted(ctx, form={'title': 'Daily report, 14 Nov'})
        self.assertEqual((self.state(second['id']), self.state(third['id'])), ('answered', 'pending'))
        self.assertNotIn('replaces_id', third['preview'])
        # A question under a key is still never replaced silently.
        question = {'title': 'Choose a date', 'kind': 'question', 'explanation': 'Which date?', 'selection_mode': 'none'}
        self.assertEqual(self.post(ctx, key='date', form=question).status, 'ok')
        self.refused(self.post(ctx, key='date', form={**question, 'title': 'Choose another date'}), 'request_conflict')
        self.refused(self.post(ctx, key='date'), 'request_conflict')

    def test_it_is_announced_once_and_never_in_the_daily_summary(self):
        self.signoffs.clock = lambda: MORNING
        signoff = self.posted(self.coordinator())
        self.signoffs.notifications.tick()
        delivered = self.signoffs.notifications.deliveries(self.ctx)['items']
        self.assertEqual([(item['kind'], [row['id'] for row in item['items']]) for item in delivered],
                         [('individual', [signoff['id']])])
        for moment in (MORNING, NEXT_DAY):
            self.signoffs.clock = lambda moment=moment: moment
            self.signoffs.notifications.tick()
        with self.db.read() as uow:
            kinds = [row[0] for row in uow.connection.execute('SELECT kind FROM signoff_notifications')]
            pending = uow.connection.execute('SELECT next_notify_at FROM signoff_attention WHERE signoff_id=?',
                                             (signoff['id'],)).fetchone()[0]
        self.assertEqual(kinds, ['individual'])
        self.assertIsNone(pending)

    def test_it_is_counted_as_something_to_read_not_a_decision(self):
        ctx = self.coordinator()
        self.posted(ctx)
        question = {'title': 'Choose a date', 'kind': 'question', 'explanation': 'Which date?', 'selection_mode': 'none'}
        self.assertEqual(self.post(ctx, key='date', form=question).status, 'ok')
        overview = self.signoffs.notifications.overview(self.ctx)
        counts = {row['name']: row for row in overview['businesses']}['Global']
        self.assertEqual((counts['signoffs'], counts['to_read'], counts['total'], counts['urgent']), (1, 1, 1 + counts['tasks'], 0))
        listing = self.invoke('signoffs_list', {'states': ['pending']})
        self.assertEqual((listing['counts']['pending'], listing['to_read']), (2, 1))
        self.assertEqual(sorted(row['input_kind'] for row in listing['items']), ['acknowledge', 'question'])


class AcknowledgeCommentTests(unittest.TestCase):
    """The comment box is the Signoff's private conversation, on the real private-message path."""

    def setUp(self):
        directory = chats.tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = chats.build_runtime(chats.Path(directory.name))
        self.addCleanup(self.runtime.database.close)

    def test_commenting_and_acknowledging_are_independent(self):
        runtime = self.runtime
        signoff = chats.invoke(runtime, 'signoffs_input_create', {'request_key': 'daily-report', 'form': REPORT},
                               ctx=runtime.coordinator)['signoff']
        source = chats.source(signoff)
        say = lambda text, ctx=None: chats.invoke(runtime, 'owner_conversation_send', {'source': source, 'text': text}, ctx=ctx)
        say('Why was the invoice late?')
        # A comment acknowledges nothing, and the item is in the owner's queue as something to read.
        item = next(row for row in queue(runtime, runtime.owner)['items'] if row.get('signoff_id') == signoff['id'])
        self.assertTrue(item['to_read'])
        current = chats.invoke(runtime, 'signoffs_get', {'signoff_id': signoff['id']})['signoff']
        self.assertEqual(current['state'], 'pending')
        result = runtime.signoffs.respond(runtime.owner, signoff['id'], proposal_revision=current['proposal_revision'],
            payload_hash=current['payload_hash'], expected_status_revision=current['status_revision'], idempotency_key=new_id())
        self.assertEqual(result.status, 'ok', result.error)
        # The conversation stays open after the acknowledgement, in both directions.
        say('The supplier sent it on the 11th.', runtime.coordinator)
        say('Fine, thanks.')
        messages = chats.invoke(runtime, 'owner_conversation_get', {'source': source}, ctx=runtime.coordinator)['messages']
        self.assertEqual([message['body'] for message in messages],
                         ['Why was the invoice late?', 'The supplier sent it on the 11th.', 'Fine, thanks.'])
        self.assertFalse(any(row.get('signoff_id') == signoff['id'] for row in queue(runtime, runtime.owner)['items']))


if __name__ == '__main__':
    unittest.main()
