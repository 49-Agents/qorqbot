"""Explicit Project links: the one cross-scope association store of a Project record.

A Project owns the operational records whose own scope is that Project. A link is a *separate*,
explicit association with one existing readable item in any scope: another Project, a Global
record, or a foreign project's record. It never changes the target's owning scope, copies no
operational content, grants no access and never writes into the source. Reading, linking or
unlinking creates no conversation message.

This module owns the storage (``project_item_links``) and the neutral persistence rules only:

* the owning scope is the Project UUID (``business_id``);
* the target keeps its own scope (``target_business_id``, ``NULL`` for Global);
* the target namespace is normalized and non-null (``global`` / ``business:<uuid>``), so the
  uniqueness rule ``(project, target type, target namespace, canonical target id)`` cannot be
  bypassed by spelling the scope differently;
* the canonical target id is supplied by the caller: Actions are canonicalized to their
  ``action_key`` before they reach this table, so a bare and a canonical ID are one target;
* creation is idempotent and an existing association is returned unchanged;
* removal is an exact revision CAS of that one row and touches nothing else.

Who may read what, which item kinds exist and whether a target is currently accessible are the
host's: it resolves and authorizes every target through its neutral item resolver before calling
this service. Nothing here grants access to a target or to a Project.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from qorqut_operations.application.plans import timestamp, utc_clock
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

TABLE = "project_item_links"
SCOPE = "business_id"
TARGET_TYPE_MAX = 64
TARGET_ID_MAX = 300

SCHEMA = (
    """CREATE TABLE project_item_links (
 id TEXT PRIMARY KEY CHECK(length(id)=36),
 business_id TEXT NOT NULL REFERENCES businesses(id),
 target_type TEXT NOT NULL CHECK(length(target_type) BETWEEN 1 AND 64),
 target_id TEXT NOT NULL CHECK(length(target_id) BETWEEN 1 AND 300),
 target_business_id TEXT REFERENCES businesses(id),
 target_namespace TEXT NOT NULL CHECK(length(target_namespace) BETWEEN 1 AND 300),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(business_id, target_type, target_namespace, target_id)
)""",
    """CREATE INDEX project_item_links_target ON project_item_links(target_type, target_namespace, target_id)""",
)

TABLES = {TABLE: TableSpec(("business_id", "id"), SCOPE)}
# The association is append-only: a target, its scope and its creator are its identity. Unlink deletes the one row.
IMMUTABLE_FIELDS = {TABLE: frozenset({"business_id", "target_type", "target_id", "target_business_id",
                                      "target_namespace", "created_by", "created_at"})}


def storage_contribution() -> StorageContribution:
    """The link table, its index and its repository specification, for the host's trusted assembly."""
    return StorageContribution("project-links", TABLES, immutable_fields=IMMUTABLE_FIELDS, schema=SCHEMA)


def target_namespace(target_business_id) -> str:
    """The one normalized namespace of a target's own scope: never empty, never the Project's scope."""
    return "global" if target_business_id is None else "business:" + target_business_id


@dataclass(frozen=True)
class ProjectTarget:
    """One canonical target association: its neutral item type, its canonical id and its own scope."""

    type: str
    id: str
    business_id: str | None

    def __post_init__(self):
        if not isinstance(self.type, str) or not 1 <= len(self.type) <= TARGET_TYPE_MAX:
            raise ValueError("A link target needs a bounded neutral item type")
        if not isinstance(self.id, str) or not 1 <= len(self.id) <= TARGET_ID_MAX:
            raise ValueError("A link target needs a bounded canonical identity")
        if self.business_id is not None and (not isinstance(self.business_id, str) or not self.business_id):
            raise ValueError("A link target's own scope is a project UUID or NULL for Global")

    @property
    def namespace(self) -> str:
        return target_namespace(self.business_id)

    def as_dict(self) -> dict:
        return {"type": self.type, "id": self.id, "business_id": self.business_id}

    @classmethod
    def from_item(cls, item: Mapping, canonical_id: str | None = None) -> "ProjectTarget":
        """One canonical target from a resolved neutral item; ``canonical_id`` replaces an alias (a bare Action ID)."""
        if not isinstance(item, Mapping):
            raise DomainError("validation_error", "A link target is a typed existing item")
        return cls(type=item["type"], id=canonical_id or item["id"], business_id=item.get("business_id"))


def view(row: Mapping) -> dict:
    """The public link of one stored association. No target label or content: the caller resolves that itself."""
    return {"id": row["id"], "project_id": row["business_id"], "revision": row["revision"],
            "item": {"type": row["target_type"], "id": row["target_id"], "business_id": row["target_business_id"]},
            "target_namespace": row["target_namespace"], "created_by": row["created_by"], "created_at": row["created_at"]}


class ProjectItemLinks:
    """The explicit link store of one Project: persistence, idempotent creation and exact-revision removal.

    The host calls it only after it has resolved the Project and the target through its own authorized readers.
    """

    def __init__(self, application, *, clock=utc_clock):
        self.application, self.clock = application, clock

    # -- reads -----------------------------------------------------------------------------------------------------

    def rows(self, ctx, project_id, uow) -> list[dict]:
        """Every explicit association of one Project, oldest first, read in that Project's own scope."""
        return [_row(row) for row in uow.connection.execute(
            'SELECT * FROM project_item_links WHERE business_id=? ORDER BY created_at,id', (project_id,))]

    def index(self, ctx, project_id, uow) -> dict[tuple[str, str, str | None], dict]:
        """The stored associations keyed by canonical target identity, for one inventory page."""
        return {(row["target_type"], row["target_id"], row["target_business_id"]): row
                for row in self.rows(ctx, project_id, uow)}

    def get(self, ctx, project_id, link_id, uow) -> dict | None:
        row = uow.connection.execute('SELECT * FROM project_item_links WHERE business_id=? AND id=?',
                                     (project_id, link_id)).fetchone()
        return _row(row) if row is not None else None

    # -- writes ----------------------------------------------------------------------------------------------------

    def find(self, ctx, project_id, target: ProjectTarget, uow) -> dict | None:
        row = uow.connection.execute(
            'SELECT * FROM project_item_links WHERE business_id=? AND target_type=? AND target_namespace=? AND target_id=?',
            (project_id, target.type, target.namespace, target.id)).fetchone()
        return _row(row) if row is not None else None

    def link(self, ctx, project_id, target: ProjectTarget, uow) -> tuple[dict, bool]:
        """Create the one association, or return the existing one: ``(row, already_present)``.

        The insert is ``INSERT OR IGNORE`` on the uniqueness rule, so concurrent first links converge on one row.
        """
        uow.require_write()
        existing = self.find(ctx, project_id, target, uow)
        if existing is not None:
            return existing, True
        now = _timestamp(self.clock)
        uow.connection.execute(
            'INSERT OR IGNORE INTO project_item_links'
            '(id,business_id,target_type,target_id,target_business_id,target_namespace,revision,created_by,created_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,1,?,?,?)',
            (new_id(), project_id, target.type, target.id, target.business_id, target.namespace,
             ctx.principal_id, now, now))
        row = self.find(ctx, project_id, target, uow)
        if row is None:
            raise DomainError("revision_conflict", "The association changed while it was being created; read it again")
        return row, False

    def unlink(self, ctx, project_id, link_id, expected_revision, uow) -> bool:
        """Delete exactly this association with its current revision; the target itself is never touched."""
        uow.require_write()
        if type(expected_revision) is not int or expected_revision < 1:
            raise DomainError("validation_error", "expected_revision is required to unlink")
        row = self.get(ctx, project_id, link_id, uow)
        if row is None:
            raise DomainError("not_found", "No such association in this project")
        if row["revision"] != expected_revision:
            raise DomainError("revision_conflict", "The association changed; read the project again",
                              current_revision=row["revision"])
        cursor = uow.connection.execute('DELETE FROM project_item_links WHERE business_id=? AND id=? AND revision=?',
                                        (project_id, link_id, expected_revision))
        if cursor.rowcount != 1:
            raise DomainError("revision_conflict", "The association changed; read the project again")
        return True


def _row(row) -> dict:
    return dict(row) if row is not None else None


def _timestamp(clock):
    return timestamp(clock)


__all__ = ["IMMUTABLE_FIELDS", "ProjectItemLinks", "ProjectTarget", "SCHEMA", "TABLE", "TABLES", "storage_contribution",
           "target_namespace", "view"]