"""Focused episode pagination, navigation and delivery regressions (synthetic only)."""
import base64
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.overseer.evidence import (CaptureJournal, EPISODE_CURSOR_VERSION, EvidenceProvider,
                                        _episode)
from bazarlyk.overseer.provenance import source_ref


GLOBAL = {"kind": "global", "business_id": None}
BUSINESS = {"kind": "business", "business_id": "business-a"}
SELECTION = {"mode": "explicit", "namespaces": [BUSINESS]}


class Access:
    def __init__(self):
        self.denied_ids = set()
        self.denied_receipts = set()
        self.checked = []
        self.retained = []

    def resolve_selection(self, selection):
        return {**selection, "namespaces": selection.get("namespaces", [GLOBAL])}

    def authorize_source(self, ref):
        identity = ref.get("record_id")
        self.checked.append((identity, ref.get("kind")))
        if (identity in self.denied_ids or ref.get("kind") == "transcript_entry"
                and ref.get("locator", {}).get("receipt_id") in self.denied_receipts):
            raise DomainError("forbidden", "synthetic admission denial")

    def authorize_retention(self, run_ref, *, action):
        self.retained.append((run_ref, action))


class Coverage:
    def __init__(self):
        self.calls = 0

    def completed_sources(self, refs):
        self.calls += 1
        return []


def member(journal, namespace, kind, identity, content, *, relations=(), gaps=(),
           producer_id=None, overseer_origin=False, source_kind=None, locator=None):
    locator = locator or {}
    base_kind = {"task_message": "task", "action_revision": "action", "action_message": "action",
                 "signoff_note": "signoff", "signoff_response": "signoff", "execution_outcome": "signoff",
                 "document_revision": "document"}.get(source_kind or kind, source_kind or kind)
    locators = {
        "task": {"task_id": identity}, "task_message": {"task_id": locator.get("task_id", identity),
            "message_id": identity},
        "action": {"action_id": identity}, "action_revision": {"action_id": locator.get("action_id", identity), "revision": 1},
        "signoff": {"signoff_id": identity}, "signoff_note": {"signoff_id": locator.get("signoff_id", identity), "note_id": identity},
        "signoff_response": {"signoff_id": identity}, "execution_outcome": {"signoff_id": identity},
        "document": {"document_id": identity}, "document_revision": {"document_id": locator.get("document_id", identity), "version": 1},
    }
    ref = source_ref(provider="operations", kind=source_kind or kind, record_id=identity, revision="1",
        content=content, episode_id=_episode(namespace, base_kind, locator.get("episode_identity", identity) if locator else identity),
        actor_id=None, actor_kind=None, occurred_at=None, locator=locator or locators[source_kind or kind],
        scope={"description": "synthetic"}, namespace=namespace, producer_id=producer_id,
        overseer_origin=overseer_origin)
    return journal.observe("selection", str(identity), ref, content, relations=relations, gaps=gaps)


def transcript_entry(journal, namespace, agent, provider, conversation, identity, text, receipt):
    lineage = [agent, provider, conversation]
    episode_id = _episode(namespace, "transcript", lineage)
    content = {"speaker": "assistant", "text": text}
    ref = source_ref(provider="operations", kind="transcript_entry", record_id=identity,
        revision=receipt, content=content, episode_id=episode_id, actor_id=agent,
        actor_kind="agent", occurred_at=None, locator={"agent_id": agent, "provider": provider,
            "conversation_id": conversation, "archive_id": "archive", "receipt_id": receipt,
            "position": 0, "length": len(text), "identity_kind": "native_id", "identity_key": identity},
        scope={"description": "synthetic transcript"}, namespace=namespace, producer_id=agent)
    return journal.observe("selection", identity, ref, content)


def journal_at(tmp_path, access=None, coverage=None, *, overseer_ids=("overseer",)):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = access or Access()
    journal = CaptureJournal(root, access, coverage, overseer_ids=set(overseer_ids))
    return root, access, journal


def frozen(journal, namespace=BUSINESS, *, gaps=()):
    return journal.frontier({"mode": "explicit", "namespaces": [namespace]}, "selection", gaps=gaps)


def test_transcript_conversation_admission_keeps_entry_revocation_separate(tmp_path):
    _, access, journal = journal_at(tmp_path)
    entries = [transcript_entry(journal, GLOBAL, "agent", "claude", "conversation",
                                f"entry-{index}", f"transcript body {index}", "receipt-a")
               for index in range(3)]
    episode_ref = {"id": entries[0]["episode_id"]}
    frontier = frozen(journal, GLOBAL)

    # An inaccessible first entry does not become the conversation admission
    # anchor and suppress the other entries from this exact episode.
    access.denied_ids = {entries[0]["record_id"]}
    page = journal.episode(episode_ref, frontier)
    assert {ref["record_id"] for ref in page["source_refs"]} == {"entry-1", "entry-2"}
    assert page["complete"] is False and page["scope"] == {"description": "synthetic transcript"}
    assert any(kind == "transcript" for _, kind in access.checked)

    # Continuations reauthorize each immutable entry against its captured
    # receipt while the conversation-level admission remains separate.
    access.denied_ids.clear()
    first = journal.episode(episode_ref, frontier, max_chars=11)
    assert first["next_cursor"] and first["records"][0]["offset"] == 0
    access.denied_receipts.add("receipt-a")
    resumed = journal.episode(episode_ref, frontier, cursor=first["next_cursor"], max_chars=160000)
    assert resumed["records"] == [] and resumed["complete"] is False
    assert resumed["next_cursor"] is None


def test_parent_siblings_stay_navigation_and_direct_incoming_attachments_are_read(tmp_path):
    root, access, journal = journal_at(tmp_path)
    task = "child-0"
    parent = "parent"
    member(journal, BUSINESS, "task", parent, {"title": "parent"})
    member(journal, BUSINESS, "task", task, {"title": "requested"},
           relations=[{"kind": "parent_task", "record_id": parent}])
    sibling_ids = [f"child-{n}" for n in range(1, 12)]
    for identity in sibling_ids:
        member(journal, BUSINESS, "task", identity, {"title": identity},
               relations=[{"kind": "parent_task", "record_id": parent}])
    task_episode = _episode(BUSINESS, "task", task)
    member(journal, BUSINESS, "signoff", "signoff-1", {"decision": "approved"},
           relations=[{"kind": "task", "record_id": task}])
    member(journal, BUSINESS, "action_revision", "action-1", {"result": "done"},
           source_kind="action_revision", locator={"action_id": "action-1", "revision": 1},
           relations=[{"kind": "task", "record_id": task, "business_id": "business-a"}])
    member(journal, BUSINESS, "document_revision", "document-1", {"content": "spec"},
           source_kind="document_revision", locator={"document_id": "document-1", "version": 1},
           relations=[{"kind": "task", "record_id": task}])
    frontier = frozen(journal)
    access.checked.clear()

    page = journal.episode({"id": task_episode}, frontier)
    assert {ref["record_id"] for ref in page["source_refs"]} == {
        task, "signoff-1", "action-1", "document-1"}
    assert {link["record_id"] for link in page["related_episodes"]} == {parent}
    parent_link = page["related_episodes"][0]
    assert parent_link["episode_ref"]["id"] == _episode(BUSINESS, "task", parent)
    assert parent_link["captured"] is True
    assert not (set(sibling_ids) & {identity for identity, _ in access.checked})
    assert not (set(sibling_ids) & {ref["record_id"] for ref in page["source_refs"]})

    parent_page = journal.episode(parent_link["episode_ref"], frontier)
    assert {ref["record_id"] for ref in parent_page["source_refs"]} == {parent}
    assert not (set(sibling_ids) & {ref["record_id"] for ref in parent_page["source_refs"]})


def test_namespace_collision_and_explicit_global_null_relation(tmp_path):
    _, _, journal = journal_at(tmp_path)
    root_id = "same-task"
    member(journal, BUSINESS, "task", root_id, {"title": "business task"}, relations=[
        {"kind": "action", "record_id": "same-action", "business_id": None},
        {"kind": "parent_task", "record_id": "same-parent"},
    ])
    member(journal, BUSINESS, "task", "same-parent", {"title": "business parent"})
    member(journal, GLOBAL, "task", "same-parent", {"title": "global parent"})
    member(journal, GLOBAL, "action_revision", "same-action", {"result": "global"},
           source_kind="action_revision", locator={"action_id": "same-action", "revision": 1},
           relations=[{"kind": "task", "record_id": root_id, "business_id": "business-a"}])
    member(journal, BUSINESS, "action_revision", "same-action", {"result": "business"},
           source_kind="action_revision", locator={"action_id": "same-action", "revision": 1},
           relations=[{"kind": "task", "record_id": "unrelated"}])
    frontier = journal.frontier({"mode": "explicit", "namespaces": [BUSINESS, GLOBAL]}, "selection")
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frontier)
    assert {(ref["kind"], json.dumps(ref["namespace"], sort_keys=True), ref["record_id"])
            for ref in page["source_refs"]} == {
        ("task", json.dumps(BUSINESS, sort_keys=True), root_id),
        ("action_revision", json.dumps(GLOBAL, sort_keys=True), "same-action")}
    parent = next(link for link in page["related_episodes"] if link["record_id"] == "same-parent")
    assert parent["namespace"] == BUSINESS and parent["captured"] is True
    assert parent["episode_ref"]["id"] == _episode(BUSINESS, "task", "same-parent")


def test_denied_root_attachment_and_optional_target_do_not_leak_link_identity(tmp_path):
    _, access, journal = journal_at(tmp_path)
    root_id = "root"
    member(journal, BUSINESS, "task", root_id, {"title": "root"})
    member(journal, BUSINESS, "signoff", "private-bridge", {"text": "private"},
           producer_id="owner", relations=[{"kind": "task", "record_id": root_id},
                                            {"kind": "document", "record_id": "private-document"}])
    frontier = frozen(journal)
    access.denied_ids = {"private-bridge", "private-document"}
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frontier)
    assert {ref["record_id"] for ref in page["source_refs"]} == {root_id}
    assert page["related_episodes"] == []
    assert "private-document" not in json.dumps(page)
    assert "private-bridge" not in json.dumps(page)

    access.denied_ids = {root_id}
    refused = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frontier)
    assert refused["availability"] == "forbidden"
    assert refused["records"] == [] and refused["related_episodes"] == []
    assert "private-bridge" not in json.dumps(refused)
    assert refused["scope"] is None and "synthetic" not in json.dumps(refused)


def test_uncaptured_authorized_target_is_a_link_and_episode_relations_are_not_an_alternate_leak(tmp_path):
    _, _, journal = journal_at(tmp_path)
    root_id = "root"
    member(journal, BUSINESS, "task", root_id, {"title": "root"}, relations=[
        {"kind": "parent_task", "record_id": "not-captured"},
    ])
    frontier = frozen(journal)
    supplied = {**frontier, "untrusted_padding": "x" * 100000}
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, supplied)
    assert "untrusted_padding" not in page["frontier"]
    assert page["relations"] == []
    assert page["related_episodes"] == [{
        "episode_ref": {"id": _episode(BUSINESS, "task", "not-captured")},
        "kind": "task", "record_id": "not-captured", "namespace": BUSINESS,
        "relation": "parent_task", "captured": False,
    }]


def test_denied_attachment_cannot_bridge_to_its_other_neighbors(tmp_path):
    _, access, journal = journal_at(tmp_path)
    root_id = "root"
    member(journal, BUSINESS, "task", root_id, {"title": "root"})
    member(journal, BUSINESS, "signoff", "bridge", {"decision": "private"},
           relations=[{"kind": "task", "record_id": root_id},
                      {"kind": "document", "record_id": "hidden-document"}])
    access.denied_ids = {"bridge"}
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frozen(journal))
    assert {ref["record_id"] for ref in page["source_refs"]} == {root_id}
    assert page["related_episodes"] == []
    assert "hidden-document" not in json.dumps(page)


def test_excluded_relation_proof_cannot_select_attachment_but_valid_alternate_can(tmp_path):
    _, access, journal = journal_at(tmp_path)
    root_id, document_id = "proof-root", "proof-document"
    root_episode = _episode(BUSINESS, "task", root_id)
    member(journal, BUSINESS, "task", root_id, {"title": "root"})
    member(journal, BUSINESS, "task_message", "excluded-note", {"body": "private"},
        source_kind="task_message", locator={"task_id": root_id, "message_id": "excluded-note",
            "episode_identity": root_id}, producer_id="overseer", overseer_origin=True,
        relations=[{"kind": "document", "record_id": document_id}])
    member(journal, BUSINESS, "document_revision", document_id, {"body": "attached"},
        source_kind="document_revision", locator={"document_id": document_id, "version": 1},
        relations=[{"kind": "document", "record_id": "private-downstream"}])
    member(journal, BUSINESS, "task", "incoming-task", {"title": "unrelated"},
        relations=[{"kind": "document", "record_id": document_id}])
    frontier = frozen(journal)
    page = journal.episode({"id": root_episode}, frontier)
    assert {ref["record_id"] for ref in page["source_refs"]} == {root_id}
    assert page["related_episodes"] == []
    assert "proof-document" not in json.dumps(page) and "private-downstream" not in json.dumps(page)
    assert "incoming-task" not in json.dumps(page)

    # A second currently admitted direct proof is sufficient even when the
    # first frozen relation source remains excluded.
    member(journal, BUSINESS, "task_message", "allowed-note", {"body": "public"},
        source_kind="task_message", locator={"task_id": root_id, "message_id": "allowed-note",
            "episode_identity": root_id}, relations=[{"kind": "document", "record_id": document_id}])
    frontier = frozen(journal)
    access.denied_ids = {document_id}
    denied_attachment = journal.episode({"id": root_episode}, frontier)
    assert {ref["record_id"] for ref in denied_attachment["source_refs"]} == {root_id, "allowed-note"}
    assert denied_attachment["related_episodes"] == []

    access.denied_ids.clear()
    page = journal.episode({"id": root_episode}, frontier)
    assert {ref["record_id"] for ref in page["source_refs"]} == {root_id, "allowed-note", document_id}
    assert {link["record_id"] for link in page["related_episodes"]} >= {"incoming-task"}


def test_incoming_link_keeps_valid_alternate_attachment_endpoint_after_budget_yield(tmp_path, monkeypatch):
    _, access, journal = journal_at(tmp_path)
    root_id, denied_id, allowed_id = "root", "a-denied-attachment", "b-allowed-attachment"
    attachment_ids = [denied_id, allowed_id, *(f"filler-{index:02d}" for index in range(14))]
    relations = [{"kind": "document", "record_id": identity} for identity in attachment_ids]
    member(journal, GLOBAL, "task", root_id, {"body": "root"}, relations=relations)
    for identity in attachment_ids:
        member(journal, GLOBAL, "document_revision", identity, {"body": identity},
               source_kind="document_revision", locator={"document_id": identity, "version": 1})
    member(journal, GLOBAL, "task", "related-task", {"title": "related"}, relations=[
        {"kind": "document", "record_id": denied_id},
        {"kind": "document", "record_id": allowed_id},
    ])
    frontier = frozen(journal, GLOBAL)
    access.denied_ids = {denied_id}

    # Let the page process the denied endpoint, then expire cooperatively just
    # before it considers the distinct admitted endpoint proof.
    calls, expiry_call = [0], [39]
    def fake_monotonic():
        calls[0] += 1
        return 2.0 if calls[0] >= expiry_call[0] else 0.0
    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    first = journal.episode({"id": _episode(GLOBAL, "task", root_id)}, frontier)
    assert first["next_cursor"] and first["related_episodes"] == []
    cursor = json.loads(base64.urlsafe_b64decode(
        first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)))
    assert cursor["phase"] == "links" and cursor["position"] == 0 and cursor["proof"] == 1

    calls[0], expiry_call[0] = 0, 10**9
    second = journal.episode({"id": _episode(GLOBAL, "task", root_id)}, frontier,
                             cursor=first["next_cursor"])
    assert {link["record_id"] for link in second["related_episodes"]} == {"related-task"}
    assert denied_id not in json.dumps(second)


def test_attachment_relation_proof_is_rechecked_after_continuation_before_navigation(tmp_path):
    _, access, journal = journal_at(tmp_path)
    root_id, note_id, document_id = "proof-root", "proof-note", "proof-document"
    root_episode = _episode(BUSINESS, "task", root_id)
    root_content, note_content = {"body": "root"}, {"body": "proof"}
    member(journal, BUSINESS, "task", root_id, root_content)
    member(journal, BUSINESS, "task_message", note_id, note_content,
        source_kind="task_message", locator={"task_id": root_id, "message_id": note_id,
            "episode_identity": root_id}, relations=[{"kind": "document", "record_id": document_id}])
    member(journal, BUSINESS, "document_revision", document_id, {"body": "D" * 400},
        source_kind="document_revision", locator={"document_id": document_id, "version": 1},
        relations=[{"kind": "document", "record_id": "private-downstream"}])
    frontier = frozen(journal)
    root_size = len(json.dumps(root_content, sort_keys=True, separators=(",", ":")))
    note_size = len(json.dumps(note_content, sort_keys=True, separators=(",", ":")))
    first = journal.episode({"id": root_episode}, frontier, max_chars=root_size + note_size + 20)
    assert first["next_cursor"]
    cursor = json.loads(base64.urlsafe_b64decode(first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4)))
    assert cursor["phase"] == "attachments" and cursor["offset"] == 20
    access.denied_ids = {note_id}
    resumed = journal.episode({"id": root_episode}, frontier, cursor=first["next_cursor"])
    assert not any(ref["record_id"] == document_id for ref in resumed["source_refs"])
    assert resumed["related_episodes"] == []
    assert "private-downstream" not in json.dumps(resumed)


def test_many_denied_link_targets_consume_the_page_candidate_budget(tmp_path):
    _, access, journal = journal_at(tmp_path)
    root_id = "root"
    target_ids = {f"private-{n:02d}" for n in range(30)}
    member(journal, BUSINESS, "task", root_id, {"title": "root"}, relations=[
        {"kind": "task", "record_id": identity} for identity in sorted(target_ids)])
    access.denied_ids = target_ids
    access.checked.clear()
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frozen(journal))
    assert page["next_cursor"]
    assert json.loads(base64.urlsafe_b64decode(page["next_cursor"] + "=" * (-len(page["next_cursor"]) % 4)))["phase"] == "links"
    assert len(set(identity for identity, _ in access.checked)) <= 20
    assert page["related_episodes"] == []
    assert all("private-" not in str(gap) for gap in page["gaps"])


def test_overseer_origin_attachment_is_not_returned_or_linked(tmp_path):
    _, _, journal = journal_at(tmp_path, overseer_ids=("overseer",))
    root_id = "root"
    member(journal, BUSINESS, "task", root_id, {"title": "root"})
    member(journal, BUSINESS, "signoff", "overseer-signoff", {"decision": "review"},
           producer_id="overseer", relations=[{"kind": "task", "record_id": root_id},
                                               {"kind": "document", "record_id": "hidden"}])
    page = journal.episode({"id": _episode(BUSINESS, "task", root_id)}, frozen(journal))
    assert {ref["record_id"] for ref in page["source_refs"]} == {root_id}
    assert page["related_episodes"] == []
    assert "overseer-signoff" not in json.dumps(page) and "hidden" not in json.dumps(page)


def test_page_cursor_replay_journal_recreation_reset_validation_and_revocation(tmp_path):
    root, access, journal = journal_at(tmp_path, coverage=Coverage())
    identity = "large-root"
    content = {"body": "x" * 250}
    saved = member(journal, GLOBAL, "task", identity, content)
    selection = {"mode": "explicit", "namespaces": [GLOBAL]}
    frontier = journal.frontier(selection, "selection")
    episode_ref = {"id": _episode(GLOBAL, "task", identity)}
    first = journal.episode(episode_ref, frontier, max_chars=9)
    assert first["next_cursor"] and first["records"][0]["offset"] == 0
    second = journal.episode(episode_ref, frontier, cursor=first["next_cursor"], max_chars=9)
    replay = journal.episode(episode_ref, frontier, cursor=first["next_cursor"], max_chars=9)
    assert replay["records"] == second["records"] and replay["next_cursor"] == second["next_cursor"]

    restarted = CaptureJournal(root, access, journal.coverage, overseer_ids={"overseer"})
    resumed = restarted.episode(episode_ref, frontier, cursor=second["next_cursor"], max_chars=9)
    assert resumed["records"][0]["offset"] == 18
    assert journal.coverage.calls == 0

    legacy = base64.urlsafe_b64encode(json.dumps({"snapshot": frontier["snapshot_id"],
        "episode": episode_ref["id"], "sequence": 1, "offset": 9}).encode()).decode().rstrip("=")
    reset = journal.episode(episode_ref, frontier, cursor=legacy, max_chars=9)
    assert reset["cursor_reset"] is True and reset["records"][0]["offset"] == 0

    other_frontier = journal.frontier(selection, "selection")
    for wrong_ref, wrong_frontier, invalid in (
            ({"id": "different-root"}, frontier, second["next_cursor"]),
            (episode_ref, other_frontier, second["next_cursor"]),
            (episode_ref, frontier, "not-base64")):
        with pytest.raises(DomainError) as refused:
            journal.episode(wrong_ref, wrong_frontier, cursor=invalid, max_chars=9)
        assert refused.value.code == "validation_error"

    access.denied_ids = {identity}
    revoked = restarted.episode(episode_ref, frontier, cursor=first["next_cursor"], max_chars=9)
    assert revoked["availability"] == "forbidden" and revoked["records"] == []

    # A later revision is outside the already frozen member set.
    access.denied_ids.clear()
    newer = member(journal, GLOBAL, "task", identity, {"body": "new revision"})
    assert newer["revision"] == saved["revision"]  # same revision number, distinct content hash
    frozen_page = journal.episode(episode_ref, frontier, max_chars=160000)
    assert [ref["content_hash"] for ref in frozen_page["source_refs"]] == [saved["content_hash"]]


def test_cursor_rejects_boolean_version_and_terminal_nonzero_offset(tmp_path):
    _, _, journal = journal_at(tmp_path)
    identity = "cursor-root"
    member(journal, GLOBAL, "task", identity, {"body": "content"})
    frontier = frozen(journal, GLOBAL)
    episode_ref = {"id": _episode(GLOBAL, "task", identity)}
    base = {"scope": "focused_episode", "version": EPISODE_CURSOR_VERSION, "snapshot": frontier["snapshot_id"],
            "captured_at": frontier["captured_at"], "episode": episode_ref["id"],
            "phase": "root", "position": 1, "offset": 0, "gap_seen": False}
    for payload in (dict(base, version=True), dict(base, offset=1)):
        cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        with pytest.raises(DomainError) as refused:
            journal.episode(episode_ref, frontier, cursor=cursor)
        assert refused.value.code == "validation_error"


def test_root_admission_still_runs_when_metadata_work_uses_budget(tmp_path, monkeypatch):
    _, access, journal = journal_at(tmp_path)
    identity = "slow-root"
    member(journal, GLOBAL, "task", identity, {"body": "content"})
    frontier = frozen(journal, GLOBAL)
    access.checked.clear()
    clock = iter((0.0, 10.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 10.0))
    page = journal.episode({"id": _episode(GLOBAL, "task", identity)}, frontier)
    assert access.checked == [(identity, "task")]
    assert page["records"] == [] and page["next_cursor"]


def test_unrelated_frontier_diagnostics_do_not_poison_episode_but_source_gaps_remain(tmp_path):
    _, _, journal = journal_at(tmp_path)
    identity = "available"
    member(journal, GLOBAL, "task", identity, {"title": "available"})
    frontier = journal.frontier({"mode": "explicit", "namespaces": [GLOBAL]}, "selection",
                                gaps=[{"code": "unrelated_inventory_failure"}])
    page = journal.episode({"id": _episode(GLOBAL, "task", identity)}, frontier)
    assert page["complete"] is True and page["gaps"] == []
    assert page["frontier"]["gaps"] == [{"code": "unrelated_inventory_failure"}]

    member(journal, GLOBAL, "task", "gapped", {"title": "gapped"},
           gaps=[{"code": "task_thread_continuation_pending"}])
    frontier = journal.frontier({"mode": "explicit", "namespaces": [GLOBAL]}, "selection")
    page = journal.episode({"id": _episode(GLOBAL, "task", "gapped")}, frontier)
    assert page["complete"] is False
    assert {gap["code"] for gap in page["gaps"]} == {"task_thread_continuation_pending"}


def test_fake_clock_budget_bounds_candidates_and_resumes_fragments_and_links(tmp_path, monkeypatch):
    root, access, journal = journal_at(tmp_path)
    root_id = "clock-root"
    links = [{"kind": "task", "record_id": f"related-{n:02d}"} for n in range(25)]
    member(journal, GLOBAL, "task", root_id, {"body": "L" * 180}, relations=links)
    member(journal, GLOBAL, "signoff", "direct-signoff", {"body": "S" * 100},
           relations=[{"kind": "task", "record_id": root_id}])
    for number in range(28):
        member(journal, GLOBAL, "task_message", f"denied-{number:02d}", {"body": str(number)},
               source_kind="task_message", locator={"task_id": root_id, "message_id": f"denied-{number:02d}",
                   "episode_identity": root_id})
    # Deny the many related thread candidates after their frozen metadata exists.
    access.denied_ids = {f"denied-{number:02d}" for number in range(28)}
    frontier = frozen(journal, GLOBAL)
    access.checked.clear()
    clock = [0.0]
    def fake_monotonic():
        value = clock[0]
        clock[0] += 0.32
        return value
    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    expected = {root_id: json.dumps({"body": "L" * 180}, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                "direct-signoff": json.dumps({"body": "S" * 100}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)}
    offsets, fragments, links_seen, phases, cursor = {}, {key: [] for key in expected}, set(), set(), None
    pages = 0
    while True:
        access.checked.clear()
        reads = []
        original = journal.get_source
        def observed(ref):
            reads.append(ref["record_id"])
            return original(ref)
        journal.get_source = observed
        page = journal.episode({"id": _episode(GLOBAL, "task", root_id)}, frontier,
                               cursor=cursor, max_chars=17)
        journal.get_source = original
        pages += 1
        assert len(access.checked) <= 21
        assert len(set(identity for identity, _ in access.checked)) <= 20
        assert len(reads) <= 20
        for record in page["records"]:
            identity = record["source_ref"]["record_id"]
            assert identity in expected
            assert record["offset"] == offsets.get(identity, 0)
            offsets[identity] = offsets.get(identity, 0) + len(record["content_fragment"])
            fragments[identity].append(record["content_fragment"])
        links_seen.update(link["record_id"] for link in page["related_episodes"])
        cursor = page["next_cursor"]
        if cursor:
            phases.add(json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))["phase"])
        if cursor is None:
            assert not page["complete"]  # denied sources remain visible evidence gaps
            break
    assert {identity: "".join(parts) for identity, parts in fragments.items()} == expected
    assert offsets == {identity: len(text) for identity, text in expected.items()}
    assert links_seen == {f"related-{n:02d}" for n in range(25)}
    assert phases == {"root", "attachments", "links"}
    assert pages > 2


def test_complete_synthetic_host_delivery_bounds_32_episode_284_record_tree(tmp_path, monkeypatch):
    import bazarlyk.native_chat.store as native_store
    import bazarlyk.overseer.operations as operations_module
    from bazarlyk.overseer.operations import OverseerOperations

    class FakeConnection:
        def __init__(self, records):
            self.records = records
        def execute(self, _sql, params):
            identity = params[-1]
            value = (None,) if identity in self.records else None
            return SimpleNamespace(fetchone=lambda: value)

    class FakeDatabase:
        def __init__(self, identities):
            self.connection = FakeConnection(set(identities))
        @contextmanager
        def read(self):
            yield SimpleNamespace(connection=self.connection)

    class Workflows:
        def get(self, run_id):
            assert run_id == "run"
            return {"run": {"workflow_ref": "overseer/extract", "state": "running"}}

    class Layer:
        workflows = Workflows()

    class TestViews:
        def __init__(self, _layer):
            pass
        def get_admitted_run(self, run_id):
            assert run_id == "run"
            return {"kind": "extraction", "invocation": {"conversation_id": "conversation",
                "executor": {"actor_id": "agent", "actor_kind": "agent"}}}

    access = Access()
    archive_root = tmp_path / "archive"
    archive_root.mkdir(mode=0o700)
    ctx = SimpleNamespace(principal_id="agent")
    identities = {"parent"}
    identities.update(f"child-{n}" for n in range(31))
    runtime = SimpleNamespace(database=FakeDatabase(identities), agentic_layer=Layer(),
        paths=SimpleNamespace(overseer_archive=archive_root))
    provider = EvidenceProvider(runtime, ctx, archive_root, access, overseer_ids={"overseer"}, coverage=Coverage())
    member(provider.journal, BUSINESS, "task", "parent", {"title": "parent"})
    child_ids = [f"child-{n}" for n in range(31)]
    for identity in child_ids:
        member(provider.journal, BUSINESS, "task", identity, {"title": identity},
               relations=[{"kind": "parent_task", "record_id": "parent"}])
    note_counts = {identity: (2 if identity == "child-0" else 9 if 1 <= int(identity.split("-")[1]) <= 10 else 8)
                   for identity in child_ids}
    for identity, count in note_counts.items():
        for index in range(count):
            note_id = f"{identity}-note-{index}"
            member(provider.journal, BUSINESS, "task_message", note_id, {"body": note_id},
                   source_kind="task_message", locator={"task_id": identity, "message_id": note_id,
                       "episode_identity": identity})
    assert sum(note_counts.values()) + len(child_ids) + 1 == 284
    frontier = frozen(provider.journal)

    access.checked.clear()
    reads = []
    original_read = provider.journal.get_source
    def observed_read(ref):
        reads.append(ref["record_id"])
        return original_read(ref)
    provider.journal.get_source = observed_read
    monkeypatch.setattr(operations_module, "current_overseer", lambda *args, **kwargs: (ctx, None))
    monkeypatch.setattr(operations_module, "Views", TestViews)
    monkeypatch.setattr(native_store, "latest_session", lambda _conn, _agent: {"conversation_id": "conversation"})
    operations = OverseerOperations(runtime)
    monkeypatch.setattr(operations, "_provider", lambda _ctx, _uow: provider)
    uow = SimpleNamespace(connection=object())
    started = time.perf_counter()
    page = operations._evidence_delivery("overseer_episode_get", ctx,
        {"run_id": "run", "episode_ref": {"id": _episode(BUSINESS, "task", "child-0")},
         "frontier": frontier, "max_chars": 160000}, uow)
    elapsed = time.perf_counter() - started
    assert page["complete"] is True and page["next_cursor"] is None
    assert len(page["source_refs"]) == 3
    assert set(reads) == {"child-0", "child-0-note-0", "child-0-note-1"}
    assert not (set(child_ids[1:]) & set(reads))
    assert {identity for identity, _ in access.checked} <= {"child-0", "child-0-note-0", "child-0-note-1", "parent"}
    saved = provider.journal.archive.get_retention(page["presentation_ref"])
    assert saved["records"][0]["result"] == {key: value for key, value in page.items() if key != "presentation_ref"}
    assert saved["source_refs"] == page["source_refs"]
    assert provider.journal.coverage.calls == 0
    print(f"synthetic_complete_delivery_seconds={elapsed:.6f} source_reads={len(reads)} record_frontier=284 episode_count=32")
