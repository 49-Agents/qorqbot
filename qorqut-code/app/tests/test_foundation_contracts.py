from dataclasses import replace
import json

from jsonschema import Draft202012Validator, SchemaError
import pytest

import bazarlyk.application.registry as registry_module
from bazarlyk.application import CatalogEntry, OperationDescriptor, OperationRegistry
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationOptions, OperationResult,
                               Principal, ResourceRef, canonical_bytes, canonical_hash, freeze, new_id, plain)
from foundation_support import BUSINESS, context, task_plan


def test_exact_canonical_roundtrip_and_defensive_freezing():
    args = {"id": "task-1", "title": "Original"}
    plan = task_plan(context(), args)
    raw = plan.canonical
    args["title"] = "Changed outside the snapshot"
    assert plan.arguments["title"] == "Original"
    with pytest.raises(TypeError):
        plan.changes[0].values["title"] = "Mutation"
    restored = ChangeSet.from_bytes(raw, expected_hash=plan.payload_hash)
    assert restored.canonical == raw
    assert restored.payload_hash == plan.payload_hash
    assert replace(plan, review_business_id=BUSINESS).payload_hash != plan.payload_hash


@pytest.mark.parametrize("tampering", ["whitespace", "duplicate-key", "wrong-hash", "bad-field"])
def test_change_set_refuses_changed_or_noncanonical_bytes(tampering):
    plan = task_plan(context(), {"id": "task-1"})
    value, digest = plan.canonical, plan.payload_hash
    if tampering == "whitespace":
        value = json.dumps(plan.to_dict()).encode()
    elif tampering == "duplicate-key":
        value = b'{"operation_version":1,' + value[1:]
    elif tampering == "wrong-hash":
        digest = "0" * 64
    else:
        row = plan.to_dict()
        row["unexpected"] = True
        value = canonical_bytes(row)
    with pytest.raises(DomainError, match="Stored prepared change"):
        ChangeSet.from_bytes(value, expected_hash=digest)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "numeric key"}, b"bytes", {"a", "b"}])
def test_canonical_json_rejects_ambiguous_values(value):
    with pytest.raises(ValueError):
        canonical_bytes(value)
    with pytest.raises(ValueError):
        freeze(value)


def test_freeze_nested_values_is_a_defensive_snapshot():
    from dataclasses import dataclass
    @dataclass
    class Payload:
        items: list
        _private: object
    value = Payload([{"value": [True, None, 3, 1.25]}], object())
    snapshot = freeze(value)
    assert plain(snapshot) == plain(value)
    value.items[0]["value"].append("later")
    assert len(snapshot["items"][0]["value"]) == 4
    with pytest.raises(TypeError):
        snapshot["items"][0]["value"] = ()


def test_json_hash_ignores_mapping_insertion_order_but_not_array_order():
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])


@pytest.mark.parametrize("identity", [None, 1, "", "not-a-uuid"])
def test_required_principal_id_is_not_optional(identity):
    with pytest.raises(ValueError):
        Principal(identity, "human")


@pytest.mark.parametrize("identity", [None, 1, ""])
def test_resource_identity_is_a_required_string(identity):
    with pytest.raises(ValueError):
        ResourceRef("tasks", identity)


@pytest.mark.parametrize("options", [{"actor": "someone"}, {"expected_revision": True}, {"expected_revision": 0},
    {"idempotency_key": " "}, {"idempotency_key": "a" * 201}, {"business_id": "not-a-uuid"},
    {"supersedes_signoff_id": BUSINESS}, {"expected_superseded_status_revision": 2}])
def test_invalid_options_fail_closed(options):
    with pytest.raises(DomainError):
        InvocationOptions.from_mapping(options)


def test_metadata_does_not_fabricate_installed_handlers():
    registry = builtin_registry()
    assert registry.metadata("tasks_complete").supports_signoff
    assert registry.metadata("signoffs_review").owner_only
    assert "mcp" in registry.metadata("context_list").exposure
    assert "mcp" not in registry.metadata("context_maps_get").exposure
    with pytest.raises(DomainError, match="not installed"):
        registry.get("tasks_create")
    with pytest.raises(DomainError, match="Unknown"):
        registry.get("arbitrary_sql")


@pytest.mark.parametrize("changes", [
    {"exposure": frozenset({"mcp", "worker"})}, {"supports_signoff": True},
    {"effect_kind": "read"}, {"prepare": lambda *_: None},
])
def test_transport_only_descriptors_cannot_gain_domain_handlers_or_blanket_approval(changes):
    descriptor = builtin_registry().get("operations_batch")
    assert descriptor.transport_only and descriptor.read is descriptor.prepare is descriptor.apply is None
    with pytest.raises(ValueError):
        replace(descriptor, **changes)


def test_alias_and_authority_metadata_cannot_collide_or_expand_exposure():
    registry = OperationRegistry([CatalogEntry("private_read", False, "read", frozenset({"dashboard"}))])
    with pytest.raises(ValueError, match="authority"):
        registry.register(OperationDescriptor("private_read", {"type": "object"}, effect_kind="read", supports_signoff=False,
            read=lambda *_: {}))
    registry.register(OperationDescriptor("private_read", {"type": "object"}, effect_kind="read", supports_signoff=False,
        exposure=frozenset({"dashboard"}), aliases=("read_private",), read=lambda *_: {}))
    assert registry.get("read_private") is registry.get("private_read")
    with pytest.raises(ValueError):
        registry.register(OperationDescriptor("read_private", {"type": "object"}, effect_kind="read", supports_signoff=False,
            read=lambda *_: {}))


def test_descriptor_rejects_unknown_inputs_and_invalid_formats():
    descriptor = OperationDescriptor("things_read", {"type": "object", "properties": {"id": {"type": "string", "format": "uuid"}}},
        effect_kind="read", supports_signoff=False, read=lambda *_: {})
    for value in ({"actor": "spoof"}, {"id": "bad-id"}, []):
        with pytest.raises(DomainError):
            descriptor.validate(value)


@pytest.fixture
def metaschema_checks(monkeypatch):
    """Every real Draft 2020-12 metaschema validation, so cache hits and misses can be told apart."""
    checks, real = [], Draft202012Validator.check_schema

    def counting(schema, *args, **kwargs):
        checks.append(schema)
        return real(schema, *args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "check_schema", staticmethod(counting))
    return checks


def unique_schema(field=None):
    """A valid schema no earlier test in this process can have cached."""
    return {"type": "object", "properties": {field or "field_" + new_id(): {"type": "string", "maxLength": 20}}}


def read_descriptor(input_schema, output_schema=None):
    return OperationDescriptor("things_read", input_schema, effect_kind="read", supports_signoff=False, read=lambda *_: {},
                               **({} if output_schema is None else {"output_schema": output_schema}))


def test_equivalent_schemas_built_independently_are_metaschema_checked_once(metaschema_checks):
    field = "field_" + new_id()
    read_descriptor(unique_schema(field), unique_schema(field))
    assert len(metaschema_checks) == 2  # the input gains additionalProperties: false, so it is a different schema
    rebuilt = {"properties": {field: {"maxLength": 20, "type": "string"}}, "type": "object"}
    read_descriptor(rebuilt, unique_schema(field))
    assert len(metaschema_checks) == 2


def test_changed_schema_is_a_new_key_and_invalid_schemas_fail_every_time(metaschema_checks):
    schema = unique_schema()
    read_descriptor(schema)
    checked = len(metaschema_checks)
    (field,) = schema["properties"]
    schema["properties"][field]["type"] = "not-a-json-type"  # the very same dict object, different content
    for attempt in (1, 2):
        with pytest.raises(SchemaError):
            read_descriptor(schema)
        assert len(metaschema_checks) == checked + attempt  # a failure is checked again, never remembered
    schema["properties"][field]["type"] = "string"
    read_descriptor(schema)
    assert len(metaschema_checks) == checked + 2  # the restored content is the original, cached key


@pytest.mark.parametrize("invalid_side", ["input", "output"])
def test_invalid_input_or_output_schema_is_rejected_after_valid_ones_were_cached(invalid_side):
    valid, invalid = unique_schema(), {"type": "object", "properties": {"x": {"type": "no-such-type"}}}
    read_descriptor(valid, valid)
    with pytest.raises(SchemaError):
        read_descriptor(*((invalid, valid) if invalid_side == "input" else (valid, invalid)))


def test_cached_schemas_still_validate_every_invocation_and_result(metaschema_checks):
    field = "field_" + new_id()

    def build():
        return read_descriptor({"type": "object", "properties": {field: {"type": "integer"}}, "required": [field]},
                               {"type": "object", "required": ["value"]})

    first = build()
    checked = len(metaschema_checks)
    second = build()
    assert len(metaschema_checks) == checked  # both of the second descriptor's schemas came from the cache
    for descriptor in (first, second):
        assert descriptor.validate({field: 1}) == {field: 1}
        for invalid in ({}, {field: "text"}, {field: 1, "actor": "spoof"}):
            with pytest.raises(DomainError) as error:
                descriptor.validate(invalid)
            assert error.value.code == "validation_error"
        good = OperationResult(request_id=new_id(), status="ok", data={"value": 1})
        assert descriptor.validate_output(good) is good
        with pytest.raises(DomainError) as error:
            descriptor.validate_output(OperationResult(request_id=new_id(), status="ok", data={}))
        assert error.value.code == "invalid_result"


def test_schema_check_cache_is_a_bounded_least_recently_used_set(monkeypatch, metaschema_checks):
    monkeypatch.setattr(registry_module, "SCHEMA_CHECK_CACHE_LIMIT", 2)
    first, second, third = unique_schema(), unique_schema(), unique_schema()
    for schema in (first, second, first, third):  # the third evicts the second, the least recently used
        registry_module.check_schema_cached(schema)
    assert len(metaschema_checks) == 3
    registry_module.check_schema_cached(first)
    assert len(metaschema_checks) == 3
    registry_module.check_schema_cached(second)
    assert len(metaschema_checks) == 4


def test_result_roundtrip_and_error_details():
    result = OperationResult.failure(DomainError("stale_signoff", "Changed", current_revision=4, details={"signoff": {"state": "applied"}}))
    assert OperationResult.from_dict(result.to_dict()).to_dict() == result.to_dict()
    assert result.to_dict()["error"]["details"]["signoff"]["state"] == "applied"
    assert "warnings" not in result.to_dict()
