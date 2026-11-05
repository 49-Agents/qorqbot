"""Acceptance case 4: document lifecycle, shared history, inbox finalization and change publication as host
contributions around the engine's units of work.

The real ``finalize_events`` and ``ChangeFeed`` are the contributions under test; failures are injected at their
boundaries (a wrapper around the real finalizer, a feed whose publication raises) so each case proves what the
engine does when a host contribution fails, not what a mock returns."""

import hashlib
import sqlite3

import pytest
from qorqut_operations.contracts import new_id

import bazarlyk.composition.transactions as transactions
from bazarlyk.service.changes import ChangeFeed
from host_foundation_support import agent_context, build_instance, create_registered_global, register_sources, tree

TABLES = ("tasks", "inbox_events", "inbox_deliveries", "inbox_changes", "documents", "qbdv_document_versions",
          "apply_receipts", "idempotency_receipts")


class RecordingFeed(ChangeFeed):
    """The real feed, remembering each publication and what an independent connection could see at that moment."""

    def __init__(self):
        super().__init__()
        self.published, self.observe, self.break_publication = [], None, False

    def publish_commit(self, *, captured, operation, business_id, wrote=True):
        seen = self.observe() if self.observe else None
        self.published.append((operation, tuple(sorted({row["table"] for row in captured})), seen))
        super().publish_commit(captured=captured, operation=operation, business_id=business_id, wrote=wrote)

    def _publish_commit(self, captured, operation, business_id, wrote):
        if self.break_publication:
            raise RuntimeError("injected publication failure")
        super()._publish_commit(captured, operation, business_id, wrote)


class Boundary:
    """Wraps the real inbox finalizer: records what the unit held when it ran and fails when told to."""

    def __init__(self, real):
        self.real, self.fail_on, self.seen = real, None, []

    def __call__(self, uow):
        sources = sorted({row[0] for row in uow.connection.execute("SELECT source_type FROM inbox_changes")})
        self.real(uow)
        self.seen.append((tuple(sources), uow.connection.in_transaction))
        if self.fail_on and (self.fail_on is True or self.fail_on in sources):
            raise RuntimeError("injected finalizer failure")


@pytest.fixture
def host(tmp_path, monkeypatch):
    boundary = Boundary(transactions.finalize_events)
    monkeypatch.setattr(transactions, "finalize_events", boundary)
    feed = RecordingFeed()
    instance, ids = build_instance(tmp_path, feed=feed)
    business = ids["first"]
    register_sources(instance, business, "first")
    agent = create_registered_global(instance, "ordubashy")       # ordinary business work: the coordinator role
    instance.runtime.freeze_registry()
    # Visible-to-another-connection evidence of the committed documents, taken at each publication.
    def committed_notes():
        connection = sqlite3.connect(f"file:{instance.paths.database}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM documents WHERE kind='agent_note'").fetchone()[0], \
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            connection.close()
    feed.observe = committed_notes
    boundary.seen.clear()
    feed.published.clear()
    return instance, business, agent, boundary, feed


def counts(instance):
    with instance.database.read() as uow:
        return {name: uow.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}


def assign(instance, business, agent, title, key="new"):
    return instance.invoke(instance.human(business), "tasks_create",
                           {"title": title, "scope": "business", "assignee_ids": [agent]}, key=key)


def write_note(instance, business, agent, key="new", title="Audit note", body="First audit body\n"):
    return instance.invoke(agent_context(agent, business), "notes_create", {"title": title, "body": body}, key=key)


def test_record_and_inbox_event_commit_in_one_unit_then_publish_after_commit(host):
    instance, business, agent, boundary, feed = host
    before = counts(instance)
    result = assign(instance, business, agent, "Review the plan")
    assert result.status == "ok", result.error
    after = counts(instance)

    assert after["tasks"] == before["tasks"] + 1
    assert after["inbox_events"] == before["inbox_events"] + 1 and after["inbox_deliveries"] == before["inbox_deliveries"] + 1
    assert after["inbox_changes"] == 0  # every pending capture was finalized (consumed) before COMMIT
    # The finalizer ran inside the open transaction and saw the unit's own trigger captures.
    assert any(sources and "tasks" in sources and open_ for sources, open_ in boundary.seen)
    # Capture precedes finalization: the post-commit publication still names the captured tables, once.
    published = [item for item in feed.published if "tasks" in item[1]]
    assert len(published) == 1 and published[0][0] == "tasks_create"
    assert published[0][2][1] == after["tasks"]  # an independent connection already saw the committed row


def test_a_failing_inbox_finalizer_rolls_back_the_record_and_publishes_nothing(host):
    instance, business, agent, boundary, feed = host
    before, key = counts(instance), new_id()
    boundary.fail_on = "tasks"
    with pytest.raises(RuntimeError, match="injected finalizer failure"):
        assign(instance, business, agent, "Never lands", key=key)
    assert counts(instance) == before
    assert not any("tasks" in item[1] for item in feed.published)

    boundary.fail_on = None
    retried = assign(instance, business, agent, "Never lands", key=key)
    assert retried.status == "ok", retried.error
    after = counts(instance)
    assert (after["tasks"], after["inbox_events"], after["inbox_deliveries"]) == (
        before["tasks"] + 1, before["inbox_events"] + 1, before["inbox_deliveries"] + 1)
    assert sum("tasks" in item[1] for item in feed.published) == 1


def test_file_result_commits_document_history_and_publication_exactly_once(host):
    instance, business, agent, boundary, feed = host
    before = counts(instance)
    result = write_note(instance, business, agent)
    assert result.status == "ok", result.error
    document = result.data["document"]
    # A global agent's notes live in its global workspace: operations/agents/{agent}/files/notes/...
    location = instance.paths.operations / document["path"]
    assert location.parent.parent.parent == instance.paths.agent_workspace(agent)
    text = location.read_text()
    assert location.is_file() and text.startswith("---\ntitle: Audit note\n") and text.endswith("First audit body\n")

    after = counts(instance)
    assert after["documents"] == before["documents"] + 1
    assert after["qbdv_document_versions"] == before["qbdv_document_versions"] + 1
    assert after["inbox_changes"] == 0
    with instance.database.read() as uow:
        row = uow.connection.execute("SELECT availability FROM documents WHERE id=?", (document["id"],)).fetchone()
        assert row[0] == "ready"  # DocumentLifecycle.applied
        version = uow.connection.execute("SELECT content, content_sha256 FROM qbdv_document_versions "
                                         "WHERE document_id=?", (document["id"],)).fetchone()
        states = [r[0] for r in uow.connection.execute("SELECT state FROM apply_receipts")]
    assert version[0] == text  # DocumentReceiptHistory captured the verified final bytes
    assert version[1] == hashlib.sha256(location.read_bytes()).hexdigest()
    assert states.count("applied") == after["apply_receipts"]
    with_documents = [item for item in feed.published if "documents" in item[1]]
    assert len(with_documents) == 1 and with_documents[0][2][0] == 1


def test_a_failure_in_the_document_unit_leaves_no_partial_state_and_the_retry_converges(host):
    instance, business, agent, boundary, feed = host
    before, key = counts(instance), new_id()
    boundary.fail_on = "documents"
    failed = write_note(instance, business, agent, key=key)
    assert failed.status == "error" and failed.error["code"] == "operation_failed" and failed.error["retryable"]
    assert any(sources == ("documents",) and open_ for sources, open_ in boundary.seen)  # failed inside the unit

    during = counts(instance)
    # The document row, its shared-history version and every inbox capture rolled back together.
    assert during["documents"] == before["documents"]
    assert during["qbdv_document_versions"] == before["qbdv_document_versions"]
    assert during["inbox_changes"] == 0 and during["inbox_events"] == before["inbox_events"]
    with instance.database.read() as uow:
        assert [r[0] for r in uow.connection.execute("SELECT state FROM apply_receipts ORDER BY rowid")][-1] == "failed"
    assert not any("documents" in item[1] for item in feed.published)

    boundary.fail_on = None
    retried = write_note(instance, business, agent, key=key)
    assert retried.status == "ok", retried.error
    after = counts(instance)
    assert after["documents"] == before["documents"] + 1
    assert after["qbdv_document_versions"] == before["qbdv_document_versions"] + 1
    assert after["apply_receipts"] == before["apply_receipts"] + 1  # the same receipt resumed, none duplicated
    assert after["inbox_changes"] == 0
    with instance.database.read() as uow:
        assert [r[0] for r in uow.connection.execute("SELECT state FROM apply_receipts ORDER BY rowid")][-1] == "applied"
    assert sum("documents" in item[1] for item in feed.published) == 1

    notes = tree(instance.paths.agent_workspace(agent) / "files")
    assert len(notes) == 2 and notes[0] == "notes" and notes[1].startswith("notes/") and notes[1].endswith("-audit-note.md")
    assert not [name for name in notes if ".bazarlyk-" in name or name.endswith(".tmp")]


def test_a_publication_or_capture_failure_never_undoes_a_committed_write(host, monkeypatch):
    instance, business, agent, boundary, feed = host
    before = counts(instance)
    feed.break_publication = True
    committed = assign(instance, business, agent, "Committed anyway")
    assert committed.status == "ok" and counts(instance)["tasks"] == before["tasks"] + 1
    assert feed.publish_failures == 1

    feed.break_publication = False
    monkeypatch.setattr(ChangeFeed, "_capture", staticmethod(lambda connection: 1 / 0))
    second = assign(instance, business, agent, "Capture failed quietly")
    assert second.status == "ok" and counts(instance)["tasks"] == before["tasks"] + 2
    assert counts(instance)["inbox_changes"] == 0  # finalization still ran
