"""The one read adapter of the shared published Context.

``SharedContextReader(repository, runtime)`` is inert: it validates two absolute locations and touches nothing.
Every read opens *existing, initialized* state only. A repository that is absent, empty or has no published revision
answers ``not_initialized``; one that is present but unusable (an unsupported format, another runtime's binding, a
symlinked location, a missing ``git``) answers ``unavailable``. Neither is ever repaired, initialized or created here,
and no result is shaped like an empty graph unless an initialized, published graph really is empty.

Reads use the pinned engine's published ``Snapshot`` (pure Git objects at one commit) of format 2 or 3, validated by the
engine's own ``config_version``; a format 2 history simply has no notes. They do not take the engine's writer lock, do
not read the pending draft, run records or index in the Context runtime, and write nothing there; a
reader therefore cannot block or be blocked by the writer, and a draft can never be shown. Search runs the engine's own
``project`` and ``SQLiteIndex`` over the pinned snapshot in a process-private scratch index (disposable, bounded,
removed on ``close``). Every successful response carries the published ``revision`` it was read at, and a paged read
keeps that revision in its cursor, so a publication between pages never reorders or drops what was already promised."""

from __future__ import annotations

from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import weakref

from qorqut_context import Context
from qorqut_context.git_store import config_version
from qorqut_context.schema import ContextError, attributes as normalize_attributes
from qorqut_context.search import SQLiteIndex, project
from qorqut_operations.contracts import DomainError, plain

from . import cursor as cursors
from .graph import KINDS, OBJECT_RELATIONSHIPS, PublishedGraph, ref as make_ref
from .schemas import (CHANGES_LIMIT, EXPAND_LIMIT, FIND_LIMIT, HISTORY_LIMIT, HOME_LIMIT, MAX_SEARCH_WINDOW,
                      TEXT_LIMIT)

REF = re.compile(r"(?:(node|edge|note|source):)?([a-z0-9]{5})")
UNSUPPORTED_FORMAT = "Unsupported context format"
DIRECTIONS = ("out", "in", "both")
SNAPSHOT_CACHE = 4
INDEX_CACHE = 2


class _NotReady(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def dto(self) -> dict:
        return {"status": self.status, "revision": None, "reason": {"code": self.code, "message": self.message}}


def _invalid(field, message):
    return DomainError("validation_error", message, field_errors={field: message})


def _bounded(value, field, spec):
    low, high, default = spec
    if value is None:
        return default
    if type(value) is not int or not low <= value <= high:
        raise _invalid(field, f"{field} must be an integer between {low} and {high}")
    return value


def _page(returned, limit, more, next_cursor, **extra) -> dict:
    return {"limit": limit, "returned": returned, "has_more": more, "next_cursor": next_cursor, **extra}


class SharedContextReader:
    def __init__(self, repository, runtime):
        self._repository, self._runtime = self._absolute(repository, "repository"), self._absolute(runtime, "runtime")
        if self._repository == self._runtime or self._runtime in self._repository.parents or self._repository in self._runtime.parents:
            raise ValueError("The Context repository and runtime must be separate locations")
        self._lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._context = None
        self._common = None
        self._format_head = None
        self._graphs: OrderedDict[str, PublishedGraph] = OrderedDict()
        self._indexes: OrderedDict[str, SQLiteIndex] = OrderedDict()
        self._scratch = None
        self._closed = False

    @staticmethod
    def _absolute(value, label) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError(f"The Context {label} location must be a path")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
            raise ValueError(f"The Context {label} location must be an absolute path without traversal")
        return path

    def close(self):
        with self._lock, self._index_lock:
            self._closed = True
            self._graphs.clear()
            self._indexes.clear()
            if self._scratch is not None:
                shutil.rmtree(self._scratch, ignore_errors=True)
                self._scratch = None

    # ---- opening existing state

    def _bound(self):
        """``(context, head)`` of the initialized published repository; otherwise ``_NotReady``."""
        repository, runtime = self._repository, self._runtime
        for path in (repository, runtime):
            if os.path.realpath(path) != str(path):
                raise _NotReady("unavailable", "path_not_trusted", "The Context location is not a direct, trusted path")
        if not os.path.lexists(repository):
            raise _NotReady("not_initialized", "repository_missing", "The shared Context has not been initialized")
        if not os.path.isdir(repository):
            raise _NotReady("unavailable", "not_a_context_repository", "The Context location is not a repository")
        try:
            entries = os.listdir(repository)
        except OSError:
            raise _NotReady("unavailable", "repository_unreadable", "The Context repository cannot be read") from None
        if not entries:
            raise _NotReady("not_initialized", "repository_empty", "The shared Context has not been initialized")
        if ".git" not in entries:
            raise _NotReady("unavailable", "not_a_context_repository", "The Context location is not a repository")
        try:
            if self._context is None:
                self._context = Context(repository, runtime)
            git = self._context.git
            top = git.run("rev-parse", "--show-toplevel").decode().strip()
            if os.path.realpath(top) != os.path.realpath(repository):
                raise ContextError("not the repository root")
            if not git.run("show-ref", check=False).strip():
                raise _NotReady("not_initialized", "no_published_revision", "The shared Context has no published revision")
            head = git.head()
            self._check_binding(git)
            self._check_format(git, head)
        except _NotReady:
            raise
        except ContextError:
            raise _NotReady("unavailable", "not_a_context_repository", "The Context location is not a usable repository") from None
        except OSError:
            raise _NotReady("unavailable", "git_unavailable", "Git could not be run for the Context repository") from None
        return self._context, head

    @staticmethod
    def _format_code(error) -> str:
        return "unsupported_format" if UNSUPPORTED_FORMAT in str(error) else "invalid_repository"

    def _check_format(self, git, head):
        """The published head must carry a configuration the pinned engine reads, format 2 or 3 (verified once per head)."""
        if self._format_head == head:
            return
        try:
            config_version(git.run("cat-file", "blob", f"{head}:graph/config.json"))
        except ContextError as error:
            raise _NotReady("unavailable", self._format_code(error), "The published Context cannot be read") from None
        self._format_head = head

    def _check_binding(self, git):
        """A repository the engine bound to another runtime is not this instance's Context (a missing marker is fine:
        published Git objects alone are authoritative for reads)."""
        if self._common is None:
            self._common = Path(git.run("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
        marker = self._common / "qorqut-context" / "runtime.json"
        if not os.path.lexists(marker):
            return
        try:
            bound = json.loads(marker.read_bytes())
        except (OSError, ValueError):
            raise _NotReady("unavailable", "runtime_binding_invalid", "The Context runtime binding is unreadable") from None
        if bound != {"runtime_root": str(self._context.runtime)}:
            raise _NotReady("unavailable", "runtime_binding_mismatch", "The Context repository belongs to another runtime")

    def _graph(self, revision=None, field="cursor") -> PublishedGraph:
        """The graph at the captured published head, or at ``revision`` when that is the head or one of its ancestors.

        Ancestry is verified on every call, before any cached graph is reused: a valid but unpublished commit (an orphan,
        or a revision a moved ref no longer reaches) is a validation error, never a ready view."""
        context, head = self._bound()
        target = head
        if revision is not None and revision != head:
            try:
                context.git.run("merge-base", "--is-ancestor", revision, head)
            except ContextError:
                raise _invalid(field, "The pinned revision is not in the published Context history") from None
            target = revision
        with self._lock:
            if self._closed:
                raise _NotReady("unavailable", "reader_closed", "The Context reader is closed")
            graph = self._graphs.get(target)
            if graph is not None:
                self._graphs.move_to_end(target)
                return graph
            try:
                snapshot = context.snapshot(target)
            except (ContextError, KeyError, ValueError, TypeError, OSError) as error:
                raise _NotReady("unavailable", self._format_code(error), "The published Context cannot be read") from None
            graph = PublishedGraph(snapshot)
            self._graphs[snapshot.revision] = graph
            while len(self._graphs) > SNAPSHOT_CACHE:
                self._graphs.popitem(last=False)
            return graph

    @staticmethod
    def _origin(*cursors):
        """The argument a pinned revision came from, for the field an unpublished revision is reported on."""
        return "cursor" if any(cursor is not None for cursor in cursors) else "revision"

    def status(self) -> dict:
        """Readiness without building a snapshot: ``ready`` with the head revision, or why not."""
        try:
            return {"status": "ready", "revision": self._bound()[1], "reason": None}
        except _NotReady as error:
            return error.dto()

    # ---- pinning and references

    @staticmethod
    def _pin(operation, scope, cursor, revision, field="cursor"):
        offset = 0
        if revision is not None and (not isinstance(revision, str) or cursors.REVISION.fullmatch(revision) is None):
            raise _invalid("revision", "revision must be a full published Git revision")
        if cursor is not None:
            pinned, offset = cursors.decode(cursor, operation, scope)
            if revision is not None and revision != pinned:
                raise _invalid(field, "The cursor pins a different revision")
            revision = pinned
        return revision, offset

    @staticmethod
    def _parse_ref(value, field="ref"):
        match = REF.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise _invalid(field, "A Context reference is node:ID, edge:ID, note:ID, source:ID or a bare five-character ID")
        return match.group(1), match.group(2)

    def _resolve(self, graph, value, field="ref", kinds=KINDS):
        kind, object_id = self._parse_ref(value, field)
        located = graph.locate(make_ref(kind, object_id) if kind else object_id)
        if located is None or located[0] not in kinds:
            raise DomainError("not_found", "No such published Context object at this revision")
        return located

    # ---- home

    def home(self, *, limit=None, cursor=None, revision=None) -> dict:
        """A bounded page of published node summaries and attributes with their relationships."""
        limit = _bounded(limit, "limit", HOME_LIMIT)
        pinned, offset = self._pin("context_home", {}, cursor, revision)
        try:
            graph = self._graph(pinned, self._origin(cursor))
        except _NotReady as error:
            return error.dto()
        order = graph.home_order()
        selected = order[offset:offset + limit]
        more = offset + len(selected) < len(order)
        totals = graph.totals()
        return {"status": "ready", "revision": graph.revision, "empty": totals["nodes"] == 0 and totals["edges"] == 0,
                "totals": totals, "nodes": [graph.home_node(node_id) for node_id in selected],
                "page": _page(len(selected), limit, more,
                              cursors.encode("context_home", {}, graph.revision, offset + len(selected)) if more else None)}

    # ---- object, source and history

    def object(self, ref, *, history_limit=None, history_cursor=None, text_limit=None, text_cursor=None,
               revision=None) -> dict:
        """One node, edge, note or source: its record, relationships, subjects or content, support and change history."""
        history_limit = _bounded(history_limit, "history_limit", HISTORY_LIMIT)
        text_limit = _bounded(text_limit, "text_limit", TEXT_LIMIT)
        self._parse_ref(ref)
        history_pin, history_offset = self._pin("context_object", self._scope_of(ref, "history"), history_cursor, revision,
                                                "history_cursor")
        text_pin, text_offset = self._pin("context_object", self._scope_of(ref, "content"), text_cursor, history_pin,
                                          "text_cursor")
        try:
            graph = self._graph(text_pin or history_pin, self._origin(history_cursor, text_cursor))
        except _NotReady as error:
            return error.dto()
        kind, object_id = self._resolve(graph, ref)
        scope = self._scope_of(object_id, "history")
        if kind == "source":
            history_pairs = graph.source_events(object_id)
        else:
            history_pairs = graph.object_events(kind, object_id)
        selected = history_pairs[history_offset:history_offset + history_limit]
        more = history_offset + len(selected) < len(history_pairs)
        entries = (graph.source_history_entries(selected) if kind == "source"
                   else graph.history_entries(kind, object_id, selected))
        history = {"total": len(history_pairs), "items": entries,
                   "page": _page(len(entries), history_limit, more,
                                 cursors.encode("context_object", scope, graph.revision, history_offset + len(selected))
                                 if more else None)}
        result = {"status": "ready", "revision": graph.revision, "history": history}
        if kind == "source":
            result["object"] = graph.source_object(object_id)
            result["content"] = self._content(graph, kind, object_id, text_offset, text_limit)
            result["supports"] = graph.supports(object_id)
            return result
        incident = graph.adjacency(object_id) if kind == "node" else []
        result["object"] = graph.record(kind, object_id)
        result["support"] = graph.support(kind, object_id)
        if kind == "node":
            result["relationship_count"] = len(incident)
            result["relationships"] = [graph.relationship(edge_id, focus=object_id)
                                       for edge_id in incident[:OBJECT_RELATIONSHIPS]]
            result["relationships_truncated"] = len(incident) > OBJECT_RELATIONSHIPS
        if kind == "note":
            result["subjects"] = graph.subjects(object_id)
        return result

    @staticmethod
    def _scope_of(ref, part):
        """Engine IDs are unique across nodes, edges, sources and events, so the ID alone binds a cursor."""
        return {"id": SharedContextReader._parse_ref(ref)[1], "part": part}

    def _content(self, graph, kind, object_id, offset, limit):
        page = graph.source_content(object_id, offset, limit)
        if page is None:
            return None
        scope = self._scope_of(object_id, "content")
        more = page.pop("next_offset")
        page["next_cursor"] = cursors.encode("context_object", scope, graph.revision, more) if more is not None else None
        return page

    def source(self, ref, *, history_limit=None, history_cursor=None, text_limit=None, text_cursor=None,
               revision=None) -> dict:
        """The published source behind a reference; the reference alone never reads the protected operational source."""
        result = self.object(ref, history_limit=history_limit, history_cursor=history_cursor, text_limit=text_limit,
                             text_cursor=text_cursor, revision=revision)
        if result["status"] == "ready" and result["object"]["kind"] != "source":
            raise DomainError("not_found", "No such published source at this revision")
        return result

    def history(self, ref, *, limit=None, cursor=None, revision=None) -> dict:
        """The change history of one node, edge, note or source, newest first."""
        result = self.object(ref, history_limit=limit, history_cursor=cursor, revision=revision)
        if result["status"] != "ready":
            return result
        return {"status": "ready", "revision": result["revision"], "object": {key: result["object"][key] for key in ("kind", "id", "ref")},
                "history": result["history"]}

    def reference(self, ref, *, revision=None) -> dict:
        """The canonical ``{kind, id, ref, revision}`` of a published node, edge, note or source."""
        pinned, _ = self._pin("context_reference", {}, None, revision)
        try:
            graph = self._graph(pinned, "revision")
        except _NotReady as error:
            return error.dto()
        kind, object_id = self._resolve(graph, ref)
        return {"status": "ready", "revision": graph.revision, "kind": kind, "id": object_id, "ref": make_ref(kind, object_id)}

    # ---- search

    def find(self, query="", *, kind=None, attributes=None, limit=None, cursor=None, revision=None) -> dict:
        """Literal word/prefix search over published summaries, attributes, note text and source text; empty browses."""
        limit = _bounded(limit, "limit", FIND_LIMIT)
        if not isinstance(query, str) or len(query) > 2000:
            raise _invalid("query", "query must be text of at most 2000 characters")
        if kind not in (None, *KINDS):
            raise _invalid("kind", "kind must be node, edge, note or source")
        try:
            filters = normalize_attributes(attributes or {})
        except ContextError:
            raise _invalid("attributes", "attributes must map names to lists of text, numbers or booleans") from None
        scope = {"query": query, "kind": kind, "attributes": plain(filters)}
        pinned, offset = self._pin("context_find", scope, cursor, revision)
        try:
            graph = self._graph(pinned, self._origin(cursor))
            index = self._index(graph)
        except _NotReady as error:
            return error.dto()
        window = min(offset + limit + 1, MAX_SEARCH_WINDOW)
        try:
            hits = index.search(graph.revision, query, kind=kind, attributes=filters or None, limit=window)
        except (ContextError, OSError) as error:
            raise DomainError("operation_unavailable", "Context search is unavailable", retryable=True) from error
        selected = hits[offset:offset + limit]
        more = len(hits) > offset + limit
        exhausted = not more and len(hits) >= MAX_SEARCH_WINDOW
        items = [graph.entity(hit.kind, hit.id) for hit in selected]
        return {"status": "ready", "revision": graph.revision, "query": query, "items": items,
                "page": _page(len(items), limit, more,
                              cursors.encode("context_find", scope, graph.revision, offset + len(selected)) if more else None,
                              window_exhausted=exhausted)}

    def _index(self, graph) -> SQLiteIndex:
        with self._index_lock:
            if self._closed:
                raise _NotReady("unavailable", "reader_closed", "The Context reader is closed")
            index = self._indexes.get(graph.revision)
            if index is not None:
                self._indexes.move_to_end(graph.revision)
                return index
            if self._scratch is None:
                self._scratch = Path(tempfile.mkdtemp(prefix="qorqut-shared-context-"))
                weakref.finalize(self, shutil.rmtree, str(self._scratch), True)
            index = SQLiteIndex(self._scratch / f"{graph.revision}.sqlite3")
            try:
                index.rebuild(graph.revision, project(graph.snapshot))
            except (ContextError, KeyError, ValueError, OSError):
                raise _NotReady("unavailable", "search_unavailable", "The published Context cannot be indexed") from None
            self._indexes[graph.revision] = index
            while len(self._indexes) > INDEX_CACHE:
                _, old = self._indexes.popitem(last=False)
                Path(old.path).unlink(missing_ok=True)
            return index

    # ---- expansion

    def expand(self, ref, *, depth=None, direction=None, limit=None, cursor=None, revision=None) -> dict:
        """The relationships around a node out to ``depth`` (1-3), nearest first, as a pinned bounded page."""
        limit = _bounded(limit, "limit", EXPAND_LIMIT)
        depth = _bounded(depth, "depth", (1, 3, 1))
        direction = "both" if direction is None else direction
        if direction not in DIRECTIONS:
            raise _invalid("direction", "direction must be out, in or both")
        scope = {"id": self._parse_ref(ref)[1], "depth": depth, "direction": direction}
        pinned, offset = self._pin("context_expand", scope, cursor, revision)
        try:
            graph = self._graph(pinned, self._origin(cursor))
        except _NotReady as error:
            return error.dto()
        kind, node_id = self._resolve(graph, ref)
        if kind != "node":
            raise _invalid("ref", "Expansion starts from a node")
        found = graph.expansion(node_id, depth, direction)
        selected = found[offset:offset + limit]
        more = offset + len(selected) < len(found)
        return {"status": "ready", "revision": graph.revision, "origin": graph.entity("node", node_id),
                "depth": depth, "direction": direction, "total": len(found),
                "relationships": [graph.relationship(edge_id, focus=via, depth=level) for edge_id, level, via in selected],
                "page": _page(len(selected), limit, more,
                              cursors.encode("context_expand", scope, graph.revision, offset + len(selected)) if more else None)}

    # ---- counts

    def count_nodes(self, attributes, *, revision=None) -> dict:
        """How many published nodes carry every given attribute member (a navigation count, not a page)."""
        try:
            filters = normalize_attributes(attributes or {})
        except ContextError:
            raise _invalid("attributes", "attributes must map names to lists of text, numbers or booleans") from None
        pinned, _ = self._pin("count_nodes", {}, None, revision)
        try:
            graph = self._graph(pinned, "revision")
        except _NotReady as error:
            return error.dto()
        count = sum(all(member in node["attributes"].get(name, ()) for name, members in filters.items() for member in members)
                    for node in graph.snapshot.nodes.values())
        return {"status": "ready", "revision": graph.revision, "count": count}

    # ---- owner change feed

    def changes(self, *, ref=None, limit=None, cursor=None, revision=None) -> dict:
        """Published events newest first with their attributed changes (the owner diagnostic; authorization is the caller's)."""
        limit = _bounded(limit, "limit", CHANGES_LIMIT)
        scope = {"id": self._parse_ref(ref)[1] if ref is not None else None}
        pinned, offset = self._pin("context_changes", scope, cursor, revision)
        try:
            graph = self._graph(pinned, self._origin(cursor))
        except _NotReady as error:
            return error.dto()
        located = self._resolve(graph, ref) if ref is not None else None
        events = graph.events_newest_first(located)
        selected = events[offset:offset + limit]
        more = offset + len(selected) < len(events)
        return {"status": "ready", "revision": graph.revision, "total": len(events),
                "items": [graph.change_entry(event) for event in selected],
                "page": _page(len(selected), limit, more,
                              cursors.encode("context_changes", scope, graph.revision, offset + len(selected)) if more else None)}
