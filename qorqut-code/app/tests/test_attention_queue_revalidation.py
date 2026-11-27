"""The owner's attention queue and reply notifications: access is checked once per read, and a poll that finds nothing changed
is answered with 304 or with the reply build the other route just made, never with a queue that may have changed."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import InvocationOptions, new_id, plain

from qorqut.attention_queue import queue
from qorqut.reply_notifications import replies
import qorqut.reply_notifications as reply_notifications

from private_thread_runtime import build_runtime, create, invoke, reply, report_task

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

STAMP = '2026-11-20T09:00:00.000000Z'


def thread(runtime, source_type, source_id, project_id=None, text='Ordubashy answered'):
    """A stored Ordubashy channel whose newest message is the coordinator's, unread by the owner."""
    conversation = new_id()
    key = ('reference:' + source_id if source_type == 'reference'
           else 'source:' + (project_id or 'global') + ':' + source_type + ':' + source_id)
    with runtime.database.transaction() as uow:
        uow.connection.execute('INSERT INTO owner_conversations(id,source_key,project_id,source_type,source_id,audience,revision,'
            'latest_sequence,created_at,updated_at,peer_agent_id) VALUES(?,?,?,?,?,?,1,0,?,?,NULL)',
            (conversation, key, project_id, source_type, source_id, 'owner_ordubashy', STAMP, STAMP))
        uow.connection.execute('INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at,'
            'refs_json) VALUES(?,?,?,?,?,?,?)', (new_id(), conversation, runtime.coordinator.principal_id, 'coordinator', text,
            STAMP, '[]'))
    return conversation


def project(runtime, slug, archived=False):
    identity = new_id()
    with runtime.database.transaction() as uow:
        uow.connection.execute('INSERT INTO projects(id,slug,workspace,name,created_at,updated_at,archived_at) VALUES(?,?,?,?,?,?,?)',
                               (identity, slug, '/w/' + slug, slug, STAMP, STAMP, STAMP if archived else None))
    return identity


class RepliesAccessTests(unittest.TestCase):
    """Each candidate thread gets exactly the yes or no a fresh check of its own source gives it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = build_runtime(Path(directory.name))
        self.addCleanup(self.runtime.database.close)

    def test_refused_and_unavailable_threads_stay_out_and_readable_ones_stay_in(self):
        runtime = self.runtime
        readable = [create(runtime, title=f'Readable {n}') for n in range(3)]
        for signoff in readable:
            reply(runtime, signoff)
        task = report_task(runtime)
        thread(runtime, 'task', task['id'])
        archived = project(runtime, 'old', archived=True)
        # Two refusals in one archived project (the project selection refuses), a Signoff that does not exist, a Focus
        # Reference with no attention service installed, and a project source that names another project as its scope.
        refused = [thread(runtime, 'signoff', new_id(), archived), thread(runtime, 'signoff', new_id(), archived),
                   thread(runtime, 'signoff', new_id()), thread(runtime, 'reference', new_id()),
                   thread(runtime, 'project', new_id(), project(runtime, 'other'))]
        listed = replies(runtime, runtime.owner)['items']
        threads = {item['source']['id'] for item in listed if item['kind'] == 'thread'}
        self.assertEqual(threads, {signoff['id'] for signoff in readable} | {task['id']})
        # The same answer, candidate by candidate, as a check with nothing remembered between candidates.
        service = runtime.application.private_conversations
        with runtime.database.read() as uow:
            rows = uow.connection.execute('SELECT * FROM owner_conversations').fetchall()
            fresh = set()
            for row in rows:
                source = {'type': row['source_type'], 'id': row['source_id'], 'project_id': row['project_id']}
                try:
                    if service.channel_available(service.select(runtime.owner, row['project_id'], uow), source, uow):
                        fresh.add(row['source_id'])
                except Exception:
                    pass
            stored = {row['id']: row['source_id'] for row in rows}
        self.assertEqual(threads, fresh)
        self.assertFalse({stored[identity] for identity in refused} & threads)

    def test_one_memo_answers_a_refusal_again_and_keeps_scopes_apart(self):
        runtime, service = self.runtime, self.runtime.application.private_conversations
        signoff = create(runtime)
        good = {'type': 'signoff', 'id': signoff['id'], 'project_id': None}
        missing = {'type': 'signoff', 'id': new_id(), 'project_id': None}
        archived = {'type': 'signoff', 'id': signoff['id'], 'project_id': project(runtime, 'old', archived=True)}
        with runtime.database.read() as uow:
            selected, memo = service.select(runtime.owner, None, uow), {}
            # From Global each source names its own project: a refused project selection is not reused for another one.
            answers = [service.channel_available(selected, source, uow, memo)
                       for source in (archived, missing, good, archived, missing, good)]
            # A refused caller stays refused for every source, the remembered refusal included.
            stranger = runtime.worker
            self.assertEqual([service.channel_available(stranger, good, uow, memo) for _ in range(2)], [False, False])
        self.assertEqual(answers, [False, False, True, False, False, True])

    def test_one_memo_keeps_each_selected_scope_apart(self):
        """A Signoff read is authorized once per selected scope; a project's scope never answers for Global or back."""
        runtime, service = self.runtime, self.runtime.application.private_conversations
        live = project(runtime, 'live')
        owner = replace(runtime.owner, allowed_project_ids=frozenset({live}))
        created = runtime.application.invoke(owner.select_project(live), 'signoffs_input_create', {'scope': 'project',
            'request_key': new_id(), 'form': {'title': 'In a project', 'explanation': 'Here', 'kind': 'question',
            'selection_mode': 'none'}}, InvocationOptions(idempotency_key=new_id()))
        self.assertEqual(created.status, 'ok', plain(created.error))
        in_project = {'type': 'signoff', 'id': plain(created.data)['signoff']['id'], 'project_id': live}
        in_global = {'type': 'signoff', 'id': create(runtime)['id'], 'project_id': None}
        crossed = [{**in_project, 'project_id': None}, {**in_global, 'project_id': live}]
        with runtime.database.read() as uow:
            selected, memo = service.select(owner, None, uow), {}
            answers = [service.channel_available(selected, source, uow, memo)
                       for source in (in_project, in_global, *crossed, in_global, in_project)]
        self.assertEqual(answers, [True, True, False, False, True, True])


@unittest.skipUnless(SERVICE, 'Starlette is not installed')
class RevalidationTests(unittest.TestCase):
    """``GET /api/root/attention-queue`` and ``/api/root/reply-notifications`` through the real ``HTTPAPI`` dispatch."""

    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.changes = runtime.database.assembly.change_publisher
        runtime.require_authority = lambda: None
        self.signoff = create(runtime)
        reply(runtime, self.signoff)
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = runtime
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=runtime.owner.principal_id, display_name='Owner'),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        api.context = lambda actor, slug=None, owner=False: (runtime.owner, None)
        self.builds = 0
        original = reply_notifications._items

        def counted(*args):
            self.builds += 1
            return original(*args)
        patcher = patch.object(reply_notifications, '_items', counted)
        patcher.start()
        self.addCleanup(patcher.stop)

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route('/api/{path:path}', handler, methods=['GET', 'POST'])]))
        self.addCleanup(self.client.close)

    def get(self, path='/api/root/attention-queue', tag=None):
        return self.client.get(path, headers={'If-None-Match': tag} if tag else {})

    def assert_unchanged(self, tag, path='/api/root/attention-queue'):
        response = self.get(path, tag)
        self.assertEqual((response.status_code, response.headers['etag']), (304, tag))
        return response

    def assert_fresh(self, tag, path='/api/root/attention-queue'):
        response = self.get(path, tag)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers['etag'], tag)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        return response.headers['etag'], response.json()['data']

    def first(self, path='/api/root/attention-queue'):
        response = self.get(path)
        self.assertEqual(response.status_code, 200)
        return response.headers['etag'], response.json()['data']

    def test_an_unchanged_queue_answers_304_without_building_it(self):
        tag, data = self.first()
        self.assertEqual({item['kind'] for item in data['items']}, {'thread', 'signoff'})
        builds = self.builds
        self.assert_unchanged(tag)
        self.assert_unchanged(tag)
        self.assertEqual(self.builds, builds)

    def test_every_kind_of_write_the_queue_reads_answers_fresh(self):
        runtime = self.runtime
        coordinator = runtime.coordinator.principal_id
        tag, _ = self.first()
        source = {'type': 'signoff', 'id': self.signoff['id'], 'project_id': None}

        def placement():
            with runtime.database.transaction() as uow:
                uow.connection.execute('INSERT INTO agent_viewbox_items(id,agent_id,canonical_link,link,title,created_at,placed_at,'
                    'placement_sequence) VALUES(?,?,?,?,?,?,?,1)', (new_id(), coordinator, 'https://e/1', 'https://e/1', 'Board item',
                    STAMP, STAMP))

        def presentation():
            with runtime.database.transaction() as uow:
                uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.icon','star') WHERE agent_id=?",
                                       (coordinator,))

        def read_boundary():
            # A table no change-feed kind names: the owner's read position on a thread.
            latest = invoke(runtime, 'owner_conversation_get', {'source': source})['conversation']['latest_sequence']
            invoke(runtime, 'owner_conversation_mark_read', {'source': source, 'sequence': latest})

        def installation():
            with runtime.database.transaction() as uow:
                uow.connection.execute("UPDATE installation SET revision=revision+1 WHERE id='service'")

        def another_process():
            connection = sqlite3.connect(runtime.database.path)
            try:
                connection.execute("UPDATE installation SET revision=revision+1 WHERE id='service'")
                connection.commit()
            finally:
                connection.close()
            runtime.changes.publish_wildcard()   # what the external-writer ticker does on its next second

        writes = {'a new coordinator reply': lambda: reply(runtime, self.signoff, 'Another answer'), 'a read boundary': read_boundary,
                  'a new Signoff': lambda: create(runtime, title='Second'), 'a board placement': placement,
                  'an agent profile': presentation, 'an installation row': installation, 'another process': another_process}
        for name, write in writes.items():
            with self.subTest(write=name):
                write()
                tag, _ = self.assert_fresh(tag)
                self.assert_unchanged(tag)

    def test_viewing_and_discarding_answer_fresh(self):
        tag, data = self.first()
        signoff = next(item for item in data['items'] if item['kind'] == 'signoff')
        posted = self.client.post('/api/root/attention-queue', json={'viewed': signoff['viewed_ids']})
        self.assertEqual(posted.status_code, 200)
        self.assertNotIn('etag', posted.headers)
        tag, data = self.assert_fresh(tag)
        self.assertNotIn(signoff['id'], {item['id'] for item in data['items']})
        self.assert_unchanged(tag)
        discard = f'calendar:{date.today().isoformat()}:["conn","cal","event"]'
        self.assertEqual(self.client.post('/api/root/attention-queue', json={'discarded': [discard]}).status_code, 200)
        tag, data = self.assert_fresh(tag)
        self.assertEqual(data['discarded'], [discard])
        self.assertEqual(self.client.post('/api/root/attention-queue', json={'unviewed': signoff['viewed_ids']}).status_code, 200)
        _, data = self.assert_fresh(tag)
        self.assertIn(signoff['id'], {item['id'] for item in data['items']})

    def test_a_reminder_falling_due_and_a_new_day_answer_fresh(self):
        runtime = self.runtime
        at = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        created = runtime.application.invoke(runtime.owner, 'reminders_create', {'text': 'Call back', 'at': at},
                                              InvocationOptions(idempotency_key=new_id()))
        self.assertEqual(created.status, 'ok', plain(created.error))
        tag, data = self.first()
        self.assertNotIn('reminder', {item['kind'] for item in data['items']})
        # Time alone makes nothing due: the reminder pass stores the due state, and that write is what the tag sees.
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        runtime.reminders.clock = lambda: later
        self.assert_unchanged(tag)
        with runtime.database.transaction() as uow:
            self.assertEqual(runtime.reminders.reconcile(uow)['fired'], 1)
        tag, data = self.assert_fresh(tag)
        self.assertIn('reminder', {item['kind'] for item in data['items']})
        # The discard retention window moves with the day, so a new day is a new tag.
        tomorrow = date.today() + timedelta(days=1)
        with patch('qorqut.transport.http.date', SimpleNamespace(today=lambda: tomorrow)):
            self.assert_fresh(tag)

    def test_reply_notifications_revalidate_and_share_the_queue_build(self):
        path = '/api/root/reply-notifications?communications_after=0'
        tag, data = self.first(path)
        self.assertEqual([item['kind'] for item in data['items']], ['thread'])
        builds = self.builds
        # The queue's poll right after reuses that build; so does the other route's next miss.
        self.first()
        self.assertEqual(self.builds, builds)
        self.assert_unchanged(tag, path)
        other, _ = self.first('/api/root/reply-notifications?communications_after=5')
        self.assertNotEqual(other, tag)
        self.assertEqual(self.builds, builds)
        # An agent message moves the communications cursor's page; the build itself is redone after any write.
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE installation SET revision=revision+1 WHERE id='service'")
        tag, data = self.assert_fresh(tag, path)
        self.assertEqual(self.builds, builds + 1)
        self.first()
        self.assertEqual(self.builds, builds + 1)
        self.assertEqual(data['communications']['items'], [])

    def test_a_shared_build_is_a_copy_and_belongs_to_one_caller_context(self):
        runtime = self.runtime
        first = replies(runtime, runtime.owner)['items']
        first[0]['text'] = 'changed by a reader'
        self.assertNotEqual(replies(runtime, runtime.owner)['items'][0]['text'], 'changed by a reader')
        builds = self.builds
        replies(runtime, replace(runtime.owner, project_id=None, review_project_id=None, request_id=new_id()))
        self.assertEqual(self.builds, builds)   # another request of the same caller shares it
        replies(runtime, replace(runtime.owner, global_capabilities=frozenset()))
        self.assertEqual(self.builds, builds + 1)   # another context does not


if __name__ == '__main__':
    unittest.main()
