"""The host's record identity and rules beside the neutral Operations vocabulary and Task associations.

The host has no record tables of its own: it names stored documents and resource tables exactly as Operations does, its
lookup declines every hook but the configured-global agent identity, and its one Material rule refuses to classify a
historical email-message document. Synthetic rows only."""

from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError
from qorqut_operations.records import associations
from qorqut_operations.records.adapters import NOT_A_MATERIAL
from qorqut_operations.records.vocabulary import RESOURCE_TYPES

from bazarlyk.domains.items import compose
from bazarlyk.domains.items.adapters import DIRECT_RECORD_TABLES, WORKFLOW_ROLES, HostMaterialRules
from bazarlyk.domains.items.lookup import HostLookup

CTX = SimpleNamespace(business_id="business-1")
DOCUMENTS = {
    "note": {"id": "note", "kind": "business_note", "metadata_json": {}},
    "topic": {"id": "topic", "kind": "context_topic", "metadata_json": {}},
    "old-mail": {"id": "old-mail", "kind": "email_message", "metadata_json": {"conversation_id": "conversation-1"}},
}


class Documents:
    def get(self, identity):
        return DOCUMENTS.get(identity)


class Uow:
    def repository(self, name, business_id=None):
        assert name == "documents" and business_id == CTX.business_id
        return Documents()


def test_the_host_uses_the_operations_resource_tables_without_adding_any():
    assert compose.RESOURCE_TYPES is RESOURCE_TYPES
    assert list(RESOURCE_TYPES.items()) == [
        ("tasks", "task"), ("data_tables", "table"), ("agent_profiles", "agent"), ("action_logs", "action"), ("signoffs", "signoff"),
        ("context_entities", "entity")]
    assert not any("email" in table for table in RESOURCE_TYPES)
    assert DIRECT_RECORD_TABLES == {"context_entities"}
    assert WORKFLOW_ROLES == {"assigned_to": "tasks_assign", "blocked_by": "tasks_update"}


def test_every_stored_source_is_named_as_operations_names_it():
    uow = Uow()
    for identity, expected in (("note", "material"), ("topic", "topic"), ("old-mail", "material")):
        assert associations.source_item(CTX, uow, "documents", identity) == {"type": expected, "id": identity}
    for table, kind in RESOURCE_TYPES.items():
        assert associations.source_item(CTX, uow, table, "r-1") == {"type": kind, "id": "r-1"}
    with pytest.raises(KeyError):
        associations.source_item(CTX, uow, "email_messages", "r-1")
    with pytest.raises(DomainError) as missing:
        associations.source_item(CTX, uow, "documents", "absent")
    assert (missing.value.code, str(missing.value)) == ("not_found", "Document not found")


def test_a_historical_email_message_document_is_not_classified_and_nothing_else_is_refused():
    rules = HostMaterialRules()
    with pytest.raises(DomainError) as refused:
        rules.validate_material(CTX, "documents", DOCUMENTS["old-mail"], "reference", Uow())
    assert (refused.value.code, str(refused.value)) == ("workflow_required", NOT_A_MATERIAL)
    for identity in ("note", "topic"):
        assert rules.validate_material(CTX, "documents", DOCUMENTS[identity], "reference", Uow()) is None
    assert rules.fixed_material("documents", DOCUMENTS["note"]) is False


def test_the_host_lookup_declines_every_hook_but_the_configured_global_agent_identity():
    lookup, row = HostLookup(), {"id": "n"}
    assert lookup.material_resource(CTX, "r-1", Uow()) is None
    assert lookup.fallback_subtype("documents", row) is None
    assert lookup.relationship_item(CTX, {"type": "task", "id": "t"}, "tasks", row, Uow()) == {"type": "task", "id": "t"}
    assert lookup.fingerprint_content(CTX, "documents", row, "content", Uow()) is False
    assert lookup.include_candidate(CTX, "tasks") is True
    assert lookup.current_agent(SimpleNamespace(connection=None), {"home_business_id": "business-1", "agent_id": "a-1"}) is False
