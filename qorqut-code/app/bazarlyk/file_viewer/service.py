"""The owner's file viewer: one resolver for metadata, preview, download and saved history.

A file is named by an operations-root-relative ``path`` (always its current
contents) or by a registered ``document_id`` (current contents at its present
location, or one saved version). Only the installation owner reaches this (the
HTTP route checks that first); agents never do. Nothing here writes: viewing
records no history and creates no document ID.
"""

from __future__ import annotations

from pathlib import Path
import posixpath
from urllib.parse import quote, urlsplit

from qorqut_operations.contracts import DomainError, InvocationContext, plain
from bazarlyk.composition.locations import document_location, document_path_at, document_path_at_global
from qorqut_operations.document_history.text import BASELINE_AUTHOR, EXTERNAL_AUTHOR

from . import files, previews
from .inline_images import inline_images
from .paths import Selector, normalize_path, viewer_url

API = "/api/root/files"
DIFF_CHARS = 1024 * 1024
HISTORY_LIMITS = {"before_version", "limit", "from_version", "to_version"}
SYSTEM_AUTHORS = {BASELINE_AUTHOR: "History baseline (earlier edits were not recorded)",
                  EXTERNAL_AUTHOR: "Edited outside Qorqut"}


def positive(value, name, *, maximum=2**31 - 1):
    if not isinstance(value, str) or not value.isdecimal() or not 1 <= int(value) <= maximum:
        raise DomainError("validation_error", f"{name} must be a positive integer")
    return int(value)


def parse_history_query(items, roots):
    """``(Selector, history arguments)`` from a history query: a document plus a page or a two-version diff."""
    from .paths import parse_selector
    selected, arguments = [], {}
    for key, value in items:
        if key in HISTORY_LIMITS:
            if key in arguments:
                raise DomainError("validation_error", f"Duplicate field: {key}")
            arguments[key] = positive(value, key, maximum=200 if key == "limit" else 2**31 - 1)
        else:
            selected.append((key, value))
    selector = parse_selector(selected, roots)
    if not selector.is_document or selector.version is not None:
        raise DomainError("validation_error", "History is read for a document_id (with business_id unless Global)")
    diff = {"from_version", "to_version"} & arguments.keys()
    if diff and (len(diff) != 2 or {"before_version", "limit"} & arguments.keys()):
        raise DomainError("validation_error", "A comparison needs from_version and to_version only")
    return selector, arguments


def content_disposition(name):
    """An attachment header whose names cannot carry a quote, separator or line break."""
    fallback = "".join(char if 32 < ord(char) < 127 and char not in '"\\;%/' else "_" for char in name) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def version_name(name, version):
    stem, dot, ext = name.rpartition(".")
    return f"{stem}.v{version}.{ext}" if dot and stem else f"{name}.v{version}"


class FileViewer:
    """Bound to one ``runtime``; ``scope`` maps a business ID (or None for Global) to the owner's invocation context."""

    def __init__(self, runtime, scope):
        self.runtime, self.scope = runtime, scope
        self.root = Path(runtime.root).resolve(strict=True)
        self.roots = tuple(dict.fromkeys((str(self.root), str(runtime.root))))

    # Selection

    def selector(self, items):
        from .paths import parse_selector
        return parse_selector(items, self.roots)

    def _store(self):
        domains = getattr(self.runtime, "domains", None)
        if domains is None:
            raise DomainError("operation_unavailable", "Registered documents are not available in this configuration")
        return domains.documents.store

    def _paths(self):
        paths = getattr(self.runtime, "paths", None)
        if paths is None:
            raise DomainError("invalid_configuration", "The file viewer requires the explicit instance paths")
        return paths

    def location(self, row, uow):
        """The registered document's present root-relative location."""
        if row["business_id"] is None:
            return normalize_path(document_location(self._paths(), None, row["path"]), ())
        business = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (row["business_id"],)).fetchone()
        if business is None:
            raise DomainError("forbidden", "The registered document is outside the operations root")
        return normalize_path(document_location(self._paths(), business, row["path"]), ())

    def document(self, selector):
        """``(row, scope context, location)`` for a document selector, through the existing document rules."""
        ctx = self.scope(selector.business_id)
        with self.runtime.database.read() as uow:
            row = self._store().get(ctx, selector.document_id, uow)
            if row["business_id"] != selector.business_id:
                # A link never re-scopes a document: a business ID must be the owner's, and Global means Global.
                raise DomainError("not_found", "Document not found")
            return row, ctx, self.location(row, uow)

    def managed_for_path(self, relative):
        """The one registered document at this location, or None (none, or more than one)."""
        paths = self._paths()
        with self.runtime.database.read() as uow:
            connection = uow.connection
            candidates = [(None, document_path_at_global(paths, relative))]
            for business in connection.execute("SELECT id, slug FROM businesses"):
                candidates.append((business["id"], document_path_at(paths, business, relative)))
            found = [row for business_id, path in candidates if path is not None for row in connection.execute(
                "SELECT id, business_id, kind, revision FROM documents WHERE business_id IS ? AND path = ?",
                (business_id, path))]
        return dict(found[0]) if len(found) == 1 else None

    # History through the existing owner-only operation

    def read_history(self, ctx, document_id, **arguments):
        result = self.runtime.application.invoke(
            ctx, "document_history_get", {"source_kind": "document", "source_id": document_id, **arguments})
        if result.status != "ok":
            error = result.error or {}
            raise DomainError(error.get("code", "operation_failed"), error.get("message", "History is unavailable"))
        return plain(result.data)

    def _history_available(self, ctx, document_id):
        try:
            return bool(self.read_history(ctx, document_id, limit=1)["versions"])
        except DomainError:
            return False

    def _author_names(self, identities):
        names = {identity: SYSTEM_AUTHORS.get(identity, identity) for identity in identities}
        people = [identity for identity in identities if not identity.startswith("system:")]
        if people:
            with self.runtime.database.read() as uow:
                marks = ",".join("?" for _ in people)
                names.update({row["id"]: row["display_name"] for row in uow.connection.execute(
                    f"SELECT id, display_name FROM principals WHERE id IN ({marks})", people)})
        return names

    def _version(self, item, names):
        return {**item, "author_name": names.get(item["author_id"], item["author_id"])}

    def history(self, selector, arguments):
        """Saved versions (a page of metadata) or a unified diff of two, for one registered document."""
        row, ctx, location = self.document(selector)
        data = self.read_history(ctx, row["id"], **arguments)
        result = {"document_id": row["id"], "business_id": row["business_id"], "current_revision": data["current_revision"]}
        if "diff" in data:
            diff = data["diff"]
            return {**result, "from_version": data["from_version"], "to_version": data["to_version"],
                    "diff": diff[:DIFF_CHARS], "diff_truncated": len(diff) > DIFF_CHARS}
        names = self._author_names({item["author_id"] for item in data["versions"]})
        versions = [self._version(item, names) for item in data["versions"]]
        current = None
        if "before_version" not in arguments:
            current = self._current_hash(location)
        return {**result, "versions": versions, "next_before_version": data["next_before_version"],
                "current_content_sha256": current,
                "current_matches_latest": (None if current is None or not versions
                                           else current == versions[0]["content_sha256"])}

    def _current_hash(self, location):
        try:
            opened = files.open_regular(self.root, location)
        except DomainError:
            return None
        try:
            return opened.sha256(previews.PREVIEW_BYTES)
        finally:
            opened.close()

    def _saved_version(self, ctx, document_id, number):
        """The one saved version's metadata (no content)."""
        data = self.read_history(ctx, document_id, before_version=number + 1, limit=1)
        if not data["versions"] or data["versions"][0]["history_version"] != number:
            raise DomainError("not_found", "No saved version with this number")
        item = data["versions"][0]
        return self._version(item, self._author_names({item["author_id"]}))

    # Metadata, content, download

    def metadata(self, selector):
        if selector.is_document:
            row, ctx, location = self.document(selector)
            managed = self._managed(row, ctx, selector.business_id)
        else:
            row, ctx, location = None, None, selector.path
            found = self.managed_for_path(location)
            managed = None
            if found:
                try:
                    managed = self._managed(found, self.scope(found["business_id"]), found["business_id"])
                except DomainError:
                    managed = None  # An archived or unavailable business: show the plain current file.
        name = location.rsplit("/", 1)[-1]
        base = {"name": name, "path": location, "managed": managed}
        if selector.version is not None:
            saved = self._saved_version(ctx, row["id"], selector.version)
            return {**base, "current": False, "version": saved, "size": None, "modified_at": saved["created_at"],
                    "preview_kind": previews.kind_for(name), "binary": False,
                    "urls": self._urls(selector, managed, name)}
        opened = files.open_regular(self.root, location)
        try:
            head, _ = opened.read_preview(previews.SNIFF_BYTES)
            kind = previews.sniff(name, head)
            return {**base, "current": True, "version": None, "size": opened.size, "modified_at": opened.modified_at,
                    "preview_kind": kind, "binary": kind == "binary", "preview_limit": previews.PREVIEW_BYTES,
                    "urls": self._urls(selector, managed, name)}
        finally:
            opened.close()

    def _managed(self, row, ctx, business_id):
        available = self._history_available(ctx, row["id"])
        business = None
        if business_id:
            with self.runtime.database.read() as uow:
                business = uow.connection.execute("SELECT slug, name FROM businesses WHERE id=?", (business_id,)).fetchone()
        return {"document_id": row["id"], "business_id": business_id, "scope": "business" if business_id else "global",
                "business_name": business["name"] if business else None, "kind": row["kind"],
                "revision": row["revision"], "history_available": available,
                "stable_url": viewer_url(document_id=row["id"], business_id=business_id) if available else None}

    def _urls(self, selector, managed, name):
        query = urlsplit(selector.url()).query
        result = {"view": selector.url(), "content": f"{API}/content?{query}", "download": f"{API}/download?{query}",
                  "copy": selector.url(), "image": f"{API}/image?{query}"}
        if managed and managed["history_available"] and not selector.is_document:
            result["copy"] = managed["stable_url"]
        if selector.version is not None:
            result["current"] = viewer_url(document_id=selector.document_id, business_id=selector.business_id)
        return result

    def base_dir(self, location):
        return posixpath.dirname(location)

    def content(self, selector):
        if selector.version is not None:
            row, ctx, location = self.document(selector)
            saved = self.read_history(ctx, row["id"], version=selector.version)["version"]
            data = saved["content"].encode("utf-8")
            # A saved version never picks up the sibling images of today's file; their references are only counted.
            return previews.build_preview(location.rsplit("/", 1)[-1], data[:previews.PREVIEW_BYTES],
                                          truncated=len(data) > previews.PREVIEW_BYTES, size=len(data),
                                          base_dir=self.base_dir(location),
                                          embed_images=lambda text: self._embed_images(text, location, historical=True))
        location = self.document(selector)[2] if selector.is_document else selector.path
        opened = files.open_regular(self.root, location)
        try:
            data, truncated = opened.read_preview(previews.PREVIEW_BYTES)
            return previews.build_preview(opened.name, data, truncated=truncated, size=opened.size,
                                          base_dir=self.base_dir(location),
                                          embed_images=lambda text: self._embed_images(text, location))
        finally:
            opened.close()

    def _embed_images(self, text, location, *, historical=False):
        rendered, counts = inline_images(text, self.base_dir(location), None if historical else lambda path: files.open_regular(self.root, path))
        return rendered, {**counts, "historical": historical}

    def download(self, selector):
        """``(filename, size, opened file or bytes)``: the open file to stream, or a saved version's exact text."""
        if selector.version is not None:
            row, ctx, location = self.document(selector)
            saved = self.read_history(ctx, row["id"], version=selector.version)["version"]
            data = saved["content"].encode("utf-8")
            return version_name(location.rsplit("/", 1)[-1], selector.version), len(data), data
        location = self.document(selector)[2] if selector.is_document else selector.path
        opened = files.open_regular(self.root, location)
        return opened.name, opened.size, opened

    def image(self, selector):
        """Stream recognized raster bytes through the same owner-only safe resolver."""
        name, size, body = self.download(selector)
        try:
            head = body[:previews.SNIFF_BYTES] if isinstance(body, bytes) else body.read_preview(previews.SNIFF_BYTES)[0]
            mime = previews.image_mime(head)
            if mime is None:
                raise DomainError("validation_error", "This file is not a supported image")
            return mime, size, body
        except BaseException:
            if not isinstance(body, bytes):
                body.close()
            raise
