"""Owner-only Server-Timing header, extended slow-unit logging, and /api/root/perf."""
from types import SimpleNamespace

from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.transport.http import HTTPAPI
from test_service_transports import ORIGIN, ServiceFixture


def parse_server_timing(value):
    return dict(part.strip().split(";dur=") for part in value.split(","))


def test_owner_route_gets_full_server_timing_header(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/api/session", headers=ORIGIN)
        assert result.status_code == 200, result.text
        timing = parse_server_timing(result.headers["server-timing"])
        assert set(timing) == {"total", "db", "units", "body", "db_wait", "db_read", "db_write"}
        assert float(timing["total"]) >= 0
        assert float(timing["db"]) >= 0
        # session_data() opens exactly one read unit of work.
        assert int(timing["units"]) >= 1
        assert int(timing["body"]) == len(result.content)


def test_events_route_gets_no_server_timing_header_and_is_not_an_mcp_tool(tmp_path, monkeypatch):
    """A single Server-Timing header emitted once at connection-open would
    only ever describe that open, never the change feed stream's lifetime
    (bazarlyk/service/web.py's `ServiceBoundary`), so it is excluded outright.
    `_events_stream` is faked one-shot here for the same reason
    test_service_compression.py's gzip-exclusion test fakes it: a real
    request through TestClient never disconnects, so its real, intentionally
    infinite generator would hang forever. Its own mechanics are exercised
    directly in test_change_feed.py.
    """
    def fake_events_stream(self, request):
        async def events():
            yield b"data: {}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    monkeypatch.setattr(HTTPAPI, "_events_stream", fake_events_stream)
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/api/root/events", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert "server-timing" not in result.headers
        # Not a registered operation: never reachable through the MCP tool surface.
        headers = service.initialize(client, service.agent)
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "events" not in {row["name"] for row in tools.json()["result"]["tools"]}


def test_events_route_rejects_cross_site_requests(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/api/root/events", headers={**ORIGIN, "sec-fetch-site": "cross-site"})
        assert result.status_code >= 400
        assert result.json()["error"]["code"] == "csrf_required"


def test_perf_route_counts_by_templated_pattern_is_owner_only_and_is_no_store(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        for _ in range(2):
            context = client.get("/api/biz/widget/context", headers=ORIGIN)
            assert context.status_code == 200, context.text
        perf = client.get("/api/root/perf", headers=ORIGIN)
        assert perf.status_code == 200, perf.text
        assert perf.headers["cache-control"] == "no-store"
        row = next(row for row in perf.json()["data"]["routes"]
            if row["method"] == "GET" and row["pattern"] == "/api/biz/{slug}/context")
        assert row["count"] == 2
        assert row["total_ms"] >= 0 and row["max_ms"] >= 0 and row["db_ms"] >= 0
        assert row["units"] >= 2 and row["bytes"] > 0
        # Not a registered operation: never reachable through the MCP tool surface.
        headers = service.initialize(client, service.agent)
        tools = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "perf" not in {row["name"] for row in tools.json()["result"]["tools"]}


def test_perf_route_counts_carry_the_etag_hit_miss_ratio_for_a_revalidated_route(tmp_path):
    """Handoff 4.3: the point of etag;desc=hit|miss on Server-Timing is that
    the 304 ratio can be read from /api/root/perf, not only inspected per
    request -- RouteCounters.record (bazarlyk/transport/http.py) tallies it
    per (method, pattern) alongside the existing total_ms/units/bytes."""
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        miss = client.get("/api/biz/widget/tasks", headers=ORIGIN)
        assert miss.status_code == 200, miss.text
        hit = client.get("/api/biz/widget/tasks", headers={**ORIGIN, "If-None-Match": miss.headers["etag"]})
        assert hit.status_code == 304
        perf = client.get("/api/root/perf", headers=ORIGIN)
        row = next(row for row in perf.json()["data"]["routes"]
            if row["method"] == "GET" and row["pattern"] == "/api/biz/{slug}/tasks")
        assert row["count"] == 2
        assert row["etag_hits"] == 1 and row["etag_misses"] == 1
        # A route this PR did not wire (session) never had etag set at all,
        # and must not be silently counted either way.
        other = client.get("/api/session", headers=ORIGIN)
        assert other.status_code == 200, other.text
        perf = client.get("/api/root/perf", headers=ORIGIN)
        row = next(row for row in perf.json()["data"]["routes"]
            if row["method"] == "GET" and row["pattern"] == "/api/session")
        assert row["etag_hits"] == 0 and row["etag_misses"] == 0


def test_route_pattern_collapses_authored_context_person_slugs(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        first = client.get("/api/biz/widget/context/person/alice", headers=ORIGIN)
        second = client.get("/api/biz/widget/context/person/bob", headers=ORIGIN)
        # Neither person document exists; both requests still get counted.
        assert first.status_code == second.status_code == 404
        perf = client.get("/api/root/perf", headers=ORIGIN)
        rows = perf.json()["data"]["routes"]
        collapsed = [row for row in rows if row["method"] == "GET"
            and row["pattern"] == "/api/biz/{slug}/context/person/…"]
        assert len(collapsed) == 1
        assert collapsed[0]["count"] == 2
        # The authored slugs themselves never surface as their own literal rows.
        assert not any("alice" in row["pattern"] or "bob" in row["pattern"] for row in rows)


def test_route_pattern_collapses_unbounded_asset_path_tails(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        # Two different authored asset names under the same group, one of them
        # nested deeper still - the free-form tail is unbounded in length.
        shallow = client.get("/api/biz/widget/asset/drafted/one.png", headers=ORIGIN)
        deep = client.get("/api/biz/widget/asset/drafted/nested/two.png", headers=ORIGIN)
        assert shallow.status_code == deep.status_code == 404
        perf = client.get("/api/root/perf", headers=ORIGIN)
        rows = perf.json()["data"]["routes"]
        collapsed = [row for row in rows if row["method"] == "GET"
            and row["pattern"] == "/api/biz/{slug}/asset/drafted/…"]
        assert len(collapsed) == 1
        assert collapsed[0]["count"] == 2
        assert not any("one.png" in row["pattern"] or "two.png" in row["pattern"] or "nested" in row["pattern"] for row in rows)


def test_route_pattern_leaves_short_fixed_vocabulary_routes_distinct(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        for _ in range(2):
            notes = client.get("/api/biz/widget/notes", headers=ORIGIN)
            assert notes.status_code == 200, notes.text
        for _ in range(3):
            search = client.get("/api/biz/widget/context/search", params={"q": ""}, headers=ORIGIN)
            assert search.status_code == 200, search.text
        client.get("/api/biz/widget/context/map", headers=ORIGIN)
        perf = client.get("/api/root/perf", headers=ORIGIN)
        rows = {(row["method"], row["pattern"]): row for row in perf.json()["data"]["routes"]}
        # A short route under the cap keeps its own uncollapsed pattern and count.
        assert rows[("GET", "/api/biz/{slug}/notes")]["count"] == 2
        # Two different routes that both sit exactly at the cap depth (4
        # segments) stay distinct from each other, not merged together.
        assert rows[("GET", "/api/biz/{slug}/context/search")]["count"] == 3
        assert ("GET", "/api/biz/{slug}/context/map") in rows
        assert rows[("GET", "/api/biz/{slug}/context/search")] is not rows[("GET", "/api/biz/{slug}/context/map")]


def test_slow_read_unit_now_logs_a_warning(caplog, monkeypatch, tmp_path):
    import bazarlyk.storage.database as database_module
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    clock = iter([0.0, 0.0, 0.30])
    monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    request_id = new_id()
    with db.observe("notes_list", request_id):
        with db.read():
            pass
    assert "operation=notes_list" in caplog.text
    assert request_id in caplog.text
    assert "hold_ms=300.0" in caplog.text and "outcome=ok" in caplog.text
    assert "SELECT" not in caplog.text and "BEGIN" not in caplog.text


def test_fast_read_unit_does_not_log(caplog, monkeypatch, tmp_path):
    import bazarlyk.storage.database as database_module
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    clock = iter([0.0, 0.0, 0.01])
    monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with db.observe("notes_list", new_id()):
        with db.read():
            pass
    assert "storage_transaction" not in caplog.text


def test_slow_write_unit_still_logs_a_warning(caplog, monkeypatch, tmp_path):
    """Regression: dropping the `writable and` guard must not weaken the write path."""
    import bazarlyk.storage.database as database_module
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    clock = iter([0.0, 0.0, 0.30])
    monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    request_id = new_id()
    with db.observe("tasks_create", request_id):
        with db.transaction():
            pass
    assert "operation=tasks_create" in caplog.text
    assert "hold_ms=300.0" in caplog.text and "outcome=ok" in caplog.text


def test_slow_unit_warning_is_throttled_per_operation(caplog, monkeypatch, tmp_path):
    import bazarlyk.storage.database as database_module
    monkeypatch.setattr(database_module, "_SLOW_UNIT_LAST_WARNED_AT", {})
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    # Three slow reads of the same operation: started, acquired, ended per unit.
    # First and second land 1s apart (inside the 10s window); the third lands
    # 20s after the first (outside the window).
    clock = iter([0.0, 0.0, 0.30, 1.0, 1.0, 1.30, 20.0, 20.0, 20.30])
    monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    request_id = new_id()

    with db.observe("notes_list", request_id):
        with db.read():
            pass
    assert caplog.text.count("storage_transaction") == 1
    caplog.clear()

    with db.observe("notes_list", request_id):
        with db.read():
            pass
    # Still inside the throttle window: no second warning line.
    assert "storage_transaction" not in caplog.text
    caplog.clear()

    with db.observe("notes_list", request_id):
        with db.read():
            pass
    # Window elapsed: warns again.
    assert caplog.text.count("storage_transaction") == 1


def test_slow_unit_warning_throttle_is_per_operation_not_global(caplog, monkeypatch, tmp_path):
    import bazarlyk.storage.database as database_module
    monkeypatch.setattr(database_module, "_SLOW_UNIT_LAST_WARNED_AT", {})
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    # Second slow unit is a different operation, 1s after the first (well
    # inside the first operation's throttle window) - it must still log.
    clock = iter([0.0, 0.0, 0.30, 1.0, 1.0, 1.30])
    monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    with db.observe("notes_list", new_id()):
        with db.read():
            pass
    assert "operation=notes_list" in caplog.text
    caplog.clear()

    with db.observe("tasks_create", new_id()):
        with db.transaction():
            pass
    assert "operation=tasks_create" in caplog.text


def test_unit_accumulates_request_timing_across_reads_and_writes(tmp_path):
    import bazarlyk.storage.database as database_module
    db = database_module.Database(tmp_path / "state.sqlite3").initialize()
    timing = database_module.RequestTiming()
    token = database_module.REQUEST_TIMING.set(timing)
    try:
        with db.read():
            pass
        with db.transaction():
            pass
    finally:
        database_module.REQUEST_TIMING.reset(token)
    assert timing.units == 2
    assert timing.db_seconds >= 0
