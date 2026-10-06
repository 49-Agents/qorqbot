"""Internal durable host byte intake, not a collector or native authenticator.

Only receive's returned receipt can authorize a future collector's ACK. SQL
inventory and raw reads are historical evidence. No method sends an ACK, tails
a source, repairs a committed object or releases a restored archive hold.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import stat

from ..agents.workspace import Workspaces, child_directory, rename_noreplace, same_child
from ..contracts import CoreError, Invocation, canonical_hash
from ..storage.database import Database, assert_external_io_allowed
from ..storage.events import EventInput, append_event
from ..storage.files import directory
from .archive_models import (
    ArchiveMetadata, ArchiveReceipt, ArchiveState, canonical_id, chunk_coordinates, coordinate,
    validate_binding, validate_chunk, validate_source,
)
from .spool import Chunk, RunBinding, Source


def _context(context):
    try:
        if not isinstance(context, Invocation):
            raise ValueError
        return Invocation.model_validate(context.model_dump())
    except (ValueError, TypeError, AttributeError):
        raise CoreError("invalid_argument", "Invalid archive invocation") from None


def _installation(tx, context):
    # Reuse the shared registered-caller rule without an import-time cycle.
    from ..app import _check_actor

    _check_actor(tx, context)
    return dict(tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone())


def _required(tx, table, where, values, message):
    row = tx.connection.execute(f"SELECT * FROM {table} WHERE {where}", values).fetchone()
    if row is None:
        raise CoreError("not_found", message)
    return dict(row)


def _compatible(row, fields):
    if any(row[name] != value for name, value in fields.items()):
        raise CoreError("idempotency_conflict", "Archive identity already has different evidence")


def _state(installation, epoch):
    return ArchiveState(source_id=epoch["source_id"], source_epoch=epoch["source_epoch"],
        host_archived_offset=epoch["host_archived_offset"], recovery_epoch=installation["recovery_epoch"],
        registered_recovery_epoch=epoch["registered_recovery_epoch"],
        recovery_held=epoch["registered_recovery_epoch"] != installation["recovery_epoch"],
        conflicted=bool(epoch["conflicted"]))


def _admit(installation, epoch):
    if epoch["registered_recovery_epoch"] != installation["recovery_epoch"]:
        raise CoreError("not_ready", "Archive epoch is held for explicit recovery review")


def _name(chunk):
    return f"{chunk.offset}-{chunk.length}-{chunk.sha256}.bin"


def _path(agent_id, chunk, conflict=False):
    parent = f"agents/{agent_id}/history/chunks/{chunk.source_id}/{chunk.source_epoch}/"
    return parent + ("conflicts/" if conflict else "") + _name(chunk)


class Archive:
    def __init__(self, database: Database, data_root: Path):
        # Passive assembly, including when the supplied root does not exist.
        self.db, self.root = database, data_root
        self._workspaces = Workspaces(database, data_root)

    def register(self, context: Invocation, binding: RunBinding, source: Source,
                 source_epoch: str, metadata: ArchiveMetadata) -> ArchiveState:
        context = _context(context)
        try:
            binding, source = validate_binding(binding), validate_source(source)
            canonical_id(source_epoch)
            if not isinstance(metadata, ArchiveMetadata):
                raise ValueError
            metadata = ArchiveMetadata.model_validate(metadata.model_dump())
            if metadata.previous_epoch == source_epoch:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CoreError("invalid_argument", "Invalid archive registration") from None
        metadata_digest = canonical_hash(metadata)
        with self.db.transaction() as tx:
            installation = _installation(tx, context)
            if (binding.installation_id != installation["installation_id"]
                    or source.installation_id != binding.installation_id or source.agent_id != binding.agent_id):
                raise CoreError("target_changed", "Archive installation or agent lineage disagrees")
            _required(tx, "agents", "id=?", (binding.agent_id,), "Archive agent is not registered")
            run_fields, source_fields = asdict(binding), asdict(source)
            metadata_fields = metadata.model_dump()
            actor, now = str(context.actor_id), self.db.clock()
            run = tx.connection.execute("SELECT * FROM archive_runs WHERE run_id=?", (binding.run_id,)).fetchone()
            if run is None:
                tx.connection.execute("""INSERT INTO archive_runs
                    (run_id,installation_id,agent_id,recovery_epoch,environment_generation,boot_id,
                        run_generation,registered_by,registered_at)
                    VALUES (:run_id,:installation_id,:agent_id,:recovery_epoch,:environment_generation,
                        :boot_id,:run_generation,:registered_by,:registered_at)""",
                    {**run_fields, "registered_by": actor, "registered_at": now})
            else:
                _compatible(run, run_fields)
            current = tx.connection.execute("SELECT * FROM archive_sources WHERE source_id=?", (source.source_id,)).fetchone()
            if current is None:
                if source.parent_source_id is not None:
                    if source.parent_source_id == source.source_id:
                        raise CoreError("invalid_argument", "Archive source cannot parent itself")
                    parent = _required(tx, "archive_sources", "source_id=?", (source.parent_source_id,),
                                       "Archive parent is not registered")
                    if (parent["installation_id"], parent["agent_id"]) != (source.installation_id, source.agent_id):
                        raise CoreError("target_changed", "Archive parent lineage disagrees")
                if metadata.previous_epoch is not None:
                    raise CoreError("not_found", "First archive epoch has no predecessor")
                tx.connection.execute("""INSERT INTO archive_sources
                    (source_id,installation_id,agent_id,source_type,native_session_id,parent_source_id,registered_by,registered_at)
                    VALUES (:source_id,:installation_id,:agent_id,:source_type,:native_session_id,
                        :parent_source_id,:registered_by,:registered_at)""",
                    {**source_fields, "registered_by": actor, "registered_at": now})
                append_event(tx, EventInput(kind="archive.source_registered", actor_id=context.actor_id,
                    agent_id=binding.agent_id, payload=source_fields))
            else:
                _compatible(current, source_fields)
            epoch = tx.connection.execute("SELECT * FROM archive_epochs WHERE source_id=? AND source_epoch=?",
                                          (source.source_id, source_epoch)).fetchone()
            if epoch is None:
                if current is not None and metadata.previous_epoch is None:
                    raise CoreError("invalid_argument", "A subsequent archive epoch requires its predecessor")
                if metadata.previous_epoch is not None:
                    prior = _required(tx, "archive_epochs", "source_id=? AND source_epoch=?",
                        (source.source_id, metadata.previous_epoch), "Archive predecessor is not registered")
                    _admit(installation, prior)
                tx.connection.execute("""INSERT INTO archive_epochs
                    (source_id,source_epoch,capability_evidence,locator,file_identity,native_created_at,
                        previous_epoch,divergence_reason,registered_recovery_epoch,registered_by,registered_at)
                    VALUES (:source_id,:source_epoch,:capability_evidence,:locator,:file_identity,:native_created_at,
                        :previous_epoch,:divergence_reason,:registered_recovery_epoch,:registered_by,:registered_at)""",
                    {**metadata_fields, "source_id": source.source_id, "source_epoch": source_epoch,
                     "registered_recovery_epoch": installation["recovery_epoch"], "registered_by": actor, "registered_at": now})
                # Full evidence stays in the immutable row. Its hash keeps the
                # event bounded even when every metadata field contains 4096
                # four-byte characters, exceeding the journal's aggregate cap.
                append_event(tx, EventInput(kind="archive.epoch_registered", actor_id=context.actor_id,
                    agent_id=binding.agent_id, payload={"source_id": source.source_id, "source_epoch": source_epoch,
                        "previous_epoch": metadata.previous_epoch, "metadata_sha256": metadata_digest,
                        "registered_recovery_epoch": installation["recovery_epoch"]}))
                epoch = _required(tx, "archive_epochs", "source_id=? AND source_epoch=?",
                    (source.source_id, source_epoch), "Archive epoch is not registered")
            else:
                _compatible(epoch, metadata_fields)
            linked = tx.connection.execute("SELECT 1 FROM archive_run_sources WHERE run_id=? AND source_id=? AND source_epoch=?",
                                            (binding.run_id, source.source_id, source_epoch)).fetchone()
            if linked is None:
                _admit(installation, epoch)
                tx.connection.execute("INSERT INTO archive_run_sources VALUES (?,?,?,?,?)",
                    (binding.run_id, source.source_id, source_epoch, actor, now))
                append_event(tx, EventInput(kind="archive.run_linked", actor_id=context.actor_id, agent_id=binding.agent_id,
                    payload={**run_fields, "source_id": source.source_id, "source_epoch": source_epoch}))
            self.db.fault("archive_register_before_commit")
            result = _state(installation, epoch)
        return result

    def state(self, context: Invocation, source_id: str, source_epoch: str) -> ArchiveState:
        context = _context(context)
        coordinate(source_id, source_epoch)
        with self.db.read() as tx:
            installation = _installation(tx, context)
            epoch = _required(tx, "archive_epochs", "source_id=? AND source_epoch=?", (source_id, source_epoch),
                              "Archive epoch is not registered")
            return _state(installation, epoch)

    def _intake(self, tx, context, binding, chunk):
        installation = _installation(tx, context)
        if binding.installation_id != installation["installation_id"]:
            raise CoreError("target_changed", "Archive installation changed")
        run = _required(tx, "archive_runs", "run_id=?", (binding.run_id,), "Archive run is not registered")
        _compatible(run, asdict(binding))
        source = _required(tx, "archive_sources", "source_id=?", (chunk.source_id,), "Archive source is not registered")
        if (source["installation_id"], source["agent_id"]) != (binding.installation_id, binding.agent_id):
            raise CoreError("target_changed", "Archive source lineage disagrees")
        epoch = _required(tx, "archive_epochs", "source_id=? AND source_epoch=?", (chunk.source_id, chunk.source_epoch),
                          "Archive epoch is not registered")
        _required(tx, "archive_run_sources", "run_id=? AND source_id=? AND source_epoch=?",
                  (binding.run_id, chunk.source_id, chunk.source_epoch), "Archive run/source link is not registered")
        _admit(installation, epoch)
        agent = _required(tx, "agents", "id=?", (binding.agent_id,), "Archive agent is not registered")
        if agent["workspace_initialized_at"] is None:
            raise CoreError("not_ready", "Archive requires an initialized agent workspace")
        key = (chunk.source_id, chunk.source_epoch, chunk.offset)
        if epoch["conflicted"]:
            known = tx.connection.execute("""SELECT * FROM archive_conflicts
                WHERE source_id=? AND source_epoch=? AND offset=? AND length=? AND sha256=?""",
                (*key, chunk.length, chunk.sha256)).fetchone()
            if known is None:
                raise CoreError("idempotency_conflict", "Archive epoch is closed by a preserved conflict")
            mode, receipt = "conflict_repeat", dict(known)
        elif chunk.offset > epoch["host_archived_offset"]:
            raise CoreError("revision_conflict", "Archive intake must be contiguous")
        elif chunk.offset == epoch["host_archived_offset"]:
            mode, receipt = "append", None
        else:
            accepted = tx.connection.execute("SELECT * FROM archive_chunks WHERE source_id=? AND source_epoch=? AND offset=?", key).fetchone()
            if accepted is not None and (accepted["length"], accepted["sha256"]) == (chunk.length, chunk.sha256):
                mode, receipt = "duplicate", dict(accepted)
            else:
                mode, receipt = "conflict", None
        if receipt is not None and receipt["object_path"] != _path(binding.agent_id, chunk, mode == "conflict_repeat"):
            raise CoreError("uncertain_effect", "Archive object inventory disagrees with its coordinates")
        return installation, agent, epoch, mode, receipt

    @contextmanager
    def _object_directory(self, agent, installation, chunk, *, conflict=False, create=False):
        with ExitStack() as stack:
            root = stack.enter_context(directory(self.root))
            links = []

            def descend(parent, name, make=False):
                child = stack.enter_context(child_directory(parent, name, create=make))
                links.append((parent, name, child))
                if make:
                    # Also sync links found after a crash between mkdir/fsync.
                    os.fsync(parent)
                    os.fsync(child)
                return child

            parent = descend(root, "agents")
            final = descend(parent, agent["id"])

            def guard():
                for parent_fd, name, child in links:
                    same_child(parent_fd, name, child)
                self._workspaces._marker(final, agent, installation, complete=True)
                with directory(self.root) as current_root:
                    if _identity(os.fstat(root)) != _identity(os.fstat(current_root)):
                        raise CoreError("target_changed", "Archive installation directory changed")

            guard()
            history = descend(final, "history")
            chunks = descend(history, "chunks")
            guard()
            source = descend(chunks, chunk.source_id, create)
            guard()
            epoch = descend(source, chunk.source_epoch, create)
            guard()
            target = descend(epoch, "conflicts", create) if conflict else epoch
            guard()
            yield target, guard

    def _verify(self, fd, chunk, *, sync=False):
        name = _name(chunk)
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise CoreError("uncertain_effect", "Archive object cannot be safely opened") from None
        with os.fdopen(handle, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != chunk.length:
                raise CoreError("uncertain_effect", "Archive object is not the expected immutable regular file")
            payload = stream.read(chunk.length + 1)
            end = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (_fingerprint(before) != _fingerprint(end) or _fingerprint(end) != _fingerprint(current)
                    or len(payload) != chunk.length or hashlib.sha256(payload).hexdigest() != chunk.sha256):
                raise CoreError("uncertain_effect", "Archive object is damaged or changed during verification")
            if sync:
                os.fsync(stream.fileno())
        return payload

    def _publish(self, fd, chunk, guard, *, committed):
        guard()
        existing = self._verify(fd, chunk, sync=True)
        if existing is None:
            if committed:
                raise CoreError("storage_blocked", "Committed archive object is missing; explicit repair is required")
            guard()
            temporary = ".qorqut-archive-stage-" + canonical_id(str(self.db.id_factory()))
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=fd)
            # Private uncommitted stages remain evidence after failure. Never
            # scan for or remove somebody else's files on replay or ordinary open.
            with os.fdopen(handle, "wb") as stream:
                stream.write(chunk.payload)
                stream.flush()
                self.db.fault("archive_before_stage_fsync")
                os.fsync(stream.fileno())
            self.db.fault("archive_after_stage_fsync")
            guard()
            self.db.fault("archive_before_publish")
            guard()
            try:
                rename_noreplace(fd, temporary, _name(chunk))
            except FileExistsError:
                # The destination, including any racing occupant, must prove
                # its exact bytes; no replacing rename fallback is permitted.
                pass
            self.db.fault("archive_after_publish")
            if self._verify(fd, chunk, sync=True) is None:
                raise CoreError("uncertain_effect", "Published archive object disappeared")
        self.db.fault("archive_before_parent_fsync")
        os.fsync(fd)
        self.db.fault("archive_after_parent_fsync")
        guard()

    def receive(self, context: Invocation, binding: RunBinding, chunk: Chunk) -> ArchiveReceipt:
        assert_external_io_allowed()
        context = _context(context)
        with self.db.lock.use(), self.db.lock.effects:
            try:
                binding, chunk = validate_binding(binding), validate_chunk(chunk)
            except (ValueError, TypeError, AttributeError):
                raise CoreError("invalid_argument", "Invalid archive chunk or binding") from None
            with self.db.read() as tx:
                snapshot = self._intake(tx, context, binding, chunk)
            installation, agent, epoch, mode, receipt = snapshot
            conflict = mode in ("conflict", "conflict_repeat")
            with self._object_directory(agent, installation, chunk, conflict=conflict,
                                        create=receipt is None) as (fd, guard):
                self._publish(fd, chunk, guard, committed=receipt is not None)
                self.db.fault("archive_before_finalize")
                guard()
                with self.db.transaction(writable=receipt is None) as tx:
                    current = self._intake(tx, context, binding, chunk)
                    # File evidence belongs to the exact snapshot checked before
                    # I/O. Revalidate host epoch, immutable lineage and cursor.
                    if current != snapshot:
                        raise CoreError("target_changed", "Archive intake coordinates changed before commit")
                    if receipt is None:
                        table = "archive_conflicts" if conflict else "archive_chunks"
                        tx.connection.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?,?)",
                            (chunk.source_id, chunk.source_epoch, chunk.offset, chunk.length, chunk.sha256,
                             _path(binding.agent_id, chunk, conflict), binding.run_id, str(context.actor_id), self.db.clock()))
                        self.db.fault("archive_after_receipt_insert")
                        if conflict:
                            tx.connection.execute("UPDATE archive_epochs SET conflicted=1 WHERE source_id=? AND source_epoch=?",
                                                  (chunk.source_id, chunk.source_epoch))
                            append_event(tx, EventInput(kind="archive.chunk_conflict", actor_id=context.actor_id,
                                agent_id=binding.agent_id, payload={"run_id": binding.run_id, "source_id": chunk.source_id,
                                    "source_epoch": chunk.source_epoch, "offset": chunk.offset,
                                    "length": chunk.length, "sha256": chunk.sha256}))
                        else:
                            tx.connection.execute("UPDATE archive_epochs SET host_archived_offset=? WHERE source_id=? AND source_epoch=?",
                                                  (chunk.offset + chunk.length, chunk.source_id, chunk.source_epoch))
                        self.db.fault("archive_after_watermark")
                    watermark = (chunk.offset + chunk.length) if mode == "append" else epoch["host_archived_offset"]
                    self.db.fault("archive_before_receipt_commit")
                self.db.fault("archive_after_receipt_commit")
            if conflict:
                raise CoreError("idempotency_conflict", "Archive conflict is preserved; epoch intake and ACKs are closed")
            self.db.fault("archive_before_return")
            return ArchiveReceipt(binding=binding, source_id=chunk.source_id, source_epoch=chunk.source_epoch,
                                  recovery_epoch=installation["recovery_epoch"], host_archived_offset=watermark)

    def read_chunk(self, context: Invocation, source_id: str, source_epoch: str, offset: int) -> Chunk:
        assert_external_io_allowed()
        context = _context(context)
        coordinate(source_id, source_epoch, offset)
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                installation = _installation(tx, context)
                source = _required(tx, "archive_sources", "source_id=?", (source_id,), "Archive source is not registered")
                _required(tx, "archive_epochs", "source_id=? AND source_epoch=?", (source_id, source_epoch),
                          "Archive epoch is not registered")
                row = _required(tx, "archive_chunks", "source_id=? AND source_epoch=? AND offset=?",
                                (source_id, source_epoch, offset), "Archive chunk start is not committed")
                agent = _required(tx, "agents", "id=?", (source["agent_id"],), "Archive agent is not registered")
            chunk = Chunk(source_id, source_epoch, offset, row["length"], row["sha256"], b"")
            try:
                chunk_coordinates(chunk)
            except (CoreError, ValueError, TypeError):
                raise CoreError("uncertain_effect", "Stored archive object coordinates are invalid") from None
            if row["object_path"] != _path(agent["id"], chunk):
                raise CoreError("uncertain_effect", "Archive object inventory disagrees with its coordinates")
            with self._object_directory(agent, installation, chunk) as (fd, guard):
                payload = self._verify(fd, chunk)
                if payload is None:
                    raise CoreError("storage_blocked", "Committed archive object is missing; explicit repair is required")
                guard()
            return Chunk(source_id, source_epoch, offset, chunk.length, chunk.sha256, payload)


def _identity(info):
    return info.st_dev, info.st_ino


def _fingerprint(info):
    return (*_identity(info), info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
