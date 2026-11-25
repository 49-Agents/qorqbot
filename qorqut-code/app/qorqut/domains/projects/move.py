"""``project_move``: place a Project under another Project (making it a subproject) or back at the top level.

The installation owner and the exact configured active Ordubashy may move any active Project they currently reach, at any
time. The new parent must be an active Project and may be neither the Project itself nor one of its subprojects. The
Project's folder moves on disk with it, inside the new parent's folder (``<parent>/subprojects/<slug>``) or back to
``projects/<slug>``; its own subprojects' folders sit inside it and move along. The rows (``parent_id``, ``folder`` and
``workspace`` of the Project and of every subproject below it) change in the same transaction, and the folder is renamed
last, without replacing anything at the destination: if the rename fails, the transaction is rolled back and nothing in
the store changes. Tasks, documents and every other record keep their Project; nothing else moves.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, TEXT, PlanBuilder, apply_records, ref, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.projects import hierarchy
from qorqut_operations.storage.directories import move_directory

from qorqut.composition.locations import project_root

from . import access

MOVE = "project_move"


class ProjectMove:
    def __init__(self, application, paths):
        self.application, self.paths = application, paths

    def register(self, registry=None):
        registry = registry or self.application.registry
        registry.register(OperationDescriptor(MOVE, schema({
                "project_id": {**UUID, "description": instruction_text('move-the-project-to-move-with-its-subprojects')},
                "parent_project_id": {"anyOf": [UUID, {"type": "null"}],
                                      "description": instruction_text('move-the-new-parent-project-or-null-for-the-top')},
                "rationale": TEXT}, ("project_id", "parent_project_id")),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", supports_signoff=False,
            description=instruction_text('move-owner-and-configured-ordubashy-place-a-project-under-another'),
            prepare=self.prepare, apply=self.apply, validate_targets=self.validate))
        self.application.project_move = self
        return self

    # -- authority and the destination ---------------------------------------------------------------------------

    def checked(self, ctx, args, uow):
        """``(current, row, parent)`` once the identity gate, the Project's reach and the destination are proven."""
        if ctx.project_id is not None:
            raise DomainError("forbidden", "Moving a project is a Global operation; do not select a project")
        current = self.application.current_context(ctx, uow)
        access.role(current, uow)
        row = access.project_row(current, args["project_id"], uow)
        parent_id = args.get("parent_project_id")
        parent = None
        if parent_id is not None:
            if not access.project_participant(current, parent_id, uow):
                raise DomainError("not_found", "The parent project does not exist or is outside your current access")
            parent = hierarchy.require_parent(uow.connection, parent_id, moving=row["id"])
            height = max((len(hierarchy.ancestors(uow.connection, below["id"])) - len(hierarchy.ancestors(uow.connection, row["id"]))
                          for below in hierarchy.descendants(uow.connection, row["id"])), default=0)
            if len(hierarchy.ancestors(uow.connection, parent_id)) + 1 + height >= hierarchy.MAX_DEPTH:
                raise DomainError("validation_error", "Projects cannot be nested this deep")
        if (row.get("parent_id") or None) == parent_id:
            raise DomainError("validation_error", "The project is already there")
        return current, row, parent

    def root(self, row, folder=None):
        return project_root(self.paths, {"slug": row["slug"], "folder": folder if folder is not None else row.get("folder")})

    # -- phases --------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        _, row, parent = self.checked(ctx, args, uow)
        old = hierarchy.folder_of(row)
        new = hierarchy.stored_folder(parent, row["slug"])
        destination = self.root(row, new or "")
        if destination.exists() or destination.is_symlink():
            raise DomainError("revision_conflict", "Something already exists where the project's folder would move to")
        if not self.root(row).is_dir():
            raise DomainError("invalid_configuration", "The project's folder is missing")
        where = ("under " + parent["name"]) if parent is not None else "to the top level"
        builder = PlanBuilder(ctx, MOVE, args, f"Move project {row['name']} {where}", namespace="global",
                              affected_scope={"kind": "project", "project_id": row["id"]})
        if parent is not None:
            builder.expect(ref("projects", parent["id"], None, "global"), uow.repository("projects").get(parent["id"]))
        builder.record("projects", row["id"], row, {"parent_id": parent["id"] if parent is not None else None, "folder": new,
                       "workspace": str(destination)}, project_id=None, namespace="global", label=row["name"])
        below = hierarchy.descendants(uow.connection, row["id"])
        for found in below:
            child = uow.repository("projects").get(found["id"], required=True)
            moved = (new or row["slug"]) + hierarchy.folder_of(child)[len(old):]
            builder.record("projects", child["id"], child, {"folder": moved, "workspace": str(self.root(child, moved))},
                           project_id=None, namespace="global", label=child["name"])
        builder.effects.extend([
            f"Move {row['name']} {where}" + (f", with its {len(below)} subproject{'s' if len(below) != 1 else ''}." if below else "."),
            f"Move its folder from {self.relative(self.root(row))} to {self.relative(destination)}.",
            "Its Tasks, documents, tables and every other record stay in the project; nothing else moves."])
        return builder.build()

    def relative(self, path):
        return path.relative_to(self.paths.operations).as_posix()

    def validate(self, ctx, plan, uow):
        self.checked(ctx, plan.arguments, uow)

    def apply(self, ctx, plan, uow):
        self.checked(ctx, plan.arguments, uow)
        project_id = plan.arguments["project_id"]
        before = uow.repository("projects").get(project_id, required=True)
        source = self.root(before)
        apply_records(ctx, plan, uow)
        after = uow.repository("projects").get(project_id, required=True)
        destination = self.root(after)
        # Last, so a refused or failed rename rolls the row changes back with it.
        move_directory(source, destination)
        return {"project_id": project_id, "parent_id": after["parent_id"], "revision": after["revision"],
                "path": hierarchy.path_view(uow.connection, project_id)}


__all__ = ["MOVE", "ProjectMove"]
