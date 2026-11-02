"""Durable change feed, consumer cursors and external-edit scanning."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .catalog import Catalog
from .core import LayerContext
from .errors import InvalidRequestError, NotFoundError, StateConflictError
from .files import FileEntry
from .util import dumps, loads, new_id

CONSUMER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_CONSUMER_RE = re.compile(CONSUMER_PATTERN)
ACTOR_KIND_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"
_ACTOR_KIND_RE = re.compile(ACTOR_KIND_PATTERN)
EXTERNAL_ATTRIBUTION = {
    "actor_id": "external/unknown",
    "actor_kind": "external",
    "reason": "Observed by an explicit scan; the author is unknown.",
    "source_refs": [],
    "trust": "external_unknown",
}
MAX_LIST_LIMIT = 500


def normalize_attribution(value: Any, *, trust: str) -> dict[str, Any]:
    """Validate a caller-supplied attribution record and stamp its trust label."""
    if not isinstance(value, dict):
        raise InvalidRequestError("attribution must be an object", details={"field": "attribution"})
    unknown = set(value) - {"actor_id", "actor_kind", "reason", "source_refs"}
    if unknown:
        raise InvalidRequestError(f"unknown attribution field(s): {sorted(unknown)}", details={"field": "attribution"})
    actor_id, actor_kind, reason = value.get("actor_id"), value.get("actor_kind"), value.get("reason")
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 200:
        raise InvalidRequestError("attribution.actor_id must be a string of 1-200 characters")
    if not isinstance(actor_kind, str) or not _ACTOR_KIND_RE.match(actor_kind):
        raise InvalidRequestError(f"attribution.actor_kind must match {ACTOR_KIND_PATTERN}")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
        raise InvalidRequestError("attribution.reason must be a string of 1-2000 characters")
    refs = value.get("source_refs", [])
    if not isinstance(refs, list) or len(refs) > 50 or not all(isinstance(r, str) and 1 <= len(r) <= 500 for r in refs):
        raise InvalidRequestError("attribution.source_refs must be a list of up to 50 strings")
    return {"actor_id": actor_id, "actor_kind": actor_kind, "reason": reason, "source_refs": list(refs), "trust": trust}


def normalize_actor(value: Any) -> dict[str, str]:
    """Validate the ``{actor_id, actor_kind}`` record used by tools and workflows."""
    if not isinstance(value, dict):
        raise InvalidRequestError("actor must be an object", details={"field": "actor"})
    unknown = set(value) - {"actor_id", "actor_kind"}
    if unknown:
        raise InvalidRequestError(f"unknown actor field(s): {sorted(unknown)}", details={"field": "actor"})
    actor_id, actor_kind = value.get("actor_id"), value.get("actor_kind")
    if not isinstance(actor_id, str) or not 1 <= len(actor_id) <= 200:
        raise InvalidRequestError("actor.actor_id must be a string of 1-200 characters")
    if not isinstance(actor_kind, str) or not _ACTOR_KIND_RE.match(actor_kind):
        raise InvalidRequestError(f"actor.actor_kind must match {ACTOR_KIND_PATTERN}")
    return {"actor_id": actor_id, "actor_kind": actor_kind}


def package_ids_of(paths: list[str]) -> list[str]:
    found = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "packages":
            found.add(parts[1])
    return sorted(found)


def insert_event(
    conn: sqlite3.Connection,
    *,
    change_id: str,
    created_at: str,
    kind: str,
    attribution: dict[str, Any],
    files: list[dict[str, Any]],
    entry_refs: list[str],
    details: dict[str, Any],
) -> int:
    """Insert a finalized event and bring the observed inventory up to date.

    ``files`` items: ``path, action, before_sha256, after_sha256, size``.
    """
    paths = [f["path"] for f in files]
    cursor = conn.execute(
        "INSERT INTO changes(change_id, created_at, kind, actor_id, actor_kind, reason, source_refs, trust,"
        " package_ids, entry_refs, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            change_id, created_at, kind, attribution["actor_id"], attribution["actor_kind"], attribution["reason"],
            dumps(attribution["source_refs"]), attribution["trust"], dumps(package_ids_of(paths)),
            dumps(sorted(entry_refs)), dumps(details),
        ),
    )
    sequence = cursor.lastrowid
    for ordinal, item in enumerate(files):
        conn.execute(
            "INSERT INTO change_files(change_id, ordinal, path, action, before_sha256, after_sha256) VALUES (?,?,?,?,?,?)",
            (change_id, ordinal, item["path"], item["action"], item["before_sha256"], item["after_sha256"]),
        )
        if item["after_sha256"] is None:
            conn.execute("DELETE FROM inventory WHERE path = ?", (item["path"],))
        else:
            conn.execute(
                "INSERT INTO inventory(path, sha256, size) VALUES (?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET sha256 = excluded.sha256, size = excluded.size",
                (item["path"], item["after_sha256"], item["size"]),
            )
    return int(sequence)


def event_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    files = conn.execute(
        "SELECT path, action, before_sha256, after_sha256, commit_id FROM change_files WHERE change_id = ? ORDER BY ordinal",
        (row["change_id"],),
    ).fetchall()
    commits = sorted({f["commit_id"] for f in files if f["commit_id"]})
    return {
        "sequence": row["sequence"],
        "change_id": row["change_id"],
        "timestamp": row["created_at"],
        "kind": row["kind"],
        "attribution": {
            "actor_id": row["actor_id"],
            "actor_kind": row["actor_kind"],
            "reason": row["reason"],
            "source_refs": loads(row["source_refs"]),
            "trust": row["trust"],
        },
        "files": [
            {
                "path": f["path"],
                "action": f["action"],
                "before_sha256": f["before_sha256"],
                "after_sha256": f["after_sha256"],
                "commit": f["commit_id"],
            }
            for f in files
        ],
        "paths": [f["path"] for f in files],
        "package_ids": loads(row["package_ids"]),
        "entry_refs": loads(row["entry_refs"]),
        "commits": commits,
        "details": loads(row["details"]),
    }


def entries_touching(catalog: Catalog, paths: set[str]) -> list[str]:
    """Entry references whose entry file, resources or package manifest is among ``paths``."""
    refs = set()
    for ref, entry in catalog.entries.items():
        package = catalog.packages[entry.package_id]
        if package.manifest_path in paths or entry.root_path in paths or any(r.root_path in paths for r in entry.resources):
            refs.add(ref)
    return sorted(refs)


class Changes:
    """Public change-feed operations (``layer.changes``)."""

    def __init__(self, ctx: LayerContext) -> None:
        self.ctx = ctx

    # -- feed --------------------------------------------------------------------
    def list(self, after: int | None = None, limit: int = 100, consumer: str | None = None) -> dict[str, Any]:
        """Finalized events with ``sequence > after`` (or after ``consumer``'s cursor)."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise InvalidRequestError(f"limit must be an integer between 1 and {MAX_LIST_LIMIT}")
        with self.ctx.store.reader() as conn:
            if after is None:
                after = 0
                if consumer is not None:
                    self._check_consumer(consumer)
                    row = conn.execute("SELECT cursor FROM consumers WHERE name = ?", (consumer,)).fetchone()
                    after = row["cursor"] if row else 0
            elif not isinstance(after, int) or isinstance(after, bool) or after < 0:
                raise InvalidRequestError("after must be a non-negative integer")
            rows = conn.execute(
                "SELECT * FROM changes WHERE sequence > ? ORDER BY sequence LIMIT ?", (after, limit + 1)
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            events = [event_dict(conn, row) for row in rows]
            head = self._head(conn)
        return {
            "changes": events,
            "after": after,
            "next_after": events[-1]["sequence"] if events else after,
            "head": head,
            "has_more": has_more,
        }

    def get(self, change_id: str) -> dict[str, Any]:
        with self.ctx.store.reader() as conn:
            row = conn.execute("SELECT * FROM changes WHERE change_id = ?", (change_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"no finalized change {change_id!r}", details={"change_id": change_id})
            return {"change": event_dict(conn, row)}

    @staticmethod
    def _head(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(sequence), 0) AS head FROM changes").fetchone()
        return int(row["head"])

    # -- cursors -----------------------------------------------------------------
    @staticmethod
    def _check_consumer(name: Any) -> str:
        if not isinstance(name, str) or not _CONSUMER_RE.match(name):
            raise InvalidRequestError(f"consumer must match {CONSUMER_PATTERN}", details={"field": "consumer"})
        return name

    def cursor(self, consumer: str) -> dict[str, Any]:
        self._check_consumer(consumer)
        with self.ctx.store.reader() as conn:
            row = conn.execute("SELECT cursor, updated_at FROM consumers WHERE name = ?", (consumer,)).fetchone()
            head = self._head(conn)
        return {
            "consumer": consumer,
            "cursor": row["cursor"] if row else 0,
            "exists": row is not None,
            "updated_at": row["updated_at"] if row else None,
            "head": head,
        }

    def advance_cursor(self, consumer: str, sequence: int, expected_cursor: int | None = None) -> dict[str, Any]:
        """Move a consumer cursor forward after the receiver acknowledged delivery.

        Advancing to the current value is a no-op; moving backwards or past the
        head is refused. ``expected_cursor`` makes the move compare-and-set.
        """
        self._check_consumer(consumer)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise InvalidRequestError("sequence must be a non-negative integer")
        with self.ctx.store.transaction() as conn:
            head = self._head(conn)
            row = conn.execute("SELECT cursor FROM consumers WHERE name = ?", (consumer,)).fetchone()
            current = row["cursor"] if row else 0
            if expected_cursor is not None and expected_cursor != current:
                raise StateConflictError(
                    "the consumer cursor is not at the expected position",
                    details={"reason": "cursor_changed", "expected": expected_cursor, "current": current},
                )
            if sequence > head:
                raise StateConflictError(
                    "cannot advance a cursor past the newest finalized change",
                    details={"reason": "beyond_head", "sequence": sequence, "head": head},
                )
            if sequence < current:
                raise StateConflictError(
                    "cursors only move forward",
                    details={"reason": "cursor_regress", "sequence": sequence, "current": current},
                )
            now = self.ctx.now()
            conn.execute(
                "INSERT INTO consumers(name, cursor, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at",
                (consumer, sequence, now),
            )
        return {"consumer": consumer, "cursor": sequence, "previous": current, "head": head, "updated_at": now}

    # -- external edits ------------------------------------------------------------
    def scan(self, attribution: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reconcile edits made outside the toolkit into one event (net change only).

        ``attribution`` may be supplied by a trusted adapter; otherwise the event
        is attributed ``external/unknown``.
        """
        attr = (
            normalize_attribution(attribution, trust=self.ctx.attribution_source)
            if attribution is not None
            else dict(EXTERNAL_ATTRIBUTION)
        )
        with self.ctx.store.lock(exclusive=True):
            self.ctx.require_no_recovery()
            return self.scan_locked(attr)

    def scan_locked(self, attribution: dict[str, Any], paths: set[str] | None = None) -> dict[str, Any]:
        """Scan while the caller holds the exclusive lock. ``paths`` restricts the scan."""
        ctx = self.ctx
        observed: dict[str, FileEntry | None] = {}
        skipped: list[dict[str, str]] = []
        if paths is None:
            files, special = ctx.tree.inventory()
            observed.update(files)
            skipped = [{"path": rel, "kind": kind} for rel, kind in sorted(special.items())]
        else:
            for rel in sorted(paths):
                kind = ctx.tree.kind(rel)
                if kind == "file":
                    observed[rel] = ctx.tree.digest(rel)
                elif kind in ("symlink", "other"):
                    skipped.append({"path": rel, "kind": kind})
        with ctx.store.reader() as conn:
            known = {row["path"]: row["sha256"] for row in conn.execute("SELECT path, sha256 FROM inventory")}
        recorded: list[dict[str, Any]] = []
        universe = set(observed) | (set(known) if paths is None else {p for p in paths if p in known})
        for rel in sorted(universe):
            now_entry = observed.get(rel)
            before = known.get(rel)
            after = now_entry.sha256 if now_entry else None
            if before == after:
                continue
            action = "create" if before is None else ("delete" if after is None else "edit")
            recorded.append(
                {
                    "path": rel,
                    "action": action,
                    "before_sha256": before,
                    "after_sha256": after,
                    "size": now_entry.size if now_entry else 0,
                }
            )
        counts = {
            "created": sum(1 for r in recorded if r["action"] == "create"),
            "modified": sum(1 for r in recorded if r["action"] == "edit"),
            "deleted": sum(1 for r in recorded if r["action"] == "delete"),
        }
        if not recorded:
            return {"change": None, **counts, "skipped": skipped, "head": self.cursor_head()}
        catalog = ctx.load_catalog_locked()
        entry_refs = entries_touching(catalog, {r["path"] for r in recorded})
        change_id = new_id("chg")
        with ctx.store.transaction() as conn:
            insert_event(
                conn,
                change_id=change_id,
                created_at=ctx.now(),
                kind="external_scan",
                attribution=attribution,
                files=recorded,
                entry_refs=entry_refs,
                details={"observed": "net change between the recorded inventory and the files on disk"},
            )
            row = conn.execute("SELECT * FROM changes WHERE change_id = ?", (change_id,)).fetchone()
            event = event_dict(conn, row)
        return {"change": event, **counts, "skipped": skipped, "head": event["sequence"]}

    def cursor_head(self) -> int:
        with self.ctx.store.reader() as conn:
            return self._head(conn)

    # -- used by authoring/history ----------------------------------------------------
    def link_commit(self, commit_id: str, committed: dict[str, str | None]) -> int:
        """Attach ``commit_id`` to change files whose recorded result is what was committed.

        ``committed`` maps path -> SHA-256 of the committed blob (``None`` for a
        deletion). A change file is linked when its ``after_sha256`` equals the
        committed value; earlier unlinked changes to the same path are subsumed
        by the commit's net result and linked too. No new content event is made.
        """
        linked = 0
        with self.ctx.store.transaction() as conn:
            for path, sha in committed.items():
                rows = conn.execute(
                    "SELECT rowid AS rid, after_sha256 FROM change_files WHERE path = ? AND commit_id IS NULL"
                    " ORDER BY rowid",
                    (path,),
                ).fetchall()
                last_match = None
                for index, row in enumerate(rows):
                    if row["after_sha256"] == sha:
                        last_match = index
                if last_match is None:
                    continue
                for row in rows[: last_match + 1]:
                    conn.execute("UPDATE change_files SET commit_id = ? WHERE rowid = ?", (commit_id, row["rid"]))
                    linked += 1
        return linked
