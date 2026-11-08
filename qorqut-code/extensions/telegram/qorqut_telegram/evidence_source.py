"""Exact previously reconciled Telegram-to-native attribution, never a new matcher."""
import json
from qorqut_operations.contracts import DomainError
from qorqut.management.identity import installation_owner
from qorqut.native_chat import store
from qorqut.overseer.access import EvidenceAccess


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

kinds = ("telegram_owner_input", "telegram_message")

def authorize(self, ctx, kind, identity, loc, business, uow):
    from qorqut.overseer.access import _forbidden
    if kind == 'telegram_owner_input':
        if identity != loc.get('owner_input_id'): _forbidden()
        rows = uow.connection.execute('SELECT agent_id,owner_principal_id,sensitive FROM telegram_owner_inputs WHERE id=?', (identity,)).fetchall()
    else:
        if identity != loc.get('message_id'): _forbidden()
        rows = uow.connection.execute('SELECT agent_id,owner_principal_id,sensitive FROM telegram_owner_inputs WHERE message_id=? LIMIT 2', (identity,)).fetchall()
        message = uow.connection.execute('SELECT direction,sensitive,body_redacted FROM telegram_messages WHERE id=?', (identity,)).fetchone()
        if message is None or message['direction'] != 'inbound' or message['sensitive'] or message['body_redacted']:
            _forbidden()
    owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
    if len(rows) != 1 or owner is None or rows[0]['owner_principal_id'] != owner[0] or rows[0]['sensitive']:
        _forbidden()
    if kind == 'telegram_owner_input' and loc.get('agent_id') != rows[0]['agent_id']: _forbidden()
    self._agent(ctx, rows[0]['agent_id'], uow)
    profile = uow.connection.execute('SELECT home_business_id FROM agent_profiles WHERE agent_id=?', (rows[0]['agent_id'],)).fetchone()
    if profile is None or profile[0] != business: _forbidden()

def evidence(self, uow, *, agent, provider, conversation, row, entry, owner_id, archive, receipt, namespace, gaps):
    original_sources = []
    # Only an existing receipt-to-exact-record association establishes ownership.
    association = uow.connection.execute(
        "SELECT i.principal_id,i.input_type,i.origin,i.state,i.id,i.source_key,i.reason,"
        "json_extract(i.evidence_json,'$.receipt_id'),json_type(i.evidence_json,'$.processing_confirmed'),i.sensitive "
        "FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id "
        "WHERE i.agent_id=? AND s.agent_id=? AND s.provider=? AND s.conversation_id=? "
        "AND (s.transcript_path IS NULL OR s.transcript_path=?) "
        "AND json_type(i.evidence_json,'$.native_input_observed')='true' "
        "AND json_extract(i.evidence_json,'$.raw_offset')=? LIMIT 2",
        (agent, agent, provider, conversation, row["transcript_path"], entry.offset)).fetchall()
    if (len(association) != 1 or association[0][0] not in (None, owner_id) or association[0][1] != "telegram"
            or association[0][2] != "owner" or association[0][3] != "uncertain"
            or association[0][6] != "transcript_confirmed" or association[0][8] != "false" or association[0][9]
            or not association[0][7]):
        gaps.append({"code": "unresolved_provenance", "reason": "unbound_native_input", "offset": entry.offset})
        return None
    try:
        observed = archive.load_receipt(association[0][7])
        if ((observed.agent_id, observed.provider, observed.conversation_id, observed.archive_id)
                != (agent, provider, conversation, receipt.archive_id)
                or observed.record_boundary < entry.offset + entry.length):
            raise ValueError("wrong receipt lineage or range")
        raw = archive.read_raw(observed, offset=entry.offset, limit=entry.length)
        if len(raw.data) != entry.length:
            raise ValueError("incomplete observed range")
    except Exception:
        gaps.append({"code": "unresolved_provenance", "reason": "association_receipt_unavailable", "offset": entry.offset})
        return None
    input_pre = self._locator(namespace, "native_input", association[0][4],
        agent_id=agent, provider=provider, conversation_id=conversation, input_id=association[0][4])
    self.access.authorize_source(input_pre)
    original_sources.append(input_pre)
    source_key = association[0][5] or ""
    if not source_key.startswith("telegram:"):
        gaps.append({"code": "unresolved_provenance", "reason": "telegram_source_missing", "offset": entry.offset})
        return None
    telegram_id = source_key.removeprefix("telegram:")
    telegram_pre = self._locator(namespace, "telegram_owner_input", telegram_id, owner_input_id=telegram_id, agent_id=agent)
    self.access.authorize_source(telegram_pre)
    telegram = uow.connection.execute("SELECT purpose,correlation_id,signoff_id,owner_principal_id,agent_id,sensitive,message_id "
        "FROM telegram_owner_inputs WHERE id=?", (telegram_id,)).fetchone()
    if (telegram is None or telegram[3] != owner_id or telegram[4] != agent
            or telegram[5] or not telegram[1]):
        gaps.append({"code": "unresolved_provenance", "reason": "telegram_source_missing", "offset": entry.offset})
        return None
    original_sources.append(telegram_pre)
    message_pre = self._locator(namespace, "telegram_message", telegram[6], message_id=telegram[6])
    self.access.authorize_source(message_pre)
    message = uow.connection.execute("SELECT body,direction,sensitive,body_redacted,reply_to_id FROM telegram_messages WHERE id=?",
        (telegram[6],)).fetchone()
    if (message is None or message[1] != "inbound" or message[2] or message[3]
            or not isinstance(message[0], str)):
        gaps.append({"code": "unresolved_provenance", "reason": "telegram_message_unavailable", "offset": entry.offset})
        return None
    if message[4] and telegram[0] != "signoff_reply":
        parent = uow.connection.execute("SELECT agent_id FROM telegram_messages WHERE id=?", (message[4],)).fetchone()
        if parent is None or parent[0] is None:
            gaps.append({"code": "unresolved_provenance", "reason": "telegram_reply_origin_unknown", "offset": entry.offset})
            return None
        if parent[0] in self.journal.overseer_ids:
            return None
    original_sources.append(message_pre)
    eligible_text = message[0]
    actor_id, actor_kind, event_id = owner_id, "human", telegram[1]
    association_info = {"association_kind": "host_reconciled_observation",
        "association_method": "exact_text_and_session_time_window", "processing_confirmed": False,
        "owner_principal_source": "telegram_owner_input", "owner_principal_ref": telegram_pre,
        "native_principal_id": association[0][0],
        "native_input_ref": input_pre, "owner_input_ref": telegram_pre,
        "receipt_ref": {"archive_id": observed.archive_id, "receipt_id": observed.receipt_id,
                        "offset": entry.offset, "length": entry.length}}
    if telegram[0] == "signoff_reply":
        signoff = uow.connection.execute("SELECT proposer_agent_id,proposer_principal_id,review_business_id FROM signoffs WHERE id=?",
            (telegram[2],)).fetchone()
        if signoff is None:
            gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_missing", "offset": entry.offset})
            return None
        if signoff[0] in self.journal.overseer_ids or signoff[1] in self.journal.overseer_ids:
            return None
        source_namespace = ({"kind": "business", "business_id": signoff[2]} if signoff[2]
                            else {"kind": "global", "business_id": None})
        source_pre = self._locator(source_namespace, "signoff_note", telegram[1], signoff_id=telegram[2], note_id=telegram[1])
        try:
            self.access.authorize_source(source_pre)
        except DomainError:
            gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_forbidden", "offset": entry.offset})
            return None
        original_sources.append(source_pre)
        note = uow.connection.execute("SELECT body FROM signoff_notes WHERE id=? AND signoff_id=?", (telegram[1], telegram[2])).fetchone()
        if note is None:
            gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_missing", "offset": entry.offset})
            return None
        eligible_text = note[0]

    return original_sources, association_info, eligible_text, actor_id, actor_kind, event_id

def reconcile(uow, row, entry, evidence):
    if row['source_key'].startswith('telegram:'):
        repository = uow.repository('telegram_owner_inputs')
        source = repository.get(row['source_key'][len('telegram:'):])
        if source and source['agent_id'] == row['agent_id'] and source['state'] in {'submitting', 'uncertain'}:
            repository.update(source['id'], {'state': 'submitted', 'error_code': None,
                'deferral_reason': None, 'next_attempt_at': None,
                'submitted_at': entry.timestamp, 'receipt_json': evidence},
                expected_revision=source['revision'])

def record(uow, linked, linked_refs, *, ctx, source, entry, base):
    message = uow.connection.execute('SELECT body FROM telegram_messages WHERE id=? AND direction=\'inbound\' AND sensitive=0 AND body_redacted=0',
                                     (linked['message_id'],)).fetchone()
    if message is None or not isinstance(message['body'], str):
        raise DomainError('evidence_unavailable', 'The original owner input is unavailable')
    owner_source = {'native_input_id': linked['native_input_id'], 'owner_principal_id': linked['owner_principal_id'],
        'agent_id': ctx.principal_id, 'provider': source['provider'], 'conversation_id': source['conversation_id'],
        'session_id': linked['session_id'], 'source_kind': 'telegram', 'source_key': linked['source_key'],
        'accepted_at': linked['accepted_at'], 'native_principal_id': linked['native_principal_id'],
        'owner_principal_source': 'telegram_owner_input', 'owner_principal_ref': linked_refs[1],
        'association_kind': 'host_reconciled_observation',
        'association_method': 'exact_text_and_session_time_window', 'processing_confirmed': False,
        'observed_receipt': {'archive_id': source['archive_id'], 'receipt_id': linked['observed_receipt_id'],
                             'offset': entry.offset, 'length': entry.length}}
    return ({**base, 'actor_kind': 'human', 'source_kind': 'telegram_owner', 'text': message['body'],
        'owner_source': owner_source, 'association_kind': 'host_reconciled_observation',
        'association_method': 'exact_text_and_session_time_window', 'processing_confirmed': False,
        'observed_receipt_id': linked['observed_receipt_id'], 'length': entry.length, 'gaps': []})

def owner_predicate():
    # i = native input, x = installation. Only a proven source belonging to the current owner is eligible.
    return "(i.input_type='telegram' AND (i.principal_id IS NULL OR i.principal_id=x.owner_principal_id) AND EXISTS (SELECT 1 FROM telegram_owner_inputs t WHERE i.source_key='telegram:'||t.id AND t.agent_id=i.agent_id AND t.owner_principal_id=x.owner_principal_id))"
