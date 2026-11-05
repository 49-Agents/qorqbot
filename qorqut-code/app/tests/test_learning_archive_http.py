"""Focused transport proof for the retained learning archive compatibility routes.

The core stream owns the domain contract for ``learnings_legacy_pending``/``learnings_review_get`` and its
reviewed changes are integrated separately. Until then a recording double represents that *target* contract
(bounded pages, three independent detail cursors, ``page_collections``) behind the real
``DashboardCompatibility`` router: these tests exercise the actual route dispatch, validation, scope forwarding
and ETag selection, not a domain mock of the router itself. No production fallback is added for the double.
"""
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from bazarlyk.transport.compatibility import DashboardCompatibility
from bazarlyk.transport.results import failure
from qorqut_operations.contracts import DomainError, OperationResult


GLOBAL = SimpleNamespace(business_id=None, principal_id="owner", principal_kind="human",
                         allowed_business_ids={"biz-1"})
PROJECT = SimpleNamespace(business_id="biz-1", principal_id="owner", principal_kind="human",
                          allowed_business_ids={"biz-1"})


def history_page(_arguments):
    return {"schema_version": 1, "items": [{"learning_id": "L1", "scope": "biz", "tag": "rule",
            "text": "historical record", "created": "2026-01-01T00:00:00Z", "work_state": "implemented",
            "decision": "accepted", "state": "implemented", "filed": True, "text_truncated": False,
            "reconciled_into": None}], "count": 1, "next_cursor": None, "read_only": True}


def review_page(_arguments):
    return {"item": {"id": "L1", "text": "historical record", "revision": 1},
            "sources": [{"run": "run-1", "session": "session-1", "status": "missing", "core": None}],
            "history": [{"id": "event-1", "event": "created"}],
            "next_evidence_cursor": None, "next_history_cursor": None,
            "evidence_complete": True, "references_complete": True, "history_complete": True,
            "evidence_status": "missing", "source_kind": "frozen_extraction_cores",
            "original_transcripts_included": False, "read_only": True,
            "page_collections": ["evidence", "history"], "complete": True, "next_content_cursor": None}


class RecordingApplication:
    """Records the operation call the router makes; the contract it returns is the target core output."""

    def __init__(self):
        self.calls = []
        self.error = None
        # Mutable current pages: the archive's conditional tag must follow the authorized bytes, not a counter.
        self.history_data = history_page({})
        self.review_data = review_page({})

    def invoke(self, context, operation, arguments=None, options=None):
        self.calls.append({"context": context, "operation": operation, "arguments": dict(arguments or {})})
        if self.error is not None:
            raise self.error
        if operation == "learnings_legacy_pending":
            return OperationResult.ok(self.history_data, request_id="fixture")
        if operation == "learnings_review_get":
            return OperationResult.ok(self.review_data, request_id="fixture")
        if operation == "learnings_get":
            return OperationResult.ok({"item": {"id": arguments["learning_id"]}}, request_id="fixture")
        raise AssertionError("Unexpected operation " + operation)


class ChangeFeed:
    """The real feed interface the router's ETag helper consumes, with a settable version."""

    nonce = "fixture-nonce"

    def __init__(self):
        self.versions = {"learnings": 0}

    def route_versions(self, kinds, scope):
        return {kind: self.versions.get(kind, 0) for kind in kinds}


class RootEndpoint:
    """The real HTTP transport's dispatch into the compatibility router, without its auth runtime."""

    def __init__(self, compatibility):
        self.compatibility = compatibility

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        parts = ["root", *[part for part in scope["path_params"]["path"].split("/") if part]]
        try:
            result = await self.compatibility.global_route(request, GLOBAL, parts)
        except DomainError as exc:
            result = failure(exc)
        await result(scope, receive, send)


class ProjectEndpoint:
    def __init__(self, compatibility):
        self.compatibility = compatibility

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        tail = [part for part in scope["path_params"]["path"].split("/") if part]
        try:
            result = await self.compatibility.business_route(request, PROJECT, {"id": PROJECT.business_id}, tail)
        except DomainError as exc:
            result = failure(exc)
        await result(scope, receive, send)


def fixture():
    application, changes = RecordingApplication(), ChangeFeed()
    runtime = SimpleNamespace(application=application, changes=changes)
    compatibility = DashboardCompatibility(runtime, local_files=SimpleNamespace())
    routes = [Route("/api/root/{path:path}", RootEndpoint(compatibility)),
              Route("/api/biz/{business}/{path:path}", ProjectEndpoint(compatibility))]
    return TestClient(Starlette(routes=routes)), application, changes


def call(client, application, path, **kwargs):
    application.calls.clear()
    response = client.get(path, **kwargs)
    return response, list(application.calls)


def test_global_and_project_history_forward_one_validated_bounded_page():
    client, application, _ = fixture()
    response, calls = call(client, application, "/api/root/learnings/history?limit=25&cursor=next")
    assert response.status_code == 200, response.text
    assert response.json() == history_page({})
    assert [(c["operation"], c["arguments"]) for c in calls] == [
        ("learnings_legacy_pending", {"limit": 25, "cursor": "next"})]
    assert calls[0]["context"] is GLOBAL

    response, calls = call(client, application, "/api/biz/biz-1/learnings/history")
    assert response.status_code == 200, response.text
    assert calls[0]["operation"] == "learnings_legacy_pending"
    assert calls[0]["arguments"] == {"limit": 50}, "the project route defaults to one bounded page"
    assert calls[0]["context"] is PROJECT


def test_literal_history_precedes_the_generic_candidate_route_and_factual_detail_is_retained():
    client, application, _ = fixture()
    response, calls = call(client, application, "/api/biz/biz-1/learnings/history")
    assert response.status_code == 200
    assert [c["operation"] for c in calls] == ["learnings_legacy_pending"], "history is never read as a candidate ID"

    response, calls = call(client, application, "/api/biz/biz-1/learnings/L1")
    assert response.status_code == 200, response.text
    assert [(c["operation"], c["arguments"]) for c in calls] == [("learnings_get", {"learning_id": "L1"})]
    assert response.json()["data"]["item"]["id"] == "L1"


def test_history_limit_and_cursor_are_validated_before_any_operation_runs():
    client, application, _ = fixture()
    for query in ("limit=0", "limit=201", "limit=abc", "limit=1.5", "cursor=", "cursor=" + "x" * 2001):
        response, calls = call(client, application, "/api/biz/biz-1/learnings/history?" + query)
        assert response.status_code == 422, (query, response.text)
        assert response.json()["error"]["code"] == "validation_error"
        assert calls == [], query


def test_review_route_forwards_each_independent_collection_cursor():
    client, application, _ = fixture()
    for path in ("/api/biz/biz-1/learnings/L1/review", "/api/root/learnings/L1/review"):
        response, calls = call(client, application, path)
        assert response.status_code == 200, response.text
        assert [(c["operation"], c["arguments"]) for c in calls] == [("learnings_review_get", {"learning_id": "L1"})]
        assert response.json() == review_page({})

    response, calls = call(client, application,
        "/api/biz/biz-1/learnings/L1/review?content_cursor=bytes&evidence_cursor=ev-next&history_cursor=hi-next")
    assert response.status_code == 200, response.text
    assert calls[0]["arguments"] == {"learning_id": "L1", "content_cursor": "bytes",
                                     "evidence_cursor": "ev-next", "history_cursor": "hi-next"}

    response, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review?content_cursor=")
    assert response.status_code == 422 and calls == []


def test_scope_and_read_authority_are_forwarded_not_reinterpreted():
    client, application, _ = fixture()
    application.error = DomainError("forbidden", "This learning is outside the authorized scope.")
    response, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "forbidden"
    assert calls[0]["context"] is PROJECT and calls[0]["context"].business_id == "biz-1"

    response, calls = call(client, application, "/api/root/learnings/L1/review")
    assert response.status_code == 403
    assert calls[0]["context"] is GLOBAL and calls[0]["context"].business_id is None


def test_cache_keys_include_scope_route_and_complete_cursor_selection():
    client, application, _ = fixture()
    first, _ = call(client, application, "/api/biz/biz-1/learnings/history")
    assert first.status_code == 200 and first.headers.get("cache-control") == "no-store"
    assert first.headers["etag"].startswith('W/"')
    tag = first.headers["etag"]
    global_tag_response, calls = call(client, application, "/api/root/learnings/history")
    assert global_tag_response.status_code == 200 and global_tag_response.headers["etag"] != tag, \
        "the root route never shares a project route's cache entry"
    changed_cursor, calls = call(client, application, "/api/biz/biz-1/learnings/history?cursor=next")
    assert changed_cursor.status_code == 200 and changed_cursor.headers["etag"] != tag, \
        "a different cursor is a different cache entry"
    changed_limit, calls = call(client, application, "/api/biz/biz-1/learnings/history?limit=25")
    assert changed_limit.headers["etag"] != tag, "a different limit is a different cache entry"
    detail, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review")
    detail_tag = detail.headers["etag"]
    evidence, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review?evidence_cursor=ev-next")
    history, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review?history_cursor=hi-next")
    assert len({detail_tag, evidence.headers["etag"], history.headers["etag"]}) == 3, \
        "each collection cursor selection gets its own detail cache entry"


def test_archive_conditional_reads_recheck_authority_scope_and_current_content():
    """A matching archive ETag must never bypass the read that proves authority and current source visibility."""
    client, application, _ = fixture()
    first, _ = call(client, application, "/api/biz/biz-1/learnings/L1/review")
    tag = first.headers["etag"]
    stable, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review", headers={"If-None-Match": tag})
    assert stable.status_code == 304
    assert len(calls) == 1, "a stable authorized page is validated before its 304"

    application.error = DomainError("forbidden", "Access was revoked")
    denied, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review", headers={"If-None-Match": tag})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"
    assert len(calls) == 1, "revoked access is rechecked and never reused as a 304"

    application.error = DomainError("validation_error", "Review cursor is invalid or the evidence changed")
    stale, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review?evidence_cursor=old",
                        headers={"If-None-Match": tag})
    assert stale.status_code == 422 and len(calls) == 1, "a stale cursor is denied by the domain read"

    application.error = None
    application.review_data["sources"] = []
    application.review_data["evidence_status"] = "missing"
    redacted, calls = call(client, application, "/api/biz/biz-1/learnings/L1/review", headers={"If-None-Match": tag})
    assert redacted.status_code == 200 and len(calls) == 1
    assert redacted.json()["sources"] == [], "changed/redacted source content is served, never a stale body"
    assert redacted.headers["etag"] != tag, "the tag follows the authorized bytes, not a learning counter"

    history_first, _ = call(client, application, "/api/biz/biz-1/learnings/history")
    history_tag = history_first.headers["etag"]
    stable_history, calls = call(client, application, "/api/biz/biz-1/learnings/history",
                                 headers={"If-None-Match": history_tag})
    assert stable_history.status_code == 304 and len(calls) == 1
    application.history_data["items"] = []
    application.history_data["count"] = 0
    redacted_history, calls = call(client, application, "/api/biz/biz-1/learnings/history",
                                   headers={"If-None-Match": history_tag})
    assert redacted_history.status_code == 200 and redacted_history.json()["items"] == []
    application.error = DomainError("forbidden", "Access was revoked")
    denied_history, calls = call(client, application, "/api/biz/biz-1/learnings/history",
                                 headers={"If-None-Match": redacted_history.headers["etag"]})
    assert denied_history.status_code == 403 and len(calls) == 1