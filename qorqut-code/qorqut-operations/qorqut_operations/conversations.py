"""Small author projection shared by source-owned conversations."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
import json

from qorqut_operations.contracts import DomainError


def author(note,uow):
    speaker=note.get('author_id') or note['created_by']
    principal=uow.repository('principals').get(speaker)
    installation=uow.repository('installation').get('service')
    note.update(created_by_name=principal['display_name'] if principal else 'Unknown',
        author_kind=principal['kind'] if principal else None,
        is_owner=bool(installation and speaker==installation['owner_principal_id']))
    profile=uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?',(speaker,)).fetchone() if principal and principal['kind']=='agent' else None
    if profile:
        note['presentation']=json.loads(profile['profile_json']).get('presentation',{})
    recorder_id=note.get('recorder_id') or note['created_by']
    recorder=uow.repository('principals').get(recorder_id)
    note.update(recorder_id=recorder_id, recorder_kind=recorder['kind'] if recorder else None,
                recorder_name=recorder['display_name'] if recorder else 'Unknown')
    return note


def participants(uow,identities):
    return [author({'created_by':identity},uow) for identity in sorted(set(identities)-{None})]


SUMMARY = {
    'type': 'string', 'minLength': 1, 'maxLength': 500,
    'pattern': r'\S',
    'description': instruction_text('conversations-separate-summary-of-the-accompanying-full-message-use'),
}


def normalize_summary(value):
    """Validate every writer, including internal source dispatch."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise DomainError('validation_error', 'Message summary must contain nonblank text of at most 500 characters')
    return ' '.join(value.split())
