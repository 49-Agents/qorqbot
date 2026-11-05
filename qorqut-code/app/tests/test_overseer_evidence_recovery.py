"""Synthetic data only: capture/analysis recovery does not use a product session."""
import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.overseer.evidence import CaptureJournal
from bazarlyk.overseer.provenance import source_ref


class Access:
    denied = False
    def resolve_selection(self, selection):
        return selection
    def authorize_source(self, ref):
        if self.denied:
            raise DomainError("forbidden", "revoked")


class Coverage:
    def __init__(self):
        self.completed = []
        self.unavailable = False
    def completed_sources(self, refs):
        if self.unavailable:
            raise RuntimeError("offline")
        return self.completed


def ref(revision, text):
    return source_ref(provider="operations", kind="task", record_id="task", revision=revision, content=text,
        episode_id="episode", actor_id=None, actor_kind=None, occurred_at=None, locator={}, scope={},
        namespace={"kind": "global", "business_id": None})


def test_episode_does_not_authorize_unrelated_frontier_records(tmp_path):
    from bazarlyk.overseer.evidence import _episode
    class ObservedAccess(Access):
        def __init__(self):
            self.checked = []
        def authorize_source(self, source):
            self.checked.append(source['record_id'])
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    access = ObservedAccess()
    journal = CaptureJournal(root, access, overseer_ids={'overseer'})
    namespace = {'kind': 'global', 'business_id': None}
    for number in range(30):
        identity = 'task-' + str(number)
        source = {**ref(1, identity), 'record_id': identity, 'episode_id': _episode(namespace, 'task', identity)}
        journal.observe('selection', identity, source, identity)
    frontier = journal.frontier({'mode': 'explicit', 'namespaces': [namespace]}, 'selection')
    access.checked.clear()
    page = journal.episode({'id': _episode(namespace, 'task', 'task-0')}, frontier)
    assert [s['record_id'] for s in page['source_refs']] == ['task-0']
    assert set(access.checked) == {'task-0'}


def test_episode_replaces_transitive_task_bridge_with_optional_single_hop_links(tmp_path):
    from bazarlyk.overseer.evidence import _episode
    class SelectiveAccess(Access):
        denied_ids = set()
        def authorize_source(self, source):
            if source['record_id'] in self.denied_ids:
                raise DomainError('forbidden', 'revoked')
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    access = SelectiveAccess()
    journal = CaptureJournal(root, access, overseer_ids={'overseer'})
    namespace = {'kind': 'global', 'business_id': None}
    # The requested task has an incoming edge from bridge, which itself links
    # to last. Task relations are links, never evidence expansion.
    for identity in ('first', 'bridge', 'last'):
        source = {**ref(1, identity), 'record_id': identity, 'episode_id': _episode(namespace, 'task', identity)}
        relations = [{'kind': 'task', 'record_id': target} for target in ('first', 'last')] if identity == 'bridge' else []
        journal.observe('selection', identity, source, identity, relations=relations)
    frontier = journal.frontier({'mode': 'explicit', 'namespaces': [namespace]}, 'selection')
    requested = {'id': _episode(namespace, 'task', 'first')}
    page = journal.episode(requested, frontier)
    assert {s['record_id'] for s in page['source_refs']} == {'first'}
    assert {link['record_id'] for link in page['related_episodes']} == {'bridge'}
    assert all(s['record_id'] != 'last' for s in page['source_refs'])
    access.denied_ids = {'bridge'}
    page = journal.episode(requested, frontier)
    assert {s['record_id'] for s in page['source_refs']} == {'first'}
    assert page['related_episodes'] == []
    assert 'bridge' not in str(page) and 'last' not in str(page)
    access.denied_ids = {'first'}
    refused = journal.episode(requested, frontier)
    assert refused['availability'] == 'forbidden' and refused['records'] == []
    assert refused['related_episodes'] == []


def test_lost_response_restart_edits_and_exact_coverage(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access, coverage = Access(), Coverage()
    journal = CaptureJournal(root, access, coverage, overseer_ids={"overseer"})
    before = journal.observe("selection", "task", ref(1, "before"), "before")
    # A lost response or failure before coverage does not acknowledge the capture.
    journal = CaptureJournal(root, access, coverage, overseer_ids={"overseer"})
    assert journal.pending("selection")["items"] == [before]
    after = journal.observe("selection", "task", ref(2, "after"), "after")
    journal = CaptureJournal(root, access, coverage, overseer_ids={"overseer"})
    assert journal.pending("selection")["items"] == [before, after]
    assert journal.get_source(before) == "before"
    assert journal.get_source(after) == "after"
    coverage.completed = [before]
    assert journal.pending("selection")["items"] == [after]
    coverage.completed = [{**after, "namespace": {"kind": "business", "business_id": "other"}}]
    assert journal.pending("selection")["items"] == [before, after]
    coverage.unavailable = True
    result = journal.pending("selection")
    assert result["items"] == [before, after] and result["gaps"] == [{"code": "coverage_unavailable"}]
    access.denied = True
    result = journal.pending("selection")
    assert not result["items"] and all(gap["code"] == "forbidden" for gap in result["gaps"])
    with pytest.raises(DomainError) as refused:
        journal.get_source(before)
    assert refused.value.code == "forbidden"
    access.denied = False
    assert journal.pending("other-selection")["items"] == []


def test_frozen_frontier_pages_and_rechecks_each_captured_source(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    selection = {"mode": "explicit", "namespaces": [{"kind": "global", "business_id": None}]}
    journal.observe("selection", "task", ref(1, "before"), "before")
    frontier = journal.frontier(selection, "selection")
    journal.observe("selection", "task", ref(2, "after"), "after")
    fragments, cursor = [], None
    while True:
        page = journal.episode({"id": "episode"}, frontier, cursor=cursor, max_chars=3)
        fragments.extend(record["content_fragment"] for record in page["records"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert "".join(fragments) == '"before"'
    access.denied = True
    refused = journal.episode({"id": "episode"}, frontier)
    assert refused["records"] == [] and refused["gaps"][0]["code"] == "forbidden"


def test_overseer_and_linked_mirrors_never_reach_extraction(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    original = {**ref(1, "private"), "record_id": "overseer-note", "provenance":
        {"producer_id": "overseer", "overseer_origin": True, "original_refs": []}}
    original = journal.observe("selection", "original", original, "private")
    mirror = {**ref(1, "private"), "record_id": "mirror", "provenance":
        {"producer_id": "someone-else", "overseer_origin": False, "original_refs": [original]}}
    mirror = journal.observe("selection", "mirror", mirror, "private")
    assert journal.pending("selection")["items"] == []
    with pytest.raises(DomainError) as refused:
        journal.get_source(mirror)
    assert refused.value.code == "forbidden"
