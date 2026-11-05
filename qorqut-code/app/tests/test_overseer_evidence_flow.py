"""End-to-end local host adapter checks with synthetic Operations records."""
from types import SimpleNamespace
from operational_support import BUSINESS, OperationalHost as EngineHost
from bazarlyk.overseer.evidence import EvidenceProvider


class OperationalHost(EngineHost):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This focused engine fixture also needs the host's review association.
        with self.database.transaction() as uow:
            uow.connection.execute('CREATE TABLE overseer_signoffs(signoff_id TEXT PRIMARY KEY)')


class Access:
    def resolve_selection(self, selection):
        return {**selection, "namespaces": [{"kind": "business", "business_id": BUSINESS}]}
    def authorize_source(self, ref):
        assert ref["namespace"] == {"kind": "business", "business_id": BUSINESS}


class Coverage:
    def completed_sources(self, refs):
        return []


def test_real_tasks_and_action_history_are_captured_and_recovered(tmp_path):
    host = OperationalHost(tmp_path / "host")
    with host.database.transaction() as uow:
        uow.connection.execute("CREATE TABLE IF NOT EXISTS documents(id TEXT,business_id TEXT,kind TEXT)")
        uow.connection.execute("CREATE TABLE IF NOT EXISTS native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,captured_length INTEGER)")
    task = host.create_task("Synthetic task")
    host.data("tasks_comment", {"task_id": task["id"], "text": "Synthetic correction"}, host.agent(), expected_revision=1)
    action = host.record_action("Synthetic result", task_id=task["id"])
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Synthetic revised result"},
              host.agent(), expected_revision=1)
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    result = provider.list_changes({"mode": "explicit", "namespaces": [{"kind": "business", "business_id": BUSINESS}]}, limit=20)
    kinds = [ref["kind"] for ref in result["items"]]
    assert "task" in kinds and "task_message" in kinds and "action_revision" in kinds
    note = next(ref for ref in result["items"] if ref["kind"] == "task_message")
    assert provider.get_source(note)["content"]["body"] == "Synthetic correction"
    page = provider.get_episode({"id": note["episode_id"]}, result["frontier"])
    assert len(page["source_refs"]) >= 2
    assert any(ref["kind"] == "action_revision" for ref in page["source_refs"])
    revisions = [ref for ref in page["source_refs"] if ref["kind"] == "action_revision"]
    assert revisions[-1]["before_ref"]["revision"] == "1"
    assert revisions[-1]["after_ref"]["revision"] == "2"
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    assert provider.get_source(note)["content"]["body"] == "Synthetic correction"


def test_pre_read_and_exact_admission_precede_body_and_private_capture(tmp_path, monkeypatch):
    from qorqut_operations.contracts import DomainError
    from bazarlyk.overseer.evidence import OperationsReaders
    host = OperationalHost(tmp_path / "host")
    task = host.create_task("Synthetic private task")
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    class Ordered(Access):
        def __init__(self):
            self.deny = "locator"
            self.checked = []
        def authorize_source(self, ref):
            super().authorize_source(ref)
            phase = "exact" if "content_hash" in ref else "locator"
            self.checked.append(phase)
            if phase == self.deny:
                raise DomainError("forbidden", "denied")
    access = Ordered()
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, access, overseer_ids={"overseer"})
    calls = []
    original = OperationsReaders.read
    def observed_read(self, *args, **kwargs):
        calls.append("read")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(OperationsReaders, "read", observed_read)
    selection = {"kind": "business", "business_id": BUSINESS}
    with host.database.read() as uow:
        try:
            provider.scanner.capture("s", selection, "task", task["id"], uow)
        except DomainError:
            pass
    assert "exact" not in access.checked and calls == []
    assert not list(root.glob("*.json"))
    access.deny = "exact"
    access.checked.clear()
    with host.database.read() as uow:
        try:
            provider.scanner.capture("s", selection, "task", task["id"], uow)
        except DomainError:
            pass
    assert calls and access.checked[0] == "locator" and access.checked[-1] == "exact"
    assert not list(root.glob("*.json"))


def test_signoff_response_and_execution_outcome_preserve_actual_attribution(tmp_path):
    host = OperationalHost(tmp_path / "host")
    action = host.record_action("Synthetic result")
    host.agents.overrides["actions_correct"] = "signoff"
    identity = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Synthetic correction"},
                          host.agent(), expected_revision=1)
    assert host.apply_approved(identity)["state"] == "applied"
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    with host.database.read() as uow:
        refs, gaps = provider.scanner._signoff("selection", {"kind": "business", "business_id": BUSINESS}, identity, uow)
    response = next(ref for ref in refs if ref["kind"] == "signoff_response")
    outcome = next(ref for ref in refs if ref["kind"] == "execution_outcome")
    assert response["actor_kind"] == "human"
    assert provider.get_source(response)["content"]["kind"] == "approve"
    assert provider.get_source(outcome) is not None


def test_provider_reoffers_lost_capture_after_edit_and_restart(tmp_path):
    host = OperationalHost(tmp_path / "host")
    with host.database.transaction() as uow:
        uow.connection.execute("CREATE TABLE IF NOT EXISTS documents(id TEXT,business_id TEXT,kind TEXT)")
        uow.connection.execute("CREATE TABLE IF NOT EXISTS native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,captured_length INTEGER)")
    task = host.create_task("Synthetic first")
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    selection = {"mode": "explicit", "namespaces": [{"kind": "business", "business_id": BUSINESS}]}
    class CoverageState:
        def __init__(self): self.completed = []
        def completed_sources(self, refs): return self.completed
    coverage = CoverageState()
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=coverage)
    first = provider.list_changes(selection, limit=20)
    old = next(ref for ref in first["items"] if ref["kind"] == "task")
    host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Synthetic second"}}, host.agent(), expected_revision=1)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=coverage)
    second = provider.list_changes(selection, limit=20)
    tasks = [ref for ref in second["items"] if ref["kind"] == "task"]
    assert len(tasks) == 2
    assert provider.get_source(old)["content"]["title"] == "Synthetic first"
    coverage.completed = [old]
    third = provider.list_changes(selection, limit=20)
    remaining = [ref for ref in third["items"] if ref["kind"] == "task"]
    assert len(remaining) == 1 and provider.get_source(remaining[0])["content"]["title"] == "Synthetic second"


def test_pending_pagination_does_not_starve_new_inventory(tmp_path):
    host = OperationalHost(tmp_path / "host")
    with host.database.transaction() as uow:
        uow.connection.execute("CREATE TABLE IF NOT EXISTS documents(id TEXT,business_id TEXT,kind TEXT)")
        uow.connection.execute("CREATE TABLE IF NOT EXISTS native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,captured_length INTEGER)")
    tasks = [host.create_task(f"Synthetic task {i}") for i in range(3)]
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    selection = {"mode": "explicit", "namespaces": [{"kind": "business", "business_id": BUSINESS}]}
    cursor, seen = None, set()
    for _ in range(10):
        page = provider.list_changes(selection, cursor=cursor, limit=1)
        seen.update(ref["record_id"] for ref in page["items"] if ref["kind"] == "task")
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == {task["id"] for task in tasks}


def test_overseer_signoff_and_its_owner_response_are_excluded(tmp_path):
    from operational_support import AGENT
    host = OperationalHost(tmp_path / "host")
    action = host.record_action("Synthetic result")
    host.agents.overrides["actions_correct"] = "signoff"
    identity = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Synthetic correction"},
                          host.agent(), expected_revision=1)
    assert host.apply_approved(identity)["state"] == "applied"
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={AGENT}, coverage=Coverage())
    with host.database.read() as uow:
        refs, gaps = provider.scanner._signoff("selection", {"kind": "business", "business_id": BUSINESS}, identity, uow)
    assert len(refs) == 1 and refs[0]["provenance"]["overseer_origin"]
    assert provider.journal.pending("selection")["items"] == []


def test_task_selection_includes_linked_action_without_unrelated_tasks(tmp_path):
    host = OperationalHost(tmp_path / "host")
    with host.database.transaction() as uow:
        uow.connection.execute("CREATE TABLE IF NOT EXISTS documents(id TEXT,business_id TEXT,kind TEXT)")
        uow.connection.execute("CREATE TABLE IF NOT EXISTS native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,captured_length INTEGER)")
    selected = host.create_task("Synthetic selected")
    other = host.create_task("Synthetic unrelated")
    action = host.record_action("Synthetic result", task_id=selected["id"])
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    selection = {"mode": "explicit", "namespaces": [{"kind": "business", "business_id": BUSINESS}], "task_ids": [selected["id"]]}
    page = provider.list_changes(selection, limit=20)
    assert any(ref["record_id"] == action["action_id"] for ref in page["items"])
    assert all(ref["record_id"] != other["id"] for ref in page["items"])


def test_bounded_thread_scan_continues_older_messages_on_next_inventory_pass(tmp_path, monkeypatch):
    import bazarlyk.overseer.evidence as evidence
    from operational_support import HUMAN
    monkeypatch.setattr(evidence, "MAX_THREAD_PAGES", 1)
    host = OperationalHost(tmp_path / "host")
    task = host.create_task("Synthetic long discussion")
    with host.database.transaction() as uow:
        for i in range(101):
            uow.connection.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,attribution_json,created_at) "
                "VALUES(?,?,'task',?,'comment',?,?,?,'{\"kind\":\"self\"}',?)",
                (BUSINESS, f"note-{i:03}", task["id"], f"Synthetic note {i}", HUMAN, HUMAN, "2026-10-12T12:00:00Z"))
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    namespace = {"kind": "business", "business_id": BUSINESS}
    with host.database.read() as uow:
        first, gaps = provider.scanner._task("selection", namespace, task["id"], uow)
    assert any(gap["code"] == "task_thread_continuation_pending" for gap in gaps)
    all_refs = list(first)
    for _ in range(101):
        with host.database.read() as uow:
            page, gaps = provider.scanner._task("selection", namespace, task["id"], uow)
        all_refs.extend(page)
        if not any(gap["code"] == "task_thread_continuation_pending" for gap in gaps):
            break
    messages = {ref["record_id"] for ref in all_refs if ref["kind"] == "task_message"}
    assert len(messages) == 101 and not any(gap["code"] == "task_thread_continuation_pending" for gap in gaps)
    assert sum(ref['kind'] == 'task_message' for ref in all_refs) == 101


def test_action_revision_scan_continues_and_links_previous_revision(tmp_path, monkeypatch):
    import bazarlyk.overseer.evidence as evidence
    monkeypatch.setattr(evidence, "MAX_HISTORY_REVISIONS", 1)
    host = OperationalHost(tmp_path / "host")
    action = host.record_action("Synthetic before")
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Synthetic after"}, host.agent(), expected_revision=1)
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    namespace = {"kind": "business", "business_id": BUSINESS}
    with host.database.read() as uow:
        first, gaps = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert first[0]["revision"] == "1" and {"code": "action_history_continuation_pending"} in gaps
    with host.database.read() as uow:
        second, gaps = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert second[0]["revision"] == "2" and second[0]["before_ref"]["revision"] == "1"
    assert provider.journal.get_source(second[0]["before_ref"])["text"] == "Synthetic before"


def test_signoff_amendment_retains_author_and_exact_before_after(tmp_path):
    import json
    from qorqut_operations.contracts import new_id
    from operational_support import HUMAN
    host = OperationalHost(tmp_path / "host")
    action = host.record_action("Synthetic result")
    host.agents.overrides["actions_correct"] = "signoff"
    original = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Synthetic first proposal"},
                          host.agent(), expected_revision=1)
    amended = new_id()
    with host.database.transaction() as uow:
        row = dict(uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (original,)).fetchone())
        row.update(id=amended, proposal_revision=2, supersedes_id=original, superseded_by_id=None,
                   updated_sequence=row["updated_sequence"] + 100)
        preview = json.loads(row["preview_json"])
        preview["owner_edit"] = {"by": HUMAN, "text": "Synthetic revised proposal"}
        row["preview_json"] = json.dumps(preview)
        columns = list(row)
        uow.connection.execute("INSERT INTO signoffs (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                               [row[column] for column in columns])
        uow.connection.execute("UPDATE signoffs SET superseded_by_id=? WHERE id=?", (amended, original))
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, Access(), overseer_ids={"overseer"}, coverage=Coverage())
    with host.database.read() as uow:
        refs, gaps = provider.scanner._signoff("selection", {"kind": "business", "business_id": BUSINESS}, amended, uow)
    edit = next(ref for ref in refs if ref["kind"] == "signoff" and ref["record_id"] == amended)
    assert edit["actor_id"] == HUMAN and edit["actor_kind"] == "human"
    assert edit["before_ref"]["record_id"] == original and edit["after_ref"]["record_id"] == amended
    assert provider.journal.get_source(edit["before_ref"])["id"] == original


def action_relink_fixture(tmp_path, *, excluded=False):
    from operational_support import HUMAN
    host = OperationalHost(tmp_path / "host")
    old_task = host.create_task("Synthetic original task")
    new_task = host.create_task("Synthetic replacement task", ctx=host.agent())
    action = host.record_action("Synthetic action", task_id=old_task["id"])
    root = tmp_path / "archive"
    root.mkdir(mode=0o700)
    class TaskAccess(Access):
        denied = set()
        check_exists = False
        def authorize_source(self, ref):
            super().authorize_source(ref)
            if ref.get("kind") == "task" and ref.get("record_id") in self.denied:
                from qorqut_operations.contracts import DomainError
                raise DomainError("forbidden", "Synthetic revoked task")
            if self.check_exists and ref.get("kind") == "task":
                with host.database.read() as uow:
                    row = uow.connection.execute("SELECT 1 FROM tasks WHERE business_id IS ? AND id=?", (BUSINESS, ref.get("record_id"))).fetchone()
                if row is None:
                    from qorqut_operations.contracts import DomainError
                    raise DomainError("forbidden", "Synthetic deleted task")
    access = TaskAccess()
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), root, access,
                                overseer_ids={HUMAN} if excluded else {"overseer"}, coverage=Coverage())
    return host, provider, access, old_task, new_task, action, {"kind": "business", "business_id": BUSINESS}


def test_action_relinked_and_removed_tasks_preserve_each_immutable_revision(tmp_path):
    host, provider, _, old_task, new_task, action, namespace = action_relink_fixture(tmp_path)
    with host.database.read() as uow:
        first, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    host.data("actions_correct", {"action_id": action["action_id"], "evidence": {"task": new_task["id"]}},
              host.agent(), expected_revision=1)
    with host.database.read() as uow:
        second, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert second[0] == first[0]
    assert provider.journal.get_source(second[0])["refs"]["task"] == old_task["id"]
    assert second[0]["provenance"]["original_refs"][0]["record_id"] == old_task["id"]
    assert provider.journal.get_source(second[1])["refs"]["task"] == new_task["id"]
    assert second[1]["provenance"]["original_refs"][0]["record_id"] == new_task["id"]
    assert second[1]["before_ref"] == first[0]
    host.data("actions_correct", {"action_id": action["action_id"], "evidence": {}}, host.agent(), expected_revision=2)
    with host.database.read() as uow:
        third, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert third[:2] == second
    assert "task" not in provider.journal.get_source(third[2])["refs"]
    assert third[2]["provenance"]["original_refs"] == [] and third[2]["before_ref"] == second[1]


def test_action_relink_cannot_hide_excluded_historical_task(tmp_path):
    from qorqut_operations.contracts import DomainError
    import pytest
    host, provider, _, old_task, new_task, action, namespace = action_relink_fixture(tmp_path, excluded=True)
    with host.database.read() as uow:
        first, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert first == [] and provider.journal.pending("selection")["items"] == []
    host.data("actions_correct", {"action_id": action["action_id"], "evidence": {"task": new_task["id"]}},
              host.agent(), expected_revision=1)
    with host.database.read() as uow:
        second, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert second == [] and provider.journal.pending("selection")["items"] == []
    excluded = provider.journal.captured_revision("operations", "action_revision", namespace, action["action_id"], 2)
    assert excluded["before_ref"]["provenance"]["overseer_origin"] is True
    with pytest.raises(DomainError) as refused:
        provider.journal.get_source(excluded)
    assert refused.value.code == "forbidden"


def test_revoked_or_deleted_historical_task_stays_a_gap_after_relink(tmp_path):
    import pytest
    from qorqut_operations.contracts import DomainError
    host, provider, access, old_task, new_task, action, namespace = action_relink_fixture(tmp_path)
    with host.database.read() as uow:
        first, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    host.data("actions_correct", {"action_id": action["action_id"], "evidence": {"task": new_task["id"]}},
              host.agent(), expected_revision=1)
    access.denied.add(old_task["id"])
    with host.database.read() as uow:
        denied, gaps = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert denied == [] and {"code": "related_source_forbidden"} in gaps
    assert provider.journal.pending("selection")["items"] == []
    with pytest.raises(DomainError):
        provider.journal.get_source(first[0])
    access.denied.clear()
    with host.database.transaction() as uow:
        uow.connection.execute("DELETE FROM tasks WHERE business_id=? AND id=?", (BUSINESS, old_task["id"]))
    with host.database.read() as uow:
        missing, gaps = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert missing == [] and {"code": "related_source_unavailable"} in gaps
    access.check_exists = True
    assert provider.journal.pending("selection")["items"] == []
    with pytest.raises(DomainError):
        provider.journal.get_source(first[0])


def test_existing_observation_is_withheld_when_task_ancestry_becomes_excluded(tmp_path):
    import pytest
    from qorqut_operations.contracts import DomainError
    from operational_support import HUMAN
    host, provider, _, old_task, new_task, action, namespace = action_relink_fixture(tmp_path)
    with host.database.read() as uow:
        first, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert first and provider.journal.get_source(first[0])["refs"]["task"] == old_task["id"]
    # Simulates a later canonical exclusion decision without rewriting history.
    provider.journal.overseer_ids = frozenset({HUMAN})
    assert provider.journal.pending("selection")["items"] == []
    with pytest.raises(DomainError):
        provider.journal.get_source(first[0])
    host.data("actions_correct", {"action_id": action["action_id"], "evidence": {"task": new_task["id"]}},
              host.agent(), expected_revision=1)
    with host.database.read() as uow:
        second, _ = provider.scanner._action("selection", namespace, action["action_id"], uow)
    assert second == [] and provider.journal.pending("selection")["items"] == []
    original = provider.journal.captured_revision("operations", "action_revision", namespace, action["action_id"], 1)
    assert original == first[0]
