"""Read the frozen retirement backlog without becoming a participant or acknowledging it."""
import json

from qorqut.agents.global_scope import active_role_holder
from qorqut_operations.contracts import DomainError
from . import retirement_contract as contract


def invalid():
    raise DomainError('invalid_configuration', 'Retirement backlog is unavailable')


def load(ctx, uow):
    installation = uow.connection.execute('SELECT installation_id,owner_principal_id FROM installation').fetchone()
    if installation is None:
        invalid()
    if ctx.business_id is not None or getattr(ctx, 'delegation_assignment_id', None) is not None:
        raise DomainError('forbidden', 'Retirement backlog requires an owner or Ordubashy Global session')
    if getattr(ctx, 'delegation_binding_id', None) is not None:
        raise DomainError('forbidden', 'Retirement backlog is unavailable to delegated workers')
    owner = ctx.principal_kind == 'human' and ctx.principal_id == installation['owner_principal_id']
    if not owner and (ctx.principal_kind != 'agent' or ctx.principal_id != active_role_holder(uow.connection, 'ordubashy')):
        raise DomainError('forbidden', 'Retirement backlog requires the owner or current Ordubashy')
    ids = contract.backlog_ids(installation['installation_id'])
    row = uow.connection.execute('SELECT * FROM thread_messages WHERE business_id IS NULL AND id=?',
                                 (ids['system-entry'],)).fetchone()
    if row is None:
        return None
    task = uow.connection.execute('SELECT * FROM tasks WHERE business_id IS NULL AND id=?', (ids['task'],)).fetchone()
    if (task is None or task['created_by'] != installation['owner_principal_id'] or row['item_type'] != 'task'
            or row['item_id'] != ids['task'] or row['kind'] != 'system' or row['author_id'] is not None
            or row['recorder_id'] != installation['owner_principal_id']):
        invalid()
    try:
        raw = json.loads(row['attribution_json'])
        value = raw[contract.MANIFEST_KEY]
        if (set(raw) != {'kind', contract.MANIFEST_KEY} or raw['kind'] != 'system'
                or set(value) != {'schema_version','installation_id','cohort_agent_ids','entries','findings'}
                or value['schema_version'] != 1 or value['installation_id'] != installation['installation_id']
                or not isinstance(value['entries'], list) or len(value['entries']) > contract.MAX_ENTRIES
                or not isinstance(value['findings'], list) or len(value['findings']) > contract.MAX_FINDINGS
                or not isinstance(value['cohort_agent_ids'], list) or len(value['cohort_agent_ids']) > 1000
                or any(not contract.canonical(i) for i in value['cohort_agent_ids'])
                or len(set(value['cohort_agent_ids'])) != len(value['cohort_agent_ids'])
                or contract.manifest(value['installation_id'], value['cohort_agent_ids'], value['entries'], value['findings']) != value
                or row['body'] != contract.body(value)):
            invalid()
        cohort = set(value['cohort_agent_ids'])
        seen = set()
        for entry in value['entries']:
            if (set(entry) != set(contract.ENTRY_KEYS) or entry['recipient_agent_id'] not in cohort
                    or type(entry['event_id']) is not int or entry['event_id'] < 1
                    or not isinstance(entry['message_id'], str) or not 1 <= len(entry['message_id']) <= 256
                    or entry['message_id'] in seen
                    or (entry['business_id'] is not None and not contract.canonical(entry['business_id']))):
                invalid()
            seen.add(entry['message_id'])
        for finding in value['findings']:
            if (set(finding) != set(contract.FINDING_KEYS) or finding['agent_id'] not in cohort
                    or not isinstance(finding['check_key'], str) or not 1 <= len(finding['check_key']) <= 512):
                invalid()
        for agent in cohort:
            profile = uow.connection.execute('SELECT a.home_business_id,a.status,p.active FROM agent_profiles a '
                'JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=?', (agent,)).fetchone()
            if not profile or profile['home_business_id'] is None or profile['status'] != 'retired' or profile['active'] != 0:
                invalid()
    except (KeyError, TypeError, ValueError):
        invalid()
    return value


def read(messages, ctx, args, uow):
    if args.get('thread_id') or args.get('counterpart_id'):
        raise DomainError('validation_error', 'Retirement backlog does not accept conversation filters')
    value = load(ctx, uow)
    if value is None:
        return {'items': [], 'findings': [], 'next_cursor': None, 'read_only': True}
    found = []
    for entry in value['entries']:
        row = uow.connection.execute('SELECT m.* FROM agent_messages m JOIN inbox_events e ON e.id=m.event_id '
            'JOIN inbox_deliveries d ON d.event_id=e.id AND d.agent_id=? '
            'WHERE m.id=? AND m.event_id=? AND m.business_id IS ? AND m.current_recipient_id=? '
            "AND e.source_type='agent_messages' AND e.source_id=m.id AND e.business_id IS m.business_id "
            "AND e.event_type='direct_message'", (entry['recipient_agent_id'], entry['message_id'], entry['event_id'],
            entry['business_id'], entry['recipient_agent_id'])).fetchone()
        if row is None:
            invalid()
        if row['event_id'] > args.get('after', 0):
            found.append(row)
    limit = args.get('limit', 20)
    page = found[:limit]
    items = []
    for row in page:
        item = messages.view(row, uow)
        item.pop('terminal_delivery', None)
        item.update(read_only=True, retirement_backlog=True, counterpart_id=None)
        items.append(item)
    findings = []
    for key in value['findings']:
        row = uow.connection.execute('SELECT agent_id,check_key,code,business_id,task_id,message,created_at,state '
            'FROM agent_checkpoint_findings WHERE agent_id=? AND check_key=?', (key['agent_id'],key['check_key'])).fetchone()
        if row is None or row['code'] != contract.FINDING_CODE:
            invalid()
        findings.append(dict(row))
    return {'items': items, 'findings': findings, 'next_cursor': page[-1]['event_id'] if len(found) > limit else None,
            'read_only': True, 'retirement_backlog': True}
