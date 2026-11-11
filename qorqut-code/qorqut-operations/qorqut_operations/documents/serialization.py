"""Source document framing: YAML frontmatter plus body, the stable slug and the text-size bounds.

The byte layout is what content hashes, exact history and filed source versions are computed over, so framing is
preserved exactly."""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.storage.structured import parse_yaml

SLUG = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}
BODY = {"type": "string", "maxLength": 200000}
HASH = {"type": "string", "pattern": "^(sha256:)?[a-f0-9]{64}$"}
MAX_TEXT_BYTES = 2_000_000
BINARY_KINDS = {"asset", "data_upload"}


def _metadata(text):
    metadata = parse_yaml(text) or {}
    if not isinstance(metadata, dict):
        raise DomainError("invalid_document", "Document frontmatter must be a mapping")
    return metadata


# File bytes and hashes are still read on every call. Only parsing of identical small
# headers is reused, including across unrelated filing-status invalidations. Never expose
# the cached mapping: split_document returns its own recursive plain() copy below.
_cached_metadata = lru_cache(maxsize=256)(_metadata)


def split_document(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DomainError("invalid_document", "Document is not UTF-8 text") from exc
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            raise DomainError("invalid_document", "Document frontmatter is unterminated")
        header = "".join(lines[1:end])
        # Keep the cache bounded by both entry count and header size (at most 8 MiB of text).
        metadata = (_cached_metadata if len(header.encode('utf-8')) <= 32768 else _metadata)(header)
        body = "".join(lines[end + 1:])
        # Remove only the framing separator, never the source's leading blank lines.
        if body.startswith("\r\n"): body = body[2:]
        elif body.startswith("\n"): body = body[1:]
        return plain(metadata), body
    return {}, text


def document_bytes(metadata, body, *, frontmatter=True):
    if not isinstance(body, str) or len(body.encode()) > MAX_TEXT_BYTES:
        raise DomainError("validation_error", "Document exceeds the text size limit")
    if not frontmatter:
        return body.encode()
    return ("---\n" + yaml.safe_dump(plain(metadata), allow_unicode=True, sort_keys=False).rstrip()
            + "\n---\n\n" + body).encode()


def slugify(title):
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64].rstrip("-")
    return value if value and value[0].isalpha() else "note-" + (value[:59] or "untitled")
