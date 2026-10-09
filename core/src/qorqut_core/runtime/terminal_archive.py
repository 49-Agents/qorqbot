"""Bounded archival of an already recorded shell, independent of native owners.

The observer supplies an owned terminal database and inspected shell. Connections
belong to this call only; import/construction starts no work. A saved exact host
receipt, never a historical watermark, authorizes local ACK and reclamation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from time import monotonic

from ..contracts import CoreError, Invocation
from ..runner.shell_models import ShellCapture, ShellSpec, ShellView, absolute_path
from ..runner.storage import configure
from ..storage.database import assert_external_io_allowed
from ..transcripts.archive import Archive
from ..transcripts.archive_models import (
    ArchiveMetadata, ArchiveReceipt, chunk_coordinates, validate_binding, validate_chunk,
    validate_source,
)
from ..transcripts.spool import CHUNK_BYTES, CaptureSpool, Chunk, ChunkReceipt, RunBinding, Source


_RANGE_CHUNKS = 128
_OBSERVATION_BYTES = 1024 * 1024


@contextmanager
def _local(database):
    database = absolute_path(database)
    info = database.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or database.resolve() != database):
        raise CoreError("target_changed", "Terminal archive store ownership changed")
    # Never create an empty database in place of a missing recorded shell.
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        configure(connection)
        yield connection
    finally:
        connection.close()


def saved_capture(database: Path, shell: ShellView) -> ShellCapture:
    """Refresh counters without probing, changing health or asserting liveness."""
    with _local(database) as local:
        row = local.execute("SELECT * FROM capture_epochs WHERE source_id=? AND source_epoch=?",
                            (str(shell.source_id), str(shell.source_epoch))).fetchone()
        if row is None:
            raise CoreError("target_changed", "Terminal capture is unavailable")
        return ShellCapture(**{name: row[name] for name in ShellCapture.model_fields})


def _producer(local, spool, shell):
    row = local.execute("SELECT * FROM runner_shell_terminals WHERE terminal_id=?",
                        (str(shell.terminal_id),)).fetchone()
    if row is None:
        raise CoreError("target_changed", "Terminal recording is unavailable")
    spec = ShellSpec.model_validate_json(row["spec_json"])
    if (spec != shell.spec or row["installation_id"] != str(spec.installation_id)
            or row["agent_id"] != str(spec.agent_id)
            or row["spec_hash"] != hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
            or row["source_id"] != str(shell.source_id) or row["source_epoch"] != str(shell.source_epoch)):
        raise CoreError("target_changed", "Terminal recording binding changed")
    binding = validate_binding(RunBinding(str(spec.installation_id), str(spec.agent_id),
        str(spec.recovery_epoch), spec.environment_generation, row["boot_id"], row["terminal_id"], 1))
    key = row["source_id"], row["source_epoch"]
    if spool.recording_binding(*key, binding.run_id) != binding:
        raise CoreError("target_changed", "Terminal recording lineage changed")
    source_row = local.execute("SELECT * FROM capture_sources WHERE source_id=?", (key[0],)).fetchone()
    if source_row is None:
        raise CoreError("target_changed", "Terminal capture source is unavailable")
    source = validate_source(Source(**{name: source_row[name] for name in Source.__dataclass_fields__}))
    if (source.source_type != "terminal" or source.native_session_id is not None
            or source.installation_id != binding.installation_id or source.agent_id != binding.agent_id):
        raise CoreError("target_changed", "Terminal capture source binding changed")
    epoch = spool.state(*key)
    metadata = ArchiveMetadata(**{name: epoch[name] for name in ArchiveMetadata.model_fields})
    return binding, source, key, metadata


def _receipt(value, intent):
    if not isinstance(value, ArchiveReceipt):
        raise ValueError("Terminal archive receipt is unavailable")
    value = ArchiveReceipt.model_validate(value.model_dump())
    if (validate_binding(value.binding) != RunBinding(**json.loads(intent["binding"]))
            or value.source_id != intent["source_id"] or value.source_epoch != intent["source_epoch"]
            or value.recovery_epoch != intent["recovery_epoch"]
            or value.host_archived_offset != intent["offset"] + intent["length"]):
        raise ValueError("Terminal archive receipt disagrees with transfer intent")
    return value


def _manifest_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Terminal archive manifest has duplicate fields")
    return value


def _manifest(intent):
    """NULL alone denotes the old single-chunk format, including saved receipts."""
    if intent["chunks_json"] is None:
        return (ChunkReceipt(intent["offset"], intent["length"], intent["sha256"]),)
    raw = intent["chunks_json"]
    # A compact maximal manifest is under 16 KiB. Bound parsing too, allowing
    # whitespace without letting a corrupt local intent allocate unbounded JSON.
    if type(raw) is not str or len(raw) > 32 * 1024:
        raise ValueError("Terminal archive manifest is oversized")
    try:
        value = json.loads(raw, object_pairs_hook=_manifest_object)
    except RecursionError:
        raise ValueError("Terminal archive manifest is malformed") from None
    if (type(value) is not dict or set(value) != {"version", "chunks"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["chunks"]) is not list or not 1 <= len(value["chunks"]) <= _RANGE_CHUNKS):
        raise ValueError("Terminal archive manifest is unsupported")
    entries, cursor = [], intent["offset"]
    for item in value["chunks"]:
        if type(item) is not dict or set(item) != {"offset", "length", "sha256"}:
            raise ValueError("Terminal archive manifest entry is malformed")
        entry = ChunkReceipt(**item)
        chunk_coordinates(Chunk(intent["source_id"], intent["source_epoch"],
                                entry.offset, entry.length, entry.sha256, b""))
        if entry.offset != cursor:
            raise ValueError("Terminal archive manifest is not contiguous")
        cursor += entry.length
        entries.append(entry)
    if cursor != intent["offset"] + intent["length"]:
        raise ValueError("Terminal archive manifest disagrees with intent")
    return tuple(entries)


def _range(spool, key, offset, *, count, byte_limit):
    """Assemble whole originals through bounded pages, without waiting for more."""
    entries, payload = [], bytearray()
    cursor = offset
    while len(entries) < count and len(payload) < byte_limit:
        limit = min(16, count - len(entries))
        page = spool.pending(*key, after=cursor, limit=limit)
        if len(page.chunks) > limit:
            raise ValueError("Terminal archive pending page is oversized")
        page_end = cursor
        for chunk in page.chunks:
            validate_chunk(chunk)
            if (chunk.source_id, chunk.source_epoch) != key or chunk.offset != page_end:
                raise ValueError("Terminal archive cursor disagrees with chunk")
            page_end += chunk.length
        if page.next_offset != page_end:
            raise ValueError("Terminal archive pending cursor changed")
        if not page.chunks:
            break
        for chunk in page.chunks:
            if len(payload) + chunk.length > byte_limit:
                break
            entries.append(ChunkReceipt(chunk.offset, chunk.length, chunk.sha256))
            payload.extend(chunk.payload)
            cursor = chunk.offset + chunk.length
        if cursor != page.next_offset:
            break  # Leave the next whole chunk for a later range/observation.
    if not entries:
        return None, ()
    data = bytes(payload)
    return Chunk(*key, offset, len(data), hashlib.sha256(data).hexdigest(), data), tuple(entries)


def _check_ledger(spool, source, key, intent, entries):
    """Corroborate every original boundary even after payload reclamation."""
    cursor, end = intent["offset"], intent["offset"] + intent["length"]
    checked = 0
    while checked < len(entries):
        limit = min(16, len(entries) - checked)
        page = spool.receipts(*key, after=cursor, through=end, limit=limit)
        expected = entries[checked:checked + limit]
        if (page.source != source or page.source_epoch != key[1] or page.through_offset != end
                or page.receipts != expected
                or page.next_offset != expected[-1].offset + expected[-1].length):
            raise ValueError("Terminal archive intent disagrees with capture")
        cursor = page.next_offset
        checked += len(expected)


def transfer(database: Path, shell: ShellView, archive: Archive, context: Invocation) -> None:
    """Transfer at most 128 original chunks / 1 MiB, including resumed intents.

    Each range contains at most 128 originals / 256 KiB. Allow one whole pending
    range, then stop starting ranges after 200 ms. Count originals, not receives.
    Receipt/ACK/reclamation always finish; I/O can exceed this scheduling budget.

    Caller retains its workspace/provisioning guards. Keep the host lifetime and
    effects guards through local cleanup as well as receive; no SQL transaction
    surrounds host filesystem or recorder I/O. Stopped/unprobed shells can drain.
    """
    deadline = monotonic() + 0.2
    transferred_bytes = completed = 0
    assert_external_io_allowed()
    with archive.db.lock.use(), archive.db.lock.effects, _local(database) as local:
        with archive.db.read() as tx:
            installation = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
            actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?",
                                          (str(context.actor_id),)).fetchone()
        if (installation["installation_id"] != str(shell.spec.installation_id)
                or installation["recovery_epoch"] != str(shell.spec.recovery_epoch)
                or tuple(actor or ()) != ("agent", str(shell.spec.agent_id))):
            raise CoreError("target_changed", "Terminal archive invocation binding changed")
        with CaptureSpool(database) as spool:
            binding, source, key, metadata = _producer(local, spool, shell)
            host = archive.register(context, binding, source, key[1], metadata)
            if host.recovery_held:
                raise CoreError("not_ready", "Terminal archive is held for recovery")
            if host.conflicted:
                raise CoreError("idempotency_conflict", "Terminal archive has a preserved conflict")
            with local:
                local.execute("BEGIN IMMEDIATE")
                local.execute("""CREATE TABLE IF NOT EXISTS terminal_archive_transfers (
                    source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
                    binding TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset >= 0),
                    length INTEGER NOT NULL CHECK(length BETWEEN 1 AND 262144),
                    sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                    recovery_epoch TEXT NOT NULL, receipt TEXT, chunks_json TEXT,
                    PRIMARY KEY(source_id,source_epoch),
                    FOREIGN KEY(source_id,source_epoch) REFERENCES capture_epochs(source_id,source_epoch)
                )""")
                if "chunks_json" not in {row["name"] for row in local.execute(
                        "PRAGMA table_info(terminal_archive_transfers)")}:
                    local.execute("ALTER TABLE terminal_archive_transfers ADD COLUMN chunks_json TEXT")
            while completed < _RANGE_CHUNKS:
                if completed and (transferred_bytes >= _OBSERVATION_BYTES or monotonic() >= deadline):
                    break
                intent = local.execute("SELECT * FROM terminal_archive_transfers WHERE source_id=? AND source_epoch=?",
                                       key).fetchone()
                acknowledged = spool.state(*key)["host_archived_offset"]
                if intent is None:
                    chunk, entries = _range(spool, key, acknowledged, count=_RANGE_CHUNKS - completed,
                        byte_limit=min(CHUNK_BYTES, _OBSERVATION_BYTES - transferred_bytes))
                    if chunk is None:
                        break  # An empty page never authorizes cleanup.
                    with local:
                        local.execute("""INSERT INTO terminal_archive_transfers
                            (source_id,source_epoch,binding,offset,length,sha256,recovery_epoch,chunks_json)
                            VALUES (?,?,?,?,?,?,?,?)""",
                            (*key, json.dumps(asdict(binding), sort_keys=True), chunk.offset,
                             chunk.length, chunk.sha256, host.recovery_epoch,
                             json.dumps({"version": 1, "chunks": [asdict(e) for e in entries]},
                                        separators=(",", ":"))))
                    intent = local.execute("SELECT * FROM terminal_archive_transfers WHERE source_id=? AND source_epoch=?",
                                           key).fetchone()
                original = validate_binding(RunBinding(**json.loads(intent["binding"])))
                chunk_coordinates(Chunk(*key, intent["offset"], intent["length"], intent["sha256"], b""))
                end = intent["offset"] + intent["length"]
                if original != binding or intent["recovery_epoch"] != host.recovery_epoch:
                    raise CoreError("target_changed", "Terminal archive transfer binding changed")
                if acknowledged not in (intent["offset"], end):
                    raise ValueError("Terminal archive ACK disagrees with intent")
                entries = _manifest(intent)
                _check_ledger(spool, source, key, intent, entries)
                # reclaim() releases a prefix. Do not sweep older retained data
                # for which this callable has no saved receipt authorization.
                if local.execute("SELECT 1 FROM capture_chunks WHERE source_id=? AND source_epoch=? "
                                 "AND offset<? AND payload IS NOT NULL LIMIT 1", (*key, intent["offset"])).fetchone():
                    raise ValueError("Terminal archive has unconfirmed retained payload")
                if intent["receipt"] is None:
                    if acknowledged != intent["offset"]:
                        raise ValueError("Terminal archive ACK has no saved receipt")
                    chunk, retained = _range(spool, key, intent["offset"], count=len(entries),
                                             byte_limit=intent["length"])
                    if (chunk is None or retained != entries or
                            (chunk.offset, chunk.length, chunk.sha256) !=
                            (intent["offset"], intent["length"], intent["sha256"])):
                        raise ValueError("Terminal archive intent disagrees with payload")
                    receipt = _receipt(archive.receive(context, original, chunk), intent)
                    with local:
                        local.execute("UPDATE terminal_archive_transfers SET receipt=? WHERE source_id=? AND source_epoch=?",
                                      (receipt.model_dump_json(), *key))
                else:
                    receipt = _receipt(ArchiveReceipt.model_validate_json(intent["receipt"]), intent)
                spool.acknowledge(*key, receipt.host_archived_offset)
                spool.reclaim(*key)
                with local:
                    local.execute("DELETE FROM terminal_archive_transfers WHERE source_id=? AND source_epoch=?", key)
                transferred_bytes += intent["length"]
                completed += len(entries)
