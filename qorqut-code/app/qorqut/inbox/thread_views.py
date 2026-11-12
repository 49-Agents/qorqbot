"""Read-only thread navigation and related-record projections. No conversation or source writes."""
import json

from qorqut_operations.contracts import DomainError, InvocationOptions
from .private_conversations import UNAVAILABLE, has_unread, read_cursor

CONTEXT_NOTES = 5


def owner_context(service, ctx, uow):
    ctx = service.app.current_context(ctx, uow)
    if service.role(ctx, uow) != 'owner':
        raise DomainError('forbidden', 'Only the owner can browse private threads')
    return ctx


def project(service, ctx, source, uow):
    """Canonical source authorization precedes every label, including list previews."""
    selected = service.select(ctx, source.get('business_id'), uow)
    if source['type'] == 'reference':
        service.source_check(selected, 'reference', source['id'], uow)
        row = uow.connection.execute('SELECT display_name,reference_json,reason FROM focus_cards WHERE id=?', (source['id'],)).fetchone()
        reference = json.loads(row['reference_json'] or '{}')
        return {**source, 'label': row['display_name'] or reference.get('title') or 'Reference',
                'summary': row['reason'], 'url': (reference.get('target') or {}).get('url'),
                'business_name': 'Global', 'business_slug': None}
    if source['type'] == 'project':
        # The one Project guard: identity, current reach, archive state and project_record_get policy including deny.
        from qorqut.domains.projects import access as project_access
        source = project_access.validate_public_reference(source)
        _, row = project_access.guard(selected, service.app, uow, project_id=source['id'],
                                      operation='project_record_get')
        summary = (row.get('config_json') or {}).get('one_liner')
        return {**source, 'label': row['name'], 'summary': ' '.join(summary.split()) if isinstance(summary, str) else '',
                'url': None, 'business_name': row['name'], 'business_slug': row['slug']}
    result = service.app.attention.legacy.resolve(selected, source, uow)
    if source['type'] == 'action':
        action = service.app.registry.get('actions_get').read(selected, {'action_id': source['id']}, InvocationOptions(), uow)['action']
        result.update(label=action['summary'], summary=action['text'])
    return result


def task_context(service, ctx, source, uow, audience=None):
    """What an opened Task/Story conversation shows first: the current overview and the newest progress notes.

    The one canonical ``tasks_get`` read in the source's own scope (the caller's read policy for it was already checked by
    ``service.resolve``), so field projection, note authors, dates and references are exactly the Task's own. Writes nothing."""
    business = source['business_id']
    selected = service.select(ctx, business, uow)
    task = service.app.registry.get('tasks_get').read(
        selected, {'task_id': source['id'], 'notes_limit': CONTEXT_NOTES, **({'audience': audience} if audience else {}),
                   **({'scope': 'global'} if business is None else {})},
        InvocationOptions(), uow)['task']
    project = uow.repository('businesses').get(business) if business else None
    return {'title': task['title'], 'task_type': task['task_type'], 'status': task['status'], 'overview': task['overview'],
            'revision': task['revision'], 'business_slug': project['slug'] if project else None,
            **{key: task[key] for key in ('input_requests', 'input_needed', 'input_version', 'input_request_missing', 'work_status')},
            'notes': task['notes'], 'notes_next_cursor': task['notes_next_cursor'],
            # With ``notes_audience: owner`` the notes are what is for the owner; this is how many working notes were left out.
            **({'notes_hidden': task['notes_hidden']} if 'notes_hidden' in task else {})}


def directory(service, ctx, args, uow):
    ctx = owner_context(service, ctx, uow)
    limit = args.get('limit', 50)
    # Scan a bounded page. A cursor can accompany an empty page if all its sources became inaccessible.
    rows = uow.connection.execute(
        "SELECT c.*,m.body,m.summary,m.created_at AS message_at,m.author_role,m.sequence AS message_sequence "
        "FROM owner_conversations c JOIN owner_conversation_messages m ON m.conversation_id=c.id "
        "AND m.sequence=c.latest_sequence WHERE c.peer_agent_id IS NULL "
        "AND m.author_role IN ('owner','coordinator') AND m.sequence<? ORDER BY m.sequence DESC LIMIT ?",
        (args.get('before', 9223372036854775807), limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        source = {'type': row['source_type'], 'id': row['source_id'], 'business_id': row['business_id']}
        try:
            service.resolve(ctx, {'source': source}, uow)
            resolved = project(service, ctx, source, uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                continue
            raise
        items.append({'id': row['id'], 'source': source, 'resolved_source': resolved,
                      'latest_sequence': row['message_sequence'], 'last_message_at': row['message_at'],
                      'preview': (row['summary'] or row['body'])[:500], 'author_role': row['author_role'],
                      'unread': has_unread(uow, row['id'], read_cursor(uow, row['id'], ctx.principal_id))})
    return {'items': items, 'next_before': rows[limit - 1]['message_sequence'] if len(rows) > limit else None}


def related(service, ctx, args, uow):
    ctx = owner_context(service, ctx, uow)
    _, source = service.resolve(ctx, args, uow)
    from qorqut_operations.signoffs.sources import source_key
    inputs = service.app.signoffs.sources.pending(ctx, [source], uow)[source_key(source)]
    if source['type'] == 'project':
        # A Project's related records are its owned inventory plus its explicit links, deduplicated by the same
        # scoped identity and paged independently of older messages.
        projects = getattr(service.app, 'projects', None)
        if projects is None:
            raise DomainError('operation_unavailable', 'Project records are not installed')
        return {**projects.related(ctx, source['id'], args, uow), 'input_requests': inputs}
    kind, identity, business = source['type'], source['id'], source['business_id']
    # All candidates come from stored links, never guessed from matching titles/text. Each candidate is independently
    # authorized below. UNION/GROUP BY collapse records linked in several ways; the earliest stored link places the row.
    rows = uow.connection.execute("""
        WITH candidates(type,id,business_id,created_at) AS (
          SELECT to_type,to_id,business_id,created_at FROM relationships
            WHERE business_id IS :business AND from_type=:kind AND from_id=:id AND role='concerns'
          UNION ALL SELECT 'signoff',s.id,s.review_business_id,l.created_at FROM signoff_source_links l
            JOIN signoffs s ON s.id=l.signoff_id
            WHERE l.source_type=:kind AND l.source_id=:id AND l.source_business_id IS :business
          UNION ALL SELECT from_type,from_id,business_id,created_at FROM relationships
            WHERE business_id IS :business AND to_type=:kind AND to_id=:id AND role='concerns'
          UNION ALL SELECT 'task',id,business_id,created_at FROM tasks
            WHERE :kind='task' AND business_id IS :business AND parent_id=:id
          UNION ALL SELECT 'action',action_key,business_id,created_at FROM action_logs
            WHERE business_id IS :business AND json_extract(refs_json,'$.' || :kind)=:id
          UNION ALL SELECT 'signoff',s.id,s.review_business_id,s.created_at FROM signoffs s, json_each(s.expected_targets_json) t
            WHERE s.review_business_id IS :business
              AND json_extract(t.value,'$.resource.resource_type')=CASE :kind WHEN 'task' THEN 'tasks' WHEN 'table' THEN 'data_tables' ELSE '' END
              AND json_extract(t.value,'$.resource.resource_id')=:id
              AND json_extract(t.value,'$.resource.business_id') IS :business
          UNION ALL SELECT json_extract(r.value,'$.type'),json_extract(r.value,'$.id'),
            json_extract(r.value,'$.business_id'),c.created_at FROM focus_cards c,json_each(c.references_json) r
            WHERE (json_extract(c.source_json,'$.type')=:kind AND json_extract(c.source_json,'$.id')=:id
                   AND json_extract(c.source_json,'$.business_id') IS :business)
               OR (json_extract(c.context_json,'$.type')=:kind AND json_extract(c.context_json,'$.id')=:id
                   AND json_extract(c.context_json,'$.business_id') IS :business)
               OR (:kind='reference' AND c.id=:id)
        ), canonical AS (
          SELECT type,CASE WHEN type='action' AND id NOT LIKE 'global/%' AND id NOT LIKE 'business/%'
            THEN CASE WHEN business_id IS NULL THEN 'global/' ELSE 'business/' || business_id || '/' END || id
            ELSE id END AS id,business_id,created_at FROM candidates
        ) SELECT type,id,business_id,min(created_at) AS created_at FROM canonical
          WHERE type IS NOT NULL AND id IS NOT NULL AND NOT(type=:kind AND id=:id AND business_id IS :business)
          GROUP BY type,id,business_id ORDER BY created_at DESC,type,id,business_id LIMIT :limit OFFSET :offset
        """, {'kind': kind, 'id': identity, 'business': business, 'limit': args.get('limit', 50) + 1,
              'offset': args.get('offset', 0)}).fetchall()
    limit, items = args.get('limit', 50), []
    for row in rows[:limit]:
        binding = {key: row[key] for key in ('type', 'id', 'business_id')}
        try:
            view = project(service, ctx, binding, uow)
        except DomainError as error:
            if error.code in UNAVAILABLE:
                continue
            raise
        items.append({**view, 'created_at': row['created_at']})
    result = {'items': items, 'next_offset': args.get('offset', 0) + limit if len(rows) > limit else None,
              'input_requests': inputs}
    if kind == 'task' and not args.get('offset', 0):
        result['context'] = task_context(service, ctx, source, uow, args.get('notes_audience'))
    return result
