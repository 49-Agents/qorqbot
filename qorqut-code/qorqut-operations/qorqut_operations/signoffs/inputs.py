"""Typed owner input requests; answering never authorizes a prepared effect."""
from dataclasses import replace
from datetime import datetime, timezone

from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.conversations import SUMMARY
from qorqut_operations.storage.repositories import decode_row
from .ports import ABSENT

FOCUS_UNAVAILABLE = "The Focus attention domain is not installed"
MANAGEMENT_UNAVAILABLE = "The management domain is not installed"

TEXT = {"type": "string", "minLength": 1, "maxLength": 8000, "pattern": r"\S"}
CHOICE = schema({"id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,40}$"},
    "label": {"type": "string", "maxLength": 80, "pattern": r"^\S+(?:\s+\S+)?$"},
    "explanation": TEXT}, ("id", "label", "explanation"))
ATTENTION = schema({"notify": {"type": "boolean"}, "urgent": {"type": "boolean"},
    "blocking": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 2000},
    "deadline": {"type": ["string", "null"], "format": "date-time"}})
FORM = schema({"title": {**TEXT, "maxLength": 200}, "explanation": TEXT,
    "kind": {"enum": ["question", "request"]}, "selection_mode": {"enum": ["none", "single", "multiple"]},
    "response_policy": {"enum": ["management_allowed", "owner_only"]},
    "choices": {"type": "array", "items": CHOICE, "maxItems": 26},
    "selection_required": {"type": "boolean"}, "notes_required": {"type": "boolean"}},
    ("title", "explanation", "kind", "selection_mode"))
SCOPE = {"scope": {"enum": ["business", "global"]}}


def normalize_attention(values):
    result = {"notify": False, "urgent": False, "blocking": False, "reason": "", "deadline": None, **values}
    if (result['urgent'] or result['blocking']) and not result['reason'].strip():
        raise DomainError('validation_error', 'Urgent or blocking signoffs require a reason')
    if result['deadline']:
        result['deadline'] = datetime.fromisoformat(result['deadline'].replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    return result


def validate_form(form):
    form = {"choices": [], "selection_required": False, "notes_required": False, "response_policy": "management_allowed", **form}
    choices, mode = form['choices'], form['selection_mode']
    if len({c['id'] for c in choices}) != len(choices):
        raise DomainError('validation_error', 'Choice IDs must be unique')
    if (mode == 'none' and (choices or form['selection_required'])) or (mode != 'none' and len(choices) < 2):
        raise DomainError('validation_error', 'Selection mode and choices disagree')
    return form


def validate_response(form, notes, selected):
    if not isinstance(notes, str) or len(notes) > 8000:
        raise DomainError('validation_error', 'Notes must contain at most 8000 characters')
    if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected) or len(selected) != len(set(selected)):
        raise DomainError('validation_error', 'Selected choices must be unique IDs')
    choices = {c['id'] for c in form['choices']}
    if set(selected) - choices or (form['selection_mode'] == 'single' and len(selected) > 1):
        raise DomainError('validation_error', 'Select only choices belonging to this question')
    # A written response is a valid answer even when the proposer requested a
    # choice. This lets the owner explain an answer that is not represented by
    # the predefined choices; consumers should use the notes as the answer.
    if form['selection_required'] and not selected and not notes.strip():
        raise DomainError('validation_error', 'Select an answer or provide a written answer before submitting')
    if form['notes_required'] and not notes.strip():
        raise DomainError('validation_error', 'A written answer is required')
    return notes, sorted(selected)


class InputRequests:
    def __init__(self, signoffs):
        self.service = signoffs

    def descriptors(self):
        focus = self.service.ports.focus
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context',
            resolve_namespace=lambda c,a: 'global' if a.get('scope') == 'global' else 'business:' + (c.business_id or ''))
        descriptors = []
        for name, properties, required, description in (
            ('signoffs_input_create', {'request_key': {"type": "string", "minLength": 1, "maxLength": 200},
                'form': FORM, 'attention': ATTENTION, **({'focus': schema(focus.fields)} if focus is not ABSENT else {})}, ('request_key', 'form'),
                'Ask the owner a question or request input. Use a stable request_key for the same outstanding question. Answers are read through signoffs_get/list; this never runs an action.'),
            ('signoffs_attention_set', {'signoff_id': {"type": "string", "format": "uuid"}, 'attention': ATTENTION},
                ('signoff_id', 'attention'), 'Set notification urgency/blocking for your pending signoff. Active global Ordubashy may also reprioritize other agents\' accessible pending signoffs. This never approves, answers, or executes a signoff. Include a reason; urgent/blocking notifications repeat hourly.'),
            ('signoffs_input_cancel', {'signoff_id': {"type": "string", "format": "uuid"}}, ('signoff_id',),
                'Withdraw your pending input request while retaining its history.')):
            descriptors.append(OperationDescriptor(name, schema({**SCOPE, **properties}, required), **common,
                description=description, prepare=lambda c,a,o,u,n=name: self.prepare(n,c,a,o,u), apply=self.apply))
        # Delegated management answers need the management domain; without it the operation is not installed.
        if self.service.ports.management is ABSENT:
            return tuple(descriptors)
        descriptors.append(OperationDescriptor('signoffs_input_answer_delegated', schema({**SCOPE,
            'signoff_id': {"type": "string", "format": "uuid"},
            'proposal_revision': {"type": "integer", "minimum": 1}, 'payload_hash': {"type": "string", "minLength": 1},
            'expected_status_revision': {"type": "integer", "minimum": 1},
            'summary': SUMMARY, 'notes': {"type": "string", "maxLength": 8000}, 'selected_choices': {"type": "array", "items": {"type": "string"}, "uniqueItems": True}},
            ('signoff_id','proposal_revision','payload_hash','expected_status_revision')),
            **common, description='Answer an eligible management-routed input request as the configured Codex Ordubashy. This never approves or executes a protected action.',
            prepare=self.prepare_delegated, apply=self.apply_delegated))
        return tuple(descriptors)

    def prepare(self, name, ctx, args, options, uow):
        if args.get('scope') == 'global':
            ctx = replace(ctx, business_id=None, review_business_id=None)
        ctx = self.service._context(ctx, uow)
        scope = {'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'}
        builder = PlanBuilder(ctx, name, args, 'Request owner input',
            namespace='business:' + ctx.business_id if ctx.business_id else 'global', affected_scope=scope)
        if name == 'signoffs_input_create':
            if 'focus' in args:
                self.service.ports.require('focus', FOCUS_UNAVAILABLE).creation_access(self.service.app,ctx,args['focus'],uow)
            form = validate_form(args['form'])
            attention = normalize_attention(args.get('attention', {}))
            row = decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE proposer_principal_id=? AND review_business_id IS ? AND request_key=? AND state='pending' AND superseded_by_id IS NULL",
                (ctx.principal_id, ctx.business_id, args['request_key'])).fetchone())
            if row and row['preview_json'].get('input') != form:
                raise DomainError('request_conflict', 'This request key already has a different pending question; withdraw it before replacing')
            identity = row['id'] if row else new_id()
            builder.record('signoffs', identity, row, {'form': form, 'request_key': args['request_key'], **({'attention': attention} if not row or 'attention' in args else {})}, action='input_create')
        else:
            row = self.service._row(ctx, args['signoff_id'], uow)
            self.authorize_update(ctx, row, name, uow)
            if name == 'signoffs_input_cancel' and row['category'] != 'input_request':
                raise DomainError('invalid_transition', 'This operation only withdraws input requests')
            values = {'attention': normalize_attention(args['attention'])} if name == 'signoffs_attention_set' else {}
            builder.record('signoffs', row['id'], row, values, action='attention_set' if values else 'input_cancel')
        return builder.build()

    def authorize_update(self, ctx, row, name, uow):
        if ctx.principal_kind == 'human':
            self.service._context(ctx, uow, owner=True)
        elif row['proposer_principal_id'] != ctx.principal_id:
            profile = uow.repository('agent_profiles', business_id=None).get(ctx.principal_id)
            proposer = uow.repository('principals').get(row['proposer_principal_id'])
            delegated = (name == 'signoffs_attention_set' and ctx.principal_kind == 'agent'
                and profile and profile['status'] == 'active'
                and profile['profile_json']['role']['id'] == 'ordubashy'
                and profile['profile_json']['scope']['kind'] == 'ecosystem'
                and proposer and proposer['kind'] == 'agent')
            if not delegated:
                raise DomainError('forbidden', 'Only the proposer or authorized Ordubashy can update this signoff attention')
        if row['state'] != 'pending' or row['superseded_by_id']:
            raise DomainError('invalid_transition', 'Only pending signoffs can be updated')

    def attention(self, identity, values, uow, *, actor_id=None):
        now = self.service.clock()
        # Editing an unchanged alert does not reset its reminder timer or owner snooze.
        old = uow.connection.execute('SELECT * FROM signoff_attention WHERE signoff_id=?', (identity,)).fetchone()
        if old and all(old[key] == (int(value) if isinstance(value, bool) else value) for key,value in values.items()):
            return False
        immediate = values['notify'] and (values['urgent'] or values['blocking'])
        was_immediate = old and old['notify'] and (old['urgent'] or old['blocking'])
        next_at = old['next_notify_at'] if was_immediate and immediate else now if immediate else None
        uow.connection.execute('''INSERT INTO signoff_attention(signoff_id,notify,urgent,blocking,reason,deadline,next_notify_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(signoff_id) DO UPDATE SET notify=excluded.notify,
            urgent=excluded.urgent,blocking=excluded.blocking,reason=excluded.reason,deadline=excluded.deadline,
            next_notify_at=excluded.next_notify_at,revision=signoff_attention.revision+1,updated_at=excluded.updated_at''',
            (identity, int(values['notify']), int(values['urgent']), int(values['blocking']), values['reason'], values['deadline'], next_at, now))
        uow.connection.execute('UPDATE signoff_attention SET priority_updated_by=?,priority_updated_at=? WHERE signoff_id=?',
            (actor_id, now, identity))
        return True

    def apply(self, ctx, plan, uow):
        change = plan.changes[0]
        identity, values = change.resource.resource_id, plain(change.values)
        repo = uow.repository('signoffs', business_id=change.resource.business_id)
        row = repo.get(identity)
        if change.action in {'attention_set', 'input_cancel'}:
            scoped = replace(ctx, business_id=change.resource.business_id, review_business_id=change.resource.business_id)
            row = self.service._row(scoped, identity, uow)
            self.authorize_update(scoped, row, plan.operation_name, uow)
        existed = row is not None
        if change.action == 'input_create' and not row:
            form = values['form']
            row = repo.insert({'id': identity, 'category': 'input_request', 'request_key': values['request_key'],
                'review_scope_kind': 'business' if change.resource.business_id else 'global',
                'proposer_principal_id': ctx.principal_id, 'proposer_agent_id': ctx.agent_id,
                'initiated_by_principal_id': ctx.initiated_by_principal_id,
                'operation_name': plan.operation_name, 'operation_version': 1, 'proposal_revision': 1,
                'payload_hash': plan.payload_hash, 'change_set': plan.canonical,
                'preview_json': {'title': form['title'], 'explanation': form['explanation'], 'input': form},
                'expected_targets_json': [], 'policy_binding_json': plain(plan.policy_binding),
                'affected_scope_json': plain(plan.affected_scope), 'state': 'pending',
                'updated_sequence': uow.next_sequence('signoffs'), 'created_at': self.service.clock()})
            if self.service.ports.management is not ABSENT:
                self.service.ports.management.auto_route_input(ctx, row, uow)
        elif change.action == 'input_cancel':
            row = self.service._update(row, {'state': 'cancelled'}, uow)
        if 'attention' in values:
            changed = self.attention(identity, values['attention'], uow, actor_id=ctx.principal_id)
            if existed and changed:
                row = self.service._update(row, {}, uow)
        if 'focus' in plan.arguments:
            self.service.ports.require('focus', FOCUS_UNAVAILABLE).present_created(self.service.app,ctx,plan.arguments['focus'],{'type':'signoff','id':row['id'],'business_id':row['review_business_id']},uow)
        return {'signoff': self.service._dto(row, uow)}

    def _coordinator(self, uow):
        return self.service.ports.require('management', MANAGEMENT_UNAVAILABLE).coordinator(uow)

    def prepare_delegated(self, ctx, args, options, uow):
        if ctx.principal_kind != 'agent' or ctx.principal_id != self._coordinator(uow):
            raise DomainError('forbidden', 'Only the configured management coordinator can answer delegated input')
        return PlanBuilder(ctx, 'signoffs_input_answer_delegated', args, 'Answer management input',
            namespace=self.service.ports.references.task_namespace(ctx,args), affected_scope={'kind': 'business', 'business_id': ctx.business_id}
            if ctx.business_id else {'kind':'global'}).build()

    def apply_delegated(self, ctx, plan, uow):
        args = plain(plan.arguments)
        coordinator = self._coordinator(uow)
        if ctx.principal_id != coordinator:
            raise DomainError('forbidden', 'Only the configured management coordinator can answer delegated input')
        self.service.app.authorize(ctx, self.service.app.registry.get('signoffs_get'), (), uow,
            namespace=plan.target_namespace)
        row = self.service._row(ctx,args['signoff_id'],uow)
        if row['category'] != 'input_request':
            raise DomainError('forbidden','Only input questions can be answered through management')
        route = self.service.ports.management.routing_for(uow, row['id'])
        if (not route or route['coordinator_id'] != coordinator or route['attention_destination']!='management'
                or not route['management_allowed'] or row['preview_json']['input'].get('response_policy','owner_only') != 'management_allowed'
                or row['preview_json']['input'].get('kind') != 'question'):
            raise DomainError('forbidden', 'This input request is not delegated to management')
        if ctx.business_id is not None and row['review_business_id'] != ctx.business_id:
            raise DomainError('forbidden', 'Input request is outside the coordinator scope')
        notes, selected = validate_response(row['preview_json']['input'], args.get('notes', ''), args.get('selected_choices') or [])
        # The application dispatcher owns idempotency, including exact MCP
        # retries. Do not introduce a second domain key that transports strip.
        if row['state'] != 'pending' or row['superseded_by_id']:
            raise DomainError('invalid_transition', 'Only pending, non-superseded input requests may be delegated')
        if not self.service._binding_matches(row, args['proposal_revision'], args['payload_hash']) or row['status_revision'] != args['expected_status_revision']:
            raise self.service._stale(ctx, row, uow, 'Input request binding changed; read its current outcome')
        message_id = self.service.notes.decision_message(ctx,row,notes,uow,summary=args.get('summary'))
        row = self.service._update(row, {'state': 'answered', 'decision_json': {'kind': 'respond', 'by': ctx.principal_id, 'at': self.service.clock(), 'message_id': message_id, 'selected_choices': selected}}, uow)
        return self.service._decision_result(ctx, row, uow)
