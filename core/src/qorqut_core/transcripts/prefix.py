"""Finite, read-only prefix evidence from a caller-owned regular descriptor.

Receipt hashes compare supplied bytes even after payload reclamation; they do
not recover originals or attest native ownership, readiness or future bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import stat

from .spool import BindingConflict, CaptureBlocked, CaptureError, CaptureSpool


_READ_BYTES = 65536


class FilePrefixChanged(CaptureError):
    """A fixed comparison outcome; reason is read-only, traceback is ordinary."""

    def __init__(self, reason: str):
        if reason not in {"identity_changed", "prefix_shorter", "prefix_mismatch",
                          "changed_during_read"}:
            raise ValueError("invalid file prefix change reason")
        object.__setattr__(self, "_reason", reason)
        super().__init__(reason)

    @property
    def reason(self) -> str:
        return self._reason

    def __setattr__(self, name, value):
        if name in {"reason", "_reason"}:
            raise AttributeError("file prefix change reason is immutable")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in {"reason", "_reason"}:
            raise AttributeError("file prefix change reason is immutable")
        super().__delattr__(name)


@dataclass(frozen=True)
class FilePrefixProof:
    source_id: str
    source_epoch: str
    through_offset: int
    device: int
    inode: int
    file_size: int
    mtime_ns: int
    ctime_ns: int
    prefix_sha256: str
    tail_offset: int
    tail_length: int
    tail_sha256: str


def verify_file_prefix(spool: CaptureSpool, source_id: str, source_epoch: str, fd: int, *,
                       expected_device: int, expected_inode: int,
                       through: int | None = None) -> FilePrefixProof:
    """Compare a frozen receipt prefix without opening, seeking or closing fd.

    Suffix bytes already present are not read or certified. Final stat equality
    rejects observed changes, including growth; it is not a filesystem snapshot.
    """
    for value in (fd, expected_device, expected_inode):
        if type(value) is not int or value < 0:
            raise ValueError("descriptor and file identity must be nonnegative integers")
    page = spool.receipts(source_id, source_epoch, through=through)
    if page.source.source_type != "native":
        raise BindingConflict("file prefix verification requires a declared native source")
    target = page.through_offset

    def observation() -> os.stat_result:
        try:
            return os.fstat(fd)
        except (OSError, OverflowError):
            raise CaptureBlocked("file prefix descriptor is unavailable") from None

    initial = observation()
    if not stat.S_ISREG(initial.st_mode):
        raise CaptureBlocked("file prefix requires a regular descriptor")
    if (initial.st_dev, initial.st_ino) != (expected_device, expected_inode):
        raise FilePrefixChanged("identity_changed")
    if initial.st_size < target:
        raise FilePrefixChanged("prefix_shorter")

    prefix_hash = hashlib.sha256()
    tail_hash = hashlib.sha256()
    tail_offset = max(0, target - _READ_BYTES)
    while True:
        for receipt in page.receipts:
            receipt_hash = hashlib.sha256()
            position = receipt.offset
            end = position + receipt.length
            while position < end:
                try:
                    data = os.pread(fd, min(_READ_BYTES, end - position), position)
                except (OSError, OverflowError):
                    raise CaptureBlocked("file prefix read failed") from None
                if not data:
                    raise FilePrefixChanged("prefix_shorter")
                receipt_hash.update(data)
                prefix_hash.update(data)
                if position + len(data) > tail_offset:
                    tail_hash.update(data[max(0, tail_offset - position):])
                position += len(data)
            if receipt_hash.hexdigest() != receipt.sha256:
                raise FilePrefixChanged("prefix_mismatch")
        if page.next_offset == target:
            break
        page = spool.receipts(source_id, source_epoch, after=page.next_offset, through=target)

    final = observation()
    if any(getattr(initial, field) != getattr(final, field) for field in
           ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        raise FilePrefixChanged("changed_during_read")
    return FilePrefixProof(source_id, source_epoch, target, initial.st_dev, initial.st_ino,
                           initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns,
                           prefix_hash.hexdigest(), tail_offset, target - tail_offset,
                           tail_hash.hexdigest())
