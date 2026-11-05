"""Focused regression coverage for transport incident boundaries."""

from contextlib import contextmanager
import asyncio
import json
from types import SimpleNamespace

from bazarlyk.application.engine import Application
from qorqut_operations.contracts import InvocationContext, Principal, new_id
from bazarlyk.incidents import IncidentReporter
from bazarlyk.transport.auth import AuthenticatedPrincipal
from bazarlyk.transport.http import HTTPAPI
from bazarlyk.transport.mcp import MCPTransport
from starlette.requests import Request


def context():
    return InvocationContext(Principal(new_id(), "agent"), request_id=new_id())


def spool(reporter):
    return [json.loads(line) for line in
            (reporter.spool_dir / "system-incidents.jsonl").read_text().splitlines()]


def test_readiness_boundary_records_failure_and_matching_success(tmp_path):
    reporter = IncidentReporter(tmp_path / "incidents")
    ctx = context()

    class Config:
        data = {"preparation": {"ready": False}}

    reporter.boundary(ctx, "agent.readiness", "resolve", lambda: Config(), readiness=True)
    Config.data = {"preparation": {"ready": True}}
    reporter.boundary(ctx, "agent.readiness", "resolve", lambda: Config(), readiness=True)

    observations = spool(reporter)
    assert [row["error_code"] for row in observations] == ["bootstrap_unavailable", "success"]
    assert all(row["request_id"] == ctx.request_id for row in observations)
    assert all("session_id" not in row or row["session_id"] is None for row in observations)


def test_transport_failure_is_bounded_and_keeps_trusted_refs_only(tmp_path):
    reporter = IncidentReporter(tmp_path / "incidents")
    agent_id, request_id = new_id(), new_id()
    reporter.transport_failure(component="http", operation="business.operation",
        request_id=request_id, agent_id=agent_id, business_id=new_id())

    row = spool(reporter)[0]
    assert row["request_id"] == request_id
    assert row["agent_id"] == agent_id
    assert row["message"] == ""


def test_unavailable_reporter_does_not_mark_boundary_exception_as_reported(tmp_path):
    reporter = IncidentReporter(tmp_path / "incidents")
    reporter._spool = lambda encoded: False
    error = RuntimeError("boundary crash")
    try:
        reporter.boundary(context(), "mcp", "tasks_list", lambda: (_ for _ in ()).throw(error))
    except RuntimeError as caught:
        assert not getattr(caught, "_qorqut_incident_reported", False)
    else:
        raise AssertionError("boundary did not re-raise the transport error")


class _Database:
    @contextmanager
    def observe(self, *args, **kwargs):
        yield


def test_application_exception_marker_allows_outer_boundary_to_deduplicate():
    app = Application(_Database(), SimpleNamespace())
    app.incident_reporter = object()
    app.observe_result = lambda *args, **kwargs: True
    app.invoke_or_raise = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

    try:
        app.invoke(context(), "operation")
    except RuntimeError as exc:
        assert exc._qorqut_incident_reported is True
    else:
        raise AssertionError("application invocation did not fail")


class _RecordingReporter:
    def __init__(self):
        self.failures, self.successes = [], []

    def transport_failure(self, **values):
        self.failures.append(values)
        return {"status": "queued"}

    def transport_success(self, **values):
        self.successes.append(values)
        return {"status": "queued"}


class _Origins:
    def __init__(self, *, fail=False):
        self.fail = fail

    def validate(self, request, mutation=False):
        if self.fail:
            self.fail = False
            raise RuntimeError("origin probe failed")


def _request(path, *, headers=()):
    scope = {"type": "http", "method": "GET", "path": path, "path_params": {"path": path.removeprefix("/api/")},
             "headers": list(headers), "query_string": b"", "scheme": "http", "http_version": "1.1",
             "root_path": "", "client": ("127.0.0.1", 1), "server": ("test", 80)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_http_outer_crash_returns_internal_error_and_next_success_probes_recovery():
    reporter = _RecordingReporter()
    origins = _Origins(fail=True)
    owner = Principal(new_id(), "human", "Owner")
    auth = SimpleNamespace(installation_owner=lambda: (owner, new_id(), 1))
    runtime = SimpleNamespace(incidents=None, application=SimpleNamespace(incident_reporter=reporter), require_authority=lambda: None)
    api = HTTPAPI(runtime, auth, origins)

    first = asyncio.run(api(_request("/api/session")))
    second = asyncio.run(api(_request("/api/session")))
    assert first.status_code == 500
    assert second.status_code == 200
    assert len(reporter.failures) == 1
    assert len(reporter.successes) == 1
    assert reporter.failures[0]["request_id"] != reporter.successes[0]["request_id"]
    assert reporter.failures[0]["operation"] == reporter.successes[0]["operation"] == "session"


def test_mcp_single_crash_reports_once_and_preserves_internal_error_result():
    reporter = _RecordingReporter()
    principal = Principal(new_id(), "agent", "Agent")
    actor = AuthenticatedPrincipal(principal, credential_id=new_id(), registration_id=new_id())
    descriptor = SimpleNamespace(name="tasks_list", owner_only=False, exposure={"mcp"}, transport_only=False,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False}, scope_kinds={"business"})
    runtime = SimpleNamespace(incidents=None, application=SimpleNamespace(
        incident_reporter=reporter, invoke=lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))),
        registry=SimpleNamespace(metadata=lambda name: descriptor, get=lambda name: descriptor),
        agent_context=lambda *args, **kwargs: InvocationContext(principal))
    transport = MCPTransport(runtime, SimpleNamespace(), _Origins())

    result = asyncio.run(transport._single_result("tasks_list", {}, actor))
    assert result["status"] == "error"
    assert result["error"]["code"] == "internal_error"
    assert len(reporter.failures) == 1
    assert reporter.failures[0]["agent_id"] == principal.id


def test_mcp_asgi_outer_crash_returns_structured_error_and_reports():
    reporter = _RecordingReporter()
    principal = Principal(new_id(), "agent", "Agent")
    actor = AuthenticatedPrincipal(principal, credential_id=new_id(), registration_id=new_id())
    runtime = SimpleNamespace(incidents=None, application=SimpleNamespace(incident_reporter=reporter),
        require_authority=lambda: None)
    auth = SimpleNamespace(agent=lambda authorization: actor)
    transport = MCPTransport(runtime, auth, _Origins())

    async def broken_manager(*args):
        raise RuntimeError("manager crash")

    transport.manager.handle_request = broken_manager
    sent = []
    async def send(message):
        sent.append(message)
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer token")],
             "query_string": b"", "scheme": "http", "http_version": "1.1", "root_path": "",
             "client": ("127.0.0.1", 1), "server": ("test", 80)}

    asyncio.run(transport(scope, receive, send))
    assert next(message for message in sent if message["type"] == "http.response.start")["status"] == 500
    assert len(reporter.failures) == 1
    assert reporter.failures[0]["agent_id"] == principal.id
