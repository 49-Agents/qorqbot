"""Authorized file sources for outbound Telegram files and photos.

``FileSources`` answers one question for the later send tool: may this exact active Global agent read this one file,
and from where? It names no bytes and sends nothing. A source is one of two closed shapes:

``{"type": "workspace", "path": "exports/report.pdf"}``
    a relative POSIX path below the caller's own registered workspace, in ``exports/`` or ``downloads/`` only;
``{"type": "document", "document_id": "<uuid>", "business_id": "<uuid>" | null}``
    one registered document in an explicit scope (``null`` is Global), of a kind with a canonical reader:
    ``asset`` (``assets_get``), ``data_upload`` (``data_uploads_get``), ``agent_note`` and the business-owned note
    (``notes_get``), and ``content`` and ``context_note`` (``content_get``). Every other kind is refused.

``authorize`` derives the current principal and scope, applies the canonical reader's own operation authorization and the
document store's per-kind visibility, and returns a frozen ``AuthorizedSource``: a trusted root, a root-relative path, a
validated file name and a JSON ``binding`` of the facts that made it authorized. It reads no file. ``recheck`` re-derives
that authority from a stored binding (at apply and again before dispatch) without needing the file, its current content or
its revision. ``open_source`` opens the one regular file without following any symlink and yields an ``OpenedSource`` whose
``copy_to`` is the bounded, hashed copy the snapshot store seals.

Errors are ``DomainError`` codes. Engine decisions pass through unchanged (``forbidden``, ``not_found``, ``policy_denied``,
``busy`` for a document still being applied, ``invalid_principal``, ``inactive_agent``, ``invalid_configuration``). This module
adds: ``validation_error`` (malformed source, path or digest; ``details.field`` names it), ``source_unsupported`` (a kind or
reader outside the allowlist, or a reader that does not exist in that scope), ``source_archived``, ``source_changed`` (the
binding or the file no longer matches what was authorized), ``source_hardlinked`` (a workspace file with more than one link)
and, from the opener, ``symlink_not_followed``, ``not_a_regular_file`` and ``file_unreadable``. ``copy_to`` adds
``file_empty``, ``file_too_large`` and ``checksum_mismatch``.

Same-user application policy is not an operating-system sandbox: these checks bound what this application will read for an
agent, not what the service user could read.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import errno
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
import unicodedata

from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError

from bazarlyk.agents.global_scope import reaches_business
from bazarlyk.composition import locations
from qorqut_operations.documents.business_notes import KIND as BUSINESS_NOTE
from bazarlyk.file_viewer import files
from .media_validation import BIDI_CONTROLS, invalid, validate_filename, validate_sha256

BINDING_VERSION = 1
MAX_PATH_CHARS = 1024
WORKSPACE_SUBTREES = ("exports", "downloads")
# Internal files the native upload receipts keep beside a download; hidden names are refused anyway.
RESERVED_NAMES = frozenset({".receipt.json", ".upload.lock"})
CHUNK = 64 * 1024

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UUID = re.compile(UUID_PATTERN)
_UUID_SCHEMA = {"type": "string", "pattern": UUID_PATTERN, "minLength": 36, "maxLength": 36}
SOURCE_SCHEMA = {
    "description": "Exactly one file source. A workspace file is a relative path below your own workspace and only in "
                   "exports/ or downloads/. A document is a registered document ID with its business_id (null for Global).",
    "oneOf": [
        schema({"type": {"const": "workspace"}, "path": {"type": "string", "minLength": 1, "maxLength": MAX_PATH_CHARS}},
               ("type", "path")),
        schema({"type": {"const": "document"}, "document_id": _UUID_SCHEMA,
                "business_id": {"anyOf": [_UUID_SCHEMA, {"type": "null"}]}}, ("type", "document_id", "business_id")),
    ],
}
EXPECTED_SHA256_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$", "minLength": 64, "maxLength": 64,
                          "description": "The lowercase hexadecimal SHA-256 of the exact file bytes you expect to send."}

# Registered kind -> its canonical reader. A kind outside this table is never sent, whatever else may read it.
READERS = {"asset": "assets_get", "data_upload": "data_uploads_get", "agent_note": "notes_get", BUSINESS_NOTE: "notes_get",
           "content": "content_get", "context_note": "content_get"}
_READER_ARGUMENT = {"assets_get": "asset_id", "data_uploads_get": "upload_id", "notes_get": "document_id",
                    "content_get": "document_id"}
# Bidirectional controls and line separators are not ordinary path text either (see ``validate_filename``).
_HIDDEN_PATH_CATEGORIES = {"Cc", "Cs", "Zl", "Zp"}
_WORKSPACE_KEYS = {"version", "type", "principal_id", "scope", "workspace", "workspace_root", "path"}
_DOCUMENT_KEYS = {"version", "type", "principal_id", "scope", "document_id", "document_kind", "reader"}


def _changed(message="The authorized source no longer matches its binding"):
    return DomainError("source_changed", message)


# ----- lexical validation ------------------------------------------------------------------------------------------


def validate_expected_sha256(value):
    """The lowercase 64-hex digest unchanged, or ``validation_error`` naming ``expected_sha256``."""
    return validate_sha256(value, "expected_sha256")


def _canonical_absolute(value):
    """``value`` when it is an absolute POSIX path with no empty, ``.``, ``..`` or NUL component, else ``None``."""
    if not isinstance(value, str) or "\0" in value or not value.startswith("/") or value.startswith("//"):
        return None
    parts = PurePosixPath(value).parts
    if len(parts) < 2 or str(PurePosixPath(value)) != value or any(part in {".", ".."} for part in parts):
        return None
    return value


def _closed(value, keys, field):
    if not isinstance(value, Mapping):
        raise invalid(f"{field} must be an object", field)
    if set(value) != keys:
        extra, missing = sorted(set(value) - keys), sorted(keys - set(value))
        raise invalid(f"{field} takes exactly {', '.join(sorted(keys))}"
                      + (f"; unexpected: {', '.join(str(key)[:40] for key in extra)}" if extra else "")
                      + (f"; missing: {', '.join(missing)}" if missing else ""), field)


def normalize_workspace_path(path):
    """``path`` unchanged when it is a lexically valid file path for a workspace source, else ``validation_error``.

    Relative, POSIX, at least two segments, the first ``exports`` or ``downloads``; no empty, ``.`` or ``..`` segment, no
    backslash, control or separator character, no hidden (dot-leading) segment or internal receipt name, at most 255 UTF-8
    bytes per segment and 1024 characters in all, and a final file name that is a valid upload file name with no leading or
    trailing whitespace and no bidirectional override or isolate (stricter than the upload file name contract alone). Nothing
    is normalized or resolved, and nothing here touches the filesystem."""
    if not isinstance(path, str) or not path:
        raise invalid("A workspace path is required", "source.path")
    if len(path) > MAX_PATH_CHARS:
        raise invalid("The workspace path is too long", "source.path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise invalid("The workspace path must be valid Unicode text", "source.path") from None
    if path.startswith("/") or "\\" in path:
        raise invalid("Give a relative POSIX path below your workspace", "source.path")
    if any(unicodedata.category(char) in _HIDDEN_PATH_CATEGORIES for char in path):
        raise invalid("The workspace path contains a control character", "source.path")
    segments = path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise invalid("The workspace path may not contain empty, '.' or '..' segments", "source.path")
    if any(segment.startswith(".") or segment in RESERVED_NAMES for segment in segments):
        raise invalid("The workspace path may not name a hidden or internal file", "source.path")
    if any(len(segment.encode("utf-8")) > 255 for segment in segments):
        raise invalid("A workspace path segment exceeds 255 UTF-8 bytes", "source.path")
    if len(segments) < 2 or segments[0] not in WORKSPACE_SUBTREES:
        raise invalid("Only files below exports/ and downloads/ of your workspace can be sent", "source.path")
    name = segments[-1]
    if name != name.strip() or any(char in BIDI_CONTROLS for char in name):
        raise invalid("The workspace file name may not have surrounding whitespace or bidirectional controls", "source.path")
    validate_filename(name)
    return path


def parse_source(value):
    """The normalized, closed source: ``{"type": "workspace", "path"}`` or ``{"type": "document", "document_id", "business_id"}``.

    Unknown or missing keys, other types, non-canonical (non lowercase) UUIDs and unsafe paths are ``validation_error``."""
    if not isinstance(value, Mapping):
        raise invalid("source must be an object", "source")
    kind = value.get("type")
    if kind == "workspace":
        _closed(value, {"type", "path"}, "source")
        return {"type": "workspace", "path": normalize_workspace_path(value["path"])}
    if kind == "document":
        _closed(value, {"type", "document_id", "business_id"}, "source")
        document_id, business_id = value["document_id"], value["business_id"]
        if not isinstance(document_id, str) or not _UUID.fullmatch(document_id):
            raise invalid("document_id must be a lowercase UUID", "source.document_id")
        if business_id is not None and (not isinstance(business_id, str) or not _UUID.fullmatch(business_id)):
            raise invalid("business_id must be a lowercase UUID, or null for Global", "source.business_id")
        return {"type": "document", "document_id": document_id, "business_id": business_id}
    raise invalid("source.type must be workspace or document", "source.type")


def _registered_segments(path):
    """The segments of a registered document path, refusing anything that could leave its root."""
    segments = path.split("/") if isinstance(path, str) else []
    if (not segments or any(not segment or segment in {".", ".."} or "\0" in segment for segment in segments)
            or path.startswith("/") or "\\" in path):
        raise DomainError("invalid_configuration", "The registered document location is unsafe")
    return segments


# ----- results -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class AuthorizedSource:
    """One accepted source. ``root`` and ``relative_path`` locate the file; ``binding`` (plain JSON data) records the
    facts that authorized it and is what ``FileSources.recheck`` re-derives. Only ``FileSources`` creates one."""

    kind: str
    root: str
    relative_path: str
    filename: str
    binding: dict


@dataclass(frozen=True)
class SourceCopy:
    size: int
    sha256: str


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def _describe(mode):
    return ("a directory" if stat.S_ISDIR(mode) else "a device, pipe or socket"
            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) else "not a regular file")


def _lstat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        raise files.missing() from None
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise files.missing() from None
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise files.unreadable() from None
        raise


def _open_component(parent, name, flags):
    """``name`` opened relative to the ``parent`` descriptor; a symlink is refused, never followed."""
    try:
        return os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise files.unreadable() from None
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            # Either a symlink (refused) or a non-directory where a directory is needed (no such path).
            if stat.S_ISLNK(_lstat(parent, name).st_mode):
                raise files.symlink() from None
            raise files.missing() from None
        if exc.errno in {errno.ENOENT, errno.ENAMETOOLONG}:
            raise files.missing() from None
        raise


def _open_anchored(root, relative_path):
    """``(stream, fstat)`` of the regular file at ``relative_path`` below the absolute, canonical ``root``.

    The walk starts at the filesystem root directory and opens every component, the absolute ones of ``root`` first and then
    the relative ones, relative to the descriptor of its parent with ``O_DIRECTORY|O_NOFOLLOW``. A name swapped for a symlink
    before it is opened is refused; one swapped after its parent was opened cannot move the directory already held, so the
    file is always found below the directory this walk anchored. The final entry is checked without following it, opened
    non-blocking without becoming a controlling terminal, and checked again on its descriptor. Every descriptor but the
    returned stream's is closed before returning or raising. Nothing is read."""
    if _canonical_absolute(root) is None:
        raise DomainError("invalid_configuration", "The source root must be a canonical absolute path")
    names = [*PurePosixPath(root).parts[1:], *_registered_segments(relative_path)]
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in names[:-1]:
            child = _open_component(parent, name, _DIRECTORY)
            previous, parent = parent, child
            os.close(previous)
        info = _lstat(parent, names[-1])
        if stat.S_ISLNK(info.st_mode):
            raise files.symlink()
        if not stat.S_ISREG(info.st_mode):
            raise files.not_regular(_describe(info.st_mode))
        descriptor = _open_component(parent, names[-1], _FILE)
    finally:
        os.close(parent)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise files.not_regular(_describe(opened.st_mode))
        return os.fdopen(descriptor, "rb"), opened
    except BaseException:
        os.close(descriptor)
        raise


class OpenedSource:
    """One securely opened regular file. Read it only through ``copy_to`` (or ``stream`` for a bounded peek)."""

    def __init__(self, stream, filename, info):
        self.stream, self.filename = stream, filename
        self.size = info.st_size
        self._signature = _signature(info)

    def _unchanged(self):
        return _signature(os.fstat(self.stream.fileno())) == self._signature

    def assert_unchanged(self):
        """Raise ``source_changed`` unless the opened descriptor still shows the identity, size and times (change time
        included) it had when opened. A caller that reads ``stream`` itself (the snapshot store's binary-stream copy) calls this
        after the last byte, so an ordinary edit during the copy is never sealed, exactly as ``copy_to`` guarantees."""
        if not self._unchanged():
            raise _changed("The file changed while it was being copied")

    def copy_to(self, sink, *, limit, expected_sha256=None, chunk_size=CHUNK):
        """Copy the whole file to ``sink.write`` in bounded chunks and return ``SourceCopy(size, sha256)``.

        Refuses before reading when the opened size is empty or above ``limit``. It never reads more than the opened size plus
        one byte, which is at most ``limit + 1``, and never passes more than ``limit`` bytes to ``sink``: a file that grows past
        the size it had when opened is ``source_changed``. Afterwards the opened descriptor must still show the size, times and
        identity it had when opened (``source_changed`` otherwise, so an ordinary edit during the copy is never sealed), and
        the digest must equal ``expected_sha256`` when one is given (``checksum_mismatch``). The caller owns ``sink`` and
        discards what it received after any error."""
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive byte count")
        expected = None if expected_sha256 is None else validate_expected_sha256(expected_sha256)
        if self.size < 1:
            raise DomainError("file_empty", "The file is empty")
        if self.size > limit:
            raise DomainError("file_too_large", f"The file exceeds the {limit:,}-byte limit", details={"limit": limit})
        self.stream.seek(0)
        digest, total = hashlib.sha256(), 0
        while True:
            chunk = self.stream.read(min(chunk_size, self.size + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > self.size:
                raise _changed("The file grew while it was being copied")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = sink.write(view)
                if written is None:
                    break
                if written < 1:
                    raise OSError("The copy destination accepted no bytes")
                view = view[written:]
        if total != self.size or not self._unchanged():
            raise _changed("The file changed while it was being copied")
        actual = digest.hexdigest()
        if expected is not None and actual != expected:
            raise DomainError("checksum_mismatch", "The file does not match expected_sha256",
                              details={"expected_sha256": expected, "actual_sha256": actual})
        return SourceCopy(total, actual)


# ----- the authorizer ----------------------------------------------------------------------------------------------


class FileSources:
    """Source authority bound to one runtime (its application, document store and explicit instance paths)."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.app = runtime.application

    @property
    def paths(self):
        paths = getattr(self.runtime, "paths", None)
        if paths is None:
            raise DomainError("invalid_configuration", "File sources require the explicit instance paths")
        return paths

    @property
    def store(self):
        domains = getattr(self.runtime, "domains", None)
        if domains is None:
            raise DomainError("operation_unavailable", "Registered documents are not available in this configuration")
        return domains.documents.store

    # Public contract

    def authorize(self, ctx, source, uow):
        """The ``AuthorizedSource`` for ``source`` as the current ``ctx`` principal, or a ``DomainError``. Reads no file."""
        source = parse_source(source)
        ctx = self._caller(ctx, uow)
        if source["type"] == "workspace":
            return self._workspace(ctx, source["path"], uow)
        return self._document(ctx, source, uow)

    def recheck(self, ctx, binding, uow):
        """Re-derive the authority a stored ``binding`` recorded, and return the freshly authorized source.

        The same principal must still be an active Global agent, and the source must still authorize to exactly the same
        facts: the same workspace location, or the same registered document in the same scope and kind, not archived, with its
        canonical reader still permitted. The file, its bytes and the document's revision are not consulted, so a removed
        generated file or an unrelated edit does not matter. ``source_changed`` reports a binding that no longer matches."""
        binding = self._binding(binding)
        ctx = self._caller(ctx, uow)
        if binding["principal_id"] != ctx.principal_id:
            raise _changed("The source was authorized for a different principal")
        if binding["type"] == "workspace":
            fresh = self._workspace(ctx, binding["path"], uow)
        else:
            fresh = self._document(ctx, {"type": "document", "document_id": binding["document_id"],
                                         "business_id": binding["scope"].get("business_id")}, uow)
        if fresh.binding != binding:
            raise _changed()
        return fresh

    @contextmanager
    def open_source(self, authorized):
        """A context manager yielding the ``OpenedSource`` of ``authorized``; the descriptor closes on exit.

        The file is reached by one descriptor-anchored walk from the filesystem root through the trusted root and then the
        relative path, every component opened without following a symlink (a component swapped for a link after validation
        is refused or leaves the walk on the directory it already holds, never in another tree). The final file must be
        regular, and a workspace file must have exactly one link. Nothing is read."""
        if not isinstance(authorized, AuthorizedSource):
            raise TypeError("open_source needs an AuthorizedSource")
        stream, info = _open_anchored(authorized.root, authorized.relative_path)
        try:
            if authorized.kind == "workspace" and info.st_nlink != 1:
                raise DomainError("source_hardlinked", "A workspace file with more than one hard link cannot be sent")
            source = OpenedSource(stream, authorized.filename, info)
        except BaseException:
            stream.close()
            raise
        try:
            yield source
        finally:
            stream.close()

    # Principal and binding

    def _caller(self, ctx, uow):
        """The current context of an active Global agent acting as itself; never an owner or a delegated identity."""
        ctx = self.app.current_context(ctx, uow)
        if ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "File sources are limited to the active Global agents themselves")
        return ctx

    @staticmethod
    def _binding(value):
        """A stored binding as a plain dict, or ``validation_error`` when it is not one this module produced."""
        if not isinstance(value, Mapping) or value.get("version") != BINDING_VERSION:
            raise invalid("The source binding is not recognized", "binding")
        kind = value.get("type")
        keys = _WORKSPACE_KEYS if kind == "workspace" else _DOCUMENT_KEYS if kind == "document" else None
        if keys is None or set(value) != keys:
            raise invalid("The source binding is not recognized", "binding")
        binding = {key: (dict(item) if isinstance(item, Mapping) else item) for key, item in value.items()}
        scope = binding["scope"]
        if (not isinstance(binding["principal_id"], str) or not isinstance(scope, dict)
                or scope.get("kind") not in {"global", "business"}
                or set(scope) != ({"kind"} if scope.get("kind") == "global" else {"kind", "business_id"})
                or (scope["kind"] == "business"
                    and not (isinstance(scope["business_id"], str) and _UUID.fullmatch(scope["business_id"])))):
            raise invalid("The source binding is not recognized", "binding")
        if kind == "workspace":
            normalize_workspace_path(binding["path"])
            if not isinstance(binding["workspace"], str) or _canonical_absolute(binding["workspace_root"]) is None:
                raise invalid("The source binding is not recognized", "binding")
        elif not isinstance(binding["document_id"], str) or not _UUID.fullmatch(binding["document_id"]):
            raise invalid("The source binding is not recognized", "binding")
        return binding

    # Workspace sources

    def _workspace(self, ctx, path, uow):
        paths = self.paths
        workspace = locations.agent_workspace(paths, ctx.agent_id)
        folder = locations.relative_to_operations(paths, workspace)
        self._refuse_registered_document(f"{folder}/{path}", uow)
        # The folder is the identity; the canonical absolute root is where this installation keeps it, so the same folder
        # under another instance root is a different workspace. Internal metadata: never shown to the agent or the owner.
        binding = {"version": BINDING_VERSION, "type": "workspace", "principal_id": ctx.principal_id,
                   "scope": {"kind": "global"}, "workspace": folder, "workspace_root": str(workspace), "path": path}
        return AuthorizedSource("workspace", str(workspace), path, validate_filename(path.rsplit("/", 1)[-1]), binding)

    def _refuse_registered_document(self, location, uow):
        """A path that is the file of a registered document is sent as that document, under its own read rules."""
        paths, connection = self.paths, uow.connection
        candidates = [(None, locations.document_path_at_global(paths, location))]
        candidates += [(row["id"], locations.document_path_at(paths, row, location))
                       for row in connection.execute("SELECT id, slug FROM businesses")]
        for business_id, path in candidates:
            if path is not None and connection.execute(
                    "SELECT 1 FROM documents WHERE business_id IS ? AND path = ?", (business_id, path)).fetchone():
                raise DomainError("forbidden", "This file is a registered document; send it by its document_id so its "
                                  "read rules apply")

    # Document sources

    def _scope(self, ctx, business_id, uow):
        """``ctx`` selected on one explicit scope, as the engine selects it for a read: Global, or one reachable active business."""
        if business_id is None:
            return replace(ctx, business_id=None, review_business_id=None)
        if business_id not in ctx.allowed_business_ids or not reaches_business(uow.connection, ctx.agent_id, business_id):
            raise DomainError("not_found", "The business is unavailable")
        return replace(ctx.select_business(business_id), review_business_id=business_id)

    def _reader(self, name, business_id):
        """The canonical reader's descriptor, refused when it is absent or has no such scope."""
        try:
            descriptor = self.app.registry.get(name)
        except DomainError:
            raise DomainError("source_unsupported", f"The {name} reader is not available") from None
        kinds = descriptor.scope_kinds
        if (business_id is None and not kinds & {"global", "agent_workspace"}) or (
                business_id is not None and "business" not in kinds):
            raise DomainError("source_unsupported", f"The {name} reader is not available in this scope")
        return descriptor

    def _document(self, ctx, source, uow):
        document_id, business_id = source["document_id"], source["business_id"]
        scoped = self._scope(ctx, business_id, uow)
        row = self.store.get(scoped, document_id, uow)
        if row["business_id"] != business_id:
            # A reference never re-scopes a document: the business must be the document's own, and null means Global.
            raise DomainError("not_found", "Document not found")
        kind = row["kind"]
        reader = READERS.get(kind)
        if reader is None:
            raise DomainError("source_unsupported", f"A {kind} document cannot be sent as a Telegram file")
        if row["archived"]:
            raise DomainError("source_archived", "The document is archived")
        descriptor = self._reader(reader, business_id)
        arguments = {_READER_ARGUMENT[reader]: document_id}
        self.app.authorize(scoped, descriptor, (), uow, namespace=self.app.namespace(scoped, descriptor, arguments))
        business = None if business_id is None else uow.repository("businesses").get(business_id, required=True)
        root = locations.document_root(self.paths, business, row["path"])
        _registered_segments(row["path"])
        binding = {"version": BINDING_VERSION, "type": "document", "principal_id": ctx.principal_id,
                   "scope": {"kind": "global"} if business_id is None else {"kind": "business", "business_id": business_id},
                   "document_id": document_id, "document_kind": kind, "reader": reader}
        return AuthorizedSource("document", str(root), row["path"], self._document_filename(row), binding)

    @staticmethod
    def _document_filename(row):
        """The document's own file name when it has a usable one, else the basename of its registered path."""
        declared = (row["metadata_json"] or {}).get("filename")
        for candidate in (declared, PurePosixPath(row["path"]).name):
            if isinstance(candidate, str):
                try:
                    return validate_filename(candidate)
                except DomainError:
                    continue
        raise DomainError("validation_error", "The document has no file name that can be sent", details={"field": "filename"})
