"""Closed work moves with its history: Email Tasks move, and links whose other end stays behind stay there as history.

A move used to refuse every Task bound to an email conversation and every relationship that crossed it, so a project's
closed history could never leave. Now the historical email binding moves unchanged, a crossing link stays in the scope it
was written in and is still read from both ends, only a live dependency of an open Task blocks, and a moved Task's id is
reserved for good so history left behind never attaches to another Task."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.relationships.operations import RelationshipOperations
from qorqut_operations.relationships.ports import RelationshipPorts, ReviewEdit
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import identity
from qorqut_operations.tasks.associated import TaskAssociatedRecords
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.ports import TaskPorts

BEFORE, STAMP, AFTER = '2026-11-01T12:00:00Z', '2026-11-10T12:00:00Z', '2026-11-12T12:00:00Z'


def ports():
    noop = lambda *a, **k: None
    return TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *a: False, worker_excluded=lambda *a: False),
                     incidents=NS(link_task=noop),
                     references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
                     routing=NS(routes=lambda *a: ((), ()), snooze_routes=lambda *a: ()))


class Lookup:
    """Tasks are read in the selected scope; a material or Signoff exists only in the scope the test put it in."""

    def __init__(self, records):
        self.records = records

    def resolve(self, ctx, item, uow, builder=None, allow_busy=False):
        if item['type'] == 'task':
            row = uow.connection.execute('SELECT title FROM tasks WHERE business_id IS ? AND id=?', (ctx.business_id, item['id'])).fetchone()
            label = row['title'] if row else None
        else:
            label = self.records.get((ctx.business_id, item['type'], item['id']))
        if label is None:
            raise DomainError('not_found', 'Context item does not exist in this project')
        return {**item, 'label': label, 'archived': False, 'business_id': ctx.business_id}

    resource = relationship_item = fingerprint = lambda *a, **k: None


class TaskMoveHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.agent = str(uuid4()), str(uuid4())
        self.source, self.destination = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            for principal, kind in ((self.owner, 'human'), (self.agent, 'agent')):
                uow.repository('principals').insert(dict(id=principal, kind=kind, display_name=kind, active=1,
                                                         created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                                                       owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            for business, slug in ((self.source, 'launch'), (self.destination, 'product')):
                uow.repository('businesses').insert({'id': business, 'name': slug.title(), 'slug': slug,
                                                     'workspace': slug, 'hq_path': 'hq-' + slug})
                uow.repository('business_memberships', business_id=business).insert({'principal_id': self.owner, 'is_owner': True})
        self.ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard',
                                     global_capabilities=frozenset({'read_global_context'}),
                                     allowed_business_ids=frozenset({self.source, self.destination}))
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        self.records = {}
        items = ItemResolver(self.app, [ItemContribution('test', ('task', 'material', 'signoff', 'agent'), Lookup(self.records))],
                             candidates=NS(candidates=lambda *a, **k: ()))
        self.tasks = TaskOperations(self.app, items, ports=ports())
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)
        noop = lambda *a, **k: None
        self.relationships = RelationshipOperations(items, RelationshipPorts(
            roles={'concerns': ('Concerns', 'Concerned by')}, workflow_roles={},
            materials=NS(matches=lambda *a: True, prepare=noop, apply=noop, filters={}, material_types=('document',), tags={}),
            archive=NS(prepare=noop, apply=noop), navigation=NS(read=noop),
            review_edits={'tasks_update': ReviewEdit('task', 'task_id')}))

    # -- fixtures -------------------------------------------------------------------------------------------------

    def task(self, title, *, status='done', parent=None, attributes=None, business=None):
        with self.db.transaction() as uow:
            task_id = identity.new_task_id(uow.connection)
            uow.repository('tasks', business_id=business or self.source).insert(
                {'id': task_id, 'title': title, 'status': status, 'parent_id': parent, 'created_by': self.owner,
                 'created_at': BEFORE, 'updated_at': BEFORE, **({'fields_json': {'$task': attributes}} if attributes else {})})
        return task_id

    def edge(self, source, target, role='concerns', *, at=BEFORE, business=None):
        edge_id = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('relationships', business_id=business or self.source).insert(
                {'id': edge_id, 'from_type': source[0], 'from_id': source[1], 'to_type': target[0], 'to_id': target[1],
                 'role': role, 'context': '', 'created_at': at, 'updated_at': at, 'created_by': self.owner})
        return edge_id

    def preview(self, *task_ids):
        with self.db.read() as uow:
            return self.tasks.relocation.inspect(replace(self.ctx, business_id=self.source), uow, self.source, self.destination, list(task_ids))

    def move(self, *task_ids):
        inspection = self.preview(*task_ids)
        self.assertEqual(inspection.blockers, [])
        with self.db.transaction() as uow:
            self.tasks.relocation.apply(replace(self.ctx, business_id=self.source), uow, self.source, self.destination, list(task_ids),
                                        inspection.fingerprint(self.owner), move_id=str(uuid4()), at=STAMP)
        return inspection

    def listing(self, business, **arguments):
        with self.db.read() as uow:
            return self.relationships.read('relationships_list', replace(self.ctx, business_id=business), arguments, uow)['items']

    def read_scope(self, edge_id):
        with self.db.read() as uow:
            return uow.connection.execute('SELECT business_id FROM relationships WHERE id=?', (edge_id,)).fetchone()[0]

    # -- tests ----------------------------------------------------------------------------------------------------

    def test_an_email_task_moves_with_its_historical_binding(self):
        task_id = self.task('Reply to a creator', attributes={'task_type': 'email', 'email_conversation_id': 'conversation-1'})
        self.move(task_id)
        with self.db.read() as uow:
            row = uow.repository('tasks', business_id=self.destination).get(task_id)
        self.assertEqual(row['fields_json']['$task']['email_conversation_id'], 'conversation-1')

    def test_a_link_to_a_record_that_stays_is_history_read_from_both_ends(self):
        self.records[(self.source, 'material', 'brief')] = 'Launch brief'
        root = self.task('Launch')
        child = self.task('Send the brief', parent=root)
        inner = self.edge(('task', child), ('task', root))
        crossing = self.edge(('task', child), ('material', 'brief'))
        inspection = self.move(root)
        self.assertEqual(inspection.counts['historical_relationships'], 1)
        # The link between two moved Tasks travels; the link to the material stays where the material is.
        self.assertEqual((self.read_scope(inner), self.read_scope(crossing)), (self.destination, self.source))
        # From the moved Task's side the link is listed with the material read in the scope it lives in.
        [shown] = [edge for edge in self.listing(self.destination, item={'type': 'task', 'id': child}) if edge['id'] == crossing]
        self.assertEqual((shown['business_id'], shown['target_item']['label']), (self.source, 'Launch brief'))
        # From the material's side the Task end is read where it is now.
        [shown] = self.listing(self.source, item={'type': 'material', 'id': 'brief'})
        self.assertEqual((shown['source_item']['business_id'], shown['source_item']['moved']), (self.destination, True))

    def test_only_links_written_during_the_stay_belong_to_the_moved_task(self):
        self.records[(self.source, 'material', 'old')] = 'Old note'
        task_id = self.task('Launch')
        kept = self.edge(('task', task_id), ('material', 'old'))
        self.move(task_id)
        # Written after the Task left: it names the bare id in the old scope, which no longer holds this Task.
        later = self.edge(('task', task_id), ('material', 'old'), at=AFTER)
        listed = {edge['id'] for edge in self.listing(self.destination, item={'type': 'task', 'id': task_id})}
        self.assertEqual(listed, {kept})
        self.assertNotIn(later, listed)

    def test_the_story_keeps_signoffs_linked_during_an_earlier_stay(self):
        task_id = self.task('Approve the launch post')
        self.edge(('signoff', 'proposal-1'), ('task', task_id))
        self.move(task_id)
        reader = TaskAssociatedRecords(self.tasks)
        with self.db.read() as uow:
            stays = reader.stays(uow, self.destination, [task_id])
            found = reader.stay_sources(replace(self.ctx, business_id=self.destination), uow, 'signoff', stays)
        self.assertEqual(found, {('proposal-1', task_id, self.source)})

    def test_links_to_agents_travel_with_the_task(self):
        task_id = self.task('Campaign')
        edge_id = self.edge(('task', task_id), ('agent', self.agent), role='campaign_responsible')
        inspection = self.move(task_id)
        self.assertEqual((self.read_scope(edge_id), inspection.counts['historical_relationships']), (self.destination, 0))

    def test_only_an_open_tasks_dependency_blocks(self):
        other = self.task('Still open elsewhere', status='open')
        waiting = self.task('Waiting on it', status='open')
        self.edge(('task', waiting), ('task', other), role='blocked_by')
        self.assertEqual([blocker['code'] for blocker in self.preview(waiting).blockers], ['crossing_dependency'])
        # A Task outside the move that waits on a moved one blocks it too.
        self.assertEqual([blocker['code'] for blocker in self.preview(other).blockers], ['crossing_dependency'])
        finished = self.task('Finished', status='done')
        self.edge(('task', finished), ('task', other), role='blocked_by')
        self.assertEqual(self.move(finished).counts['historical_relationships'], 1)

    def test_a_link_stored_on_another_task_follows_the_move(self):
        moved = self.task('Moved')
        staying = self.task('Stays', status='open')
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET fields_json=? WHERE business_id=? AND id=?",
                                   ('{"$task_links":[{"business_id":"%s","task_id":"%s"}]}' % (self.source, moved), self.source, staying))
        self.assertEqual(self.move(moved).counts['linking_tasks'], 1)
        with self.db.read() as uow:
            row = uow.repository('tasks', business_id=self.source).get(staying)
        self.assertEqual(row['fields_json']['$task_links'], [{'business_id': self.destination, 'task_id': moved}])
        self.assertEqual(row['revision'], 2)

    def test_an_older_custom_field_the_destination_does_not_define_moves_unchanged(self):
        task_id = self.task('Imported')
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET fields_json=? WHERE business_id=? AND id=?",
                                   ('{"_migration_source":{"id":"old"}}', self.source, task_id))
        self.move(task_id)
        with self.db.read() as uow:
            row = uow.repository('tasks', business_id=self.destination).get(task_id)
        self.assertEqual(row['fields_json']['_migration_source'], {'id': 'old'})

    def test_a_reviewed_link_stays_behind_but_a_reviewed_assignment_blocks(self):
        first, second = self.task('First'), self.task('Second')
        reviewed = self.edge(('task', first), ('task', second))
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO context_reviews(business_id,id,relationship_id,item_type,item_id,source_type,source_id,source_token,"
                "relationship_revision,result,explanation,actor_id,prepared_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.source, str(uuid4()), reviewed, 'task', first, 'task', second, 't', 1, 'no_change', 'Read', self.owner, STAMP, STAMP))
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET parent_id=? WHERE business_id=? AND id=?", (first, self.source, second))
        self.assertEqual(self.move(first).counts['historical_relationships'], 1)
        self.assertEqual(self.read_scope(reviewed), self.source)
        self.assertIn(reviewed, {edge['id'] for edge in self.listing(self.destination, item={'type': 'task', 'id': first})})

    def test_a_moved_tasks_id_stays_reserved_after_the_task_is_gone(self):
        task_id = self.task('Moved then removed')
        self.move(task_id)
        with self.db.transaction() as uow:
            uow.connection.execute('DELETE FROM tasks WHERE business_id=? AND id=?', (self.destination, task_id))
        with self.db.read() as uow:
            self.assertTrue(identity.taken(uow.connection, task_id))
            self.assertTrue(identity.taken(uow.connection, task_id.upper()))

    def test_a_link_left_behind_finds_a_note_moved_to_another_project(self):
        note = str(uuid4())
        self.records[(self.destination, 'material', note)] = 'Launch brief'
        with self.db.transaction() as uow:
            uow.connection.execute("INSERT INTO documents(id,business_id,namespace,kind,path,content_hash,created_at,updated_at) "
                                   "VALUES(?,?,?,'business_owned_note','notes/brief.md','x',?,?)",
                                   (note, self.destination, self.destination, STAMP, STAMP))
            uow.connection.execute("INSERT INTO document_moves(move_id,document_id,kind,source_business_id,destination_business_id,source_path,"
                                   "destination_path,revision_before,actor_id,moved_at) VALUES(?,?,'business_owned_note',?,?,?,?,1,?,?)",
                                   (str(uuid4()), note, self.source, self.destination, 'notes/brief.md', 'notes/brief.md', self.owner, STAMP))
        task_id = self.task('Send the brief', status='open')
        edge = self.edge(('task', task_id), ('material', note))
        [shown] = [item for item in self.listing(self.source, item={'type': 'task', 'id': task_id}) if item['id'] == edge]
        self.assertEqual((shown['target_item']['business_id'], shown['target_item']['moved']), (self.destination, True))


if __name__ == '__main__':
    unittest.main()
