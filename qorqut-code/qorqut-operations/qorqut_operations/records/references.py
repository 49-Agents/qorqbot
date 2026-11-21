"""Complete a record reference that names no project.

Record ids are unique across the installation, so a reference that names an existing record by its type and id needs no
``project_id``. ``References`` is the ``complete_references`` step of an operation whose arguments hold such references
(see ``Application._locate``). It runs before namespace resolution, authorization, the input hash and the plan, so the
arguments it returns are the call's own in every respect: what an operation checks, stores and compares is always the
full reference.

* a reference that carries the project key (a project id, or ``null`` for Global) is left exactly as given: the
  operation's own checks answer a project that does not hold the record;
* a reference without the key gets the project that holds the record, when that project is active, in the caller's
  reach and one where the caller may use the kind's read operation: the caller could have named that project itself. An
  owner session and a delegated worker are completed only with the project they are bound to;
* in any other case (an unknown id, a record in Global, a project out of reach or archived, a kind that is not looked
  up) the reference gets ``null``, the Global scope, or stays without the key where the operation reads a missing key
  its own way (``absent="keep"``). Nothing says where a record is or whether it exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.records.locate import ScopeReach

# A project reference names itself: its id is its project.
PROJECT = "project"


@dataclass(frozen=True)
class Kind:
    """Where one kind of record keeps its id and its project, and the read operation whose authority decides where a
    caller may read it. ``visible(ctx, uow, identity, project_id)`` is a further check of the one record (a Signoff's
    visibility); ``scope_stated`` says the read operation takes its scope from a ``scope`` argument."""
    table: str
    read_operation: str
    scope_column: str = "project_id"
    scope_stated: bool = True
    visible: Callable | None = None


KINDS = {
    "task": Kind("tasks", "tasks_get"),
    "table": Kind("data_tables", "tables_get"),
    "signoff": Kind("signoffs", "signoffs_get", scope_column="review_project_id"),
}


@dataclass(frozen=True)
class Ref:
    """One place in the arguments that holds a reference, or a list of them.

    ``path`` is the keys that lead to it; ``"*"`` steps through every element of a list. ``kind`` fixes the record kind
    when the reference has no ``type`` of its own. ``absent`` is what a reference gets when no project is found for it:
    ``"global"`` writes ``null``, ``"keep"`` leaves it without the key."""
    path: tuple
    kind: str | None = None
    id_key: str = "id"
    project_key: str = "project_id"
    type_key: str = "type"
    absent: str = "global"

    def __post_init__(self):
        if self.absent not in ("global", "keep"):
            raise ValueError("Unknown completion of a reference without a project")


def _each(value, path):
    """Every mapping at ``path`` under ``value``."""
    if not path:
        if isinstance(value, Mapping):
            yield value
        return
    head, rest = path[0], path[1:]
    if head == "*":
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                yield from _each(item, rest)
    elif isinstance(value, Mapping) and head in value:
        yield from _each(value[head], rest)


class References:
    """The ``complete_references`` step of an operation: ``(application, ctx, arguments, uow) -> arguments``."""

    def __init__(self, *refs, kinds=None):
        self.refs = tuple(refs)
        self.kinds = dict(KINDS if kinds is None else kinds)

    def __call__(self, application, ctx, arguments, uow):
        """The arguments with every reference that named no project completed; the same object when none needed it."""
        if not any(ref.project_key not in holder for ref in self.refs for holder in _each(arguments, ref.path)):
            return arguments
        completed, reaches = plain(arguments), {}
        for ref in self.refs:
            for holder in _each(completed, ref.path):
                if ref.project_key in holder:
                    continue
                kind = ref.kind or holder.get(ref.type_key)
                project = self.project_of(application, ctx, uow, kind, holder.get(ref.id_key), reaches)
                if project is not None or ref.absent == "global":
                    holder[ref.project_key] = project
        return completed

    def project_of(self, application, ctx, uow, kind, identity, reaches=None):
        """The project that holds the record of this kind and id, when the caller could have named it itself; else ``None``."""
        if not isinstance(kind, str) or not isinstance(identity, str):
            return None
        known = self.kinds.get(kind)
        if kind == PROJECT:
            project = identity
        elif known is None:
            return None
        else:
            row = uow.connection.execute(f'SELECT "{known.scope_column}" FROM {known.table} WHERE id=?', (identity,)).fetchone()
            project = row[0] if row else None
        if project is None:
            return None
        if (ctx.owner_session_project_id is not None or ctx.delegation_assignment_id is not None) and project != ctx.project_id:
            return None
        reaches = {} if reaches is None else reaches
        operation = known.read_operation if known else None
        if operation not in reaches:
            reaches[operation] = ScopeReach(application, ctx, uow, operation)
        reach = reaches[operation]
        if project not in reach.names:
            return None
        if known is None:
            return project
        try:
            readable = reach.can_read(project, known.scope_stated)
        except DomainError:
            # The kind's read operation is not installed here: nothing decides the caller may read it.
            return None
        if not readable:
            return None
        if known.visible is not None and not known.visible(ctx, uow, identity, project):
            return None
        return project


__all__ = ["KINDS", "Kind", "PROJECT", "Ref", "References"]
