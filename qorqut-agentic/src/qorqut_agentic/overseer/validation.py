"""Structural checks for portable Overseer records."""
from __future__ import annotations

from uuid import UUID

from ..errors import ValidationFailedError


def invalid(field, message='is invalid'):
    raise ValidationFailedError(f'{field} {message}', details={'field': field})


def obj(value, field, required):
    if not isinstance(value, dict):
        invalid(field, 'must be an object')
    missing = set(required) - value.keys()
    if missing:
        invalid(field, 'is missing ' + ', '.join(sorted(missing)))


def string(value, field):
    if not isinstance(value, str) or not value.strip():
        invalid(field, 'must be a nonempty string')


def uuid(value, field):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        invalid(field, 'must be a canonical UUID')


def array(value, field):
    if not isinstance(value, list):
        invalid(field, 'must be an array')


def scope(value):
    fields = ('description', 'project_ids', 'work_types', 'workflow_refs', 'conditions', 'exceptions')
    obj(value, 'scope', fields)
    string(value['description'], 'scope.description')
    for field in fields[1:]:
        array(value[field], f'scope.{field}')
        for v in value[field]:
            string(v, f'scope.{field}')


def version(record):
    if record.get('schema_version') != 1 or type(record.get('schema_version')) is not int:
        invalid('schema_version', 'must be 1')


def source(ref):
    fields = ('provider', 'kind', 'record_id', 'revision', 'content_hash', 'origin_event_id',
              'episode_id', 'actor_id', 'actor_kind', 'occurred_at', 'locator', 'scope',
              'provenance', 'availability', 'namespace')
    obj(ref, 'source.ref', fields)
    for name in fields[:10]:
        if name in ('origin_event_id', 'episode_id', 'actor_id', 'actor_kind', 'occurred_at') and ref[name] is None:
            continue
        string(ref[name], f'source.ref.{name}')
    if not isinstance(ref['locator'], dict) or not isinstance(ref['provenance'], dict):
        invalid('source.ref', 'requires locator and provenance objects')
    provenance = ref['provenance']
    obj(provenance, 'source.ref.provenance', ('producer_id', 'original_refs', 'overseer_origin'))
    if provenance['producer_id'] is not None:
        string(provenance['producer_id'], 'source.ref.provenance.producer_id')
    array(provenance['original_refs'], 'source.ref.provenance.original_refs')
    if provenance['overseer_origin'] is not None and type(provenance['overseer_origin']) is not bool:
        invalid('source.ref.provenance.overseer_origin')
    scope(ref['scope'])
    string(ref['availability'], 'source.ref.availability')
    obj(ref['namespace'], 'source.ref.namespace', ('kind', 'business_id'))
    namespace = ref['namespace']
    if namespace['kind'] not in ('global', 'business') or (namespace['kind'] == 'global' and namespace['business_id'] is not None):
        invalid('source.ref.namespace')
    if namespace['kind'] == 'business':
        uuid(namespace['business_id'], 'source.ref.namespace.business_id')


def proposal(record):
    required = ('schema_version', 'id', 'extraction_run_id', 'summary', 'change_direction', 'observed_fact',
                'interpretation', 'scope', 'target_refs', 'origin_type', 'sources', 'created_at', 'created_by')
    obj(record, 'proposal', required)
    version(record)
    uuid(record['id'], 'id')
    for field in ('extraction_run_id', 'summary', 'change_direction', 'observed_fact', 'interpretation', 'created_at', 'created_by'):
        string(record[field], field)
    scope(record['scope'])
    array(record['target_refs'], 'target_refs')
    if record['origin_type'] not in ('direct_instruction', 'stated_preference', 'decision', 'inferred_preference', 'inferred_improvement'):
        invalid('origin_type')
    array(record['sources'], 'sources')
    supporting = False
    for link in record['sources']:
        obj(link, 'source', ('ref', 'role'))
        if link['role'] not in ('supporting', 'contextual', 'contrary'):
            invalid('source.role')
        source(link['ref'])
        if link['ref']['provenance']['overseer_origin'] is True:
            invalid('sources', 'must not include Overseer-origin extraction material')
        supporting |= link['role'] == 'supporting'
    if not supporting:
        invalid('sources', 'must include a supporting reference')


def revision(record):
    required = ('schema_version', 'delta_id', 'revision', 'summary', 'change_direction', 'scope', 'target_refs',
                'proposal_ids', 'support_summary', 'uncertainties', 'ranking_reason', 'created_at', 'created_by')
    obj(record, 'delta_revision', required)
    version(record)
    uuid(record['delta_id'], 'delta_id')
    if type(record['revision']) is not int or record['revision'] < 1:
        invalid('revision')
    for field in ('summary', 'change_direction', 'created_at', 'created_by'):
        string(record[field], field)
    scope(record['scope'])
    for field in ('target_refs', 'proposal_ids', 'uncertainties'):
        array(record[field], field)
    for item in record['proposal_ids']:
        uuid(item, 'proposal_ids')
    if len(set(record['proposal_ids'])) != len(record['proposal_ids']):
        invalid('proposal_ids', 'must contain distinct proposals')
    reason = record['ranking_reason']
    if not isinstance(reason, str) or len(reason.split()) > 50:
        invalid('ranking_reason', 'must be at most 50 words')
    support = record['support_summary']
    obj(support, 'support_summary', ('proposal_count', 'event_count', 'episode_count', 'event_refs', 'episode_refs'))
    for field in ('proposal_count', 'event_count', 'episode_count'):
        if type(support[field]) is not int or support[field] < 0:
            invalid('support_summary.' + field)
    for field in ('event_refs', 'episode_refs'):
        array(support[field], 'support_summary.' + field)
        for item in support[field]:
            string(item, 'support_summary.' + field)
