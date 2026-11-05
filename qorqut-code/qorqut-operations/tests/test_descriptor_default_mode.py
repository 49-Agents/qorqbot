"""An operation may declare its standing default mode; the declaration is validated and changes no other gate."""

import pytest

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.registry import OperationRegistry

SCHEMA = {"type": "object"}
READ = dict(effect_kind="read", supports_signoff=False, read=lambda *_: {})
MUTATION = dict(prepare=lambda *_: None, apply=lambda *_: None)


def test_default_mode_is_absent_unless_declared():
    assert OperationDescriptor("things_get", SCHEMA, **READ).default_mode is None
    assert OperationDescriptor("things_get", SCHEMA, default_mode="allow", **READ).default_mode == "allow"


@pytest.mark.parametrize("mode", ["sometimes", "", "ALLOW"])
def test_an_unknown_default_mode_is_refused(mode):
    with pytest.raises(ValueError, match="default permission mode"):
        OperationDescriptor("things_get", SCHEMA, default_mode=mode, **READ)


def test_a_signoff_default_needs_exact_change_signoff_support():
    OperationDescriptor("things_set", SCHEMA, default_mode="signoff", **MUTATION)
    with pytest.raises(ValueError, match="Signoff support"):
        OperationDescriptor("things_set", SCHEMA, default_mode="signoff", supports_signoff=False, **MUTATION)


def test_an_owner_only_operation_can_only_default_to_deny():
    owner = dict(owner_only=True, exposure=frozenset({"dashboard"}), **MUTATION)
    OperationDescriptor("things_review", SCHEMA, default_mode="deny", **owner)
    with pytest.raises(ValueError, match="Owner-only"):
        OperationDescriptor("things_review", SCHEMA, default_mode="allow", **owner)


def test_the_registry_serves_the_declared_default_to_policy_rules():
    registry = OperationRegistry()
    registry.register(OperationDescriptor("things_get", SCHEMA, default_mode="allow", **READ))
    assert registry.get("things_get").default_mode == "allow"
