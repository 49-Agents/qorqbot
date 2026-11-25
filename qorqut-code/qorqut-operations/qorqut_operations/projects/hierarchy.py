"""Project ancestry: subprojects are ordinary ``projects`` rows with a parent Project, to any depth.

``parent_id`` names the parent (NULL at the top level). A subproject's folder sits inside its parent's folder, at
``<parent folder>/subprojects/<slug>``; ``folder`` stores that location relative to the projects directory and is NULL for
a top-level Project, whose folder is its slug. ``archived_with`` names the Project whose archiving also archived this one
(NULL when it was archived on its own or is not archived), so restoring that Project restores exactly what went with it.

Every walk is bounded by ``MAX_DEPTH`` and stops at a repeated identity, so a damaged store can never loop.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

MAX_DEPTH = 50
SUBPROJECTS = "subprojects"


def folder_of(row) -> str:
    """The Project's folder relative to the projects directory: its slug at the top level, else its stored ``folder``."""
    keys = row.keys() if hasattr(row, "keys") else ()
    folder = row["folder"] if "folder" in keys else None
    return folder if folder else row["slug"]


def child_folder(parent_row, slug) -> str:
    """Where a subproject named ``slug`` sits inside ``parent_row``'s folder."""
    return f"{folder_of(parent_row)}/{SUBPROJECTS}/{slug}"


def stored_folder(parent_row, slug):
    """The ``folder`` column value for a Project with this parent (``None`` at the top level)."""
    return None if parent_row is None else child_folder(parent_row, slug)


def ancestors(connection, project_id) -> list:
    """The ancestor rows of ``project_id``, root first; empty for a top-level Project or an unknown id."""
    chain, seen = [], {project_id}
    row = connection.execute("SELECT parent_id FROM projects WHERE id=?", (project_id,)).fetchone()
    parent = row["parent_id"] if row is not None else None
    while parent and parent not in seen and len(seen) <= MAX_DEPTH:
        found = connection.execute("SELECT * FROM projects WHERE id=?", (parent,)).fetchone()
        if found is None:
            break
        seen.add(parent)
        chain.append(found)
        parent = found["parent_id"]
    return chain[::-1]


def lineage(connection, project_id) -> list:
    """``ancestors`` followed by the Project's own row: the path from the root down to it."""
    own = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return [*ancestors(connection, project_id), *([own] if own is not None else [])]


def children(connection, project_id, *, include_archived=True) -> list:
    """The direct subproject rows of ``project_id``, by name."""
    archived = "" if include_archived else " AND archived_at IS NULL"
    return connection.execute(f"SELECT * FROM projects WHERE parent_id=?{archived} ORDER BY name, id", (project_id,)).fetchall()


def descendants(connection, project_id) -> list:
    """Every subproject row below ``project_id`` at any depth, parents before their children."""
    found, seen, frontier, depth = [], {project_id}, [project_id], 0
    while frontier and depth < MAX_DEPTH:
        depth += 1
        marks = ",".join("?" for _ in frontier)
        rows = [row for row in connection.execute(
            f"SELECT * FROM projects WHERE parent_id IN ({marks}) ORDER BY name, id", frontier) if row["id"] not in seen]
        seen.update(row["id"] for row in rows)
        found.extend(rows)
        frontier = [row["id"] for row in rows]
    return found


def path_view(connection, project_id) -> list:
    """``[{id, name, slug}]`` from the root Project down to and including ``project_id``."""
    return [{"id": row["id"], "name": row["name"], "slug": row["slug"]} for row in lineage(connection, project_id)]


def require_parent(connection, parent_id, *, moving=None):
    """The parent row a Project may be placed under, or a refusal.

    The parent must exist and not be archived; when ``moving`` names a Project, the parent may be neither that Project nor
    one of its descendants (that would make a cycle)."""
    row = connection.execute("SELECT * FROM projects WHERE id=?", (parent_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "The parent project does not exist")
    if row["archived_at"] is not None:
        raise DomainError("project_archived", "The parent project is archived; restore it first")
    if moving is not None:
        if parent_id == moving or any(found["id"] == moving for found in ancestors(connection, parent_id)):
            raise DomainError("validation_error", "A project cannot be placed under itself or one of its own subprojects")
        if len(ancestors(connection, parent_id)) + 1 >= MAX_DEPTH:
            raise DomainError("validation_error", "Projects cannot be nested this deep")
    elif len(ancestors(connection, parent_id)) + 1 >= MAX_DEPTH:
        raise DomainError("validation_error", "Projects cannot be nested this deep")
    return row
