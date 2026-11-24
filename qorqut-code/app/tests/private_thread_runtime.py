"""An isolated store with the real Signoff, private-message and receipt paths, for tests of the owner's private threads.

``build_runtime`` gives the owner, the coordinator and a worker; ``create`` opens a private thread on a question Signoff with
the owner's first message, ``reply`` answers in it as the coordinator.
"""
from types import SimpleNamespace

from qorqut_operations.application import Application, OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal, new_id, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths

from qorqut.agents.attention import Attention
from qorqut.composition.assembly import host_assembly
from qorqut.composition.catalog import builtin_registry
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.extensions import NO_EXTENSIONS
from qorqut.reminders.service import ReminderService
from qorqut.service.changes import ChangeFeed
from qorqut.signoffs.ports import HostExecution, HostPeers, HostReferences, HostTasks, PrivateSignoffVisibility


class Policy:
    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision('allow', 'test', {'global_capability': descriptor.global_capability})

    def context_for(self, *args, **kwargs):
        raise DomainError('not_found', 'No active attention work in this fixture')


def build_runtime(tmp_path):
    database = Database(tmp_path / 'operations.sqlite', assembly=host_assembly(ChangeFeed()))
    database.initialize_target()
    registry = builtin_registry()
    ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
        platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT, narrowing=PrivateSignoffVisibility({}))
    signoffs = SignoffService(database, ports, locks=LockPaths.under(tmp_path / 'locks'))
    app = Application(database, registry, policy=Policy(), signoffs=signoffs)
    signoffs.bind(app)
    for descriptor in signoffs.descriptors():
        registry.register(descriptor)
    inbox = SimpleNamespace(app=app, domains=SimpleNamespace())
    runtime = SimpleNamespace(database=database, application=app, signoffs=signoffs, inbox=inbox,
        focus=SimpleNamespace(inbox=inbox), paths=SimpleNamespace(runtime=tmp_path / 'runtime'), selection=NO_EXTENSIONS)
    app.ordubashy_attention = Attention(runtime)
    runtime.private_conversations = PrivateConversations(inbox, references=signoffs.notes).register()
    runtime.reminders = ReminderService(runtime).register()
    owner, coordinator, worker = new_id(), new_id(), new_id()
    with database.transaction() as uow:
        for identity, kind in ((owner, 'human'), (coordinator, 'agent'), (worker, 'agent')):
            uow.repository('principals').insert({'id': identity, 'kind': kind, 'display_name': kind})
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(),
            'owner_principal_id': owner, 'management_coordinator_id': coordinator})
        for identity, role in ((coordinator, 'ordubashy'), (worker, 'global-worker')):
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': identity, 'principal_id': identity,
                'name': role, 'workspace': str(tmp_path / role), 'status': 'active',
                'profile_json': {'name': role, 'status': 'active', 'role': {'id': role}, 'runtime': 'claude-code',
                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
    # Every report belongs to a Task. The Task engine is not part of this fixture, so ``tasks_get`` is its read of a Global
    # Task row; ``report_task(runtime)`` adds one.
    def read_task(ctx, args, options, uow):
        row = uow.connection.execute('SELECT id,title FROM tasks WHERE project_id IS NULL AND id=?', (args['task_id'],)).fetchone()
        if row is None:
            raise DomainError('not_found', 'Task does not exist')
        return {'task': dict(row)}
    registry.register(OperationDescriptor('tasks_get', schema({'scope': {'enum': ['project', 'global']}, 'task_id': {'type': 'string'}}, ('task_id',)),
        scope_kinds=frozenset({'project', 'global'}), global_capability='read_global_context', effect_kind='read',
        supports_signoff=False, resolve_namespace=lambda ctx, args: 'global', read=read_task))
    runtime.owner = InvocationContext(Principal(owner, 'human'), source='dashboard', global_capabilities={'read_global_context'})
    runtime.coordinator = InvocationContext(Principal(coordinator, 'agent'), source='mcp', profile_revision=1, global_capabilities={'read_global_context'})
    runtime.worker = InvocationContext(Principal(worker, 'agent'), source='mcp', profile_revision=1, global_capabilities={'read_global_context'})
    return runtime


def invoke(runtime, operation, args=None, *, ctx=None, key=None):
    result = runtime.application.invoke(ctx or runtime.owner, operation, {'scope': 'global', **(args or {})},
        InvocationOptions(idempotency_key=key or new_id()))
    assert result.status == 'ok', plain(result.error)
    return plain(result.data)


def report_task(runtime, identity='report-work'):
    """A Global Task for a report to belong to, as the ``report_task`` argument names it."""
    with runtime.database.transaction() as uow:
        uow.connection.execute("INSERT OR IGNORE INTO tasks(project_id,id,title,status,created_by,created_at,updated_at) "
            "VALUES(NULL,?,'Report work','done',?,'2026-11-01T09:00:00.000000Z','2026-11-01T09:00:00.000000Z')",
            (identity, runtime.owner.principal_id))
    return {'id': identity, 'project_id': None}


def create(runtime, text='First private question', title='A private thread', **kwargs):
    """A question Signoff and the owner's first private message to Ordubashy on it: a private thread to talk in."""
    signoff = invoke(runtime, 'signoffs_input_create', {'request_key': new_id(), 'form': {
        'title': title, 'explanation': 'Talk about it here', 'kind': 'question', 'selection_mode': 'none'}})['signoff']
    invoke(runtime, 'owner_conversation_send', {'source': source(signoff), 'text': text, **kwargs})
    return signoff


def source(signoff):
    return {'type': 'signoff', 'id': signoff['id'], 'project_id': signoff['review_project_id']}


def reply(runtime, signoff, text='Reply'):
    return invoke(runtime, 'owner_conversation_send', {'source': source(signoff), 'text': text}, ctx=runtime.coordinator)
