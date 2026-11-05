"""Synthetic records and a disposable history database; never live transcripts."""
from types import SimpleNamespace
import sqlite3
import pytest
from qorqut_business_docs_versioning import HistoryStore, initialize_schema
from qorqut_operations.contracts import DomainError
from bazarlyk.overseer.evidence import AdmittedReaders, resolve_selection


class Access:
    def __init__(self):
        self.denied = False
    def resolve_selection(self, value):
        if self.denied:
            raise DomainError("forbidden", "denied")
        return {**value, "namespaces": value.get("namespaces", [{"kind": "global", "business_id": None}])}
    def authorize_source(self, ref):
        if self.denied:
            raise DomainError("forbidden", "denied")


def test_selection_requires_admission_and_explicit_modes():
    access = Access()
    assert resolve_selection(access, {"mode": "all_authorized"})["namespaces"] == [{"kind": "global", "business_id": None}]
    with pytest.raises(DomainError):
        resolve_selection(None, {"mode": "all_authorized"})
    with pytest.raises(DomainError) as unsupported:
        resolve_selection(access, {"mode": "all_authorized", "workflow_refs": ["workflow"]})
    assert unsupported.value.code == "unsupported_filter"
    with pytest.raises(DomainError):
        resolve_selection(access, {"mode": "explicit"})


def test_document_versions_use_admission_and_exact_registered_revision(tmp_path):
    conn = sqlite3.connect(tmp_path / "synthetic.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    initialize_schema(conn)
    conn.execute("CREATE TABLE documents(id TEXT, business_id TEXT, kind TEXT, owning_agent_id TEXT, source_refs_json TEXT)")
    conn.execute("INSERT INTO documents VALUES('doc', NULL, 'note', NULL, '[]')")
    history = HistoryStore(conn)
    history.save_document("global", "doc", "before", "author", expected_version=0, reason="synthetic")
    history.save_document("global", "doc", "after", "owner", expected_version=1, reason="synthetic")
    access = Access()
    reader = AdmittedReaders(None, access)
    ref = {"namespace": {"kind": "global", "business_id": None}, "locator": {"document_id": "doc", "version": 1}}
    assert reader.document_revision(ref, SimpleNamespace(connection=conn))["content"] == "before"
    ref["locator"]["version"] = 2
    assert reader.document_revision(ref, SimpleNamespace(connection=conn))["content"] == "after"
    access.denied = True
    with pytest.raises(DomainError) as refused:
        reader.document_revision(ref, SimpleNamespace(connection=conn))
    assert refused.value.code == "forbidden"
    conn.close()


def test_transcript_uses_registered_receipt_and_rechecks_admission():
    capture = {"provider": "codex", "conversation_id": "conversation", "receipt_id": "receipt"}
    class Transcripts:
        def listing(self, agent):
            assert agent == "agent"
            return [capture]
        def content(self, agent, row):
            assert row is capture
            return b'{"synthetic":true}\n'
    access = Access()
    reader = AdmittedReaders(SimpleNamespace(native_chat=SimpleNamespace(transcripts=Transcripts())), access)
    ref = {"locator": {"agent_id": "agent", "provider": "codex", "conversation_id": "conversation", "receipt_id": "receipt"}}
    assert reader.transcript(ref).startswith(b'{')
    ref["locator"]["receipt_id"] = "missing"
    with pytest.raises(DomainError) as missing:
        reader.transcript(ref)
    assert missing.value.code == "evidence_unavailable"
    access.denied = True
    with pytest.raises(DomainError) as refused:
        reader.transcript(ref)
    assert refused.value.code == "forbidden"


def test_operations_reader_uses_actual_scoped_operations_and_rechecks(tmp_path):
    from operational_support import BUSINESS, OperationalHost
    from bazarlyk.overseer.evidence import OperationsReaders
    host = OperationalHost(tmp_path)
    task = host.create_task("Synthetic source task")
    ctx = host.agent()
    access = Access()
    ref = {"provider": "operations", "kind": "task", "record_id": task["id"],
           "namespace": {"kind": "business", "business_id": BUSINESS}}
    readers = OperationsReaders(host.app, ctx, access)
    with host.database.read() as uow:
        result = readers.read("tasks_get", {"task_id": task["id"]}, ref, uow)
        assert result["task"]["title"] == "Synthetic source task"
        access.denied = True
        with pytest.raises(DomainError) as refused:
            readers.read("tasks_get", {"task_id": task["id"]}, ref, uow)
        assert refused.value.code == "forbidden"


def test_action_history_uses_real_revision_reader_and_authority(tmp_path):
    from operational_support import BUSINESS, OperationalHost
    from bazarlyk.overseer.evidence import OperationsReaders
    host = OperationalHost(tmp_path)
    action = host.record_action("Synthetic original")
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Synthetic corrected"},
              host.agent(), expected_revision=1)
    access = Access()
    reader = OperationsReaders(host.app, host.agent(), access)
    ref = {"provider": "operations", "kind": "action", "record_id": action["action_id"],
           "namespace": {"kind": "business", "business_id": BUSINESS}}
    with host.database.read() as uow:
        before = reader.read("actions_get", {"action_id": action["action_id"], "revision": 1}, ref, uow)
        after = reader.read("actions_get", {"action_id": action["action_id"], "revision": 2}, ref, uow)
    assert before["action"]["text"] == "Synthetic original"
    assert after["action"]["text"] == "Synthetic corrected"
    assert before["action"]["recorded_by"]["kind"] == "agent"


def test_document_scanner_captures_before_after_with_real_history(tmp_path):
    from bazarlyk.overseer.evidence import CaptureJournal, HostScanner
    conn = sqlite3.connect(tmp_path / "history.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    initialize_schema(conn)
    conn.execute("CREATE TABLE documents(id TEXT, business_id TEXT, kind TEXT, owning_agent_id TEXT, source_refs_json TEXT)")
    conn.execute("INSERT INTO documents VALUES('doc', NULL, 'note', NULL, '[]')")
    store = HistoryStore(conn)
    store.save_document("global", "doc", "before", "agent", expected_version=0, reason="synthetic")
    store.save_document("global", "doc", "after", "human", expected_version=1, reason="synthetic")
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    refs, gaps = scanner._document("selection", {"kind": "global", "business_id": None}, "doc", SimpleNamespace(connection=conn))
    assert not gaps and len(refs) == 2
    assert refs[1]["before_ref"]["revision"] == "1"
    assert refs[1]["after_ref"]["revision"] == "2"
    assert journal.get_source(refs[0])["content"] == "before"
    assert journal.get_source(refs[1])["content"] == "after"
    conn.close()


def test_selection_refuses_silent_namespace_or_filter_changes():
    class Dropping(Access):
        def resolve_selection(self, value):
            return {**value, "namespaces": []}
    with pytest.raises(DomainError) as refused:
        resolve_selection(Dropping(), {"mode": "explicit", "namespaces": [{"kind": "global", "business_id": None}]})
    assert refused.value.code == "forbidden"
    class DroppingFilter(Access):
        def resolve_selection(self, value):
            return {**value, "task_ids": [], "namespaces": [{"kind": "global", "business_id": None}]}
    with pytest.raises(DomainError):
        resolve_selection(DroppingFilter(), {"mode": "all_authorized", "task_ids": ["synthetic"]})


def test_document_mirror_of_overseer_source_is_withheld(tmp_path):
    import json
    from bazarlyk.overseer.evidence import CaptureJournal, HostScanner
    conn = sqlite3.connect(tmp_path / "history.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    initialize_schema(conn)
    conn.execute("CREATE TABLE documents(id TEXT,business_id TEXT,kind TEXT,owning_agent_id TEXT,source_refs_json TEXT)")
    conn.execute("INSERT INTO documents VALUES('original',NULL,'note','overseer','[]')")
    conn.execute("INSERT INTO documents VALUES('mirror',NULL,'note',NULL,?)",
                 (json.dumps([{"resource_type": "documents", "resource_id": "original", "business_id": None}]),))
    history = HistoryStore(conn)
    history.save_document("global", "original", "private feedback", "overseer", expected_version=0, reason="synthetic")
    history.save_document("global", "mirror", "copied feedback", "owner", expected_version=0, reason="synthetic")
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    refs, gaps = scanner._document("selection", {"kind": "global", "business_id": None}, "mirror", SimpleNamespace(connection=conn))
    assert refs == [] and gaps == [] and journal.pending("selection")["items"] == []
    conn.close()


@pytest.mark.parametrize('budget', ['count', 'deadline'])
def test_document_history_continuation_retains_exact_adjacent_revisions(tmp_path, monkeypatch, budget):
    import bazarlyk.overseer.evidence as evidence
    from bazarlyk.overseer.evidence import CaptureJournal, HostScanner
    monkeypatch.setattr(evidence, "MAX_HISTORY_REVISIONS", 2)
    conn = sqlite3.connect(tmp_path / "history.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    initialize_schema(conn)
    conn.execute("CREATE TABLE documents(id TEXT,business_id TEXT,kind TEXT,owning_agent_id TEXT,source_refs_json TEXT)")
    conn.execute("INSERT INTO documents VALUES('doc',NULL,'note',NULL,'[]')")
    history = HistoryStore(conn)
    for i in range(3):
        history.save_document("global", "doc", f"version-{i + 1}", "author", expected_version=i, reason="synthetic")
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    if budget == 'deadline':
        monkeypatch.setattr(scanner, 'expired', lambda: True)
    namespace = {"kind": "global", "business_id": None}
    first, gaps = scanner._document("selection", namespace, "doc", SimpleNamespace(connection=conn))
    assert [ref["revision"] for ref in first] == (["1", "2"] if budget == 'count' else ['1'])
    assert {"code": "document_history_continuation_pending"} in gaps
    second, gaps = scanner._document("selection", namespace, "doc", SimpleNamespace(connection=conn))
    if budget == 'deadline':
        assert [ref['revision'] for ref in second] == ['2']
        assert second[0]['before_ref']['revision'] == '1'
        second, gaps = scanner._document("selection", namespace, "doc", SimpleNamespace(connection=conn))
    assert [ref["revision"] for ref in second] == ["3"] and gaps == []
    assert second[0]["before_ref"]["revision"] == "2"
    assert journal.get_source(second[0]["before_ref"])["content"] == "version-2"
    conn.close()
