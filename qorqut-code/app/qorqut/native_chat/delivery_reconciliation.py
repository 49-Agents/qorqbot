"""Confirm uncertain connector and Todo reminder inputs from exact archived entries.

This never submits text or acknowledges inbox work. A bounded, restartable scan also
repairs receipts whose transcript evidence predates installation of this code.
Private inputs have no retained body and cannot be matched this way.
"""
from datetime import datetime
import json

from . import store
from .contracts import TYPE_NUDGE
from .projection import Projector, ProjectorState, SPEAKER_NOTICE, SPEAKER_USER, marker_text


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


class DeliveryReconciler:
    def __init__(self, runtime):
        self.runtime = runtime
        self.offsets = {}

    def step(self, archive, session, receipt):
        types = tuple(self.runtime.selection.owner_input_sources)
        with self.runtime.database.read() as uow:
            pending = [dict(row) for row in uow.connection.execute(
                "SELECT i.*, s.ended_at FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id "
                "WHERE i.agent_id=? AND s.provider=? AND s.conversation_id=? AND s.transcript_path=? "
                f"AND (i.input_type IN ({','.join('?' for _ in types) or 'NULL'}) OR "
                "(i.input_type='inbox_nudge' AND i.origin='system' AND i.marker IS NOT NULL "
                "AND i.source_key='attention:'||json_extract(i.metadata_json,'$.attention_reminder'))) "
                "AND i.state='uncertain' AND i.sensitive=0 AND i.body IS NOT NULL",
                (session['agent_id'], session['provider'], session['conversation_id'], session['transcript_path'], *types))]
        if not pending:
            return
        keys = {row['id']: (receipt.archive_id, row['id']) for row in pending}
        positions = [self.offsets.get(keys[row['id']], (0, False)) for row in pending]
        start = min(offset for offset, _ in positions)
        if start >= receipt.record_boundary:
            return
        # A bounded Codex scan may resume past session_meta. Retain only the
        # binding established at that exact saved position, never assume it.
        bound = any(offset == start and verified for offset, verified in positions)
        projector = Projector(session['provider'], session['conversation_id'], ProjectorState(bound=bound))
        confirmed, consumed = [], start
        with archive.iter_records(receipt, offset=start) as records:
            for count, record in enumerate(records, 1):
                if record.kind == 'partial' or record.end > receipt.record_boundary:
                    break
                entry = projector.feed(record).entry
                if entry and entry.speaker in (SPEAKER_USER, SPEAKER_NOTICE) and not entry.partial:
                    when = timestamp(entry.timestamp)
                    for row in pending:
                        created, ended = timestamp(row['created_at']), timestamp(row['ended_at'])
                        reminder = row['input_type'] == TYPE_NUDGE
                        expected = marker_text(row['marker'], TYPE_NUDGE, row['body']) if reminder else row['body']
                        speaker = SPEAKER_NOTICE if reminder else SPEAKER_USER
                        if (entry.speaker == speaker and entry.text == expected and when and created and when >= created
                                and (not row['ended_at'] or (ended and when <= ended))):
                            confirmed.append((row, entry))
                consumed = record.end
                if count >= 20000 or consumed - start >= 16 * 1024 * 1024:
                    break
        if confirmed:
            with self.runtime.database.transaction(operation='native_chat.delivery_reconcile') as uow:
                self.runtime.application.ensure_writes_open(uow)
                for row, entry in confirmed:
                    evidence = {**json.loads(row['evidence_json']), 'receipt_id': receipt.receipt_id,
                                'raw_offset': entry.offset, 'native_input_observed': True,
                                'native_input_observed_at': entry.timestamp,
                                'processing_confirmed': False}
                    if not store.settle_input(uow.connection, row['id'], 'uncertain', 'transcript_confirmed',
                                              evidence, ('uncertain',)):
                        continue
                    uow.connection.execute('UPDATE native_inputs SET body=NULL WHERE id=?', (row['id'],))
                    # Keep the producer's receipt consistent without sending a second connector notice.
                    adapter = self.runtime.selection.owner_input_sources.get(row['input_type'])
                    if adapter is not None:
                        adapter.reconcile(uow, row, entry, evidence)
        # Advance only after successful settlement. A crash safely rechecks archived evidence.
        matched = {row['id'] for row, _ in confirmed}
        for row in pending:
            if row['id'] in matched:
                self.offsets.pop(keys[row['id']], None)
            else:
                if consumed >= self.offsets.get(keys[row['id']], (0, False))[0]:
                    self.offsets[keys[row['id']]] = (consumed, projector.bound)
