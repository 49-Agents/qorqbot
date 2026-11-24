"""Move whole Tables between projects without changing any Table's identity, as a Task move does for Tasks.

A Table (general or CRM) is owned by one project. Moving it changes the ``project_id`` of the Table row and of every row that
belongs to it - its data rows, its history captures, its CRM memberships, its discussion, follows and subscriptions - in one
transaction. Ids, contents, revisions of the rows, history paths and Git commits are unchanged; a CRM membership keeps naming
its Task wherever that Task is. Nothing is copied, recreated or merged.

``TableRelocation.inspect`` reads and returns an ``Inspection``: what travels, the concrete reasons the move cannot be done
(``blockers``) and a fingerprint of that exact state. ``apply`` recomputes it inside the write transaction, refuses a changed
state, and then arms the unit's relocation guard for exactly these Tables (``table:<id>``) and this scope pair, so that the two
scope guards that admit a move (a Table's discussion entries and its CRM memberships) accept these rows and no others.

Relationships follow the Task rule. An edge between two moved Tables travels, and so does an edge whose other end already sits
in the destination (a Task or Table moved there earlier: the edge reunites them). Any other edge stays in the source scope as
written, as history; a reviewed edge stays too, since its Context review decisions cannot be rewritten. Project links that
name a moved Table are retargeted (or dropped when the destination itself is the owner).

History captures keep the path they were committed under, so earlier revisions stay readable where they are; the next revision
is captured under the destination project. A Table whose latest revision is not yet saved to Git cannot move until it is. A
file the Table's rows were imported from (a ``data_upload`` document) moves with it through ``DocumentRelocation``, provided no
Table staying behind uses it.

``table_moves`` is the append-only audit and the input of ``record_moves.moved_location``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import hashlib

from qorqut_operations.contracts import DomainError
from qorqut_operations.documents.relocation import UPLOAD_KIND
from qorqut_operations.relationships import relocation as edges
from qorqut_operations.tasks.relocation import FORMAT, blocker, canonical, digest, existing_tables, marks, namespace

MAXIMUM_TABLES = 100
MAXIMUM_ROWS = 200_000
MAXIMUM_BLOCKERS = 100
# The relocation guard key of a Table (a Task's key is its bare id); the scope triggers pass the same prefix.
GUARD_PREFIX = "table:"
COUNTS = ("tables", "rows", "uploads", "versions", "crm_entries", "comments", "project_links", "historical_relationships")


@dataclass(frozen=True)
class TableMove:
    """The exact move an adapter works for: scopes and the Table ids."""

    source: str
    destination: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class TableInspection:
    move: TableMove
    tables: list
    counts: dict
    blockers: list
    material: dict
    rows: dict = field(repr=False, default_factory=dict)

    @property
    def can_move(self) -> bool:
        return not self.blockers

    def fingerprint(self, principal_id) -> str:
        return hashlib.sha256(canonical({"format": FORMAT, "kind": "tables", "principal": principal_id,
                                         "material": self.material}).encode()).hexdigest()


class TableRelocation:
    """The Table move helper. ``adapters`` are the host's references to a Table (private conversations, Focus, ...)."""

    def __init__(self, adapters: Sequence = (), *, documents=None):
        self.adapters, self.documents = tuple(adapters), documents

    @staticmethod
    def selection(table_ids) -> tuple[str, ...]:
        if (not isinstance(table_ids, (list, tuple)) or not 1 <= len(table_ids) <= MAXIMUM_TABLES
                or any(not isinstance(item, str) or not item for item in table_ids) or len(set(table_ids)) != len(table_ids)):
            raise DomainError("validation_error", f"Name 1 to {MAXIMUM_TABLES} unique Table ids")
        return tuple(sorted(table_ids))

    def inspect(self, ctx, uow, source, destination, table_ids) -> TableInspection:
        ids = self.selection(table_ids)
        if source is None:
            raise DomainError("validation_error", "A Global table cannot be moved; only a project's tables can")
        if not isinstance(destination, str) or not destination or destination == source:
            raise DomainError("validation_error", "Choose a project other than the table's current project")
        connection = uow.connection
        target = connection.execute("SELECT id, slug, name, archived_at FROM projects WHERE id=?", (destination,)).fetchone()
        if target is None or target["archived_at"] is not None:
            raise DomainError("not_found", "Destination project is unavailable")
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (source,)).fetchone() is None:
            raise DomainError("not_found", "Source project is unavailable")
        found = {row["id"]: dict(row) for row in connection.execute(
            "SELECT * FROM data_tables WHERE project_id=? AND id IN " + marks(ids) + " ORDER BY id", (source, *ids))}
        if set(ids) - set(found):
            raise DomainError("not_found", "Table does not exist in this project")
        move, tables = TableMove(source, destination, ids), existing_tables(uow)
        blockers, counts, material = [], {}, {"source": source, "destination": destination,
                                               "destination_project": [target["id"], target["slug"], target["name"]]}
        material["tables"] = [[row["id"], row["revision"], row["updated_at"], row["table_kind"]] for row in found.values()]
        counts["tables"] = len(ids)
        for row in connection.execute("SELECT id FROM data_tables WHERE project_id=? AND id IN " + marks(ids), (destination, *ids)):
            blockers.append(blocker("table_id_exists", "The destination project already has a table with this id", row["id"]))

        def owned(table, extra=""):
            return connection.execute(f"SELECT * FROM {table} WHERE project_id=? AND table_id IN " + marks(ids) + extra
                                      + " ORDER BY id", (source, *ids)).fetchall()

        data_rows, versions = owned("data_rows"), owned("table_versions")
        entries = owned("crm_entries") if "crm_entries" in tables else []
        if len(data_rows) + len(entries) > MAXIMUM_ROWS:
            raise DomainError("result_too_large", f"These tables hold more than {MAXIMUM_ROWS} rows; move fewer at once")
        material["rows"] = [len(data_rows), digest([[row["id"], row["revision"]] for row in data_rows])]
        material["versions"] = [len(versions), digest([[row["id"], row["git_commit"]] for row in versions])]
        material["entries"] = [len(entries), digest([[row["id"], row["revision"]] for row in entries])]
        counts.update(rows=len(data_rows), versions=len(versions), crm_entries=len(entries))
        for row in versions:
            if row["git_commit"] is None:
                blockers.append(blocker("history_pending", "The table's latest change is still being saved to its history; "
                                        "try again once it is saved", row["table_id"]))
        uploads, carried_uploads = sorted({row["source_upload_id"] for row in data_rows if row["source_upload_id"]}), None
        if uploads and self.documents is None:
            for table_id in sorted({row["table_id"] for row in data_rows if row["source_upload_id"]}):
                blockers.append(blocker("table_upload", "Rows of this table came from an uploaded file, which cannot move here", table_id))
        elif uploads:
            # The uploaded files travel with the tables made from them, in the same transaction.
            carried_uploads = self.documents.inspect(ctx, uow, source, destination, uploads, kinds=frozenset({UPLOAD_KIND}), with_tables=ids)
            blockers.extend(carried_uploads.blockers)
            material["uploads"] = carried_uploads.material
        counts["uploads"] = len(uploads)
        for table, label in (("data_rows", "row"), ("table_versions", "history capture"), ("crm_entries", "membership")):
            rows_here = {"data_rows": data_rows, "table_versions": versions, "crm_entries": entries}[table]
            keys = [row["id"] for row in rows_here]
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                if table in tables and connection.execute(f"SELECT 1 FROM {table} WHERE project_id=? AND id IN " + marks(chunk)
                                                          + " LIMIT 1", (destination, *chunk)).fetchone():
                    blockers.append(blocker(f"{table}_id_exists", f"The destination project already has a table {label} with the same id"))
                    break

        # Discussion, follows and subscriptions of the Tables follow them; ids and contents are unchanged.
        messages = connection.execute("SELECT item_id, id, event_id, created_at, reference_namespace FROM thread_messages WHERE project_id=? "
                                      "AND item_type='table' AND item_id IN " + marks(ids) + " ORDER BY item_id, id", (source, *ids)).fetchall()
        material["messages"] = [len(messages), digest(messages)]
        counts["comments"] = len(messages)
        if messages and connection.execute("SELECT 1 FROM thread_messages WHERE project_id=? AND id IN (SELECT id FROM thread_messages "
                                           "WHERE project_id=? AND item_type='table' AND item_id IN " + marks(ids) + ") LIMIT 1",
                                           (destination, source, *ids)).fetchone():
            blockers.append(blocker("message_id_exists", "The destination project already has a discussion entry with the same id"))
        follows = connection.execute("SELECT * FROM thread_follows WHERE project_id=? AND item_type='table' AND item_id IN " + marks(ids)
                                     + " ORDER BY item_id, principal_id", (source, *ids)).fetchall()
        material["follows"] = [len(follows), digest(follows)]
        if connection.execute("SELECT 1 FROM thread_follows WHERE project_id=? AND item_type='table' AND item_id IN " + marks(ids) + " LIMIT 1",
                              (destination, *ids)).fetchone():
            blockers.append(blocker("follow_exists", "The destination project already has follows for the same tables"))
        subscriptions = connection.execute("SELECT id, agent_id, source_id FROM inbox_subscriptions WHERE project_id=? AND source_type='data_tables' "
                                           "AND source_id IN " + marks(ids) + " ORDER BY id", (source, *ids)).fetchall() if "inbox_subscriptions" in tables else []
        material["subscriptions"] = [len(subscriptions), digest(subscriptions)]
        if subscriptions and connection.execute("SELECT 1 FROM inbox_subscriptions WHERE project_id=? AND source_type='data_tables' AND "
                                                "source_id IN " + marks(ids) + " LIMIT 1", (destination, *ids)).fetchone():
            blockers.append(blocker("subscription_exists", "The destination project already has subscriptions for the same tables"))

        # Relationships: see the module text.
        carried, historical, edge_blockers, material["relationships"] = edges.classify(uow, source, destination,
                                                                                     {("table", identity) for identity in ids})
        blockers.extend(edge_blockers)
        counts["historical_relationships"] = historical

        links = connection.execute("SELECT * FROM project_item_links WHERE target_type='table' AND target_namespace=? AND target_id IN "
                                   + marks(ids) + " ORDER BY id", (namespace(source), *ids)).fetchall() if "project_item_links" in tables else []
        material["project_links"] = [len(links), digest(links)]
        counts["project_links"] = len(links)

        for adapter in self.adapters:
            report = adapter.inspect(uow, move)
            material["adapter:" + adapter.name] = report.material
            for name, value in report.counts.items():
                counts[name] = counts.get(name, 0) + value
            blockers.extend(dict(item) for item in report.blockers)
        blockers.sort(key=lambda item: (item["code"], item.get("task_id") or "", item["message"]))
        if len(blockers) > MAXIMUM_BLOCKERS:
            more = len(blockers) - MAXIMUM_BLOCKERS
            blockers = [*blockers[:MAXIMUM_BLOCKERS], blocker("more_blockers", f"{more} more reasons are not listed")]
        material["blockers"] = blockers
        shown = [{"id": row["id"], "title": row["title"], "table_kind": row["table_kind"], "archived": bool(row["archived"])}
                 for row in found.values()]
        return TableInspection(move, shown, counts, blockers, material,
                               {"tables": found, "edges": carried, "links": links, "store": tables,
                                                       "uploads": carried_uploads})

    def apply(self, ctx, uow, source, destination, table_ids, token, *, move_id, at):
        """Recompute inside the write transaction, refuse any difference from the preview, then move everything atomically."""
        uow.require_write()
        inspection = self.inspect(ctx, uow, source, destination, table_ids)
        if inspection.fingerprint(ctx.principal_id) != token:
            raise DomainError("revision_conflict", "The selection changed after the preview; preview it again")
        if not inspection.can_move:
            raise DomainError("move_blocked", "The move is blocked", details={"blockers": inspection.blockers})
        move, connection, store = inspection.move, uow.connection, inspection.rows["store"]
        ids = move.ids
        watermark = connection.execute("SELECT COALESCE(MAX(id),0) FROM inbox_changes").fetchone()[0] if "inbox_changes" in store else None
        counts = {name: inspection.counts.get(name, 0) for name in COUNTS}
        params = (destination, source)
        with uow.relocating(source, destination, [GUARD_PREFIX + identity for identity in ids]):
            for identity, row in inspection.rows["tables"].items():
                if connection.execute("UPDATE data_tables SET project_id=?, revision=revision+1, updated_at=? WHERE project_id=? AND id=? AND revision=?",
                                      (destination, at, source, identity, row["revision"])).rowcount != 1:
                    raise DomainError("revision_conflict", "A table changed during the move; preview it again")
            for table in ("data_rows", "table_versions", *(("crm_entries",) if "crm_entries" in store else ())):
                connection.execute(f"UPDATE {table} SET project_id=? WHERE project_id=? AND table_id IN " + marks(ids), (*params, *ids))
            # A message's references name records of the scope it was written in: the first relocation pins that scope.
            connection.execute("UPDATE thread_messages SET project_id=?, reference_namespace=COALESCE(reference_namespace, ?) WHERE project_id=? "
                               "AND item_type='table' AND item_id IN " + marks(ids), (destination, namespace(source), source, *ids))
            connection.execute("UPDATE thread_follows SET project_id=? WHERE project_id=? AND item_type='table' AND item_id IN " + marks(ids),
                               (*params, *ids))
            if "inbox_subscriptions" in store:
                connection.execute("UPDATE inbox_subscriptions SET project_id=? WHERE project_id=? AND source_type='data_tables' AND source_id IN "
                                   + marks(ids), (*params, *ids))
            edges.carry(uow, source, destination, inspection.rows["edges"])
            for link in inspection.rows["links"]:
                if link["project_id"] == destination:
                    connection.execute("DELETE FROM project_item_links WHERE id=?", (link["id"],))
                else:
                    connection.execute("UPDATE project_item_links SET target_project_id=?, target_namespace=?, revision=revision+1, updated_at=? "
                                       "WHERE id=?", (destination, namespace(destination), at, link["id"]))
            if inspection.rows["uploads"] is not None:
                self.documents.apply(ctx, uow, inspection.rows["uploads"], move_id=move_id, at=at)
            for adapter in self.adapters:
                for name, value in adapter.apply(uow, move, at).items():
                    counts[name] = counts.get(name, 0) + value
            for identity, row in inspection.rows["tables"].items():
                connection.execute("INSERT INTO table_moves(move_id,table_id,source_project_id,destination_project_id,revision_before,actor_id,"
                                   "moved_at) VALUES(?,?,?,?,?,?,?)", (move_id, identity, source, destination, row["revision"], ctx.principal_id, at))
            if watermark is not None:
                # A relocation is not new activity: the capture triggers' rows would otherwise announce every moved row.
                connection.execute("DELETE FROM inbox_changes WHERE id>?", (watermark,))
            self.verify(uow, move, inspection)
        return {"moved": [{"id": identity, "project_id": destination} for identity in ids], "counts": counts}

    @staticmethod
    def verify(uow, move, inspection):
        connection, ids = uow.connection, move.ids
        left = sum(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id=? AND table_id IN " + marks(ids), (move.source, *ids)).fetchone()[0]
                   for table in ("data_rows", "table_versions", *(("crm_entries",) if "crm_entries" in inspection.rows["store"] else ())))
        left += connection.execute("SELECT COUNT(*) FROM data_tables WHERE project_id=? AND id IN " + marks(ids), (move.source, *ids)).fetchone()[0]
        left += connection.execute("SELECT COUNT(*) FROM thread_messages WHERE project_id=? AND item_type='table' AND item_id IN " + marks(ids),
                                   (move.source, *ids)).fetchone()[0]
        if left:
            raise DomainError("relocation_failed", "The move left records behind; nothing was changed")
        for table in ("data_rows", "table_versions", "crm_entries"):
            if table in inspection.rows["store"] and connection.execute(f'PRAGMA foreign_key_check("{table}")').fetchone() is not None:
                raise DomainError("relocation_failed", "The move would leave a dangling reference; nothing was changed")


__all__ = ["COUNTS", "GUARD_PREFIX", "TableInspection", "TableMove", "TableRelocation"]
