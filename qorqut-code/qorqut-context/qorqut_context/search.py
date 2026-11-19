"""Revisioned retrieval boundary shared by text and future embedding indexes."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterable, Protocol

from .schema import ContextError, attributes as normalize_attributes, digest, value_key


class StaleIndex(ContextError):
    pass


@dataclass(frozen=True)
class SearchRecord:
    kind: str
    id: str
    text: str
    attributes: dict
    content_digest: str

    @property
    def key(self):
        return f"{self.kind}:{self.id}"


@dataclass(frozen=True)
class SearchHit:
    kind: str
    id: str
    revision: str
    content_digest: str
    text: str
    input_mode: str | None = None


@dataclass(frozen=True)
class IndexChanges:
    base_revision: str
    revision: str
    changed: tuple[SearchRecord, ...]
    removed: tuple[str, ...]


class SearchBackend(Protocol):
    """All data is scoped to one Context; indexes never define graph authority."""

    def revision(self) -> str | None: ...
    def rebuild(self, revision: str, records: Iterable[SearchRecord]) -> None: ...
    def apply(self, changes: IndexChanges) -> None: ...
    def search(self, revision: str, query: str = "", *, kind: str | None = None,
               attributes: dict | None = None, limit: int = 20) -> list[SearchHit]: ...


def project(snapshot) -> tuple[SearchRecord, ...]:
    result = []
    for kind, objects in (("node", snapshot.nodes), ("edge", snapshot.edges)):
        for record in objects.values():
            text = record["summary"] + "\n" + " ".join(
                str(v) for values in record["attributes"].values() for v in values)
            attrs = record["attributes"]
            result.append(SearchRecord(kind, record["id"], text, attrs,
                                       digest({"text": text, "attributes": attrs})))
    for note in snapshot.notes.values():
        result.append(SearchRecord("note", note["id"], note["text"], {},
                                   digest({"text": note["text"], "objects": note["objects"]})))
    for source_id, event in snapshot.sources.items():
        if event["operation"] == "remove":
            continue
        source = event["source"]
        body = snapshot.files[source["path"]].decode("utf-8", errors="replace")
        text = source["title"] + "\n" + body
        result.append(SearchRecord("source", source_id, text, {}, digest({"text": text})))
    return tuple(sorted(result, key=lambda r: r.key))


def changes_between(before, after) -> IndexChanges:
    old, new = ({r.key: r for r in project(s)} for s in (before, after))
    return IndexChanges(before.revision, after.revision,
                        tuple(record for key, record in new.items()
                              if key not in old or record.content_digest != old[key].content_digest),
                        tuple(sorted(set(old) - set(new))))


class SQLiteIndex:
    def __init__(self, path: Path):
        self.path = Path(path)

    def revision(self):
        if not self.path.exists():
            return None
        try:
            with sqlite3.connect(self.path) as db:
                row = db.execute("SELECT revision FROM state WHERE singleton = 1").fetchone()
                return row[0] if row else None
        except sqlite3.DatabaseError:
            return None  # Disposable corruption is repaired by a complete rebuild.

    @staticmethod
    def _schema(db):
        db.execute("CREATE TABLE state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision TEXT NOT NULL)")
        db.execute("CREATE TABLE records (key TEXT PRIMARY KEY, kind TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, attributes TEXT NOT NULL, content_digest TEXT NOT NULL)")
        db.execute("CREATE VIRTUAL TABLE words USING fts5(key UNINDEXED, text, tokenize='unicode61')")

    @staticmethod
    def _insert(db, record):
        db.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)",
                   (record.key, record.kind, record.id, record.text,
                    json.dumps(record.attributes, ensure_ascii=False), record.content_digest))
        db.execute("INSERT INTO words VALUES (?, ?)", (record.key, record.text))

    def rebuild(self, revision, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="search-", suffix=".sqlite3", dir=self.path.parent)
        os.close(fd)
        try:
            with sqlite3.connect(temporary) as db:
                self._schema(db)
                for record in records:
                    self._insert(db, record)
                db.execute("INSERT INTO state VALUES (1, ?)", (revision,))
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def apply(self, changes):
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM state WHERE singleton=1").fetchone()
            if row is None or row[0] != changes.base_revision:
                raise StaleIndex("Index delta does not match its base revision")
            for key in (*changes.removed, *(record.key for record in changes.changed)):
                db.execute("DELETE FROM words WHERE key = ?", (key,))
                db.execute("DELETE FROM records WHERE key = ?", (key,))
            for record in changes.changed:
                self._insert(db, record)
            db.execute("UPDATE state SET revision = ? WHERE singleton=1", (changes.revision,))

    def search(self, revision, query="", *, kind=None, attributes=None, limit=20):
        return self.matched(revision, query, kind=kind, attributes=attributes, limit=limit)[0]

    def matched(self, revision, query="", *, kind=None, attributes=None, limit=20):
        """The hits and how the query matched: ``all``, ``any`` or ``browse`` (an empty query, in key order).

        Every word is a literal word or prefix. Records holding every word come first, most relevant first (bm25,
        then key); records holding only some of the words follow, ranked the same way. ``all`` says at least one
        record holds every word, ``any`` that none does. The order depends on the revision, query and filters alone,
        never on ``limit``, so paging through one query stays stable."""
        if not isinstance(query, str) or kind not in (None, "node", "edge", "note", "source"):
            raise ContextError("Invalid search text or record kind")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ContextError("limit must be an integer between 1 and 1000")
        filters = normalize_attributes(attributes or {})
        tokens = list(dict.fromkeys(re.findall(r"\w+", query, re.UNICODE)))
        if not tokens and query.strip():
            return [], "all"
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT revision FROM state WHERE singleton=1").fetchone()
            if row is None or row[0] != revision:
                raise StaleIndex("Search index does not represent the requested revision")
            if not tokens:
                return self._select(db, revision, None, kind, filters, limit), "browse"
            hits = self._select(db, revision, " AND ".join('"' + word + '"*' for word in tokens), kind, filters, limit)
            mode = "all" if hits or len(tokens) == 1 else "any"
            if len(tokens) > 1 and len(hits) < limit:
                # One- and two-letter words carry no meaning as prefixes ("is" would match "issue"), so the partial
                # matches leave them out whenever the query has longer words.
                words = [word for word in tokens if len(word) >= 3] or tokens
                seen = {(hit.kind, hit.id) for hit in hits}
                partial = self._select(db, revision, " OR ".join('"' + word + '"*' for word in words), kind, filters,
                                       limit + len(hits))
                hits += [hit for hit in partial if (hit.kind, hit.id) not in seen][:limit - len(hits)]
            return hits, mode

    @staticmethod
    def _select(db, revision, match, kind, filters, limit):
        sql, parameters, predicates = "SELECT r.* FROM records r", [], []
        if match:
            sql += " JOIN words ON r.key=words.key"
            predicates.append("words MATCH ?")
            parameters.append(match)
        if kind:
            predicates.append("r.kind = ?")
            parameters.append(kind)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += " ORDER BY bm25(words), r.key" if match else " ORDER BY r.key"
        result = []
        for _, record_kind, record_id, body, attrs_json, content_hash in db.execute(sql, parameters):
            attrs = json.loads(attrs_json)
            if any(not {value_key(v) for v in values} <=
                   {value_key(v) for v in attrs.get(name, [])}
                   for name, values in filters.items()):
                continue
            result.append(SearchHit(record_kind, record_id, revision, content_hash, body))
            if len(result) == limit:
                break
        return result
