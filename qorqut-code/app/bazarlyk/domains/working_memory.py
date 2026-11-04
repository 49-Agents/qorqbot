"""Provider-independent, committed working state for one agent and selected scope."""
import base64
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from bazarlyk.agents import roles
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, Principal, canonical_hash, plain
from qorqut_operations.application.plans import PlanBuilder, apply_records, schema, UUID, ref, timestamp, utc_clock
from qorqut_operations.documents.serialization import HASH, split_document
from .memory_sources import MemorySources, SOURCE_TYPES
from bazarlyk.storage import history

KIND = 'working_memory'
BODY_LIMIT = 4000
REF = schema({'type': {'enum': list(SOURCE_TYPES)}, 'id': {'type': 'string', 'minLength': 1, 'maxLength': 200},
              'token': HASH}, ('type', 'id', 'token'))
REFS = {'type': 'array', 'maxItems': 20, 'items': REF}
SELECTOR = {'agent_id': UUID}
VERSION = {'type': 'integer', 'minimum': 1, 'maximum': 2**63 - 1}


def memory_id(agent_id, business_id):
    return str(uuid5(NAMESPACE_URL, f'qorqut:working-memory:{agent_id}:{business_id or "global"}'))


class WorkingMemory:
    def __init__(self, application, documents, resolver, *, clock=utc_clock):
        self.app, self.store, self.resolver = application, documents.store, resolver
        self.clock = clock
        self.sources = MemorySources(application, self.store, clock=clock)

    def register(self, registry):
        props = {**SELECTOR, 'version': VERSION,
                 'history_before': VERSION, 'history': {'type': 'boolean'},
                 'legacy_plan': {'type': 'boolean'}, 'metadata_only': {'type': 'boolean'},
                 'observe': {'type': 'array', 'maxItems': 20, 'items': schema(
                     {'type': REF['properties']['type'], 'id': REF['properties']['id']}, ('type', 'id'))}}
        for name, arguments in [('working_memory_get', props), ('agent_context_get', {**SELECTOR,
                'inventory': {'type': 'boolean'},
                'inventory_limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
                'inventory_after': {'type': 'string', 'minLength': 1, 'maxLength': 24000,
                    'description': 'Pass next_cursor exactly. New cursors are short process-local handles bound to this agent, scope and page size; they expire or become unavailable after restart/eviction, then restart from the first page. Legacy cursors remain accepted.'},
                'business_after': {'type': 'string', 'maxLength': 36}})]:
            registry.register(OperationDescriptor(name, schema(arguments), effect_kind='read',
                scope_kinds={'business', 'global'}, global_capability='read_global_context', supports_signoff=False,
                resolve_namespace=lambda ctx, args: 'business:' + ctx.business_id if ctx.business_id else 'global',
                read=lambda ctx, args, opts, uow, op=name: self.read(op, ctx, args, uow),
                description=('Read committed own working memory or bounded historical revisions. metadata_only returns the current revision/hash '
                    'without body or evidence, for recovery when evidence is revoked or a file has drifted. Observe same-scope '
                    'source IDs to get freshness tokens; this does not acknowledge evidence or inbox events.' if name == 'working_memory_get'
                    else 'Read compact current working memory and live attention. inventory=true returns paginated own tasks (including snoozed), requested Signoffs and pending inbox summaries across authorized scopes; follow inventory_after from next_cursor. '
                    'No complete tool catalog, copied task bodies, inbox acknowledgement or scope mutation.')))
        registry.register(OperationDescriptor('working_memory_update', schema({**SELECTOR,
            'body': {'type': 'string', 'maxLength': BODY_LIMIT}, 'sources': REFS,
            'create_if_missing': {'type': 'boolean'}, 'expected_content_hash': HASH,
            'repair_observed_hash': HASH, 'legacy_plan_content_hash': HASH}, ('body', 'sources')), effect_kind='compound',
            scope_kinds={'business', 'global'}, global_capability='read_global_context', supports_signoff=False,
            prepare=self.prepare, apply=self.apply,
            resolve_namespace=lambda ctx, args: 'business:' + ctx.business_id if ctx.business_id else 'global',
            validate_targets=self.validate,
            description='Save own working handoff: focus, approach, findings, uncertainty and next step. '
                'Keep tasks/decisions in their owning records. Max 4000 characters; evidence must be same-scope. '
                'Existing saves require expected_revision and expected_content_hash; first save requires create_if_missing:true. '
                'Sources use tokens observed with working_memory_get, not guessed current revisions. '
                'If an own legacy plan exists in this scope, read it first and supply legacy_plan_content_hash on first save; '
                'its exact original bytes are preserved and legacy writes are retired. '
                'Do not copy credentials or project evidence into Global notes. Never claim pending work succeeded.'))

    def access(self, ctx, args, uow):
        identity = args.get('agent_id', ctx.agent_id)
        if not identity or ctx.principal_kind not in {'human', 'agent'}:
            raise DomainError('forbidden', 'Working memory requires an agent or installation owner')
        if ctx.principal_kind == 'agent' and identity != ctx.agent_id:
            raise DomainError('forbidden', 'Agents may access only their own working memory')
        if ctx.principal_kind == 'human':
            installation = uow.repository('installation').get('service')
            if not installation or installation['owner_principal_id'] != ctx.principal_id:
                raise DomainError('forbidden', 'Working memory inspection requires the installation owner')
            target = uow.connection.execute('SELECT agent_id FROM agent_profiles WHERE agent_id=?', (identity,)).fetchone()
            if not target or ctx.business_id is not None and ctx.business_id not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Select a registered agent and an authorized scope')
            # The owner can inspect retained history after agent deactivation or
            # removal from a business; that does not restore the agent's access.
            return identity
        profile, _ = self.resolver.profile(Principal(identity, 'agent'), connection=uow.connection)
        if profile.data['status'] != 'active':
            raise DomainError('forbidden', 'Working memory requires an active agent')
        if ctx.business_id is None:
            if profile.scope.kind != 'ecosystem':
                raise DomainError('forbidden', 'Project agents have no Global working memory')
        else:
            # The resolver's profile carries the derived scope (every active business); no membership row.
            if ctx.business_id not in profile.scope.businesses or ctx.business_id not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Working memory is outside current project scope')
        return identity

    def row(self, identity, business_id, uow):
        return uow.repository('documents', business_id=business_id).get(memory_id(identity, business_id))

    def legacy(self, identity, business_id, uow):
        from qorqut_operations.storage.repositories import decode_row
        rows = uow.connection.execute("SELECT * FROM documents WHERE kind='plan' AND owning_agent_id=? AND business_id IS ? LIMIT 2",
                                      (identity, business_id)).fetchall()
        if len(rows) > 1:
            raise DomainError('invalid_configuration', 'Multiple legacy plans require owner reconciliation')
        return decode_row(rows[0]) if rows else None

    def strict_bytes(self, row, uow):
        if row['availability'] != 'ready':
            raise DomainError('busy', 'Working memory has an unfinished application')
        raw = self.store.read_bytes(row, uow)
        if hashlib.sha256(raw).hexdigest() != row['content_hash'].removeprefix('sha256:'):
            raise DomainError('memory_drift', 'Working memory has uncommitted file changes; owner repair is required')
        if len(split_document(raw)[1]) > BODY_LIMIT:
            raise DomainError('memory_drift', 'Working memory exceeds its committed format limit')
        return raw

    def view(self, ctx, row, uow, *, historical=None):
        refs = historical['source_refs_json'] if historical else row['source_refs_json']
        observations = self.sources.check(ctx, refs, uow)
        if any(item['state'] == 'unavailable' for item in observations):
            raise DomainError('memory_sources_unavailable', 'Working memory evidence is unavailable; body withheld')
        raw = base64.b64decode(historical['file_bytes'], validate=True) if historical else self.strict_bytes(row, uow)
        digest = historical['content_hash'] if historical else row['content_hash']
        if hashlib.sha256(raw).hexdigest() != digest.removeprefix('sha256:'):
            raise DomainError('memory_drift', 'Working memory bytes do not match the committed version')
        return {'id': row['id'], 'agent_id': row['owning_agent_id'], 'business_id': row['business_id'],
                'revision': historical['version'] if historical else row['revision'],
                'content_hash': digest, 'body': split_document(raw)[1],
                'sources': plain(refs), 'observations': observations,
                'updated_at': historical['created_at'] if historical else row['updated_at']}

    def read(self, operation, ctx, args, uow):
        identity = self.access(ctx, args, uow)
        if args.get('metadata_only') and (args.get('version') or args.get('legacy_plan')):
            raise DomainError('validation_error', 'Metadata-only reads cannot also select historical text')
        if operation == 'agent_context_get':
            if any(k in args for k in ('inventory_after', 'inventory_limit')) and not args.get('inventory'):
                raise DomainError('validation_error', 'Inventory pagination requires inventory=true')
            if args.get('inventory'):
                from .startup_inventory import StartupInventory
                return StartupInventory(self, ctx, identity, uow).page(args.get('inventory_after'), limit=args.get('inventory_limit', 25))
            return self.context(ctx, identity, uow, args)
        row = self.row(identity, ctx.business_id, uow)
        result = {'document': None}
        if row:
            historical = None
            if args.get('version'):
                historical = history.working_memory_version(uow.connection, ctx.business_id, row['id'], args['version'])
                if historical is None:
                    raise DomainError('not_found', 'Resource does not exist')
            result['document'] = ({k: row[k] for k in ('id', 'business_id', 'revision', 'content_hash', 'availability')}
                                  if args.get('metadata_only') else self.view(ctx, row, uow, historical=historical))
            if args.get('legacy_plan'):
                # The legacy snapshot is private content too, not a metadata bypass.
                self.view(ctx, row, uow, historical=historical)
                first = uow.repository('working_memory_provenance', business_id=ctx.business_id).get(
                    {'document_id': row['id'], 'version': 1}, required=True)
                legacy = plain(first['legacy_plan_json'])
                if legacy:
                    raw = base64.b64decode(legacy['file_bytes'], validate=True)
                    if hashlib.sha256(raw).hexdigest() != legacy['content_hash'].removeprefix('sha256:'):
                        raise DomainError('memory_drift', 'Legacy snapshot does not match its committed hash')
                    legacy = {**legacy, 'body': split_document(raw)[1]}
                    del legacy['file_bytes']
                result['legacy_plan'] = legacy or None
            if args.get('history'):
                versions = uow.connection.execute('SELECT version,created_at FROM working_memory_provenance '
                    'WHERE document_id=? AND version<? ORDER BY version DESC LIMIT 21',
                    (row['id'], args.get('history_before', 2**63-1))).fetchall()
                result['history'] = [dict(v) for v in versions[:20]]
                result['history_before'] = versions[19]['version'] if len(versions) > 20 else None
        if 'observe' in args:
            result['observations'] = [self.sources.observe(ctx, source, uow) for source in args['observe']]
        return result

    def prepare(self, ctx, args, opts, uow):
        identity = self.access(ctx, args, uow)
        row = self.row(identity, ctx.business_id, uow)
        namespace = 'business:' + ctx.business_id if ctx.business_id else 'global'
        builder = PlanBuilder(ctx, 'working_memory_update', args, 'Save working memory', namespace=namespace,
            affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'})
        legacy_snapshot = {}
        if row:
            if row['revision'] != opts.expected_revision or args.get('expected_content_hash', '').removeprefix('sha256:') != row['content_hash'].removeprefix('sha256:'):
                raise DomainError('revision_conflict', 'Read current working memory before replacing it', current_revision=row['revision'])
            if args.get('repair_observed_hash'):
                if ctx.principal_kind != 'human':
                    raise DomainError('forbidden', 'Only the owner may reconcile memory file drift')
                observed = hashlib.sha256(self.store.read_bytes(row, uow)).hexdigest()
                if observed != args['repair_observed_hash'].removeprefix('sha256:'):
                    raise DomainError('revision_conflict', 'Memory file changed before repair')
            else:
                self.strict_bytes(row, uow)
        elif not args.get('create_if_missing') or opts.expected_revision is not None:
            raise DomainError('revision_conflict', 'First save requires create_if_missing:true and no expected_revision')
        else:
            legacy = self.legacy(identity, ctx.business_id, uow)
            if legacy:
                self.sources.authorize(ctx, 'plans_get', uow)
                raw = self.store.read_bytes(legacy, uow)
                digest = hashlib.sha256(raw).hexdigest()
                if (legacy['availability'] != 'ready' or digest != legacy['content_hash'].removeprefix('sha256:')
                        or digest != args.get('legacy_plan_content_hash', '').removeprefix('sha256:')):
                    raise DomainError('legacy_plan_conflict', 'Read the committed legacy plan and supply its content hash before importing; resolve native file drift first')
                legacy_snapshot = {k: legacy[k] for k in ('id', 'path', 'revision', 'content_hash')}
                legacy_snapshot['file_bytes'] = base64.b64encode(raw).decode()
                builder.record('documents', legacy['id'], legacy,
                    {'metadata_json': {**plain(legacy['metadata_json']), 'working_memory_id': memory_id(identity, ctx.business_id)}},
                    namespace=namespace)
                resource = self.store.file_ref(legacy['id'], legacy['path'], ctx.business_id, namespace=namespace)
                from qorqut_operations.contracts import ExpectedTarget
                builder.targets[resource] = ExpectedTarget(resource, content_hash=digest, exists=True)
        refs = args['sources']
        if legacy_snapshot and any(source['type'] == 'document' and source['id'] == legacy_snapshot['id'] for source in refs):
            raise DomainError('validation_error', 'The imported plan is preserved automatically; do not cite its retired document ID as current evidence')
        if len({(r['type'], r['id']) for r in refs}) != len(refs):
            raise DomainError('validation_error', 'Working-memory references must be unique by type and ID')
        for source in refs:
            self.sources.observe(ctx, source, uow)
        if row and not args.get('repair_observed_hash') and split_document(self.strict_bytes(row, uow))[1] == args['body'] and plain(row['source_refs_json']) == refs:
            builder.expect(ref('documents', row['id'], ctx.business_id, namespace), row)
            return builder.build()
        self.store.write(builder, uow, kind=KIND, identity=memory_id(identity, ctx.business_id), before=row,
            path=row['path'] if row else f'context/agent-memory/{identity}.md', body=args['body'],
            metadata={'title': 'Working memory', 'schema_version': 1}, agent_id=identity,
            expected_revision=opts.expected_revision, expected_content_hash=args.get('repair_observed_hash') or args.get('expected_content_hash'),
            namespace=namespace, source_refs=refs)
        document = next(c for c in builder.changes if c.resource.resource_type == 'documents' and c.resource.resource_id == memory_id(identity, ctx.business_id))
        version = row['revision'] + 1 if row else 1
        # Content becomes the shared history version when the verified file is finalised.
        builder.record('working_memory_provenance', {'document_id': document.resource.resource_id, 'version': version}, None,
            {'document_id': document.resource.resource_id, 'version': version, 'business_id': ctx.business_id,
             'content_hash': document.values['content_hash'],
             'source_refs_json': refs, 'legacy_plan_json': legacy_snapshot,
             'author_id': ctx.principal_id, 'created_at': builder.at}, namespace=namespace)
        return builder.build()

    def validate(self, ctx, plan, uow):
        self.access(ctx, plan.arguments, uow)
        if plan.arguments.get('legacy_plan_content_hash'):
            self.sources.authorize(ctx, 'plans_get', uow)
        for source in plan.arguments['sources']:
            self.sources.observe(ctx, source, uow)

    def apply(self, ctx, plan, uow):
        self.validate(ctx, plan, uow)
        apply_records(ctx, plan, uow)
        identity = self.access(ctx, plan.arguments, uow)
        row = self.row(identity, ctx.business_id, uow)
        # Idempotency receipts may be replayed after evidence access changes. Do
        # not persist private body text in the mutation's response envelope.
        return {'document_id': row['id'], 'revision': row['revision'],
                'content_hash': row['content_hash'], 'saved': True}

    def context(self, ctx, identity, uow, args=None):
        args = args or {}
        result = {'agent_id': identity, 'business_id': ctx.business_id, 'memory': None,
                  'attention': [], 'notices': [], 'read_only': True}
        task_attention, decision_attention = [], []
        try:
            self.sources.authorize(ctx, 'working_memory_get', uow)
            row = self.row(identity, ctx.business_id, uow)
            if row:
                historical = None
                if row['availability'] == 'applying':
                    historical = history.working_memory_version(uow.connection, ctx.business_id, row['id'])
                memory = self.view(ctx, row, uow, historical=historical)
                # Observations are presented once in attention, not repeated beside prose.
                result['memory'] = {k: v for k, v in memory.items() if k not in {'observations', 'sources'}}
                if historical:
                    result['memory']['pending_application'] = True
                    result['notices'].append(self.resolver.require_agentic_view().runtime_instructions().render("memory-pending"))
                result['attention'] = [o for o in memory['observations'] if o['state'] != 'unchanged']
            else:
                result['notices'].append('No working memory saved for this agent and scope.')
        except DomainError as error:
            result['notices'].append('Working memory unavailable: ' + error.code)
        if result['memory'] is None:
            legacy = uow.connection.execute("SELECT id FROM documents WHERE kind='plan' AND owning_agent_id=? AND business_id IS ? LIMIT 1",
                                            (identity, ctx.business_id)).fetchone()
            if legacy:
                result['notices'].append(self.resolver.require_agentic_view().runtime_instructions().render("memory-legacy-plan"))
        try:
            self.sources.authorize(ctx, 'tasks_list', uow)
            from qorqut_operations.tasks import TaskOperations
            from .tasks import ACTIVE_SQL
            configuration, _ = TaskOperations.configuration(ctx, uow)
            closed = [s['name'] for s in configuration['states'] if s['kind'] == 'closed']
            role = ctx.role_artifact_ref.id if ctx.role_artifact_ref else None
            manager = role == roles.ORDUBASHY_ROLE
            query = "SELECT t.id,t.title,t.status,t.revision FROM tasks t WHERE t.business_id IS ? AND t.archived=0 AND " + ACTIVE_SQL
            params = [ctx.business_id, timestamp(self.clock)]
            if closed:
                query += ' AND t.status NOT IN (' + ','.join('?' for _ in closed) + ')'
                params += closed
            if not manager:
                query += " AND EXISTS (SELECT 1 FROM relationships r WHERE r.business_id IS t.business_id AND r.from_type='task' AND r.from_id=t.id AND r.role='assigned_to' AND r.to_id=?)"
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
            self.sources.authorize(ctx, 'signoffs_list', uow)
            clause, params = self.app.signoffs._visibility(self.app.signoffs._context(ctx, uow),uow)
            decisions = uow.connection.execute('SELECT s.id,s.state,s.status_revision FROM signoffs s WHERE s.proposer_principal_id=? '
                'AND s.review_business_id IS ? AND ' + clause + ' ORDER BY s.updated_sequence DESC LIMIT 11',
                (identity, ctx.business_id, *params)).fetchall()
            for decision in decisions[:10]:
                decision_attention.append({'type': 'signoff', 'id': decision['id'], 'state': decision['state'],
                                           'revision': decision['status_revision'], 'reason': 'recent_outcome_or_proposal'})
            if len(decisions) > 10:
                result['notices'].append('More signoffs exist; use signoffs_list.')
        except DomainError as error:
            result['notices'].append('Signoff attention unavailable: ' + error.code)
        # Interleave evidence changes, decisions and tasks so a busy task queue
        # cannot silently crowd every owner outcome out of the brief.
        groups, chosen, seen = [result['attention'], decision_attention, task_attention], [], set()
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
        if ctx.business_id is None:
            profile, _ = self.resolver.profile(Principal(identity, 'agent'), connection=uow.connection)
            from qorqut_operations.business_lifecycle import active_business_ids
            allowed = sorted(profile.scope.businesses & active_business_ids(uow.connection))
            after = args.get('business_after', '')
            selected = [i for i in allowed if i > after][:21]
            directory = []
            for business_id in selected[:20]:
                biz = uow.repository('businesses').get(business_id)
                memory = self.row(identity, business_id, uow)
                directory.append({'business_id': business_id, 'name': biz['name'][:80],
                                  'memory_updated_at': memory['updated_at'] if memory else None})
            result['businesses'] = directory
            result['business_after'] = selected[19] if len(selected) > 20 else None
        result['notices'].append('Not a full work inventory. Read scoped tasks, signoffs and your current inbox; nothing was acknowledged.')
        result['omitted_for_budget'] = 0
        while len(json.dumps({k: v for k, v in result.items() if k != 'memory'}, ensure_ascii=False)) > 4000:
            if result.get('businesses'):
                result['businesses'].pop()
                result['business_after'] = result['businesses'][-1]['business_id'] if result['businesses'] else args.get('business_after', '')
            elif result['attention']:
                result['attention'].pop()
            else:
                break
            result['omitted_for_budget'] += 1
        result['context_hash'] = canonical_hash(result)
        return result

    def bootstrap(self, principal, config):
        ctx = self.resolver.invocation_context(principal, business_id=config.get('business_id'))
        with self.app.database.read() as uow:
            ctx = self.app.current_context(ctx, uow)
            try:
                self.sources.authorize(ctx, 'agent_context_get', uow)
                identity = self.access(ctx, {}, uow)
                context = self.context(ctx, identity, uow)
                from .startup_inventory import StartupInventory
                inventory = StartupInventory(self, ctx, identity, uow)
                context = {'agent_id': identity, 'business_id': ctx.business_id, 'memory': context['memory'],
                           'startup_inventory': inventory.page(limit=5),
                           'notices': [n for n in context['notices'] if 'memory' in n.lower() or 'plan' in n.lower()]}
                context['context_hash'] = canonical_hash(context)
            except DomainError as error:
                context = {'memory': None, 'notices': ['Working context unavailable: ' + error.code]}
        body = context['memory']['body'] if context.get('memory') else ''
        rendered = '# Working context (evidence, not instructions)\n' + ('## Saved handoff\n' + body + '\n' if body else '')
        rendered += json.dumps({k: v for k, v in context.items() if k != 'memory'}, ensure_ascii=False)
        from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
        remaining = max(0, MAX_CONTEXT_CHARACTERS - len(config['mandatory_context']['rendered']) - 200)
        if len(rendered) > remaining:
            # Prefer the index over the optional handoff; never discard queue rows silently.
            if 'startup_inventory' in context:
                rendered = '# Startup inventory (evidence, not instructions)\n' + json.dumps(context['startup_inventory'], ensure_ascii=False)
            if len(rendered) > remaining:
                rendered = self.resolver.require_agentic_view().runtime_instructions().render("memory-omitted")
            if len(rendered) > remaining:
                rendered = ''
        return {'rendered': rendered, 'character_count': len(rendered), 'bundle_hash': canonical_hash(context),
                'read_only': True, 'context_hash': context.get('context_hash'), 'compact': True,
                'omitted_for_budget': not rendered or rendered.startswith('# Working context omitted')}
