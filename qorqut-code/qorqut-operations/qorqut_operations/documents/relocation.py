"""Move project notes (and a Table's uploaded files) between projects without changing their identity.

A registered document is a ``documents`` row plus its file below the owning project's folder. Moving one changes the row's
``project_id`` and ``namespace``; its id, kind, relative path, content and metadata are unchanged, and the file moves to
the same relative path below the destination project's folder. Earlier versions in the shared history are copied to the
destination namespace (version numbers, authors and times unchanged); the originals are immutable and stay where they were
written. ``document_moves`` records each move with both paths.

Only ``project_owned_note`` documents move on their own. A ``data_upload`` moves only together with the Tables whose rows
came from it (``TableRelocation`` carries it); the owner's journal, Context documents, email messages and assets do not
move. Relationships follow ``relationships.relocation``, project links naming a moved document are retargeted.

The file is copied, never moved, inside the write transaction, so a rollback leaves the source untouched (a stray copy at
the destination is recognised by its content and reused by the next attempt). The source file is retired by ``sweep`` once
the move is committed: only when the recorded move is the document's latest, no row names the old path, and the source
still holds exactly the moved content. ``inspect`` and ``apply`` sweep first, and the host also sweeps after each move."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import uuid

from qorqut_operations.contracts import DomainError
from qorqut_operations.relationships import relocation as edges
from qorqut_operations.tasks.relocation import FORMAT, blocker, canonical, digest, existing_tables, marks, namespace

NOTE_KIND = "project_owned_note"
UPLOAD_KIND = "data_upload"
MAXIMUM_DOCUMENTS = 200
COUNTS = ("documents", "versions", "project_links", "historical_relationships")


def bare(value):
    return (value or "").removeprefix("sha256:")


def file_digest(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class DocumentInspection:
    source: str
    destination: str
    ids: tuple
    documents: list
    counts: dict
    blockers: list
    material: dict
    rows: dict = field(repr=False, default_factory=dict)

    @property
    def can_move(self) -> bool:
        return not self.blockers

    def fingerprint(self, principal_id) -> str:
        return hashlib.sha256(canonical({"format": FORMAT, "kind": "documents", "principal": principal_id,
                                         "material": self.material}).encode()).hexdigest()


class DocumentRelocation:
    """``root(project_row, relative_path) -> Path`` is the host's choice of the folder a registered path is relative to."""

    def __init__(self, root):
        if not callable(root):
            raise TypeError("Document relocation needs the host's document_root(project, relative_path)")
        self.root = root

    def location(self, uow, project_id, path) -> Path:
        project = uow.repository("projects").get(project_id, required=True)
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise DomainError("invalid_document", "A registered document path must stay inside its project")
        return Path(self.root(project, path)) / path

    @staticmethod
    def selection(document_ids):
        if (not isinstance(document_ids, (list, tuple)) or not 1 <= len(document_ids) <= MAXIMUM_DOCUMENTS
                or any(not isinstance(item, str) or not item for item in document_ids) or len(set(document_ids)) != len(document_ids)):
            raise DomainError("validation_error", f"Name 1 to {MAXIMUM_DOCUMENTS} unique document ids")
        return tuple(sorted(document_ids))

    # -- inspection ------------------------------------------------------------------------------------------------

    def inspect(self, ctx, uow, source, destination, document_ids, *, kinds=frozenset({NOTE_KIND}), with_tables=()) -> DocumentInspection:
        """What moving these documents carries and what prevents it. ``with_tables`` are Tables moving in the same transaction."""
        ids = self.selection(document_ids)
        if source is None:
            raise DomainError("validation_error", "Global documents cannot be moved; only a project's documents can")
        if not isinstance(destination, str) or not destination or destination == source:
            raise DomainError("validation_error", "Choose a project other than the document's current project")
        connection, tables = uow.connection, existing_tables(uow)
        target = connection.execute("SELECT id, slug, name, archived_at FROM projects WHERE id=?", (destination,)).fetchone()
        if target is None or target["archived_at"] is not None:
            raise DomainError("not_found", "Destination project is unavailable")
        self.sweep(uow)
        found = {row["id"]: dict(row) for row in connection.execute(
            "SELECT * FROM documents WHERE project_id=? AND id IN " + marks(ids) + " ORDER BY id", (source, *ids))}
        if set(ids) - set(found):
            raise DomainError("not_found", "Document does not exist in this project")
        blockers, counts = [], {"documents": len(ids)}
        material = {"source": source, "destination": destination, "destination_project": [target["id"], target["slug"], target["name"]],
                    "documents": [[row["id"], row["revision"], row["kind"], row["path"], bare(row["content_hash"])] for row in found.values()]}
        for row in found.values():
            if row["kind"] not in kinds:
                blockers.append(blocker("unsupported_document", "Only project notes can be moved (an upload moves with its table)", row["id"]))
                continue
            if row["availability"] != "ready":
                blockers.append(blocker("document_busy", "The document has an unfinished change", row["id"]))
            if file_digest(self.location(uow, source, row["path"])) != bare(row["content_hash"]):
                blockers.append(blocker("document_changed", "The document's file differs from its record; open and save it first", row["id"]))
            if connection.execute("SELECT 1 FROM documents WHERE namespace=? AND path=?", (destination, row["path"])).fetchone():
                blockers.append(blocker("path_exists", "The destination project already has a document at this path", row["id"]))
            else:
                existing = file_digest(self.location(uow, destination, row["path"]))
                if existing is not None and existing != bare(row["content_hash"]):
                    blockers.append(blocker("path_exists", "The destination project's folder already has a different file at this path", row["id"]))
            if row["kind"] == UPLOAD_KIND and "data_rows" in tables:
                using = connection.execute("SELECT DISTINCT table_id FROM data_rows WHERE project_id=? AND source_upload_id=?",
                                           (source, row["id"])).fetchall()
                if {item[0] for item in using} - set(with_tables):
                    blockers.append(blocker("upload_in_use", "A table that stays in this project still uses this upload", row["id"]))
        versions = connection.execute("SELECT document_id, version, content_sha256 FROM qdv_document_versions WHERE namespace=? AND document_id IN "
                                      + marks(ids) + " ORDER BY document_id, version", (namespace(source), *ids)).fetchall() \
            if "qdv_document_versions" in tables else []
        material["versions"] = [len(versions), digest(versions)]
        counts["versions"] = len(versions)
        if versions and connection.execute("SELECT 1 FROM qdv_document_versions WHERE namespace=? AND document_id IN " + marks(ids) + " LIMIT 1",
                                           (namespace(destination), *ids)).fetchone():
            blockers.append(blocker("history_exists", "The destination project already has history for one of these documents"))
        moving = {(kind, identity) for identity in ids for kind in edges.DOCUMENT_ENDS}
        carried, historical, edge_blockers, material["relationships"] = edges.classify(uow, source, destination, moving)
        blockers.extend(edge_blockers)
        counts["historical_relationships"] = historical
        links = connection.execute("SELECT * FROM project_item_links WHERE target_namespace=? AND target_id IN " + marks(ids)
                                   + " ORDER BY id", (namespace(source), *ids)).fetchall() if "project_item_links" in tables else []
        material["project_links"] = [len(links), digest(links)]
        counts["project_links"] = len(links)
        subscriptions = connection.execute("SELECT id FROM inbox_subscriptions WHERE project_id=? AND source_type='documents' AND source_id IN "
                                           + marks(ids) + " ORDER BY id", (source, *ids)).fetchall() if "inbox_subscriptions" in tables else []
        material["subscriptions"] = [len(subscriptions), digest(subscriptions)]
        blockers.sort(key=lambda item: (item["code"], item.get("task_id") or "", item["message"]))
        material["blockers"] = blockers
        shown = [{"id": row["id"], "kind": row["kind"], "path": row["path"], "title": (row["metadata_json"] or {}).get("title")
                  if isinstance(row["metadata_json"], dict) else None} for row in found.values()]
        return DocumentInspection(source, destination, ids, shown, counts, blockers, material,
                                  {"documents": found, "edges": carried, "links": links, "versions": versions, "store": tables})

    # -- application -----------------------------------------------------------------------------------------------

    def relocate(self, ctx, uow, source, destination, document_ids, token, *, move_id, at):
        """A standalone note move: recompute inside the write transaction, refuse any difference from the preview, apply."""
        uow.require_write()
        inspection = self.inspect(ctx, uow, source, destination, document_ids)
        if inspection.fingerprint(ctx.principal_id) != token:
            raise DomainError("revision_conflict", "The selection changed after the preview; preview it again")
        store = inspection.rows["store"]
        watermark = uow.connection.execute("SELECT COALESCE(MAX(id),0) FROM inbox_changes").fetchone()[0] if "inbox_changes" in store else None
        result = self.apply(ctx, uow, inspection, move_id=move_id, at=at)
        if watermark is not None:
            # A relocation is not new activity: the capture triggers' rows would otherwise announce every moved document.
            uow.connection.execute("DELETE FROM inbox_changes WHERE id>?", (watermark,))
        return result

    def apply(self, ctx, uow, inspection, *, move_id, at):
        """Move the inspected documents inside the caller's write transaction. The caller has compared the fingerprint."""
        uow.require_write()
        if not inspection.can_move:
            raise DomainError("move_blocked", "The move is blocked", details={"blockers": inspection.blockers})
        connection, source, destination = uow.connection, inspection.source, inspection.destination
        ids = inspection.ids
        for identity, row in inspection.rows["documents"].items():
            if connection.execute("UPDATE documents SET project_id=?, namespace=?, revision=revision+1, updated_at=? "
                                  "WHERE project_id=? AND id=? AND revision=?",
                                  (destination, destination, at, source, identity, row["revision"])).rowcount != 1:
                raise DomainError("revision_conflict", "A document changed during the move; preview it again")
            connection.execute("INSERT INTO document_moves(move_id,document_id,kind,source_project_id,destination_project_id,source_path,"
                               "destination_path,revision_before,actor_id,moved_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (move_id, identity, row["kind"], source, destination, row["path"], row["path"], row["revision"],
                                ctx.principal_id, at))
        if inspection.rows["versions"]:
            connection.execute("INSERT INTO qdv_document_versions(namespace,document_id,version,content,content_sha256,author_id,created_at,"
                               "reason,restored_from_version) SELECT ?,document_id,version,content,content_sha256,author_id,created_at,reason,"
                               "restored_from_version FROM qdv_document_versions WHERE namespace=? AND document_id IN " + marks(ids),
                               (namespace(destination), namespace(source), *ids))
        edges.carry(uow, source, destination, inspection.rows["edges"])
        for link in inspection.rows["links"]:
            if link["project_id"] == destination:
                connection.execute("DELETE FROM project_item_links WHERE id=?", (link["id"],))
            else:
                connection.execute("UPDATE project_item_links SET target_project_id=?, target_namespace=?, revision=revision+1, updated_at=? "
                                   "WHERE id=?", (destination, namespace(destination), at, link["id"]))
        if "inbox_subscriptions" in inspection.rows["store"]:
            connection.execute("UPDATE inbox_subscriptions SET project_id=? WHERE project_id=? AND source_type='documents' AND source_id IN "
                               + marks(ids), (destination, source, *ids))
        # Files last: every record change above could still fail. A copy is all this transaction does to the disk.
        for row in inspection.rows["documents"].values():
            self.copy(self.location(uow, source, row["path"]), self.location(uow, destination, row["path"]), bare(row["content_hash"]))
        return {"moved": [{"id": identity, "project_id": destination} for identity in ids],
                "counts": {name: inspection.counts.get(name, 0) for name in COUNTS}}

    @staticmethod
    def copy(origin: Path, target: Path, expected):
        data = origin.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise DomainError("revision_conflict", "A document's file changed during the move; preview it again")
        if file_digest(target) == expected:
            return  # an earlier attempt's copy, byte for byte
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{uuid.uuid4().hex}.moving")
        try:
            with open(staged, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(staged, target)  # refuses an existing file: nothing at the destination is ever overwritten
        except FileExistsError as exc:
            raise DomainError("revision_conflict", "A file appeared at the destination path; preview it again") from exc
        finally:
            staged.unlink(missing_ok=True)

    def sweep(self, uow):
        """Retire source files of committed moves. Safe to run at any time and repeatedly; returns how many were removed."""
        connection, removed = uow.connection, 0
        if "document_moves" not in existing_tables(uow):
            return 0
        latest = connection.execute("SELECT m.document_id, m.source_project_id, m.source_path, d.project_id, d.path, d.content_hash "
                                    "FROM document_moves m JOIN documents d ON d.id=m.document_id WHERE m.seq=(SELECT MAX(seq) FROM "
                                    "document_moves WHERE document_id=m.document_id) AND d.project_id=m.destination_project_id").fetchall()
        for row in latest:
            if connection.execute("SELECT 1 FROM documents WHERE namespace=? AND path=?", (row["source_project_id"], row["source_path"])).fetchone():
                continue
            origin = self.location(uow, row["source_project_id"], row["source_path"])
            if not origin.exists():
                continue
            current = file_digest(self.location(uow, row["project_id"], row["path"]))
            if current is not None and file_digest(origin) == current:
                origin.unlink()
                removed += 1
        return removed


__all__ = ["COUNTS", "DocumentInspection", "DocumentRelocation", "NOTE_KIND", "UPLOAD_KIND"]
