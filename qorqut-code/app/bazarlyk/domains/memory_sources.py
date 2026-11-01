"""Permission-checked, bounded observation tokens; never a content/search index."""
from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.application.plans import timestamp, utc_clock

MAX_CHILDREN = 200
SOURCE_TYPES = ("task", "document", "signoff", "email")


class MemorySources:
    def __init__(self, application, store, email, *, clock=utc_clock):
        self.app, self.store, self.email = application, store, email
        self.clock = clock

    def authorize(self, ctx, operation, uow):
        descriptor = self.app.registry.get(operation)
        args = {'scope': 'global'} if ctx.business_id is None and operation.startswith(('signoffs_', 'email_')) else {}
        self.app.authorize(ctx, descriptor, (), uow, namespace=self.app.namespace(ctx, descriptor, args))
        if operation.startswith('tasks_'):
            from qorqut_operations.tasks.scope import task_context
            task_context(ctx, args, uow)

    def bounded(self, uow, sql, params):
        found = uow.connection.execute(sql + " LIMIT ?", (*params, MAX_CHILDREN + 1)).fetchall()
        if len(found) > MAX_CHILDREN:
            raise DomainError("observation_unavailable", "Source has too many related records for a compact observation")
        return [dict(row) for row in found]

    def observe(self, ctx, source, uow):
        kind, identity = source['type'], source['id']
        tables = {'task': 'tasks', 'document': 'documents',
                  'signoff': 'signoffs', 'email': 'email_conversations'}
        operations = {'task': 'tasks_get', 'signoff': 'signoffs_get',
                      'email': 'email_conversation_get'}
        if kind not in tables:
            raise DomainError('validation_error', 'Unsupported working-memory source type')
        if kind != 'document':
            self.authorize(ctx, operations[kind], uow)
        # No Global fallback: supporting evidence must belong to this selected scope.
        if kind == 'signoff':
            row = self.app.signoffs._row(self.app.signoffs._context(ctx, uow), identity, uow)
            if row['review_business_id'] != ctx.business_id:
                raise DomainError('not_found', 'Source is not available in this scope')
        else:
            row = uow.repository(tables[kind], business_id=ctx.business_id).get(identity)
        if not row:
            raise DomainError('not_found', 'Source is not available in this scope')
        token = {'v': 1, 'type': kind, 'id': identity, 'business_id': ctx.business_id,
                 'revision': row.get('revision', row.get('status_revision')),
                 'archived': row.get('archived', False)}
        summary = {'id': identity, 'type': kind, 'archived': bool(row.get('archived'))}
        if kind == 'document':
            if row['kind'] in {'working_memory', 'legacy_working_plan', 'asset', 'data_upload'}:
                raise DomainError('forbidden', 'This document is not working-memory evidence')
            if row['kind'] == 'personal_context':
                operation = 'personal_context_get'
                from .personal_context import PersonalContextOperations
                PersonalContextOperations.access(ctx, uow)
            elif row['kind'] in {'agent_note', 'business_note', 'journal', 'global_note', 'business_owned_note'}:
                operation = 'notes_get'
                if row['kind'] == 'global_note' and ctx.agent_id:
                    raise DomainError('not_found', 'Owner notebook is not agent evidence')
            elif row['kind'] == 'plan':
                operation = 'plans_get'
                if row['metadata_json'].get('working_memory_id'):
                    raise DomainError('not_found', 'Migrated plans use private memory access')
            elif row['kind'] == 'email_message':
                operation = 'email_conversation_get'
                self.email.access(ctx, uow)
                message = uow.repository('email_messages', business_id=ctx.business_id).get(row['metadata_json'].get('message_id'))
                if not message or message['deleted']:
                    raise DomainError('not_found', 'Email is no longer available')
            else:
                from .documents.library import ContextLibrary
                if not ContextLibrary.eligible(row):
                    raise DomainError('forbidden', 'Document is outside the registered context library')
                operation = 'context_get'
            self.authorize(ctx, operation, uow)
            self.store.get(ctx, identity, uow)
            # Never accept externally modified evidence as a committed source token.
            import hashlib
            if hashlib.sha256(self.store.read_bytes(row, uow)).hexdigest() != row['content_hash'].removeprefix('sha256:'):
                raise DomainError('observation_unavailable', 'Source has uncommitted file changes')
            token['content_hash'] = row['content_hash']
            summary['title'] = str(row['metadata_json'].get('title', 'Document'))[:200]
        elif kind == 'task':
            summary.update(title=row['title'][:200], state=row['status'])
            token['relationships'] = self.bounded(uow,
                "SELECT id,revision,role,to_type,to_id FROM relationships WHERE business_id IS ? AND from_type=? AND from_id=? ORDER BY id",
                (ctx.business_id, kind, identity))
            token['parent_id'] = row.get('parent_id')
            from qorqut_operations.tasks import snooze_view
            summary.update(snooze_view(row, timestamp(self.clock)))
            if summary['snoozed_until'] is not None:
                # Expiry changes freshness without a write or a clock-dependent token.
                token['snoozed'] = summary['snoozed']
            # Canonical task comments already advance the task revision.
            # Do not scan its entire note history on every bootstrap.
            token['blockers'] = self.bounded(uow,
                "SELECT t.id,t.revision,t.status FROM relationships r JOIN tasks t ON t.business_id IS r.business_id AND t.id=r.to_id WHERE r.business_id IS ? AND r.from_type='task' AND r.from_id=? AND r.role='blocked_by' ORDER BY t.id",
                (ctx.business_id, identity))
            cfg = uow.repository('business_configs', business_id=ctx.business_id).get('tasks')
            token['task_config_revision'] = cfg['revision'] if cfg else None
        elif kind == 'email':
            self.email.access(ctx, uow)
            token['messages'] = self.bounded(uow,
                'SELECT id,revision,deleted FROM email_messages WHERE business_id IS ? AND conversation_id=? ORDER BY id',
                (ctx.business_id, identity))
            summary['title'] = str(row.get('subject', 'Email conversation'))[:200]
        else:
            summary['state'] = row['state']
        return {**summary, 'token': canonical_hash(token), 'token_version': 1}

    def check(self, ctx, refs, uow):
        results = []
        for source in refs:
            try:
                current = self.observe(ctx, source, uow)
            except DomainError:
                # Cannot distinguish deleted evidence from revoked visibility safely.
                # Withhold derived prose unless every cited source can be authorized.
                results.append({'type': source['type'], 'id': source['id'], 'state': 'unavailable',
                                'reason': 'source_unavailable'})
            else:
                results.append({**current, 'source_state': current.get('state'),
                                'state': 'unchanged' if current['token'] == source['token'] else 'changed'})
        return results
