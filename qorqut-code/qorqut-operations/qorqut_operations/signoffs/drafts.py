"""Owner-authored draft versions. Saving and returning content never stages an effect."""
import json

from qorqut_operations.contracts import DomainError, canonical_hash


SCHEMA = (
    """CREATE TABLE signoff_draft_revisions (
    signoff_id TEXT NOT NULL REFERENCES signoffs(id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    field_values_json TEXT NOT NULL CHECK(json_valid(field_values_json) AND json_type(field_values_json)='object'),
    created_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(signoff_id,revision)
)""",
    """CREATE TRIGGER signoff_draft_revisions_no_update BEFORE UPDATE ON signoff_draft_revisions BEGIN SELECT RAISE(ABORT,'Draft versions are immutable'); END""",
    """CREATE TRIGGER signoff_draft_revisions_no_delete BEFORE DELETE ON signoff_draft_revisions BEGIN SELECT RAISE(ABORT,'Draft versions are immutable'); END""",
)


def is_draft(row):
    return row['category'] == 'input_request' and row['preview_json'].get('input', {}).get('kind') == 'draft_review'


def validate_values(row, values, *, final=False):
    if not is_draft(row):
        raise DomainError('invalid_transition', 'This signoff is not a draft review')
    fields = row['preview_json']['input']['draft_fields']
    if not isinstance(values, dict) or set(values) != {field['id'] for field in fields}:
        raise DomainError('validation_error', 'Supply exactly the fields in this draft review')
    for field in fields:
        value = values[field['id']]
        if not isinstance(value, str) or len(value) > 16000:
            raise DomainError('validation_error', 'Draft fields must be text of at most 16000 characters')
        if not field.get('multiline', True) and any(c in value for c in '\r\n'):
            raise DomainError('validation_error', 'Single-line draft fields cannot contain line breaks')
        if final and field.get('required') and not value.strip():
            raise DomainError('validation_error', field['label'] + ' is required before returning the draft')
    if sum(map(len, values.values())) > 32000:
        raise DomainError('validation_error', 'Draft content must contain at most 32000 characters')
    return dict(values)


class DraftReviews:
    def __init__(self, service):
        self.service = service

    def original(self, row):
        return {'revision': 0, 'values': {f['id']: f['value'] for f in row['preview_json']['input']['draft_fields']},
                'created_by': row['proposer_principal_id'], 'created_at': row['created_at']}

    @staticmethod
    def decode(record):
        return {'revision': record['revision'], 'values': json.loads(record['field_values_json']),
                'created_by': record['created_by'], 'created_at': record['created_at']}

    def current(self, row, uow):
        saved = uow.connection.execute('SELECT * FROM signoff_draft_revisions WHERE signoff_id=? ORDER BY revision DESC LIMIT 1',
                                       (row['id'],)).fetchone()
        return self.decode(saved) if saved else self.original(row)

    def history(self, row, uow, *, before=None):
        # One extra row distinguishes a final page from a page with older versions.
        records = uow.connection.execute('SELECT * FROM signoff_draft_revisions WHERE signoff_id=? AND revision<? ORDER BY revision DESC LIMIT 21',
                                         (row['id'], before if before is not None else 9223372036854775807)).fetchall()
        items = [self.decode(record) for record in records[:20]]
        more = len(records) > 20
        if not more and (before is None or before > 0):
            items.append(self.original(row))
        return {'items': items, 'next_before': items[-1]['revision'] if more else None}

    def save(self, context, row, values, uow):
        current = self.current(row, uow)
        if values == current['values']:
            return current
        revision = {'revision': current['revision'] + 1, 'values': values,
                    'created_by': context.principal_id, 'created_at': self.service.clock()}
        uow.connection.execute('INSERT INTO signoff_draft_revisions(signoff_id,revision,field_values_json,created_by,created_at) VALUES(?,?,?,?,?)',
            (row['id'], revision['revision'], json.dumps(values, ensure_ascii=False), revision['created_by'], revision['created_at']))
        return revision

    def validate_binding(self, context, row, uow, proposal_revision, payload_hash, expected_status_revision):
        if (not is_draft(row) or row['legacy_evidence_json'] is not None):
            raise DomainError('invalid_transition', 'Only draft reviews can save draft versions')
        if (row['state'] != 'pending' or row['superseded_by_id'] or row['status_revision'] != expected_status_revision
                or not self.service._binding_matches(row, proposal_revision, payload_hash)):
            raise self.service._stale(context, row, uow, 'This draft review changed. Your text is kept; reload before saving.')

    def input_hash(self, signoff_id, values, proposal_revision, payload_hash, expected_status_revision):
        return canonical_hash({'id': signoff_id, 'values': values, 'proposal_revision': proposal_revision,
                               'payload_hash': payload_hash, 'revision': expected_status_revision})
