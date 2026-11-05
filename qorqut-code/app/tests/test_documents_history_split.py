"""The documents/history split: the committed schema statement for statement, the host's tracked-table union, and the
aggregate's explicit composition. No runtime, provider or live data."""

from types import SimpleNamespace

import pytest

from bazarlyk.composition import schema_ddl
from bazarlyk.domains.documents import DocumentOperations
from bazarlyk.storage import history
from qorqut_operations.document_history import schema as history_schema
from qorqut_operations.documents import schema as documents_schema

DOCUMENTS = schema_ddl.OWNER_SCHEMA["documents"]


def test_the_operations_documents_statements_are_the_committed_ones_verbatim():
    assert documents_schema.SCHEMA == tuple(DOCUMENTS[0:2])
    assert documents_schema.CHANGE_CAPTURE == tuple(DOCUMENTS[2:5])
    assert all(statement.startswith("CREATE TRIGGER inbox_capture_documents_") for statement in documents_schema.CHANGE_CAPTURE)
    assert DOCUMENTS[0].startswith("CREATE TABLE documents ") and DOCUMENTS[1].startswith("CREATE INDEX documents_scope ")


def test_the_shared_history_statements_are_the_committed_ones_verbatim():
    assert history_schema.SCHEMA == tuple(DOCUMENTS[15:23])
    assert {name for statement in history_schema.SCHEMA for name in history_schema.HISTORY_TABLES if name in statement} == set(
        history_schema.HISTORY_TABLES)
    assert set(history_schema.HISTORY_TABLES) <= set(schema_ddl.OWNER_TABLES["documents"])


def test_the_host_still_tracks_exactly_the_records_it_tracked_before_the_split():
    assert history.TRACKED_TABLES == {"documents", "tasks", "action_logs", "learning_candidates", "learning_decisions",
                                      "learning_history"}
    assert tuple(history.RECORDS.facts) == ("action_logs", "learning_history")
    assert set(history.SQL.descriptors) == {"learning_candidates", "learning_decisions"}
    assert set(history.RECEIPTS.byte_capture) == {history.WORKING_MEMORY}
    # The module functions the reserved composition calls delegate to the composed mechanism.
    assert callable(history.record_applied) and callable(history.capture_receipt)


def test_the_aggregate_composes_only_when_every_host_collaborator_is_named(tmp_path):
    paths = SimpleNamespace(operations=tmp_path / "operations", root=tmp_path)
    database = SimpleNamespace(path=tmp_path / "state.sqlite3")
    documents = DocumentOperations(database, paths)
    for kwargs in ({"working_memory": None, "shared_context": object()}, {"working_memory": object(), "shared_context": None}):
        with pytest.raises(TypeError):
            documents.register(SimpleNamespace(), SimpleNamespace(), **kwargs)
    assert not hasattr(DocumentOperations, "__getattr__")
