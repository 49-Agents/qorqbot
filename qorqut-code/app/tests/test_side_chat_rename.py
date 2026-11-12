"""Side chats exercise the real Signoff, private-message and receipt paths in an isolated store."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.application import Application
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal, new_id, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths

from qorqut.agents.attention import Attention
from qorqut.attention_queue import queue
from qorqut.composition.assembly import host_assembly
from qorqut.composition.catalog import builtin_registry
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.inbox.side_chats import SideChats
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
    runtime.private_conversations = PrivateConversations(inbox).register()
    runtime.side_chats = SideChats(runtime).register()
    runtime.reminders = ReminderService(runtime).register()
    owner, coordinator, worker = new_id(), new_id(), new_id()
    with database.transaction() as uow:
        for identity, kind in ((owner, 'human'), (coordinator, 'agent'), (worker, 'agent')):
            uow.repository('principals').insert({'id': identity, 'kind': kind, 'display_name': kind})
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(),
            'owner_principal_id': owner, 'management_coordinator_id': coordinator})
        for identity, role in ((coordinator, 'ordubashy'), (worker, 'global-worker')):
            uow.repository('agent_profiles', business_id=None).insert({'agent_id': identity, 'principal_id': identity,
                'namespace': 'ecosystem', 'name': role, 'workspace': str(tmp_path / role), 'status': 'active',
                'profile_json': {'name': role, 'status': 'active', 'role': {'id': role}, 'runtime': 'claude-code',
                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
    runtime.owner = InvocationContext(Principal(owner, 'human'), source='dashboard', global_capabilities={'read_global_context'})
    runtime.coordinator = InvocationContext(Principal(coordinator, 'agent'), source='mcp', profile_revision=1, global_capabilities={'read_global_context'})
    runtime.worker = InvocationContext(Principal(worker, 'agent'), source='mcp', profile_revision=1, global_capabilities={'read_global_context'})
    return runtime


def invoke(runtime, operation, args=None, *, ctx=None, key=None):
    result = runtime.application.invoke(ctx or runtime.owner, operation, {'scope': 'global', **(args or {})},
        InvocationOptions(idempotency_key=key or new_id()))
    assert result.status == 'ok', plain(result.error)
    return plain(result.data)


def create(runtime, text='First private question', **kwargs):
    return invoke(runtime, 'side_chats_create', {'text': text, **kwargs})['signoff']


def source(signoff):
    return {'type': 'signoff', 'id': signoff['id'], 'business_id': signoff['review_business_id']}


def reply(runtime, signoff, text='Reply'):
    return invoke(runtime, 'owner_conversation_send', {'source': source(signoff), 'text': text}, ctx=runtime.coordinator)


class SideChatRenameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp_path = Path(directory.name)
        self.runtime = build_runtime(self.tmp_path)
        self.addCleanup(self.runtime.database.close)

    def test_rename_persists_without_changing_history_unread_or_order(self):
        runtime = self.runtime
        s = create(runtime)
        reply(runtime, s)
        create(runtime, title='Latest conversation')
        before = queue(runtime, runtime.owner)['side_chats']
        thread = invoke(runtime, 'owner_conversation_get', {'source': source(s)})
        key = new_id()
        args = {'signoff_id': s['id'], 'title': '  A new topic  '}
        renamed = invoke(runtime, 'side_chats_rename', args, key=key)['signoff']
        assert renamed['title'] == 'A new topic' and renamed['state'] == 'pending'
        assert renamed['preview'] == s['preview'] and renamed['payload_hash'] == s['payload_hash']
        assert invoke(runtime, 'side_chats_rename', args, key=key)['signoff'] == renamed
        assert invoke(runtime, 'signoffs_get', {'signoff_id': s['id']})['signoff']['title'] == 'A new topic'
        after = queue(runtime, runtime.owner)['side_chats']
        assert after == [{**chat, 'title': 'A new topic'} if chat['id'] == s['id'] else chat for chat in before]
        assert invoke(runtime, 'owner_conversation_get', {'source': source(s)}) == thread
        unchanged = invoke(runtime, 'side_chats_rename', {'signoff_id': s['id'], 'title': 'A new topic'})['signoff']
        assert unchanged['status_revision'] == renamed['status_revision']

    def test_rename_requires_owner_and_a_side_chat(self):
        runtime = self.runtime
        s = create(runtime)
        for ctx in (runtime.coordinator, replace(runtime.coordinator, source='worker'), runtime.worker):
            result = runtime.application.invoke(ctx, 'side_chats_rename',
                {'scope': 'global', 'signoff_id': s['id'], 'title': 'Agent rename'}, InvocationOptions(idempotency_key=new_id()))
            assert result.status == 'error' and result.error['code'] == 'forbidden'
        approval = invoke(runtime, 'signoffs_input_create', {'request_key': new_id(), 'form': {
            'title': 'An approval', 'explanation': 'Choose one', 'kind': 'question', 'selection_mode': 'none'}})['signoff']
        result = runtime.application.invoke(runtime.owner, 'side_chats_rename',
            {'scope': 'global', 'signoff_id': approval['id'], 'title': 'Changed approval'}, InvocationOptions(idempotency_key=new_id()))
        assert result.status == 'error' and result.error['code'] == 'invalid_transition'
        assert invoke(runtime, 'signoffs_get', {'signoff_id': s['id']})['signoff']['title'] == 'Side chat'
        assert invoke(runtime, 'signoffs_get', {'signoff_id': approval['id']})['signoff']['title'] == 'An approval'

    def test_project_side_chat_keeps_its_scope_in_the_global_queue(self):
        runtime = self.runtime
        tmp_path = self.tmp_path
        business = new_id()
        with runtime.database.transaction() as uow:
            uow.repository('businesses').insert({'id': business, 'slug': 'project', 'name': 'Project',
                'workspace': str(tmp_path / 'project'), 'hq_path': str(tmp_path / 'hq')})
            uow.repository('business_memberships', business_id=business).insert({
                'principal_id': runtime.owner.principal_id, 'active': 1, 'is_owner': 1})
        runtime.owner = replace(runtime.owner, allowed_business_ids={business})
        selected = replace(runtime.owner, business_id=business, review_business_id=business)
        s = invoke(runtime, 'side_chats_create', {'scope': 'business', 'text': 'Project question'}, ctx=selected)['signoff']
        chat = queue(runtime, runtime.owner)['side_chats'][0]
        assert chat['source']['business_id'] == business and chat['business_slug'] == 'project'
        renamed = invoke(runtime, 'side_chats_rename', {'scope': 'business', 'signoff_id': s['id'], 'title': 'Project topic'}, ctx=selected)['signoff']
        assert renamed['title'] == 'Project topic' and renamed['review_business_id'] == business
        assert queue(runtime, runtime.owner)['side_chats'][0]['title'] == 'Project topic'
        assert invoke(runtime, 'side_chats_close', {'scope': 'business', 'signoff_id': s['id']}, ctx=selected)['signoff']['state'] == 'cancelled'
        assert queue(runtime, runtime.owner)['side_chats'] == []

    def test_rename_rejects_invalid_names(self):
        runtime = self.runtime
        for title in ('', ' \t\n ', 'x' * 201):
            with self.subTest(title=title):
                s = create(runtime, title='Keep this name')
                result = runtime.application.invoke(runtime.owner, 'side_chats_rename',
                    {'scope': 'global', 'signoff_id': s['id'], 'title': title}, InvocationOptions(idempotency_key=new_id()))
                assert result.status == 'error' and result.error['code'] == 'validation_error'
                assert queue(runtime, runtime.owner)['side_chats'][0]['title'] == 'Keep this name'
