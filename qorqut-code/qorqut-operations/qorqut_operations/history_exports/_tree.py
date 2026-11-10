"""Deterministic Git tree files for one Operations history export.

Small tables are one JSON-lines file; a table that declares sharding writes one file per fixed hash-prefix shard of its
stable key, so a new row rewrites one small file. Rows keep the export's stable key order, the manifest carries the
coverage and schema identity, and no clock value enters any file: identical state produces identical bytes.

A table the export reports as unchanged has no rows here; its files are the ones made from the earlier export.
"""
from __future__ import annotations

import hashlib

from qorqut_operations.contracts import canonical_bytes

from .catalog import ExportedTable, HistoryExport

DATABASE = "database"
MANIFEST = f"{DATABASE}/manifest.json"


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def table_files(table: ExportedTable) -> dict[str, bytes]:
    """``{tree path: bytes}`` for all rows of one exported table: every file of that table, and nothing else."""
    name, buckets = table.table, {}
    for row in table.rows:
        shard = table.shard_of(row)
        path = f"{DATABASE}/{name}.jsonl" if shard is None else f"{DATABASE}/{name}/{shard}.jsonl"
        buckets.setdefault(path, []).append(canonical_bytes(row) + b"\n")
    return {path: b"".join(lines) for path, lines in buckets.items()}


def manifest_file(export: HistoryExport) -> bytes:
    return export.manifest_bytes() + b"\n"


def export_files(export: HistoryExport) -> dict[str, bytes]:
    """``{tree path: bytes}`` for the rows of every table checked in this export and the coverage/schema manifest."""
    files: dict[str, bytes] = {}
    for table in export.tables.values():
        files.update(table_files(table))
    files[MANIFEST] = manifest_file(export)
    return files
