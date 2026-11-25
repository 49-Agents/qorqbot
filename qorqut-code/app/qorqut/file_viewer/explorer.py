"""Owner-only, read-only folder listings beneath the file viewer's operations root.

One directory per request, bounded response and memory, no recursive walk or
symlink following. Project badges use the same owner visibility as Signoffs.
Folders without an active accessible project have unknown counts, never zero.
"""
from __future__ import annotations

import base64
import heapq
import json
import os
import stat

from qorqut_operations.contracts import DomainError
from qorqut_operations.projects.hierarchy import SUBPROJECTS, folder_of
from qorqut_operations.signoffs import ABSENT
from qorqut_operations.tasks.schema import CLOSED_STATES

from . import files
from .paths import normalize_path
from .service import positive

PAGE_SIZE = 100
MAX_ENTRIES = 50_000


def query(items, roots):
    values = {}
    for key, value in items:
        if key not in {"path", "limit", "cursor", "hidden"} or key in values:
            raise DomainError("validation_error", "Unknown or duplicate directory query field")
        values[key] = value
    path = normalize_path(values.get("path", ""), roots, directory=True)
    raw_limit = values.get("limit", str(PAGE_SIZE))
    if len(raw_limit) > 3:
        raise DomainError("validation_error", "limit must be between 1 and 100")
    limit = positive(raw_limit, "limit", maximum=PAGE_SIZE)
    if values.get("hidden", "0") not in {"0", "1"}:
        raise DomainError("validation_error", "hidden must be 0 or 1")
    hidden, after = values.get("hidden") == "1", None
    if "cursor" in values:
        try:
            token = values["cursor"]
            if not 1 <= len(token) <= 12000:
                raise ValueError
            binding, after = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
            if binding != [path, hidden] or not isinstance(after, list) or len(after) != 3:
                raise ValueError
            if type(after[0]) is not int or after[0] not in (0, 1) or not all(isinstance(x, str) for x in after[1:]):
                raise ValueError
            after = tuple(after)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DomainError("validation_error", "Cursor does not match this folder or visibility") from exc
    return path, limit, hidden, after


def order(entry):
    return (0 if entry["kind"] == "directory" else 1, entry["name"].casefold(), entry["name"])


def listing(root, path, limit, hidden, after):
    total = skipped = scanned = 0

    def entries(descriptor):
        nonlocal total, skipped, scanned
        with os.scandir(descriptor) as children:
            for child in children:
                scanned += 1
                if scanned > MAX_ENTRIES:
                    raise DomainError("directory_too_large", "This folder has too many entries to browse (limit: 50,000)")
                if not hidden and child.name.startswith("."):
                    continue
                relative = path + "/" + child.name if path else child.name
                try:
                    normalize_path(relative, ())
                    # Surrogate-escaped POSIX names cannot be represented in JSON.
                    relative.encode("utf-8")
                except (DomainError, UnicodeError):
                    skipped += 1
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A concurrent rename/deletion; refresh reads the new directory.
                mode = info.st_mode
                kind = ("directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode)
                        else "symlink" if stat.S_ISLNK(mode) else "special")
                entry = {"name": child.name, "path": relative, "kind": kind,
                         "size": info.st_size if kind == "file" else None, "modified_at": files.iso(info.st_mtime)}
                total += 1
                if after is None or order(entry) > after:
                    yield entry

    try:
        with files.open_directory(root, path) as descriptor:
            page = heapq.nsmallest(limit + 1, entries(descriptor), key=order)
    except PermissionError:
        raise files.unreadable() from None
    next_cursor = None
    if len(page) > limit:
        page = page[:limit]
        next_cursor = base64.urlsafe_b64encode(json.dumps([[path, hidden], order(page[-1])]).encode()).decode().rstrip("=")
    return {"path": path, "entries": page, "total": total, "next_cursor": next_cursor, "skipped": skipped}


def project_metadata(runtime, scope, folders):
    """Badges keyed by project folder (below ``projects``: a slug, or ``<parent folder>/subprojects/<slug>``)."""
    if not folders:
        return {}
    result = {}
    with runtime.database.read() as uow:
        signoffs = runtime.signoffs
        context = signoffs._context(scope(None), uow)
        allowed = context.allowed_project_ids
        projects = [row for row in uow.connection.execute(
            "SELECT id,slug,name,config_json,folder FROM projects WHERE archived_at IS NULL")
            if row["id"] in allowed and folder_of(row) in folders]
        if not projects:
            return result
        ids = [row["id"] for row in projects]
        marks = ",".join("?" for _ in ids)
        closed_marks = ",".join("?" for _ in CLOSED_STATES)
        tasks = {row["project_id"]: row["n"] for row in uow.connection.execute(
            f"SELECT project_id,COUNT(*) n FROM tasks WHERE project_id IN ({marks}) AND archived=0 "
            f"AND status NOT IN ({closed_marks}) "
            "AND COALESCE(json_extract(fields_json,'$.\"$task\".task_type'),'task')='task' GROUP BY project_id",
            [*ids, *CLOSED_STATES])}
        clause, params = signoffs._visibility(context, uow)
        if signoffs.ports.management is not ABSENT:
            clause += " AND NOT (" + signoffs.ports.management.managed_input_sql + ")"
        pending = {row["review_project_id"]: row["n"] for row in uow.connection.execute(
            f"SELECT s.review_project_id,COUNT(*) n FROM signoffs s WHERE {clause} "
            f"AND s.review_project_id IN ({marks}) AND s.state='pending' "
            "AND s.superseded_by_id IS NULL GROUP BY s.review_project_id", [*params, *ids])}
        children = {row["parent_id"]: row["n"] for row in uow.connection.execute(
            f"SELECT parent_id,COUNT(*) n FROM projects WHERE parent_id IN ({marks}) AND archived_at IS NULL GROUP BY parent_id",
            ids)}
        for row in projects:
            config = json.loads(row["config_json"])
            description = config.get("one_liner", config.get("description", ""))
            result[folder_of(row)] = {"id": row["id"], "name": row["name"],
                "description": description if isinstance(description, str) else "",
                "open_tasks": tasks.get(row["id"], 0), "signoffs": pending.get(row["id"], 0),
                # Its active direct subprojects; other folders inside a project are not subprojects.
                "subprojects": children.get(row["id"], 0)}
    return result


def project_folder_of(path):
    """The deepest project folder (below ``projects``) a path is in, or ``None``: ``projects/a/...`` is in ``a`` and
    ``projects/a/subprojects/b/...`` in ``a/subprojects/b``."""
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "projects":
        return None
    depth = 2
    while len(parts) >= depth + 2 and parts[depth] == SUBPROJECTS:
        depth += 2
    return "/".join(parts[1:depth])


def directory(viewer, items):
    result = listing(viewer.root, *query(items, viewer.roots))
    path, entries = result["path"], result["entries"]
    project_folder = project_folder_of(path)
    folders = {project_folder} if project_folder else set()
    # The projects folder and a project's subprojects folder list project folders; each gets its badge.
    parts = path.split("/")
    listing_parent = (path + "/")[len("projects/"):] if path == "projects" or (
        len(parts) >= 3 and parts[-1] == SUBPROJECTS and project_folder_of("/".join(parts[:-1])) == "/".join(parts[1:-1])) else None
    if listing_parent is not None:
        folders.update(listing_parent + row["name"] for row in entries if row["kind"] == "directory")
    projects = project_metadata(viewer.runtime, viewer.scope, folders)
    result["project"] = projects.get(project_folder)
    if listing_parent is not None:
        for row in entries:
            if row["kind"] == "directory":
                row["project"] = projects.get(listing_parent + row["name"])
    return result
