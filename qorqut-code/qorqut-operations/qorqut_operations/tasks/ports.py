"""Typed host ports of the Task operations.

The Task flow (records, fixed states, custom fields, parents, dependencies, tags, discussion, follows, events and the
snooze) is the engine's. What the engine cannot know comes in through ``TaskPorts``, each part required and none
discovered by name: the host's contact and peer rules for who may be assigned, the owner
Focus presentation, the incident ticket link, the references a note may cite beyond engine items, and the routes that v1
made mandatory for owner work. A port narrows or enriches an operation; it cannot grant access, and every port is
called inside the same prepared plan and transaction as the Task write it belongs to."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from qorqut_operations.contracts import InvocationContext
from qorqut_operations.storage.assembly import AssemblyError


class AssigneeRules(Protocol):
    """The host's rules for who a Task may be assigned to, beyond active membership of the owning scope.

    ``protect(ctx, identity, uow)`` raises when the caller may not direct that principal (v1 ``protect_peer_control``).
    ``owner_directed(ctx, identity, uow)`` is true for principals only the owner may assign (Learning Applier);
    the engine refuses them and leaves them out of ``assignees_list``."""

    def protect(self, ctx: InvocationContext, identity: str, uow) -> None: ...

    def owner_directed(self, ctx: InvocationContext, identity: str, uow) -> bool: ...

    def worker_excluded(self, ctx: InvocationContext, identity: str, uow) -> bool: ...


class Focus(Protocol):
    """The owner Focus presentation of a new Task (v1 ``creation_access`` / ``present_created``).

    ``schema`` is the property schema of the optional ``focus`` argument of ``tasks_create``."""

    schema: Mapping[str, Any]

    def creation_access(self, ctx: InvocationContext, focus: Mapping[str, Any], uow) -> None: ...

    def present_created(self, ctx: InvocationContext, focus: Mapping[str, Any], item: Mapping[str, Any], uow) -> None: ...


class Incidents(Protocol):
    """Links a Task to the development ticket a note cites (v1 ``IncidentReporter.link_task``)."""

    def link_task(self, uow, ticket_id: str, business_id: str | None, task_id: str, principal_id: str, at: str) -> None: ...


class NoteReferences(Protocol):
    """References a Task note may cite besides the engine's neutral items.

    ``types`` are all the reference types a note may cite, in order (engine item kinds and the host's own);
    ``host_types`` are the ones ``resolve`` handles itself (for example ``dev_ticket``). ``resolve`` returns
    ``{label, state}``, checks the caller's authority to read the reference and pins it on ``builder`` when preparing.
    ``derived(ctx, uow, message)`` is the extra reference resolved on read for a message (the outreach handoff email
    link) or ``None``."""

    types: Sequence[str]
    host_types: Sequence[str]

    def resolve(self, ctx: InvocationContext, item: Mapping[str, Any], uow, builder=None) -> Mapping[str, Any]: ...

    def derived(self, ctx: InvocationContext, uow, message: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


class Routing(Protocol):
    """Mandatory and optional recipients the host adds to a Task event (v1 coordinator routing).

    ``routes(uow, business_id, task, actor_id)`` returns ``(required, optional)`` principal ids: the Focus coordinator
    for an owner's change to a focused Task and the Ordubashy followers of owner work are required; the subscribers of a
    group Task this Task is directly related to (a child, or a related link in either direction) are optional. The engine adds the recipients every Task has (assignees, assignment targets, owners
    of dependent work, followers) itself, and the acting principal is always removed.

    ``snooze_routes(uow, business_id, task, human_ids)`` is the one required route of a snooze wake: ``human_ids`` are
    the human principals responsible for the Task (its human assignees, or its human creator when it has no assignee at
    all) and the result is the agent ids that must be told on their behalf. Human responsibility has no inbox of its
    own, so this is where the host answers for it (the Focus coordinator, the owner-work coordinators); optional
    followers are never part of it. The engine filters the result to agents eligible in the Task's scope."""

    def routes(self, uow, business_id: str | None, task: Mapping[str, Any], actor_id: str | None) -> tuple[Sequence[str], Sequence[str]]: ...

    def snooze_routes(self, uow, business_id: str | None, task: Mapping[str, Any], human_ids: Sequence[str]) -> Sequence[str]: ...


class Boards(Protocol):
    """The host's work boards, told when a Task names new Workers (who must act next).

    ``workers_named(ctx, item, task, worker_ids, uow)`` runs once per write that adds Workers, before that write's event
    is routed: ``item`` is the Task reference, ``task`` its row after the write and ``worker_ids`` only the principals
    this write added. The host may list the Task on a board it keeps for one of them; it never refuses the write, and
    it decides by its own rules whose board the caller may fill. Removing a Worker tells nobody."""

    def workers_named(self, ctx: InvocationContext, item: Mapping[str, Any], task: Mapping[str, Any],
                      worker_ids: Sequence[str], uow) -> None: ...


@dataclass(frozen=True)
class TaskPorts:
    """All host knowledge of one ``TaskOperations``, built once by trusted assembly code.

    ``boards`` is the one optional part: an assembly without work boards leaves it out."""

    assignees: AssigneeRules
    focus: Focus
    incidents: Incidents
    references: NoteReferences
    routing: Routing
    boards: Boards | None = None

    def __post_init__(self):
        if self.boards is not None and not callable(getattr(self.boards, "workers_named", None)):
            raise AssemblyError("The optional task port boards must provide workers_named")
        for label, value, methods in (
                ("assignees", self.assignees, ("protect", "owner_directed", "worker_excluded")),
                ("focus", self.focus, ("creation_access", "present_created")), ("incidents", self.incidents, ("link_task",)),
                ("references", self.references, ("resolve", "derived")), ("routing", self.routing, ("routes", "snooze_routes"))):
            missing = [name for name in methods if not callable(getattr(value, name, None))]
            if value is None or missing:
                raise AssemblyError(f"Required task port {label} must provide {', '.join(missing) or 'an implementation'}")
        if not isinstance(getattr(self.focus, "schema", None), Mapping):
            raise AssemblyError("The Focus port provides the schema of the tasks_create focus argument")
        if any(not isinstance(getattr(self.references, name, None), Sequence) or isinstance(getattr(self.references, name), str)
               for name in ("types", "host_types")):
            raise AssemblyError("The note reference port lists its reference types and its host-resolved types")


__all__ = ["AssigneeRules", "Boards", "Focus", "Incidents", "NoteReferences", "Routing", "TaskPorts"]
