"""Lossless, provider-aware raw transcript archive for verified native sessions.

``TranscriptArchive(archive_root)`` is inert: it validates a path string and creates nothing.
``capture`` appends only the verified new suffix of an exactly bound transcript source to one
append-only payload per agent/provider/native conversation and publishes a fixed-size, hash
chained receipt; reads are bounded by a committed receipt and verified by streaming. There is no
database, discovery of provider homes or processes, input submission, Chat projection or service
loop here; the design and recovery algorithm are in ``docs/v1.2/NATIVE_ARCHIVE_CHECKPOINT.md``.
"""
from ._archive import MAX_READ_BYTES, RecordIterator, TranscriptArchive
from ._types import (
    CaptureBinding, CaptureError, CaptureReceipt, FinalCaptureEvidence, IndexEntry, RawRange, RawRecord,
)

__all__ = [
    "CaptureBinding", "CaptureError", "CaptureReceipt", "FinalCaptureEvidence", "IndexEntry", "MAX_READ_BYTES",
    "RawRange", "RawRecord", "RecordIterator", "TranscriptArchive",
]
