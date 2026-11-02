"""Current source admission for the dedicated Overseer experience reader."""
from dataclasses import replace
import json

from qorqut_operations.contracts import DomainError, InvocationOptions
from .authority import current_overseer


def _forbidden():
    raise DomainError('forbidden', 'The requested experience source is unavailable')


def linked_source_refs(value):
    """Find exact source references inside an already authorized saved binding."""
    refs = []
    def visit(item):
        if isinstance(item, dict):
            if (item.get('provider') in ('operations', 'native_archive') and isinstance(item.get('locator'), dict)
                    and isinstance(item.get('record_id'), str)):
                refs.append(item)
            else:
                for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    return refs


class EvidenceAccess:
    def __init__(self, runtime, context):
        self.runtime, self.context = runtime, context

    def _current(self, uow):
        return current_overseer(self.runtime, self.context, uow, capability='read_agent_experience')[0]

    @staticmethod
    def _namespace(ctx, namespace):
        if not isinstance(namespace, dict) or set(namespace) != {'kind', 'business_id'}:
            _forbidden()
        business = namespace['business_id']
        if namespace['kind'] == 'global' and business is None and 'read_global_context' in ctx.global_capabilities:
            return None
        if namespace['kind'] == 'business' and isinstance(business, str) and business in ctx.allowed_business_ids:
            return business
        _forbidden()

    def resolve_selection(self, selection):
        with self.runtime.database.read() as uow:
            ctx = self._current(uow)
            allowed = ([{'kind': 'global', 'business_id': None}] if 'read_global_context' in ctx.global_capabilities else [])
            allowed += [{'kind': 'business', 'business_id': identity} for identity in sorted(ctx.allowed_business_ids)]
            namespaces = allowed if selection['mode'] == 'all_authorized' else selection.get('namespaces', [])
            for ns in namespaces:
                self._namespace(ctx, ns)
            # Subject IDs are admitted as specific canonical records, never silently dropped.
            for agent_id in selection.get('agent_ids', []):
                self._agent(ctx, agent_id, uow)
                row = uow.connection.execute('SELECT home_business_id FROM agent_profiles WHERE agent_id=?', (agent_id,)).fetchone()
                if row is None or ({'kind': 'business', 'business_id': row[0]} if row[0] else {'kind': 'global', 'business_id': None}) not in namespaces:
                    _forbidden()
            for task_id in selection.get('task_ids', []):
                rows = uow.connection.execute('SELECT business_id FROM tasks WHERE id=? LIMIT 101', (task_id,)).fetchall()
                candidates = [r[0] for r in rows if {'kind': 'business', 'business_id': r[0]} in namespaces
                              or r[0] is None and {'kind': 'global', 'business_id': None} in namespaces]
                if len(candidates) != 1:
                    _forbidden()
                self._read(ctx, 'tasks_get', {'task_id': task_id}, candidates[0], uow)
            return {**selection, 'namespaces': list(namespaces)}

    def _read(self, ctx, operation, args, business, uow):
        app = self.runtime.application
        descriptor = self.runtime.registry.get(operation)
        namespace = 'business:' + business if business else 'global'
        ctx = replace(ctx, business_id=business, review_business_id=business)
        args = {**args, **({'scope': 'global'} if business is None and 'scope' in descriptor.input_schema.get('properties', {}) else {})}
        args = app.registry.bind_arguments(operation, args)
        app.authorize(ctx, descriptor, (), uow, namespace=namespace)
        descriptor.read(ctx, args, InvocationOptions(), uow)

    def _agent(self, ctx, agent_id, uow):
        if not isinstance(agent_id, str) or not agent_id:
            _forbidden()
        self._read(ctx, 'agents_get', {'agent_id': agent_id}, None, uow)

    def authorize_source(self, ref):
        if not isinstance(ref, dict) or not isinstance(ref.get('locator'), dict):
            _forbidden()
        kind, provider, identity, loc = ref.get('kind'), ref.get('provider'), ref.get('record_id'), ref['locator']
        if not isinstance(identity, str) or not identity:
            _forbidden()
        if provider != ('native_archive' if kind == 'transcript' else 'operations'):
            _forbidden()
        with self.runtime.database.read() as uow:
            ctx = self._current(uow)
            business = self._namespace(ctx, ref.get('namespace'))
            if kind in ('task', 'task_message'):
                task_id = loc.get('task_id')
                if not task_id or (kind == 'task' and identity != task_id): _forbidden()
                if kind == 'task_message':
                    if identity != loc.get('message_id') or not uow.connection.execute(
                            "SELECT 1 FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id=? AND id=?",
                            (business, task_id, identity)).fetchone(): _forbidden()
                    self._read(ctx, 'tasks_thread_get', {'task_id': task_id}, business, uow)
                self._read(ctx, 'tasks_get', {'task_id': task_id}, business, uow)
            elif kind in ('action', 'action_revision', 'action_message'):
                action_id = loc.get('action_id')
                if not action_id or (kind != 'action_message' and identity != action_id): _forbidden()
                if kind == 'action_message':
                    action = uow.connection.execute('SELECT id FROM action_logs WHERE action_key=? AND business_id IS ?', (action_id, business)).fetchone()
                    if (identity != loc.get('message_id') or action is None or not uow.connection.execute(
                            "SELECT 1 FROM thread_messages WHERE business_id IS ? AND item_type='action' AND item_id=? AND id=?",
                            (business, action[0], identity)).fetchone()): _forbidden()
                    self._read(ctx, 'actions_thread_get', {'action_id': action_id}, business, uow)
                args = {'action_id': action_id}
                if kind == 'action_revision':
                    if type(loc.get('revision')) is not int or loc['revision'] < 1: _forbidden()
                    args['revision'] = loc['revision']
                self._read(ctx, 'actions_get', args, business, uow)
            elif kind in ('signoff', 'signoff_note', 'signoff_response', 'execution_outcome'):
                signoff_id = loc.get('signoff_id')
                if not signoff_id or (kind != 'signoff_note' and identity != signoff_id): _forbidden()
                if kind == 'signoff_note':
                    if identity != loc.get('note_id') or not uow.connection.execute('SELECT 1 FROM signoff_notes WHERE id=? AND signoff_id=?', (identity, signoff_id)).fetchone(): _forbidden()
                    self._read(ctx, 'signoffs_notes_list', {'signoff_id': signoff_id}, business, uow)
                self._read(ctx, 'signoffs_get', {'signoff_id': signoff_id}, business, uow)
            elif kind in ('document', 'document_revision'):
                if identity != loc.get('document_id'): _forbidden()
                ctx = replace(ctx, business_id=business, review_business_id=business)
                doc = self.runtime.domains.documents.store.get(ctx, identity, uow)
                if doc['business_id'] != business or doc['kind'] == 'working_memory': _forbidden()
            elif kind == 'agent':
                if business is not None or identity != loc.get('agent_id'): _forbidden()
                self._agent(ctx, identity, uow)
            elif kind in ('transcript', 'transcript_entry', 'native_input'):
                agent, engine, conversation = loc.get('agent_id'), loc.get('provider'), loc.get('conversation_id')
                if not all(isinstance(x, str) and x for x in (agent, engine, conversation)): _forbidden()
                self._agent(ctx, agent, uow)
                profile = uow.connection.execute('SELECT home_business_id FROM agent_profiles WHERE agent_id=?', (agent,)).fetchone()
                if profile is None or profile['home_business_id'] != business: _forbidden()
                capture = uow.connection.execute('SELECT receipt_id FROM native_captures WHERE agent_id=? AND provider=? AND conversation_id=?',
                                                 (agent, engine, conversation)).fetchone()
                if capture is None: _forbidden()
                if kind == 'transcript':
                    if identity != json.dumps([agent, engine, conversation], sort_keys=True, separators=(',', ':'), ensure_ascii=False): _forbidden()
                    if loc.get('receipt_id'):
                        from bazarlyk.native_chat import store
                        target = store.active_profile(uow.connection, agent, self.runtime.paths)
                        if target is None or self.runtime.native_chat is None: _forbidden()
                        try:
                            receipt = self.runtime.native_chat.indexer.archive(target).load_receipt(loc['receipt_id'])
                        except Exception:
                            _forbidden()
                        if (receipt.agent_id, receipt.provider, receipt.conversation_id) != (agent, engine, conversation): _forbidden()
                elif kind == 'native_input':
                    if identity != loc.get('input_id') or not uow.connection.execute(
                            'SELECT 1 FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id WHERE i.id=? AND i.agent_id=? AND s.provider=? AND s.conversation_id=?',
                            (identity, agent, engine, conversation)).fetchone(): _forbidden()
                else:
                    from bazarlyk.native_chat.projection import Lineage
                    keys = ('archive_id', 'receipt_id', 'identity_kind', 'identity_key', 'position', 'length')
                    if any(k not in loc for k in keys) or type(loc['position']) is not int or type(loc['length']) is not int or loc['position'] < 0 or loc['length'] <= 0: _forbidden()
                    if Lineage(agent, engine, conversation, loc['archive_id']).entry_id(loc['identity_kind'], loc['identity_key']) != identity: _forbidden()
                    from bazarlyk.native_chat import store
                    profile = store.active_profile(uow.connection, agent, self.runtime.paths)
                    if profile is None or self.runtime.native_chat is None: _forbidden()
                    archive = self.runtime.native_chat.indexer.archive(profile)
                    try:
                        receipt = archive.load_receipt(loc['receipt_id'])
                    except Exception:
                        _forbidden()
                    if ((receipt.agent_id, receipt.provider, receipt.conversation_id, receipt.archive_id) != (agent, engine, conversation, loc['archive_id'])
                            or loc['position'] + loc['length'] > receipt.record_boundary): _forbidden()
            elif kind in ('telegram_owner_input', 'telegram_message'):
                if kind == 'telegram_owner_input':
                    if identity != loc.get('owner_input_id'): _forbidden()
                    rows = uow.connection.execute('SELECT agent_id,owner_principal_id,sensitive FROM telegram_owner_inputs WHERE id=?', (identity,)).fetchall()
                else:
                    if identity != loc.get('message_id'): _forbidden()
                    rows = uow.connection.execute('SELECT agent_id,owner_principal_id,sensitive FROM telegram_owner_inputs WHERE message_id=? LIMIT 2', (identity,)).fetchall()
                    message = uow.connection.execute('SELECT direction,sensitive,body_redacted FROM telegram_messages WHERE id=?', (identity,)).fetchone()
                    if message is None or message['direction'] != 'inbound' or message['sensitive'] or message['body_redacted']:
                        _forbidden()
                owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
                if len(rows) != 1 or owner is None or rows[0]['owner_principal_id'] != owner[0] or rows[0]['sensitive']:
                    _forbidden()
                if kind == 'telegram_owner_input' and loc.get('agent_id') != rows[0]['agent_id']: _forbidden()
                self._agent(ctx, rows[0]['agent_id'], uow)
                profile = uow.connection.execute('SELECT home_business_id FROM agent_profiles WHERE agent_id=?', (rows[0]['agent_id'],)).fetchone()
                if profile is None or profile[0] != business: _forbidden()
            else:
                _forbidden()

    def authorize_retention(self, run_ref, *, action):
        return self.runtime.overseer_retention.authorize_retention(self.context, run_ref, action=action)


class OwnerEvidenceAccess(EvidenceAccess):
    """Owner dashboard access uses the actual authenticated human principal."""
    def _current(self, uow):
        from bazarlyk.management.identity import installation_owner
        ctx = self.runtime.application.current_context(self.context, uow)
        if (ctx.principal_kind != 'human' or ctx.principal_id != installation_owner(uow)
                or ctx.source not in ('dashboard', 'worker')):
            _forbidden()
        return ctx
