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
  owner session is completed only with the project it is bound to and a delegated worker only with the project of its
  assignment;
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
from qorqut_operations.records.vocabulary import document_type

# A project reference names itself: its id is its project.
PROJECT = "project"


@dataclass(frozen=True)
class Kind:
    """Where one kind of record keeps its id and its project, and the read operation whose authority decides where a
    caller may read it: its name, or ``read_operation(ctx, uow, row)`` where the stored row picks it (``None`` when the
    row is no record of this kind for this caller). ``visible(ctx, uow, identity, project_id)`` is a further check of
    the one record (a Signoff's visibility); ``scope_stated`` says the read operation takes its scope from a ``scope``
    argument."""
    table: str
    read_operation: str | Callable
    scope_column: str = "project_id"
    scope_stated: bool = True
    visible: Callable | None = None

    def reads(self, ctx, uow, row):
        return self.read_operation(ctx, uow, row) if callable(self.read_operation) else self.read_operation


def _linked(ctx, uow, identity, project_id):
    """A caller that selects a project reads the Actions linked to it."""
    return uow.connection.execute("SELECT 1 FROM action_projects WHERE action_id=? AND project_id=?",
                                  (identity, project_id)).fetchone() is not None


def _document(kind):
    """A Context document as an item of one type (``person``, ``topic`` or ``material``). Its stored kind picks the read
    operation; the journal kinds are the owner's alone and working memory is no item."""
    def read_operation(ctx, uow, row):
        from qorqut_operations.documents.journal_access import JOURNAL_KINDS, is_owner
        from qorqut_operations.records.resources import DOCUMENT_READS
        stored = row["kind"]
        if document_type(row) != kind or stored == "working_memory" or (stored in JOURNAL_KINDS and not is_owner(ctx, uow)):
            return None
        return DOCUMENT_READS.get(stored, "context_get")
    return Kind("documents", read_operation, scope_stated=False)


KINDS = {
    "task": Kind("tasks", "tasks_get"),
    "table": Kind("data_tables", "tables_get"),
    "signoff": Kind("signoffs", "signoffs_get", scope_column="review_project_id"),
    "action": Kind("action_logs", "actions_get", visible=_linked),
    "routine": Kind("routines", "routines_get"),
    "entity": Kind("context_entities", "context_items_get"),
    "person": _document("person"),
    "topic": _document("topic"),
    "material": _document("material"),
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


def _bound(application, ctx, uow):
    """``(bound, project_id)``: whether the caller is tied to one scope (an owner session to its project, a delegated
    worker to its assignment's), and that scope's project."""
    if ctx.owner_session_project_id is not None:
        return True, ctx.owner_session_project_id
    if ctx.delegation_assignment_id is None:
        return False, None
    provider = application.delegation
    assignment = provider.assignment(uow, ctx.delegation_assignment_id) if provider is not None else None
    return True, assignment["project_id"] if assignment else None


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
        operation = None
        if kind == PROJECT:
            project = identity
        elif known is None:
            return None
        else:
            row = uow.connection.execute(f"SELECT * FROM {known.table} WHERE id=?", (identity,)).fetchone()
            project = row[known.scope_column] if row else None
            operation = known.reads(ctx, uow, row) if project is not None else None
            if operation is None:
                return None
        if project is None:
            return None
        bound, only = _bound(application, ctx, uow)
        if bound and project != only:
            return None
        reaches = {} if reaches is None else reaches
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
