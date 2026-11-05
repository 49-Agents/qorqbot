"""Observe-only change feed: bazarlyk/service/changes.py, wired into
Database._unit (capture/publish) and bazarlyk/transport/http.py's
GET /api/root/events (HTTPAPI._events_stream).

Two layers: the pure `ChangeFeed`/`_events_stream` mechanics driven directly
(no HTTP, no TestClient -- a real request through TestClient never reaches
disconnection for an intentionally infinite SSE generator, confirmed against
this codebase's own precedent in test_terminal_execution.py, which drives the
terminal mirror's identical `while not await request.is_disconnected():`
shape the same way), and a `Database`-integration layer proving a real
writable unit of work captures and publishes correctly.
"""
import asyncio
import json
import queue
from types import SimpleNamespace

import pytest

from bazarlyk.service.changes import (
    ANY_SCOPE, GLOBAL_ONLY_KINDS, ChangeFeed, EXTERNAL_WRITER_POLL_SECONDS, RING_SIZE, _coarse_kind_for_operation, _kinds_for_row)
from bazarlyk.transport.http import HTTPAPI
from foundation_support import BUSINESS, HUMAN, database_at
from test_terminal_execution import _collect, _next_async


class Request:
    def __init__(self, headers=None):
        self.headers = headers or {}

    async def is_disconnected(self):
        return True


class InstantEmptyQueue:
    """Stands in for a subscriber's real queue.Queue: never actually waits
    out the 1s timeout `_events_stream` passes to `.get()`, so a heartbeat
    test does not cost a real second of wall clock."""

    def get(self, block, timeout):
        raise queue.Empty()


def _api(changes):
    return SimpleNamespace(runtime=SimpleNamespace(changes=changes))


def _frame_payload(frame):
    return json.loads(frame.split(b"data: ", 1)[1])


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------

def test_coarse_kind_for_operation_matches_by_registered_prefix():
    assert _coarse_kind_for_operation("routines_runs") == "jobs_routines"
    assert _coarse_kind_for_operation("jobs_get") == "jobs_routines"
    assert _coarse_kind_for_operation("learnings_list") == "learnings"
    assert _coarse_kind_for_operation("tasks_create") is None
    assert _coarse_kind_for_operation(None) is None


def test_the_retired_setup_kind_is_no_longer_published():
    for operation in ("business_setup_update", "business_setup_submit", "cloudflare_zone_token_ensure",
                      "business_email_default_set", "environment_set", "environment_get"):
        assert _coarse_kind_for_operation(operation) is None, operation
    assert "business_setup" not in GLOBAL_ONLY_KINDS


def test_relationship_bumps_both_mapped_endpoint_kinds():
    kinds = _kinds_for_row("relationships", {"from_type": "task", "to_type": "person", "business_id": BUSINESS}, None)
    assert kinds == {"tasks", "documents"}


def test_relationship_with_one_unmapped_endpoint_bumps_only_the_mapped_side():
    kinds = _kinds_for_row("relationships",
        {"from_type": "task", "to_type": "unknown_thing", "business_id": BUSINESS}, None)
    assert kinds == {"tasks"}


# ---------------------------------------------------------------------------
# ChangeFeed.route_versions: PR3's ETag reads exactly these (kind, scope)
# pairs, so a pair missing here is a write a route could silently miss.
# ---------------------------------------------------------------------------

def test_route_versions_checks_only_the_routes_own_scope_for_a_plain_kind():
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)
    versions = changes.route_versions({"tasks"}, BUSINESS)
    assert versions == {("tasks", BUSINESS): changes.version("tasks", BUSINESS)}
    assert versions[("tasks", BUSINESS)] > 0


def test_route_versions_checks_only_global_for_a_global_only_kind_even_with_a_business_scope():
    changes = ChangeFeed()
    changes.publish_commit(captured=[], operation="routines_run", business_id=BUSINESS)
    versions = changes.route_versions({"jobs_routines"}, BUSINESS)
    assert list(versions) == [("jobs_routines", "global")]
    assert versions[("jobs_routines", "global")] > 0


def test_route_versions_checks_both_scopes_for_an_also_global_kind():
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "signoffs", "scope": BUSINESS, "kinds": {"signoffs"}}],
        operation="signoffs_input_create", business_id=BUSINESS)
    changes.publish_commit(captured=[], operation="signoffs_notification_tick", business_id=None)
    versions = changes.route_versions({"signoffs"}, BUSINESS)
    assert set(versions) == {("signoffs", BUSINESS), ("signoffs", "global")}
    assert versions[("signoffs", BUSINESS)] > 0 and versions[("signoffs", "global")] > 0


def test_route_versions_does_not_duplicate_global_for_an_also_global_kind_at_global_scope():
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "principals", "scope": "global", "kinds": {"agents"}}],
        operation="principals_update", business_id=None)
    versions = changes.route_versions({"agents"}, None)
    assert list(versions) == [("agents", "global")]


def test_route_versions_any_scope_finds_the_highest_version_across_every_known_business():
    other = "00000000-0000-4000-8000-000000000099"
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)
    changes.publish_commit(captured=[{"table": "tasks", "scope": other, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=other)
    versions = changes.route_versions({"tasks"}, ANY_SCOPE)
    assert versions == {("tasks", "any"): changes.version("tasks", other)}
    assert versions[("tasks", "any")] == max(changes.version("tasks", BUSINESS), changes.version("tasks", other))


def test_route_versions_any_scope_is_zero_when_a_kind_was_never_bumped():
    changes = ChangeFeed()
    assert changes.route_versions({"tasks"}, ANY_SCOPE) == {("tasks", "any"): 0}


# ---------------------------------------------------------------------------
# HTTPAPI._events_stream mechanics: subscribe is synchronous (real, on the
# runtime's one ChangeFeed); only the generator Starlette drains is async, so
# these drive it directly with a fake request, exactly like
# test_terminal_execution.py drives the terminal mirror's own SSE generator.
# ---------------------------------------------------------------------------

def test_fresh_connect_has_no_replay_and_streams_a_live_event():
    changes = ChangeFeed()
    request = Request()
    disconnects = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnects)
    response = HTTPAPI._events_stream(_api(changes), request)
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)
    frames = asyncio.run(_collect(response.body_iterator))
    assert len(frames) == 1
    assert not frames[0].startswith(b"event: reset")
    payload = _frame_payload(frames[0])
    assert payload == {"seq": 1, "tables": ["tasks"], "kinds": ["tasks"], "business_id": BUSINESS, "operation": "tasks_create"}
    # The generator's finally: block unsubscribes once the client disconnects.
    assert changes._subscribers == {}


def test_replay_delivers_missed_events_and_dedupes_a_racing_live_publish():
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)  # seq=1, before this client (re)connected
    request = Request({"last-event-id": "0"})
    disconnects = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnects)
    response = HTTPAPI._events_stream(_api(changes), request)
    # Lands after subscribe() (already done, synchronously, above) but before
    # the generator itself starts draining: present in both the replay's ring
    # read and the subscriber's live queue. Must be delivered exactly once.
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_update", business_id=BUSINESS)  # seq=2
    frames = asyncio.run(_collect(response.body_iterator))
    payloads = [_frame_payload(frame) for frame in frames]
    assert [payload["seq"] for payload in payloads] == [1, 2]


def test_replay_resets_when_last_event_id_fell_off_the_ring():
    changes = ChangeFeed()
    for _ in range(RING_SIZE + 5):
        changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
            operation="tasks_create", business_id=BUSINESS)
    request = Request({"last-event-id": "1"})
    response = HTTPAPI._events_stream(_api(changes), request)  # default Request.is_disconnected: True first check
    frames = asyncio.run(_collect(response.body_iterator))
    assert frames == [b"event: reset\ndata: {}\n\n"]


def test_slow_subscriber_gets_reset_instead_of_blocking_publish():
    changes = ChangeFeed()
    request = Request()
    disconnects = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnects)
    response = HTTPAPI._events_stream(_api(changes), request)
    subscriber = next(iter(changes._subscribers.values()))
    stale_event = {"seq": 0, "tables": [], "kinds": [], "business_id": None, "operation": "noop"}
    for _ in range(300):
        subscriber.offer(stale_event)
    assert subscriber.dropped is True
    # 300 offers on a real, bounded queue.Queue never raised or blocked this
    # thread -- the same guarantee a real committing thread depends on.
    frames = asyncio.run(_collect(response.body_iterator))
    assert frames == [b"event: reset\ndata: {}\n\n"]


def test_heartbeat_emitted_when_the_subscriber_queue_stays_empty(monkeypatch):
    monkeypatch.setattr("bazarlyk.service.changes.HEARTBEAT_SECONDS", 0)
    changes = ChangeFeed()
    request = Request()
    disconnects = iter((False, True))
    request.is_disconnected = lambda: _next_async(disconnects)
    response = HTTPAPI._events_stream(_api(changes), request)
    subscriber = next(iter(changes._subscribers.values()))
    subscriber.queue = InstantEmptyQueue()
    frames = asyncio.run(_collect(response.body_iterator))
    assert frames == [b": heartbeat\n\n"]


def test_publish_commit_swallows_internal_failures_and_counts_them(monkeypatch):
    changes = ChangeFeed()
    def boom(*args, **kwargs):
        raise RuntimeError("simulated internal failure")
    monkeypatch.setattr(changes, "_publish_commit", boom)
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)  # must not raise into a caller mid-commit
    assert changes.publish_failures == 1


def test_unregistered_change_feed_is_a_clean_not_found():
    from qorqut_operations.contracts import DomainError
    with pytest.raises(DomainError) as error:
        HTTPAPI._events_stream(_api(None), Request())
    assert error.value.code == "not_found"


# ---------------------------------------------------------------------------
# Database integration: a real writable unit of work, capture before
# finalize_events() deletes inbox_changes, publish after commit.
# ---------------------------------------------------------------------------

def test_writable_unit_publishes_a_trigger_covered_table_with_its_business(tmp_path):
    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    with database.transaction(business_id=BUSINESS, operation="tasks_create") as uow:
        uow.repository("tasks", business_id=BUSINESS).insert({"id": "T1", "title": "Task", "status": "open", "created_by": HUMAN})
    events = changes.replay_since(0)
    assert len(events) == 1
    event = events[0]
    assert event["tables"] == ["tasks"]
    assert event["kinds"] == ["tasks"]
    assert event["business_id"] == BUSINESS
    assert event["operation"] == "tasks_create"
    assert changes.version("tasks", BUSINESS) == event["seq"]


def test_coarse_operation_without_a_capture_trigger_still_publishes_a_kind(tmp_path):
    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    with database.transaction(business_id=BUSINESS, operation="jobs_get") as uow:
        uow.repository("jobs", business_id=BUSINESS).insert({"id": "job-1", "target_namespace": "ns", "kind": "test",
            "principal_id": HUMAN, "arguments_json": "{}", "dedupe_key": "d1", "state": "queued"})
    events = changes.replay_since(0)
    assert len(events) == 1
    event = events[0]
    assert event["tables"] == []  # jobs has no capture trigger: no row to report.
    assert event["kinds"] == ["context_queue", "jobs_routines"]       # the Context queue kind is additive to the existing coarse kind
    assert event["business_id"] == BUSINESS


def test_idle_coarse_operation_with_no_write_publishes_nothing(tmp_path):
    """Review fix 1: the routine scheduler and job admission tag every tick
    this way (`jobs_get`'s coarse kind is `jobs_routines`) whether or not
    anything was due. Before the fix, `_publish_commit` bumped the coarse
    kind on operation name alone -- an idle tick published every few seconds
    forever, a higher idle load than the polling this stage replaces. Compare
    directly against the sibling above, which writes a row under the same
    operation tag and must still publish.
    """
    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    with database.transaction(business_id=BUSINESS, operation="jobs_get"):
        pass  # Nothing due, nothing written -- Database._unit's total_changes must not move.
    assert changes.replay_since(0) == []


def test_read_only_unit_publishes_nothing(tmp_path):
    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    with database.read() as uow:
        uow.repository("principals").get(HUMAN)
    assert changes.replay_since(0) == []


def test_failed_unit_publishes_nothing(tmp_path):
    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    with pytest.raises(RuntimeError):
        with database.transaction(business_id=BUSINESS, operation="tasks_create") as uow:
            uow.repository("tasks", business_id=BUSINESS).insert({"id": "T1", "title": "Task", "status": "open", "created_by": HUMAN})
            raise RuntimeError("handler failed")
    assert changes.replay_since(0) == []


def test_ticker_does_not_wildcard_for_an_in_process_write_that_publishes_nothing(tmp_path):
    """Review fix 2: `businesses` has no capture trigger, and an operation
    name outside COARSE_KIND_BY_OPERATION_PREFIX publishes no kind either --
    exactly an untracked write like the agent-activity sweep or an inbox
    watcher's lease renewal. It still moves `PRAGMA data_version`. Before the
    fix, the ticker compared the ring's own sequence (unchanged, since
    nothing published) and wrongly called this "external," wildcarding every
    known version on every such write and collapsing the 304 hit ratio.
    """
    import sqlite3
    import time

    database = database_at(tmp_path)
    changes = ChangeFeed()
    database.change_feed = changes
    changes.start_external_writer_ticker(lambda: sqlite3.connect(database.path))
    try:
        with database.transaction(business_id=BUSINESS, operation="agent_activity_sweep") as uow:
            uow.connection.execute("UPDATE businesses SET name = ? WHERE id = ?", ("Untracked", BUSINESS))
        # Three full poll intervals: enough for the ticker to have noticed
        # the data_version change and wrongly wildcarded, if it were going to.
        time.sleep(EXTERNAL_WRITER_POLL_SECONDS * 3)
        assert changes.replay_since(0) == []
        assert changes._wildcard_seq == 0
    finally:
        changes.stop()


def test_wildcard_folds_into_a_kind_that_was_never_written_in_this_process():
    """Review fix 3: `publish_wildcard` only bumped keys already present in
    `_versions`. A (kind, scope) never written in this process kept version 0
    forever, so a tag computed before the wildcard still matched one computed
    after it -- a genuine external write served stale. `route_versions` is
    the only method PR3's ETag mechanism actually calls, in both its normal
    and its ANY_SCOPE (owner-aggregate) branch, so both must fold the
    wildcard in.
    """
    changes = ChangeFeed()
    assert changes.route_versions(["tasks"], BUSINESS) == {("tasks", BUSINESS): 0}
    assert changes.route_versions(["tasks"], ANY_SCOPE) == {("tasks", "any"): 0}
    changes.publish_wildcard()
    assert changes.route_versions(["tasks"], BUSINESS)[("tasks", BUSINESS)] > 0
    assert changes.route_versions(["tasks"], ANY_SCOPE)[("tasks", "any")] > 0


def test_etag_mixes_in_a_per_process_nonce():
    """Review fix 4: version counters restart at zero on every service start,
    but a browser's ETag cache survives the restart. Two fresh `ChangeFeed`s
    (standing in for two process starts) have identical version state --
    both zero -- for the same route, so without `changes.nonce` mixed into
    `_etag_for`'s hash these would collide, and a tag cached against the
    first process would false-positive a 304 against the second's own fresh
    version 0.
    """
    from bazarlyk.transport.http import _etag_for

    def fake_request(if_none_match=None):
        return SimpleNamespace(url=SimpleNamespace(path="/api/biz/widget/tasks"), query_params={},
            headers={"if-none-match": if_none_match} if if_none_match else {}, scope={})

    first, second = ChangeFeed(), ChangeFeed()
    tag1, hit1 = _etag_for(fake_request(), first, ["tasks"], BUSINESS)
    tag2, hit2 = _etag_for(fake_request(), second, ["tasks"], BUSINESS)
    assert not hit1 and not hit2
    assert tag1 != tag2
    _, cross_hit = _etag_for(fake_request(tag1), second, ["tasks"], BUSINESS)
    assert cross_hit is False


def test_external_writer_ticker_publishes_wildcard_for_a_commit_outside_the_pool(tmp_path):
    import sqlite3
    import time

    database = database_at(tmp_path)
    changes = ChangeFeed()
    changes.publish_commit(captured=[{"table": "tasks", "scope": BUSINESS, "kinds": {"tasks"}}],
        operation="tasks_create", business_id=BUSINESS)
    before = changes.version("tasks", BUSINESS)
    changes.start_external_writer_ticker(lambda: sqlite3.connect(database.path))
    try:
        # `businesses` has no capture trigger (unlike `principals`), so this
        # plain connection does not also need `inbox_actor()` registered --
        # a real external writer (migration, repair script) may not know to.
        external = sqlite3.connect(database.path)
        try:
            external.execute("PRAGMA busy_timeout = 5000")
            external.execute("UPDATE businesses SET name = 'External' WHERE id = ?", (BUSINESS,))
            external.commit()
        finally:
            external.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and changes.version("tasks", BUSINESS) == before:
            time.sleep(0.1)
        assert changes.version("tasks", BUSINESS) > before
    finally:
        changes.stop()
