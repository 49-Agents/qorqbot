"""Private durable snapshots of files an agent asked Ordubashy to send through Telegram.

``MediaPayloads(bridge)`` owns the byte lifecycle behind ``telegram_file_payloads`` (``media_schema``): a request is
reserved in a short bridge write, its bytes are copied, hashed and validated outside any transaction into
``<state root>/outbound-media``, sealed atomically, and later bound to exactly one queued send. Dispatch opens the sealed
snapshot, never the original source. Nothing here calls the network, reads a source path on its own, or regenerates bytes.

Construction is inert: it keeps the bridge and touches no file. The private directory (0700, files 0600, opaque names) is
created by the first ``stage``; ``open_payload``, ``recover``, ``sweep`` and ``status`` never create it. The bridge supplies the
database (``write``/``read`` units), the clock (``now``) and the validated private state root (``wall_state_dir``). The inbound
secret spool lives beside it and shares nothing with it.

Public surface (all plain values; no path, storage name or byte ever leaves this module):

``stage(principal_id=, request_key=, arguments_hash=, source_binding=, expected_sha256=, filename=, send_as=, opener=,
validate_snapshot=)``
    Outside any transaction. Reserves the durable request identity, quota and a copy lease, copies ``opener()`` (a zero-argument
    context-manager factory yielding a binary source) through a streaming SHA-256 with a limit+1 read, requires the hash to equal
    ``expected_sha256``, runs ``validate_snapshot(stream, send_as=, size=, filename=)`` on a read-only seekable view of the copy,
    seals it and returns ``inspect``-shaped metadata. One attempt deadline (``COPY_SECONDS``, shorter than the lease) covers the
    copy, the validation and the seal; a copy past its deadline, or whose lease expired or was lost, is never sealed. A repeated
    key with the same manifest returns the sealed original without reading the source again; a different manifest is
    ``idempotency_conflict``. Any failure after the reservation spends the key (``staging_failed`` on retry). A failure the
    running attempt itself observes removes its partial copy at once; an attempt that merely disappears (a crash) keeps its
    record, bytes and reservation until its 24 hour ``expires_at``, and only then, with its lease also expired, is it cleaned up.
``inspect(uow, payload_id)`` / ``find(uow, principal_id, request_key)`` / ``for_message(uow, message_id)`` / ``attach(uow, payload_id, message_id=, outbox_id=)``
    Short-transaction reads (a payload by id, by its durable request identity, or by the message it is attached to; ``None`` when
    there is none) and the one-way binding of a ready payload to its queued send (idempotent for the same pair).
``open_payload(payload_id)``
    Context manager yielding ``OpenPayload(stream, metadata)``: a verified, read-only, seekable binary stream at offset 0 and
    ``{"payload_id", "filename", "send_as", "size", "sha256", "validated"}``. Missing, unsafe or altered bytes raise
    ``payload_missing`` / ``payload_unsafe`` / ``payload_corrupt``; a deleted or unfinished payload raises ``payload_unavailable``.
    All are pre-send failures. Bytes are never reconstructed.
``recover()`` / ``sweep()`` / ``status()``
    Startup reconciliation, the periodic retention and cleanup pass, and a bounded counts-and-bytes report. Neither removes
    the bytes of a valid lease, of an attempt before its ``expires_at``, or of a queued, dispatching or uncertain delivery.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time
import unicodedata

from qorqut_operations.contracts import DomainError, canonical_bytes, plain
from . import store
from .media_schema import MAX_BYTES, OPERATION_NAME, TABLE
from .private_files import fsync_directory, require_private_directory

LOGGER = logging.getLogger(__name__)

DIRECTORY = "outbound-media"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STORAGE_NAME = re.compile(r"^[0-9a-f]{32}$")
FILE_NAME = re.compile(r"^([0-9a-f]{32})\.(part|media)$")
OPEN_DELIVERY = ("queued", "dispatching", "uncertain")
TERMINAL_DELIVERY = ("sent", "failed", "cancelled")
INTEGRITY_CODES = ("payload_missing", "payload_unsafe", "payload_corrupt")
MAX_REQUEST_KEY = 200
MAX_FILENAME_BYTES = 255
MAX_BINDING_BYTES = 8192
MAX_METADATA_BYTES = 4096


class _Stop(Exception):
    """A staging attempt ends here. ``public`` is the error the caller sees (defaults to a ``DomainError`` of the same code)."""

    def __init__(self, code, message, public=None):
        super().__init__(message)
        self.code, self.message, self.public = code[:64], message[:500], public


class _Directory(Exception):
    """The private media directory is absent (``missing``) or not a private real directory of this user (``unsafe``)."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class OpenPayload:
    """What ``open_payload`` yields. ``stream`` is a read-only seekable ``BufferedReader`` at offset 0 whose size and SHA-256
    were verified; it is closed when the context exits. ``metadata`` is plain: ``payload_id``, ``filename``, ``send_as``,
    ``size``, ``sha256`` and ``validated`` (exactly what ``validate_snapshot`` returned when the copy was sealed)."""

    stream: io.BufferedReader
    metadata: dict


def validate_filename(name):
    """The upload filename filter: a Unicode basename of 1..255 UTF-8 bytes, not ``.`` or ``..``, with no ``/``, ``\\`` or ASCII
    double quote and no Unicode Cc, Cs, Zl or Zp character. Every other character is kept exactly as given (no normalization),
    so a valid authorized name cannot fail here on a filter the transport and source layers do not share."""
    try:
        size = len(name.encode("utf-8")) if isinstance(name, str) else 0
    except UnicodeEncodeError:
        size = 0
    if not 1 <= size <= MAX_FILENAME_BYTES or name in (".", "..") or any(ch in name for ch in '/\\"'):
        raise DomainError("validation_error",
                          "The filename must be a basename of 1-255 UTF-8 bytes without a slash, backslash or double quote")
    if any(unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp") for ch in name):
        raise DomainError("validation_error", "The filename must not contain control or line-break characters")
    return name


class MediaPayloads:
    # Contract limits. Instance attributes of the same name are the test seams; production never changes them.
    TOTAL_BYTES = 1 << 30
    MAX_PENDING = 50
    MAX_COPIES = 2
    RETENTION_SECONDS = 24 * 3600
    LEASE_SECONDS = 300.0
    # A copy gives up before its lease can expire, so a lease still valid in the database means a copy that may truly be running.
    COPY_SECONDS = 240.0
    CHUNK_BYTES = 1 << 20
    CLAIM_BATCH = 25
    CLEANUP_BACKOFF = (30, 120, 600, 1800, 3600)

    def __init__(self, bridge, *, monotonic=time.monotonic):
        self.bridge = bridge
        self.monotonic = monotonic

    # ----- public: staging ---------------------------------------------------------------------------------

    def stage(self, *, principal_id, request_key, arguments_hash, source_binding, expected_sha256, filename, send_as, opener,
              validate_snapshot):
        request = self._request(principal_id, request_key, arguments_hash, source_binding, expected_sha256, filename, send_as)
        if not callable(opener) or not callable(validate_snapshot):
            raise DomainError("validation_error", "opener and validate_snapshot must be callable")
        try:
            directory = self._directory_fd(create=True)
        except _Directory as exc:
            raise DomainError("invalid_configuration", "The private media directory is unavailable") from exc
        try:
            self._reap()
            deadline = self.monotonic() + self.COPY_SECONDS  # started before the lease, so it always ends before the lease does
            kind, payload = self._reserve(request)
            if kind == "fail":
                raise payload
            if kind == "reuse":
                self._require_present(directory, payload)
                return self._view(payload)
            return self._copy(directory, payload, opener, validate_snapshot, deadline)
        finally:
            os.close(directory)

    def _request(self, principal_id, request_key, arguments_hash, source_binding, expected_sha256, filename, send_as):
        if not isinstance(principal_id, str) or not 1 <= len(principal_id) <= 100:
            raise DomainError("validation_error", "principal_id is required")
        if (not isinstance(request_key, str) or not request_key.strip() or len(request_key) > MAX_REQUEST_KEY
                or "\0" in request_key):
            raise DomainError("validation_error", "The request key must contain 1-200 characters")
        for name, value in (("arguments_hash", arguments_hash), ("expected_sha256", expected_sha256)):
            if not isinstance(value, str) or not SHA256.fullmatch(value):
                raise DomainError("validation_error", f"{name} must be 64 lowercase hexadecimal characters")
        if send_as not in MAX_BYTES:
            raise DomainError("validation_error", "send_as must be document or photo")
        validate_filename(filename)
        try:
            binding = plain(source_binding)
            encoded = canonical_bytes(binding)
        except (TypeError, ValueError):
            raise DomainError("validation_error", "source_binding must be plain JSON") from None
        if not isinstance(binding, dict) or len(encoded) > MAX_BINDING_BYTES:
            raise DomainError("validation_error", "source_binding must be a JSON object of at most 8192 bytes")
        return {"principal_id": principal_id, "request_key": request_key, "arguments_hash": arguments_hash,
                "source_binding_json": binding, "expected_sha256": expected_sha256, "filename": filename, "send_as": send_as,
                "max_bytes": MAX_BYTES[send_as], "canonical_binding": encoded}

    def _reserve(self, request):
        """One short write: the request identity, the quota decision and the lease, or the outcome for a repeated key."""
        now = self.bridge.now()
        with self.bridge.write("media.reserve") as uow:
            installation = uow.repository("installation").get("service")
            if not installation:
                raise DomainError("binding_invalid", "The installation record is missing")
            identity = (installation["installation_id"], request["principal_id"], OPERATION_NAME, request["request_key"])
            found = uow.connection.execute(
                f"SELECT * FROM {TABLE} WHERE installation_id=? AND principal_id=? AND operation_name=? AND request_key=?", identity).fetchone()
            if found is not None:
                return self._repeated(uow, store.decode(found), request, now)
            self._require_capacity(uow, request, now)
            row = uow.repository(TABLE).insert({
                "id": secrets.token_hex(16), "installation_id": identity[0], "principal_id": request["principal_id"],
                "operation_name": OPERATION_NAME, "request_key": request["request_key"], "arguments_hash": request["arguments_hash"],
                "source_binding_json": request["source_binding_json"], "expected_sha256": request["expected_sha256"],
                "filename": request["filename"], "send_as": request["send_as"], "storage_name": secrets.token_hex(16),
                "max_bytes": request["max_bytes"], "reserved_bytes": request["max_bytes"], "state": "staging",
                "lease_owner": secrets.token_hex(16), "lease_expires_at": store.plus(now, self.LEASE_SECONDS),
                "expires_at": store.plus(now, self.RETENTION_SECONDS), "created_at": now, "updated_at": now})
            return "copy", row

    def _repeated(self, uow, row, request, now):
        if (row["arguments_hash"], row["expected_sha256"], row["filename"], row["send_as"]) != (
                request["arguments_hash"], request["expected_sha256"], request["filename"], request["send_as"]) or (
                canonical_bytes(row["source_binding_json"]) != request["canonical_binding"]):
            raise DomainError("idempotency_conflict", "This idempotency key was already used for a different file request")
        if row["state"] in ("ready", "attached"):
            return "reuse", row
        if row["state"] == "staging":
            if row["lease_expires_at"] > now:
                return "fail", DomainError("busy", "This file is still being copied; retry with the same idempotency key", retryable=True)
            # Abandoned by a crash: spent, never copied again under this key. Its record and bytes stay until ``expires_at``.
            return "fail", self._spent({"failure_code": "interrupted"})
        if row["failure_code"]:
            return "fail", self._spent(row)
        return "fail", DomainError("payload_unavailable", "The snapshot for this idempotency key was already removed; use a new key")

    @staticmethod
    def _spent(row):
        return DomainError("staging_failed", f"This file request failed earlier ({row['failure_code']}); use a new idempotency key",
                           details={"failure_code": row["failure_code"]})

    def _require_capacity(self, uow, request, now):
        c = uow.connection
        copies = c.execute(f"SELECT count(*) FROM {TABLE} WHERE state='staging' AND lease_expires_at > ?", (now,)).fetchone()[0]
        if copies >= self.MAX_COPIES:
            raise DomainError("capacity_exceeded", "Two files are already being copied; retry shortly", retryable=True,
                              details={"limit": "concurrent_copies"})
        pending = c.execute(
            f"SELECT count(*) FROM {TABLE} p WHERE p.state IN ('staging','ready') OR (p.state='attached' AND EXISTS ("
            f"SELECT 1 FROM telegram_outbox o WHERE o.id=p.outbox_id AND o.state IN ({','.join('?' * len(OPEN_DELIVERY))})))",
            OPEN_DELIVERY).fetchone()[0]
        if pending >= self.MAX_PENDING:
            raise DomainError("capacity_exceeded", "Too many files are waiting to be delivered", retryable=True,
                              details={"limit": "pending_deliveries"})
        reserved = c.execute(f"SELECT COALESCE(SUM(reserved_bytes), 0) FROM {TABLE} WHERE state != 'deleted'").fetchone()[0]
        if reserved + request["max_bytes"] > self.TOTAL_BYTES:
            raise DomainError("capacity_exceeded", "The outbound file storage is full", retryable=True,
                              details={"limit": "stored_bytes"})

    # ----- staging: copy, validate, seal ------------------------------------------------------------------------------

    def _copy(self, directory, row, opener, validate_snapshot, deadline):
        lease, name = row["lease_owner"], row["storage_name"]
        part, final = f"{name}.part", f"{name}.media"
        try:
            try:
                total, digest = self._copy_source(directory, part, row, opener, deadline)
                self._check_deadline(deadline)
                metadata = self._validate(directory, part, row, total, validate_snapshot)
                self._check_deadline(deadline)
                os.rename(part, final, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            except _Stop:
                raise
            except OSError as exc:
                raise self._storage_error(exc) from exc
            return self._seal(row, lease, total, digest, metadata, deadline)
        except _Stop as stop:
            self._abandon(row, lease, stop)
            raise (stop.public or DomainError(stop.code, stop.message)) from None
        except BaseException:
            self._abandon(row, lease, _Stop("interrupted", "The copy was interrupted"))
            raise

    def _check_deadline(self, deadline):
        if self.monotonic() > deadline:
            raise _Stop("timeout", "Staging the file took too long; the request is spent, use a new idempotency key")

    def _copy_source(self, directory, part, row, opener, deadline):
        limit, digest, total = row["max_bytes"], hashlib.sha256(), 0
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        except OSError as exc:
            raise self._storage_error(exc) from exc
        try:
            try:
                with opener() as source:
                    before = self._fingerprint(source)
                    while True:
                        chunk = source.read(min(self.CHUNK_BYTES, limit + 1 - total))
                        if not chunk:
                            if chunk is None:
                                raise _Stop("source_unreadable", "The file could not be read")
                            break
                        total += len(chunk)
                        if total > limit:
                            raise _Stop("source_too_large", f"The file is larger than the {limit} byte limit for {row['send_as']}")
                        digest.update(chunk)
                        try:
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(fd, view):]
                        except OSError as exc:
                            raise self._storage_error(exc) from exc
                        self._check_deadline(deadline)
                    after = self._fingerprint(source)
            except _Stop:
                raise
            except DomainError as exc:
                raise _Stop("source_unreadable", exc.message or "The file could not be read", public=exc) from None
            except Exception:  # noqa: BLE001 - any failure of the authorized reader is a pre-send staging failure
                raise _Stop("source_unreadable", "The file could not be read") from None
            if before != after:
                raise _Stop("source_changed", "The file changed while it was being copied")
            if total == 0:
                raise _Stop("source_empty", "The file is empty")
            if digest.hexdigest() != row["expected_sha256"]:
                raise _Stop("hash_mismatch", "The file does not match expected_sha256")
            try:
                os.fsync(fd)
            except OSError as exc:
                raise self._storage_error(exc) from exc
        finally:
            os.close(fd)
        self._check_deadline(deadline)
        return total, digest.hexdigest()

    @staticmethod
    def _storage_error(exc):
        return _Stop("storage_error", f"The snapshot could not be stored ({errno.errorcode.get(exc.errno, 'OSError')})")

    @staticmethod
    def _fingerprint(source):
        try:
            info = os.fstat(source.fileno())
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
            return None
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def _validate(self, directory, part, row, total, validate_snapshot):
        fd = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            try:
                metadata = validate_snapshot(stream, send_as=row["send_as"], size=total, filename=row["filename"])
            except DomainError as exc:
                raise _Stop(exc.code, exc.message or "The file failed validation", public=exc) from None
            except Exception:  # noqa: BLE001 - a callback error is a pre-send staging failure
                raise _Stop("validation_failed", "The file failed validation") from None
        info = os.stat(part, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size != total or info.st_nlink != 1:
            raise _Stop("source_changed", "The snapshot changed while it was being validated")
        try:
            metadata = plain(metadata)
            encoded = canonical_bytes(metadata)
        except (TypeError, ValueError):
            raise _Stop("invalid_metadata", "The validation result is not plain JSON") from None
        if not isinstance(metadata, dict) or len(encoded) > MAX_METADATA_BYTES:
            raise _Stop("invalid_metadata", "The validation result must be a JSON object of at most 4096 bytes")
        return metadata

    def _seal(self, row, lease, total, digest, metadata, deadline):
        """Publish the ready row in one short write (no file I/O): only the attempt that still owns a still-valid lease, and only
        before its deadline. A commit whose outcome is unknown is resolved by reading the row back, so a sealed row is kept."""
        try:
            with self.bridge.write("media.seal") as uow:
                now = self.bridge.now()
                self._check_deadline(deadline)
                current = uow.repository(TABLE).get(row["id"])
                if current is None or current["state"] != "staging" or current["lease_owner"] != lease:
                    raise _Stop("lost_lease", "The file copy lost its lease before it was sealed; the request is spent, "
                                              "use a new idempotency key")
                if current["lease_expires_at"] <= now:
                    raise _Stop("lost_lease", "The file copy outlived its lease and was not sealed; the request is spent, "
                                              "use a new idempotency key")
                sealed = self._update(uow, current, state="ready", size_bytes=total, sha256=digest, metadata_json=metadata,
                                      sealed_at=now, reserved_bytes=total, lease_owner=None, lease_expires_at=None)
        except _Stop:
            raise
        except Exception:
            committed = self._committed_seal(row, digest)
            if committed is None:
                raise
            return committed
        return self._view(sealed)

    def _committed_seal(self, row, digest):
        """The sealed view when an unreported failure nevertheless left this attempt's row ready, else ``None``."""
        try:
            with self.bridge.read() as uow:
                current = uow.repository(TABLE).get(row["id"])
        except Exception:  # noqa: BLE001
            return None
        if current is not None and current["state"] in ("ready", "attached") and current["sha256"] == digest:
            return self._view(current)
        return None

    # ----- failure and cleanup -----------------------------------------------------------------------------

    def _abandon(self, row, lease, stop):
        """Record the failure (when this copy still owns its lease) and remove its files. Best effort: the lease expires and
        ``recover`` finishes whatever this could not. A snapshot this copy already sealed is never removed here."""
        try:
            with self.bridge.write("media.abandon") as uow:
                current = uow.repository(TABLE).get(row["id"])
                if current["state"] == "staging" and current["lease_owner"] == lease:
                    current = self._fail_row(uow, current, stop.code, stop.message)
            if current["state"] == "deleting":
                self._finish(row["id"])
            elif current["state"] == "deleted":
                self._unlink(row["storage_name"])
        except Exception:  # noqa: BLE001
            LOGGER.warning("A failed Telegram file copy could not be cleaned up yet; recovery will retry")

    def _fail_row(self, uow, row, code, message):
        return self._update(uow, row, state="deleting", failure_code=code, failure_message=message, cleanup_reason="failed",
                            lease_owner=None, lease_expires_at=None, next_cleanup_at=None)

    def _reap(self):
        """Before a new reservation: claim abandoned attempts that are past their retention and finish due deletions, so their
        quota is freed on time. A lease that merely expired frees the copy slot by itself and nothing else."""
        now = self.bridge.now()
        try:
            with self.bridge.read() as uow:
                due = uow.connection.execute(
                    f"SELECT 1 FROM {TABLE} WHERE (state='staging' AND expires_at <= ? AND lease_expires_at <= ?) OR "
                    "(state='deleting' AND (next_cleanup_at IS NULL OR next_cleanup_at <= ?)) LIMIT 1", (now, now, now)).fetchone()
            if due:
                self._claim_abandoned(now)
                self._finish_due(now)
        except Exception:  # noqa: BLE001 - cleanup trouble must not block a new request
            LOGGER.warning("Telegram file payload reaping failed; the request continues")

    def _claim_abandoned(self, now):
        """Claim staging attempts that nobody ended: eligible only at or after ``expires_at`` AND with the lease expired. The
        spent key gets the stable failure ``interrupted``; the files are removed afterwards, outside this write."""
        with self.bridge.write("media.expire") as uow:
            stale = uow.connection.execute(
                f"SELECT id FROM {TABLE} WHERE state='staging' AND expires_at <= ? AND lease_expires_at <= ? ORDER BY expires_at, id",
                (now, now)).fetchall()
            for item in stale:
                self._fail_row(uow, uow.repository(TABLE).get(item["id"]), "interrupted", "The earlier copy was interrupted")
        return len(stale)

    def _finish_due(self, now, *, force=False):
        with self.bridge.read() as uow:
            query = f"SELECT id FROM {TABLE} WHERE state='deleting'"
            ids = [r["id"] for r in uow.connection.execute(
                query + ("" if force else " AND (next_cleanup_at IS NULL OR next_cleanup_at <= ?)") + " ORDER BY created_at LIMIT 200",
                () if force else (now,))]
        return sum(1 for payload_id in ids if self._finish(payload_id))

    def _finish(self, payload_id):
        """Remove a claimed payload's files (outside any transaction), then record the result. Returns whether it is deleted."""
        with self.bridge.read() as uow:
            row = uow.repository(TABLE).get(payload_id)
        if row is None or row["state"] != "deleting":
            return row is not None and row["state"] == "deleted"
        try:
            self._unlink(row["storage_name"])
        except (OSError, _Directory) as exc:
            detail = "unsafe_directory" if isinstance(exc, _Directory) else errno.errorcode.get(exc.errno, "OSError")
            self._record_cleanup_error(row, f"The snapshot could not be removed ({detail})")
            return False
        now = self.bridge.now()
        try:
            with self.bridge.write("media.deleted") as uow:
                current = uow.repository(TABLE).get(payload_id)
                if current["state"] == "deleting":
                    self._update(uow, current, state="deleted", reserved_bytes=0, deleted_at=now, next_cleanup_at=None,
                                 error_code=None, error_message=None, error_at=None)
        except DomainError as exc:
            if exc.code != "revision_conflict":
                raise
        return True

    def _unlink(self, storage_name):
        try:
            directory = self._directory_fd()
        except _Directory as exc:
            if exc.kind == "missing":
                return
            raise
        try:
            for suffix in (".part", ".media"):
                try:
                    os.unlink(f"{storage_name}{suffix}", dir_fd=directory)
                except FileNotFoundError:
                    pass
            os.fsync(directory)
        finally:
            os.close(directory)

    def _record_cleanup_error(self, row, message):
        now = self.bridge.now()
        delay = self.CLEANUP_BACKOFF[min(row["cleanup_attempts"], len(self.CLEANUP_BACKOFF) - 1)]
        try:
            with self.bridge.write("media.cleanup_error") as uow:
                current = uow.repository(TABLE).get(row["id"])
                if current["state"] == "deleting":
                    self._update(uow, current, cleanup_attempts=current["cleanup_attempts"] + 1, error_code="cleanup_failed",
                                 error_message=message, error_at=now, next_cleanup_at=store.plus(now, delay))
        except DomainError as exc:
            if exc.code != "revision_conflict":
                raise

    # ----- public: apply (short transactions) ----------------------------------------------------------------

    def inspect(self, uow, payload_id):
        row = uow.repository(TABLE).get(payload_id)
        if row is None:
            raise DomainError("not_found", "The file payload does not exist")
        return self._view(row)

    def find(self, uow, principal_id, request_key):
        """The payload of one durable request identity (this installation, ``principal_id``, the operation and ``request_key``),
        or ``None``. A read that opens no file and changes nothing, so a caller can learn what a repeated key already froze
        before it derives anything new."""
        installation = uow.repository("installation").get("service")
        if not installation:
            return None
        found = uow.connection.execute(
            f"SELECT * FROM {TABLE} WHERE installation_id=? AND principal_id=? AND operation_name=? AND request_key=?",
            (installation["installation_id"], principal_id, OPERATION_NAME, request_key)).fetchone()
        return self._view(store.decode(found)) if found is not None else None

    def for_message(self, uow, message_id):
        """The payload attached to one outbound message, or ``None``."""
        found = uow.connection.execute(f"SELECT * FROM {TABLE} WHERE message_id=?", (message_id,)).fetchone()
        return self._view(store.decode(found)) if found is not None else None

    def attach(self, uow, payload_id, *, message_id, outbox_id):
        """Bind a ready payload to its one queued ``agent_file`` send in the caller's write unit. Repeating the same pair is a
        no-op; any other association, state or mismatch with the outbox row or the message is refused."""
        uow.require_write()
        row = uow.repository(TABLE).get(payload_id)
        if row is None:
            raise DomainError("not_found", "The file payload does not exist")
        if row["state"] == "attached":
            if (row["message_id"], row["outbox_id"]) == (message_id, outbox_id):
                return self._view(row)
            raise DomainError("payload_attached", "The file payload is already attached to another delivery")
        if row["state"] != "ready":
            raise DomainError("payload_unavailable", f"The file payload is {row['state']} and cannot be attached")
        outbox, message = store.outbox(uow, outbox_id), store.message(uow, message_id)
        if (outbox is None or message is None or outbox["message_id"] != message_id or outbox["source_kind"] != "agent_file"
                or outbox["kind"] != row["send_as"] or outbox["agent_id"] != row["principal_id"]
                or message["direction"] != "outbound" or message["agent_id"] != row["principal_id"]
                or message["outbox_id"] not in (None, outbox_id) or not self._envelope(outbox["payload_json"], payload_id)):
            raise DomainError("payload_mismatch", "The delivery does not match the file payload")
        if outbox["state"] != "queued":
            raise DomainError("payload_mismatch", "A file payload attaches only to a queued delivery")
        return self._view(self._update(uow, row, state="attached", message_id=message_id, outbox_id=outbox_id,
                                       attached_at=self.bridge.now()))

    @staticmethod
    def _envelope(value, payload_id):
        """Exactly ``{"version": 1, "payload_id": <this id>}``: two keys, an integer (not true, not 1.0) and the id as text."""
        return (isinstance(value, dict) and set(value) == {"version", "payload_id"} and type(value["version"]) is int
                and value["version"] == 1 and type(value["payload_id"]) is str and value["payload_id"] == payload_id)

    # ----- public: dispatch -------------------------------------------------------------------------------

    @contextmanager
    def open_payload(self, payload_id):
        with self.bridge.read() as uow:
            row = uow.repository(TABLE).get(payload_id)
        if row is None:
            raise DomainError("payload_unavailable", "The file payload does not exist")
        if row["state"] not in ("ready", "attached"):
            raise DomainError("payload_unavailable", f"The file payload is {row['state']}")
        stream = self._open_verified(row)
        try:
            yield OpenPayload(stream, {"payload_id": row["id"], "filename": row["filename"], "send_as": row["send_as"],
                                       "size": row["size_bytes"], "sha256": row["sha256"], "validated": row["metadata_json"]})
        finally:
            stream.close()

    def _open_verified(self, row):
        try:
            directory = self._directory_fd()
        except _Directory as exc:
            raise self._integrity("payload_missing" if exc.kind == "missing" else "payload_unsafe") from None
        try:
            try:
                fd = os.open(f"{row['storage_name']}.media", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                raise self._integrity("payload_missing") from None
            except OSError:
                raise self._integrity("payload_unsafe") from None
        finally:
            os.close(directory)
        stream = None
        try:
            info = os.fstat(fd)
            problem = self._unsafe(info) or ("payload_corrupt" if info.st_size != row["size_bytes"] else None)
            if problem:
                raise self._integrity(problem)
            stream = os.fdopen(fd, "rb")
        except BaseException:
            if stream is None:
                os.close(fd)
            raise
        try:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(self.CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            if digest.hexdigest() != row["sha256"] or (after.st_size, after.st_mtime_ns, after.st_ino) != (
                    info.st_size, info.st_mtime_ns, info.st_ino):
                raise self._integrity("payload_corrupt")
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
        return stream

    @staticmethod
    def _unsafe(info):
        """The private-file invariants: a regular, singly linked, 0600 file owned by this user."""
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return "payload_unsafe"
        return None

    @staticmethod
    def _integrity(code):
        message = {"payload_missing": "The sealed snapshot is missing; it is never recreated from the source",
                   "payload_unsafe": "The sealed snapshot is not a private regular file of this service",
                   "payload_corrupt": "The sealed snapshot no longer matches its recorded size and hash"}[code]
        return DomainError(code, message)

    def _require_present(self, directory, row):
        """A cheap early refusal for a reused snapshot whose file is already known to be missing or unsafe (no hashing here)."""
        try:
            info = os.stat(f"{row['storage_name']}.media", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            raise self._integrity("payload_missing") from None
        except OSError:
            raise self._integrity("payload_unsafe") from None
        problem = self._unsafe(info) or ("payload_corrupt" if info.st_size != row["size_bytes"] else None)
        if problem:
            raise self._integrity(problem)

    # ----- public: recovery, retention, status ------------------------------------------------------------------

    def recover(self):
        """Startup reconciliation: claim abandoned attempts that are past their 24 hour retention with an expired lease, resume
        claimed deletions, remove files no live row owns and record which retained snapshots are missing or unsafe. Never
        creates the directory; never touches a valid lease, an abandoned attempt before its ``expires_at``, or an open delivery."""
        now = self.bridge.now()
        result = {"stale_staging": self._claim_abandoned(now), "deleted": self._finish_due(now, force=True)}
        result["orphans_removed"] = self._remove_orphans()
        result["flagged"] = self._flag_integrity(now)
        return result

    def sweep(self):
        """Retention and cleanup: claim what is eligible in one short write, delete outside it, record the results. A
        queued, dispatching or uncertain delivery's payload is never claimed. Returns the counts."""
        now = self.bridge.now()
        cutoff = store.plus(now, -self.RETENTION_SECONDS)
        result = {"stale_staging": self._claim_abandoned(now)}
        with self.bridge.write("media.claim") as uow:
            expired = [r["id"] for r in uow.connection.execute(
                f"SELECT id FROM {TABLE} WHERE state='ready' AND expires_at <= ? ORDER BY expires_at, id LIMIT ?", (now, self.CLAIM_BATCH))]
            delivered = [r["id"] for r in uow.connection.execute(
                f"SELECT p.id FROM {TABLE} p JOIN telegram_outbox o ON o.id=p.outbox_id WHERE p.state='attached' "
                f"AND o.state IN ({','.join('?' * len(TERMINAL_DELIVERY))}) AND "
                "(CASE WHEN o.state='sent' THEN COALESCE(o.sent_at, o.updated_at) ELSE o.updated_at END) <= ? "
                "ORDER BY o.updated_at, p.id LIMIT ?", (*TERMINAL_DELIVERY, cutoff, self.CLAIM_BATCH))]
            for reason, ids in (("expired", expired), ("delivered", delivered)):
                for payload_id in ids:
                    self._update(uow, uow.repository(TABLE).get(payload_id), state="deleting", cleanup_reason=reason)
        result.update(expired=len(expired), delivered=len(delivered))
        result["deleted"] = self._finish_due(now)
        return result

    def _remove_orphans(self):
        try:
            directory = self._directory_fd()
        except _Directory:
            return 0
        removed = 0
        try:
            names = [name for name in os.listdir(directory) if FILE_NAME.fullmatch(name)]
            with self.bridge.read() as uow:
                live = {r["storage_name"]: r["state"] for r in uow.connection.execute(
                    f"SELECT storage_name, state FROM {TABLE} WHERE state != 'deleted'")}
            for name in names:
                base, suffix = FILE_NAME.fullmatch(name).groups()
                state = live.get(base)
                if state == "staging" or (state in ("ready", "attached", "deleting") and suffix == "media"):
                    continue
                try:
                    os.unlink(name, dir_fd=directory)
                    removed += 1
                except FileNotFoundError:
                    pass
            if removed:
                os.fsync(directory)
        finally:
            os.close(directory)
        return removed

    def _flag_integrity(self, now):
        """Persist which ready or attached snapshots are missing or unsafe (size only; hashing is left to dispatch)."""
        try:
            directory = self._directory_fd()
        except _Directory as exc:
            directory, absent = None, exc.kind
        flagged = 0
        try:
            with self.bridge.read() as uow:
                rows = [dict(r) for r in uow.connection.execute(
                    f"SELECT id, storage_name, size_bytes, error_code FROM {TABLE} WHERE state IN ('ready','attached')")]
            for row in rows:
                if directory is None:
                    code = "payload_missing" if absent == "missing" else "payload_unsafe"
                else:
                    try:
                        info = os.stat(f"{row['storage_name']}.media", dir_fd=directory, follow_symlinks=False)
                        code = self._unsafe(info) or ("payload_corrupt" if info.st_size != row["size_bytes"] else None)
                    except FileNotFoundError:
                        code = "payload_missing"
                    except OSError:
                        code = "payload_unsafe"
                if code is not None:
                    flagged += 1
                if code != row["error_code"] and (code is not None or row["error_code"] in INTEGRITY_CODES):
                    with self.bridge.write("media.flag") as uow:
                        current = uow.repository(TABLE).get(row["id"])
                        if current["state"] in ("ready", "attached"):
                            self._update(uow, current, error_code=code, error_at=now if code else None,
                                         error_message=self._integrity(code).message if code else None)
        finally:
            if directory is not None:
                os.close(directory)
        return flagged

    def status(self, uow=None):
        """Bounded owner status: counts, bytes and one short code. No identifier, name, path or message text."""
        if uow is None:
            with self.bridge.read() as unit:
                return self.status(unit)
        c = uow.connection
        states = {r["state"]: r["n"] for r in c.execute(f"SELECT state, count(*) n FROM {TABLE} GROUP BY state")}
        pending = c.execute(
            f"SELECT count(*) FROM {TABLE} p WHERE p.state IN ('staging','ready') OR (p.state='attached' AND EXISTS ("
            f"SELECT 1 FROM telegram_outbox o WHERE o.id=p.outbox_id AND o.state IN ({','.join('?' * len(OPEN_DELIVERY))})))",
            OPEN_DELIVERY).fetchone()[0]
        last = c.execute(f"SELECT error_code FROM {TABLE} WHERE error_code IS NOT NULL ORDER BY error_at DESC LIMIT 1").fetchone()
        return {
            "payloads": {state: states.get(state, 0) for state in ("staging", "ready", "attached", "deleting", "deleted")},
            "reserved_bytes": c.execute(f"SELECT COALESCE(SUM(reserved_bytes), 0) FROM {TABLE} WHERE state != 'deleted'").fetchone()[0],
            "limit_bytes": self.TOTAL_BYTES,
            "pending_deliveries": pending, "limit_pending": self.MAX_PENDING,
            "active_copies": c.execute(f"SELECT count(*) FROM {TABLE} WHERE state='staging' AND lease_expires_at > ?",
                                       (self.bridge.now(),)).fetchone()[0],
            "limit_copies": self.MAX_COPIES,
            "cleanup_errors": c.execute(f"SELECT count(*) FROM {TABLE} WHERE state='deleting' AND error_code IS NOT NULL").fetchone()[0],
            "cleanup_attempts": c.execute(f"SELECT COALESCE(SUM(cleanup_attempts), 0) FROM {TABLE} WHERE state='deleting'").fetchone()[0],
            "unavailable_snapshots": c.execute(
                f"SELECT count(*) FROM {TABLE} WHERE state IN ('ready','attached') AND error_code IS NOT NULL").fetchone()[0],
            "last_error_code": last["error_code"] if last else None,
        }

    # ----- shared helpers -------------------------------------------------------------------------------------

    @staticmethod
    def _update(uow, row, **changes):
        return uow.repository(TABLE).update(row["id"], changes, expected_revision=row["revision"])

    @staticmethod
    def _view(row):
        return {"payload_id": row["id"], "state": row["state"], "installation_id": row["installation_id"],
                "principal_id": row["principal_id"], "request_key": row["request_key"], "arguments_hash": row["arguments_hash"],
                "source_binding": row["source_binding_json"], "expected_sha256": row["expected_sha256"], "filename": row["filename"],
                "send_as": row["send_as"], "size_bytes": row["size_bytes"], "sha256": row["sha256"], "validated": row["metadata_json"],
                "created_at": row["created_at"], "sealed_at": row["sealed_at"], "expires_at": row["expires_at"],
                "message_id": row["message_id"], "outbox_id": row["outbox_id"], "attached_at": row["attached_at"],
                "failure_code": row["failure_code"], "error_code": row["error_code"], "revision": row["revision"]}

    def _state_root(self):
        return Path(self.bridge.wall_state_dir())

    def _directory_fd(self, *, create=False):
        """An open descriptor of the private ``outbound-media`` directory, validated; every file operation goes through it
        (``dir_fd``) so a swapped path cannot redirect it. ``create`` is only for ``stage``."""
        state = self._state_root()
        try:
            if create:
                require_private_directory(state, create=True)
                from .bridge import require_unversioned
                runtime = getattr(self.bridge, "runtime", None)
                require_unversioned(state, getattr(runtime, "root", None), getattr(runtime, "workspace", None))
            elif not os.path.lexists(state):
                raise _Directory("missing")
            else:
                require_private_directory(state)
        except DomainError:
            raise _Directory("unsafe") from None
        root = state / DIRECTORY
        if not os.path.lexists(root):
            if not create:
                raise _Directory("missing")
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                pass  # a concurrent first stage created it; the checks below judge what is there
            else:
                os.chmod(root, 0o700)
                fsync_directory(state)
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise _Directory("unsafe") from None
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(fd)
            raise _Directory("unsafe")
        return fd


__all__ = ["DIRECTORY", "MediaPayloads", "OpenPayload", "validate_filename"]
