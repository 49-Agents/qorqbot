"""Small canonical management intake over messages and input Signoffs."""
import json

from bazarlyk.domains.conversations import SUMMARY, normalize_summary
from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError, new_id, plain
from bazarlyk.domains.common import PlanBuilder, schema, timestamp, utc_clock, ref, require_revision
from bazarlyk.domains.tasks.operations import task_namespace
from .identity import installation_owner, ordubashy_profile, require_coordinator
from bazarlyk.storage.repositories import decode_row
from dataclasses import replace
from datetime import datetime
import uuid

UUID = {'type': 'string', 'format': 'uuid'}
SCOPE = {'scope': {'enum': ['business', 'global']}}

# Shared owner-attention predicate. Immutable eligibility and the canonical
# Signoff lifecycle prevent a routing row from hiding protected approvals.
MANAGED_INPUT_SQL = """s.category='input_request' AND s.state='pending'
 AND json_extract(s.preview_json,'$.input.kind')='question'
 AND coalesce(json_extract(s.preview_json,'$.input.response_policy'),'owner_only')='management_allowed'
 AND EXISTS (SELECT 1 FROM management_routing r WHERE r.source_type='signoffs'
 AND r.source_id=s.id AND r.attention_destination='management' AND r.management_allowed=1)"""


class ManagementService:
    def __init__(self, application, inbox=None, *, clock=None):
        self.app = application
        self.inbox = inbox
        self.clock = clock or (inbox.clock if inbox else utc_clock)

    def register(self, registry=None):
        registry = registry or self.app.registry
        common = dict(scope_kinds=frozenset({'business', 'global'}), global_capability='read_global_context',
            resolve_namespace=task_namespace)
        registry.register(OperationDescriptor('management_list', schema({**SCOPE,
            'all_items': {'type': 'boolean'}, 'attention_destination': {'enum': ['management', 'owner']},
            'disposition': {'enum': ['pending', 'resolved', 'dismissed']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'cursor': {'type': 'string', 'maxLength': 200}}, ()),
            **common, effect_kind='read', supports_signoff=False, description='List authorized management intake with source links and actual attribution.', read=self.list))
        registry.register(OperationDescriptor('management_get', schema({**SCOPE, 'routing_id': UUID}, ('routing_id',)),
            **common, effect_kind='read', supports_signoff=False, description='Read one authorized management intake item.', read=self.get))
        registry.register(OperationDescriptor('management_route', schema({**SCOPE, 'source_type': {'enum': ['signoffs', 'agent_messages']},
            'source_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'attention_destination': {'enum': ['management', 'owner']},
            'escalation_reason': {'type': 'string', 'maxLength': 4000}, 'recommendation': {'type': 'string', 'maxLength': 8000},
            'deadline': {'type': ['string', 'null'], 'format': 'date-time'}, 'management_allowed': {'type': 'boolean'},
            'expected_routing_revision':{'type':'integer','minimum':1}},
            ('source_type', 'source_id', 'attention_destination')),
            **common, supports_signoff=False, description='Route one canonical message or input request to management or owner attention.', prepare=self.prepare_route, apply=self.apply_route))
        registry.register(OperationDescriptor('management_message_send', schema({**SCOPE, 'summary': SUMMARY, 'message': {'type': 'string', 'minLength': 1, 'maxLength': 16000},
            'escalation_reason': {'type': 'string', 'maxLength': 4000}, 'recommendation': {'type': 'string', 'maxLength': 8000}}, ('message',)),
            **common, supports_signoff=False, description='Send a durable management message to the exact configured coordinator.', prepare=self.prepare_message, apply=self.apply_message))
        registry.register(OperationDescriptor('management_coordinator_set', schema({'coordinator_id': UUID}, ('coordinator_id',)),
            scope_kinds=frozenset({'global'}), exposure=frozenset({'dashboard', 'worker'}), owner_only=True,
            global_capability='read_global_context', supports_signoff=False, description='Set the one installation management coordinator. The target must be an active Codex Ordubashy.',
            prepare=self.prepare_coordinator, apply=self.apply_coordinator))
        registry.register(OperationDescriptor('management_resolve', schema({**SCOPE, 'routing_id': UUID,
            'disposition': {'enum': ['resolved', 'dismissed']}},
            ('routing_id', 'disposition')), **common, supports_signoff=False,
            description='Resolve or dismiss a management route while retaining its immutable canonical source.',
            prepare=self.prepare_resolve, apply=self.apply_resolve))
        self.app.management = self
        return self

    @staticmethod
    def _coordinator(uow):
        # Management intake stays Codex-only; the private owner conversation
        # uses the same registered identity with require_codex=False.
        return require_coordinator(uow)

    def _scope(self, ctx, business_id, uow):
        if business_id is None:
            if ctx.business_id is not None:
                raise DomainError('forbidden','Global management items require Global scope')
            if 'read_global_context' not in ctx.global_capabilities and ctx.principal_id != self._owner(uow):
                raise DomainError('forbidden', 'Global management scope is unavailable')
        elif business_id not in ctx.allowed_business_ids:
            raise DomainError('forbidden', 'Management item is outside the selected scope')
        elif (ctx.business_id is not None and business_id != ctx.business_id) or not uow.connection.execute(
                'SELECT 1 FROM business_memberships m JOIN businesses b ON b.id=m.business_id '
                'WHERE m.principal_id=? AND m.business_id=? AND m.active=1 AND b.archived_at IS NULL',
                (ctx.principal_id,business_id)).fetchone():
            raise DomainError('forbidden','Management source membership is unavailable')

    @staticmethod
    def _owner(uow):
        return installation_owner(uow)

    def coordinator_dto(self, uow):
        installation = uow.repository('installation').get('service')
        identity = installation and installation['management_coordinator_id']
        profile = uow.connection.execute('SELECT p.display_name,p.active,a.status,a.profile_json FROM principals p LEFT JOIN agent_profiles a ON a.agent_id=p.id WHERE p.id=?', (identity,)).fetchone() if identity else None
        data = json.loads(profile['profile_json'] or '{}') if profile else {}
        valid = ordubashy_profile(uow, identity, require_codex=True)
        observed = uow.connection.execute('SELECT state,last_seen_at,runtime FROM agent_activity WHERE agent_id=? ORDER BY last_seen_at DESC LIMIT 1', (identity,)).fetchone() if identity else None
        now = self.clock()
        if isinstance(now,str):
            now = datetime.fromisoformat(now.replace('Z','+00:00'))
        fresh = False
        if observed and observed['last_seen_at']:
            try:
                seen = datetime.fromisoformat(observed['last_seen_at'].replace('Z', '+00:00'))
                fresh = 0 <= (now - seen).total_seconds() <= 180
            except (ValueError,TypeError):
                pass
        observed_live = bool(fresh and observed['runtime']=='codex' and observed['state'] in {'running','idle'})
        return {'principal_id': identity, 'display_name': profile['display_name'] if profile else None,
                'role_id': data.get('role', {}).get('id') if profile else None, 'runtime': data.get('runtime') if profile else None,
                'active': bool(profile and profile['active'] and profile['status'] == 'active'), 'available': valid and observed_live,
                'configured': valid, 'observed_state': observed['state'] if observed and (fresh or observed['state']=='offline') else 'unobserved',
                'last_seen_at': observed['last_seen_at'] if observed else None, 'fresh': fresh,
                'status': 'available' if valid and observed_live else ('configured_unobserved' if valid else ('unconfigured' if not identity else 'unavailable'))}

    def _source(self, source_type, source_id, ctx, uow):
        if source_type == 'signoffs':
            row = uow.connection.execute('SELECT * FROM signoffs WHERE id=?', (source_id,)).fetchone()
            business_id = row['review_business_id'] if row else None
        else:
            row = uow.connection.execute('SELECT * FROM agent_messages WHERE id=?', (source_id,)).fetchone()
            business_id = row['business_id'] if row else None
        if not row:
            raise DomainError('not_found', 'Management source does not exist')
        self._scope(ctx, business_id, uow)
        if source_type == 'signoffs':
            if row['category'] != 'input_request' or row['state'] != 'pending' or row['superseded_by_id']:
                raise DomainError('invalid_transition', 'Only pending input requests can enter management intake')
        return decode_row(row), business_id

    def prepare_route(self, ctx, args, options, uow):
        source, business_id = self._source(args['source_type'], args['source_id'], ctx, uow)
        coordinator = self._coordinator(uow)
        existing = uow.connection.execute('SELECT * FROM management_routing WHERE source_type=? AND source_id=?', (args['source_type'], args['source_id'])).fetchone()
        requester = source['proposer_principal_id'] if args['source_type'] == 'signoffs' else source['current_sender_id']
        if ctx.principal_id not in {requester,self._owner(uow)} and (ctx.principal_id != coordinator or not existing or existing['coordinator_id'] != coordinator):
            raise DomainError('forbidden', 'Only the requester or configured coordinator can route this source')
        if existing and args.get('expected_routing_revision') != existing['revision']:
            raise DomainError('revision_conflict','Read the current management routing revision before changing attention')
        if args['attention_destination']=='owner' and not args.get('escalation_reason','').strip():
            raise DomainError('validation_error','Owner escalation needs a concrete reason')
        if args['source_type'] == 'signoffs':
            form = source['preview_json']['input']
            if form and (form.get('kind') == 'request' or form.get('response_policy', 'owner_only') == 'owner_only') and args['attention_destination'] == 'management':
                raise DomainError('forbidden', 'Human-only input requests must remain owner attention')
        builder = PlanBuilder(ctx, 'management_route', args, 'Route canonical management intake',
            namespace=task_namespace(ctx,args), affected_scope={'kind': 'business', 'business_id': business_id})
        builder.expect(ref(args['source_type'], args['source_id'], business_id), source)
        if existing:
            builder.expect(ref('management_routing',existing['id'],None),dict(existing))
        return builder.build()

    def auto_route_input(self, ctx, row, uow):
        form = row['preview_json'].get('input', {})
        if (ctx.principal_kind!='agent' or not ctx.home_business_id or form.get('kind') != 'question'
                or form.get('response_policy','owner_only') != 'management_allowed' or not row.get('review_business_id')):
            return
        try:
            coordinator = self._coordinator(uow)
        except DomainError:
            return
        if ctx.principal_id==coordinator:
            return
        # Binding must include the requester's business; offline presence is fine.
        if self.inbox is not None:
            try: self.inbox.messages.peer(ctx,coordinator,uow)
            except DomainError: return
        now = timestamp(self.clock)
        inserted = uow.connection.execute('''INSERT OR IGNORE INTO management_routing
            (id,source_type,source_id,business_id,coordinator_id,attention_destination,
             management_allowed,created_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', (new_id(), 'signoffs', row['id'], row['review_business_id'], coordinator,
             'management', 1, row['proposer_principal_id'], now, now)).rowcount
        if inserted and self.inbox is not None:
            event = uow.connection.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) '
                'VALUES(?,?,?,?,?,?,?)',(row['review_business_id'],'signoffs',row['id'],ctx.principal_id,
                'management_requested','Management input requested',now)).lastrowid
            uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)',(coordinator,event))

    def apply_route(self, ctx, plan, uow):
        args = plain(plan.arguments)
        self.prepare_route(ctx,args,None,uow)  # Recheck actor, response policy and routing CAS at apply.
        source, business_id = self._source(args['source_type'], args['source_id'], ctx, uow)
        expected = next((item for item in plan.expected_targets if item.resource.resource_type == args['source_type']), None)
        if args['source_type'] == 'signoffs' and expected and source['status_revision'] != expected.revision:
            raise DomainError('revision_conflict', 'Input request changed; route it again from the current record')
        if args['attention_destination'] == 'management' and args['source_type'] == 'signoffs' and (source['preview_json']['input'].get('kind') == 'request' or source['preview_json']['input'].get('response_policy', 'management_allowed') == 'owner_only'):
            raise DomainError('forbidden', 'Human-only input requests must remain owner attention')
        coordinator = self._coordinator(uow)
        existing = uow.connection.execute('SELECT * FROM management_routing WHERE source_type=? AND source_id=?', (args['source_type'], args['source_id'])).fetchone()
        now = timestamp(self.clock)
        identity = existing['id'] if existing else new_id()
        values = (identity, args['source_type'], args['source_id'], business_id, coordinator, args['attention_destination'], args.get('escalation_reason', ''), args.get('recommendation', ''), args.get('deadline'), int(args.get('management_allowed', args['attention_destination'] == 'management')), ctx.principal_id, now)
        if existing:
            uow.connection.execute('''UPDATE management_routing SET coordinator_id=?,attention_destination=?,escalation_reason=?,recommendation=?,deadline=?,management_allowed=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=?''', values[4:10] + (now,ctx.principal_id,identity))
        else:
            uow.connection.execute('''INSERT INTO management_routing(id,source_type,source_id,business_id,coordinator_id,attention_destination,escalation_reason,recommendation,deadline,management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', values[:10] + (values[10], now, now))
        if ctx.principal_id == coordinator and args['attention_destination'] == 'owner' and args['source_type'] == 'signoffs' and getattr(self.app,'attention',None):
            from bazarlyk.inbox.attention import present_created
            present_created(self.app,ctx,{'reason':args.get('escalation_reason','')},
                {'type':'signoff','id':args['source_id'],'business_id':business_id},uow)
        return {'routing': self._view(uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (identity,)).fetchone(), uow)}

    def prepare_message(self, ctx, args, options, uow):
        normalize_summary(args.get('summary'))
        if ctx.principal_kind != 'agent':
            raise DomainError('forbidden', 'Management intake is for business agents')
        coordinator = self._coordinator(uow)
        if coordinator == ctx.principal_id:
            raise DomainError('validation_error', 'Coordinator cannot send management messages to itself')
        if self.inbox is None:
            raise DomainError('operation_unavailable', 'Inbox message service is not installed')
        self.inbox.messages.peer(ctx, coordinator, uow)
        return PlanBuilder(ctx, 'management_message_send', args, 'Send management message',
            namespace=task_namespace(ctx,args), affected_scope={'kind': 'business', 'business_id': ctx.business_id}).build()

    def _validate_coordinator(self, identity, uow):
        if not ordubashy_profile(uow, identity, require_codex=True):
            raise DomainError('validation_error', 'Coordinator must be an active Codex Ordubashy profile')

    def prepare_coordinator(self, ctx, args, options, uow):
        if ctx.principal_kind != 'human' or self._owner(uow) != ctx.principal_id or ctx.business_id is not None:
            raise DomainError('forbidden', 'Only the installation owner can configure the global coordinator')
        self._validate_coordinator(args['coordinator_id'], uow)
        return PlanBuilder(ctx, 'management_coordinator_set', args, 'Configure management coordinator', namespace='global', affected_scope={'kind': 'global'}).build()

    def apply_coordinator(self, ctx, plan, uow):
        if ctx.principal_kind != 'human' or self._owner(uow) != ctx.principal_id:
            raise DomainError('forbidden', 'Only the installation owner can configure the global coordinator')
        identity = plain(plan.arguments)['coordinator_id']
        self._validate_coordinator(identity, uow)
        uow.connection.execute("UPDATE installation SET management_coordinator_id=?, revision=revision+1, updated_at=? WHERE id='service'", (identity, timestamp(self.clock)))
        return {'coordinator_id': identity}

    def prepare_resolve(self, ctx, args, options, uow):
        row = uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (args['routing_id'],)).fetchone()
        if not row or not self._authorized(self.app.current_context(ctx, uow), row, args, uow):
            raise DomainError('not_found', 'Management intake is unavailable')
        if row['source_type']!='agent_messages' or ctx.principal_id not in {row['coordinator_id'],self._owner(uow)}:
            raise DomainError('forbidden','Only management or the owner can resolve a message; Signoffs retain their own lifecycle')
        require_revision(dict(row), options.expected_revision)
        builder = PlanBuilder(ctx, 'management_resolve', args, 'Resolve management intake', namespace=task_namespace(ctx,args), affected_scope={'kind': 'business', 'business_id': row['business_id']})
        builder.record('management_routing', row['id'], decode_row(row), {'disposition': args['disposition'], 'updated_by':ctx.principal_id,'updated_at': timestamp(self.clock)}, action='update')
        return builder.build()

    def apply_resolve(self, ctx, plan, uow):
        args = plain(plan.arguments)
        row = uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (args['routing_id'],)).fetchone()
        expected = next(t.revision for t in plan.expected_targets if t.resource.resource_type=='management_routing')
        if not row or row['revision'] != expected or not self._authorized(ctx, row, args, uow):
            raise DomainError('revision_conflict', 'Management intake changed; read its current state')
        if row['source_type']!='agent_messages' or ctx.principal_id not in {row['coordinator_id'],self._owner(uow)}:
            raise DomainError('forbidden','Only management or the owner can resolve a message')
        now = timestamp(self.clock)
        uow.connection.execute('UPDATE management_routing SET disposition=?,updated_at=?,updated_by=?,revision=revision+1 WHERE id=? AND revision=?', (args['disposition'],now,ctx.principal_id,row['id'],expected))
        return {'routing': self._view(uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (row['id'],)).fetchone(), uow)}

    def apply_message(self, ctx, plan, uow):
        args = plain(plan.arguments)
        coordinator = self._coordinator(uow)
        peer = self.inbox.messages.peer(ctx, coordinator, uow)
        now, identity = timestamp(self.clock), new_id()
        event = uow.connection.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)', (ctx.business_id, 'agent_messages', identity, ctx.principal_id, 'direct_message', args['message'][:160], now)).lastrowid
        uow.connection.execute('INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (identity, event, ctx.business_id, ctx.principal_id, peer.principal_id, None, identity, args['message'], now, normalize_summary(args.get('summary')), ctx.principal_id, peer.principal_id))
        uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (coordinator, event))
        self._insert_route(uow, identity, ctx.business_id, coordinator, ctx.principal_id, args, now)
        return {'message_id': identity, 'thread_id': identity,
                'routing': self._view(uow.connection.execute('SELECT * FROM management_routing WHERE source_id=?', (identity,)).fetchone(), uow)}

    def _insert_route(self, uow, source_id, business_id, coordinator, actor, args, now):
        uow.connection.execute('''INSERT INTO management_routing(id,source_type,source_id,business_id,coordinator_id,attention_destination,escalation_reason,recommendation,management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (new_id(), 'agent_messages', source_id, business_id, coordinator, 'management', args.get('escalation_reason', ''), args.get('recommendation', ''), 1, actor, now, now))

    def _view(self, row, uow):
        if row is None:
            raise DomainError('not_found', 'Management intake does not exist')
        result = dict(row)
        business = uow.repository('businesses').get(row['business_id']) if row['business_id'] else None
        result.update(business_slug=business['slug'] if business else None,
                      business_name=business['name'] if business else 'Global')
        for key in ('created_by', 'coordinator_id','updated_by'):
            principal = uow.repository('principals').get(row[key])
            result[key + '_display_name'] = principal['display_name'] if principal else None
        result['source'] = {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['business_id'],
                            'business_slug': result['business_slug']}
        if row['source_type'] == 'signoffs':
            source = decode_row(uow.connection.execute('SELECT * FROM signoffs WHERE id=?', (row['source_id'],)).fetchone())
            if source:
                result['source_state'] = source['state']
                result['source_title'] = source['preview_json'].get('title')
                result['decision'] = source['decision_json']
                result['source_status_revision'] = source['status_revision']
                result['disposition'] = 'pending' if source['state']=='pending' and not source['superseded_by_id'] else 'resolved'
                result['resolved_by'] = (source['decision_json'] or {}).get('by')
        else:
            source = uow.connection.execute('SELECT body,thread_id,created_at,summary FROM agent_messages WHERE id=?', (row['source_id'],)).fetchone()
            if source:
                result['source_state'] = 'message'
                result['source_title'] = source['body'][:160]
                result['thread_id'] = source['thread_id']
                result['body'] = source['body']
                result['message_summary'] = source['summary']
                result['resolved_by'] = row['updated_by'] if row['disposition']!='pending' else None
        return result

    def _authorized(self, ctx, row, args, uow):
        self._scope(ctx, row['business_id'], uow)
        if row['source_type']=='signoffs':
            selected = replace(ctx,business_id=row['business_id'],review_business_id=row['business_id'])
            descriptor = self.app.registry.get('signoffs_get')
            self.app.authorize(selected,descriptor,(),uow,
                namespace='business:'+row['business_id'] if row['business_id'] else 'global')
        if ctx.principal_id == row['coordinator_id']:
            return True
        if ctx.principal_kind == 'human' and (ctx.principal_id == self._owner(uow) or row['business_id'] and uow.connection.execute('SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1 AND is_owner=1', (row['business_id'], ctx.principal_id)).fetchone()):
            return True
        if row['source_type'] == 'signoffs':
            source = uow.connection.execute('SELECT proposer_principal_id FROM signoffs WHERE id=?', (row['source_id'],)).fetchone()
        else:
            source = uow.connection.execute('SELECT current_sender_id FROM agent_messages WHERE id=?', (row['source_id'],)).fetchone()
        return bool(source and source[0] == ctx.principal_id)

    def list(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        rows, params, clauses = [], [], []
        disposition = args.get('disposition','pending')
        if ctx.business_id:
            clauses.append('r.business_id=?'); params.append(ctx.business_id)
        else:
            scopes = list(ctx.allowed_business_ids)
            business_clause = 'r.business_id IN ('+','.join('?' for _ in scopes)+')' if scopes else '0'
            clauses.append('(r.business_id IS NULL OR '+business_clause+')'); params.extend(scopes)
        if ctx.principal_kind=='agent':
            clauses.append('(r.coordinator_id=? OR s.proposer_principal_id=? OR m.current_sender_id=?)')
            params.extend([ctx.principal_id]*3)
        destination = args.get('attention_destination')
        if not destination and ctx.principal_kind=='human' and not args.get('all_items'):
            destination='owner'
        if destination:
            clauses.append('r.attention_destination=?'); params.append(destination)
        derived="CASE WHEN r.source_type='signoffs' THEN CASE WHEN s.state='pending' AND s.superseded_by_id IS NULL THEN 'pending' ELSE 'resolved' END ELSE r.disposition END"
        clauses.append(derived+'=?'); params.append(disposition)
        cursor = args.get('cursor')
        if cursor:
            try:
                at,identity=cursor.split('|')
                datetime.fromisoformat(at); uuid.UUID(identity)
            except (ValueError,TypeError,AttributeError) as exc:
                raise DomainError('validation_error','Invalid management cursor') from exc
            clauses.append('(r.updated_at,r.id)<(?,?)'); params.extend([at,identity])
        limit=args.get('limit',50)
        candidates=uow.connection.execute('SELECT r.* FROM management_routing r LEFT JOIN signoffs s '
            "ON r.source_type='signoffs' AND s.id=r.source_id LEFT JOIN agent_messages m "
            "ON r.source_type='agent_messages' AND m.id=r.source_id WHERE "+' AND '.join(clauses)+
            ' ORDER BY r.updated_at DESC,r.id DESC LIMIT ?',[*params,limit+1]).fetchall()
        for row in candidates[:limit]:
            try:
                authorized = self._authorized(ctx, row, args, uow)
            except DomainError:
                authorized = False
            if not authorized:
                continue
            value=self._view(row,uow); value.pop('body',None)
            rows.append(value)
        last=candidates[limit-1] if len(candidates)>limit else None
        next_cursor = f"{last['updated_at']}|{last['id']}" if last else None
        return {'items': rows, 'next_cursor': next_cursor, 'coordinator': self.coordinator_dto(uow)}

    def get(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        row = uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (args['routing_id'],)).fetchone()
        if not row or not self._authorized(ctx, row, args, uow):
            raise DomainError('not_found', 'Management intake is unavailable')
        return {'routing': self._view(row, uow), 'coordinator': self.coordinator_dto(uow)}

    def startup(self, principal, config):
        ctx = self.app.policy.agent_resolver.invocation_context(principal,business_id=config.get('business_id'))
        try:
            with self.app.database.read() as uow:
                ctx=self.app.current_context(ctx,uow)
                descriptor=self.app.registry.get('management_list')
                args={'limit':10,**({'scope':'global'} if not ctx.business_id else {})}
                self.app.authorize(ctx,descriptor,(),uow,namespace=task_namespace(ctx,args))
                result=self.list(ctx,args,None,uow)
                health=result['coordinator']
                lines=['MANAGEMENT INTAKE: '+health['status']+'; coordinator '+str(health['principal_id'] or 'not configured')+'.']
                for item in result['items']:
                    lines.append(f"{item['id']}: {item.get('source_title','Request')[:150]}; {item['attention_destination']}; "
                        f"source {item['source_type']}/{item['source_id']}; deadline {item['deadline'] or 'none'}")
                if result['next_cursor']:
                    lines.append('More requests: use management_list and follow next_cursor.')
                return '\n'.join(lines)
        except DomainError:
            return ''
