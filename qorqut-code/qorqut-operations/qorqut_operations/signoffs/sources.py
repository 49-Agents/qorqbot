"""Explicit dependencies on owner responses, separate from immutable proposals and Task revisions."""
from dataclasses import replace
import json

from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec
from .ports import ABSENT

TABLE = 'signoff_source_links'
SOURCE = schema({'type': {'enum': ['task', 'table', 'project', 'reference']},
    'id': {'type': 'string', 'minLength': 1, 'maxLength': 300},
    'business_id': {'type': ['string', 'null'], 'format': 'uuid'}}, ('type', 'id', 'business_id'))
SCHEMA = (
    '''CREATE TABLE signoff_source_links (
 id TEXT PRIMARY KEY CHECK(length(id)=36),
 signoff_id TEXT NOT NULL REFERENCES signoffs(id),
 source_type TEXT NOT NULL CHECK(source_type IN ('task','table','project','reference')),
 source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 300),
 source_business_id TEXT REFERENCES businesses(id),
 source_namespace TEXT NOT NULL CHECK(source_namespace=CASE WHEN source_business_id IS NULL THEN 'global' ELSE 'business:' || source_business_id END),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 UNIQUE(signoff_id,source_type,source_namespace,source_id)
)''',
    'CREATE INDEX signoff_source_links_source ON signoff_source_links(source_type,source_id,source_business_id)',
    # Reuse the Signoff clock for input projections. Attaching a dependency must never revise a Task or a proposal.
    *('CREATE TRIGGER signoff_source_links_' + event + ' AFTER ' + event.upper() + ' ON signoff_source_links '
      "BEGIN UPDATE change_sequences SET value=value+1 WHERE name='signoffs'; END" for event in ('insert', 'delete', 'update')),
    '''CREATE TRIGGER signoff_source_state AFTER UPDATE OF state,superseded_by_id ON signoffs
 WHEN OLD.state<>NEW.state OR OLD.superseded_by_id IS NOT NEW.superseded_by_id
 BEGIN UPDATE change_sequences SET value=value+1 WHERE name='signoffs'; END''',
)


def storage_contribution():
    return StorageContribution('signoff-sources', {TABLE: TableSpec(('id',))}, schema=SCHEMA)


def source_key(source):
    return (source['type'], source['id'], source['business_id'])


def task_projection(task, requests, version):
    # Old freely-authored needs_input rows are retained for explicit reconciliation, but cannot claim a missing question.
    work_status = task.get('work_status', task['status'])
    missing = work_status == 'needs_input'
    if missing:
        work_status = 'open'
    status = 'needs_input' if requests else work_status
    return {**task, 'work_status': work_status, 'status': status,
            'closed': status in {'done', 'cancelled'}, 'paused': status == 'waiting',
            'input_requests': requests, 'input_needed': bool(requests), 'input_version': version,
            'input_request_missing': missing and not requests}


class SignoffSources:
    def __init__(self, service):
        self.service = service

    def descriptors(self):
        if self.service.ports.sources is ABSENT:
            return ()
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context',
            resolve_namespace=lambda c, a: 'global' if a.get('scope') == 'global' else 'business:' + (c.business_id or ''))
        fields = {'scope': {'enum': ['business', 'global']}, 'source': SOURCE,
                  'signoff_id': {'type': 'string', 'format': 'uuid'}}
        return tuple(OperationDescriptor(name, schema({**fields, **extra}, ('source', 'signoff_id', *required)),
            **common, description=description, prepare=lambda c, a, o, u, n=name: self.prepare(n, c, a, u), apply=self.apply)
            for name, extra, required, description in (
                ('signoffs_source_link', {}, (), 'Make an existing pending owner Signoff the explicit reason a task or record thread needs input. Authorizes both ends; never changes the proposal or task revision.'),
                ('signoffs_source_unlink', {'link_id': {'type': 'string', 'format': 'uuid'},
                    'expected_link_revision': {'type': 'integer', 'minimum': 1}}, ('link_id', 'expected_link_revision'),
                 'Remove one response dependency using its current link revision. Keeps the Signoff, answers and work status unchanged.')))

    def check_source(self, ctx, source, uow):
        self.service.ports.require('sources', 'Source-linked Signoffs are not installed').check(self.service.app, ctx, source, uow)

    def check_signoff(self, ctx, row, uow):
        self.service.inputs.authorize_update(ctx, row, 'signoffs_source_link', uow)
        if row['category'] == 'input_request' and row['preview_json']['input'].get('kind') == 'acknowledge':
            raise DomainError('validation_error', 'A read-and-acknowledge request cannot be what a Task or record waits on')
        if row['category'] == 'input_request' and row['preview_json']['input']['response_policy'] != 'owner_only':
            raise DomainError('validation_error', 'Only owner_only input requests may mark a source as needing user input')

    def find(self, identity, source, uow):
        row = uow.connection.execute('SELECT * FROM signoff_source_links WHERE signoff_id=? AND source_type=? '
            'AND source_id=? AND source_business_id IS ?', (identity, *source_key(source))).fetchone()
        return dict(row) if row else None

    def prepare(self, name, ctx, args, uow):
        if args.get('scope') == 'global':
            ctx = replace(ctx, business_id=None, review_business_id=None)
        ctx = self.service._context(ctx, uow)
        self.service.app.authorize(ctx, self.service.app.registry.get('signoffs_get'), (), uow,
            namespace='business:' + ctx.business_id if ctx.business_id else 'global')
        row = self.service._row(ctx, args['signoff_id'], uow)
        # Unlinking a terminal request is allowed, but requires the same source and proposer/owner authority.
        if name.endswith('_link'):
            self.check_signoff(ctx, row, uow)
        elif ctx.principal_kind == 'human':
            self.service._context(ctx, uow, owner=True)
        elif row['proposer_principal_id'] != ctx.principal_id:
            raise DomainError('forbidden', 'Only the proposer or owner can remove this dependency')
        self.check_source(ctx, args['source'], uow)
        existing = self.find(row['id'], args['source'], uow)
        if name.endswith('_unlink') and (not existing or existing['id'] != args['link_id']
                or existing['revision'] != args['expected_link_revision']):
            raise DomainError('revision_conflict', 'Read the current source dependency before removing it')
        builder = PlanBuilder(ctx, name, args, 'Link owner input' if name.endswith('_link') else 'Unlink owner input')
        builder.record(TABLE, existing['id'] if existing else new_id(), existing,
            {'signoff_id': row['id'], 'source': args['source']}, action='unlink' if name.endswith('_unlink') else 'link')
        return builder.build()

    def link(self, ctx, identity, source, uow, *, link_id=None):
        existing = self.find(identity, source, uow)
        if existing:
            return self.view(existing)
        count = uow.connection.execute('SELECT count(*) FROM signoff_source_links l JOIN signoffs s ON s.id=l.signoff_id '
            "WHERE l.source_type=? AND l.source_id=? AND l.source_business_id IS ? AND s.state='pending' AND s.superseded_by_id IS NULL",
            source_key(source)).fetchone()[0]
        if count >= 100:
            raise DomainError('result_too_large', 'Resolve or remove existing input requests before adding more to this source')
        uow.connection.execute('INSERT INTO signoff_source_links '
            '(id,signoff_id,source_type,source_id,source_business_id,source_namespace,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',
            (link_id or new_id(), identity, *source_key(source),
             'business:' + source['business_id'] if source['business_id'] else 'global', ctx.principal_id, self.service.clock()))
        return self.view(self.find(identity, source, uow))

    def apply(self, ctx, plan, uow):
        # Recheck live authorization, including the source's write policy, inside the committing transaction.
        self.prepare(plan.operation_name, ctx, plain(plan.arguments), uow)
        change = plan.changes[0]
        values = plain(change.values)
        if change.action == 'unlink':
            deleted = uow.connection.execute('DELETE FROM signoff_source_links WHERE id=? AND revision=?',
                (change.resource.resource_id, plan.arguments['expected_link_revision'])).rowcount
            if deleted != 1:
                raise DomainError('revision_conflict', 'The source dependency changed')
            return {'unlinked': True}
        return {'link': self.link(ctx, values['signoff_id'], values['source'], uow, link_id=change.resource.resource_id)}

    @staticmethod
    def view(row):
        return {'id': row['id'], 'revision': row['revision'], 'signoff_id': row['signoff_id'],
                'source': {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['source_business_id']}}

    def pending(self, ctx, sources, uow):
        """Batch indexed dependencies; canonical Signoff policy independently protects each review scope.

        An unreadable pending request remains an unavailable blocker, without exposing its ID, title or scope.
        The caller must have already authorized reads of the supplied sources.
        """
        result = {source_key(source): [] for source in sources}
        if not sources:
            return result
        candidates = []
        for offset in range(0, len(sources), 150):
            batch = sources[offset:offset + 150]
            wanted = ' OR '.join('(l.source_type=? AND l.source_id=? AND l.source_business_id IS ?)' for _ in batch)
            candidates.extend(uow.connection.execute('SELECT l.*,s.review_business_id,s.created_at AS requested_at FROM signoff_source_links l '
                'JOIN signoffs s ON s.id=l.signoff_id WHERE (' + wanted + ") AND s.state='pending' AND s.superseded_by_id IS NULL ORDER BY s.created_at,l.id",
                [value for source in batch for value in source_key(source)]).fetchall())
        visible = self.readable(ctx, candidates, uow)
        for row in candidates:
            entry = {'link_id': row['id'], 'link_revision': row['revision']}
            if row['signoff_id'] in visible:
                entry.update(type='signoff', id=row['signoff_id'], business_id=row['review_business_id'],
                             title=visible[row['signoff_id']], created_at=row['requested_at'])
            else:
                entry['unavailable'] = True
            result[(row['source_type'], row['source_id'], row['source_business_id'])].append(entry)
        return result

    def readable(self, ctx, candidates, uow):
        """``{signoff id: title}`` of the candidate rows' Signoffs the caller may read, each judged in its own review scope."""
        visible = {}
        for business in {row['review_business_id'] for row in candidates}:
            try:
                selected = self.service._context(replace(ctx, business_id=business, review_business_id=business), uow)
                self.service.app.authorize(selected, self.service.app.registry.get('signoffs_get'), (), uow,
                    namespace='business:' + business if business else 'global')
                where, params = self.service._visibility(selected, uow)
                ids = list({row['signoff_id'] for row in candidates if row['review_business_id'] == business})
                for offset in range(0, len(ids), 400):
                    batch = ids[offset:offset + 400]
                    for row in uow.connection.execute('SELECT s.id,json_extract(s.preview_json,\'$.title\') AS title FROM signoffs s WHERE '
                            + where + ' AND s.id IN (' + ','.join('?' for _ in batch) + ')', [*params, *batch]):
                        visible[row['id']] = row['title']
            except DomainError as error:
                if error.code not in {'forbidden', 'policy_denied', 'not_found', 'inactive_agent', 'operation_unavailable', 'invalid_principal'}:
                    raise
        return visible

    def decided(self, ctx, sources, uow):
        """The newest decided Signoff of each source, keyed like ``pending``; a source with none is absent.

        A decision is the owner's submitted response, whatever happened to the change afterwards; ``kind`` and
        ``selected_choices`` say in short what it was. One the caller may not
        read is reported as unavailable, without its ID, title or scope. The caller must have already authorized reads of
        the supplied sources."""
        candidates = []
        for offset in range(0, len(sources), 150):
            batch = sources[offset:offset + 150]
            wanted = ' OR '.join('(l.source_type=? AND l.source_id=? AND l.source_business_id IS ?)' for _ in batch)
            candidates.extend(uow.connection.execute(
                'SELECT l.source_type,l.source_id,l.source_business_id,l.signoff_id,s.review_business_id,s.state,s.decision_json,'
                "coalesce(json_extract(s.decision_json,'$.at'),s.updated_at) AS decided_at FROM signoff_source_links l "
                'JOIN signoffs s ON s.id=l.signoff_id WHERE (' + wanted
                + ") AND s.decision_json IS NOT NULL AND s.state<>'pending'",
                [value for source in batch for value in source_key(source)]).fetchall())
        visible, result = self.readable(ctx, candidates, uow), {}
        for row in sorted(candidates, key=lambda row: (row['decided_at'], row['signoff_id']), reverse=True):
            key = (row['source_type'], row['source_id'], row['source_business_id'])
            if key in result:
                continue
            if row['signoff_id'] not in visible:
                result[key] = {'unavailable': True}
                continue
            decision = json.loads(row['decision_json'])
            result[key] = {'type': 'signoff', 'id': row['signoff_id'], 'business_id': row['review_business_id'],
                           'title': visible[row['signoff_id']], 'state': row['state'], 'decided_at': row['decided_at'],
                           # What was chosen, in short; the full response is the Signoff's own.
                           **{name: decision[name] for name in ('kind', 'selected_choices') if decision.get(name) is not None}}
        return result

    def project_tasks(self, ctx, tasks, uow):
        if self.service.ports.sources is ABSENT:
            return tasks
        sources = [{'type': 'task', 'id': task['id'], 'business_id': task['business_id']} for task in tasks]
        pending = self.pending(ctx, sources, uow)
        version = uow.connection.execute("SELECT value FROM change_sequences WHERE name='signoffs'").fetchone()[0]
        return [task_projection(task, pending[source_key(source)], version) for task, source in zip(tasks, sources)]
