"""The provider-aware, append-only raw transcript archive.

Layout of one lineage (one agent + provider + native conversation, independent of process
generation), below the explicit archive root::

    <root>/<provider>/<encoded conversation id>/
        lineage.json        immutable header: agent, provider, original conversation ID
        payload.raw         the single append-only raw payload (exact provider bytes)
        index.bin           append-only 32-byte entries for complete records that are not JSON objects
        receipts/NNN.rcpt   immutable fixed-size receipt records (hash chained)
        latest.json         atomically replaced pointer to the newest committed receipt
        staging/            transient: the *current* new suffix and unpublished receipt images
        .lock               process-safe capture lock

``docs/v1.2/NATIVE_ARCHIVE_CHECKPOINT.md`` states the write order and the recovery algorithm;
the comments below mark where each rule lives.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import os
from typing import Callable

from qorqut import file_identity

from . import _files, _format as fmt, _providers, _relocation
from ._types import (
    CaptureBinding, CaptureError, CaptureReceipt, FinalCaptureEvidence, IndexEntry, MAX_ENCODED_CONVERSATION,
    RawRange, RawRecord, REASON_PARTIAL_TAIL, REASON_UNSTABLE, absolute_path, encode_component,
)

CHUNK = 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024
DEFAULT_LOCK_TIMEOUT = 10.0

HEADER, PAYLOAD, INDEX, POINTER, LOCK = "lineage.json", "payload.raw", "index.bin", "latest.json", ".lock"
RECEIPTS, STAGING = "receipts", "staging"
NO_FILE = -1


def _fail(code: str, message: str, *, retryable: bool = False, latest: CaptureReceipt | None = None) -> CaptureError:
    return CaptureError(code, message, retryable=retryable, latest=latest)


def _common_prefix(left: bytes, right: bytes) -> int:
    low, high = 0, min(len(left), len(right))
    while low < high:
        middle = (low + high + 1) // 2
        if left[:middle] == right[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


class _Lineage:
    """Open directory descriptors of one lineage; closed together."""

    def __init__(self, fd: int, archive_id: str):
        self.fd, self.archive_id = fd, archive_id
        self._children: dict[str, int] = {}

    def child(self, name: str, *, create: bool = False) -> int | None:
        if name in self._children:
            return self._children[name]
        try:
            fd = _files.open_child_directory(self.fd, name, create=create)
        except FileNotFoundError:
            return None
        self._children[name] = fd
        return fd

    def close(self) -> None:
        for fd in self._children.values():
            os.close(fd)
        self._children.clear()
        os.close(self.fd)


class _State:
    """What a capture found under the lock, after validating the receipt chain."""

    def __init__(self, header, pointer, ahead, chain, payload_size, index_size):
        self.header, self.pointer, self.ahead, self.chain = header, pointer, ahead, chain
        self.payload_size, self.index_size = payload_size, index_size

    @property
    def candidate(self) -> CaptureReceipt | None:
        return self.ahead[-1] if self.ahead else self.pointer

    @property
    def committed(self) -> int:
        return self.candidate.length if self.candidate else 0


class _Staged:
    """The current new suffix, copied from the source into a private staging file."""

    def __init__(self, directory: int, name: str, fd: int):
        self.directory, self.name, self.fd = directory, name, fd

    def discard(self) -> None:
        if self.fd != NO_FILE:
            os.close(self.fd)
            self.fd = NO_FILE
        _files.unlink_quietly(self.directory, self.name)


class _Scan:
    """Frames, validates and counts bytes from the last committed record boundary to the new end."""

    def __init__(self, binding: CaptureBinding, base: CaptureReceipt | None):
        self.evidence = _providers.IdentityEvidence(
            binding.provider, binding.conversation_id, binding.workspace, bound=base is not None)
        self.start = base.record_boundary if base else 0
        self.scanner = _providers.FrameScanner(self.start)
        self.records = base.records if base else 0
        self.malformed = base.malformed if base else 0
        self.entries = bytearray()

    def feed(self, data: bytes) -> None:
        for frame in self.scanner.feed(data):
            if frame.kind == "json":
                try:
                    self.evidence.check(frame.value)
                except _providers.IdentityError as error:
                    raise _fail("session_mismatch", str(error)) from error
                self.records += 1
            elif frame.kind in {"malformed", "oversized"}:
                self.malformed += 1
                self.entries.extend(_providers.pack_entry(frame))


class RecordIterator:
    """Iterator over a receipt's records that owns its descriptors; use it as a context manager or close() it."""

    def __init__(self, stack: ExitStack, payload_fd: int, receipt: CaptureReceipt, offset: int):
        self._stack, self._fd, self._receipt = stack, payload_fd, receipt
        self._scanner = _providers.FrameScanner(offset)
        self._position = offset
        self._pending: list[RawRecord] = []
        self._finished = False

    def __iter__(self) -> "RecordIterator":
        return self

    def __enter__(self) -> "RecordIterator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def close(self) -> None:
        """Stop early: release the descriptors and drop any record not yet returned."""
        self._pending = []
        self._release()

    def _release(self) -> None:
        self._finished = True
        self._stack.close()

    def __next__(self) -> RawRecord:
        while not self._pending:
            if self._finished:
                raise StopIteration
            length = self._receipt.length
            if self._position >= length:
                start, size = self._scanner.pending()
                if size:
                    self._pending.append(RawRecord(start, size, "partial"))
                self._release()
                continue
            data = _files.pread_full(self._fd, min(CHUNK, length - self._position), self._position)
            if not data:
                self._release()
                raise _fail("archive_integrity", "The payload ended inside a committed receipt")
            self._position += len(data)
            self._pending = [RawRecord(frame.offset, frame.length, frame.kind, frame.value)
                             for frame in reversed(self._scanner.feed(data))]
        return self._pending.pop()


class TranscriptArchive:
    """A lossless, append-only archive of provider transcript bytes below one explicit root.

    Construction validates the root string and creates nothing. ``capture`` is the only
    operation that writes; it creates the archive directories it needs (the root's *parent* must
    already exist) and publishes receipts. The caller chooses the root from the registered
    agent's transcripts directory; nothing here discovers agents, businesses, provider homes or
    processes.
    """

    def _crash_point(self, name: str) -> None:
        """Seam for crash-gap tests: called after each durable step of the write path.

        A test subclass raises from it to leave the exact on-disk state a crashed writer would
        leave. Names: suffix_read, staged, payload_chunk, payload_appended, index_appended,
        receipt_temp, receipt_published, pointer_temp, pointer_published.
        """

    _COPY_CHUNK = CHUNK

    def __init__(self, archive_root, *, clock: Callable[[], datetime] | None = None,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT):
        self._root = absolute_path(archive_root, "archive_root")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock_timeout = lock_timeout

    @property
    def archive_root(self) -> str:
        return self._root

    # ----------------------------------------------------------------- public operations

    def capture(self, binding: CaptureBinding, *, final_evidence: FinalCaptureEvidence | None = None) -> CaptureReceipt:
        """Archive the verified source's new bytes and return the receipt that covers them.

        Returns the existing latest receipt, with no new storage, when the bytes, binding and
        completeness evidence are unchanged. Raises :class:`CaptureError` (the prior trusted
        receipt stays readable and is carried as ``error.latest``) for a wrong binding, a
        replaced/shrunk/rewritten source, a corrupt archive or an unresolved interrupted write.
        """
        self._require_binding(binding)
        if final_evidence is not None:
            if not isinstance(final_evidence, FinalCaptureEvidence):
                raise TypeError("final_evidence must be FinalCaptureEvidence")
            if (final_evidence.agent_id, final_evidence.provider, final_evidence.conversation_id,
                    final_evidence.generation) != (binding.agent_id, binding.provider, binding.conversation_id,
                                                   binding.generation):
                raise _fail("final_evidence_mismatch", "Final evidence names another agent, conversation or generation")
        self._check_fits(binding, final_evidence)
        with self._guarded():
            with self._open(binding.provider, encode_component(binding.conversation_id), create=True) as lineage:
                try:
                    with _files.exclusive_lock(lineage.fd, LOCK, self._lock_timeout):
                        return self._capture_locked(lineage, binding, final_evidence)
                except _files.LockTimeout:
                    raise _fail("archive_busy", "Another capture holds this archive", retryable=True) from None

    def relocate_source(self, binding: CaptureBinding, *, expected: CaptureReceipt) -> CaptureReceipt:
        """Operator-authorized instance migration, never an automatic response to a changed source.

        Verify the entire archived prefix in the new file, preserve all existing receipts,
        and append a receipt under an immutable relocation record. The caller must provide
        the exact inspected receipt and a later verified native session binding.
        """
        self._require_binding(binding)
        if not isinstance(expected, CaptureReceipt):
            raise TypeError("expected must be a CaptureReceipt")
        self._check_fits(binding, None)
        with self._guarded():
            with self._open(binding.provider, encode_component(binding.conversation_id), create=False) as lineage:
                try:
                    with _files.exclusive_lock(lineage.fd, LOCK, self._lock_timeout):
                        _relocation.authorize(self, lineage, binding, expected)
                        return self._capture_locked(lineage, binding, None)
                except _files.LockTimeout:
                    raise _fail("archive_busy", "Another capture holds this archive", retryable=True) from None

    def latest(self, binding: CaptureBinding) -> CaptureReceipt | None:
        """The newest committed receipt of the binding's lineage, or None when nothing is committed."""
        self._require_binding(binding)
        with self._reading(binding.provider, encode_component(binding.conversation_id)) as lineage:
            if lineage is None:
                return None
            header = self._read_header(lineage)
            if header is None:
                if self._pointer(lineage) is not None:
                    raise _fail("archive_integrity", "The lineage header is missing")
                return None
            self._check_identity(header, binding)
            return self._committed(lineage)

    def load_receipt(self, receipt_id: str) -> CaptureReceipt:
        """Load one committed receipt by ID (a receipt published ahead of the pointer is not loadable)."""
        try:
            provider, encoded, sequence = fmt.parse_receipt_id(receipt_id)
        except fmt.FormatError:
            raise ValueError("receipt_id is not a receipt ID") from None
        if len(encoded) > MAX_ENCODED_CONVERSATION:
            raise ValueError("receipt_id is not a receipt ID")
        with self._reading(provider, encoded) as lineage:
            if lineage is None:
                raise _fail("not_found", "No such archive")
            return self._load_committed(lineage, sequence)

    def read_raw(self, receipt: CaptureReceipt | str, *, offset: int, limit: int) -> RawRange:
        """Up to ``limit`` bytes at ``offset``, never beyond the receipt's committed boundary.

        The receipt's whole prefix is stream-verified against its digest first (bounded memory),
        so the call costs a pass over the prefix.
        """
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 < limit <= MAX_READ_BYTES:
            raise ValueError(f"offset must be nonnegative and limit between 1 and {MAX_READ_BYTES}")
        committed = self._resolve(receipt)
        if offset > committed.length:
            raise ValueError("offset is beyond the receipt's committed boundary")
        with ExitStack() as stack:
            payload = self._open_verified_payload(stack, committed)
            data = _files.pread_full(payload, min(limit, committed.length - offset), offset)
        end = offset + len(data)
        return RawRange(committed.receipt_id, data, offset, end if end < committed.length else None, committed.length)

    def iter_records(self, receipt: CaptureReceipt | str, *, offset: int = 0) -> RecordIterator:
        """Raw record locators (parsed JSON where valid, malformed/partial evidence otherwise).

        ``offset`` must be a record boundary (0 or just after a newline) inside the receipt. The
        receipt's prefix is verified before the first record is returned.
        """
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        committed = self._resolve(receipt)
        if offset > committed.length:
            raise ValueError("offset is beyond the receipt's committed boundary")
        stack = ExitStack()
        try:
            payload = self._open_verified_payload(stack, committed)
            if offset and _files.pread_full(payload, 1, offset - 1) != b"\n":
                raise ValueError("offset is not a record boundary")
        except BaseException:
            stack.close()
            raise
        return RecordIterator(stack, payload, committed, offset)

    def index_entries(self, receipt: CaptureReceipt | str, *, start: int = 0, limit: int = 1024) -> tuple[IndexEntry, ...]:
        """Entries ``[start, start+limit)`` of the incremental malformed/oversized-record index, verified.

        A supplementary read of the index evidence the receipt cites; it adds no authority.
        """
        if type(start) is not int or start < 0 or type(limit) is not int or not 0 < limit <= 65536:
            raise ValueError("start must be nonnegative and limit between 1 and 65536")
        committed = self._resolve(receipt)
        size = committed.index_entries * _providers.ENTRY_SIZE
        if not size:
            return ()
        with self._reading(committed.provider, encode_component(committed.conversation_id)) as lineage:
            if lineage is None:
                raise _fail("archive_integrity", "The archive disappeared")
            try:
                index = _files.open_regular(lineage.fd, INDEX)
            except FileNotFoundError:
                raise _fail("archive_integrity", "The record index is missing") from None
            try:
                self._stream(index, size, [(size, committed.index_sha256)])
                count = max(min(limit, committed.index_entries - start), 0)
                data = _files.pread_full(index, count * _providers.ENTRY_SIZE, start * _providers.ENTRY_SIZE)
            finally:
                os.close(index)
        return tuple(IndexEntry(*entry) for entry in _providers.unpack_entries(data))

    # ------------------------------------------------------------------- opening lineages

    @contextmanager
    def _guarded(self):
        """Map low-level failures of an archive operation to typed errors."""
        try:
            yield
        except _files.UnsafePath as error:
            raise _fail("unsafe_path", f"Archive path is unsafe: {error}") from error
        except OSError as error:
            raise _fail("io_error", f"Archive file access failed: {error.strerror or error}", retryable=True) from error

    @contextmanager
    def _open(self, provider: str, encoded: str, *, create: bool):
        with ExitStack() as stack:
            try:
                root = _files.open_directory_chain(self._root, create_last=create)
            except FileNotFoundError:
                if create:
                    raise _fail("archive_root_missing", "The archive root's parent directory does not exist") from None
                raise
            stack.callback(os.close, root)
            provider_fd = _files.open_child_directory(root, provider, create=create)
            stack.callback(os.close, provider_fd)
            lineage = _Lineage(_files.open_child_directory(provider_fd, encoded, create=create), f"{provider}:{encoded}")
            stack.callback(lineage.close)
            yield lineage

    @contextmanager
    def _reading(self, provider: str, encoded: str):
        """The lineage for a read-only operation (None when it does not exist); creates nothing."""
        with self._guarded():
            try:
                stack = ExitStack()
                lineage = stack.enter_context(self._open(provider, encoded, create=False))
            except FileNotFoundError:
                lineage, stack = None, None
            try:
                yield lineage
            finally:
                if stack is not None:
                    stack.close()

    @staticmethod
    def _require_binding(binding: object) -> None:
        if not isinstance(binding, CaptureBinding):
            raise TypeError("binding must be a CaptureBinding")

    # --------------------------------------------------------------- reading stored records

    def _read_header(self, lineage: _Lineage) -> dict | None:
        data = _files.read_small(lineage.fd, HEADER, fmt.SMALL_LIMIT)
        if data is None:
            return None
        try:
            return fmt.decode_header(data, lineage.archive_id)
        except fmt.FormatError as error:
            raise _fail("archive_integrity", str(error)) from error

    @staticmethod
    def _check_identity(header: dict, binding: CaptureBinding) -> None:
        if header["agent_id"] != binding.agent_id:
            raise _fail("agent_mismatch", "This archive lineage belongs to another agent")
        if (header["provider"], header["conversation_id"]) != (binding.provider, binding.conversation_id):
            raise _fail("archive_integrity", "The lineage header names another conversation")

    def _receipt(self, lineage: _Lineage, sequence: int) -> CaptureReceipt | None:
        receipts = lineage.child(RECEIPTS)
        if receipts is None:
            return None
        try:
            fd = _files.open_regular(receipts, fmt.receipt_name(sequence))
        except FileNotFoundError:
            return None
        try:
            data = _files.pread_full(fd, fmt.RECORD_SIZE + 1, 0)
        finally:
            os.close(fd)
        try:
            return fmt.decode_receipt(data, archive_id=lineage.archive_id, sequence=sequence)
        except fmt.FormatError as error:
            raise _fail("archive_integrity", f"Receipt {sequence}: {error}") from error

    def _pointer(self, lineage: _Lineage) -> tuple[int, str] | None:
        data = _files.read_small(lineage.fd, POINTER, fmt.SMALL_LIMIT)
        if data is None:
            return None
        try:
            return fmt.decode_pointer(data, lineage.archive_id)
        except fmt.FormatError as error:
            raise _fail("archive_integrity", str(error)) from error

    def _committed(self, lineage: _Lineage) -> CaptureReceipt | None:
        pointer = self._pointer(lineage)
        if pointer is None:
            return None
        sequence, digest = pointer
        receipt = self._receipt(lineage, sequence)
        if receipt is None or receipt.record_sha256 != digest:
            raise _fail("archive_integrity", "The latest pointer does not match its receipt")
        return receipt

    def _load_committed(self, lineage: _Lineage, sequence: int) -> CaptureReceipt:
        head = self._committed(lineage)
        receipt = self._receipt(lineage, sequence)
        if head is None or sequence > head.sequence:
            if receipt is None:
                raise _fail("not_found", "No such receipt")
            raise _fail("receipt_uncommitted", "That receipt is published but not committed by the latest pointer")
        if receipt is None:
            raise _fail("archive_integrity", "A committed receipt is missing")
        if sequence < head.sequence:
            following = self._receipt(lineage, sequence + 1)
            if following is None or following.predecessor_sha256 != receipt.record_sha256:
                raise _fail("archive_integrity", "A committed receipt does not match its successor's citation")
        return receipt

    def _resolve(self, receipt: CaptureReceipt | str) -> CaptureReceipt:
        """The committed on-disk receipt a caller's value names; a forged or stale value is refused."""
        if isinstance(receipt, CaptureReceipt):
            loaded = self.load_receipt(receipt.receipt_id)
            if loaded != receipt:
                raise _fail("receipt_mismatch", "The receipt value differs from the committed receipt")
            return loaded
        return self.load_receipt(receipt)

    def _open_verified_payload(self, stack: ExitStack, receipt: CaptureReceipt) -> int:
        """Open the payload read-only and stream-verify the receipt's whole prefix before any byte is used."""
        with self._guarded():
            lineage = stack.enter_context(
                self._open(receipt.provider, encode_component(receipt.conversation_id), create=False))
            try:
                fd = _files.open_regular(lineage.fd, PAYLOAD)
            except FileNotFoundError:
                raise _fail("archive_integrity", "The archive payload is missing") from None
            stack.callback(os.close, fd)
            self._stream(fd, receipt.length, [(receipt.length, receipt.prefix_sha256)])
        return fd

    # --------------------------------------------------------------- streaming verification

    def _stream(self, fd: int, total: int, checkpoints, source: int | None = None):
        """Hash ``fd[0:total]`` in bounded chunks, checking each ``(length, digest)`` checkpoint on the way.

        With ``source`` the bytes are also compared with the source's; the second result is the
        length of the common prefix (``total`` when they agree throughout). The hash state is
        returned so a caller can continue it; after a divergence only the checkpoints are still
        hashed, so the state is then not usable beyond them.
        """
        expected: dict[int, str] = {}
        for length, digest in checkpoints:
            if expected.setdefault(length, digest) != digest:
                raise _fail("archive_integrity", "Receipts disagree about the digest of one prefix length")
        hasher = hashlib.sha256()
        position, diverged = 0, None
        last_checkpoint = max(expected, default=0)
        for stop in sorted({*expected, total}):
            if stop > total:
                raise _fail("archive_integrity", "A receipt extends beyond the stored bytes")
            while position < stop:
                if diverged is not None and position >= last_checkpoint:
                    return hasher, diverged
                size = min(CHUNK, stop - position)
                data = _files.pread_full(fd, size, position)
                if len(data) != size:
                    raise _fail("archive_integrity", "The stored bytes are shorter than their receipts")
                hasher.update(data)
                if source is not None and diverged is None:
                    other = _files.pread_full(source, size, position)
                    if other != data:
                        diverged = position + _common_prefix(data, other)
                position += size
            if stop in expected and hasher.hexdigest() != expected[stop]:
                raise _fail("archive_integrity", f"The stored bytes do not match the receipt digest at {stop}")
        return hasher, total if diverged is None else diverged

    # ------------------------------------------------------------------- state and recovery

    @staticmethod
    def _check_link(lineage: _Lineage, header: dict, previous: CaptureReceipt | None, current: CaptureReceipt) -> None:
        """Invariants of one chain link; recovery relies on them before it adopts anything."""
        problem = None
        if (current.agent_id, current.provider, current.conversation_id) != (
                header["agent_id"], header["provider"], header["conversation_id"]):
            problem = "identity differs from the lineage header"
        elif current.malformed != current.index_entries:
            problem = "index entry count differs from the malformed count"
        elif current.appended != current.length - (previous.length if previous else 0):
            problem = "appended bytes differ from the committed length change"
        elif previous is not None:
            if current.predecessor_sha256 != previous.record_sha256:
                problem = "predecessor citation does not match the previous record"
            elif (current.length < previous.length or current.records < previous.records
                  or current.malformed < previous.malformed or current.record_boundary < previous.record_boundary):
                problem = "committed progress moves backwards"
            elif current.workspace != previous.workspace:
                problem = "workspace changed within a lineage"
            # source_device is not compared: device numbers can change across a restart (see qorqut.file_identity).
            elif ((current.source_path, current.source_inode)
                  != (previous.source_path, previous.source_inode)
                  and not _relocation.permits(lineage, previous, current.source_path, current.source_device,
                                              current.source_inode, current.generation)):
                problem = "source binding changed within a lineage"
        if problem:
            raise _fail("archive_integrity", f"Receipt {current.sequence}: {problem}")

    def _load_state(self, lineage: _Lineage, header: dict | None) -> _State:
        payload_size = _files.file_size(lineage.fd, PAYLOAD)
        index_size = _files.file_size(lineage.fd, INDEX)
        has_pointer = self._pointer(lineage) is not None
        if header is None:
            if has_pointer or payload_size or index_size or lineage.child(RECEIPTS) is not None:
                raise _fail("archive_integrity", "The lineage header is missing")
            return _State(None, None, [], None, 0, 0)
        pointer = self._committed(lineage) if has_pointer else None
        ahead: list[CaptureReceipt] = []
        sequence = (pointer.sequence if pointer else 0) + 1
        while (receipt := self._receipt(lineage, sequence)) is not None:
            ahead.append(receipt)
            sequence += 1
        chain = None
        if ahead:
            # A receipt published ahead of its pointer, or a lost pointer: validate the complete chain.
            chain = []
            for number in range(1, (pointer.sequence if pointer else 0) + 1):
                member = self._receipt(lineage, number)
                if member is None:
                    raise _fail("archive_integrity", f"Receipt {number} is missing from the chain")
                chain.append(member)
            chain.extend(ahead)
            for previous, current in zip([None, *chain], chain):
                self._check_link(lineage, header, previous, current)
        elif pointer is not None:
            previous = self._receipt(lineage, pointer.sequence - 1) if pointer.sequence > 1 else None
            if pointer.sequence > 1 and previous is None:
                raise _fail("archive_integrity", "The receipt before the latest is missing")
            self._check_link(lineage, header, previous, pointer)
        return _State(header, pointer, ahead, chain, payload_size, index_size)

    # ------------------------------------------------------------------------- the writer

    @staticmethod
    def _fields(binding: CaptureBinding, *, sequence, predecessor, device, inode, observed_at, source_size,
                source_size_end, length, digest, appended, reasons, final, records, malformed, boundary, tail,
                entries, index_digest) -> dict:
        return {
            "format": fmt.RECEIPT_FORMAT, "archive_id": binding.archive_id, "sequence": sequence,
            "predecessor_sha256": predecessor, "agent_id": binding.agent_id, "provider": binding.provider,
            "conversation_id": binding.conversation_id, "generation": binding.generation,
            "workspace": binding.workspace, "source_path": binding.source_path, "source_device": device,
            "source_inode": inode, "observed_at": observed_at, "source_size": source_size,
            "source_size_end": source_size_end, "length": length, "prefix_sha256": digest, "appended": appended,
            "complete": not reasons, "incomplete_reasons": list(reasons),
            "final": None if final is None else {
                "process_token": final.process_token, "exited_at": fmt.timestamp(final.exited_at)},
            "records": records, "malformed": malformed, "record_boundary": boundary, "partial_tail_bytes": tail,
            "index_entries": entries, "index_sha256": index_digest,
        }

    def _check_fits(self, binding: CaptureBinding, final: FinalCaptureEvidence | None) -> None:
        """Refuse, before anything is written, a binding whose worst-case receipt exceeds the fixed size."""
        worst = (1 << 64) - 1
        fields = self._fields(
            binding, sequence=999999999999, predecessor="f" * 64, device=worst, inode=worst, observed_at="x" * 27,
            source_size=worst, source_size_end=worst, length=worst, digest="f" * 64, appended=worst, reasons=(),
            final=final, records=worst, malformed=worst, boundary=worst, tail=worst, entries=worst,
            index_digest="f" * 64)
        try:
            fmt.encode_receipt(fields)
        except fmt.FormatError:
            raise _fail("receipt_too_large", "The binding's identifiers and paths do not fit one fixed-size receipt") from None

    def _capture_locked(self, lineage: _Lineage, binding: CaptureBinding, final: FinalCaptureEvidence | None) -> CaptureReceipt:
        header = self._read_header(lineage)
        if header is not None:
            self._check_identity(header, binding)        # a wrong-agent error carries no other agent's receipt
        state = None
        try:
            state = self._load_state(lineage, header)
            _files.remove_temporaries(lineage.fd)
            staging = lineage.child(STAGING, create=True)
            if state.payload_size == state.committed:
                # No uncommitted payload exists, so staging leftovers are only abandoned temporaries of
                # an interrupted writer (copies of source bytes and unpublished receipt images).
                _files.remove_all_files(staging)
            return self._advance(lineage, binding, final, state, staging)
        except CaptureError as error:
            if error.latest is None:
                error.latest = state.pointer if state else self._trusted_latest(lineage)
            raise
        except OSError as error:
            raise _fail("io_error", f"Archive file access failed: {error.strerror or error}", retryable=True,
                        latest=state.pointer if state else self._trusted_latest(lineage)) from error

    def _trusted_latest(self, lineage: _Lineage) -> CaptureReceipt | None:
        try:
            return self._committed(lineage)
        except CaptureError:
            return None

    def _advance(self, lineage: _Lineage, binding: CaptureBinding, final: FinalCaptureEvidence | None,
                 state: _State, staging: int) -> CaptureReceipt:
        candidate = state.candidate
        if candidate is not None:
            if candidate.workspace != binding.workspace:
                raise _fail("workspace_mismatch", "The binding's workspace differs from the archive lineage's")
        committed, payload_size = state.committed, state.payload_size
        if payload_size < committed:
            raise _fail("archive_integrity", "The payload is shorter than the newest receipt")
        try:
            source = _files.Source.open(binding.source_path)
        except _files.UnsafePath as error:
            raise _fail("unsafe_path", f"Transcript source is unsafe: {error}") from error
        except FileNotFoundError:
            raise _fail("source_unavailable", "The transcript source does not exist", retryable=True) from None
        with ExitStack() as stack:
            stack.callback(source.close)
            first = source.stat()
            if candidate is not None:
                moved = candidate.source_path != binding.source_path
                replaced = not file_identity.matches(
                    {"device": candidate.source_device, "inode": candidate.source_inode},
                    {"device": first.st_dev, "inode": first.st_ino}, os.fstat(source.parent_fd).st_dev)
                if (moved or replaced) and not _relocation.permits(
                        lineage, candidate, binding.source_path, first.st_dev, first.st_ino, binding.generation):
                    raise _fail("source_path_changed" if moved else "source_replaced",
                                "The transcript source differs from the archive lineage's verified binding")

            payload = self._existing(stack, lineage, PAYLOAD)
            # Stream-verify the committed prefix (and any uncommitted tail) against its digests and
            # against the exact source prefix, in one bounded-memory pass.
            checkpoints = [(r.length, r.prefix_sha256) for r in (state.chain or ([state.pointer] if state.pointer else []))]
            hasher, agreed = self._stream(payload, payload_size, checkpoints, source.fd)
            if agreed < committed:
                if first.st_size < committed:
                    raise _fail("source_shrunk", "The transcript source is shorter than the archived prefix")
                raise _fail("source_prefix_changed", "The transcript source no longer contains the archived prefix")
            index = self._existing(stack, lineage, INDEX)
            index_hasher = self._verify_index_state(index, state)
            if state.ahead:
                # The chain was validated against the actual payload bytes and the current exact source;
                # only now does the pointer adopt the receipt(s) a crashed writer published.
                self._publish_pointer(lineage, candidate)
                state.pointer, state.ahead = candidate, []
            if payload_size > committed and agreed < payload_size:
                raise _fail("unresolved_tail", "The payload has uncommitted bytes the transcript source cannot confirm",
                            latest=candidate)

            observed = max(first.st_size, payload_size)
            scan = _Scan(binding, candidate)
            self._reframe(scan, payload, candidate, payload_size)
            staged = None
            try:
                if observed > payload_size:
                    staged = self._stage_suffix(staging, source, payload_size, observed, hasher, scan)
                self._crash_point("suffix_read")
                last = source.stat()
                if source.path_identity() != (first.st_dev, first.st_ino):
                    raise _fail("source_replaced", "The transcript source path was replaced during capture", retryable=True)
                if last.st_size < observed:
                    raise _fail("source_changed", "The transcript source shrank during capture", retryable=True)
                if staged is not None and not self._same(staged, source, payload_size, observed):
                    raise _fail("source_changed", "The transcript source changed during capture", retryable=True)
                if not scan.evidence.bound:
                    raise _fail("no_identity_evidence",
                                "The transcript has no complete matching session/workspace evidence", retryable=True)
                self._crash_point("staged")

                stable = last.st_size == first.st_size and last.st_mtime_ns == first.st_mtime_ns
                tail_start, tail = scan.scanner.pending()
                final_ok = final is not None and stable
                reasons = []
                if tail and not final_ok:
                    reasons.append(REASON_PARTIAL_TAIL)
                if not stable:
                    reasons.append(REASON_UNSTABLE)
                final_record = final if final_ok else None
                added = observed - committed
                if added == 0 and not self._new_evidence(candidate, binding, final_record, not reasons):
                    if staged is not None:
                        staged.discard()
                    return candidate

                if state.header is None:
                    self._ensure_header(lineage, binding)
                if staged is not None:
                    if payload == NO_FILE:
                        payload = self._create(lineage, PAYLOAD, stack)
                    self._append(payload, staged, payload_size)
                if added:
                    # Matching content is not durability: bytes a crashed writer left in place cross the same
                    # barrier as bytes written now, before any receipt cites them.
                    os.fsync(payload)
                self._crash_point("payload_appended")
                if scan.entries and index == NO_FILE:
                    index = self._create(lineage, INDEX, stack)
                if self._append_index(index, state, bytes(scan.entries)):
                    self._crash_point("index_appended")
                if payload_size > committed or state.index_size > self._index_bytes(candidate):
                    _files.fsync_directory(lineage.fd)    # a crashed writer may not have synced the new files' entries
                index_hasher.update(bytes(scan.entries))
                fields = self._fields(
                    binding, sequence=(candidate.sequence if candidate else 0) + 1,
                    predecessor=candidate.record_sha256 if candidate else None, device=first.st_dev,
                    inode=first.st_ino, observed_at=fmt.timestamp(self._clock()), source_size=first.st_size,
                    source_size_end=last.st_size, length=observed, digest=hasher.hexdigest(), appended=added,
                    reasons=tuple(reasons), final=final_record, records=scan.records, malformed=scan.malformed,
                    boundary=tail_start, tail=tail, entries=scan.malformed, index_digest=index_hasher.hexdigest())
                receipt = self._publish_receipt(lineage, staging, fields)
                self._publish_pointer(lineage, receipt)
                if staged is not None:
                    staged.discard()
                _files.remove_all_files(staging)
                return receipt
            except (CaptureError, OSError):
                if staged is not None:
                    staged.discard()
                raise

    @staticmethod
    def _reframe(scan: _Scan, payload: int, candidate: CaptureReceipt | None, payload_size: int) -> None:
        """Feed the stored bytes after the newest receipt's record boundary to the scan.

        The boundary is a stored claim, so it is checked against the bytes first: it must follow a
        newline (or be the start) and the committed bytes after it must contain no complete line.
        """
        if candidate is not None and scan.start:
            if _files.pread_full(payload, 1, scan.start - 1) != b"\n":
                raise _fail("archive_integrity", "The newest receipt's record boundary is not a record boundary")
        committed = candidate.length if candidate else 0
        position = scan.start
        while position < payload_size:
            stop = committed if position < committed else payload_size
            data = _files.pread_full(payload, min(CHUNK, stop - position), position)
            scan.feed(data)
            position += len(data)
            if position == committed and scan.scanner.pending() != (scan.start, committed - scan.start):
                raise _fail("archive_integrity", "The newest receipt's tail cursor does not match the stored bytes")

    @staticmethod
    def _new_evidence(candidate: CaptureReceipt, binding: CaptureBinding, final: FinalCaptureEvidence | None,
                      complete: bool) -> bool:
        """Whether an observation that adds no bytes still carries genuinely new evidence."""
        if (final is not None and candidate.final is not None and candidate.generation == binding.generation
                and candidate.final != final):
            raise _fail("final_evidence_conflict", "Different final evidence was already recorded for this generation")
        return (binding.generation != candidate.generation
                or (final is not None and candidate.final != final)
                or (complete and not candidate.complete))

    @staticmethod
    def _existing(stack: ExitStack, lineage: _Lineage, name: str) -> int:
        """Descriptor of an existing archive file, or NO_FILE when it has not been created yet."""
        try:
            fd = _files.open_regular(lineage.fd, name, os.O_RDWR)
        except FileNotFoundError:
            return NO_FILE
        stack.callback(os.close, fd)
        return fd

    @staticmethod
    def _create(lineage: _Lineage, name: str, stack: ExitStack) -> int:
        fd = _files.open_regular(lineage.fd, name, os.O_RDWR | os.O_CREAT)
        stack.callback(os.close, fd)
        _files.fsync_directory(lineage.fd)               # the new entry must be durable before a receipt cites the file
        return fd

    def _verify_index_state(self, index: int, state: _State):
        committed = self._index_bytes(state.candidate)
        if state.index_size < committed:
            raise _fail("archive_integrity", "The record index is shorter than the newest receipt")
        if state.index_size > committed and state.payload_size == state.committed:
            raise _fail("archive_integrity", "The record index is ahead of the payload")
        checkpoints = [(r.index_entries * _providers.ENTRY_SIZE, r.index_sha256)
                       for r in (state.chain or ([state.pointer] if state.pointer else []))]
        hasher, _ = self._stream(index, committed, checkpoints)
        return hasher

    # ---- staging, appending and publishing

    def _stage_suffix(self, staging: int, source: _files.Source, start: int, stop: int, hasher, scan: _Scan) -> _Staged:
        """Copy only ``source[start:stop]`` to a private staging file while hashing and framing it."""
        name = f"{os.urandom(8).hex()}.suffix"
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=staging)
        staged = _Staged(staging, name, fd)
        try:
            position = start
            while position < stop:
                data = _files.pread_full(source.fd, min(CHUNK, stop - position), position)
                if not data:
                    raise _fail("source_changed", "The transcript source shrank during capture", retryable=True)
                _files.pwrite_full(fd, data, position - start)
                hasher.update(data)
                scan.feed(data)
                position += len(data)
        except BaseException:
            staged.discard()
            raise
        return staged

    @staticmethod
    def _same(staged: _Staged, source: _files.Source, start: int, stop: int) -> bool:
        """Re-read the source suffix and compare it with what was staged (catches an in-place rewrite)."""
        position = start
        while position < stop:
            size = min(CHUNK, stop - position)
            if _files.pread_full(staged.fd, size, position - start) != _files.pread_full(source.fd, size, position):
                return False
            position += size
        return True

    def _append(self, payload: int, staged: _Staged, offset: int) -> None:
        """Append the staged suffix at the exact committed end (the caller flushes the payload)."""
        if os.fstat(payload).st_size != offset:
            raise _fail("archive_integrity", "The payload changed size while the archive lock was held")
        position, end = 0, os.fstat(staged.fd).st_size
        while position < end:
            data = _files.pread_full(staged.fd, min(self._COPY_CHUNK, end - position), position)
            _files.pwrite_full(payload, data, offset + position)
            position += len(data)
            self._crash_point("payload_chunk")

    @staticmethod
    def _append_index(index: int, state: _State, entries: bytes) -> bool:
        """Append the new index entries after verifying any already-present uncommitted ones equal them."""
        if index == NO_FILE:
            return False
        committed = TranscriptArchive._index_bytes(state.candidate)
        present = os.fstat(index).st_size - committed
        if present > len(entries) or _files.pread_full(index, present, committed) != entries[:present]:
            raise _fail("archive_integrity", "The record index holds entries the payload does not confirm")
        if not entries:
            return False
        if present < len(entries):
            _files.pwrite_full(index, entries[present:], committed + present)
        os.fsync(index)                                   # entries a crashed writer left in place need the barrier too
        return True

    @staticmethod
    def _index_bytes(receipt: CaptureReceipt | None) -> int:
        return (receipt.index_entries if receipt else 0) * _providers.ENTRY_SIZE

    def _ensure_header(self, lineage: _Lineage, binding: CaptureBinding) -> None:
        temporary = _files.stage_temporary(
            lineage.fd, fmt.encode_header(binding.agent_id, binding.provider, binding.conversation_id))
        try:
            _files.publish_immutable(lineage.fd, temporary, lineage.fd, HEADER)
        except FileExistsError:
            self._check_identity(self._read_header(lineage), binding)

    def _publish_receipt(self, lineage: _Lineage, staging: int, fields: dict) -> CaptureReceipt:
        data = fmt.encode_receipt(fields)
        receipts = lineage.child(RECEIPTS, create=True)
        temporary = _files.stage_temporary(staging, data)
        self._crash_point("receipt_temp")
        try:
            _files.publish_immutable(staging, temporary, receipts, fmt.receipt_name(fields["sequence"]))
        except FileExistsError:
            raise _fail("archive_integrity", "A receipt with this number already exists unadopted") from None
        self._crash_point("receipt_published")
        return fmt.decode_receipt(data, archive_id=fields["archive_id"], sequence=fields["sequence"])

    def _publish_pointer(self, lineage: _Lineage, receipt: CaptureReceipt) -> None:
        temporary = _files.stage_temporary(
            lineage.fd, fmt.encode_pointer(receipt.archive_id, receipt.sequence, receipt.record_sha256))
        self._crash_point("pointer_temp")
        _files.replace_atomic(lineage.fd, temporary, POINTER)
        self._crash_point("pointer_published")
