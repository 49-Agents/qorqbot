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

from ..contracts import CoreError, Invocation
from ..runner.shell_models import ShellCapture, ShellSpec, ShellView, absolute_path
from ..runner.storage import configure
from ..storage.database import assert_external_io_allowed
from ..transcripts.archive import Archive
from ..transcripts.archive_models import (
    ArchiveMetadata, ArchiveReceipt, chunk_coordinates, validate_binding, validate_chunk,
    validate_source,
)
from ..transcripts.spool import CaptureSpool, Chunk, RunBinding, Source


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


def transfer(database: Path, shell: ShellView, archive: Archive, context: Invocation) -> None:
    """Transfer at most four original chunks (1 MiB), including resumed intents.

    Caller retains its workspace/provisioning guards. Keep the host lifetime and
    effects guards through local cleanup as well as receive; no SQL transaction
    surrounds host filesystem or recorder I/O. Stopped/unprobed shells can drain.
    """
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
                local.execute("""CREATE TABLE IF NOT EXISTS terminal_archive_transfers (
                    source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
                    binding TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset >= 0),
                    length INTEGER NOT NULL CHECK(length BETWEEN 1 AND 262144),
                    sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                    recovery_epoch TEXT NOT NULL, receipt TEXT,
                    PRIMARY KEY(source_id,source_epoch),
                    FOREIGN KEY(source_id,source_epoch) REFERENCES capture_epochs(source_id,source_epoch)
                )""")
            for _ in range(4):
                chunk = None
                intent = local.execute("SELECT * FROM terminal_archive_transfers WHERE source_id=? AND source_epoch=?",
                                       key).fetchone()
                acknowledged = spool.state(*key)["host_archived_offset"]
                if intent is None:
                    page = spool.pending(*key, limit=1)
                    if not page.chunks:
                        break  # An empty page never authorizes cleanup.
                    chunk = validate_chunk(page.chunks[0])
                    if chunk.offset != acknowledged:
                        raise ValueError("Terminal archive cursor disagrees with chunk")
                    with local:
                        local.execute("INSERT INTO terminal_archive_transfers VALUES (?,?,?,?,?,?,?,NULL)",
                            (*key, json.dumps(asdict(binding), sort_keys=True), chunk.offset,
                             chunk.length, chunk.sha256, host.recovery_epoch))
                    intent = local.execute("SELECT * FROM terminal_archive_transfers WHERE source_id=? AND source_epoch=?",
                                           key).fetchone()
                original = validate_binding(RunBinding(**json.loads(intent["binding"])))
                chunk_coordinates(Chunk(*key, intent["offset"], intent["length"], intent["sha256"], b""))
                end = intent["offset"] + intent["length"]
                if original != binding or intent["recovery_epoch"] != host.recovery_epoch:
                    raise CoreError("target_changed", "Terminal archive transfer binding changed")
                if acknowledged not in (intent["offset"], end):
                    raise ValueError("Terminal archive ACK disagrees with intent")
                # Corroborate the exact original ledger even after ACK/reclaim.
                ledger = spool.receipts(*key, after=intent["offset"], through=end, limit=1).receipts
                if (len(ledger) != 1 or (ledger[0].offset, ledger[0].length, ledger[0].sha256)
                        != (intent["offset"], intent["length"], intent["sha256"])):
                    raise ValueError("Terminal archive intent disagrees with capture")
                # reclaim() releases a prefix. Do not sweep older retained data
                # for which this callable has no saved receipt authorization.
                if local.execute("SELECT 1 FROM capture_chunks WHERE source_id=? AND source_epoch=? "
                                 "AND offset<? AND payload IS NOT NULL LIMIT 1", (*key, intent["offset"])).fetchone():
                    raise ValueError("Terminal archive has unconfirmed retained payload")
                if intent["receipt"] is None:
                    if acknowledged != intent["offset"]:
                        raise ValueError("Terminal archive ACK has no saved receipt")
                    if chunk is None:
                        page = spool.pending(*key, limit=1)
                        if not page.chunks:
                            raise ValueError("Terminal archive intent has no pending payload")
                        chunk = validate_chunk(page.chunks[0])
                    if (chunk.offset, chunk.length, chunk.sha256) != (intent["offset"], intent["length"], intent["sha256"]):
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
