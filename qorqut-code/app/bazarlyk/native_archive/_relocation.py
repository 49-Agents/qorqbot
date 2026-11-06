"""Explicit, prefix-verified source moves; existing receipts and payload bytes never change."""
from contextlib import ExitStack
import json

from . import _files, _format as fmt
from ._types import CaptureError

FORMAT = "qorqut.native-archive.relocation/1"


def _name(previous):
    return f"relocation-{previous.sequence:012d}.json"


def _record(previous, path, device, inode, generation):
    return {"format": FORMAT, "receipt_id": previous.receipt_id,
            "receipt_sha256": previous.record_sha256, "length": previous.length,
            "prefix_sha256": previous.prefix_sha256, "source_path": path,
            "source_device": device, "source_inode": inode, "generation": generation}


def permits(lineage, previous, path, device, inode, generation):
    raw = _files.read_small(lineage.fd, _name(previous), fmt.SMALL_LIMIT)
    if raw is None:
        return False
    try:
        record = json.loads(raw)
        minimum = record["generation"]
        return (type(minimum) is int and previous.generation < minimum <= generation
                and record == _record(previous, path, device, inode, minimum)
                and raw == fmt.canonical(record) + b"\n")
    except (ValueError, KeyError, TypeError):
        return False


def authorize(archive, lineage, binding, expected):
    header = archive._read_header(lineage)
    if header is None:
        raise CaptureError("not_found", "There is no archived conversation to relocate")
    archive._check_identity(header, binding)
    state = archive._load_state(lineage, header)
    previous = state.pointer
    if previous is None or previous != expected:
        raise CaptureError("receipt_mismatch", "The archive advanced; inspect its latest receipt before relocating")
    if binding.workspace != previous.workspace:
        raise CaptureError("workspace_mismatch", "Source relocation cannot change the registered workspace")
    if binding.generation <= previous.generation:
        raise CaptureError("invalid_binding", "Relocation requires a later verified native session generation")
    if state.ahead or state.payload_size != previous.length or state.index_size != archive._index_bytes(previous):
        raise CaptureError("unresolved_tail", "Recover the outstanding capture before relocating its source")
    with ExitStack() as stack:
        source = _files.Source.open(binding.source_path)
        stack.callback(source.close)
        first = source.stat()
        payload = archive._existing(stack, lineage, "payload.raw")
        _, agreed = archive._stream(payload, previous.length,
                                   [(previous.length, previous.prefix_sha256)], source.fd)
        if agreed != previous.length:
            raise CaptureError("source_prefix_changed", "The copied transcript does not contain the entire archived prefix")
        index = archive._existing(stack, lineage, "index.bin")
        archive._verify_index_state(index, state)
        last = source.stat()
        if source.path_identity() != (first.st_dev, first.st_ino) or last.st_size < first.st_size:
            raise CaptureError("source_changed", "The copied transcript changed during verification", retryable=True)
        record = _record(previous, binding.source_path, first.st_dev, first.st_ino, binding.generation)
        raw = fmt.canonical(record) + b"\n"
        if len(raw) > fmt.SMALL_LIMIT:
            raise CaptureError("receipt_too_large", "Source relocation evidence exceeds its bound")
        existing = _files.read_small(lineage.fd, _name(previous), fmt.SMALL_LIMIT)
        if existing is not None:
            if existing != raw:
                raise CaptureError("relocation_conflict", "This receipt already has a different source relocation")
            return
        temporary = _files.stage_temporary(lineage.fd, raw)
        _files.publish_immutable(lineage.fd, temporary, lineage.fd, _name(previous))
