"""Synthetic helpers for the host Context source-status checks (H2-host).

Everything runs on ``context_completion_support.build``: the composed host with the Context integration's queue and filing storage, the
real ``DocumentOperations`` / ``ContextFiling`` / registered ``context.file`` job / queue driver, a disposable real Context and an
injected model runner. The retained dashboard compatibility routes are driven in-process with real Starlette ``Request`` objects
through the same registered handlers the HTTP layer dispatches to (``business_route`` / ``global_route``); no server, authenticated
session, provider, model, native session or operational data is involved, and the HTTP layer's own principal/business resolution is
replaced by the invocation contexts the fixture builds (that is the stated isolated-wiring limitation of every capture made here)."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

from starlette.requests import Request

from bazarlyk.transport.compatibility import DashboardCompatibility

FILE_PATH = "foundation/documents/{}.txt"


class Routes:
    """The retained compatibility routes, called with real requests. ``business`` is the selected business slug (or None for Global)."""

    def __init__(self, host):
        self.host = host
        self.compat = DashboardCompatibility(host.runtime, background=host.background)

    @staticmethod
    def request(method, path, query=None, body=None, extra=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())] if body is not None else []
        headers += [(key.lower().encode(), value.encode()) for key, value in (extra or {}).items()]
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                        "query_string": urlencode(query or {}).encode(), "headers": headers, "scheme": "http",
                        "server": ("testserver", 80), "client": ("127.0.0.1", 1), "http_version": "1.1"}, receive)

    def send(self, ctx, method, path, query=None, body=None, extra=None):
        """The route's response object; ``path`` is the API path without the ``/api`` prefix."""
        request = self.request(method, "/api/" + path, query, body, extra)
        parts = path.split("/")
        if parts[0] == "biz":
            return asyncio.run(self.compat.business_route(request, ctx, {"slug": parts[1], "id": ctx.business_id}, parts[2:]))
        return asyncio.run(self.compat.global_route(request, ctx, parts))

    def call(self, ctx, method, path, query=None, body=None):
        """``(status_code, decoded JSON)`` of one route."""
        result = self.send(ctx, method, path, query, body)
        return result.status_code, json.loads(result.body) if result.body else None

    def revalidate(self, ctx, path, etag=None, **query):
        """``(status_code, decoded JSON or None, ETag)`` of a cacheable GET, optionally conditional on a previously issued tag."""
        result = self.send(ctx, "GET", path, query, None, {"if-none-match": etag} if etag else None)
        return result.status_code, json.loads(result.body) if result.body else None, result.headers.get("etag")

    def get(self, ctx, path, **query):
        return self.call(ctx, "GET", path, query)

    def post(self, ctx, path, body):
        return self.call(ctx, "POST", path, None, body)


def shown(host, ctx, document_id, **arguments):
    """The document as an unscoped reader sees it."""
    return host.read(ctx, "context_get", {"document_id": document_id, **arguments})["document"]


def scoped(host, ctx, document_id, **arguments):
    """The document as the scoped library reader sees it (``scope`` selects the business library)."""
    return host.read(ctx, "context_get", {"document_id": document_id, "scope": "business", **arguments})["document"]


def filings(host, ctx, document_id, query="Alice"):
    """The current-source filing of one upload as each read path presents it: unscoped and scoped get/list/search."""
    def item(items):
        return next(entry for entry in items if entry["id"] == document_id)
    return {
        "get": shown(host, ctx, document_id)["filing"],
        "list": item(host.read(ctx, "context_list", {"kind": "context_upload"})["items"])["filing"],
        "search": item(host.read(ctx, "context_search", {"query": query})["items"])["filing"],
        "scoped_get": scoped(host, ctx, document_id)["filing"],
        "scoped_list": item(host.read(ctx, "context_list", {"scope": "business", "kind": "context_upload"})["items"])["filing"],
        "scoped_search": item(host.read(ctx, "context_search", {"scope": "business", "query": query})["items"])["filing"],
    }


def settle(host, job_id=None):
    """Run the registered job once after its 15 second deferral has elapsed."""
    host.clock.advance(seconds=16)
    return host.run_job(job_id)


def upload(host, body="Met Alice about the pilot.", *, agent=None, **arguments):
    data = host.upload(agent or host.agent(), body, **arguments)
    return data["document"], data["job"]["id"]


def cancel(host, job_id):
    """The job is cancelled by a trusted path (no existing API cancels a non-routine job): only its stored state changes."""
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET state='cancelled', error_json=? WHERE id=?",
                               (json.dumps({"code": "cancelled", "message": "Owner stopped this job", "retryable": False}), job_id))


def brief(filing):
    """The facts a status walk asserts: state, derived status, completion, retry hint, job state, operation and attempts."""
    return {key: filing[key] for key in ("state", "status", "completion_state", "retryable", "job_state", "operation", "attempts")}


ORIGIN = "http://localhost:8080"


class Http:
    """The real ``HTTPAPI`` dispatcher over the composed host, driven with synthetic in-process requests.

    Real: ``HTTPAPI.__call__`` (origin/CSRF validation, request-size and JSON checks, the root and business operation dispatch, option
    splitting, selector refusal, ``failure`` mapping), the real ``Authentication.installation_owner`` resolution of the single-owner
    dashboard principal, ``OriginPolicy``, ``policy.context_for`` and ``Application.invoke``. Faked: only the signing key (never used: no
    cookie is minted) and the transport itself (no socket or server)."""

    def __init__(self, host):
        from bazarlyk.transport.auth import Authentication, OriginPolicy
        from bazarlyk.transport.http import HTTPAPI

        self.host = host
        self.authentication = Authentication(host.database, host.paths, signing_key=b"synthetic-dispatch-key-not-a-secret-000")
        self.api = HTTPAPI(host.runtime, self.authentication, OriginPolicy([ORIGIN]),
                           compatibility=DashboardCompatibility(host.runtime, background=host.background))

    @staticmethod
    def request(method, path, body=None, query=None, extra=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"host", b"localhost:8080")]
        if method != "GET":
            headers += [(b"origin", ORIGIN.encode()), (b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
        headers += [(key.lower().encode(), value.encode()) for key, value in (extra or {}).items()]
        sent = {"done": False}

        async def receive():
            if sent["done"]:
                return {"type": "http.disconnect"}
            sent["done"] = True
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
                        "path_params": {"path": path.removeprefix("/api/")}, "query_string": urlencode(query or {}).encode(),
                        "headers": headers, "scheme": "http", "server": ("localhost", 8080), "client": ("127.0.0.1", 1),
                        "http_version": "1.1"}, receive)

    def send(self, method, path, body=None, query=None, extra=None):
        return asyncio.run(self.api(self.request(method, "/api/" + path, body, query, extra)))

    def call(self, method, path, body=None, query=None, extra=None):
        """``(status_code, decoded JSON, response)`` of one request through the dispatcher."""
        result = self.send(method, path, body, query, extra)
        return result.status_code, json.loads(result.body) if result.body else None, result

    def operation(self, name, body=None, *, business=None):
        """``POST /api/root/operations/{name}`` (Global), or ``/api/biz/{business}/operations/{name}``."""
        prefix = f"biz/{business}/operations" if business else "root/operations"
        status, payload, _ = self.call("POST", f"{prefix}/{name}", body or {})
        return status, payload
