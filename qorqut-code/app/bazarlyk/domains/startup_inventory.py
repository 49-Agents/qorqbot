"""Paginated own work indexes, using canonical visibility without consuming inbox."""
from bazarlyk.contracts import DomainError, InvocationOptions, Principal, canonical_hash
from .inventory_cursors import inventory_cursors, is_handle


def compact(row, fields):
    value = {key: row[key] for key in fields if key in row}
    for key in ('title', 'summary'):
        if key in value and len(value[key]) > 200:
            value[key] = value[key][:200]
            value[key + '_shortened'] = True
    return value


class StartupInventory:
    def __init__(self, memory, ctx, identity, uow):
        self.memory, self.app, self.uow = memory, memory.app, uow
        self.identity = identity
        actor = Principal(identity, 'agent')
        self.ctx = self.app.current_context(memory.resolver.invocation_context(actor,
            business_id=ctx.business_id), uow)
        self.scopes = [ctx.business_id]
        if ctx.business_id is None:
            self.scopes += sorted(self.ctx.allowed_business_ids)
        self.legacy_binding = canonical_hash({'agent': identity, 'scopes': self.scopes})

    def read(self, ctx, name, args):
        descriptor = self.app.registry.get(name)
        assert descriptor.effect_kind == 'read'
        args = self.app.registry.bind_arguments(name, args)
        self.app.authorize(ctx, descriptor, (), self.uow, namespace=self.app.namespace(ctx, descriptor, args))
        return descriptor.read(ctx, args, InvocationOptions(), self.uow)

    def position(self, after):
        if after is None:
            return {'tasks': [0, None], 'signoffs': [0, None], 'inbox': 0}
        if is_handle(after):
            return inventory_cursors.resolve(after, self.binding)
        try:
            import base64
            import json
            value = json.loads(base64.b64decode(after + '=' * (-len(after) % 4), altchars=b'-_', validate=True))
            assert value['binding'] == self.legacy_binding and set(value) == {'binding', 'positions'}
            positions = value['positions']
            assert set(positions) == {'tasks', 'signoffs', 'inbox'}
            for kind in ('tasks', 'signoffs'):
                item = positions[kind]
                assert isinstance(item, list) and len(item) == 2
                assert type(item[0]) is int and 0 <= item[0] <= len(self.scopes)
                assert item[1] is None or isinstance(item[1], str)
            assert positions['inbox'] is None or type(positions['inbox']) is int and positions['inbox'] >= 0
            return positions
        except (ValueError, TypeError, KeyError, AssertionError):
            raise DomainError('validation_error', 'Inventory cursor does not match this agent and current scope') from None

    def page(self, after=None, *, limit=25):
        self.limit = limit
        self.binding = canonical_hash({'agent': self.identity, 'scopes': self.scopes,
            'limit': limit})
        positions = self.position(after)
        result = {'agent_id': self.identity, 'tasks': [], 'signoffs': [], 'inbox': [],
                  'unavailable': [], 'read_only': True}
        for kind in ('tasks', 'signoffs'):
            index, cursor = positions[kind]
            while index < len(self.scopes) and len(result[kind]) < limit:
                business = self.scopes[index]
                try:
                    ctx = self.app.current_context(self.ctx.select_business(business), self.uow)
                    args = {'limit': limit - len(result[kind])}
                    if business is None:
                        args['scope'] = 'global'
                    if cursor is not None:
                        args['cursor'] = cursor
                    if kind == 'tasks':
                        args.update(assignee_id=self.identity, include_closed=False, snooze_filter='all')
                    else:
                        args['proposer_principal_id'] = self.identity
                    data = self.read(ctx, kind + '_list', args)
                    rows = data['tasks' if kind == 'tasks' else 'items']
                    # Global Signoff reads can include accessible business reviews;
                    # only consume Global rows here; business pages follow separately.
                    rows = [row for row in rows if kind != 'signoffs' or row.get('review_business_id') == business]
                    fields = ('id', 'title', 'status', 'revision', 'due', 'snooze', 'snoozed') if kind == 'tasks' else (
                        'id', 'title', 'state', 'decision_state', 'execution_state', 'status_revision',
                        'proposal_revision', 'updated_sequence', 'updated_at', 'actionable', 'superseded_by_id')
                    for row in rows:
                        item = {**compact(row, fields), 'business_id': business}
                        if kind == 'signoffs':
                            item['attention'] = compact(row.get('attention', {}), ('urgent', 'blocking', 'notify'))
                        else:
                            item['blocked'] = bool(row.get('unresolved_blockers'))
                        result[kind].append(item)
                    cursor = data.get('next_cursor')
                    if cursor is not None:
                        continue
                except DomainError as error:
                    if error.code not in {'forbidden', 'policy_denied', 'not_found', 'scope_required', 'operation_unavailable', 'business_archived'}:
                        raise
                    result['unavailable'].append({'queue': kind, 'business_id': business, 'reason': error.code})
                index, cursor = index + 1, None
            positions[kind] = [index, cursor]
        if positions['inbox'] is not None:
            try:
                # Pure visibility read: no session claim, watcher lease or acknowledgement.
                self.memory.sources.authorize(self.ctx, 'inbox_list', self.uow)
                inbox = self.app.inbox
                data = inbox.pending(inbox.context(self.ctx, self.uow), self.uow,
                                     after=positions['inbox'], limit=limit)
                result['inbox'] = [compact(row, ('id', 'business_id', 'source_type', 'source_id',
                    'source_revision', 'event_type', 'summary', 'actor_id', 'actor_name', 'created_at')) for row in data['items']]
                positions['inbox'] = data['next_cursor']
            except DomainError as error:
                if error.code not in {'forbidden', 'policy_denied', 'operation_unavailable'}:
                    raise
                result['unavailable'].append({'queue': 'inbox', 'reason': error.code})
                positions['inbox'] = None
        result['complete'] = all(positions[k][0] == len(self.scopes) for k in ('tasks', 'signoffs')) and positions['inbox'] is None
        result['next_cursor'] = None if result['complete'] else inventory_cursors.issue(self.binding, positions)
        result['guidance'] = ('Own unfinished tasks include snoozed work; Signoffs include current requested outcomes, not a since-last-session claim. '
            'Inbox entries are pending change summaries, not full messages or approval evidence. Read sources before acting or acknowledging. '
            'Follow next_cursor with agent_context_get(inventory=true, inventory_after=...) before claiming the queues are loaded. '
            'Continuation handles are process-local and expire; if one is unavailable, restart the inventory from its first page.')
        return result
