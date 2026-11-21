"""Find a Task's owning scope from its id alone, for an agent that selected no project.

Every Task lives in one owning scope: a project, or Global. Task ids are unique across scopes going forward, so an agent
calling a Task tool over MCP may name the Task by ``task_id`` alone. ``TaskLookup`` is the ``locate_scope`` hook of the
operations that take a ``task_id`` (see ``Application._locate``). It looks the id up only in the scopes the caller can
reach - its allowed active projects, and Global when it may use Global Tasks at all (never for an owner session) - and,
in each, only where its ``tasks_get`` authority lets it read Tasks:

* no project selected (no ``project_id``, no ``scope``): one match runs the operation in that scope, exactly as if it had
  been selected; several (an id reused before ids were unique) are refused with ``validation_error`` listing them;
* a project selected: the selection stands. A Task that is not there but in one other reachable scope is refused with
  ``not_found`` saying where; a Task moved out of the selected scope keeps the operation's own ``record_moved`` answer;
* no exact match: an id with upper-case letters is looked up again in lower case (Task ids are lower case), and an input
  of at least six ``[0-9a-f-]`` characters that begins exactly one reachable Task id is refused with ``not_found`` and
  ``details.did_you_mean``. The operation never runs on such a guess.

Nothing outside the caller's reach is searched or disclosed. With nothing found the operation runs as it always did and
gives its ordinary answer. A delegated worker's call is looked up only in the one scope its assignment binds: a
project assignment's Task is found in that project and nowhere else, and a Global assignment already runs in Global.
"""

from __future__ import annotations

from dataclasses import replace
import re

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.relocation import moved_location, namespace
from qorqut_operations.tasks.scope import global_tasks_allowed, task_namespace

# Appended to the description of every operation that takes this hook.
LOCATED = instruction_text('operations-pass-task-id-alone-its-project-is-found')
SHORT_ID = re.compile(r"[0-9a-f-]{6,}")
CANDIDATES = 20
PREFIX_ROWS = 50


class Reach:
    """The scopes one caller may look a Task up in, with its ``tasks_get`` authority in each (decided once per scope)."""

    def __init__(self, tasks, ctx, uow):
        self.tasks, self.ctx, self.uow = tasks, ctx, uow
        allowed = sorted(ctx.allowed_project_ids)
        rows = uow.connection.execute(
            f"SELECT id, name, slug FROM projects WHERE archived_at IS NULL AND id IN ({','.join('?' * len(allowed))})",
            allowed).fetchall() if allowed else ()
        self.names = {row["id"]: row["name"] or row["slug"] for row in rows}
        self.global_tasks = ctx.owner_session_project_id is None and global_tasks_allowed(ctx, uow)
        self.readable = {}

    def rows(self, test, values, limit):
        """Task rows matching ``test`` in reachable scopes, Global first, at most ``limit``."""
        scopes, params = [], []
        if self.names:
            scopes.append(f"project_id IN ({','.join('?' * len(self.names))})")
            params.extend(self.names)
        if self.global_tasks:
            scopes.append("project_id IS NULL")
        if not scopes:
            return []
        found = self.uow.connection.execute(
            f"SELECT project_id, id, title FROM tasks WHERE {test} AND ({' OR '.join(scopes)}) "
            "ORDER BY project_id IS NOT NULL, project_id, id LIMIT ?", (*values, *params, limit)).fetchall()
        return [dict(row) for row in found]

    def can_read(self, project_id):
        if project_id not in self.readable:
            app = self.tasks.application
            descriptor = app.registry.get("tasks_get")
            selected = replace(self.ctx, project_id=project_id, review_project_id=project_id)
            arguments = {"scope": "project" if project_id else "global"}
            try:
                app.authorize(selected, descriptor, (), self.uow, namespace=app.namespace(selected, descriptor, arguments))
                self.readable[project_id] = True
            except DomainError:
                self.readable[project_id] = False
        return self.readable[project_id]

    def view(self, row, *, identity=False):
        return {**({"id": row["id"]} if identity else {}), "project_id": row["project_id"],
                "project_name": self.names[row["project_id"]] if row["project_id"] else "Global", "title": row["title"]}


class TaskLookup:
    """The ``locate_scope`` hook of every operation that names one Task by ``task_id``."""

    def __init__(self, tasks):
        self.tasks = tasks

    def __call__(self, ctx, arguments, uow):
        """``(arguments, namespace or None)``: the arguments with ``task_id`` in the case it was found in, and the namespace
        to run in when the caller selected none and the Task was found in exactly one reachable scope."""
        task_id = arguments.get("task_id")
        if not isinstance(task_id, str):
            return arguments, None
        if ctx.delegation_assignment_id is not None:
            return self.delegated(ctx, arguments, task_id, uow)
        reach = Reach(self.tasks, ctx, uow)
        if ctx.project_id is not None or "scope" in arguments:
            return self.selected(ctx, arguments, task_id, reach, uow), None
        found, task_id = self.exact(reach, task_id)
        arguments = {**arguments, "task_id": task_id}
        if len(found) == 1:
            return arguments, namespace(found[0]["project_id"])
        if found:
            raise DomainError(
                "validation_error", "This task id is in more than one project; call again with the project_id of the one "
                "you mean (scope=global for Global)", field_errors={"project_id": "required: the task id is in more than one project"},
                details={"task_id": task_id, "candidates": [reach.view(row) for row in found[:CANDIDATES]]})
        self.suggest(reach, task_id)
        return arguments, None

    def delegated(self, ctx, arguments, task_id, uow):
        """A delegated worker that selects no scope works in its assignment's project when the Task is there; nothing else
        is searched, so the worker learns nothing about other scopes and the assignment's own scope check still decides."""
        provider = self.tasks.application.delegation
        if ctx.project_id is not None or "scope" in arguments or provider is None:
            return arguments, None
        assignment = provider.assignment(uow, ctx.delegation_assignment_id)
        project_id = assignment["project_id"] if assignment else None
        if project_id is None:
            return arguments, None
        for candidate in dict.fromkeys((task_id, task_id.lower())):
            if uow.connection.execute("SELECT 1 FROM tasks WHERE project_id=? AND id=?", (project_id, candidate)).fetchone():
                return {**arguments, "task_id": candidate}, namespace(project_id)
        return arguments, None

    def selected(self, ctx, arguments, task_id, reach, uow):
        """The caller's own selection stands; a Task that is not there is refused saying where it is, when it can be told."""
        if task_namespace(ctx, arguments) == "global":
            here = None
        elif ctx.project_id is None:
            return arguments  # scope=project without a project: the operation refuses it.
        else:
            here = ctx.project_id
        for candidate in dict.fromkeys((task_id, task_id.lower())):
            if uow.connection.execute("SELECT 1 FROM tasks WHERE project_id IS ? AND id=?", (here, candidate)).fetchone():
                return {**arguments, "task_id": candidate}
        if moved_location(uow, here, task_id) is not None:
            return arguments  # The operation answers record_moved, to a caller who may read it where it is now.
        found, task_id = self.exact(reach, task_id)
        if len(found) == 1:
            raise DomainError("not_found", f"Task is not in the selected scope; it is in {reach.view(found[0])['project_name']}. "
                              "Call again without project_id and scope, or select that scope",
                              details={"task": reach.view(found[0], identity=True)})
        if found:
            raise DomainError("not_found", "Task is not in the selected scope; its id is in more than one other project. "
                              "Call again with the project_id of the one you mean",
                              details={"task_id": task_id, "candidates": [reach.view(row) for row in found[:CANDIDATES]]})
        self.suggest(reach, task_id)
        return arguments

    def find(self, ctx, uow, task_id):
        """``(task_id, places)``: the id as stored, and each scope in the caller's reach that holds a Task with it which
        the caller may read, as ``Reach.view`` shows it. For an operation that links a Task without running in the
        Task's scope (``actions_record``): nothing is selected or refused here, and that operation decides what none or
        several places mean."""
        reach = Reach(self.tasks, ctx, uow)
        found, task_id = self.exact(reach, task_id)
        return task_id, [reach.view(row) for row in found]

    @staticmethod
    def exact(reach, task_id):
        """The readable rows with exactly this id, else with it in lower case, and the id they have."""
        for candidate in dict.fromkeys((task_id, task_id.lower())):
            found = [row for row in reach.rows("id = ?", (candidate,), PREFIX_ROWS) if reach.can_read(row["project_id"])]
            if found:
                return found, candidate
        return [], task_id

    @staticmethod
    def suggest(reach, task_id):
        """Refuse a shortened id that begins exactly one readable Task id, naming that Task; otherwise do nothing."""
        prefix = task_id.lower()
        if not SHORT_ID.fullmatch(prefix):
            return
        rows = reach.rows("id GLOB ?", (prefix + "*",), PREFIX_ROWS)
        found = [row for row in rows if reach.can_read(row["project_id"])]
        if len(rows) < PREFIX_ROWS and len(found) == 1:
            raise DomainError("not_found", "Task does not exist in this scope. The id given is only the start of a task id: "
                              "call again with the full id in details.did_you_mean",
                              details={"did_you_mean": reach.view(found[0], identity=True)})


__all__ = ["LOCATED", "TaskLookup"]
