"""Focused D57 boundary checks using an isolated deterministic HTTP browser."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import UUID, uuid4
from unittest.mock import patch

from messaging.support import ChatCase
from qorqut_core.app import Application
from qorqut_core.browser.models import Binding
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runtime.models import NativeAttentionView
from qorqut_core.storage.database import assert_external_io_allowed
from test_foundation import InjectedCrash
from .peer import Peer


class Requests(ChatCase):
    def setUp(self):
        super().setUp()
        self.peer = Peer(self.onbash['id'], self.config()['recovery_epoch'])
        self.addCleanup(self.peer.close)
        attached = self.attach()
        self.assertTrue(attached.ok, attached)
        self.surface = attached.data['surface']['surface_id']

    def attach(self, key='attach', **extra):
        return self.app.invoke('browsers_attach', self.context, {
            'agent_id': self.onbash['id'], 'service_url': self.peer.url, 'idempotency_key': key, **extra})

    def request(self, key='request', **extra):
        return self.app.invoke('input_requests_create', self.actor(self.a), {
            'target_agent_id': self.onbash['id'], 'prompt': '  Enter the local form 文\n', 'idempotency_key': key, **extra})

    def listing(self, **extra):
        result = self.app.invoke('input_requests_list', self.context, {'agent_id': self.onbash['id'], **extra})
        self.assertTrue(result.ok, result)
        return result.data

    def finish(self, view, name='complete', key='finish', **extra):
        return self.app.invoke('input_requests_' + name, self.context, {'request_id': view['request_id'],
            'expected_revision': view['revision'], 'idempotency_key': key, **extra})

    def ready_request(self):
        request = self.request()
        self.assertTrue(request.ok, request)
        self.pump()
        view = self.listing()['items'][0]
        self.assertEqual(view['state'], 'ready')
        return view

    def test_exact_facts_one_card_dated_publication_and_terminal_notification(self):
        observed = {'binding': self.peer.control.binding.model_dump(mode='json'), 'tab_id': 'tab-7'}
        original = self.request(observed_tab=observed)
        self.assertTrue(original.ok, original)
        self.assertEqual(original.data['state'], 'requested')
        self.assertTrue(original.data['control_pending'])
        self.assertTrue(self.get(self.onbash['id'])['needs_user_input'])
        waiting = NativeAttentionView(state='waiting', kind='question', prompt='Local fixture?',
            observed_at=self.clock(), owner_id=uuid4(), terminal_id=uuid4())
        for attention in (waiting, NativeAttentionView(state='none', observed_at=self.clock()), NativeAttentionView()):
            with patch.object(self.app.native_attention, 'view', return_value=attention):
                self.assertTrue(self.get(self.onbash['id'])['needs_user_input'])
        with patch.object(self.app.browsers.client, 'observe', side_effect=AssertionError('Receipt replay did I/O')):
            self.assertEqual(self.request(observed_tab=observed), original)
        self.assertEqual(self.request('other').error.code, 'invalid_transition')
        self.pump()
        card = self.conversation(self.onbash)['items'][0]
        self.assertEqual(card['kind'], 'browser_request')
        self.assertEqual(card['source'], {'type': 'browser_request', 'request_id': original.data['request_id']})
        self.assertEqual(card['text'], '  Enter the local form 文\n')
        self.assertEqual(card['browser_request']['state'], 'ready')
        lines = [json.loads(line) for line in self.path(self.onbash).read_bytes().splitlines()]
        self.assertEqual(len(lines), 1)
        self.assertNotIn('browser_request', lines[0])
        self.assertEqual(lines[0]['source'], card['source'])
        source = self.rows('input_requests')[0]
        self.assertEqual(json.loads(source['creation_json']), {'actor_id': self.a['actor_id'],
            'target_agent_id': self.onbash['id'], 'prompt': card['text'], 'idempotency_key': 'request', 'observed_tab': observed})
        complete = self.finish(card['browser_request'], note='Entered a value; inspect it')
        self.assertTrue(complete.ok, complete)
        self.assertTrue(complete.data['control_pending'])
        self.assertEqual(self.finish(card['browser_request'], key='different').data, complete.data)
        self.assertEqual(self.finish(card['browser_request'], 'cancel', 'opposite').error.code, 'invalid_transition')
        self.pump()
        message = self.rows('messages')[0]
        self.assertEqual((message['sender_actor_id'], message['recipient_agent_id']), (str(self.owner.actor_id), self.a['id']))
        for fragment in ('completed', card['text'], original.data['request_id'], 'Entered a value; inspect it'):
            self.assertIn(fragment, message['body'])
        self.assertFalse(self.get(self.onbash['id'])['needs_user_input'])
        with patch.object(self.app.native_attention, 'view', return_value=waiting):
            self.assertTrue(self.get(self.onbash['id'])['needs_user_input'])
        self.assertEqual(len(self.rows('input_requests')), 1)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(sum(e['kind'] == 'browser_request' for e in self.rows('conversation_entries')), 1)
        self.assertEqual(self.peer.control.mode, 'automation')
        self.assertFalse(self.listing()['items'][0]['control_pending'])
        self.restart()
        self.assertEqual(self.request(observed_tab=observed), original)
        self.assertEqual(self.listing()['items'][0]['state'], 'completed')
        self.assertEqual(len(self.rows('messages')), 1)

    def test_done_cancel_race_one_winner_one_notification_and_atomic_rollback(self):
        view = self.ready_request()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda action: self.finish(view, action, action), ('complete', 'cancel')))
        self.assertEqual(sum(r.ok for r in outcomes), 1)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(next(r.error.code for r in outcomes if not r.ok), 'invalid_transition')
        self.pump()
        second = self.request('second')
        self.assertTrue(second.ok, second)
        self.pump()
        view = next(v for v in self.listing()['items'] if v['request_id'] == second.data['request_id'])
        before = {t: self.rows(t) for t in ('input_requests', 'browser_control_intents', 'conversation_entries', 'messages', 'jobs', 'events', 'operations')}
        self.fault.point = 'browser_terminal_after_event'
        with self.assertRaises(InjectedCrash):
            self.finish(view, 'cancel', 'rollback')
        self.assertEqual(before, {t: self.rows(t) for t in before})
        self.assertTrue(self.finish(view, 'cancel', 'rollback').ok)
        self.assertEqual(len(self.rows('messages')), 2)

    def test_pending_take_cancel_and_lost_receipt_survive_restart_with_original_command(self):
        self.peer.pending = True
        created = self.request()
        self.assertTrue(created.ok, created)
        for _ in range(10):
            if self.peer.posts:
                break
            self.app.jobs.run_next(worker_id=uuid4())
        view = self.listing()['items'][0]
        self.assertNotEqual(view['state'], 'ready')
        self.assertTrue(view['control_pending'])
        cancelled = self.finish(view, 'cancel')
        self.assertTrue(cancelled.ok, cancelled)
        original = self.rows('browser_control_intents')[0]
        self.restart()
        self.peer.pending = False
        self.peer.lose_response = True
        self.clock.advance(6)
        # A POST commits in the peer but its response is lost. Core must read
        # the original receipt, never invent a replacement command.
        for _ in range(12):
            self.app.jobs.run_next(worker_id=uuid4())
        self.clock.advance(6)
        self.app.jobs.reconcile_pending()
        self.pump()
        takes = [cmd for cmd, actor in self.peer.posts if cmd.mode == 'human']
        self.assertTrue(all(str(cmd.command_id) == original['command_id'] for cmd in takes))
        self.assertEqual(len(self.rows('browser_control_intents')), 2)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(self.peer.control.mode, 'automation')
        self.assertEqual(self.listing()['items'][0]['state'], 'cancelled')

    def test_response_crash_receipt_recovery_and_service_restart_explicit_resume(self):
        created = self.request()
        self.fault.point = 'browser_control_after_response'
        with self.assertRaises(InjectedCrash):
            for _ in range(10):
                self.app.jobs.run_next(worker_id=uuid4())
        original_posts = len(self.peer.posts)
        self.restart()
        self.clock.advance(31)
        self.app.jobs.reconcile_pending()
        self.pump()
        self.assertEqual(len(self.peer.posts), original_posts)
        ready = self.listing()['items'][0]
        self.assertEqual(ready['state'], 'ready')
        self.peer.restart()
        interrupted = self.listing()['items'][0]
        self.assertEqual(interrupted['state'], 'interrupted')
        resumed = self.finish(interrupted, 'resume', 'resume')
        self.assertTrue(resumed.ok, resumed)
        self.assertEqual(resumed.data['request_id'], created.data['request_id'])
        self.pump()
        self.assertEqual(self.listing()['items'][0]['state'], 'ready')
        self.assertEqual(len(self.rows('input_requests')), 1)
        self.assertEqual(len(self.rows('conversation_entries')), 1)
        intents = self.rows('browser_control_intents')
        self.assertEqual(len(intents), 2)
        self.assertNotEqual(intents[0]['command_id'], intents[1]['command_id'])
        self.assertTrue(all(json.loads(i['payload_json'])['settled_commands'] == [] for i in intents))

    def test_idle_restart_requires_fresh_attach_key_and_never_clears_human_hold(self):
        self.peer.restart()
        self.assertFalse(self.peer.control.ready)
        self.assertTrue(self.attach().ok)
        self.assertEqual(self.rows('browser_control_intents'), [])
        self.assertFalse(self.peer.control.ready)
        reconnect = self.attach('idle-reconnect')
        self.assertTrue(reconnect.ok, reconnect)
        self.assertEqual(self.attach('idle-reconnect'), reconnect)
        self.assertEqual(len(self.rows('browser_control_intents')), 1)
        self.pump()
        self.assertTrue(self.peer.control.ready)
        self.assertEqual(self.peer.control.mode, 'automation')
        self.assertTrue(self.attach('already-ready').ok)
        self.assertEqual(len(self.rows('browser_control_intents')), 1)
        self.peer.control = self.peer.control.model_copy(update={'mode': 'human', 'ready': False})
        self.assertTrue(self.attach('existing-human').ok)
        self.assertEqual(len(self.rows('browser_control_intents')), 1)
        self.assertFalse(self.peer.control.ready)

    def test_validation_cursor_target_and_no_network_inside_sql(self):
        for url in ('http://remote:1', 'http://127.0.0.1', 'https://127.0.0.1:10', self.peer.url + '/', self.peer.url + '?a=b'):
            self.assertEqual(self.attach(url, service_url=url).error.code, 'invalid_argument')
        # D63: an ordinary agent is a local host and may attach; only a migrated legacy vm row is refused.
        with sqlite3.connect(self.settings.data_root / 'state.sqlite3', timeout=10) as db:
            db.execute('DROP TRIGGER agents_permanent_identity')  # kind is otherwise immutable
            db.execute("UPDATE agents SET kind='vm' WHERE id=?", (self.a['id'],))
        self.assertEqual(self.attach('vm', agent_id=self.a['id']).error.code, 'unsupported_capability')
        old = self.peer.identity
        stale = self.peer.control.binding.model_dump(mode='json')
        stale['control_epoch'] += 1
        self.assertEqual(self.request(observed_tab={'binding': stale, 'tab_id': 'old-tab'}).error.code, 'target_changed')
        self.peer.identity = old.model_copy(update={'agent_id': uuid4()})
        self.assertEqual(self.attach('wrong-target').error.code, 'target_changed')
        self.peer.identity = old
        connect = self.app.browsers.client.call
        def checked(*args, **kwargs):
            assert_external_io_allowed()
            self.assertFalse(self.app.db.last_timing and self.app.db.last_timing.writable and not self.app.db.last_timing.committed)
            return connect(*args, **kwargs)
        with patch.object(self.app.browsers.client, 'call', side_effect=checked):
            self.assertTrue(self.attach('same').ok)
            self.assertEqual(len(self.rows('browser_surfaces')), 1)
            self.assertEqual(self.rows('browser_control_intents'), [])
            for index in range(3):
                result = self.request(str(index))
                self.assertTrue(result.ok, result)
                self.pump()
                view = next(v for v in self.listing()['items'] if v['request_id'] == result.data['request_id'])
                self.assertTrue(self.finish(view, key=str(index)).ok)
                self.pump()
        first = self.listing(limit=2)
        self.assertTrue(first['has_more'])
        last = first['items'][-1]
        cursor = base64.urlsafe_b64encode(json.dumps([last['created_at'], last['request_id']]).encode()).decode().rstrip('=')
        rest = self.listing(limit=2, before_created_at=cursor)
        self.assertEqual(len(rest['items']), 1)
        self.assertFalse(rest['has_more'])
        for cursor in ('a', 'e30', 'x' * 513, 'WzEsMl0', 'YWJj='):
            result = self.app.invoke('input_requests_list', self.context, {'agent_id': self.onbash['id'], 'before_created_at': cursor})
            self.assertEqual(result.error.code, 'invalid_argument')
        for prompt in ('', ' \n', 'x' * 4097):
            self.assertEqual(self.request(prompt=prompt).error.code, 'invalid_argument')
        self.peer.online = False
        unavailable = self.app.invoke('browser_get', self.context, {'agent_id': self.onbash['id']})
        self.assertTrue(unavailable.ok, unavailable)
        self.assertEqual(unavailable.data['surface']['status'], 'unavailable')
        self.assertNotIn('service_url', unavailable.model_dump_json())

    def test_explicit_replacement_retires_unconfirmed_old_release_without_rebinding_history(self):
        ready = self.ready_request()
        original_identity = self.peer.identity
        original_attachment = self.rows('browser_attachments')[0]
        self.peer.identity = original_identity.model_copy(update={'browser_instance': uuid4()})
        self.peer.control = self.peer.control.model_copy(update={'binding': Binding(identity=self.peer.identity, control_epoch=0),
                                                               'ready': True, 'mode': 'automation'})
        self.assertEqual(self.attach('active-replacement').error.code, 'invalid_transition')
        self.assertEqual(self.finish(self.listing()['items'][0], 'resume', 'changed').error.code, 'target_changed')
        self.assertTrue(self.finish(ready, 'cancel').ok)
        replaced = self.attach('replace')
        self.assertTrue(replaced.ok, replaced)
        self.assertEqual(replaced.data['surface']['surface_id'], self.surface)
        self.assertEqual(self.rows('browser_attachments')[0], original_attachment)
        old = self.listing()['items'][0]
        self.assertFalse(old['control_pending'])
        self.assertEqual(old['error']['code'], 'retired_attachment')
        self.assertEqual(old['state'], 'cancelled')
        self.assertEqual(len(self.rows('messages')), 1)
        posts = len(self.peer.posts)
        self.pump()
        self.assertEqual(len(self.peer.posts), posts)
        release = self.rows('browser_control_intents')[-1]
        inspected = self.app.invoke('operations_get', self.context, {'operation_id': release['operation_id']})
        self.assertTrue(inspected.ok, inspected)
        retired = next(job for job in inspected.data['jobs'] if job['kind'] == 'browser_control')
        self.assertEqual((retired['state'], retired['progress']['phase']), ('cancelled', 'retired_attachment'))
        self.assertEqual(retired['last_error']['code'], 'uncertain_effect')
        self.assertEqual(self.app.browsers.run_intent(UUID(release['command_id'])).state, 'cancelled')
        self.assertEqual(len(self.peer.posts), posts)
        self.assertTrue(self.request('new-browser').ok)
        self.pump()
        self.assertEqual(sum(v['state'] == 'ready' for v in self.listing()['items']), 1)

    def test_immutable_source_facts_control_payloads_and_recovery_hold(self):
        ready = self.ready_request()
        for table in ('browser_attachments', 'browser_control_intents', 'browser_control_receipts', 'conversation_entries'):
            row = self.rows(table)[0]
            for sql, values in ((f'DELETE FROM {table}', ()),
                    (f'UPDATE {table} SET {next(iter(row))}={next(iter(row))}', ()),
                    (f'INSERT OR REPLACE INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))):
                with self.assertRaises(CoreError), self.app.db.transaction() as tx:
                    tx.connection.execute(sql, values)
        for field in ('prompt', 'requester_actor_id', 'creation_json', 'observed_tab_json'):
            with self.assertRaises(CoreError), self.app.db.transaction() as tx:
                tx.connection.execute(f'UPDATE input_requests SET {field}=?', ('different',))
        self.recovery()
        posts = len(self.peer.posts)
        self.assertEqual(self.listing()['items'][0]['state'], 'interrupted')
        self.assertEqual(self.finish(ready).error.code, 'target_changed')
        self.app.jobs.reconcile_pending()
        self.assertEqual(len(self.peer.posts), posts)


class InitialAttachment(ChatCase):
    def test_initial_reconciliation_then_manual_cancel_notifies_the_target(self):
        peer = Peer(self.onbash['id'], self.config()['recovery_epoch'])
        self.addCleanup(peer.close)
        peer.restart()
        absent = self.app.invoke('browser_get', self.context, {'agent_id': self.onbash['id']})
        self.assertIsNone(absent.data['surface'])
        attached = self.app.invoke('browsers_attach', self.context, {'agent_id': self.onbash['id'],
            'service_url': peer.url, 'idempotency_key': 'initial'})
        self.assertTrue(attached.ok, attached)
        self.assertEqual(attached.data['surface']['reason'], 'control_pending')
        self.assertEqual(len(self.rows('browser_control_intents')), 1)
        self.pump()
        self.assertTrue(peer.control.ready)
        self.assertEqual(peer.posts[0][0].settled_commands, ())
        request = self.app.invoke('input_requests_create', self.context, {'target_agent_id': self.onbash['id'],
            'prompt': 'Manual browser control', 'idempotency_key': 'manual'})
        self.assertTrue(request.ok, request)
        cancelled = self.app.invoke('input_requests_cancel', self.context, {'request_id': request.data['request_id'],
            'expected_revision': request.data['revision'], 'idempotency_key': 'cancel-manual'})
        self.assertTrue(cancelled.ok, cancelled)
        self.pump()
        message = self.rows('messages')[0]
        self.assertEqual(message['recipient_agent_id'], self.onbash['id'])
        self.assertEqual(message['sender_actor_id'], str(self.owner.actor_id))
        self.assertIn('cancelled', message['body'])
        self.assertEqual(peer.control.mode, 'automation')


class LocalAgentPeers(ChatCase):
    """Two ordinary local agents, each with its own SIMULATED HTTP browser peer (not Chrome, VNC or a provider)."""

    def setUp(self):
        super().setUp()
        epoch = self.config()['recovery_epoch']
        self.peers = {agent['id']: Peer(agent['id'], epoch) for agent in (self.a, self.b)}
        for peer in self.peers.values():
            self.addCleanup(peer.close)

    def attach(self, agent, key='attach', peer=None):
        return self.app.invoke('browsers_attach', self.context, {
            'agent_id': agent['id'], 'service_url': (peer or self.peers[agent['id']]).url, 'idempotency_key': key})

    def ask(self, agent, **extra):
        return self.app.invoke('input_requests_create', self.context, {'target_agent_id': agent['id'],
            'prompt': f"Enter the {agent['id']} form\n", 'idempotency_key': 'request-' + agent['id'], **extra})

    def test_agents_get_distinct_surfaces_requests_control_cards_and_messages(self):
        # Nothing is borrowed from onbash or from the other agent: no attachment means no surface and no request.
        for agent in (self.onbash, self.a):
            self.assertIsNone(self.app.invoke('browser_get', self.context, {'agent_id': agent['id']}).data['surface'])
        self.assertFalse(self.ask(self.a).ok)
        # A peer that presents the other agent's identity is refused, leaving no attachment behind.
        self.assertEqual(self.attach(self.a, 'swapped', self.peers[self.b['id']]).error.code, 'target_changed')
        self.assertEqual(self.rows('browser_attachments'), [])
        attached = {agent['id']: self.attach(agent, 'attach-' + agent['id']) for agent in (self.a, self.b)}
        self.assertTrue(all(result.ok for result in attached.values()), attached)
        self.pump()
        surfaces = {agent_id: result.data['surface']['surface_id'] for agent_id, result in attached.items()}
        self.assertEqual(len(set(surfaces.values())), 2)
        urls = {row['attachment_id']: row['service_url'] for row in self.rows('browser_attachments')}
        self.assertEqual({row['agent_id']: urls[row['attachment_id']] for row in self.rows('browser_surfaces')},
                         {agent_id: peer.url for agent_id, peer in self.peers.items()})
        self.assertIsNone(self.app.invoke('browser_get', self.context, {'agent_id': self.onbash['id']}).data['surface'])

        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(self.ask, (self.a, self.b)))
        self.assertTrue(all(result.ok for result in created), created)
        self.pump()
        for agent, result in zip((self.a, self.b), created):
            card = self.conversation(agent)['items'][0]
            self.assertEqual((card['kind'], card['source']['request_id'], card['browser_request']['state']),
                             ('browser_request', result.data['request_id'], 'ready'))
            self.assertEqual(card['text'], f"Enter the {agent['id']} form\n")
            self.assertEqual(len(self.conversation(agent)['items']), 1)
            self.assertEqual(self.conversation(self.onbash)['items'], [])
        # Each peer saw control posts for its own agent only, with its own binding.
        for agent_id, peer in self.peers.items():
            self.assertTrue(peer.posts)
            self.assertEqual({str(command.expected.identity.agent_id) for command, _ in peer.posts}, {agent_id})
        # Independent completion and cancellation reach each agent, not the other or onbash.
        views = {agent['id']: self.conversation(agent)['items'][0]['browser_request'] for agent in (self.a, self.b)}
        done = self.app.invoke('input_requests_complete', self.context, {'request_id': views[self.a['id']]['request_id'],
            'expected_revision': views[self.a['id']]['revision'], 'idempotency_key': 'finish-a', 'note': 'A entered it'})
        cancelled = self.app.invoke('input_requests_cancel', self.context, {'request_id': views[self.b['id']]['request_id'],
            'expected_revision': views[self.b['id']]['revision'], 'idempotency_key': 'cancel-b'})
        self.assertTrue(done.ok and cancelled.ok, (done, cancelled))
        self.pump()
        messages = {row['recipient_agent_id']: row['body'] for row in self.rows('messages')}
        self.assertEqual(set(messages), {self.a['id'], self.b['id']})
        self.assertIn('completed', messages[self.a['id']])
        self.assertIn('A entered it', messages[self.a['id']])
        self.assertIn('cancelled', messages[self.b['id']])
        self.assertNotIn('A entered it', messages[self.b['id']])
        for agent_id, peer in self.peers.items():
            self.assertEqual(peer.control.mode, 'automation')
        # Saved cards publish to each agent's own dated transcript.
        for agent in (self.a, self.b):
            lines = [json.loads(line) for line in self.path(agent).read_bytes().splitlines()]
            self.assertEqual([line['source']['request_id'] for line in lines if line['kind'] == 'browser_request'],
                             [views[agent['id']]['request_id']])
        self.assertFalse(self.path(self.onbash).exists())

