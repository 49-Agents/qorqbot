"""D45 request text is validated without rewriting its idempotency inputs."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field

from ..contracts import Counter, Model, Mutation, Timestamp, _utf8
from ..messaging.models import EntryReference


def _plain(value: str, *, note=False) -> str:
    _utf8(value)
    if any((ord(c) < 32 and not (note and c in "\n\t")) or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("Unsupported control character")
    return value


def _path(value: str) -> str:
    _plain(value)
    if ".." in value.split("/"):
        raise ValueError("Parent path components are unsupported")
    return value


def _label(value: str) -> str:
    _plain(value)
    if not value.strip():
        raise ValueError("Label requires non-whitespace text")
    return value


PathText = Annotated[str, Field(strict=True, min_length=1, max_length=4096), AfterValidator(_path)]
Label = Annotated[str, Field(strict=True, min_length=1, max_length=200), AfterValidator(_label)]
Note = Annotated[str, Field(strict=True, max_length=4000), AfterValidator(lambda value: _plain(value, note=True))]


class FilesReference(Mutation):
    path: PathText
    agent_id: UUID | None = None
    label: Label | None = None
    note: Note | None = None


class FileReference(Model):
    reference_id: UUID
    agent_id: UUID
    conversation_id: UUID
    actor_id: UUID
    installation_id: UUID
    recovery_epoch: UUID
    state: Literal["referenced"] = "referenced"
    location: Literal["workspace", "host"]
    path: PathText
    requested_path: PathText
    label: Label
    note: Note | None
    size_bytes: Counter
    mtime_ns: Annotated[str, Field(strict=True, pattern=r"^-?[0-9]+$")]
    observed_at: Timestamp
    recorded_at: Timestamp
    sha256: Literal[None] = None


class FileReferenced(Model):
    reference: FileReference
    entry: EntryReference
