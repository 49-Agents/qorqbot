"""Verified archive framing and SQL-only native conversation insertion.

The importing run is an admitted context, not a claim that its process emitted
every record in a resumed source. Raw archive bytes remain authoritative.
"""

from dataclasses import asdict
import hashlib
from uuid import UUID

from ..adapters.native_records import MAX_RECORD_BYTES, PARSER_VERSION, NativeTextItem, parse_native_record
from ..agents.registry import get_row
from ..contracts import CoreError, Counter, Model, OperationResult, Timestamp, canonical, canonical_hash
from ..storage.database import assert_external_io_allowed
from ..transcripts.archive import _context, _installation, _required
from ..transcripts.archive_models import coordinate, validate_binding
from ..transcripts.spool import RunBinding
from .ledger import entry_view
from .models import Entry, NativeProvenance, NativeSource, PublishJob


class ParseCheckpoint(Model):
    source_id: UUID
    source_epoch: UUID
    parser_version: str
    through_offset: Counter  # Every record through here has an outcome.
    scan_offset: Counter     # May be inside an unhandled partial record/chunk.
    ingested_at: Timestamp


def _lineage(tx, agent_id, run_id, source_id, source_epoch, *, binding=None):
    agent = get_row(tx, agent_id)
    installation = tx.connection.execute("SELECT * FROM installation").fetchone()
    run = _required(tx, "archive_runs", "run_id=?", (run_id,), "Native archive run is not registered")
    source = _required(tx, "archive_sources", "source_id=?", (source_id,), "Native source is not registered")
    epoch = _required(tx, "archive_epochs", "source_id=? AND source_epoch=?", (source_id, source_epoch),
        "Native source epoch is not registered")
    _required(tx, "archive_run_sources", "run_id=? AND source_id=? AND source_epoch=?",
        (run_id, source_id, source_epoch), "Native run/source link is not registered")
    owner = _required(tx, "session_owners", "owner_id=?", (run_id,), "Native run has no admitted owner")
    if binding is not None and any(run[key] != value for key, value in asdict(binding).items()):
        raise CoreError("target_changed", "Native archive run binding disagrees")
    if (any(owner[key] != run[key] for key in ("installation_id", "agent_id", "recovery_epoch",
            "environment_generation", "boot_id", "run_generation"))
            or owner["conversation_id"] != agent["conversation_id"]
            or source["agent_id"] != agent["id"] or run["agent_id"] != agent["id"]
            or source["installation_id"] != installation["installation_id"]
            or run["installation_id"] != installation["installation_id"]
            or source["native_session_id"] != owner["native_session_id"]):
        raise CoreError("target_changed", "Native source, run and owner lineage disagree")
    if source["source_type"] != "native" or source["parent_source_id"] is not None:
        raise CoreError("unsupported_capability", "Projection requires an admitted primary native source")
    if epoch["registered_recovery_epoch"] != installation["recovery_epoch"]:
        raise CoreError("not_ready", "Native archive epoch is held for recovery review")
    if epoch["conflicted"]:
        raise CoreError("idempotency_conflict", "Native archive epoch has a preserved conflict")
    return agent, owner, epoch


def _coverage(tx, source_id, source_epoch, start, end, watermark):
    if not 0 <= start <= end <= watermark:
        raise CoreError("target_changed", "Native raw range is not fully archived")
    cursor = start
    rows = tx.connection.execute("""SELECT offset,length FROM archive_chunks
        WHERE source_id=? AND source_epoch=? AND offset<? AND offset+length>?
        ORDER BY offset""", (source_id, source_epoch, end, start))
    for row in rows:
        if row["offset"] > cursor:
            raise CoreError("target_changed", "Native raw range has missing archive inventory")
        cursor = min(end, row["offset"] + row["length"])
    if cursor != end:
        raise CoreError("target_changed", "Native raw range has missing archive inventory")


def _activate(tx, source_id, source_epoch, version):
    row = tx.connection.execute("SELECT parser_version FROM native_projections WHERE source_id=? AND source_epoch=?",
        (source_id, source_epoch)).fetchone()
    if version != PARSER_VERSION or (row is not None and row[0] != version):
        raise CoreError("unsupported_capability", "Native parser replacement requires an explicit rebuild and switch")
    if row is None:
        tx.connection.execute("INSERT INTO native_projections VALUES (?,?,?)", (source_id, source_epoch, version))


def _input_hash(item, provenance):
    values = item.model_dump(mode="json")
    if item.timestamp_origin == "ingestion":
        # A retry samples another ingestion time. Missing native time is the
        # immutable fact; the first durable fallback must survive that retry.
        values["occurred_at"] = None
    return canonical_hash({"item": values, "provenance": provenance.model_dump(mode="json")})


def append_native(conversations, tx, agent_id, item, provenance, *, operation_id=None):
    """SQL-only writer; the trusted caller has verified and parsed the raw bytes."""
    tx.require_write()
    try:
        if not isinstance(item, NativeTextItem) or not isinstance(provenance, NativeProvenance):
            raise ValueError
        item = NativeTextItem.model_validate(item.model_dump())
        provenance = NativeProvenance.model_validate(provenance.model_dump())
        if item.occurred_at is None or provenance.length > MAX_RECORD_BYTES:
            raise ValueError
    except (ValueError, TypeError):
        raise CoreError("invalid_argument", "Native insertion requires typed content, time and bounded provenance") from None
    db, app = conversations.db, conversations.app
    source_id, epoch, run_id = str(provenance.source_id), str(provenance.source_epoch), str(provenance.run_id)
    agent, owner, archive = _lineage(tx, agent_id, run_id, source_id, epoch)
    if owner["provider"] != provenance.provider:
        raise CoreError("target_changed", "Native provider disagrees with the admitted owner")
    _coverage(tx, source_id, epoch, provenance.offset, provenance.offset + provenance.length, archive["host_archived_offset"])
    _activate(tx, source_id, epoch, provenance.parser_version)
    recovery_epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
    if operation_id is not None:
        parent = tx.connection.execute("SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)).fetchone()
        if parent is None or parent["actor_id"] != agent["actor_id"] or parent["recovery_epoch"] != recovery_epoch:
            raise CoreError("target_changed", "Native publication requires this agent's current operation receipt")
    identity = {"source_id": source_id, "source_epoch": epoch, "parser_version": provenance.parser_version,
        "native_id": item.native_id, "part_index": item.part_index}
    saved = tx.connection.execute("""SELECT * FROM conversation_native WHERE source_id=:source_id
        AND source_epoch=:source_epoch AND parser_version=:parser_version AND native_id=:native_id AND part_index=:part_index""",
        identity).fetchone()
    digest = _input_hash(item, provenance)
    if saved is not None:
        row = tx.connection.execute("SELECT * FROM conversation_entries WHERE entry_id=?", (saved["entry_id"],)).fetchone()
        entry = entry_view(row, saved)
        old_item = NativeTextItem(native_id=entry.source.native_id, role=entry.role, text=entry.text,
            part_index=entry.source.part_index, occurred_at=entry.occurred_at, timestamp_origin=entry.source.timestamp_origin)
        old_provenance = NativeProvenance(**entry.source.model_dump(exclude={"type", "native_id", "part_index", "timestamp_origin"}))
        if row["agent_id"] != agent["id"] or _input_hash(old_item, old_provenance) != digest:
            raise CoreError("idempotency_conflict", "Native identity already has different content or provenance")
        return entry
    cursor = tx.connection.execute("SELECT last_seq FROM conversation_cursors WHERE agent_id=?", (agent["id"],)).fetchone()
    if cursor is None:
        tx.connection.execute("INSERT INTO conversation_cursors VALUES (?,?,0)", (agent["id"], agent["conversation_id"]))
    sequence = (cursor[0] if cursor else 0) + 1
    if sequence > 2**63 - 1:
        raise CoreError("storage_blocked", "Conversation cursor exhausted")
    entry = Entry(entry_id=db.id_factory(), agent_id=agent["id"], conversation_id=agent["conversation_id"],
        conversation_seq=sequence, kind="message", role=item.role,
        direction="incoming" if item.role == "user" else "outgoing", text=item.text,
        sender_actor_id=None if item.role == "user" else agent["actor_id"],
        source=NativeSource(**provenance.model_dump(), native_id=item.native_id,
            part_index=item.part_index, timestamp_origin=item.timestamp_origin),
        occurred_at=item.occurred_at, recorded_at=db.clock())
    values = entry.model_dump(mode="json", exclude={"source"})
    values.update(provenance.model_dump(mode="json"))
    values.update(source_type="native", recording_date=entry.recorded_at[:10])
    # The companion table carries only the additional native-specific fields.
    columns = [key for key in values if key not in ("sha256", "parser_version")]
    tx.connection.execute(f"INSERT INTO conversation_entries ({','.join(columns)}) VALUES ({','.join(':'+key for key in columns)})", values)
    tx.connection.execute("""INSERT INTO conversation_native
        (entry_id,source_id,source_epoch,parser_version,native_id,part_index,sha256,timestamp_origin)
        VALUES (:entry_id,:source_id,:source_epoch,:parser_version,:native_id,:part_index,:sha256,:timestamp_origin)""",
        {**identity, "entry_id": str(entry.entry_id), "sha256": provenance.sha256, "timestamp_origin": item.timestamp_origin})
    tx.connection.execute("UPDATE conversation_cursors SET last_seq=? WHERE agent_id=?", (sequence, agent["id"]))
    if operation_id is None:
        operation_id = db.id_factory()
        result = OperationResult(ok=True, outcome="accepted", operation_id=operation_id,
            data={"entry_id": str(entry.entry_id), "agent_id": agent["id"], "conversation_seq": sequence, "publication": "queued"})
        tx.connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
            (str(operation_id), agent["actor_id"], "native_entry_import", canonical_hash(identity), digest,
             canonical(result), entry.recorded_at, recovery_epoch))
    date = entry.recorded_at[:10]
    if tx.connection.execute("SELECT 1 FROM conversation_publications WHERE agent_id=? AND recording_date=?",
            (agent["id"], date)).fetchone() is None:
        tx.connection.execute("INSERT INTO conversation_publications (agent_id,recording_date) VALUES (?,?)", (agent["id"], date))
    app.jobs.enqueue(tx, "conversation_publish", PublishJob(agent_id=agent["id"], date=date, required_seq=sequence),
        operation_id=operation_id, target_id=UUID(agent["id"]))
    db.fault("conversation_after_entry")
    return entry


class NativeProjector:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _checkpoint(self, tx, key):
        return _required(tx, "native_parse_checkpoints", "source_id=? AND source_epoch=? AND parser_version=?",
            key, "Native parse checkpoint is missing")

    def _chunk(self, context, source_id, source_epoch, offset):
        with self.db.read() as tx:
            row = tx.connection.execute("""SELECT offset,length,sha256 FROM archive_chunks
                WHERE source_id=? AND source_epoch=? AND offset<=? AND offset+length>?
                ORDER BY offset DESC LIMIT 1""", (source_id, source_epoch, offset, offset)).fetchone()
        if row is None:
            raise CoreError("target_changed", "Native parse coordinate has no committed archive chunk")
        chunk = self.app.archive.read_chunk(context, source_id, source_epoch, row["offset"])
        if (chunk.offset, chunk.length, chunk.sha256) != tuple(row):
            raise CoreError("target_changed", "Native archive inventory changed during read")
        return chunk

    def _tail(self, context, source_id, source_epoch, start, end):
        tail = bytearray()
        if end - start <= MAX_RECORD_BYTES:
            while start < end:
                chunk = self._chunk(context, source_id, source_epoch, start)
                stop = min(end, chunk.offset + chunk.length)
                tail.extend(chunk.payload[start - chunk.offset:stop - chunk.offset])
                start = stop
        return tail

    def consume(self, context, binding: RunBinding, source_id, source_epoch, *, max_bytes=1048576) -> ParseCheckpoint:
        """Scan at most max_bytes new bytes; reconstruct a bounded pending tail.

        All reads use committed Archive objects outside SQL writes. A saved scan
        cursor avoids stalling on records longer than one budget; through_offset
        advances only at complete records, atomically with their entries or gap.
        """
        assert_external_io_allowed()
        context = _context(context)
        coordinate(source_id, source_epoch)
        try:
            binding = validate_binding(binding)
            if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RECORD_BYTES:
                raise ValueError
        except (TypeError, ValueError):
            raise CoreError("invalid_argument", "Native projection requires a binding and 1–4194304 byte budget") from None
        with self.db.lock.use():
            return self._consume(context, binding, source_id, source_epoch, max_bytes)

    def _consume(self, context, binding, source_id, source_epoch, max_bytes):
        key = (source_id, source_epoch, PARSER_VERSION)
        now = self.db.clock()
        with self.db.transaction() as tx:
            _installation(tx, context)
            _, owner, epoch = _lineage(tx, binding.agent_id, binding.run_id, source_id, source_epoch, binding=binding)
            _activate(tx, *key)
            if tx.connection.execute("SELECT 1 FROM native_parse_checkpoints WHERE source_id=? AND source_epoch=? AND parser_version=?", key).fetchone() is None:
                tx.connection.execute("INSERT INTO native_parse_checkpoints VALUES (?,?,?,0,0,?)", (*key, now))
            saved = self._checkpoint(tx, key)
            target = min(epoch["host_archived_offset"], saved["scan_offset"] + max_bytes)
            if saved["scan_offset"] > epoch["host_archived_offset"]:
                raise CoreError("target_changed", "Native checkpoint exceeds its archive watermark")
        through, scan = saved["through_offset"], saved["scan_offset"]
        ingested = saved["ingested_at"] if scan > through else now
        pending = self._tail(context, source_id, source_epoch, through, scan) if scan < target else bytearray()
        while scan < target:
            chunk = self._chunk(context, source_id, source_epoch, scan)
            stop = min(target, chunk.offset + chunk.length)
            payload = chunk.payload[scan - chunk.offset:stop - chunk.offset]
            outcomes, position = [], 0
            while position < len(payload):
                newline = payload.find(b"\n", position)
                end = len(payload) if newline < 0 else newline + 1
                length = end - position
                if scan + length - through <= MAX_RECORD_BYTES:
                    pending.extend(payload[position:end])
                else:
                    pending.clear()
                scan += length
                position = end
                if newline >= 0:
                    if scan - through > MAX_RECORD_BYTES:
                        outcomes.append((through, scan, None, None, "record_too_large"))
                    else:
                        raw = bytes(pending)
                        outcome = parse_native_record(owner["provider"], owner["cli_version"], owner["native_session_id"], raw)
                        items = tuple(item.model_copy(update={"occurred_at": ingested}) if item.occurred_at is None else item
                            for item in outcome.items)
                        outcomes.append((through, scan, items, hashlib.sha256(raw).hexdigest(),
                            outcome.reason if outcome.disposition == "gap" else None))
                    pending.clear()
                    through, ingested = scan, now
            with self.db.transaction() as tx:
                _installation(tx, context)
                _, current_owner, current_epoch = _lineage(tx, binding.agent_id, binding.run_id, source_id, source_epoch, binding=binding)
                _activate(tx, *key)
                if self._checkpoint(tx, key) != saved:
                    raise CoreError("revision_conflict", "Native parser checkpoint changed during archive reads")
                _coverage(tx, source_id, source_epoch, saved["through_offset"], scan, current_epoch["host_archived_offset"])
                for start, end, items, digest, reason in outcomes:
                    if items:
                        tx.connection.execute("SAVEPOINT native_record")
                        try:
                            provenance = NativeProvenance(provider=current_owner["provider"], run_id=binding.run_id,
                                source_id=source_id, source_epoch=source_epoch, offset=start, length=end-start,
                                sha256=digest, parser_version=PARSER_VERSION)
                            for item in items:
                                self.app.conversations.append_native(tx, binding.agent_id, item, provenance)
                        except CoreError as exc:
                            if exc.code != "idempotency_conflict":
                                raise
                            tx.connection.execute("ROLLBACK TO native_record")
                            reason = "native_identity_conflict"
                        finally:
                            tx.connection.execute("RELEASE native_record")
                    if reason:
                        tx.connection.execute("INSERT INTO native_parse_gaps VALUES (?,?,?,?,?,?,?)",
                            (*key, start, end, reason, now))
                tx.connection.execute("""UPDATE native_parse_checkpoints SET through_offset=?,scan_offset=?,ingested_at=?
                    WHERE source_id=? AND source_epoch=? AND parser_version=?""", (through, scan, ingested, *key))
                self.db.fault("native_projection_before_commit")
                saved = self._checkpoint(tx, key)
        return ParseCheckpoint(**saved)
