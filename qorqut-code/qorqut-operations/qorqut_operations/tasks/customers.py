"""The scoped Customer-state vocabulary: suggestions that grow with use and never depend on current usage.

Each owning scope (one business, or Global) has at most one ``business_configs`` row, domain ``task_customer_states``,
``{"v": 1, "states": [...]}``: the display spellings in the order they were first introduced. A Customer write that
introduces a label appends it in the same prepared plan as the Task row, pinned to the row's revision (or absence), so a
failed, denied or stale write adds nothing and two writers can never lose each other's label: the later one gets the
ordinary revision conflict. Labels are never removed, whether or not any Customer still uses them, and the row is
separate from the ``tasks`` configuration, so a configuration update cannot clear it. It grants nothing and triggers
nothing; it is only the list the owner is offered."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError, ResourceRef, plain
from qorqut_operations.tasks.types import STATE_LIMIT, normalize_state, state_key

DOMAIN = "task_customer_states"


def stored(config) -> list[str]:
    """The display labels of a stored configuration; a row that is not exactly the vocabulary is refused, not guessed at."""
    states = config.get("states") if isinstance(config, dict) and config.get("v") == 1 and set(config) == {"v", "states"} else None
    if (not isinstance(states, list) or any(not isinstance(label, str) or not label or label != normalize_state(label)
                                            or len(label) > STATE_LIMIT for label in states)
            or len({state_key(label) for label in states}) != len(states)):
        raise DomainError("validation_error", "The stored Customer states are not a valid vocabulary")
    return list(states)


def read(uow, business_id):
    """``(labels, row)`` for the owning scope; no row is an empty vocabulary."""
    row = uow.repository("business_configs", business_id=business_id).get(DOMAIN)
    return (stored(plain(row["config_json"])) if row else []), row


def introduced(before, after) -> str | None:
    """The label a Task write puts in use (an empty or unchanged state introduces nothing)."""
    return after if after and after != before else None


def append(builder, row, states, label):
    """Plan the vocabulary row with ``label`` appended, if no equal label is there yet. Returns whether it was added."""
    if any(state_key(item) == state_key(label) for item in states):
        return False
    config = {"v": 1, "states": [*states, label]}
    builder.record("business_configs", DOMAIN, row, {"config_json": config} if row else {"domain": DOMAIN, "config_json": config})
    return True


def check(plan, uow, business_id, before, after):
    """Prepared-plan validation: a Task write may change ``business_configs`` only by the one exact append of its new label.

    Any ``business_configs`` effect counts, whatever resource id or domain it claims. When the write introduces no new
    label there must be none; when it does there must be exactly one: this scope's vocabulary row, inserted (domain and
    config) while absent or updated (config only) while present, pinned to the row's existence and revision."""
    changes = [change for change in plan.changes if change.resource.resource_type == "business_configs"]
    label = introduced(before, after)
    if label is None:
        if changes:
            raise DomainError("invalid_plan", "This Task write does not change the Customer states")
        return
    states, row = read(uow, business_id)
    present = any(state_key(item) == state_key(label) for item in states)
    if present and label not in states:
        raise DomainError("validation_error", "A Customer state uses the first spelling of its label")
    if present:
        if changes:
            raise DomainError("invalid_plan", "This Task write does not change the Customer states")
        return
    resource = ResourceRef("business_configs", DOMAIN, business_id)
    config = {"v": 1, "states": [*states, label]}
    target = next((item for item in plan.expected_targets if item.resource == resource), None)
    if (len(changes) != 1 or changes[0].kind != "record" or changes[0].resource != resource or changes[0].operation_name is not None
            or changes[0].action != ("update" if row else "insert")
            or plain(changes[0].values) != ({"config_json": config} if row else {"domain": DOMAIN, "config_json": config})
            or target is None or target.exists != bool(row) or target.content_hash is not None
            or target.revision != (row["revision"] if row else None)):
        raise DomainError("invalid_plan", "A new Customer state is one exact append to this scope's vocabulary row")


__all__ = ["DOMAIN", "append", "check", "introduced", "read", "stored"]
