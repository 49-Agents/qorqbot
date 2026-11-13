"""Lexical path rules, strict viewer selectors and link construction.

Nothing here touches the filesystem. ``files_link`` (an agent-callable helper)
uses these rules only, so an agent can name a file it already knows without
gaining any read access; the owner's viewer (``service.py`` and ``files.py``)
applies the same rules and then opens the file securely.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote

from qorqut_operations.contracts import DomainError

VIEWER_PATH = "/files/view"
MAX_PATH_CHARS = 4096
MAX_LABEL_CHARS = 200

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VERSION = re.compile(r"[1-9][0-9]{0,8}")
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
# A decoded path that still carries an escape for a separator, dot, backslash or NUL
# was encoded twice; a link builder never produces one.
_DOUBLE_ENCODED = re.compile(r"%(?:2e|2f|5c|00)", re.IGNORECASE)
_BAD_ESCAPE = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_LABEL_SPECIALS = re.compile(r"([\\\[\]`*_<>])")
SELECTOR_KEYS = frozenset({"path", "document_id", "business_id", "version"})


def invalid(message):
    return DomainError("validation_error", message)


def root_parts(root):
    return tuple(part for part in str(root).split("/") if part)


def normalize_path(value, roots, *, directory=False):
    """An operations-root-relative POSIX path from an absolute path under ``roots`` or a relative path.

    ``roots`` are the accepted spellings of this instance's operations root, so a relative path keeps its first
    segment (``projects/<slug>/hq/...`` or ``agents/<agent>/...``). The
    result has no empty, ``.`` or ``..`` segments; traversal is rejected, never
    resolved. With ``directory=True``, empty selects the root and trailing
    slashes are accepted. Whether the path exists, or is a symlink, is not decided here.
    """
    if not isinstance(value, str) or (not value.strip() and not (directory and value == "")):
        raise invalid("A file path is required")
    if len(value) > MAX_PATH_CHARS:
        raise invalid("The file path is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise invalid("The file path contains a control character")
    if _DOUBLE_ENCODED.search(value):
        raise invalid("The file path contains a doubly encoded separator, dot or NUL")
    if _URL_SCHEME.match(value):
        raise invalid("Give an absolute path under the operations root or a path relative to it, not a URL")
    parts = value.split("/")
    if ".." in parts:
        raise invalid("The file path may not contain '..'")
    if value.startswith("/"):
        names = [part for part in parts if part and part != "."]
        for root in roots:
            prefix = list(root_parts(root))
            if names[:len(prefix)] == prefix:
                names = names[len(prefix):]
                break
        else:
            raise invalid("The absolute path is outside the operations root")
    else:
        names = [part for part in parts if part and part != "."]
    if not names and not directory:
        raise invalid("A file path below the operations root is required")
    if value.endswith("/") and not directory:
        raise invalid("A file path, not a directory, is required")
    return "/".join(names)


def reject_bad_escapes(raw_query):
    """Refuse a raw query string holding a malformed percent escape (a stray ``%``)."""
    if _BAD_ESCAPE.search(raw_query or b""):
        raise invalid("The link contains a malformed percent escape")


@dataclass(frozen=True)
class Selector:
    """One file: an operations-root-relative ``path`` or a registered ``document_id`` (optionally one saved ``version``)."""

    path: str | None = None
    document_id: str | None = None
    business_id: str | None = None
    version: int | None = None

    @property
    def is_document(self):
        return self.document_id is not None

    def url(self):
        return viewer_url(path=self.path, document_id=self.document_id, business_id=self.business_id, version=self.version)


def parse_selector(items, roots):
    """Strict viewer selector from ``(key, value)`` query pairs: exactly one of ``path`` or ``document_id``.

    ``business_id`` (absent means Global) and ``version`` belong to ``document_id`` only. Unknown keys, duplicates,
    empty values and any ambiguity are errors.
    """
    values = {}
    for key, value in items:
        if key not in SELECTOR_KEYS:
            raise invalid(f"Unknown link field: {key[:40]}")
        if key in values:
            raise invalid(f"Duplicate link field: {key}")
        if value == "":
            raise invalid(f"Empty link field: {key}")
        values[key] = value
    if ("path" in values) == ("document_id" in values):
        raise invalid("A link names exactly one of path or document_id")
    if "path" in values:
        if "business_id" in values or "version" in values:
            raise invalid("business_id and version apply only to a document_id link")
        return Selector(path=normalize_path(values["path"], roots))
    if not _UUID.fullmatch(values["document_id"]):
        raise invalid("document_id must be a lowercase UUID")
    business = values.get("business_id")
    if business is not None and not _UUID.fullmatch(business):
        raise invalid("business_id must be a lowercase UUID")
    version = None
    if "version" in values:
        if not _VERSION.fullmatch(values["version"]):
            raise invalid("version must be a positive integer")
        version = int(values["version"])
    return Selector(document_id=values["document_id"], business_id=business, version=version)


def viewer_url(*, path=None, document_id=None, business_id=None, version=None):
    """The root-relative viewer link; it resolves against whichever origin the owner opens it on."""
    if path is not None:
        return f"{VIEWER_PATH}?path={quote(path, safe='/')}"
    query = f"document_id={document_id}"
    if business_id:
        query += f"&business_id={business_id}"
    if version:
        query += f"&version={int(version)}"
    return f"{VIEWER_PATH}?{query}"


def escape_label(label):
    """Markdown-safe single-line link text."""
    text = " ".join(str(label).split())[:MAX_LABEL_CHARS] or "file"
    return _LABEL_SPECIALS.sub(r"\\\1", text)


def markdown_link(label, url):
    return f"[{escape_label(label)}]({url})"
