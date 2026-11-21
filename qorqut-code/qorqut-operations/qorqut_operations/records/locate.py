"""Find the project of one existing record from its id alone, for every caller that selected none.

Record ids are unique across the whole installation, so an operation that names one existing record by its id needs no
project: ``RecordLookup`` is the ``locate_scope`` hook of those operations (see ``Application._locate``). It never lets a
caller do or learn anything it could not by selecting the record's project itself. A record counts as found only where
the caller could read the family by selecting that scope: the scope is an active project in the caller's reach, or Global
when the family admits the caller there, and the family's read operation is authorized in it.

* nothing selected (no ``project_id``, no scope argument): a record in a reachable, readable project runs the operation in
  that project, exactly as if it had been selected. A record in Global runs it in Global: an operation that takes Global
  from its scope argument has that scope stated in the arguments the hook returns. Any other case (unknown id, a project
  out of reach or archived, a family the caller may not read there) returns the arguments unchanged, so the operation
  gives the answer it gives for any id, and nothing says where a record is or whether it exists;
* a project selected (or a scope stated): the selection stands. A record that is not there but is in another scope the
  caller can reach and read is refused with ``not_found`` saying where; any other case keeps the operation's own answer;
* an owner session is bound to its project and a delegated worker to its assignment: neither is looked up elsewhere.

``ScopeReach`` is the set of scopes one caller may look a record up in, with its read authority in each (decided once per
scope); the Task lookup shares it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.contracts import DomainError

# Appended to the description of every operation that takes this hook.
LOCATED = instruction_text('operations-pass-a-record-id-alone-its-project-is-found')


class ScopeReach:
    """The scopes one caller may enter (its allowed, active projects) and, per scope, whether it may use ``read_operation``."""

    def __init__(self, application, ctx, uow, read_operation, global_gate=None):
        self.application, self.ctx, self.uow = application, ctx, uow
        self.read_operation, self.global_gate = read_operation, global_gate
        allowed = sorted(ctx.allowed_project_ids)
        rows = uow.connection.execute(
            f"SELECT id, name, slug FROM projects WHERE archived_at IS NULL AND id IN ({','.join('?' * len(allowed))})",
            allowed).fetchall() if allowed else ()
        self.names = {row["id"]: row["name"] or row["slug"] for row in rows}
        self.readable = {}

    def can_read(self, project_id, scope_stated=True):
        """Whether the read operation is authorized in this scope (``None`` is Global), as an explicit selection of it
        would be. A family without a scope argument (``scope_stated`` false) selects Global by selecting no project."""
        if project_id not in self.readable:
            self.readable[project_id] = self.decide(project_id, scope_stated)
        return self.readable[project_id]

    def decide(self, project_id, scope_stated):
        app = self.application
        if project_id is None:
            if self.global_gate is not None:
                try:
                    self.global_gate(self.ctx, self.uow)
                except DomainError:
                    return False
        elif project_id not in self.names:
            return False
        descriptor = app.registry.get(self.read_operation)
        selected = replace(self.ctx, project_id=project_id, review_project_id=project_id)
        arguments = {"scope": "project" if project_id else "global"} if scope_stated else {}
        try:
            app.authorize(selected, descriptor, (), self.uow, namespace=app.namespace(selected, descriptor, arguments))
        except DomainError:
            return False
        return True

    def name(self, project_id):
        return self.names[project_id] if project_id else "Global"


@dataclass(frozen=True)
class Family:
    """One record family that operations name by id.

    ``table`` and ``scope_column`` hold the id and its scope (``NULL`` is Global); ``id_argument`` is the operation argument
    that carries the id; ``read_operation`` is the family's read operation, whose authority decides where the caller may
    read it. ``scope_argument`` names the argument that states Global (``None`` when the operations take no such argument:
    selecting no project is then Global) and ``state_global`` says a record found in Global needs that argument stated.
    ``global_gate(ctx, uow)`` raises when the caller may not enter Global for this family. ``refuses_elsewhere`` turns on the
    ``not_found`` that says where a record is when the caller selected another scope. ``global_serves_selected`` says a
    selected project also serves Global records (documents). ``named_project(arguments)`` is a project the arguments
    themselves select (a note's ``target_project_id``)."""
    table: str
    id_argument: str
    read_operation: str
    noun: str
    scope_column: str = "project_id"
    scope_argument: str | None = "scope"
    state_global: bool = False
    global_gate: Callable | None = None
    refuses_elsewhere: bool = True
    global_serves_selected: bool = False
    named_project: Callable | None = None


class RecordLookup:
    """The ``locate_scope`` hook of every operation that names one existing record of a family by its id."""

    def __init__(self, family):
        self.family = family
        self.application = None

    def attach(self, application):
        """The Application that runs this hook gives it the registry and policy that authorize its read checks."""
        self.application = application
        return self

    def __call__(self, ctx, arguments, uow):
        """``(arguments, namespace or None)``: the namespace to run in when the caller selected none and the record is in a
        scope it may read, with Global stated in the arguments when the operation takes it from its scope argument."""
        family = self.family
        record_id = arguments.get(family.id_argument)
        if (not isinstance(record_id, str) or self.application is None or ctx.owner_session_project_id is not None
                or ctx.delegation_assignment_id is not None):
            return arguments, None
        selected, here = self.selection(ctx, arguments)
        if selected:
            if here is not False:
                self.elsewhere(ctx, arguments, record_id, here, uow)
            return arguments, None
        found, place = self.place(record_id, uow)
        if not found:
            return arguments, None
        reach = self.reach(ctx, uow)
        stated = family.scope_argument is not None
        if place is None:
            if not reach.can_read(None, stated):
                return arguments, None
            if family.state_global:
                return {**arguments, family.scope_argument: "global"}, "global"
            return arguments, None
        if reach.can_read(place, stated):
            return arguments, "project:" + place
        return arguments, None

    def selection(self, ctx, arguments):
        """``(selected, here)``: whether the call names a scope, and which (``None`` is Global; ``False`` when it names a scope
        no record can be looked up in, such as ``scope=project`` with no project)."""
        family = self.family
        if family.scope_argument is not None and arguments.get(family.scope_argument) == "global":
            return True, None
        if ctx.project_id is not None:
            return True, ctx.project_id
        named = family.named_project(arguments) if family.named_project else None
        if named is not None:
            return True, named
        if family.scope_argument is not None and family.scope_argument in arguments:
            return True, False
        return False, None

    def reach(self, ctx, uow):
        family = self.family
        return ScopeReach(self.application, ctx, uow, family.read_operation, family.global_gate)

    def place(self, record_id, uow):
        """``(found, project_id)``: whether a record has this id, and the project that holds it (``None`` for Global)."""
        family = self.family
        row = uow.connection.execute(f'SELECT "{family.scope_column}" FROM {family.table} WHERE id=?', (record_id,)).fetchone()
        return row is not None, row[0] if row else None

    def elsewhere(self, ctx, arguments, record_id, here, uow):
        """The caller's selection stands; a record that is in another scope it may read is refused saying where it is."""
        family = self.family
        if not family.refuses_elsewhere:
            return
        found, place = self.place(record_id, uow)
        if not found or place == here or (place is None and family.global_serves_selected):
            return
        reach = self.reach(ctx, uow)
        if not reach.can_read(place, family.scope_argument is not None):
            return
        name = reach.name(place)
        raise DomainError("not_found", f"{family.noun} is not in the selected scope; it is in {name}. "
                          "Call again without project_id and scope, or select that scope",
                          details={"id": record_id, "project_id": place, "project_name": name})


__all__ = ["Family", "LOCATED", "RecordLookup", "ScopeReach"]
