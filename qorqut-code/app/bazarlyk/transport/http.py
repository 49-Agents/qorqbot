"""Same-origin owner routes and scoped hook routes over the real application."""

import json
from dataclasses import replace
import logging
import queue
import re
import time
import uuid
from contextlib import nullcontext
from functools import partial

import anyio
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, OperationResult,
                                         canonical_bytes, canonical_hash, new_id, plain)
from qorqut_operations import local_time
from qorqut_operations.storage.database import REQUEST_TIMING, RequestTiming
from qorqut_operations.storage.repositories import decode_row
from .auth import AuthenticatedPrincipal
from .mcp import split_options
from .results import failure, response

_UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class RouteCounters:
    """Process-lifetime (method, route-pattern) request counters for GET /api/root/perf.

    Mutated only from the event loop (never inside `anyio.to_thread.run_sync`),
    so plain dict access is safe without a lock.
    """
    def __init__(self):
        self._rows = {}

    def record(self, method, pattern, *, total_ms, db_ms, units, byte_count, etag=None):
        key = (method, pattern)
        row = self._rows.get(key)
        if row is None:
            row = self._rows[key] = {"method": method, "pattern": pattern, "count": 0,
                "total_ms": 0.0, "max_ms": 0.0, "db_ms": 0.0, "units": 0, "bytes": 0,
                "etag_hits": 0, "etag_misses": 0}
        row["count"] += 1
        row["total_ms"] += total_ms
        row["max_ms"] = max(row["max_ms"], total_ms)
        row["db_ms"] += db_ms
        row["units"] += units
        row["bytes"] += byte_count
        # None (most routes; no ETag declared) leaves both counters at 0 rather
        # than diluting the ratio with routes PR3 never wired.
        if etag == "hit":
            row["etag_hits"] += 1
        elif etag == "miss":
            row["etag_misses"] += 1

    def snapshot(self):
        return sorted(self._rows.values(), key=lambda row: (row["pattern"], row["method"]))

logger = logging.getLogger(__name__)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def reject_constant(value):
    raise ValueError("Nonfinite JSON number")


def check_body_length(request, limit):
    declared = request.headers.get("content-length")
    if declared is None:
        return
    if not declared.isdecimal():
        raise DomainError("invalid_request", "Content-Length must be a nonnegative integer")
    if int(declared) > limit:
        raise DomainError("payload_too_large", "Request body exceeds its bounded size")


async def read_json(request, *, limit=2 * 1024 * 1024):
    check_body_length(request, limit)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise DomainError("invalid_request", "This endpoint requires application/json")
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > limit:
            raise DomainError("payload_too_large", "Request body exceeds its bounded size")
    try:
        value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise DomainError("invalid_json", "Request body must be valid JSON") from None
    if not isinstance(value, dict):
        raise DomainError("invalid_request", "Request body must be an object")
    return value


def checked_fields(body, allowed, required=()):
    unknown, missing = set(body) - set(allowed), set(required) - set(body)
    if unknown or missing:
        raise DomainError("validation_error", "Request fields are invalid", field_errors={
            **{name: "unknown field" for name in sorted(unknown)}, **{name: "required" for name in sorted(missing)}})


def _etag_for(request, changes, kinds, scope):
    """Shared by `revalidated`/`revalidated_async` (handoff PR3, section 4.3).

    The ETag hashes the request's own path and query -- not just a
    caller-supplied label -- so two `?view=` variants of the same route
    never share a tag, plus `scope` and the declared `kinds`' versions from
    the in-memory change feed. Versions are read before any database
    access, so a 304 returns without the caller's real read ever running
    and opens no unit of work. `route_versions` (bazarlyk/service/changes.py)
    widens the scope lookup for kinds whose bump side cannot always
    attribute one business; skipping that would risk a stale 304, which
    this must never serve. A coarse kind can still cause an unrelated miss
    -- an accepted cost, not a correctness bug. Returns (None, False) if no
    change feed is attached (a bare `Runtime()`, e.g. in a test fixture that
    never wires one): there is nothing to revalidate against, so callers
    must treat that as an unconditional miss with no ETag issued, not guess.
    """
    if changes is None:
        return None, False
    from bazarlyk.service.changes import ANY_SCOPE
    versions = changes.route_versions(kinds, scope)
    # `changes.nonce` is minted once per ChangeFeed (a fresh value every
    # service start): version counters restart at zero on every restart, but
    # a browser's ETag cache does not, so without this a tag cached against a
    # previous process's version 0 could false-positive a 304 against this
    # process's own version 0, up until the next real write to that kind.
    tag = 'W/"' + canonical_hash({"path": request.url.path, "query": sorted(request.query_params.items()),
        "scope": "any" if scope is ANY_SCOPE else scope, "versions": sorted(versions.items()),
        "nonce": changes.nonce}) + '"'
    hit = request.headers.get("if-none-match") == tag
    request.scope["bazarlyk.etag"] = "hit" if hit else "miss"
    return tag, hit


def revalidated(request, changes, kinds, scope, produce):
    """For a route whose own handler is a plain sync function (compatibility.py
    routes, offloaded to a worker thread in one piece by their caller)."""
    tag, hit = _etag_for(request, changes, kinds, scope)
    if hit:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-store"})
    result = produce()
    # An ETag is revalidation-only (handoff 4.3): compatibility.py's plain
    # JSONResponse(...) returns do not set Cache-Control themselves the way
    # transport/results.py's response() does, so this must set it explicitly
    # rather than assume the body is already marked no-store.
    result.headers["Cache-Control"] = "no-store"
    if tag is not None:
        result.headers["ETag"] = tag
    return result


async def revalidated_async(request, changes, kinds, scope, produce):
    """For a route whose own handler is `async def` and awaits its own
    `anyio.to_thread.run_sync` calls (this module's routes), where `produce`
    is an async callable rather than a plain one."""
    tag, hit = _etag_for(request, changes, kinds, scope)
    if hit:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-store"})
    result = await produce()
    # Every current caller's produce() already goes through transport/results.py's
    # response(), which sets this -- set it here too so the guarantee belongs to
    # this helper's own contract rather than to caller discipline.
    result.headers["Cache-Control"] = "no-store"
    if tag is not None:
        result.headers["ETag"] = tag
    return result


class HTTPAPI:
    def __init__(self, runtime, authentication, origins, *, compatibility=None, terminals=None):
        self.runtime, self.authentication, self.origins = runtime, authentication, origins
        self.compatibility, self.terminals = compatibility, terminals
        self.route_counters = RouteCounters()
        # The selected extensions' routes are exact keys on the frozen selection; a route grants no authority, the
        # dispatch below still applies its own owner, bearer and origin checks before any handler is called.
        selection = runtime.selection
        self.statuses = selection.error_statuses
        self.guards = selection.operation_guards()
        self.owner_routes, self.integration_routes, self.agent_routes = selection.routes()

    def context(self, actor, slug=None, *, owner=False):
        with self.runtime.database.read() as uow:
            business = uow.connection.execute("SELECT * FROM businesses WHERE slug=? AND archived_at IS NULL", (slug,)).fetchone() if slug else None
            if slug and business is None:
                raise DomainError("not_found", "Project is not available to this principal")
            business_id = business["id"] if business else None
            if business_id:
                membership = uow.repository("business_memberships", business_id=business_id).get(actor.principal.id)
                if not membership or not membership["active"]:
                    raise DomainError("not_found", "Project is not available to this principal")
                if owner and not membership["is_owner"]:
                    raise DomainError("forbidden", "This action requires the current project owner")
            context = self.runtime.policy.context_for(actor.principal.id, business_id=business_id,
                review_business_id=business_id, uow=uow)
        return context, decode_row(business)

    def session_data(self, actor):
        with self.runtime.database.read() as uow:
            installation = uow.repository("installation").get("service")
            global_owner = bool(installation and installation["owner_principal_id"] == actor.principal.id)
            owned = uow.connection.execute("SELECT 1 FROM business_memberships WHERE principal_id=? AND active=1 AND is_owner=1 LIMIT 1", (actor.principal.id,)).fetchone()
        return {"principal_id": actor.principal.id, "principal_kind": "human", "display_name": actor.principal.display_name,
                "is_owner": bool(owned) or global_owner, "is_installation_owner": global_owner,
                "operational_timezone": local_time.TIMEZONE}

    def _report_transport_failure(self, request, operation, *, context=None):
        reporter = getattr(getattr(self.runtime, "incidents", None), "reporter", None)
        if reporter is None:
            reporter = getattr(getattr(self.runtime, "application", None), "incident_reporter", None)
        if reporter is None:
            return
        actor = request.scope.get("bazarlyk.auth")
        principal = getattr(actor, "principal", None)
        try:
            reporter.transport_failure(component="http", operation=operation,
                request_id=request.scope.get("bazarlyk.request_id"), context=context,
                agent_id=(principal.id if getattr(principal, "kind", None) == "agent" else None),
                session_id=None)
        except Exception:
            # Reporting must never replace or recurse into the transport error.
            pass

    def _report_transport_success(self, request, operation):
        reporter = getattr(getattr(self.runtime, "incidents", None), "reporter", None)
        if reporter is None:
            reporter = getattr(getattr(self.runtime, "application", None), "incident_reporter", None)
        if reporter is None:
            return
        actor = request.scope.get("bazarlyk.auth")
        principal = getattr(actor, "principal", None)
        try:
            reporter.transport_success(component="http", operation=operation,
                request_id=request.scope.get("bazarlyk.request_id"),
                agent_id=(principal.id if getattr(principal, "kind", None) == "agent" else None))
        except Exception:
            pass

    @staticmethod
    def _transport_operation(request):
        parts = request.path_params.get("path", "").strip("/").split("/")
        if parts[:1] == ["agent"]:
            return "agent." + parts[1] if len(parts) > 1 and parts[1] in {
                "readiness", "bootstrap", "browser", "focus", "inbox", "checkpoint", "technical-observation"} else "agent"
        if parts[:2] == ["root", "operations"]:
            return "root.operation"
        if parts[:1] == ["biz"]:
            return "business.operation" if "operations" in parts[2:] else "business"
        return parts[0] if parts and parts[0] in {"session", "me", "root"} else "request"

    # Every generated record id (tasks, learnings, jobs, signoffs, documents,
    # routines, ...) is a uuid4 from contracts.new_id(), so `_UUID_RE`
    # already collapses those regardless of nesting depth. What it cannot
    # collapse is authored, non-uuid, free-form text embedded in a path: an
    # asset's real file path (compatibility.py's `["asset", *path_parts]`
    # branch, unbounded in both length and content) and a context/person
    # slug (`context/person/{slug}`, derived from a person's real name).
    # Those can start as early as the segment right after "biz"/"{slug}".
    #
    # The deepest route built entirely from literal, fixed keywords (no
    # authored or generated value at all) is 4 segments long, e.g.
    # /api/biz/{slug}/context/file|search|map or /api/biz/{slug}/assets/upload
    # (see business_get/business_post/document_get/document_post in
    # compatibility.py). Every other real endpoint either fits within that
    # same 4-segment envelope before an id/slug appears, or extends past it
    # only through a uuid (already templated) or authored content (asset
    # paths, context/person slugs, and terminal/agent name selectors, which
    # accept either the agent's uuid or its plain registered name). Capping
    # at 4 keeps every literal-only route distinct while forcing any
    # authored tail of any length beyond it into one placeholder segment.
    _ROUTE_SEGMENT_CAP = 4

    @staticmethod
    def _route_pattern(parts):
        # Route pattern means the template ("/api/biz/{slug}/notes"), not the
        # concrete slug: template the one known dynamic segment (a business
        # slug right after "biz") plus any UUID-shaped segment anywhere, then
        # collapse anything past `_ROUTE_SEGMENT_CAP` into a single "…" entry
        # so an authored/free-form tail of any length is one bounded row
        # instead of a permanent counter per distinct value ever seen.
        capped = parts[:HTTPAPI._ROUTE_SEGMENT_CAP]
        templated = ["{slug}" if index == 1 and parts[:1] == ["biz"] else ("{id}" if _UUID_RE.fullmatch(part) else part)
            for index, part in enumerate(capped)]
        if len(parts) > HTTPAPI._ROUTE_SEGMENT_CAP:
            templated.append("…")
        return "/api/" + "/".join(templated)

    async def __call__(self, request):
        result = await self._dispatch(request)
        timing = REQUEST_TIMING.get()
        total_ms = (time.monotonic() - timing.started) * 1000 if timing is not None else 0.0
        db_ms = timing.db_seconds * 1000 if timing is not None else 0.0
        units = timing.units if timing is not None else 0
        # The wait/read/write split comes from the paired Operations component. An older build lacks those fields and
        # reports zeros here rather than failing the request. All of them are sums over units (see RequestTiming).
        db_wait_ms = getattr(timing, "db_wait_seconds", 0.0) * 1000 if timing is not None else 0.0
        db_read_ms = getattr(timing, "db_read_seconds", 0.0) * 1000 if timing is not None else 0.0
        db_write_ms = getattr(timing, "db_write_seconds", 0.0) * 1000 if timing is not None else 0.0
        read_units = getattr(timing, "read_units", 0) if timing is not None else 0
        write_units = getattr(timing, "write_units", 0) if timing is not None else 0
        byte_count = len(getattr(result, "body", None) or b"")
        request.scope["bazarlyk.total_ms"] = total_ms
        request.scope["bazarlyk.db_ms"] = db_ms
        request.scope["bazarlyk.units"] = units
        request.scope["bazarlyk.db_wait_ms"] = db_wait_ms
        request.scope["bazarlyk.db_read_ms"] = db_read_ms
        request.scope["bazarlyk.db_write_ms"] = db_write_ms
        request.scope["bazarlyk.read_units"] = read_units
        request.scope["bazarlyk.write_units"] = write_units
        request.scope["bazarlyk.response_bytes"] = byte_count
        parts = request.path_params.get("path", "").strip("/").split("/")
        self.route_counters.record(request.method, self._route_pattern(parts),
            total_ms=total_ms, db_ms=db_ms, units=units, byte_count=byte_count,
            etag=request.scope.get("bazarlyk.etag"))
        if (request.scope.get("bazarlyk.transport_operation") != "agent.technical-observation"
                and not request.scope.get("bazarlyk.transport_failed")
                and getattr(result, "status_code", 500) < 400):
            self._report_transport_success(request, request.scope.get("bazarlyk.transport_operation", "request"))
        return result

    async def _dispatch(self, request):
        request_id = new_id()
        request.scope["bazarlyk.request_id"] = request_id
        request.scope["bazarlyk.transport_operation"] = self._transport_operation(request)
        # Each HTTP request is its own asyncio Task with an isolated context,
        # so this never needs a matching reset.
        REQUEST_TIMING.set(RequestTiming())
        try:
            self.origins.validate(request)
            parts = request.path_params.get("path", "").strip("/").split("/")
            if "token" in request.query_params:
                raise DomainError("invalid_request", "Query credentials are accepted only by the dashboard bootstrap page")
            if parts[:1] == ["agent"]:
                return await self.agent_route(request, parts)
            if parts[:1] == ["integrations"]:
                return await self.integration_route(request, parts)
            # This private, single-owner dashboard opens directly as its owner.
            # Agent bootstrap above and MCP retain their separate scoped identities.
            authorization = request.headers.get("authorization")
            credential_kind = getattr(self.authentication, "credential_kind", None)
            if (authorization and credential_kind is not None
                    and await anyio.to_thread.run_sync(lambda: credential_kind(authorization)) == "integration"):
                raise DomainError("forbidden", "Integration credentials are accepted only on the integration routes")
            actor = await anyio.to_thread.run_sync(lambda: AuthenticatedPrincipal(self.authentication.installation_owner()[0]))
            if request.method not in {"GET", "HEAD"}:
                self.origins.validate(request, mutation=True)
            if parts == ["session"] and request.method in {"GET", "POST"}:
                return response(OperationResult.ok(self.session_data(actor), request_id=request_id))
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            if parts == ["root", "extensions"] and request.method == "GET":
                # The enabled UI assets of the selected extensions only: no configuration, path or secret.
                return response(OperationResult.ok(self.runtime.selection.shell_assets(), request_id=request_id))
            owner_route = self.owner_routes.get((request.method, tuple(parts)))
            if owner_route is not None:
                return await owner_route(self, request, actor, request_id)
            if parts == ["root", "attention-queue"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Attention queue is for the owner's dashboard")
                from bazarlyk.attention_queue import queue
                context, _ = self.context(actor)
                body = await read_json(request, limit=131072) if request.method == "POST" else None
                data = await anyio.to_thread.run_sync(partial(queue, self.runtime, context, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "reply-notifications"] and request.method == "GET":
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Reply notifications are for the owner's dashboard")
                from bazarlyk.reply_notifications import replies
                context, _ = self.context(actor)
                after = request.query_params.get("communications_after")
                if after is not None:
                    if not after.isdigit():
                        raise DomainError("validation_error", "communications_after must be a non-negative integer")
                    after = int(after)
                data = await anyio.to_thread.run_sync(partial(replies, self.runtime, context, True, after))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "onboarding"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Walkthrough preferences are for the owner's dashboard")
                from bazarlyk.onboarding_preferences import progress
                body = await read_json(request, limit=1024) if request.method == "POST" else None
                if request.method == "POST" and not isinstance(body, dict):
                    raise DomainError("validation_error", "Walkthrough preferences must be an object")
                data = await anyio.to_thread.run_sync(partial(progress, self.runtime.paths.runtime, actor.principal.id, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "agent-trust"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Agent trust consent is for the owner's dashboard")
                context, _ = self.context(actor)
                with self.runtime.database.read() as uow:
                    owner = uow.repository("installation").get("service")
                if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
                    raise DomainError("forbidden", "Only the installation owner can change agent trust")
                from bazarlyk.agent_trust import owner_view
                body = await read_json(request, limit=1024) if request.method == "POST" else None
                if request.method == "POST" and not isinstance(body, dict):
                    raise DomainError("validation_error", "Agent trust consent must be an object")
                agent_id = request.query_params.get("agent_id")
                data = await anyio.to_thread.run_sync(partial(owner_view, self.runtime, body, agent_id))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts in (["root", "about"], ["root", "agent-clients"]) and request.method == "GET":
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Settings details are for the owner's dashboard")
                context, _ = self.context(actor)
                with self.runtime.database.read() as uow:
                    owner = uow.repository("installation").get("service")
                if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
                    raise DomainError("forbidden", "Only the installation owner can read these settings")
                from bazarlyk import owner_settings
                reader = owner_settings.about_view if parts[1] == "about" else owner_settings.agent_clients_view
                data = await anyio.to_thread.run_sync(reader)
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "connectors"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Connectors are for the owner's dashboard")
                context, _ = self.context(actor)
                with self.runtime.database.read() as uow:
                    owner = uow.repository("installation").get("service")
                if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
                    raise DomainError("forbidden", "Only the installation owner can change connectors")
                from bazarlyk.connectors import owner_view as connectors_view
                body = await read_json(request, limit=1024) if request.method == "POST" else None
                if request.method == "POST" and not isinstance(body, dict):
                    raise DomainError("validation_error", "A connector change must be an object")
                data = await anyio.to_thread.run_sync(partial(connectors_view, self.runtime, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "agent-pins"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Agent pins are for the owner's dashboard")
                from bazarlyk.dashboard_preferences import pins
                body = await read_json(request, limit=65536) if request.method == "POST" else None
                if body is not None and not isinstance(body, dict):
                    raise DomainError("validation_error", "Pin preferences must be an object")
                data = await anyio.to_thread.run_sync(partial(pins, self.runtime.paths.runtime, actor.principal.id, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "testing-mode"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Testing mode is for the owner's dashboard")
                from bazarlyk.dashboard_preferences import testing_mode
                body = await read_json(request, limit=65536) if request.method == "POST" else None
                if body is not None and not isinstance(body, dict):
                    raise DomainError("validation_error", "Testing mode must be an object")
                data = await anyio.to_thread.run_sync(partial(testing_mode, self.runtime.paths.runtime, actor.principal.id, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            if parts == ["root", "calendar-statuses"] and request.method in {"GET", "POST"}:
                if request.headers.get("authorization") is not None:
                    raise DomainError("forbidden", "Calendar statuses are for the owner's dashboard")
                from bazarlyk.calendar.preferences import statuses
                context, _ = self.context(actor)
                body = await read_json(request, limit=131072) if request.method == "POST" else None
                data = await anyio.to_thread.run_sync(partial(statuses, self.runtime, context, body))
                result = response(OperationResult.ok(data, request_id=request_id))
                result.headers["Cache-Control"] = "private, no-store"
                return result
            from bazarlyk.overseer import http as overseer_http
            if overseer_http.matches(parts):
                if request.headers.get('authorization') is not None:
                    raise DomainError('forbidden', 'Overseer reads require the owner dashboard identity')
                context, _ = self.context(actor)
                return await overseer_http.route(self.runtime, request, context, parts, request_id)
            from bazarlyk.native_chat import http as native_chat_http
            if native_chat_http.matches(parts) and self.runtime.native_chat is not None:
                return await native_chat_http.route(self.runtime, request, actor.principal.id, parts, request_id)
            if parts[:1] == ["root"] and parts[1:2] in (["agents"], ["terminal"], ["terminals"], ["workspace-terminal"]):
                context, _ = self.context(actor)
                if parts == ["root", "agents"] and request.method == "GET":
                    result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "agents_list", {"scope": "global"}))
                    return response(result)
                if len(parts) == 4 and parts[1] == "agents" and parts[3] == "viewbox" and request.method == "GET":
                    return await self.viewbox_board(request, context, parts[2], request_id)
                if len(parts) == 5 and parts[1] == "agents" and parts[3:] == ["viewbox", "dismiss"] and request.method == "POST":
                    return await self.viewbox_dismiss(request, context, parts[2], request_id)
                if len(parts) == 4 and parts[1] == "agents" and parts[3] == "focus-layout" and request.method in {"GET", "POST"}:
                    return await self.focus_layout(request, context, parts[2], request_id)
                if len(parts) == 4 and parts[1] == "agents" and parts[3] == "browser-surfaces" and request.method == "GET" and self.compatibility:
                    data = await anyio.to_thread.run_sync(lambda: self.compatibility.local_files.browser_surfaces(context, parts[2]))
                    return JSONResponse(data, headers={"Cache-Control": "no-store"})
                if self.terminals and parts[1:2] in (["terminal"], ["terminals"], ["workspace-terminal"]):
                    return await self.terminals.route(request, context, None, parts[1:])
                raise DomainError("not_found", "Global agent endpoint is not registered")
            if parts[:2] == ['root','release'] and len(parts)==3:
                if request.headers.get('authorization') is not None:
                    raise DomainError('forbidden','Release control requires the installation owner dashboard identity')
                from bazarlyk.release_worker.continuity import snapshot
                if parts[-1]=='status' and request.method=='GET':
                    data=await anyio.to_thread.run_sync(snapshot,self.runtime)
                    return response(OperationResult.ok(data,request_id=new_id()))
                if parts[-1] in {'drain','resume'} and request.method=='POST':
                    body=await read_json(request,limit=8192)
                    checked_fields(body,{'timeout'},{'timeout'})
                    if type(body['timeout']) not in (int,float) or not 0<body['timeout']<=5:
                        raise DomainError('validation_error','Drain budget must be at most five seconds')
                    admission=self.runtime.application.writer_admission
                    if admission is None:
                        raise DomainError('operation_unavailable','Application writer admission is unavailable')
                    if parts[-1]=='resume':admission.open()
                    else:
                        try:
                            await anyio.to_thread.run_sync(partial(admission.close_and_wait,timeout=body['timeout']))
                        except TimeoutError:
                            admission.open()
                            raise DomainError('busy','Application writers did not drain',retryable=True)
                    return response(OperationResult.ok({'drained':parts[-1]=='drain'},request_id=new_id()))
            if parts == ["root", "perf"] and request.method == "GET":
                # Dashboard-only observability, not a registered operation: never exposed to MCP.
                return response(OperationResult.ok({"routes": self.route_counters.snapshot()}, request_id=request_id))
            if parts[:2] == ["root", "files"]:
                return await self.files_route(request, actor, parts[2:])
            if parts == ["root", "events"] and request.method == "GET":
                if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
                    raise DomainError("csrf_required", "The change feed stream must originate in this dashboard")
                return self._events_stream(request)
            if parts == ["root", "businesses", "archived"] and request.method == "GET":
                context, _ = self.context(actor)
                result = await anyio.to_thread.run_sync(
                    lambda: self.runtime.application.invoke(context, "business_archived_list", {}))
                return response(result)
            if parts == ["root", "business-restore"] and request.method == "POST":
                context, _ = self.context(actor)
                args, options = split_options(await read_json(request), domain_fields=("business_id",))
                if options.business_id is not None or options.review_business_id is not None:
                    raise DomainError("forbidden", "This endpoint requires Global scope")
                result = await anyio.to_thread.run_sync(lambda: self.runtime.domains.archive.restore(context, args, options))
                return response(result)
            if len(parts) == 3 and parts[:2] == ["root", "operations"] and parts[2] in {
                    "broadcast_recipients", "broadcasts_list", "broadcasts_get", "broadcasts_create", "broadcasts_update",
                    "broadcasts_pause", "broadcasts_resume", "broadcasts_cancel", "broadcasts_send_now",
                    "dev_tickets_list", "dev_tickets_status_set", "document_history_get", "files_link",
                    "management_coordinator_set", "management_list", "management_get", "management_resolve",
                    "system_incidents_list", "system_incidents_get",
                    "dashboard_summary_get", "global_progress_get",
                    "actions_record", "actions_list", "actions_get", "actions_correct", "actions_withdraw", "actions_config_get", "working_memory_get", "working_memory_update", "agent_context_get",
                    "costs_overview",
                    "calendar_calendars_list", "calendar_events_list", "calendar_event_get",
                    "calendar_event_create", "calendar_event_update", "calendar_event_delete", "calendar_event_respond",
                    "owner_reminder", "reminders_create", "reminders_get", "reminders_list", "reminders_update", "reminders_snooze",
                    "reminders_dismiss", "reminders_cancel",
                    "focus_get", "focus_present", "focus_edit", "focus_move", "focus_set_archived", "focus_reply", "signoffs_comment", "signoffs_notes_list", "tasks_thread_get", "crm_activity_list", "crm_activity_record",
                    "attention_get", "attention_add", "attention_remove", "todo_get", "todo_add", "todo_remove", "workers_list",
                    "workspace_types", "workspace_get",
                    "owner_conversation_get", "owner_conversation_send", "owner_conversation_mark_read", "owner_conversations_list", "owner_conversation_related", "owner_conversation_peers",
                    # Project record reads and explicit links: the six project_* operations and the narrow reminder read.
                    # The root route names the Project with scope=global and no business option; a business route must agree
                    # with project_id, and the engine refuses any target differing from the selected project.
                    "project_record_get", "project_items_list", "project_items_candidates", "project_item_get",
                    "project_items_link", "project_items_unlink", "owner_reminders_list",
                    # Move to project: the source is the explicit source_business_id argument (null for Global), so the root
                    # route is allowed and the business route must agree with it. No scope selector is accepted.
                    "tasks_move_targets", "tasks_move_to_project_preview", "tasks_move_to_project",
                    # Parent-detail readers promised to the record pane: a routine's runs and a known job's own safe
                    # result. Root is Global-source only; the business route reads its Project source. Both are normal
                    # reads through split_options, argument validation and policy, with no job browser or enumeration.
                    "routines_runs", "jobs_get",
                    # Global work: the eight neutral relationship operations that take the business-or-Global namespace, and the
                    # Task follow and Action discussion/follow and shared-tag operations. Each is the normal application call; Tags and
                    # Actions declare no scope input, so a caller passes none.
                    "relationship_roles_list", "relationships_list", "relationships_get", "relationships_create",
                    "relationships_update", "relationships_remove", "context_items_list", "context_items_get",
                    "tasks_follow", "actions_thread_get", "actions_follow", "actions_comment", "tags_list", "tags_create",
                    "tasks_list", "tasks_get", "tasks_notes_list", "tasks_associated_records", "agents_activity_list", "agent_work_update", "tasks_create", "tasks_update", "tasks_assign", "tasks_comment", "tasks_complete", "tasks_config_get", "tasks_config_update", "assignees_list",
                    "business_create",
                    "context_home", "context_object", "context_expand", "context_find", "context_changes", "context_upload_map", "context_filings", "context_filing_costs", "context_instruct", "context_edit",
                    # Raw Global sources (scope=shared) and the exact-input and document commands of the source status UI: only these eight.
                    "context_list", "context_get", "context_search", "context_retry", "context_retract",
                    "context_queue_get", "context_queue_retry", "context_queue_discard",
                    # The record panel's Global note reader: the existing owner-only READ, which declares only ``document_id`` (the
                    # route supplies the scope). No note write and no scope input is added here.
                    "notes_get"} and request.method == "POST":
                context, _ = self.context(actor)
                # workspace_get is the one owner cross-scope read that names its
                # target business as a domain field, not a scope selector.
                domain_fields = ("business_id",) if parts[2] == "workspace_get" else ()
                args, options = split_options(await read_json(request), domain_fields=domain_fields)
                if options.business_id is not None or options.review_business_id is not None:
                    raise DomainError("forbidden", "This endpoint requires Global scope")
                if parts[2] in {"routines_runs", "jobs_get"} and args.get("scope") != "global":
                    raise DomainError("forbidden", "Global routines and jobs are read with scope=global")
                result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, parts[2], args, options))
                return response(result)
            if parts == ["root", "operations", "agent_terminal_start"] and request.method == "POST":
                # Chat's Start button: the owner's own start of one Global agent, through the normal operation, policy and
                # durable terminal dispatch. Only a single target is accepted here; batches stay with terminal_command_issue.
                context, _ = self.context(actor)
                args, options = split_options(await read_json(request, limit=4096))
                checked_fields(args, {"target_agent_id", "rationale"}, {"target_agent_id", "rationale"})
                if options.business_id is not None or options.review_business_id is not None:
                    raise DomainError("forbidden", "This endpoint requires Global scope")
                result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, parts[2], args, options))
                return response(result)
            if len(parts) == 3 and parts[:2] == ["root", "operations"] and parts[2] in {
                    "agents_message_send", "agents_messages_list", "agents_message_recipients"} and request.method == "POST":
                context, _ = self.context(actor)
                args, options = split_options(await read_json(request))
                if options.business_id is not None or args.get("scope", "global") != "global":
                    raise DomainError("forbidden", "Global message endpoint cannot select a project")
                args["scope"] = "global"
                result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, parts[2], args, options))
                return response(result)
            if len(parts) == 3 and parts[:2] == ["root", "operations"] and parts[2] in {
                    "tables_list", "tables_get", "tables_rows_list", "tables_history", "tables_create", "tables_update",
                    "tables_rows_write", "tables_history_retry", "tables_archive",
                    # CRM memberships and the Table team discussion serve the Global Table namespace through the same route.
                    "crm_entries_list", "crm_entries_add", "crm_entries_update", "crm_entries_remove",
                    "tables_thread_get", "tables_comment", "tables_follow"} and request.method == "POST":
                context, _ = self.context(actor)
                args, options = split_options(await read_json(request))
                if options.business_id is not None or args.get("scope", "global") != "global":
                    raise DomainError("forbidden", "Global tables cannot select a project")
                args["scope"] = "global"
                result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, parts[2], args, options))
                return response(result)
            if parts == ["me", "agents", "activity"] and request.method == "GET":
                context, _ = self.context(actor)
                from bazarlyk.transport.compatibility import integer_parameter
                args = dict(request.query_params)
                checked_fields(args, {"limit", "cursor", "agent_id"}, set())
                args["limit"] = integer_parameter(args.get("limit", "100"), "limit", 1, 200)
                args["scope"] = "global"
                result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "agents_activity_list", args))
                return response(result)
            if len(parts) == 6 and parts[:2] == ["me", "agents"] and parts[3:5] == ["focus", "documents"] and request.method == "GET":
                context, _ = self.context(actor)
                data = await anyio.to_thread.run_sync(partial(self.runtime.focus.owner_document, context, parts[2], parts[5]))
                return response(OperationResult.ok(data, request_id=request_id))
            if parts[:2] == ["me", "agents"] and len(parts) in {4, 5} and parts[3] == "focus":
                context, _ = self.context(actor)
                if len(parts) == 4 and request.method == "GET":
                    data = await anyio.to_thread.run_sync(partial(self.runtime.focus.owner_snapshot, context, parts[2]))
                elif len(parts) == 5 and parts[4] == "done" and request.method == "POST":
                    body = await read_json(request, limit=32768)
                    checked_fields(body, {"request_id", "note"}, {"request_id"})
                    data = await anyio.to_thread.run_sync(partial(self.runtime.focus.owner_done, context, parts[2], **body))
                else:
                    raise DomainError('not_found', 'Focus endpoint is not registered')
                return response(OperationResult.ok(data, request_id=request_id))
            if len(parts) == 6 and parts[:2] == ["me", "agents"] and parts[3:5] == ["inbox", "documents"] and request.method == "GET":
                context, _ = self.context(actor)
                data = await anyio.to_thread.run_sync(partial(self.runtime.inbox.owner_document, context, parts[2], parts[5]))
                return response(OperationResult.ok(data, request_id=request_id))
            if len(parts) == 4 and parts[:2] == ["me", "agents"] and parts[3] == "inbox" and request.method == "GET":
                context, _ = self.context(actor)
                try:
                    after = int(request.query_params.get("after", "0"))
                    if after < 0: raise ValueError
                except ValueError:
                    raise DomainError("validation_error", "Inbox cursor must be a nonnegative integer")
                data = await anyio.to_thread.run_sync(partial(self.runtime.inbox.owner_snapshot, context, parts[2], after=after))
                return response(OperationResult.ok(data, request_id=request_id))
            if parts[:2] == ["me", "attention"]:
                context, _ = self.context(actor)
                notifications = self.runtime.signoffs.notifications
                if request.method == "GET" and parts in (["me", "attention"], ["me", "attention", "deliveries"]):
                    # This aggregates across every business the owner belongs
                    # to, a set an ETag computed before any read cannot know,
                    # so ANY_SCOPE checks every known scope instead of one.
                    from bazarlyk.service.changes import ANY_SCOPE
                    all_items = request.query_params.get("all_items", "false").lower() == "true"
                    method = notifications.overview if parts == ["me", "attention"] else notifications.deliveries
                    async def produce():
                        data = await anyio.to_thread.run_sync(partial(method, context, all_items=all_items))
                        return response(OperationResult.ok(data, request_id=request_id))
                    return await revalidated_async(request, self.runtime.changes, {"tasks", "signoffs", "agents"}, ANY_SCOPE, produce)
                elif request.method == "POST" and len(parts) == 5 and parts[2] == "deliveries" and parts[4] == "dismiss":
                    body = await read_json(request)
                    checked_fields(body, set())
                    data = await anyio.to_thread.run_sync(partial(notifications.dismiss, context, parts[3]))
                elif request.method == "POST" and len(parts) == 5 and parts[2] == "signoffs" and parts[4] == "snooze":
                    body = await read_json(request)
                    checked_fields(body, {"until"}, {"until"})
                    data = await anyio.to_thread.run_sync(partial(notifications.snooze, context, parts[3], body["until"]))
                else:
                    raise DomainError("not_found", "Attention endpoint is not registered")
                return response(OperationResult.ok(data, request_id=request_id))
            if parts[:2] == ["me", "management"] and request.method == "GET":
                context, _ = self.context(actor)
                if len(parts) == 2:
                    args = {"scope": "global", "all_items": True}
                    result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "management_list", args))
                elif len(parts) == 3:
                    args = {"scope": "global", "routing_id": parts[2]}
                    result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "management_get", args))
                else:
                    raise DomainError("not_found", "Management endpoint is not registered")
                return response(result)
            if parts[:2] == ["root", "signoffs"]:
                context, _ = self.context(actor)
                return await self.signoff_route(request, context, parts[1:])
            if parts[:1] == ["biz"] and len(parts) >= 2:
                context, business = await anyio.to_thread.run_sync(lambda: self.context(actor, parts[1], owner=request.method != "GET"))
                tail = parts[2:]
                if len(tail) == 3 and tail[0] == "data-uploads" and tail[2] == "download" and request.method == "GET":
                    return await self.data_upload_download(context, tail[1])
                if tail[:1] == ["signoffs"]:
                    return await self.signoff_route(request, context, tail)
                if len(tail) == 3 and tail[0] == "agents" and tail[2] in {"config", "permissions"}:
                    if tail[2] == "config" and request.method == "GET":
                        config = await anyio.to_thread.run_sync(lambda: self.runtime.agents.owner_config(context, tail[1]))
                        return response(OperationResult.ok({"effective_config": config}, request_id=context.request_id))
                    if tail[2] == "permissions" and request.method == "POST":
                        body = await read_json(request)
                        checked_fields(body, {"expected_revision", "idempotency_key", "overrides"}, {"expected_revision", "idempotency_key", "overrides"})
                        options = InvocationOptions(expected_revision=body["expected_revision"], idempotency_key=body["idempotency_key"])
                        result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "permissions_set", {"agent_id": tail[1], "overrides": body["overrides"]}, options))
                        return response(result)
                if len(tail) == 2 and tail[0] == "operations" and request.method == "POST":
                    body = await read_json(request, limit=28 * 1024 * 1024 if tail[1] == "data_uploads_upload" else 2 * 1024 * 1024)
                    args, options = split_options(body)
                    for guard in self.guards:
                        if guard.names(tail[1]):
                            options = guard.check(request, tail[1], args, options)
                    if options.business_id is not None and options.business_id != context.business_id:
                        raise DomainError("forbidden", "The body cannot change the URL's selected project")
                    if tail[1] in {"routines_runs", "jobs_get"} and args.get("scope") == "global":
                        raise DomainError("forbidden", "Global routines and jobs are read from the root route")
                    result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, tail[1], args, options))
                    return response(result, statuses=self.statuses)
                if tail[:1] == ["chat"]:
                    raise DomainError("legacy_chat_retired", "Project chat execution is retired; use the registered Claude Code terminal")
                if tail[:1] in (["terminal"], ["terminals"]) and self.terminals:
                    return await self.terminals.route(request, context, business, tail)
                if self.compatibility:
                    return await self.compatibility.business_route(request, context, business, tail)
            elif self.compatibility:
                context, _ = self.context(actor)
                return await self.compatibility.global_route(request, context, parts)
            raise DomainError("not_found", "Endpoint is not registered")
        except DomainError as exc:
            return failure(exc, request_id, statuses=self.statuses)
        except Exception as exc:
            request.scope["bazarlyk.transport_failed"] = True
            if (request.scope.get("bazarlyk.transport_operation") != "agent.technical-observation"
                    and not getattr(exc, "_qorqut_incident_reported", False)):
                self._report_transport_failure(request, request.scope["bazarlyk.transport_operation"])
            logger.exception("HTTP request failed: %s", request_id)
            return failure(DomainError("internal_error", "The request failed; use its correlation ID for diagnosis"), request_id)

    async def files_route(self, request, actor, tail):
        """The owner's file viewer reads: ``metadata``, ``content`` (preview), ``download`` and saved ``history``.

        Reached only after the Host/Origin checks, by the private tokenless owner dashboard, which acts as the
        installation owner. ``credential_kind`` only classifies a stored token (a revoked or another person's human
        credential included), so it cannot authorize here: any explicit ``Authorization`` header, human, agent,
        integration or invalid, is refused before the database or disk is touched. Never a registered operation, so
        it is not exposed to MCP; GET only reads, records no history and creates no document ID. Every answer is
        ``Cache-Control: private, no-store``; a download is always an attachment of octet-stream bytes streamed from
        the opened file, never served as same-origin HTML.
        """
        if request.headers.get("authorization") is not None:
            raise DomainError("forbidden", "The file viewer is for the owner's dashboard and takes no bearer credential")
        if request.method != "GET" or len(tail) != 1 or tail[0] not in {"metadata", "content", "download", "history", "image"}:
            raise DomainError("not_found", "File endpoint is not registered")
        from bazarlyk.file_viewer import service as file_service
        from bazarlyk.file_viewer.paths import reject_bad_escapes
        reject_bad_escapes(request.scope.get("query_string", b""))
        items, action = request.query_params.multi_items(), tail[0]

        def scope(business_id):
            if business_id is None:
                return self.context(actor)[0]
            with self.runtime.database.read() as uow:
                row = uow.connection.execute("SELECT slug FROM businesses WHERE id=? AND archived_at IS NULL", (business_id,)).fetchone()
            if row is None:
                raise DomainError("not_found", "Project is not available to this principal")
            return self.context(actor, row["slug"])[0]

        def read():
            viewer = file_service.FileViewer(self.runtime, scope)
            if action == "history":
                return viewer.history(*file_service.parse_history_query(items, viewer.roots))
            return getattr(viewer, action)(viewer.selector(items))

        data = await anyio.to_thread.run_sync(read)
        if action not in {"download", "image"}:
            result = response(OperationResult.ok(data, request_id=request.scope["bazarlyk.request_id"]))
            result.headers["Cache-Control"] = "private, no-store"
            return result
        name, size, body = data
        headers = {"Cache-Control": "private, no-store", "Content-Length": str(size),
                   "Content-Disposition": file_service.content_disposition(name)}
        media_type = "application/octet-stream"
        if action == "image":
            media_type = name  # Validated raster MIME from the already opened descriptor.
            headers["Content-Disposition"] = "inline"
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        if isinstance(body, bytes):
            return Response(body, media_type=media_type, headers=headers)
        return StreamingResponse(body.chunks(), media_type=media_type, headers=headers,
                                 background=BackgroundTask(body.close))

    def _events_stream(self, request):
        """SSE change feed for the owner dashboard: bazarlyk/service/changes.py.

        Never a registered operation (no `application.invoke`, so it cannot
        appear in `tools/list`/`tools/call`), matching `root/perf` above.
        Subscribing is a fast, lock-protected, in-memory call, so this method
        itself does not need to be a coroutine; only the generator Starlette
        drains is async.
        """
        from bazarlyk.service.changes import HEARTBEAT_SECONDS
        changes = self.runtime.changes
        if changes is None:
            raise DomainError("not_found", "The change feed is not registered")
        subscriber = changes.subscribe()

        def frame(event):
            return f"id: {event['seq']}\ndata: {json.dumps(event)}\n\n".encode()

        async def events():
            # A dedicated limiter, not the shared worker pool: this blocks a
            # thread for up to a second at a time for the stream's whole life,
            # same reasoning as the terminal mirror's own `idle_limiter`.
            idle_limiter = anyio.CapacityLimiter(1)
            last_seq = 0
            last_heartbeat = time.monotonic()
            try:
                header = request.headers.get("last-event-id")
                requested = None
                if header is not None:
                    try:
                        requested = int(header)
                    except ValueError:
                        requested = None
                # No header at all means a freshly-loaded tab: it just read
                # everything fresh over the normal API, so there is nothing to
                # replay and no need to tell it to reset.
                if header is not None:
                    replay = changes.replay_since(requested) if requested is not None else None
                    if replay is None:
                        yield b"event: reset\ndata: {}\n\n"
                    else:
                        for event in replay:
                            yield frame(event)
                            last_seq = event["seq"]
                while not await request.is_disconnected():
                    try:
                        event = await anyio.to_thread.run_sync(subscriber.queue.get, True, 1.0, limiter=idle_limiter)
                    except queue.Empty:
                        event = None
                    if subscriber.dropped:
                        # The queue overflowed while this subscriber was slow;
                        # its own missed events are unrecoverable, so tell the
                        # client to refetch rather than resume from a gap.
                        subscriber.dropped = False
                        yield b"event: reset\ndata: {}\n\n"
                    if event is None:
                        if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                            yield b": heartbeat\n\n"
                            last_heartbeat = time.monotonic()
                        continue
                    if event["seq"] <= last_seq:
                        continue  # Already sent via the replay above.
                    last_seq = event["seq"]
                    yield frame(event)
                    last_heartbeat = time.monotonic()
            finally:
                changes.unsubscribe(subscriber)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def data_upload_download(self, context, identity):
        from urllib.parse import quote
        def read():
            with self.runtime.database.read() as uow:
                self.runtime.application.authorize(context, self.runtime.registry.get("data_uploads_get"), (), uow)
                return self.runtime.domains.data_uploads.download(context, identity, uow)
        name, data = await anyio.to_thread.run_sync(read)
        return Response(data, media_type="application/octet-stream", headers={"Cache-Control": "no-store",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})

    async def signoff_route(self, request, context, tail):
        if request.method == "GET":
            if len(tail) == 1:
                args = dict(request.query_params)
                if "limit" in args:
                    try:
                        args["limit"] = int(args["limit"])
                    except ValueError:
                        raise DomainError("validation_error", "Signoff limit must be an integer") from None
                if args.get("updated_after") == "0":
                    args["updated_after"] = 0
                if "responded" in args:
                    if args["responded"] not in {"true", "false"}:
                        raise DomainError("validation_error", "responded must be true or false")
                    args["responded"] = args["responded"] == "true"
                if "states" in args:
                    args["states"] = args["states"].split(",")
                if "execution_states" in args:
                    args["execution_states"] = args["execution_states"].split(",")
                if "scope" in args and args["scope"] != ("business" if context.business_id else "global"):
                    raise DomainError("forbidden", "Queue scope differs from the URL")
                args["scope"] = "business" if context.business_id else "global"
                async def produce():
                    result = await anyio.to_thread.run_sync(lambda: self.runtime.application.invoke(context, "signoffs_list", args))
                    return response(result)
                return await revalidated_async(request, self.runtime.changes, {"signoffs"}, context.business_id, produce)
            if len(tail) == 2:
                return response(await anyio.to_thread.run_sync(lambda: self.runtime.signoffs.get(context, tail[1])))
        if request.method == "POST" and len(tail) == 3 and tail[2] == "amend":
            body = await read_json(request)
            required = {"changes", "proposal_revision", "payload_hash", "expected_status_revision", "idempotency_key"}
            checked_fields(body, required, required)
            return response(await anyio.to_thread.run_sync(partial(self.runtime.signoffs.amend, context, tail[1], **body)))
        if request.method == "POST" and len(tail) == 3 and tail[2] == "respond":
            body = await read_json(request)
            required = {"proposal_revision", "payload_hash", "expected_status_revision", "idempotency_key"}
            checked_fields(body, required | {"notes", "selected_choices"}, required)
            return response(await anyio.to_thread.run_sync(partial(self.runtime.signoffs.respond, context, tail[1], **body)))
        if request.method == "POST" and len(tail) == 3 and tail[2] in {"approve", "reject", "retry"}:
            body = await read_json(request)
            required = {"proposal_revision", "payload_hash", "expected_status_revision", "idempotency_key"}
            checked_fields(body, required | {"feedback"}, required)
            method = self.runtime.signoffs.retry if tail[2] == "retry" else self.runtime.signoffs.review
            if tail[2] == "retry":
                checked_fields(body, required, required)
                call = partial(method, context, tail[1], **body)
            else:
                call = partial(method, context, tail[1], decision=tail[2], **body)
            return response(await anyio.to_thread.run_sync(call))
        raise DomainError("not_found", "Signoff endpoint is not registered")

    async def integration_route(self, request, parts):
        """The narrow cross-application surface; bearer integration identity only. The generic credential dispatch is
        core's; the identity lookup and every route belong to the selected extension that registered the slug."""
        if not self.integration_routes:
            raise DomainError("not_found", "Endpoint is not registered")
        await anyio.to_thread.run_sync(self.runtime.require_authority)
        actor = await anyio.to_thread.run_sync(lambda: self.authentication.integration(request.headers.get("authorization")))
        request.scope["bazarlyk.auth"] = actor
        if len(parts) < 2 or parts[1] != actor.integration_slug:
            raise DomainError("forbidden", "The integration credential does not own this route")
        route = self.integration_routes.get((parts[1], request.method, tuple(parts[2:])))
        if route is None:
            raise DomainError("not_found", "Integration endpoint is not registered")
        return await route(self, request, actor, request.scope.get("bazarlyk.request_id", new_id()))

    async def viewbox_board(self, request, context, agent_id, request_id):
        """Owner-only bounded read of one agent's Viewbox board; conditional so a visible pane can refresh cheaply.

        The private tokenless dashboard is the installation owner, so a bearer credential can never authorize this read:
        any explicit ``Authorization`` header (agent, human, owner session, invalid or empty) is refused before the board is read.
        """
        if request.headers.get("authorization") is not None:
            raise DomainError("forbidden", "The Viewbox board is for the owner's dashboard and takes no bearer credential")
        from bazarlyk.viewbox_preferences import board_with_dismissals, revision
        data = await anyio.to_thread.run_sync(partial(
            board_with_dismissals, self.runtime.domains.viewbox, self.runtime.paths.runtime, context, agent_id))
        # The owner's dismissals are part of what the pane shows, so they are part of the tag.
        etag = f'W/"viewbox-{data["agent_id"]}-{data["revision"]}-{revision(data["dismissed_attention_ids"])}"'
        candidates = {value.strip() for value in request.headers.get("if-none-match", "").split(",")}
        if etag in candidates:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-store"})
        result = response(OperationResult.ok(data, request_id=request_id))
        result.headers["ETag"] = etag
        result.headers["Cache-Control"] = "private, no-store"
        return result

    async def viewbox_dismiss(self, request, context, agent_id, request_id):
        """Owner-only, idempotent: remember that one exact current placement is dismissed from the Focus queue.

        Same bearer refusal as the board read. The board, its source and the viewed acknowledgements are untouched.
        """
        if request.headers.get("authorization") is not None:
            raise DomainError("forbidden", "The Viewbox board is for the owner's dashboard and takes no bearer credential")
        from bazarlyk.viewbox_preferences import dismiss
        body = await read_json(request, limit=4096)
        data = await anyio.to_thread.run_sync(partial(
            dismiss, self.runtime.domains.viewbox, self.runtime.paths.runtime, context, agent_id, body))
        result = response(OperationResult.ok(data, request_id=request_id))
        result.headers["Cache-Control"] = "private, no-store"
        return result

    async def focus_layout(self, request, context, agent_id, request_id):
        """Owner-only read or revision-checked save of one agent's personal Focus text blocks, plus the matching Focus card IDs.

        No bearer credential of any kind can authorize it, and reading creates nothing. The layout is a dashboard preference
        outside every Focus card, task and agent context; the card IDs are read-only and never accepted from a save.
        """
        if request.headers.get("authorization") is not None:
            raise DomainError("forbidden", "Focus layouts are for the owner's dashboard and take no bearer credential")
        from bazarlyk.focus_layout_preferences import MAX_REQUEST_BYTES, layout, query_flags
        flags = query_flags(request.query_params.multi_items())
        body = None
        if request.method == "POST":
            try:
                body = await read_json(request, limit=MAX_REQUEST_BYTES)
            except DomainError as error:
                if error.code != "payload_too_large":
                    raise
                raise DomainError("validation_error", "A Focus layout request is limited to 1 MiB",
                                  field_errors={"blocks": "the encoded request exceeds 1 MiB"}) from None
        data = await anyio.to_thread.run_sync(partial(layout, self.runtime, context, agent_id, body, **flags))
        result = response(OperationResult.ok(data, request_id=request_id))
        result.headers["Cache-Control"] = "private, no-store"
        return result

    async def agent_route(self, request, parts):
        agent_route = self.agent_routes.get((request.method, tuple(parts))) or self.agent_routes.get((None, tuple(parts)))
        if agent_route is not None:
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            return await agent_route(self, request)
        if request.method == "POST" and parts == ["agent", "technical-observation"]:
            # This endpoint is deliberately a narrow authenticated write port
            # for prepared hooks.  It does not invoke the application registry
            # and never observes its own reporting failures.
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            checked_fields(body, {"registration_id", "session_id", "observation"},
                           {"registration_id", "session_id", "observation"})
            if body["registration_id"] != actor.registration_id:
                # The reason is a fixed safe code: a prepared client quarantines
                # only this per-record rejection, never a generic forbidden.
                raise DomainError("forbidden", "Technical report registration is not bound to this credential",
                                  details={"reason": "technical_registration_mismatch"})
            if (not isinstance(body["observation"], dict) or not isinstance(body["session_id"], str)
                    or body["session_id"] != body["observation"].get("session_id")):
                raise DomainError("validation_error", "Technical report session attribution is invalid")

            def ingest():
                from qorqut_operations.application.engine import Application
                from bazarlyk.incidents import ensure_system_reporter, technical_session_binding
                incidents = self.runtime.incidents
                if incidents is None:
                    raise DomainError("service_unavailable", "Technical reporting is not installed", retryable=True)
                writer = incidents.writer
                with writer() if writer else nullcontext():
                    with self.runtime.database.transaction(operation="agent_technical_observation") as uow:
                        Application.ensure_writes_open(uow)
                        if technical_session_binding(uow.connection, body["session_id"], actor.principal.id,
                                                     actor.registration_id) == "foreign":
                            raise DomainError("forbidden", "Technical report session belongs to another registration",
                                              details={"reason": "technical_session_mismatch"})
                        profile = uow.connection.execute(
                            "SELECT home_business_id FROM agent_profiles WHERE agent_id=? AND status='active'",
                            (actor.principal.id,)).fetchone()
                        business_id = profile["home_business_id"] if profile else None
                        reporter_id = incidents.reporter_id
                        if reporter_id is None:
                            reporter_id = ensure_system_reporter(uow, incidents.authority)
                            incidents.reporter_id = reporter_id
                        try:
                            observation = incidents.reporter.external_observation(
                                body["observation"], agent_id=actor.principal.id, business_id=business_id,
                                observation_namespace=actor.principal.id)
                        except ValueError as exc:
                            raise DomainError("validation_error", "Technical observation is invalid") from exc
                        # Reuse the canonical ingestion path, including the
                        # durable observation receipt that makes replay exact-once.
                        result = incidents.reporter._drain_lines(
                            uow, [canonical_bytes(observation.to_dict())], reporter_id)
                return {"status": "accepted", "observation_id": body["observation"]["observation_id"],
                        "processed": result["processed"], "created": result["created"],
                        "updated": result["updated"], "recovered": result["recovered"]}

            value = await anyio.to_thread.run_sync(ingest)
            return response(OperationResult.ok(value, request_id=new_id()))
        if request.method == "POST" and parts == ["agent", "focus", "browser"]:
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            fields = {"registration_id", "consumer_id", "command", "command_id", "phase",
                      "turn_id", "lease_id"}
            checked_fields(body, fields, {"registration_id", "consumer_id", "command", "command_id", "phase"})
            if body.pop("registration_id") != actor.registration_id:
                raise DomainError('forbidden', 'Browser registration is not bound to this credential')
            # Recheck current authority without rebuilding the agent's authored startup bundle
            # for each browser command. The Focus service checks the prepared session claim.
            context, _ = await anyio.to_thread.run_sync(partial(self.context, actor))
            turn = None
            if body["phase"] == "running" and self.runtime.browser_access is not None:
                acquired = await anyio.to_thread.run_sync(partial(self.runtime.browser_access.configured_control,
                    actor.principal, "acquire", consumer_id=body["consumer_id"]))
                body.update({"turn_id": acquired["turn"]["id"], "lease_id": acquired["turn"]["lease_id"]})
                turn = acquired
            elif body["phase"] == "active" and self.runtime.browser_access is not None:
                await anyio.to_thread.run_sync(partial(self.runtime.browser_access.configured_control,
                    actor.principal, "renew", consumer_id=body["consumer_id"],
                    turn_id=body.get("turn_id"), lease_id=body.get("lease_id")))
            try:
                data = await anyio.to_thread.run_sync(partial(self.runtime.focus.browser_activity, context,
                                                               **{key: body[key] for key in ("consumer_id", "command", "command_id", "phase")}))
            except Exception:
                if turn is not None:
                    await anyio.to_thread.run_sync(partial(self.runtime.browser_access.configured_control,
                        actor.principal, "release", consumer_id=body["consumer_id"],
                        turn_id=turn["turn"]["id"], lease_id=turn["turn"]["lease_id"]))
                raise
            if body["phase"] in {"finished", "failed"} and self.runtime.browser_access is not None:
                await anyio.to_thread.run_sync(partial(self.runtime.browser_access.configured_control,
                    actor.principal, "release", consumer_id=body["consumer_id"],
                    turn_id=body.get("turn_id"), lease_id=body.get("lease_id")))
            if turn:
                data = {**data, "browser_turn": turn}
            return response(OperationResult.ok(data, request_id=new_id()))
        if request.method == "POST" and parts == ["agent", "readiness"]:
            # Readiness validates the prepared registration without creating a
            # session, inbox lease, delivery receipt, or other mutation.
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            checked_fields(body, {"registration_id"}, {"registration_id"})
            if body["registration_id"] != actor.registration_id:
                raise DomainError("forbidden", "Readiness registration is not bound to this credential")
            context = InvocationContext(actor.principal, source="mcp",
                request_id=request.scope.get("bazarlyk.request_id", new_id()))
            config = await anyio.to_thread.run_sync(lambda: self.runtime.observed_agent_call(
                context, "agent.readiness", "resolve", lambda: self.runtime.resolver.resolve_agent_config(
                    actor.principal, session_registration={"registration_id": body["registration_id"]}), readiness=True))
            preparation = config.data.get("preparation", {})
            return response(OperationResult.ok({
                "ready": preparation.get("ready") is True,
                "effective_config_id": config.effective_config_id,
            }, request_id=new_id()))
        if request.method == "POST" and parts == ["agent", "delegation", "browser"]:
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            if actor.delegation_assignment_id is None:
                raise DomainError("forbidden", "Only a bound durable worker may use assignment browser access")
            body = await read_json(request, limit=128 * 1024)
            checked_fields(body, {"action", "turn_id", "lease_id", "turn_token", "command",
                                  "checkpoint", "last_action", "pending_outcome", "next_action", "lease_seconds", "reason"},
                           {"action"})
            access = self.runtime.browser_access
            if access is None:
                raise DomainError("operation_unavailable", "Browser access is not installed", retryable=True)
            data = await anyio.to_thread.run_sync(access.worker, actor, body["action"], body)
            result = response(OperationResult.ok(data, request_id=request.scope.get("bazarlyk.request_id", new_id())))
            result.headers["Cache-Control"] = "no-store"
            return result
        if request.method == "POST" and parts in (["agent", "delegation", "claim"],
                ["agent", "delegation", "poll"], ["agent", "delegation", "fail"]):
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            if actor.delegation_assignment_id is not None:
                raise DomainError("forbidden", "A delegated worker cannot control the parent dispatcher")
            body = await read_json(request, limit=8192)
            fields = {"registration_id", "consumer_id"}
            if parts[-1] in {"poll", "fail"}:
                fields |= {"assignment_id", "generation"}
            if parts[-1] == "fail":
                fields.add("reason")
            if parts[-1] == "poll":
                fields |= {"runtime_thread_id", "turn_number", "turn_usage", "reset_runtime_thread"}
            optional = ({"reason"} if parts[-1] == "fail" else
                        {"runtime_thread_id", "turn_number", "turn_usage", "reset_runtime_thread"}
                        if parts[-1] == "poll" else set())
            optional |= {"binding_generation"}
            checked_fields(body, fields | {"binding_generation"}, fields - optional)
            if body["registration_id"] != actor.registration_id:
                raise DomainError("forbidden", "Dispatcher registration is not bound to this credential")
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal,
                session_registration_id=actor.registration_id))
            if 'binding_generation' in body and (type(body['binding_generation']) is not int or body['binding_generation']<0):
                raise DomainError('validation_error','Binding generation must be a nonnegative integer')
            if 'binding_generation' in body:
                context = replace(context,inbox_session_id=body['consumer_id'],inbox_generation=body['binding_generation'])
            if parts[-1] == "claim":
                call = partial(self.runtime.delegation_operations.claim_next_for_dispatch,
                    context, body["registration_id"], body["consumer_id"])
            elif parts[-1] == "poll":
                call = partial(self.runtime.delegation_operations.poll_worker, context,
                    body["registration_id"], body["consumer_id"], body["assignment_id"], body["generation"],
                    runtime_thread_id=body.get("runtime_thread_id"), turn_number=body.get("turn_number"),
                    turn_usage=body.get("turn_usage"), reset_runtime_thread=body.get("reset_runtime_thread", False))
            else:
                call = partial(self.runtime.delegation_operations.dispatch_failed, context,
                    body["registration_id"], body["consumer_id"], body["assignment_id"], body["generation"],
                    reason=body.get("reason", "Codex worker execution failed"))
            data = await anyio.to_thread.run_sync(call)
            result = response(OperationResult.ok(data, request_id=request.scope.get("bazarlyk.request_id", new_id())))
            result.headers["Cache-Control"] = "no-store"
            return result
        if request.method == "POST" and parts == ["agent", "inbox", "recovery"]:
            # The one retained helper wake: a prepared context handoff is announced to its own exact native session through the
            # input arbiter. It is not an inbox event and acknowledges nothing; pending inbox work is announced by the backend.
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            fields = {"registration_id", "consumer_id", "notification_id", "runtime"}
            checked_fields(body, fields | {"owner", "binding_generation"}, fields)
            if body["registration_id"] != actor.registration_id:
                raise DomainError("forbidden", "Recovery registration is not bound to this credential")
            try:
                if str(uuid.UUID(body["notification_id"])) != body["notification_id"]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise DomainError("validation_error", "Recovery notification requires its episode UUID")
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal, diagnostic=True))
            nudges = self.runtime.inbox_nudges
            if nudges is None:
                raise DomainError("inbox_delivery_unavailable", "Native input is not running", retryable=True)
            call = partial(nudges.recovery_notice, actor.principal.id, body["notification_id"], consumer_id=body["consumer_id"])
            data = await anyio.to_thread.run_sync(partial(self.runtime.observed_agent_call, context, "inbox.recovery", "notice", call))
            return response(OperationResult.ok(data, request_id=new_id()))
        if request.method == "POST" and parts == ["agent", "attention-hook"]:
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            checked_fields(body, {'registration_id', 'session_id', 'action', 'claim'},
                           {'registration_id', 'session_id', 'action'})
            if body['registration_id'] != actor.registration_id:
                raise DomainError('forbidden', 'Attention hook registration does not match credential')
            action = body['action']
            attention = self.runtime.attention_runtime
            if action == 'claim':
                call = partial(attention.hook_claim, actor.principal.id, actor.registration_id, body['session_id'])
            elif action in ('begin', 'settle'):
                claim = body.get('claim')
                keys = {'id', 'claim_token', 'claim_consumer_id', 'claim_binding_generation',
                        'focus_generation', 'revision'}
                if not isinstance(claim, dict) or not keys.issubset(claim) or claim['claim_consumer_id'] != body['session_id']:
                    raise DomainError('validation_error', 'Invalid attention hook claim')
                call = partial(attention.hook_begin if action == 'begin' else attention.hook_settle,
                               actor.principal.id, actor.registration_id, claim)
            else:
                raise DomainError('validation_error', 'Invalid attention hook action')
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal, diagnostic=True))
            value = await anyio.to_thread.run_sync(partial(self.runtime.observed_agent_call, context,
                'agent.attention_hook', action, call))
            return response(OperationResult.ok({'result': value}, request_id=new_id()))
        if request.method == "POST" and parts == ["agent", "checkpoint"]:
            await anyio.to_thread.run_sync(self.runtime.require_authority)
            actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
            request.scope["bazarlyk.auth"] = actor
            body = await read_json(request, limit=8192)
            checked_fields(body, {"registration_id","session_id","turn_id","allow_correction","claim_id","binding_generation","focus_only"},
                           {"registration_id","session_id","turn_id"})
            if body["registration_id"] != actor.registration_id:
                raise DomainError("forbidden", "Checkpoint registration is not bound to this credential")
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal, diagnostic=True))
            value = await anyio.to_thread.run_sync(partial(self.runtime.observed_agent_call, context,
                'agent.checkpoint', 'inspect', partial(self.runtime.checkpoints.check, context, **body)))
            return response(OperationResult.ok(value, request_id=new_id()))
        if request.method != "POST" or parts not in (["agent", "bootstrap"], ["agent", "bootstrap", "receipt"]):
            raise DomainError("not_found", "Agent endpoint is not registered")
        await anyio.to_thread.run_sync(self.runtime.require_authority)
        actor = await anyio.to_thread.run_sync(lambda: self.authentication.agent(request.headers.get("authorization")))
        request.scope["bazarlyk.auth"] = actor
        body = await read_json(request, limit=8192)
        fields = {"registration_id", "session_id", "event"}
        if len(parts) == 2:
            checked_fields(body, fields | {"last_effective_config_id", "runtime"}, fields)
            method = self.runtime.resolver.bootstrap
        else:
            fields |= {"effective_config_id", "bundle_hash", "emitted_at"}
            checked_fields(body, fields | {"briefing_hash"}, fields)
            method = self.runtime.resolver.record_delivery
        if body["registration_id"] != actor.registration_id:
            raise DomainError("forbidden", "Bootstrap registration is not bound to this credential")
        context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal, diagnostic=True))
        value = await anyio.to_thread.run_sync(partial(self.runtime.observed_agent_call, context,
            'agent.bootstrap', 'prepare' if len(parts) == 2 else 'receipt',
            partial(method, actor.principal, **body), readiness=len(parts) == 2))
        if len(parts) == 2 and value.get('ready') and self.runtime.activity:
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal))
            value['activity_available'] = await anyio.to_thread.run_sync(partial(self.runtime.activity.register_session, context,
                body['registration_id'], body['session_id'], body.get('runtime','claude-code')))
        if len(parts) == 2 and value.get('ready') and value.get('inbox_available'):
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal))
            def bind_native():
                from bazarlyk.bootstrap.native_binding import NativeBindingAdapter
                with self.runtime.database.read() as uow:
                    old = uow.connection.execute('SELECT binding_json,generation,consumer_id FROM native_session_owners WHERE agent_id=?', (actor.principal.id,)).fetchone()
                adapter = getattr(self.runtime, 'native_binding_adapter', None) or NativeBindingAdapter(self.runtime)
                try:
                    binding, state = adapter.verify(actor.principal.id, body['registration_id'], body['session_id'],
                        previous=json.loads(old['binding_json']) if old else None)
                except DomainError:
                    # The current owner's own session whose native process cannot be verified now keeps its owner row
                    # and binding unchanged; any other session is refused.
                    if old and old['consumer_id'] == body['session_id']:
                        binding, state = None, 'unknown'
                    else:
                        raise
                return self.runtime.inbox.start_session(context, body['registration_id'], body['session_id'], body['event'],
                    binding=binding, old_state=state, expected_generation=old['generation'] if old else 0)
            try:
                ownership = await anyio.to_thread.run_sync(bind_native)
                value.update(ownership)
            except DomainError as error:
                if error.code not in {'inbox_binding_required','inbox_binding_ambiguous','inbox_session_superseded','inbox_generation_stale','inbox_selection_required'}:
                    raise
                value['inbox_available'] = False
                value['inbox_binding_error'] = error.code
        if len(parts) == 2 and value.get('ready') and 'mandatory_context' in value:
            from bazarlyk.service.environment import bootstrap_environment
            context = await anyio.to_thread.run_sync(partial(self.runtime.agent_context, actor.principal))
            value['environment'] = await anyio.to_thread.run_sync(
                partial(bootstrap_environment, self.runtime, context))
        return response(OperationResult.ok(value, request_id=new_id()))
