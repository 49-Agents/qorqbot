"""Small author projection shared by source-owned conversations."""
import json

from qorqut_operations.contracts import DomainError
from qorqut_operations.departments import profile_department


def author(note,uow):
    principal=uow.repository('principals').get(note['created_by'])
    installation=uow.repository('installation').get('service')
    note.update(created_by_name=principal['display_name'] if principal else 'Unknown',
        author_kind=principal['kind'] if principal else None,
        is_owner=bool(installation and note['created_by']==installation['owner_principal_id']))
    profile=uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?',(note['created_by'],)).fetchone() if principal and principal['kind']=='agent' else None
    if profile:
        data=json.loads(profile['profile_json'])
        note['presentation']={**data.get('presentation',{}),'department':profile_department(data)}
    return note


def participants(uow,identities):
    return [author({'created_by':identity},uow) for identity in sorted(set(identities)-{None})]


SUMMARY = {
    'type': 'string', 'minLength': 1, 'maxLength': 500,
    'pattern': r'\S',
    'description': 'Separate summary of the accompanying full message. Use very simple, common words. Aim for 2–5 words and keep it within 10 when possible; longer is allowed. Keep the full message complete.',
}


def normalize_summary(value):
    """Validate every writer, including internal source dispatch."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise DomainError('validation_error', 'Message summary must contain nonblank text of at most 500 characters')
    return ' '.join(value.split())
