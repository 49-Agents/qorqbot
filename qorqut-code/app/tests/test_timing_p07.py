"""Owner Server-Timing: the wait/read/write split beside the established entries, and the header's boundaries.

Synthetic only: a tmp instance behind the real HTTP application for the executable routes, and a bare ``ServiceBoundary`` around
a stub ASGI app for the header formatter's defaults and its agent/MCP/event exclusions (no real stream is opened, so nothing can
hang). Durations are only checked for shape, never for a wall time."""

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import bazarlyk.transport.http as http_module
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import ServiceBoundary, create_app
from bazarlyk.transport.auth import Authentication, OriginPolicy
from host_foundation_support import build_instance

ORIGIN = {"Origin": "http://testserver"}
ESTABLISHED = ["total", "db", "units", "body"]
SPLIT = ["db_wait", "db_read", "db_write"]


def entries(response):
    """The Server-Timing header as an ordered list of (name, parameter-text) pairs."""
    return [tuple(part.strip().split(";", 1)) for part in response.headers["server-timing"].split(",")]


def durations(response):
    return {name: parameter.removeprefix("dur=") for name, parameter in entries(response)}


@pytest.fixture
def host(tmp_path):
    instance, _ids = build_instance(tmp_path)
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    authentication = Authentication(instance.database, runtime.paths)
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    app = create_app(runtime, dashboard_root=dashboard, origins=["http://testserver"], authentication=authentication,
                     manage_lifecycle=False)
    scopes = []

    async def recording(scope, receive, send):
        # The ASGI scope is the one dict HTTPAPI fills and the header formatter reads.
        if scope["type"] == "http":
            scopes.append(scope)
        await app(scope, receive, send)

    with TestClient(recording, base_url="http://testserver") as client:
        client.cookies.set("bazarlyk_session", authentication.issue_browser())
        yield SimpleNamespace(client=client, scopes=scopes, runtime=runtime)


def test_owner_read_route_keeps_the_established_entries_and_adds_the_split(host):
    response = host.client.get("/api/session", headers=ORIGIN)
    assert response.status_code == 200, response.text
    header = entries(response)
    # The four established entries keep their names, order and meaning; the split follows them.
    assert [name for name, _ in header] == ESTABLISHED + SPLIT
    values = durations(response)
    assert float(values["total"]) >= 0 and float(values["db"]) >= 0
    assert int(values["units"]) >= 1
    assert int(values["body"]) == len(response.content)
    assert all(float(values[name]) >= 0 for name in SPLIT)
    scope = host.scopes[-1]
    assert scope["bazarlyk.units"] == int(values["units"])
    assert scope["bazarlyk.read_units"] + scope["bazarlyk.write_units"] == scope["bazarlyk.units"]
    assert scope["bazarlyk.read_units"] >= 1
    # Hold time is split by kind, and the established `db` figure is still the whole of it (up to the header's rounding).
    assert scope["bazarlyk.db_read_ms"] + scope["bazarlyk.db_write_ms"] == pytest.approx(scope["bazarlyk.db_ms"])
    assert scope["bazarlyk.db_wait_ms"] >= 0
    assert scope["bazarlyk.response_bytes"] == len(response.content)


def test_owner_write_route_counts_its_write_unit(host):
    response = host.client.post("/api/root/operations/tags_create", json={"label": "spring", "idempotency_key":
        "00000000-0000-4000-8000-0000000000aa"}, headers=ORIGIN)
    assert response.status_code == 200 and response.json()["status"] == "ok", response.text
    assert [name for name, _ in entries(response)] == ESTABLISHED + SPLIT
    scope = host.scopes[-1]
    assert scope["bazarlyk.write_units"] >= 1
    assert scope["bazarlyk.read_units"] + scope["bazarlyk.write_units"] == scope["bazarlyk.units"]
    assert scope["bazarlyk.db_write_ms"] >= 0
    assert float(durations(response)["db_write"]) == pytest.approx(scope["bazarlyk.db_write_ms"], abs=0.06)


def test_an_older_paired_component_without_the_split_reports_zeros_and_still_serves(host, monkeypatch):
    class OlderTiming:
        """What the previous Operations build recorded: no wait, read or write fields at all."""
        __slots__ = ("started", "db_seconds", "units")

        def __init__(self):
            self.started, self.db_seconds, self.units = http_module.time.monotonic(), 0.0, 0

        def record(self, *, writable, wait, held):
            self.db_seconds += held
            self.units += 1

    monkeypatch.setattr(http_module, "RequestTiming", OlderTiming)
    response = host.client.get("/api/session", headers=ORIGIN)
    assert response.status_code == 200, response.text
    values = durations(response)
    assert [name for name, _ in entries(response)] == ESTABLISHED + SPLIT
    assert int(values["units"]) >= 1  # the established figures still come through
    assert (values["db_wait"], values["db_read"], values["db_write"]) == ("0.0", "0.0", "0.0")
    scope = host.scopes[-1]
    assert (scope["bazarlyk.read_units"], scope["bazarlyk.write_units"]) == (0, 0)


# -- the header formatter and its boundaries, around a stub app -----------------------------------------------------------

def boundary_response(path, scope_values=None, *, operation="", method="GET"):
    async def stub(scope, receive, send):
        scope.update(scope_values or {})
        scope["bazarlyk.transport_operation"] = operation
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"private body text"})

    # Not entered as a context manager: the stub has no lifespan, and none is needed.
    client = TestClient(ServiceBoundary(stub, OriginPolicy(["http://testserver"])), base_url="http://testserver")
    return client.request(method, path, headers=ORIGIN)


def test_absent_scope_values_default_to_zero_without_changing_the_established_entries():
    response = boundary_response("/api/root/anything")
    assert response.headers["server-timing"] == ("total;dur=0.0, db;dur=0.0, units;dur=0, body;dur=0, "
                                                 "db_wait;dur=0.0, db_read;dur=0.0, db_write;dur=0.0")


def test_scope_values_are_formatted_in_milliseconds_and_etag_stays_last():
    response = boundary_response("/api/root/anything", {
        "bazarlyk.total_ms": 12.34, "bazarlyk.db_ms": 3.0, "bazarlyk.units": 4, "bazarlyk.response_bytes": 17,
        "bazarlyk.db_wait_ms": 0.449, "bazarlyk.db_read_ms": 2.5, "bazarlyk.db_write_ms": 0.5, "bazarlyk.etag": "miss"})
    assert response.headers["server-timing"] == ("total;dur=12.3, db;dur=3.0, units;dur=4, body;dur=17, "
                                                 "db_wait;dur=0.4, db_read;dur=2.5, db_write;dur=0.5, etag;desc=miss")


def test_the_header_is_bounded_and_carries_no_content():
    response = boundary_response("/api/root/anything", {"bazarlyk.total_ms": 1.0, "bazarlyk.response_bytes": 17})
    header = response.headers["server-timing"]
    names = [name for name, _ in entries(response)]
    assert names == ESTABLISHED + SPLIT and len(set(names)) == len(names)
    assert len(header) < 200 and "private" not in header


@pytest.mark.parametrize("path, operation", [
    ("/api/agent/bootstrap", "agent.bootstrap"),
    ("/api/agent/anything", "agent"),
    ("/api/root/events", "events"),
    ("/mcp", ""),
    ("/js/app.js", ""),
    ("/health/live", ""),
])
def test_agent_event_mcp_and_static_responses_still_get_no_server_timing(path, operation):
    response = boundary_response(path, {"bazarlyk.db_wait_ms": 5.0, "bazarlyk.db_read_ms": 5.0, "bazarlyk.db_write_ms": 5.0},
                                 operation=operation)
    assert response.status_code == 200
    assert "server-timing" not in response.headers
