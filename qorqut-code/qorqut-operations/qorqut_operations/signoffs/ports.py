"""Typed host ports for the extracted Signoff machinery.

The deployed v1 Signoff code reached into platform-action, email, campaign-group, Task, Focus, management and identity
modules by import or by optional attributes of the application. Every such reach is one small typed port here,
named after the v1 function it replaces and keeping that function's arguments and results. The engine keeps the
scope, decision and application checks itself: a port supplies presentation, vocabulary, an additional *narrowing*
filter or an identity lookup, and cannot grant authority or rewrite a prepared plan.

``SignoffPorts`` is built once by trusted assembly code and handed to ``SignoffService``. A required port that is
missing or incomplete fails when ``SignoffPorts`` is constructed. A domain that is genuinely not installed is
declared with ``ABSENT``; the operations that need it then fail with ``operation_unavailable`` (or, where v1 already
treated the domain as optional, behave exactly as v1 did without it) instead of returning an invented default.
The source-to-port call map is in docs/EXTRACTION.md."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from qorqut_operations.contracts import ChangeSet, DomainError, InvocationContext
from qorqut_operations.storage.assembly import AssemblyError


class Absent:
    """The explicit declaration that an optional host domain is not installed. A single instance, ``ABSENT``."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "ABSENT"

    def __bool__(self):
        return False


ABSENT = Absent()


# -- required ports -----------------------------------------------------------------------------------------------

class ExecutionPresentation(Protocol):
    """Execution-state vocabulary of a proposal (v1 ``platform_actions.proposals``).

    ``execution_states`` -> ``EXECUTION_STATES`` (the ``signoffs_list`` filter enum, in order).
    ``execution_state(row, uow=None)`` -> one of those names for a stored Signoff row.
    ``execution_sql("s")`` -> a fixed SQL expression over alias ``s`` that mirrors ``execution_state`` for filtering;
    the engine only embeds it (never caller values) and only to narrow a listing."""

    execution_states: Sequence[str]

    def execution_state(self, row: Mapping[str, Any], uow=None) -> str: ...

    def execution_sql(self, alias: str) -> str: ...


class PeerIdentities(Protocol):
    """The exact current identity lookups behind the standing-authority checks (v1 ``management.identity`` and
    ``agents.global_peers``).

    ``configured_ordubashy(uow, *, require_codex)`` -> the one configured coordinator principal id, or ``None``
    when it cannot act. ``marketing_peer(uow, principal_id)`` -> ``True`` only for an active Global Marketing
    peer holding its current peer capability. The engine decides what these answers authorize and re-checks
    them before execution."""

    def configured_ordubashy(self, uow, *, require_codex: bool) -> str | None: ...

    def marketing_peer(self, uow, principal_id: str) -> bool: ...


class OwnerTaskAttention(Protocol):
    """Task/snooze knowledge for the owner attention overview (v1 ``tasks.models.DEFAULT_CONFIG`` and
    ``tasks.snooze.ACTIVE_SQL`` inside ``SignoffNotifications.overview``).

    ``open_assigned_count(uow, business_id, principal_id, now)`` -> the number of non-archived, non-closed,
    not-snoozed tasks assigned to that person in that business (``None`` is the Global workspace). ``now`` is the
    normalized UTC stamp the engine compares against."""

    def open_assigned_count(self, uow, business_id: str | None, principal_id: str, now: str) -> int: ...


@dataclass(frozen=True)
class ReferenceTarget:
    """How one note-reference kind is read: the read operation and the argument that names the record."""

    operation: str
    argument: str


class ReferenceVocabulary(Protocol):
    """The conversation reference schema and its exact scoped lookup (v1 ``tasks.operations.NOTE_REFS`` and the
    kind table inside ``SignoffNotes.references``).

    ``targets`` is ordered, e.g. ``{"task": ReferenceTarget("tasks_get", "task_id"), ...}``; the note-reference
    schema is derived from its keys. The engine performs the lookup itself: it resolves the named read
    operation, authorizes it for the actor in the actor's namespace and then calls the descriptor's read handler,
    so a reference never reads something the actor could not read directly. ``task_namespace(context,
    arguments)`` is the host's scoped-namespace rule used by delegated management answers."""

    targets: Mapping[str, ReferenceTarget]

    def task_namespace(self, context: InvocationContext, arguments: Mapping[str, Any]) -> str: ...


# -- optional domains ---------------------------------------------------------------------------------------------

class PlatformActions(Protocol):
    """The platform-action domain (v1 ``app.platform_actions`` and ``platform_actions.proposals``).

    Called inside the writing transaction, in the same order as v1: ``staging_values`` before the Signoff insert,
    ``staged`` after it, ``transition`` after every status update of a ``platform_action`` row,
    ``replaceable_after_cancel`` while checking supersession. ``view`` is v1 ``platform_view``,
    ``enqueue_binding`` the v1 function of that name and ``is_email_submission`` the v1
    ``Application._email_submission`` classification of a prepared plan. Staging itself is a host contribution:
    ``staging_values`` returns ``{"id", "execution_job_id"}`` bound to the prepared plan; it cannot change the
    plan the engine stores."""

    def staging_values(self, context: InvocationContext, plan: ChangeSet, old, uow) -> Mapping[str, Any]: ...

    def staged(self, context: InvocationContext, row: Mapping[str, Any], plan: ChangeSet, old, uow) -> None: ...

    def transition(self, row: Mapping[str, Any], updated: Mapping[str, Any], uow) -> None: ...

    def replaceable_after_cancel(self, old: Mapping[str, Any], uow) -> bool: ...

    def view(self, row: Mapping[str, Any], uow=None) -> Mapping[str, Any]: ...

    def enqueue_binding(self, plan: ChangeSet) -> Mapping[str, Any]: ...

    def is_email_submission(self, plan: ChangeSet) -> bool: ...


class EmailDecisions(Protocol):
    """Email proposal presentation (v1 ``domains.email.decisions``): ``visibility(context, uow)`` -> ``(sql, params)``
    and ``email_context(row, uow)`` -> the email context of an input request, or ``None``.

    The ``visibility`` string is appended *verbatim* to the engine's own visibility predicate: the engine adds no
    ``AND`` and no parentheses. A nonempty fragment therefore includes its own leading `` AND `` (an empty string is
    allowed), is written over alias ``s`` with ``?`` placeholders bound by ``params``, and, if it combines predicates
    with ``OR``, parenthesizes them itself. It can only narrow the engine's visibility predicate, never replace it."""

    def visibility(self, context: InvocationContext, uow) -> tuple[str, Sequence[Any]]: ...

    def email_context(self, row: Mapping[str, Any], uow) -> Mapping[str, Any] | None: ...


class CampaignFilter(Protocol):
    """Campaign filtering of ``signoffs_list`` (v1 ``stories.attribution.campaign`` plus the campaign SQL).

    The engine first authorizes the read operation named by ``authorizing_operation`` (the host names the read of the
    Task that is the campaign; v1 named the retired group read) for the
    caller. ``narrow(context, campaign_id, uow)`` then validates the campaign (v1 ``campaign``) and returns
    ``(sql, params)``: a BARE SQL predicate over alias ``s`` with ``?`` placeholders bound by ``params`` and NO
    leading ``AND``. The engine supplies the connective and the parentheses (it appends `` AND (`` + the predicate +
    ``)`` to the visibility predicate), so the predicate can only narrow the listing and never replace the visibility
    predicate."""

    authorizing_operation: str

    def narrow(self, context: InvocationContext, campaign_id: str, uow) -> tuple[str, Sequence[Any]]: ...


class ManagementQueries(Protocol):
    """Management routing knowledge (v1 ``management.service`` and ``management.identity``).

    ``managed_input_sql`` is v1 ``MANAGED_INPUT_SQL`` over alias ``s``. ``coordinator(uow)`` is
    ``ManagementService._coordinator`` (raises ``management_unavailable`` exactly as v1). ``auto_route_input(ctx,
    row, uow)`` runs in the transaction that creates an input request. ``routing_for(uow, signoff_id)`` returns
    the stored routing row of a Signoff (v1 queried ``management_routing`` directly) or ``None``."""

    managed_input_sql: str

    def coordinator(self, uow) -> str: ...

    def auto_route_input(self, context: InvocationContext, row: Mapping[str, Any], uow) -> None: ...

    def routing_for(self, uow, signoff_id: str) -> Mapping[str, Any] | None: ...


class FocusAttention(Protocol):
    """Focus/Task attention surfaces (v1 ``inbox.attention``). ``fields`` is ``FIELDS`` (the ``focus`` argument's
    property schema). ``creation_access`` and ``present_created`` keep v1's leading ``application`` argument;
    ``release_signoff(row, updated, uow)`` is ``AttentionBoard.release_signoff`` called after each status update."""

    fields: Mapping[str, Any]

    def creation_access(self, application, context: InvocationContext, focus: Mapping[str, Any], uow) -> None: ...

    def present_created(self, application, context: InvocationContext, focus: Mapping[str, Any],
                        source: Mapping[str, Any], uow) -> None: ...

    def release_signoff(self, row: Mapping[str, Any], updated: Mapping[str, Any], uow) -> None: ...


# -- the assembled set --------------------------------------------------------------------------------------------

def _check(value, label: str, methods: Sequence[str] = (), attributes: Sequence[str] = ()) -> None:
    missing = [name for name in methods if not callable(getattr(value, name, None))]
    missing += [name for name in attributes if getattr(value, name, None) is None]
    if missing:
        raise AssemblyError(f"Signoff port {label} must provide {', '.join(missing)}")


@dataclass(frozen=True)
class SignoffPorts:
    """All host ports of one Signoff service. ``execution``, ``peers``, ``tasks`` and ``references`` are required;
    ``platform``, ``email``, ``campaigns``, ``management`` and ``focus`` are optional domains that must be declared,
    either with an implementation or with ``ABSENT``."""

    execution: ExecutionPresentation
    peers: PeerIdentities
    tasks: OwnerTaskAttention
    references: ReferenceVocabulary
    platform: PlatformActions | Absent
    email: EmailDecisions | Absent
    campaigns: CampaignFilter | Absent
    management: ManagementQueries | Absent
    focus: FocusAttention | Absent

    def __post_init__(self):
        for name, value in (("execution", self.execution), ("peers", self.peers), ("tasks", self.tasks),
                            ("references", self.references)):
            if value is None or value is ABSENT:
                raise AssemblyError(f"Required Signoff port missing: {name}")
        _check(self.execution, "execution", ("execution_state", "execution_sql"), ("execution_states",))
        _check(self.peers, "peers", ("configured_ordubashy", "marketing_peer"))
        _check(self.tasks, "tasks", ("open_assigned_count",))
        _check(self.references, "references", ("task_namespace",), ("targets",))
        targets = self.references.targets
        if (not isinstance(targets, Mapping) or not targets
                or any(type(item) is not ReferenceTarget for item in targets.values())):
            raise AssemblyError("Signoff port references must name ReferenceTarget values")
        for name, methods, attributes in (
                ("platform", ("staging_values", "staged", "transition", "replaceable_after_cancel", "view",
                              "enqueue_binding", "is_email_submission"), ()),
                ("email", ("visibility", "email_context"), ()),
                ("campaigns", ("narrow",), ("authorizing_operation",)),
                ("management", ("coordinator", "auto_route_input", "routing_for"), ("managed_input_sql",)),
                ("focus", ("creation_access", "present_created", "release_signoff"), ("fields",))):
            value = getattr(self, name)
            if value is None:
                raise AssemblyError(f"Optional Signoff domain {name} must be declared: pass a port or ABSENT")
            if value is not ABSENT:
                _check(value, name, methods, attributes)

    def require(self, name: str, message: str | None = None):
        """The installed optional port, or the explicit ``operation_unavailable`` refusal."""
        value = getattr(self, name)
        if value is ABSENT:
            raise DomainError("operation_unavailable", message or f"The {name} domain is not installed")
        return value
