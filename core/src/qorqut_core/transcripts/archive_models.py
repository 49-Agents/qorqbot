"""Bounded host archive values; capture coordinates retain their own lineage."""

from __future__ import annotations

from dataclasses import fields
import hashlib
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, Field, StrictBool, TypeAdapter, field_validator, model_validator

from ..contracts import CoreError, Counter, Model, Revision, Timestamp
from .spool import CHUNK_BYTES, Chunk, RunBinding, Source


def canonical_id(value: str) -> str:
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("Expected a canonical UUID")
    return value


Identity = Annotated[str, Field(strict=True), AfterValidator(canonical_id)]


def text(value: str) -> str:
    value.encode("utf-8")
    if "\0" in value:
        raise ValueError("Metadata cannot contain NUL")
    return value


Evidence = Annotated[str, Field(strict=True, min_length=1, max_length=4096), AfterValidator(text)]
_counter = TypeAdapter(Counter)
_revision = TypeAdapter(Revision)
_evidence = TypeAdapter(Evidence)


class ArchiveMetadata(Model):
    capability_evidence: Evidence
    locator: Evidence | None = None
    file_identity: Evidence | None = None
    native_created_at: Timestamp | None = None
    previous_epoch: Identity | None = None
    divergence_reason: Evidence | None = None

    @field_validator("native_created_at", mode="before")
    @classmethod
    def bounded_time(cls, value):
        # Bound the supplied text before shared Timestamp normalization can
        # shorten an arbitrarily long fractional-seconds representation.
        return _evidence.validate_python(value) if value is not None else None

    @model_validator(mode="after")
    def predecessor(self):
        if (self.previous_epoch is None) != (self.divergence_reason is None):
            raise ValueError("Predecessor and divergence reason must be paired")
        return self


class ArchiveState(Model):
    source_id: Identity
    source_epoch: Identity
    host_archived_offset: Counter
    recovery_epoch: Identity
    registered_recovery_epoch: Identity
    recovery_held: StrictBool
    conflicted: StrictBool


class ArchiveReceipt(Model):
    binding: RunBinding
    source_id: Identity
    source_epoch: Identity
    recovery_epoch: Identity
    host_archived_offset: Counter


def validate_binding(binding: RunBinding) -> RunBinding:
    if not isinstance(binding, RunBinding):
        raise ValueError("Expected a capture run binding")
    values = {f.name: getattr(binding, f.name) for f in fields(RunBinding)}
    for name, value in values.items():
        if name in ("environment_generation", "run_generation"):
            _revision.validate_python(value)
        else:
            canonical_id(value)
    return RunBinding(**values)


def validate_source(source: Source) -> Source:
    if not isinstance(source, Source):
        raise ValueError("Expected a capture source")
    source = Source(**{f.name: getattr(source, f.name) for f in fields(Source)})
    for value in (source.source_id, source.installation_id, source.agent_id):
        canonical_id(value)
    if source.parent_source_id is not None:
        canonical_id(source.parent_source_id)
    if source.native_session_id is not None:
        text(source.native_session_id)
    return source


_absent = object()


def coordinate(source_id, source_epoch, offset=_absent):
    try:
        canonical_id(source_id)
        canonical_id(source_epoch)
        if offset is not _absent:
            _counter.validate_python(offset)
    except (ValueError, TypeError, AttributeError):
        raise CoreError("invalid_argument", "Invalid archive coordinates") from None


def chunk_coordinates(chunk: Chunk) -> None:
    coordinate(chunk.source_id, chunk.source_epoch, chunk.offset)
    _counter.validate_python(chunk.length)
    if not 1 <= chunk.length <= CHUNK_BYTES:
        raise ValueError("Invalid chunk length")
    _counter.validate_python(chunk.offset + chunk.length)
    if (type(chunk.sha256) is not str or len(chunk.sha256) != 64
            or any(c not in "0123456789abcdef" for c in chunk.sha256)):
        raise ValueError("Invalid chunk digest")


def validate_chunk(chunk: Chunk) -> Chunk:
    if not isinstance(chunk, Chunk):
        raise ValueError("Expected a capture chunk")
    chunk = Chunk(**{f.name: getattr(chunk, f.name) for f in fields(Chunk)})
    chunk_coordinates(chunk)
    if type(chunk.payload) is not bytes or len(chunk.payload) != chunk.length:
        raise ValueError("Invalid chunk shape")
    if hashlib.sha256(chunk.payload).hexdigest() != chunk.sha256:
        raise ValueError("Invalid chunk digest")
    return chunk
