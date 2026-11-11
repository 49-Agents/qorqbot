"""Bounded source-library projections and a disposable SQLite text-search cache.

Registered authored files remain authoritative. The cache observes file drift;
it never reconciles that drift into the document registry. No model calls.

Selection, parsing, the one cache/index, search, chunking and the cursors are source behavior and live here. A
publication adapter (Context filing, supplied by the host) composes one read through the narrow
``present(item, row, ctx, uow)`` callback, applied at the same points inside list, get and search (and their response-size
accounting) as every other field of an item; without one the identity presentation is used.
"""

import base64
from contextlib import contextmanager
import hashlib
import json
import re
import sqlite3

from markdown_it import MarkdownIt

from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash
from qorqut_operations.application.plans import IDENTITY, PAGE, TEXT, UUID, page, rows
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.documents.serialization import split_document
from .materials import FILTERS as MATERIAL_FILTERS, classification, matches
from .relationships import projection

KINDS = {"context_topic", "context_note", "context_person", "context_upload", "context_dump", "context_map", "context", "pillar", "content", "shared_instruction"}
FILTERS = {**MATERIAL_FILTERS, "scope": {"enum": ["business", "shared", "all"]}, "kind": TEXT,
           "topic_id": UUID, "person_id": UUID,
           "include_archived": {"type": "boolean"}}
CURSOR = {"type": "string", "maxLength": 2000}
READ_ARGUMENTS = {
    "context_list": {**PAGE, **FILTERS},
    "context_search": {**PAGE, **FILTERS, "query": {**TEXT, "maxLength": 500}, "refresh_cursor": CURSOR},
    "context_get": {"document_id": IDENTITY, "scope": FILTERS["scope"], "cursor": CURSOR,
        "heading": {**TEXT, "maxLength": 300}, "start_line": {"type": "integer", "minimum": 1, "maximum": 2000000},
        "end_line": {"type": "integer", "minimum": 1, "maximum": 2000000},
        "max_chars": {"type": "integer", "minimum": 100, "maximum": 12000}},
}
MAX_SCAN = 2000
MAX_REBUILD = 64
MAX_READ_BYTES = 8_000_000
MAX_MATCHES = 2000
MAX_RESPONSE_BYTES = 48000


def token(value):
    return base64.urlsafe_b64encode(canonical_bytes(value)).decode("ascii")


def untoken(value, binding):
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if not isinstance(decoded, dict) or decoded.get("binding") != binding:
            raise ValueError
        return decoded
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DomainError("validation_error", "Cursor belongs to a different query, scope or document version") from exc


def signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def identity_presentation(item, row, ctx, uow):
    """The presentation of a source-only caller: the item exactly as the source library composed it."""
    return item


class SourceLibrary:
    def __init__(self, store, *, present=identity_presentation):
        """``present(item, row, ctx, uow)`` is the host's publication composition of one read item (its filing fields), or the
        identity presentation when nothing is published over the sources."""
        if not callable(present):
            raise TypeError("The source library presentation is a callable present(item, row, ctx, uow)")
        self.store, self.present = store, present
        self.cache_path = store.database.path.parent / "context-search.sqlite3"

    @contextmanager
    def cache(self):
        # Rebuildable derived state is kept outside the authority transaction,
        # exports and backups. It contains only explicit context-library sources.
        db = None
        try:
            db = sqlite3.connect(self.cache_path, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, binding TEXT NOT NULL, stat TEXT, digest TEXT, status TEXT NOT NULL)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(id UNINDEXED, title, body)")
            db.execute("CREATE TEMP TABLE authorized (id TEXT PRIMARY KEY)")
            db.execute("CREATE TEMP TABLE matching_scope (id TEXT PRIMARY KEY)")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise DomainError("context_index_unavailable", "Context search cache is unavailable", retryable=True) from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def eligible(row):
        if row["kind"] not in KINDS or row["owning_agent_id"] is not None:
            return False
        if row["business_id"] is None:
            return row["namespace"] == "global" and row["path"].startswith(("context/", "files/instructions/"))
        return True

    @staticmethod
    def require_shared(ctx):
        if "read_global_context" not in ctx.global_capabilities:
            raise DomainError("forbidden", "Shared context capability is not granted")

    def shared_source(self, ctx, identity, uow):
        """One shared Context document exactly as the shared-scope ``context_get`` serves it, with the hash of its current bytes.

        The same capability, eligibility (registered kind, unowned, Global namespace under ``context/`` or ``files/instructions/``),
        ready and unarchived rules apply; no body is returned. Anything else is not found, so nothing about it is disclosed."""
        self.require_shared(ctx)
        row = uow.repository("documents", business_id=None).get(identity)
        if row is None or row.get("archived") or not self.eligible(row):
            raise DomainError("not_found", "Document is not in the selected context library")
        return row, self.observe(row, uow)[1]

    def namespaces(self, ctx, args):
        """The namespaces a library read may select from for ``args``, after its scope and capability checks."""
        scope = args.get("scope", "business" if ctx.business_id else "shared")
        if scope in {"business", "all"} and not ctx.business_id:
            raise DomainError("scope_required", "Select a project for its context library")
        if scope in {"shared", "all"}:
            self.require_shared(ctx)
        return ([ctx.business_id] if scope in {"business", "all"} else []) + ([None] if scope in {"shared", "all"} else [])

    def excerpt(self, ctx, args, uow):
        """The opening of one library document, for a trusted caller that reads inside a write transaction.

        It serves what ``context_get`` would for the same ``document_id`` and ``scope``: the same scope and capability
        checks, the same eligibility (so private, agent-owned and unregistered-kind documents are not found), and the same
        observation of the current bytes and their hash. Its cost is bounded by the text file limit alone: the document is
        found by one lookup per selected namespace instead of loading the library, and nothing is parsed, so there is no
        heading outline, line range or cursor. The host's presentation of filing fields is not applied."""
        row = None
        for business in self.namespaces(ctx, args):
            found = uow.repository("documents", business_id=business).get(args["document_id"])
            if found is not None and self.eligible(found):
                row = {**found, "metadata_json": projection(found, uow)}
                break
        if row is None:
            raise DomainError("not_found", "Document is not in the selected context library")
        try:
            body, digest, _, _ = self.observe(row, uow)
        except OSError as exc:
            raise DomainError("document_unavailable", "Registered context source is unavailable") from exc
        part = body[:args.get("max_chars", 6000)]
        return {"document": {**self.summary(row), "content_hash": digest,
                             "metadata_stale": digest != (row["content_hash"] or "").removeprefix("sha256:"), "body": part},
                "complete": len(part) == len(body)}

    def select(self, ctx, args, uow):
        namespaces = self.namespaces(ctx, args)
        marks = ",".join("?" for _ in KINDS)
        found = [row for business in namespaces for row in rows(uow, "documents", business,
            where="kind IN (" + marks + ") AND owning_agent_id IS NULL", parameters=sorted(KINDS)) if self.eligible(row)]
        found = [{**row, "metadata_json": projection(row, uow)} for row in found if args.get("include_archived") or not row.get("archived")]
        return sorted(found, key=lambda row: row["id"])

    @staticmethod
    def filtered(row, args):
        meta = row["metadata_json"]
        return (matches(classification(row), args) and all(row["kind"] == args[key] if key == "kind" else meta.get(key) == args[key]
                    for key in ("kind",) if key in args)
                and ("topic_id" not in args or args["topic_id"] in meta.get("topic_ids", []))
                and ("person_id" not in args or args["person_id"] in meta.get("people", [])))

    @staticmethod
    def summary(row):
        meta = row["metadata_json"]
        return {**classification(row), "id": row["id"], "business_id": row["business_id"], "scope": "business" if row["business_id"] else "shared",
            "kind": row["kind"], "title": str(meta.get("title", meta.get("name", row["path"])))[:240],
            "summary": str(meta.get("summary", ""))[:400], "revision": row["revision"],
            "content_hash": row["content_hash"], "availability": row["availability"],
            "filing_status": meta.get('status') if row['kind'] in {'context_upload', 'context_dump'} else None,
            "topic_id": str(meta["topic_id"])[:64] if meta.get("topic_id") else None,
            "topic_ids": meta.get("topic_ids", []), "parent_ids": meta.get("parent_ids", []), "archived": bool(row.get("archived")),
            "people_ids": [str(x)[:64] for x in meta.get("people", [])[:20]]}

    @staticmethod
    def binding(row):
        return canonical_hash([row["business_id"], row["namespace"], row["path"], row["kind"], row["revision"],
                               row["content_hash"], row["availability"], row["metadata_json"]])

    def path(self, row, uow):
        return self.store._file_target(self.store.file_ref(row["id"], row["path"], row["business_id"]), uow).resolve()

    def observe(self, row, uow):
        if row["availability"] != "ready":
            raise DomainError("busy", "Context document has an unfinished application", retryable=True)
        path = self.path(row, uow)
        before = signature(path)
        raw = self.store.read_bytes(row, uow)
        if before != signature(path):
            raise DomainError("revision_conflict", "Context source changed during reading", retryable=True)
        digest = hashlib.sha256(raw).hexdigest()
        body = split_document(raw)[1]
        return body, digest, before, len(raw)

    def reconcile(self, db, selected, args, binding, uow):
        start = 0
        if args.get("refresh_cursor"):
            after = untoken(args["refresh_cursor"], binding).get("after")
            if not isinstance(after, str):
                raise DomainError("validation_error", "Invalid context refresh cursor")
            start = next((i + 1 for i, row in enumerate(selected) if row["id"] == after), -1)
            if start < 0:
                raise DomainError("validation_error", "Context refresh source is no longer selected")
        indexed, byte_count, checked, last = 0, 0, 0, None
        for row in selected[start:]:
            if checked >= MAX_SCAN or indexed >= MAX_REBUILD or byte_count >= MAX_READ_BYTES:
                break
            old = db.execute("SELECT * FROM sources WHERE id = ?", (row["id"],)).fetchone()
            bind = self.binding(row)
            stat, digest, body, status = None, None, "", "unavailable"
            try:
                stat = signature(self.path(row, uow))
                if old and old["status"] in {"current", "stale"} and old["binding"] == bind and old["stat"] == json.dumps(stat) and row["availability"] == "ready":
                    last, checked = row["id"], checked + 1
                    continue
                if stat[2] + byte_count > MAX_READ_BYTES and checked:
                    break
                body, digest, stat, size = self.observe(row, uow)
                byte_count += size
                status = "current" if digest == (row["content_hash"] or "").removeprefix("sha256:") else "stale"
            except (DomainError, OSError):
                pass
            db.execute("DELETE FROM search WHERE id = ?", (row["id"],))
            if digest is not None:
                db.execute("INSERT INTO search(id,title,body) VALUES (?,?,?)", (row["id"], self.summary(row)["title"], body))
            db.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)", (row["id"], bind, json.dumps(stat), digest, status))
            last, checked, indexed = row["id"], checked + 1, indexed + 1
        pending = start + checked < len(selected)
        return token({"binding": binding, "after": last}) if pending and last else None, pending

    def read(self, operation, ctx, args, uow):
        selected = self.select(ctx, {**args, **({"include_archived": True} if operation == "context_get" else {})}, uow)
        binding = canonical_hash({"sources": [(row["id"], self.binding(row)) for row in selected],
            "query": {key: value for key, value in args.items() if key not in {"cursor", "refresh_cursor", "limit", "max_chars"}},
            "business_id": ctx.business_id, "allowed": sorted(ctx.allowed_business_ids), "global": sorted(ctx.global_capabilities)})
        if operation == "context_get":
            row = next((row for row in selected if row["id"] == args["document_id"]), None)
            if row is None:
                raise DomainError("not_found", "Document is not in the selected context library")
            return self.get(row, args, self.document_binding(row, ctx, args), ctx, uow)
        if operation == "context_list":
            values = [self.summary(row) for row in selected if self.filtered(row, args)]
            by_id = {row["id"]: row for row in selected}

            def listed(limit):
                chosen, following = page(values, {**args, "limit": limit}, binding, operation, key=lambda row: (row["id"],), reverse=False)
                return [self.present(item, by_id[item["id"]], ctx, uow) for item in chosen], following
            items, cursor = listed(min(args.get("limit", 30), 50))
            used, count = 0, 0
            for item in items:
                size = len(canonical_bytes(item))
                if count and used + size > MAX_RESPONSE_BYTES:
                    break
                used, count = used + size, count + 1
            if count < len(items):
                items, cursor = listed(count)
            return {"items": items, "next_cursor": cursor, "total": len(values), "projection": "registered_metadata"}
        return self.search(selected, args, binding, ctx, uow)

    @staticmethod
    def document_binding(row, ctx, args):
        """What a body cursor is bound to: the document's identity, the query and the caller's scope. The body bytes it pages
        are bound separately (``get``). The document's row revision and stored metadata are deliberately not part of it: a filing
        completion rewrites both, and that bookkeeping must not strand a reader in the middle of an unchanged body."""
        return canonical_hash({"document": [row["id"], row["business_id"], row["kind"]],
            "query": {key: value for key, value in args.items() if key not in {"cursor", "refresh_cursor", "limit", "max_chars"}},
            "business_id": ctx.business_id, "allowed": sorted(ctx.allowed_business_ids), "global": sorted(ctx.global_capabilities)})

    def refresh_managed(self, identities, uow):
        """Opportunistic cache refresh after verified managed effects; reads repair misses."""
        if not identities:
            return
        selected = []
        for identity in identities:
            record = uow.connection.execute("SELECT * FROM documents WHERE id = ?", (identity,)).fetchone()
            if record:
                row = decode_row(record)
                if self.eligible(row):
                    selected.append({**row, "availability": "ready"})
        try:
            with self.cache() as db:
                for identity in identities:
                    db.execute("DELETE FROM sources WHERE id = ?", (identity,))
                    db.execute("DELETE FROM search WHERE id = ?", (identity,))
                self.reconcile(db, sorted(selected, key=lambda row: row["id"]), {}, "managed", uow)
        except DomainError:
            # Disposable index availability must not decide the authority commit.
            # Search always checks revisions and file fingerprints before use.
            pass

    def search(self, selected, args, binding, ctx, uow):
        terms = list(dict.fromkeys(re.findall(r"\w+", args["query"].casefold())))
        if not 1 <= len(terms) <= 32:
            raise DomainError("validation_error", "Search needs 1 to 32 words")
        query = " AND ".join('"' + term + '"' for term in terms)
        with self.cache() as db:
            refresh, pending = self.reconcile(db, selected, args, binding, uow)
            allowed = {row["id"]: row for row in selected}
            db.executemany("INSERT INTO authorized VALUES (?)", [(identity,) for identity in allowed])
            db.executemany("INSERT INTO matching_scope VALUES (?)", [(row["id"],) for row in selected if self.filtered(row, args)])
            sources = {row["id"]: row for row in db.execute("SELECT s.* FROM sources s JOIN authorized a ON a.id=s.id")}
            unknown = [row["id"] for row in selected if row["id"] not in sources or sources[row["id"]]["binding"] != self.binding(row)]
            stale = [identity for identity, row in sources.items() if row["status"] != "current"]
            generation = canonical_hash([(identity, row["binding"], row["digest"], row["status"]) for identity, row in sorted(sources.items())])
            cursor_binding = canonical_hash([binding, generation])
            offset = 0
            if args.get("cursor"):
                offset = untoken(args["cursor"], cursor_binding).get("offset")
                if type(offset) is not int or not 0 <= offset <= MAX_MATCHES:
                    raise DomainError("validation_error", "Invalid context result cursor")
            matches = []
            # Authorization precedes matching, result counting, excerpts and pagination.
            # Scores use only a matching document's own title, never corpus-wide stats.
            results = db.execute("SELECT search.id, search.title FROM search JOIN matching_scope a ON a.id=search.id WHERE search MATCH ? ORDER BY search.id LIMIT ?", (query, MAX_MATCHES + 1)).fetchall()
            for result in results:
                row = allowed[result["id"]]
                if result["id"] in unknown or not self.filtered(row, args):
                    continue
                score = sum(term in result["title"].casefold() for term in terms)
                matches.append((score, row["id"]))
            matches.sort(key=lambda item: (-item[0], item[1]))
            limited = len(results) > MAX_MATCHES
            matches = matches[:MAX_MATCHES]
            items, used_bytes, read_bytes, consumed = [], 0, 0, 0
            for _, identity in matches[offset:offset + min(args.get("limit", 20), 50)]:
                row = allowed[identity]
                try:
                    size = signature(self.path(row, uow))[2]
                    if read_bytes + size > MAX_READ_BYTES:
                        break
                    body, digest, _, size = self.observe(row, uow)
                    read_bytes += size
                except (DomainError, OSError):
                    pending = True
                    consumed += 1
                    continue
                if digest != sources[identity]["digest"]:
                    raise DomainError("revision_conflict", "Search source changed during retrieval; repeat the search", retryable=True)
                folded = body.casefold()
                hits = [folded.find(term) for term in terms if term in folded]
                at = max(0, min(hits, default=0) - 100)
                item = self.present({**self.summary(row), "excerpt": body[at:at + 500],
                    "content_hash": digest, "metadata_stale": digest != (row["content_hash"] or "").removeprefix("sha256:"),
                    "line": body.count("\n", 0, at) + 1}, row, ctx, uow)
                size = len(canonical_bytes(item))
                if items and used_bytes + size > MAX_RESPONSE_BYTES:
                    break
                items.append(item)
                used_bytes += size
                consumed += 1
            next_cursor = token({"binding": cursor_binding, "offset": offset + consumed}) if offset + consumed < len(matches) else None
            reasons = (["refresh_incomplete"] if pending or unknown else []) + (["source_drift_or_unavailable"] if stale else []) + (["match_limit"] if limited else [])
            return {"items": items, "next_cursor": next_cursor, "refresh_cursor": refresh, "complete": not reasons,
                "incomplete_reasons": reasons, "matched": len(matches), "generation": generation,
                "coverage": {"selected": len(selected), "indexed": len(sources), "stale_or_unavailable": len(stale)},
                "observation": "File checks span the refresh pass; this is not an atomic filesystem snapshot."}

    def get(self, row, args, binding, ctx, uow):
        try:
            body, digest, _, _ = self.observe(row, uow)
        except OSError as exc:
            raise DomainError("document_unavailable", "Registered context source is unavailable") from exc
        lines = body.splitlines(keepends=True)
        start, end = args.get("start_line", 1) - 1, args.get("end_line", len(lines))
        if "end_line" in args and args["end_line"] < args.get("start_line", 1):
            raise DomainError("validation_error", "End line precedes start line")
        headings = []
        tokens = MarkdownIt("commonmark").parse(body)
        for i, parsed in enumerate(tokens):
            if parsed.type == "heading_open":
                headings.append({"title": tokens[i + 1].content[:300], "line": parsed.map[0] + 1, "level": int(parsed.tag[1:])})
        if "heading" in args:
            if "start_line" in args or "end_line" in args:
                raise DomainError("validation_error", "Choose a heading or a line range")
            matches = [i for i, heading in enumerate(headings) if heading["title"] == args["heading"]]
            if len(matches) != 1:
                raise DomainError("validation_error", "Heading must identify exactly one section; use lines for duplicate headings")
            i = matches[0]
            start = headings[i]["line"] - 1
            end = next((heading["line"] - 1 for heading in headings[i + 1:] if heading["level"] <= headings[i]["level"]), len(lines))
        if start < 0 or end < start or (start >= len(lines) and (start or lines)):
            raise DomainError("validation_error", "Line range is outside the document")
        selected = "".join(lines[start:end])
        # The pages are bound to the body text itself, not to the file digest: the digest also covers the frontmatter that a filing
        # completion rewrites, which must not invalidate a cursor into an unchanged body.
        cursor_binding = canonical_hash([binding, hashlib.sha256(body.encode()).hexdigest(), start, end])
        offset = untoken(args["cursor"], cursor_binding).get("offset") if args.get("cursor") else 0
        if type(offset) is not int or not 0 <= offset <= len(selected):
            raise DomainError("validation_error", "Invalid context read cursor")
        part = selected[offset:offset + args.get("max_chars", 6000)].encode()[:24000].decode("utf-8", errors="ignore")
        following = offset + len(part)
        return {"document": self.present({**self.summary(row), "content_hash": digest,
            "metadata_stale": digest != (row["content_hash"] or "").removeprefix("sha256:"), "body": part,
            "start_line": start + selected.count("\n", 0, offset) + 1, "character_offset": offset,
            "total_lines": len(lines),
            "headings": [{**heading, "title": heading["title"][:120]} for heading in headings[:25]],
            "headings_truncated": len(headings) > 25}, row, ctx, uow),
            "next_cursor": token({"binding": cursor_binding, "offset": following}) if following < len(selected) else None,
            "complete": following == len(selected)}
