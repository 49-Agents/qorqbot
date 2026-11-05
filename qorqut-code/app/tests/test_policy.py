import pytest

from bazarlyk.application.catalog import builtin_registry
from bazarlyk.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from bazarlyk.policy import resolve_operation_policy


def test_policy_default_override_fallback_and_canonical_alias():
    registry = builtin_registry()
    registry.register(OperationDescriptor("tasks_create", {"type": "object"},
        aliases=("create_work",), prepare=lambda *args: None, apply=lambda *args: None))
    assert resolve_operation_policy("create_work", {"tasks_create": "allow"},
                                    {"tasks_create": "signoff"}, registry).mode == "signoff"
    decision = resolve_operation_policy("tasks_create", {"tasks_create": "signoff"},
                                        {"tasks_create": "allow"}, registry)
    assert (decision.mode, decision.provenance) == ("allow", "agent_override")
    assert resolve_operation_policy("tasks_create", {"tasks_create": "allow"}, {}, registry).provenance == "role_default"
    assert resolve_operation_policy("new_unknown", {}, {}, registry).mode == "deny"
    assert resolve_operation_policy("tasks_get", {}, {}, registry).mode == "deny"


@pytest.mark.parametrize("rule", [{"unknown": "allow"}, {"tasks_get": "signoff"},
    {"tasks_create": "sometimes"}, {"signoffs_review": "allow"}])
def test_invalid_grants_are_rejected(rule):
    with pytest.raises(DomainError, match="."):
        resolve_operation_policy("tasks_create", {}, rule, builtin_registry())


@pytest.mark.parametrize("role", [None, "onbashy", "ordubashy", "marketing", "founder"])
def test_learning_decisions_cannot_be_granted_to_other_roles_or_workers(role):
    decision = resolve_operation_policy("learnings_decide", {"learnings_decide": "allow"},
        {"learnings_decide": "allow"}, builtin_registry(), role_id=role)
    assert (decision.mode, decision.provenance) == ("deny", "learning_applier_only")


@pytest.mark.parametrize("role", [None, "onbashy", "ordubashy", "marketing", "founder"])
def test_learning_issue_actions_cannot_be_granted_to_other_roles_or_workers(role):
    decision = resolve_operation_policy("learnings_issue_action", {"learnings_issue_action": "allow"},
        {"learnings_issue_action": "allow"}, builtin_registry(), role_id=role)
    assert (decision.mode, decision.provenance) == ("deny", "learning_applier_only")


def test_learning_issue_actions_require_explicit_grant_and_honor_revocation():
    registry = builtin_registry()
    for defaults, overrides, expected in (({}, {}, "deny"), ({"learnings_issue_action": "allow"}, {}, "allow"),
                                          ({"learnings_issue_action": "allow"}, {"learnings_issue_action": "deny"}, "deny")):
        assert resolve_operation_policy("learnings_issue_action", defaults, overrides, registry,
            role_id="learning-applier").mode == expected
    assert registry.metadata("learnings_engine_enrollment_set").owner_only


def test_learning_decisions_require_explicit_grant_and_honor_revocation():
    registry = builtin_registry()
    for defaults, overrides, expected in (({}, {}, "deny"), ({"learnings_decide": "allow"}, {}, "allow"),
                                          ({"learnings_decide": "allow"}, {"learnings_decide": "deny"}, "deny")):
        assert resolve_operation_policy("learnings_decide", defaults, overrides, registry,
            role_id="learning-applier").mode == expected
    for name in ("learnings_review", "learnings_edit", "signoffs_review"):
        assert registry.metadata(name).owner_only
