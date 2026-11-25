"""A waiting parent's dispatch claim on a real store: what a claim with nothing to take opens, and what a claim takes.

The parent's dispatcher asks every few seconds whether one of its assignments can run now. No provider is used and no
worker is started; the claim's answer is checked, not executed.
"""
from datetime import datetime, timezone
import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from qorqut_operations.storage.database import Database
from qorqut.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from qorqut.composition.assembly import host_assembly
from qorqut.inbox.service import InboxService
from qorqut.service.changes import ChangeFeed

from background_fixture import WriteUnits, rows

CLAIM = ('write_if_needed', 'delegation.dispatch.claim')


class DelegationClaimWrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        patcher = mock.patch.dict('os.environ', {'XDG_STATE_HOME': str(root / 'state')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.database = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.database.close)
        now = datetime.now(timezone.utc)
        self.stamp = now.isoformat().replace('+00:00', 'Z')
        self.store = OperationalDelegation(clock=lambda: self.stamp)
        self.parent, self.project, self.registration, self.consumer = [new_id() for _ in range(4)]
        workspace = root / 'parent'
        (workspace / '.qorqut').mkdir(parents=True)
        # A real prepared parent has its managed runtime-instruction snapshot.
        from qorqut_agentic.behavior.instructions import from_catalog
        instruction_root = files('qorqut_agentic').joinpath('resources/defaults/operations/runtime-instructions')
        instructions = from_catalog(lambda path: instruction_root.joinpath(path).read_text())
        (workspace / '.qorqut/runtime-instructions.json').write_bytes(instructions.bytes())
        stamp = self.stamp
        with self.database.transaction() as uow:
            uow.repository('principals').insert({'id': self.parent, 'kind': 'agent', 'display_name': 'Coordinator',
                'active': 1, 'created_at': stamp, 'updated_at': stamp})
            uow.repository('projects').insert({'id': self.project, 'slug': 'sample', 'workspace': str(root), 'name': 'Sample',
                'created_at': stamp, 'updated_at': stamp})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': self.parent, 'principal_id': self.parent,
                'name': 'Coordinator', 'workspace': str(workspace),
                'profile_json': {'runtime': 'claude-code', 'role': {'id': 'ordubashy'},
                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['manage_delegated_assignments']}},
                'status': 'active', 'created_at': stamp, 'updated_at': stamp})
            uow.repository('machine_registrations').insert({'id': self.registration, 'principal_id': self.parent,
                'workspace': str(workspace), 'service_url': 'http://127.0.0.1:9', 'profile_revision': 1,
                'compatibility_json': {'durable_delegation_enabled': True}, 'created_at': stamp, 'updated_at': stamp})
            uow.connection.execute('INSERT INTO native_session_owners (agent_id,consumer_id,binding_json,generation) '
                'VALUES(?,?,?,1)', (self.parent, self.consumer, json.dumps({'runtime': 'claude-code'})))
        app = SimpleNamespace(database=self.database, current_context=lambda ctx, _uow: ctx)
        self.operations = OperationalDelegationOperations(self.store)
        self.operations.application = app
        self.operations.inbox = InboxService(app, None, clock=lambda: now)
        self.context = InvocationContext(Principal(self.parent, 'agent', 'Coordinator'), profile_revision=1,
            global_capabilities=frozenset({'manage_delegated_assignments'}),
            role_artifact_ref=ArtifactRef('ordubashy', '1.34.0', 'sha256:' + 'a' * 64),
            session_registration_id=self.registration, inbox_session_id=self.consumer, inbox_generation=1)
        self.units = WriteUnits(self.database)

    def assignment(self):
        with self.database.transaction() as uow:
            saved = self.store.create(uow, parent_principal_id=self.parent, project_id=self.project,
                scope={'kind': 'project', 'project_id': self.project, 'record_ids': {}, 'source_ids': {}},
                objective='Complete a synthetic assignment', plan={'steps': ['check', 'complete']},
                allowed_operations=[], resource_bounds={'max_turns': 2, 'max_runtime_seconds': 30})
        self.units.take()
        return saved['id']

    def claim(self, consumer=None):
        return self.operations.claim_next_for_dispatch(self.context, self.registration, consumer or self.consumer)

    def state(self, identity):
        row, = rows(self.database, 'SELECT state FROM operational_assignments WHERE id=?', identity)
        return row['state']

    def test_a_claim_with_no_assignment_takes_nothing(self):
        self.assertEqual(self.claim(), {'assignment': None, 'reason': None})
        # Its unit opens without the writer and, with nothing to change, never takes it.
        self.assertEqual(self.units.take(), ([CLAIM], []))

    def test_a_claim_with_only_a_running_lease_takes_nothing(self):
        identity = self.assignment()
        self.assertEqual(self.claim()['assignment']['id'], identity)
        self.units.take()
        self.assertEqual(self.claim(), {'assignment': None, 'reason': None})
        self.assertEqual(self.state(identity), 'running')
        self.assertEqual(self.units.take(), ([CLAIM], []))

    def test_a_claim_refused_the_writer_runs_again_and_claims_once(self):
        identity = self.assignment()
        session_owner, calls = self.operations.inbox.session_owner, []

        def session_owner_then_commit(ctx, consumer, uow):
            # Another writer commits while the claim is still reading, so its first write cannot have the writer.
            calls.append(True)
            owner = session_owner(ctx, consumer, uow)
            if len(calls) == 1:
                with self.database.transaction() as other:
                    other.connection.execute("UPDATE projects SET name='Renamed' WHERE id=?", (self.project,))
            return owner
        self.operations.inbox.session_owner = session_owner_then_commit
        self.units.take()
        claim = self.claim()
        self.assertEqual(len(calls), 2)
        self.assertEqual(claim['assignment']['id'], identity)
        self.assertEqual(self.state(identity), 'running')
        events = rows(self.database, 'SELECT event_type FROM operational_assignment_events WHERE assignment_id=?', identity)
        self.assertEqual([event['event_type'] for event in events].count('claimed'), 1)
        # The claim's one entry and the other writer's unit; the repeat runs inside the claim's own entry.
        self.assertEqual(self.units.take()[0], [CLAIM, ('transaction', None)])

    def test_a_claim_from_another_session_is_refused_with_nothing_to_take(self):
        with self.assertRaises(DomainError) as raised:
            self.claim(new_id())
        self.assertEqual(raised.exception.code, 'inbox_session_superseded')

    def test_a_queued_assignment_is_claimed(self):
        identity = self.assignment()
        claim = self.claim()
        self.assertEqual(claim['assignment']['id'], identity)
        self.assertEqual(self.state(identity), 'running')
        self.assertEqual(self.units.take()[0], [CLAIM])
        # Its lease is now active, so the next claim has nothing left to take.
        self.assertEqual(self.claim(), {'assignment': None, 'reason': None})

    def test_three_active_leases_take_no_fourth(self):
        identities = [self.assignment() for _ in range(4)]
        for _ in range(3):
            self.assertIn(self.claim()['assignment']['id'], identities)
        self.assertEqual(self.claim(), {'assignment': None, 'reason': 'three worker leases are already active'})
        self.assertEqual(sorted(self.state(identity) for identity in identities), ['queued', 'running', 'running', 'running'])


if __name__ == '__main__':
    unittest.main()
