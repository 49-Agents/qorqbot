"""The host's side of the engine's Tasks: who may be assigned, Focus, incident links, note references, routing.

The Task flow itself (states, fields, parents, dependencies, tags, discussion, follows, events, snooze) is the Operations
engine's ``TaskOperations``. Each class here answers one typed ``TaskPorts`` part with the retained v1 host rule; none
grants access, and every one runs inside the Task write it belongs to."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote

from qorqut_operations.application.plans import IDENTITY, ref, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks import TaskPorts
from qorqut_operations.tasks.scope import task_namespace

from qorqut.agents.contact import owner_directed_target
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.domains.dev_tickets import DevTickets
from qorqut.inbox import attention
from qorqut.inbox.owner_tasks import ordubashies, tasks as owner_task_rows
from qorqut_operations.inbox.related import subscribers as related_subscribers
from qorqut.incidents import IncidentReporter

# The reference types a Task note may cite. The first three are also the types Focus notes accept; ``material``
# (an existing email conversation or document) is resolved by the engine's neutral item resolver. ``email`` is a Task-note
# type only: one provider message, stored as ``<provider>:<account id>:<message id>`` with account/message percent-escaped
# like JavaScript ``encodeURIComponent``. It names a message for the owner to open; it reads and proves nothing.
NOTE_REF_TYPES = ("signoff", "task", "dev_ticket")
NOTE_REFS = {"type": "array", "maxItems": 10, "items": schema({"type": {"enum": list(NOTE_REF_TYPES)}, "id": IDENTITY}, ("type", "id"))}
TASK_NOTE_REF_TYPES = (*NOTE_REF_TYPES, "material", "email")
EMAIL_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
EMAIL_COMPONENT = re.compile(r"(?:[A-Za-z0-9_.!~*'()-]|%[0-9A-Fa-f]{2})+")


def email_reference(identity: str) -> tuple[str, str, str] | None:
    """A lowercase provider ID and two decoded, nonempty account/message components.

    This validates reference syntax only. Provider availability and permission to read
    a conversation belong to the selected connector, not the Task note.
    """
    if not isinstance(identity, str):
        return None
    parts = identity.split(":")
    if len(parts) != 3 or not EMAIL_PROVIDER.fullmatch(parts[0]) or not all(EMAIL_COMPONENT.fullmatch(part) for part in parts[1:]):
        return None
    try:
        return parts[0], unquote(parts[1], errors="strict"), unquote(parts[2], errors="strict")
    except UnicodeDecodeError:
        return None


class HostAssignees:
    """The owner-only target (Learning Applier) retained from v1, plus the engine's peer-protection hook.

    The engine's assignees port still names ``protect``; no peer roles remain to protect from each other, so it refuses nothing.
    """

    def protect(self, ctx, identity, uow):
        return None

    def owner_directed(self, ctx, identity, uow):
        return owner_directed_target(ctx, identity, uow)

    def worker_excluded(self, ctx, identity, uow):
        profile = uow.repository("agent_profiles", business_id=None).get(identity)
        return bool(profile and (profile.get("profile_json") or {}).get("role", {}).get("id") == LEARNING_APPLIER_ROLE)


class HostFocus:
    """Owner Focus presentation of a newly created Task (the attention board, installed on the application)."""

    schema = attention.FIELDS

    def __init__(self, application):
        self.application = application

    def creation_access(self, ctx, focus, uow):
        attention.creation_access(self.application, ctx, focus, uow)

    def present_created(self, ctx, focus, item, uow):
        attention.present_created(self.application, ctx, focus, item, uow)


class HostIncidents:
    def link_task(self, uow, ticket_id, business_id, task_id, principal_id, at):
        IncidentReporter.link_task(uow, ticket_id, business_id, task_id, principal_id, at)


class HostNoteReferences:
    """``dev_ticket`` and ``email`` notes and the derived references the selected extensions resolve on read; every other
    reference is a neutral item."""

    types = TASK_NOTE_REF_TYPES
    host_types = ("dev_ticket", "email")

    def __init__(self, application, derivations=()):
        self.application = application
        self.derivations = tuple(derivations)

    def resolve(self, ctx, item, uow, builder=None):
        if item["type"] == "email":
            # Format only: no mail is read or checked here, and no mail access is granted. A new note must carry a well-formed
            # identity; a stored malformed one reads as unavailable instead of failing the whole Task read.
            if email_reference(item["id"]) is None:
                if builder is not None:
                    raise DomainError("validation_error", "An email reference is <provider>:<account id>:<message id>, with a lowercase provider ID and percent-escaped account/message components")
                raise DomainError("not_found", "Email reference is not readable")
            return {"label": "Email conversation", "state": None}
        tickets = DevTickets()
        ticket = tickets.ticket(uow, item["id"])
        if ctx.principal_kind == "human":
            tickets.owner(ctx, uow)
        else:
            descriptor = self.application.registry.get("dev_tickets_get")
            self.application.authorize(ctx, descriptor, (), uow, namespace=task_namespace(ctx, {}))
            tickets.agent_access(ctx, ticket, uow, read_only=True)
        if builder:
            builder.expect(ref("dev_tickets", item["id"], None), ticket)
        return {"label": ticket["title"], "state": ticket["status"]}

    def derived(self, ctx, uow, message: Mapping):
        """A message's derived reference (an extension's, such as a handoff's email link), resolved on read."""
        for derive in self.derivations:
            value = derive(ctx, uow, message)
            if value is not None:
                return value
        return None


class HostRouting:
    """The v1 routes that are mandatory (the Focus coordinator and the Ordubashy followers of owner work) or discretionary
    (subscribers, with related work, of a group Task this Task is directly related to)."""

    @staticmethod
    def mandatory(uow, business_id, task, actor_id):
        """The mandatory v1 routes of a Task: the Focus coordinator of an owner's change to a focused Task and the
        Ordubashy followers of owner work."""
        from qorqut.agents.attention import task_recipients

        required = set(task_recipients(uow, business_id, task, actor_id))
        installation = uow.repository("installation").get("service")
        if installation and installation["management_coordinator_id"] and actor_id == installation["owner_principal_id"]:
            focused = uow.connection.execute(
                """SELECT 1 FROM focus_cards f
                WHERE (json_extract(source_json,'$.type')='task' AND json_extract(source_json,'$.id')=? AND json_extract(source_json,'$.business_id') IS ?)
                OR (json_extract(context_json,'$.type')='task' AND json_extract(context_json,'$.id')=? AND json_extract(context_json,'$.business_id') IS ?)
                OR EXISTS (SELECT 1 FROM json_each(f.references_json) r WHERE json_extract(r.value,'$.type')='task'
                    AND json_extract(r.value,'$.id')=? AND json_extract(r.value,'$.business_id') IS ?) LIMIT 1""",
                (task["id"], business_id) * 3).fetchone()
            if focused:
                required.add(installation["management_coordinator_id"])
        owners = ordubashies(uow)
        if owners and owner_task_rows(uow, business=business_id, identity=task["id"]).fetchone():
            required |= owners
        return required

    def routes(self, uow, business_id, task, actor_id):
        return self.mandatory(uow, business_id, task, actor_id), related_subscribers(uow, business_id, "task", task["id"])

    def snooze_routes(self, uow, business_id, task, human_ids):
        """The agents told when a snooze expires on behalf of the Task's responsible humans (its human assignees, or its
        human creator when it has no assignee). Human responsibility has no inbox row, so the mandatory routes answer for
        it: when the installation owner is one of them, the Focus coordinator of a focused Task and the Ordubashy owner-work
        recipients (which do not depend on an assigned-to-owner edge, so an owner-created unassigned Task reaches them).
        Other humans keep whichever mandatory route applies to them. Optional related-work/follower routes are not responsibility."""
        installation = uow.repository("installation").get("service")
        owner = installation["owner_principal_id"] if installation else None
        owner_responsible = owner is not None and owner in set(human_ids)
        required = self.mandatory(uow, business_id, task, owner if owner_responsible else None)
        if owner_responsible:
            required |= ordubashies(uow)
        return sorted(required)


def task_ports(application, derivations=()) -> TaskPorts:
    return TaskPorts(assignees=HostAssignees(), focus=HostFocus(application), incidents=HostIncidents(),
                     references=HostNoteReferences(application, derivations), routing=HostRouting())


__all__ = ["HostAssignees", "HostFocus", "HostIncidents", "HostNoteReferences", "HostRouting", "NOTE_REFS",
           "NOTE_REF_TYPES", "email_reference", "task_ports"]
