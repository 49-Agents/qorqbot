"""Gzip selection and static Cache-Control headers (bazarlyk/service/web.py, PR5).

`ServiceFixture` and `ORIGIN` come from `test_service_transports`, matching the
fixture/setup style `test_request_observability.py` already uses for the same
`create_app()` ASGI stack. httpx (the TestClient's transport) sends
`Accept-Encoding: gzip, deflate` by default on every request unless a test
overrides it, so plain `headers=ORIGIN` already exercises the "client accepts
gzip" case; an explicit `Accept-Encoding: identity` override is used below
only where a request must prove what an *uncompressed* baseline looks like.
"""
import pytest
from starlette.responses import StreamingResponse
from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.service.web import SelectiveGZip, create_app
from bazarlyk.transport.http import HTTPAPI
from test_agent_configuration import REPO, PATHS
from test_service_transports import ORIGIN, ServiceFixture

IDENTITY = {**ORIGIN, "Accept-Encoding": "identity"}


# ---------------------------------------------------------------------------
# Unit-level: the path decision itself, independent of response size, fixture
# cost, or any particular route's current behavior. This is the precise proof
# that the terminal family (every action, not only the streaming one) and
# agent tooling are excluded by construction; the integration tests below
# then prove the decision is actually wired into create_app()'s middleware.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,expected", [
    ("/", True),
    ("/app.css", True),
    ("/js/app.js", True),
    ("/css/app.css", True),
    ("/api/session", True),
    ("/api/biz/widget/notes", True),
    ("/api/biz/widget/context", True),
    ("/api/root/perf", True),
    ("/api/root/agents", True),
    ("/api/root/events", False),
    ("/api/agent/bootstrap", False),
    ("/api/agent/readiness", False),
    ("/api/agent/technical-observation", False),
    ("/api/root/terminal/abc/stream", False),
    ("/api/root/terminal/abc/start", False),
    ("/api/root/terminal/abc/input", False),
    ("/api/root/terminals", False),
    ("/api/root/workspace-terminal/stream", False),
    ("/api/root/workspace-terminal/start", False),
    ("/api/biz/widget/terminal/abc/stream", False),
    ("/api/biz/widget/terminal/abc/start", False),
    ("/api/biz/widget/terminals", False),
    ("/mcp", False),
    ("/mcp/owner", False),
    ("/health/live", False),
    ("/health/ready", False),
    ("/" + "a" * 64 + "/vnc", False),  # agent browser proxy path shape
])
def test_compressible_path_selection(path, expected):
    assert SelectiveGZip._compressible(path) is expected


def _inflate_notes(service, count=8, size=300):
    """Push /api/biz/widget/notes past GZipMiddleware's minimum_size=1024."""
    for i in range(count):
        result = service.runtime.application.invoke(service.fixture.ctx, "notes_create",
            {"title": f"Bulk note {i}", "body": "x" * size}, {"idempotency_key": new_id()})
        assert result.status == "ok", result.to_dict()


def test_gzip_present_and_effective_on_owner_api_route(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    _inflate_notes(service)
    with TestClient(service.web) as client:
        baseline = client.get("/api/biz/widget/notes", headers=IDENTITY)
        assert baseline.status_code == 200, baseline.text
        assert "content-encoding" not in baseline.headers
        uncompressed_len = int(baseline.headers["content-length"])
        assert uncompressed_len >= 1024

        compressed = client.get("/api/biz/widget/notes", headers=ORIGIN)
        assert compressed.status_code == 200, compressed.text
        assert compressed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in compressed.headers.get("vary", "").lower()
        # content-length here is the wire (compressed) size GZipMiddleware set;
        # httpx already decoded .content for us, so comparing the two proves
        # real, effective compression rather than a mislabeled passthrough.
        assert int(compressed.headers["content-length"]) < uncompressed_len
        # A successful, semantically-correct decode proves the bytes were
        # genuinely valid gzip, not just labeled as such.
        assert compressed.json() == baseline.json()
        assert len(compressed.json()) == 8


def test_gzip_present_and_effective_on_static_file(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        baseline = client.get("/app.css", headers=IDENTITY)
        assert baseline.status_code == 200, baseline.text
        assert "content-encoding" not in baseline.headers
        uncompressed_len = int(baseline.headers["content-length"])
        assert uncompressed_len >= 1024, "fixture app.css is too small to prove compression"

        compressed = client.get("/app.css", headers=ORIGIN)
        assert compressed.status_code == 200, compressed.text
        assert compressed.headers["content-encoding"] == "gzip"
        assert int(compressed.headers["content-length"]) < uncompressed_len
        assert compressed.content == baseline.content  # decodes back to the identical file


def test_mcp_route_sends_neither_gzip_nor_server_timing_when_client_accepts_gzip(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        # Both request forms the route sees: with the dashboard's Origin header and without any.
        for headers in ({**ORIGIN, "Accept-Encoding": "gzip"}, {"Accept-Encoding": "gzip"}):
            result = client.post("/mcp", json={}, headers=headers)
            assert "content-encoding" not in result.headers
            assert "server-timing" not in result.headers


def test_agent_route_sends_neither_gzip_nor_server_timing_when_client_accepts_gzip(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        hook = {"registration_id": service.registrations[service.agent.id], "session_id": "one",
            "event": "startup", "actor": "owner"}
        result = client.post("/api/agent/bootstrap", json=hook,
            headers={"Authorization": "Bearer " + service.tokens[service.agent.id], "Accept-Encoding": "gzip"})
        assert "content-encoding" not in result.headers
        assert "server-timing" not in result.headers


class _FakeTerminalSSE:
    """A minimal `terminals` collaborator: only `.route()` matters to
    `HTTPAPI`'s dispatch (`tail[:1] in (["terminal"], ["terminals"])`), so
    this exercises the real gzip wiring around a real `StreamingResponse`
    without needing a real broker, tmux session, or registered agent."""

    CHUNK = b"data: " + b"x" * 2000 + b"\n\n"  # comfortably over minimum_size=1024

    async def route(self, request, context, business, tail):
        async def events():
            yield self.CHUNK
        return StreamingResponse(events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def test_gzip_never_touches_terminal_sse_stream(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    web = create_app(service.runtime, origins=["http://testserver"],
        authentication=service.authentication, terminals=_FakeTerminalSSE())
    with TestClient(web) as client:
        result = client.get("/api/biz/widget/terminal/agent/stream", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert "content-encoding" not in result.headers
        # Byte-identical: proves GZipMiddleware never buffered or altered the
        # stream, not merely that it skipped adding a header.
        assert result.content == _FakeTerminalSSE.CHUNK


def test_gzip_never_touches_the_change_feed_stream(tmp_path, monkeypatch):
    """`_events_stream`'s real generator loops on `is_disconnected()` for the
    stream's whole life and never terminates on its own; a real request
    through `TestClient` never signals a disconnect, so (matching
    `_FakeTerminalSSE` above) only the one-shot shape needed to prove gzip
    exclusion is faked here. `_events_stream`'s own replay/reset/heartbeat
    mechanics are exercised directly, without a StreamingResponse or
    TestClient at all, in test_change_feed.py.
    """
    chunk = b"data: {}\n\n"

    def fake_events_stream(self, request):
        async def events():
            yield chunk
        return StreamingResponse(events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    monkeypatch.setattr(HTTPAPI, "_events_stream", fake_events_stream)
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/api/root/events", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert "content-encoding" not in result.headers
        assert result.content == chunk


def test_no_store_on_index_no_cache_on_other_static_mounts(tmp_path):
    """`/` keeps the initial platform commit's explicit, stricter no-store:
    PR5 widening it to no-cache (matching the other static mounts below) was
    an overreach beyond "add revalidation" and has been reverted. app.css and
    the /js, /css mounts are unrelated static assets with no such history and
    keep the no-cache revalidation PR5 added.
    """
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"

        result = client.get("/app.css", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-cache"

        js_file = next((PATHS.component("dashboard") / "js").glob("*.js")).name
        css_file = next((PATHS.component("dashboard") / "css").glob("*.css")).name
        for path in (f"/js/{js_file}", f"/css/{css_file}"):
            result = client.get(path, headers=ORIGIN)
            assert result.status_code == 200, (path, result.text)
            assert result.headers["cache-control"] == "no-cache"


def test_no_cache_is_not_added_to_api_responses(tmp_path):
    """PR5 must not add any Cache-Control logic under /api/*.

    `bazarlyk/transport/results.py::response()` already sets a blanket
    `Cache-Control: no-store` on every operation-result response (discovered
    empirically here, not assumed), and at least one dashboard route
    (`env`) sets its own `no-store` directly on a `JSONResponse` for
    unrelated, pre-existing reasons. Both mechanisms predate this PR and must
    come through completely unchanged: still exactly "no-store", not
    "no-cache", not duplicated, not removed.
    """
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        result = client.get("/api/session", headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"

        env = client.get("/api/biz/widget/env", headers=ORIGIN)
        assert env.status_code == 200, env.text
        assert env.headers["cache-control"] == "no-store"
