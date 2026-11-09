"""Bounded, replayable observation of already committed Overseer material."""
import hashlib
import json
import logging

from qorqut_operations.contracts import DomainError, utc_now
from qorqut.native_chat import store
from qorqut.native_chat.projection import Lineage, SPEAKER_AGENT, SPEAKER_USER
from qorqut.native_chat.reader import ConversationReader, visible
from .archive import PrivateArchive
from .retention import _digest
from .retention_access import RetentionEventAccess

LOG = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class RetentionDrain:
    def __init__(self, runtime):
        self.runtime = runtime
        self.reader = ConversationReader()

    def step(self):
        # Signoff events remain pending: new comments may arrive even after an answer.
        with self.runtime.database.read() as uow:
            rows = uow.connection.execute("SELECT event_key FROM overseer_retention_events WHERE state='pending' ORDER BY updated_at,event_key LIMIT 4").fetchall()
        for row in rows:
            try:
                self._event(row['event_key'])
            except Exception as error:
                # Sources remain pending, and diagnostics contain no content or identity.
                LOG.debug('Overseer retention event unavailable')
                try:
                    self.runtime.require_authority()
                    with self.runtime.database.transaction(operation='overseer.retention_retry') as uow:
                        self.runtime.application.ensure_writes_open(uow)
                        code = getattr(error, 'code', None)
                        gap = code if code in ('forbidden', 'evidence_unavailable', 'overseer_storage_unavailable') else 'retention_unavailable'
                        uow.connection.execute('UPDATE overseer_retention_events SET updated_at=?,gap_code=? WHERE event_key=? AND state=\'pending\'', (utc_now(), gap, row['event_key']))
                except Exception:
                    LOG.debug('Overseer retention retry deferred')

    def _event(self, key):
        gate = RetentionEventAccess(self.runtime, key)
        with self.runtime.database.read() as uow:
            event, source, binding, ctx, session = gate._resolve(uow)
            event = dict(event)
            cursor = json.loads(event['cursor_json']) if event['cursor_json'] else {}
        run_ref = {'provider': 'qorqut_overseer', 'agent_id': ctx.principal_id,
                   'conversation_id': session['conversation_id'], 'run_id': binding['run_id'] if binding else None,
                   'namespace': {'kind': 'global', 'business_id': None}}
        gate.authorize_retention(run_ref, action='read')
        if event['kind'] == 'native_capture':
            records, refs, targets, next_cursor, complete = self._native(gate, source, session, cursor)
        elif event['kind'] in ('signoff', 'workflow_signoff'):
            records, refs, targets, next_cursor, complete = self._signoff(gate, source, binding, cursor)
        elif event['kind'] == 'task':
            records, refs, targets, next_cursor, complete = self._task(gate, binding, cursor)
        else:
            raise DomainError('forbidden', 'Unknown retention event')
        material = None
        if records:
            # The operation is deterministic for these immutable facts; retry after a crash
            # can publish the same object and then finish its association.
            operation = 'observation:' + _digest({'event': key, 'cursor': cursor, 'records': records, 'refs': refs})
            archive = PrivateArchive(self.runtime.paths.overseer_archive, gate.authorize_source, gate.authorize_retention)
            material = archive.capture_retention(run_ref, records, refs, operation)
        self.runtime.require_authority()
        with self.runtime.database.transaction(operation='overseer.retention_commit') as uow:
            self.runtime.application.ensure_writes_open(uow)
            fresh, _, _, _, _ = gate._resolve(uow)
            if fresh['cursor_json'] != event['cursor_json'] or fresh['state'] != 'pending':
                return
            if material is not None:
                metadata = {'kind': event['kind'], 'count': len(records)}
                uow.connection.execute('''INSERT OR IGNORE INTO overseer_retained_materials
                    (material_key,event_key,material_ref_json,record_json,targets_json,occurred_at,created_at)
                    VALUES (?,?,?,?,?,NULL,?)''', (operation, key, _json(material), _json(metadata), _json(targets), utc_now()))
                if event['kind'] == 'native_capture':
                    for record in records:
                        if record.get('owner_source') is not None:
                            owner_source = record['owner_source']
                            identity = owner_source['native_input_id']
                            source_json = _json(owner_source)
                            revision = _digest({'source': owner_source, 'text': record['text']})
                            text_hash = hashlib.sha256(record['text'].encode('utf-8')).hexdigest()
                            old = uow.connection.execute('SELECT * FROM overseer_owner_inputs WHERE input_id=?', (identity,)).fetchone()
                            if old is None:
                                uow.connection.execute('INSERT INTO overseer_owner_inputs VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)',
                                    (identity, ctx.principal_id, owner_source['owner_principal_id'], owner_source['session_id'],
                                     source_json, revision, text_hash, _json(material)))
                                uow.connection.execute('INSERT INTO overseer_telegram_associations VALUES (?, ?, ?)',
                                                       (identity, key, record['entry_id']))
                            elif (old['source_json'] != source_json or old['reference_revision'] != revision
                                    or old['source_hash'] != text_hash):
                                raise DomainError('operation_conflict', 'The observed owner input changed')
                        if record.get('source_kind') != 'native_agent' or not isinstance(record.get('timestamp'), str):
                            continue
                        for target in record.get('references', []):
                            if target['type'] in ('delta', 'application', 'batch'):
                                uow.connection.execute('''INSERT OR IGNORE INTO overseer_retained_links
                                    (material_key,entry_id,target_kind,target_id,occurred_at) VALUES (?,?,?,?,?)''',
                                    (operation, record['entry_id'], target['type'], target['id'], record['timestamp']))
                elif event['kind'] == 'task':
                    for record in records:
                        message = record['message']
                        for kind, field in (('delta', 'delta_id'), ('application', 'application_id'), ('batch', 'batch_id')):
                            if binding[field] is not None:
                                uow.connection.execute('''INSERT OR IGNORE INTO overseer_retained_links
                                    (material_key,entry_id,target_kind,target_id,occurred_at) VALUES (?,?,?,?,?)''',
                                    (operation, message['id'], kind, binding[field], message['created_at']))
            uow.connection.execute('UPDATE overseer_retention_events SET cursor_json=?,material_ref_json=?,state=?,gap_code=NULL,updated_at=? WHERE event_key=?',
                (_json(next_cursor), _json(material) if material else event['material_ref_json'], 'complete' if complete else 'pending', utc_now(), key))
            successor = next_cursor.get('superseded_by_id') if event['kind'] in ('signoff', 'workflow_signoff') else None
            if successor:
                association_key = 'binding_id' if event['kind'] == 'signoff' else 'request_id'
                child = {association_key: source[association_key], 'signoff_id': successor, 'parent_event': key}
                uow.connection.execute('''INSERT OR IGNORE INTO overseer_retention_events
                    (event_key,kind,source_json,cursor_json,material_ref_json,state,updated_at)
                    VALUES (?, ?, ?, NULL, NULL, 'pending', ?)''',
                    ('signoff-amendment:' + _digest(child), event['kind'], _json(child), utc_now()))

    def _native(self, gate, source, session, cursor):
        from qorqut.native_chat.projection import SPEAKER_NOTICE
        with self.runtime.database.read() as uow:
            _, _, _, ctx, current = gate._resolve(uow)
            profile = store.active_profile(uow.connection, ctx.principal_id, self.runtime.paths)
        archive = self.runtime.native_chat.indexer.archive(profile)
        receipt = archive.load_receipt(source['receipt_id'])
        gate.authorize_retention({'provider': 'qorqut_overseer', 'agent_id': ctx.principal_id,
            'conversation_id': current['conversation_id'], 'run_id': None,
            'namespace': {'kind': 'global', 'business_id': None}}, action='read')
        window = self.reader.window(archive, receipt, source['provider'], source['conversation_id'], before=cursor.get('before'), limit=50)
        with self.runtime.database.read() as uow:
            gate._resolve(uow)
            replies = {r['raw_offset']: r for r in store.replies_at(uow.connection, ctx.principal_id,
                       source['provider'], source['conversation_id'], [e.offset for e in window.entries])}
            sends = store.sent_messages(uow.connection, ctx.principal_id,
                        [i for e in window.entries for i in e.receipts])
        lineage = Lineage(ctx.principal_id, source['provider'], source['conversation_id'], source['archive_id'])
        records, targets, refs = [], [], []
        for entry, completion in visible(window.entries, replies, sends):
            if entry.speaker == SPEAKER_NOTICE:
                continue
            identity = entry.public_id(lineage)
            base = {'kind': 'native_entry', 'entry_id': identity, 'provider': source['provider'],
                    'conversation_id': source['conversation_id'], 'receipt_id': source['receipt_id'],
                    'position': entry.offset, 'timestamp': entry.timestamp}
            if entry.speaker == SPEAKER_USER:
                from . import external_sources
                with self.runtime.database.read() as uow:
                    _, _, _, current_ctx, current_session = gate._resolve(uow)
                    linked = external_sources.association(self.runtime, uow, current_ctx, current_session, source, entry.offset, entry.length)
                if linked is None:
                    records.append({**base, 'actor_kind': 'unknown', 'source_kind': 'native_user', 'text': None,
                                    'availability': 'redacted', 'gaps': [{'code': 'sensitivity_provenance_unresolved'}]})
                    continue
                linked_refs = external_sources.references(self.runtime, source, linked)
                for ref in linked_refs:
                    gate.authorize_source(ref)
                with self.runtime.database.read() as uow:
                    _, _, _, fresh_ctx, fresh_session = gate._resolve(uow)
                    if external_sources.association(self.runtime, uow, fresh_ctx, fresh_session, source, entry.offset, entry.length) != linked:
                        raise DomainError('evidence_unavailable', 'The observed owner input changed')
                    records.append(external_sources.record(self.runtime, uow, linked, linked_refs,
                        ctx=ctx, source=source, entry=entry, base=base))
                refs.extend(linked_refs)
            elif entry.speaker == SPEAKER_AGENT:
                references = [{'type': t, 'id': i, 'business_id': b} for t, i, b in (entry.references or ())]
                records.append({**base, 'actor_kind': 'agent', 'source_kind': 'native_agent', 'text': entry.text,
                                'references': references, 'gaps': []})
                targets.extend(ref for ref in references if ref['type'] in ('delta', 'application', 'batch'))
        return records, refs, targets, {'before': window.older}, window.older is None

    def _signoff(self, gate, source, binding, cursor):
        sid, business = source['signoff_id'], binding['review_business_id']
        namespace = {'kind': 'business', 'business_id': business} if business else {'kind': 'global', 'business_id': None}
        def ref(kind, identity):
            loc = {'signoff_id': sid}
            if kind == 'signoff_note': loc['note_id'] = identity
            return {'provider': 'operations', 'kind': kind, 'namespace': namespace, 'record_id': identity, 'locator': loc}
        sref = ref('signoff', sid)
        gate.authorize_source(sref)
        linked_sources = gate.linked_sources()
        for linked_ref in linked_sources:
            gate.authorize_source(linked_ref)
        with self.runtime.database.read() as uow:
            _, _, _, ctx, _ = gate._resolve(uow)
            row = self.runtime.signoffs._row(ctx, sid, uow)
            # Only routing/sequence identities are read until each note is authorized.
            notes = uow.connection.execute('SELECT id,sequence FROM signoff_notes WHERE signoff_id=? AND sequence>? ORDER BY sequence LIMIT 50',
                                           (sid, cursor.get('note_sequence', 0))).fetchall()
        version = {'proposal_revision': row['proposal_revision'], 'payload_hash': row['payload_hash'],
                   'status_revision': row['status_revision']}
        records, refs = [], list(linked_sources)
        if cursor.get('version') != version:
            records.append({'kind': 'signoff', 'signoff_id': sid, 'version': version,
                            'state': row['state'], 'presentation': row['preview_json'],
                            'response': row['decision_json'], 'superseded_by_id': row['superseded_by_id']})
            refs.append(sref)
        sequence = cursor.get('note_sequence', 0)
        for note in notes:
            nref = ref('signoff_note', note['id'])
            gate.authorize_source(nref)
            with self.runtime.database.read() as uow:
                _, _, _, ctx, _ = gate._resolve(uow)
                actual = uow.connection.execute('SELECT id,sequence,body,created_by,created_at,refs_json FROM signoff_notes WHERE id=? AND signoff_id=?', (note['id'], sid)).fetchone()
                if actual is None: raise DomainError('evidence_unavailable', 'Linked Signoff note unavailable')
                references = self.runtime.signoffs.notes.references(ctx, json.loads(actual['refs_json']), uow, strict=True)
                records.append({'kind': 'signoff_note', 'id': actual['id'], 'signoff_id': sid,
                    'sequence': actual['sequence'], 'body': actual['body'], 'created_by': actual['created_by'],
                    'created_at': actual['created_at'], 'references': references})
                sequence = actual['sequence']
            refs.append(nref)
        targets = [{key: binding[key] for key in ('delta_id', 'delta_revision', 'application_id', 'plan_revision', 'batch_id')}]
        return records, refs, targets, {'version': version, 'note_sequence': sequence,
                                        'superseded_by_id': row['superseded_by_id']}, False

    def _task(self, gate, binding, cursor):
        from qorqut_operations.discussion.threads import message_view
        task_ref = binding['task_ref']
        namespace, identity = task_ref['namespace'], task_ref['id']
        business = namespace['business_id']
        task_source = {'provider': 'operations', 'kind': 'task', 'namespace': namespace,
                       'record_id': identity, 'locator': {'task_id': identity}}
        gate.authorize_source(task_source)
        after = cursor.get('after', ['', ''])
        with self.runtime.database.read() as uow:
            gate._resolve(uow)
            rows = uow.connection.execute('''SELECT id,created_at FROM thread_messages
                WHERE business_id IS ? AND item_type='task' AND item_id=? AND (created_at,id)>(?,?)
                ORDER BY created_at,id LIMIT 50''', (business, identity, *after)).fetchall()
        records, refs = [], [task_source]
        for row in rows:
            ref = {'provider': 'operations', 'kind': 'task_message', 'namespace': namespace,
                   'record_id': row['id'], 'locator': {'task_id': identity, 'message_id': row['id']}}
            gate.authorize_source(ref)
            with self.runtime.database.read() as uow:
                _, _, _, ctx, _ = gate._resolve(uow)
                actual = uow.connection.execute('''SELECT * FROM thread_messages WHERE business_id IS ?
                    AND item_type='task' AND item_id=? AND id=?''', (business, identity, row['id'])).fetchone()
                if actual is None: raise DomainError('evidence_unavailable', 'The linked Task message is unavailable')
                view = message_view(dict(actual), uow, self.runtime.domains.tasks.message_refs(ctx, uow))
                if any(item.get('unavailable') for item in view['refs']):
                    raise DomainError('forbidden', 'A linked Task reference is unavailable')
                records.append({'kind': 'task_message', 'task_id': identity, 'message': view})
            refs.append(ref)
        if rows: after = [rows[-1]['created_at'], rows[-1]['id']]
        targets = [{key: binding[key] for key in ('delta_id', 'delta_revision', 'application_id', 'plan_revision', 'batch_id')}]
        return records, refs, targets, {'after': after}, False
