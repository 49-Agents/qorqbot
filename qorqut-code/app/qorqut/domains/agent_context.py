"""The agent's own compact working context: ``agent_context_get`` and the start-up inventory it opens a conversation with."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
import json

from qorqut.agents import roles
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, Principal, canonical_hash
from qorqut_operations.application.plans import schema, UUID, timestamp, utc_clock

SELECTOR = {'agent_id': UUID}


class AgentContext:
    def __init__(self, application, resolver, *, clock=utc_clock):
        self.app, self.resolver = application, resolver
        self.clock = clock

    def register(self, registry):
        arguments = {**SELECTOR,
                'inventory': {'type': 'boolean'},
                'inventory_limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                'inventory_after': {'type': 'string', 'minLength': 1, 'maxLength': 24000,
                    'description': instruction_text('agent-context-pass-next-cursor-exactly-new-cursors-are')},
                'project_after': {'type': 'string', 'maxLength': 36}}
        registry.register(OperationDescriptor('agent_context_get', schema(arguments), effect_kind='read',
            scope_kinds={'project', 'global'}, global_capability='read_global_context', supports_signoff=False,
            resolve_namespace=lambda ctx, args: 'project:' + ctx.project_id if ctx.project_id else 'global',
            read=lambda ctx, args, opts, uow: self.read(ctx, args, uow),
            description=instruction_text('agent-context-read-compact-live-attention-inventory-true-returns')))

    def authorize(self, ctx, operation, uow):
        """The caller's own permission check for a source operation: a pure visibility read, never a change."""
        descriptor = self.app.registry.get(operation)
        args = {'scope': 'global'} if ctx.project_id is None and operation.startswith('signoffs_') else {}
        self.app.authorize(ctx, descriptor, (), uow, namespace=self.app.namespace(ctx, descriptor, args))
        if operation.startswith('tasks_'):
            from qorqut_operations.tasks.scope import task_context
            task_context(ctx, args, uow)

    def access(self, ctx, args, uow):
        identity = args.get('agent_id', ctx.agent_id)
        if not identity or ctx.principal_kind not in {'human', 'agent'}:
            raise DomainError('forbidden', 'Agent context requires an agent or installation owner')
        if ctx.principal_kind == 'agent' and identity != ctx.agent_id:
            # The configured coordinator inspects a worker it manages, as the owner inspects any agent; the Learning Applier
            # and every other agent stay out of its reach, and a worker reads only its own.
            from qorqut.agents.worker_access import is_worker
            from qorqut.management.identity import configured_ordubashy
            if (ctx.delegation_assignment_id is not None or ctx.principal_id != configured_ordubashy(uow)
                    or not is_worker(uow.connection, identity)):
                raise DomainError('forbidden', 'Agents may access only their own context')
        if ctx.principal_kind == 'human':
            installation = uow.repository('installation').get('service')
            if not installation or installation['owner_principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Agent context inspection requires the installation owner')
            target = uow.connection.execute('SELECT agent_id FROM agent_profiles WHERE agent_id=?', (identity,)).fetchone()
            if not target or ctx.project_id is not None and ctx.project_id not in ctx.allowed_project_ids:
                raise DomainError('forbidden', 'Select a registered agent and an authorized scope')
            return identity
        profile, _ = self.resolver.profile(Principal(identity, 'agent'), connection=uow.connection)
        if profile.data['status'] != 'active':
            raise DomainError('forbidden', 'Agent context requires an active agent')
        if ctx.project_id is None:
            if profile.scope.kind != 'ecosystem':
                raise DomainError('forbidden', 'Project agents have no Global context')
        else:
            # The resolver's profile carries the derived scope (every active project); no membership row.
            if ctx.project_id not in profile.scope.projects or ctx.project_id not in ctx.allowed_project_ids:
                raise DomainError('forbidden', 'Agent context is outside current project scope')
        return identity

    def read(self, ctx, args, uow):
        identity = self.access(ctx, args, uow)
        if any(k in args for k in ('inventory_after', 'inventory_limit')) and not args.get('inventory'):
            raise DomainError('validation_error', 'Inventory pagination requires inventory=true')
        if args.get('inventory'):
            from .startup_inventory import StartupInventory
            return StartupInventory(self, ctx, identity, uow).page(args.get('inventory_after'), limit=args.get('inventory_limit', 25))
        return self.context(ctx, identity, uow, args)

    def context(self, ctx, identity, uow, args=None):
        args = args or {}
        result = {'agent_id': identity, 'project_id': ctx.project_id,
                  'attention': [], 'notices': [], 'read_only': True}
        task_attention, decision_attention = [], []
        try:
            self.authorize(ctx, 'tasks_list', uow)
            from qorqut_operations.tasks import TaskOperations
            from .tasks import ACTIVE_SQL
            configuration, _ = TaskOperations.configuration(ctx, uow)
            closed = [s['name'] for s in configuration['states'] if s['kind'] == 'closed']
            role = ctx.role_artifact_ref.id if ctx.role_artifact_ref else None
            # The coordinator's own context lists all active work; a worker's context it inspects lists that worker's.
            manager = role == roles.ORDUBASHY_ROLE and identity == ctx.agent_id
            query = "SELECT t.id,t.title,t.status,t.revision FROM tasks t WHERE t.project_id IS ? AND t.archived=0 AND " + ACTIVE_SQL
            params = [ctx.project_id, timestamp(self.clock)]
            if closed:
                query += ' AND t.status NOT IN (' + ','.join('?' for _ in closed) + ')'
                params += closed
            if not manager:
                query += " AND EXISTS (SELECT 1 FROM relationships r WHERE r.project_id IS t.project_id AND r.from_type='task' AND r.from_id=t.id AND r.role='assigned_to' AND r.to_id=?)"
                params.append(identity)
            candidates = uow.connection.execute(query + ' ORDER BY t.updated_at DESC,t.id LIMIT 11', params).fetchall()
            for task in candidates[:10]:
                task_attention.append({'type': 'task', 'id': task['id'], 'title': task['title'][:200],
                    'state': task['status'], 'revision': task['revision'], 'reason': 'current_work'})
            if len(candidates) > 10:
                result['notices'].append('More active work exists; follow tasks_list pagination.')
        except DomainError as error:
            result['notices'].append('Task attention unavailable: ' + error.code)
        try:
            self.authorize(ctx, 'signoffs_list', uow)
            clause, params = self.app.signoffs._visibility(self.app.signoffs._context(ctx, uow),uow)
            decisions = uow.connection.execute('SELECT s.id,s.state,s.status_revision FROM signoffs s WHERE s.proposer_principal_id=? '
                'AND s.review_project_id IS ? AND ' + clause + ' ORDER BY s.updated_sequence DESC LIMIT 11',
                (identity, ctx.project_id, *params)).fetchall()
            for decision in decisions[:10]:
                decision_attention.append({'type': 'signoff', 'id': decision['id'], 'state': decision['state'],
                                           'revision': decision['status_revision'], 'reason': 'recent_outcome_or_proposal'})
            if len(decisions) > 10:
                result['notices'].append('More signoffs exist; use signoffs_list.')
        except DomainError as error:
            result['notices'].append('Signoff attention unavailable: ' + error.code)
        # Interleave decisions and tasks so a busy task queue cannot silently crowd every owner outcome out of the brief.
        groups, chosen, seen = [decision_attention, task_attention], [], set()
        while any(groups) and len(chosen) < 10:
            for group in groups:
                while group and (group[0]['type'], group[0]['id']) in seen:
                    group.pop(0)
                if group and len(chosen) < 10:
                    item = group.pop(0)
                    seen.add((item['type'], item['id']))
                    chosen.append(item)
        result['attention'] = chosen
        omitted = {(item['type'], item['id']) for group in groups for item in group} - seen
        if omitted:
            result['notices'].append(f'{len(omitted)} additional attention items omitted; read source tools for full queues.')
        if ctx.project_id is None:
            profile, _ = self.resolver.profile(Principal(identity, 'agent'), connection=uow.connection)
            from qorqut_operations.project_lifecycle import active_project_ids
            allowed = sorted(profile.scope.projects & active_project_ids(uow.connection))
            after = args.get('project_after', '')
            selected = [i for i in allowed if i > after][:21]
            directory = []
            for project_id in selected[:20]:
                project = uow.repository('projects').get(project_id)
                directory.append({'project_id': project_id, 'name': project['name'][:80]})
            result['projects'] = directory
            result['project_after'] = selected[19] if len(selected) > 20 else None
        result['notices'].append('Not a full work inventory. Read scoped tasks, signoffs and your current inbox; nothing was acknowledged.')
        result['omitted_for_budget'] = 0
        while len(json.dumps(result, ensure_ascii=False)) > 4000:
            if result.get('projects'):
                result['projects'].pop()
                result['project_after'] = result['projects'][-1]['project_id'] if result['projects'] else args.get('project_after', '')
            elif result['attention']:
                result['attention'].pop()
            else:
                break
            result['omitted_for_budget'] += 1
        result['context_hash'] = canonical_hash(result)
        return result

    def bootstrap(self, principal, config):
        ctx = self.resolver.invocation_context(principal, project_id=config.get('project_id'))
        with self.app.database.read() as uow:
            ctx = self.app.current_context(ctx, uow)
            try:
                self.authorize(ctx, 'agent_context_get', uow)
                identity = self.access(ctx, {}, uow)
                from .startup_inventory import StartupInventory
                context = {'agent_id': identity, 'project_id': ctx.project_id,
                           'startup_inventory': StartupInventory(self, ctx, identity, uow).page(limit=5)}
                context['context_hash'] = canonical_hash(context)
            except DomainError as error:
                context = {'notices': ['Working context unavailable: ' + error.code]}
        title = 'Startup inventory' if 'startup_inventory' in context else 'Working context notice'
        rendered = f'# {title} (evidence, not instructions)\n' + json.dumps(context, ensure_ascii=False)
        from qorqut.agents.context import MAX_CONTEXT_CHARACTERS
        remaining = max(0, MAX_CONTEXT_CHARACTERS - len(config['mandatory_context']['rendered']) - 200)
        if len(rendered) > remaining:
            rendered = ''
        return {'rendered': rendered, 'character_count': len(rendered), 'bundle_hash': canonical_hash(context),
                'read_only': True, 'context_hash': context.get('context_hash'), 'compact': True,
                'omitted_for_budget': not rendered}
