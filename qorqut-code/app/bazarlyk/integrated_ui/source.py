"""Safe capture of one project's authored UI entry, and discovery of the installed starting templates.

Registration and code activation read the declared entry through the existing descriptor-based file reader
(``bazarlyk.file_viewer.files``): every path component is opened relative to the project's own directory with
``O_NOFOLLOW``, so a symbolic link anywhere below it is refused, and only a regular file is opened at all. The
accepted document is read from that one descriptor, is required to be unchanged for the whole read, must be valid
UTF-8 and within the executable-document limit. The captured text and its digest are stored as an immutable version;
serving later never re-reads the authored file.

Templates are *installed package resources* (``bazarlyk.integrated_ui.templates``); discovery enumerates the package,
never a source checkout or a working-directory fallback. A template that is not installed is simply absent.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from qorqut_operations.contracts import DomainError

from bazarlyk.file_viewer.files import iso, open_regular

# The project directory that holds authored UIs, and the one entry file the host accepts as executable.
DIRECTORY = "qorqut integrated UIs"
ENTRY_FILE = "index.html"
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
MAX_SOURCE_PATH_CHARS = 512

# The currently specified starting points, in discovery order. An entry exists only when its installed directory
# carries an index.html; the title and description are small metadata for the agent copying it. This is deliberately
# not a fixed "five template" promise: the owner removed the Directory template and the CRM template's agreed plan is
# still pending, so neither is advertised here until its requirement is accepted.
TEMPLATES = (
    ("review-queue", "Review queue",
     "Review one item at a time: decision and notes, back/next, editable prior choices, progress and an explicit finish."),
    ("form", "Form",
     "Text, number, choice and checkbox fields with validation, draft saving and an explicit submission."),
    ("report", "Report",
     "Render agent-provided data as useful text, a table or a simple inline chart; no required answer."),
)


def invalid(message: str, **details: Any) -> DomainError:
    return DomainError("invalid_argument", message, details=details or None)


@dataclass(frozen=True)
class Document:
    """One exact accepted executable document: its UTF-8 text, digest and byte length."""

    html: str
    sha256: str
    byte_length: int


def parse_source_path(raw: Any) -> tuple[str, str]:
    """``(slug, normalized project-relative path)`` for ``qorqut integrated UIs/<slug>/index.html``.

    The spelling is strict: exactly that directory, one lowercase slug and the entry file. Absolute paths, ``..``,
    backslashes, control characters and any other depth are refused here, before the file system is touched. Whether
    the file exists, is a symbolic link or changes while it is read is decided by the safe reader.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise invalid("A project-relative UI source path is required")
    if len(raw) > MAX_SOURCE_PATH_CHARS:
        raise invalid("The UI source path is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise invalid("The UI source path cannot contain control characters")
    if raw.startswith("/") or "\\" in raw:
        raise invalid("Give a project-relative POSIX path, not an absolute or platform path")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise invalid("The UI source path cannot be empty, '.' or '..'")
    if len(parts) != 3 or parts[0] != DIRECTORY or parts[2] != ENTRY_FILE:
        raise invalid(f"The UI source must be exactly {DIRECTORY}/<slug>/{ENTRY_FILE}")
    slug = parts[1]
    if not SLUG.fullmatch(slug):
        raise invalid("The UI source directory name must be a lowercase slug")
    return slug, posixpath.join(*parts)


def read_document(opened, *, limit: int) -> Document:
    """The complete accepted document from one already-verified open file.

    The bytes come from the opened descriptor, never from a second path lookup; the file must still have exactly the
    size and modification time it had when it was opened, so a document that changed during capture is refused
    instead of stored half-way. The text must be strict UTF-8.
    """
    if opened.size > limit:
        raise invalid(f"The UI source must be at most {limit} bytes", byte_length=opened.size)
    opened.stream.seek(0)
    data = opened.stream.read(limit + 1)
    info = os.fstat(opened.stream.fileno())
    if len(data) != opened.size or info.st_size != opened.size or iso(info.st_mtime) != opened.modified_at:
        raise invalid("The UI source changed while it was read; retry the capture")
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise invalid("The UI source must be UTF-8 text", offset=exc.start) from exc
    return Document(html=html, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))


def capture(root: Path, relative: str, *, limit: int) -> Document:
    """Read one authored entry beneath the project directory without following a symbolic link."""
    try:
        opened = open_regular(root, relative)
    except OSError as exc:
        raise DomainError("not_found", "The UI source file is unavailable") from exc
    try:
        return read_document(opened, limit=limit)
    finally:
        opened.close()


def templates_root():
    """The installed template package directory as a traversable resource."""
    return resources.files("bazarlyk.integrated_ui").joinpath("templates")


def discover_templates(root=None) -> list[dict[str, str]]:
    """Every installed template with a usable absolute directory and entry file for an agent to copy.

    A resource that is not an ordinary installed directory (for example a zipped package) cannot be copied by an
    ordinary file capability, so it is reported as unavailable rather than as a broken path.
    """
    package = templates_root() if root is None else root
    try:
        base = Path(os.fspath(package))
    except TypeError as exc:
        raise DomainError("operation_unavailable", "Installed UI templates are not available as files") from exc
    if not base.is_dir():
        return []
    items = []
    for identity, title, description in TEMPLATES:
        directory, entry = base / identity, base / identity / ENTRY_FILE
        if not entry.is_file():
            continue
        items.append({"id": identity, "title": title, "description": description,
                      "source_directory": str(directory.resolve(strict=True)),
                      "entry_file": str(entry.resolve(strict=True))})
    return items