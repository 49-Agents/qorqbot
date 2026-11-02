"""Deterministic Git tree files for one Operations history export.

Small tables are one JSON-lines file; a table that declares sharding writes one file per fixed hash-prefix shard of its
stable key, so a new row rewrites one small file. Rows keep the export's stable key order, the manifest carries the
coverage and schema identity, and no clock value enters any file: identical state produces identical bytes.
"""
from __future__ import annotations

import hashlib

from qorqut_operations.contracts import canonical_bytes

from .catalog import HistoryExport

DATABASE = "database"
MANIFEST = f"{DATABASE}/manifest.json"


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def export_files(export: HistoryExport) -> dict[str, bytes]:
    """``{tree path: bytes}`` for the exported rows and the coverage/schema manifest."""
    buckets: dict[str, list[bytes]] = {}
    for name, table in export.tables.items():
        for row in table.rows:
            shard = table.shard_of(row)
            path = f"{DATABASE}/{name}.jsonl" if shard is None else f"{DATABASE}/{name}/{shard}.jsonl"
            buckets.setdefault(path, []).append(canonical_bytes(row) + b"\n")
    files = {path: b"".join(lines) for path, lines in buckets.items()}
    files[MANIFEST] = export.manifest_bytes() + b"\n"
    return files
