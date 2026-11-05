"""Exact previously reconciled Telegram-to-native attribution, never a new matcher."""
import json
from qorqut_operations.contracts import DomainError
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_chat import store
from .access import EvidenceAccess


def association(runtime, uow, ctx, session, source, position, length):
    owner = installation_owner(uow)
    rows = uow.connection.execute('''SELECT i.* FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id
        WHERE i.agent_id=? AND s.agent_id=? AND s.provider=? AND s.conversation_id=?
          AND (s.transcript_path IS NULL OR s.transcript_path=?)
          AND json_type(i.evidence_json,'$.native_input_observed')='true'
          AND json_extract(i.evidence_json,'$.raw_offset')=? LIMIT 2''',
        (ctx.principal_id, ctx.principal_id, source['provider'], source['conversation_id'], session['transcript_path'], position)).fetchall()
    if (len(rows) != 1 or rows[0]['principal_id'] not in (None, owner) or rows[0]['sensitive'] or rows[0]['input_type'] != 'telegram'
            or rows[0]['origin'] != 'owner' or rows[0]['state'] != 'uncertain'
            or rows[0]['reason'] != 'transcript_confirmed'):
        return None
    row = rows[0]
    evidence = json.loads(row['evidence_json'])
    receipt_id = evidence.get('receipt_id')
    if evidence.get('processing_confirmed') is not False or not receipt_id or not (row['source_key'] or '').startswith('telegram:'):
        return None
    telegram_id = row['source_key'].removeprefix('telegram:')
    telegram = uow.connection.execute('''SELECT id,purpose,correlation_id,signoff_id,owner_principal_id,agent_id,sensitive,message_id
        FROM telegram_owner_inputs WHERE id=?''', (telegram_id,)).fetchone()
    if (telegram is None or telegram['owner_principal_id'] != owner or telegram['agent_id'] != ctx.principal_id
            or telegram['sensitive'] or not telegram['correlation_id'] or telegram['purpose'] == 'signoff_reply'):
        return None
    message = uow.connection.execute('SELECT id,direction,sensitive,body_redacted,reply_to_id FROM telegram_messages WHERE id=?',
                                     (telegram['message_id'],)).fetchone()
    if message is None or message['direction'] != 'inbound' or message['sensitive'] or message['body_redacted']:
        return None
    if message['reply_to_id']:
        parent = uow.connection.execute('SELECT agent_id FROM telegram_messages WHERE id=?', (message['reply_to_id'],)).fetchone()
        if parent is None or parent['agent_id'] is None:
            return None
    profile = store.active_profile(uow.connection, ctx.principal_id, runtime.paths)
    try:
        archive = runtime.native_chat.indexer.archive(profile)
        receipt = archive.load_receipt(receipt_id)
        if ((receipt.agent_id, receipt.provider, receipt.conversation_id, receipt.archive_id)
                != (ctx.principal_id, source['provider'], source['conversation_id'], source['archive_id'])
                or receipt.record_boundary < position + length
                or len(archive.read_raw(receipt, offset=position, limit=length).data) != length):
            return None
    except Exception:
        return None
    # This uses the real current agent policy and exact admitted target; no experience grant is implied.
    EvidenceAccess(runtime, ctx)._read(ctx, 'agents_get', {'agent_id': ctx.principal_id}, None, uow)
    return {'native_input_id': row['id'], 'source_key': row['source_key'], 'session_id': row['session_id'],
            'accepted_at': row['created_at'], 'owner_principal_id': owner, 'telegram_id': telegram_id,
            'native_principal_id': row['principal_id'],
            'message_id': telegram['message_id'], 'correlation_id': telegram['correlation_id'],
            'observed_receipt_id': receipt_id, 'position': position, 'length': length}


def references(source, association):
    namespace = {'kind': 'global', 'business_id': None}
    common = {'agent_id': source['agent_id'], 'provider': source['provider'], 'conversation_id': source['conversation_id'],
              'position': association['position'], 'length': association['length'], 'input_id': association['native_input_id'],
              'observed_receipt_id': association['observed_receipt_id'], 'correlation_id': association['correlation_id']}
    return [
        {'provider': 'operations', 'kind': 'native_input', 'namespace': namespace, 'record_id': association['native_input_id'],
         'locator': common},
        {'provider': 'operations', 'kind': 'telegram_owner_input', 'namespace': namespace, 'record_id': association['telegram_id'],
         'locator': {**common, 'owner_input_id': association['telegram_id']}},
        {'provider': 'operations', 'kind': 'telegram_message', 'namespace': namespace, 'record_id': association['message_id'],
         'locator': {**common, 'message_id': association['message_id']}},
    ]
