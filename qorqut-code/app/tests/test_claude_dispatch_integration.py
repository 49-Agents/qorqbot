"""Real assignment storage/dispatch with a deterministic Claude process stand-in.

No provider or existing installation is used. The child authenticates its issued
worker credential over loopback before saving its completion checkpoint.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from importlib.resources import files
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from qorqut_operations.storage.database import Database
from qorqut.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from qorqut.bootstrap.delegated_runtime import DelegatedRuntime
from qorqut.composition.assembly import host_assembly
from qorqut.inbox.service import InboxService
from qorqut.service.changes import ChangeFeed
from qorqut.transport.auth import Authentication


class ClaudeDispatchIntegration(unittest.TestCase):
    def test_claim_execute_checkpoint_and_usage_with_real_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            state = root / 'state'
            with mock.patch.dict('os.environ', {'XDG_STATE_HOME': str(state)}):
                self.exercise(root, state)

    def exercise(self, root, state):
        database = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(database.close)
        now = datetime.now(timezone.utc)
        stamp = now.isoformat().replace('+00:00', 'Z')
        store = OperationalDelegation(clock=lambda: stamp)
        parent, business, registration, consumer, conversation = [new_id() for _ in range(5)]
        workspace = root / 'parent'
        workspace.mkdir()
        # A real prepared parent has its managed runtime-instruction snapshot.
        from qorqut_agentic.behavior.instructions import from_catalog
        instruction_root = files('qorqut_agentic').joinpath('resources/defaults/operations/runtime-instructions')
        instructions = from_catalog(lambda path: instruction_root.joinpath(path).read_text())
        (workspace / '.qorqut').mkdir()
        (workspace / '.qorqut/runtime-instructions.json').write_bytes(instructions.bytes())
        authentication = Authentication(database, root, clock=lambda: now.timestamp())
        failures = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    actor = authentication.agent(self.headers.get('Authorization', ''))
                    with database.transaction() as uow:
                        assignment = store.get(uow, actor.delegation_assignment_id)
                        store.checkpoint(uow, assignment['id'], actor_principal_id=actor.principal.id,
                            generation=actor.delegation_generation, lease_id=assignment['lease_id'],
                            expected_revision=assignment['revision'], checkpoint={'checked': True},
                            state='completed', result={'summary': 'Synthetic worker completed'})
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{}')
                except Exception as error:
                    failures.append(repr(error))
                    self.send_response(500)
                    self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        endpoint = f'http://127.0.0.1:{server.server_port}'
        try:
            with database.transaction() as uow:
                uow.repository('principals').insert({'id': parent, 'kind': 'agent', 'display_name': 'Coordinator',
                    'active': 1, 'created_at': stamp, 'updated_at': stamp})
                uow.repository('businesses').insert({'id': business, 'slug': 'sample', 'workspace': str(root),
                    'hq_path': str(root), 'name': 'Sample', 'created_at': stamp, 'updated_at': stamp})
                uow.repository('agent_profiles', business_id=None).insert({'agent_id': parent, 'principal_id': parent,
                    'namespace': 'ecosystem', 'name': 'Coordinator', 'workspace': str(workspace),
                    'profile_json': {'runtime': 'claude-code', 'role': {'id': 'ordubashy'},
                        'scope': {'kind': 'ecosystem', 'allowed_business_ids': [business],
                                  'global_capabilities': ['manage_delegated_assignments']}},
                    'status': 'active', 'created_at': stamp, 'updated_at': stamp})
                uow.repository('machine_registrations').insert({'id': registration, 'principal_id': parent,
                    'workspace': str(workspace), 'service_url': endpoint, 'profile_revision': 1,
                    'compatibility_json': {'durable_delegation_enabled': True},
                    'created_at': stamp, 'updated_at': stamp})
                uow.connection.execute('INSERT INTO native_session_owners '
                    '(agent_id,consumer_id,binding_json,generation) VALUES(?,?,?,1)',
                    (parent, consumer, json.dumps({'runtime': 'claude-code'})))
                saved = store.create(uow, parent_principal_id=parent, business_id=business,
                    scope={'kind': 'business', 'business_id': business, 'record_ids': {}, 'source_ids': {}},
                    objective='Complete a synthetic assignment', plan={'steps': ['check', 'complete']},
                    allowed_operations=[], resource_bounds={'max_turns': 2, 'max_runtime_seconds': 30})
            app = SimpleNamespace(database=database, current_context=lambda ctx, _uow: ctx)
            operations = OperationalDelegationOperations(store)
            operations.application = app
            operations.inbox = InboxService(app, None, clock=lambda: now)
            context = InvocationContext(Principal(parent, 'agent', 'Coordinator'), profile_revision=1,
                global_capabilities=frozenset({'manage_delegated_assignments'}),
                role_artifact_ref=ArtifactRef('ordubashy', '1.34.0', 'sha256:' + 'a' * 64),
                session_registration_id=registration, inbox_session_id=consumer, inbox_generation=1)
            claim = operations.claim_next_for_dispatch(context, registration, consumer)
            self.assertEqual(claim['assignment']['id'], saved['id'])
            with self.assertRaises(DomainError):
                operations.claim_next_for_dispatch(context, registration, new_id())

            class Client:
                def post(self, route, body):
                    if route.endswith('/poll'):
                        return operations.poll_worker(context, **body)
                    failures.append(body.get('reason', route))
                    return operations.dispatch_failed(context, **body)

            invoked = []
            child_code = '''import json,pathlib,sys,urllib.request
sys.stdin.read()
registration=json.loads(pathlib.Path('.qorqut/registration.json').read_text())
token=pathlib.Path('.qorqut/credential').read_text().strip()
print(json.dumps({'type':'system','subtype':'init','session_id':sys.argv[1]}),flush=True)
request=urllib.request.Request(registration['service_url']+'/checkpoint',data=b'{}',
    headers={'Authorization':'Bearer '+token},method='POST')
with urllib.request.urlopen(request,timeout=5) as response: assert response.status==200
print(json.dumps({'type':'result','subtype':'success','is_error':False,
    'usage':{'input_tokens':4,'cache_read_input_tokens':6,'output_tokens':3}}),flush=True)
'''

            def launch(command, **kwargs):
                invoked.extend(command)
                return subprocess.Popen([sys.executable, '-c', child_code, conversation], **kwargs)

            parent_registration = {'agent_id': parent, 'registration_id': registration,
                'workspace': str(workspace), 'service_url': endpoint, 'durable_delegation_enabled': True}
            driver = DelegatedRuntime(parent_registration, consumer, alive=lambda: True,
                runtime='claude-code', executable='claude', client=Client(), popen=launch,
                state_root=state / 'qorqut' / 'delegated-workers')
            driver._execute(claim)
            self.assertEqual(failures, [])
            self.assertEqual(invoked[:2], ['claude', '-p'])
            with database.read() as uow:
                completed = store.get(uow, saved['id'])
                self.assertEqual(completed['state'], 'completed')
                self.assertEqual(completed['runtime_thread_id'], conversation)
                self.assertEqual(store.worker_usage(uow, saved['id'])['turns'], 1)
                self.assertEqual(store.worker_usage(uow, saved['id'])['input_tokens'], 10)
            self.assertFalse((Path(claim['worker_workspace']) / '.qorqut/credential').exists())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
