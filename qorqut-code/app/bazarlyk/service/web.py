"""One ASGI lifecycle for dashboard, owner API, and all agent MCP sessions."""

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
import re

import anyio
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from qorqut_operations.contracts import DomainError, OperationResult, new_id
from bazarlyk.integrations.proxy import OutreachProxy
from bazarlyk.transport.auth import Authentication, OriginPolicy
from bazarlyk.transport.http import HTTPAPI
from bazarlyk.transport.mcp import MCPTransport
from bazarlyk.transport.results import failure, response


UUID_SELECTOR = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# 'unsafe-inline' styles are needed only so the sandboxed, script-less HTML preview frame (srcdoc, which inherits this
# policy) can keep its own inline styles; scripts come from this origin's own files alone.
FILE_VIEWER_PATH = "/files/view"
FILE_VIEWER_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                      "connect-src 'self'; frame-src 'none'; base-uri 'none'; form-action 'none'")
# A completed daily report's own page (bazarlyk/reports/web.py): the dashboard shows it in a same-origin, script-free iframe.
# Only this exact owner-only route may be framed, and only by this origin; every other response stays unframeable.
REPORT_PAGE = re.compile(r"/api/root/daily-reports/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/html")


class ServiceBoundary:
    def __init__(self, app, origins, *, frame_prefix=None, frame_origin=None):
        self.app, self.origins = app, origins
        self.frame_prefix, self.frame_origin = frame_prefix, frame_origin

    # The standalone owner viewers the Viewbox shows in a same-origin frame. Owner APIs, downloads and every other path
    # stay unframeable, and there is no prefix rule for either viewer.
    SAME_ORIGIN_FRAMED = frozenset({FILE_VIEWER_PATH, "/tables/view"})

    def framed(self, path):
        return bool(self.frame_prefix) and (path == self.frame_prefix
            or path.startswith(self.frame_prefix + "/"))

    @staticmethod
    def report_page(path):
        return REPORT_PAGE.fullmatch(path) is not None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            self.origins.validate(Request(scope))
        except DomainError as exc:
            return await failure(exc, new_id())(scope, receive, send)

        async def headers(message):
            if message["type"] == "http.response.start":
                path = scope.get("path", "")
                if self.framed(path):
                    framing = [(b"content-security-policy", b"frame-ancestors " + self.frame_origin.encode("ascii"))]
                elif path in self.SAME_ORIGIN_FRAMED or self.report_page(path):
                    # Only the standalone file/table viewers and a completed report's page may sit in a same-origin
                    # frame. Owner APIs, downloads and every other path stay unframeable; there is no prefix rule.
                    framing = [(b"x-frame-options", b"SAMEORIGIN"), (b"content-security-policy", b"frame-ancestors 'self'")]
                else:
                    framing = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'")]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"), *framing]
                if path.startswith("/js/") or path.startswith("/css/"):
                    # The StaticFiles mounts below take no headers= constructor
                    # argument, unlike the FileResponse calls index()/app_css()
                    # make directly; this is the lowest-risk place to give the
                    # two mounts the same revalidation header without a second
                    # middleware layer just for this.
                    message["headers"].append((b"cache-control", b"no-cache"))
                if path.startswith("/api/"):
                    operation = scope.get("bazarlyk.transport_operation", "")
                    # Agent tooling gets no dashboard-observability header at all; /mcp
                    # never reaches this branch since its path doesn't start with /api/.
                    # The change feed stream is long-lived: a header emitted once at
                    # connection-open would only ever describe that open, never the
                    # stream's lifetime, so it is left off entirely rather than published
                    # misleadingly.
                    if (not (operation == "agent" or (isinstance(operation, str) and operation.startswith("agent.")))
                            and path != "/api/root/events"):
                        # `body` is the size HTTPAPI measured from the Python response
                        # object, before any ASGI-level gzip encoding below; it is
                        # always the uncompressed byte count, even when the client
                        # received a gzip-encoded, shorter reply.
                        etag_desc = scope.get("bazarlyk.etag")
                        timing = ", ".join((
                            f"total;dur={scope.get('bazarlyk.total_ms', 0.0):.1f}",
                            f"db;dur={scope.get('bazarlyk.db_ms', 0.0):.1f}",
                            f"units;dur={scope.get('bazarlyk.units', 0)}",
                            f"body;dur={scope.get('bazarlyk.response_bytes', 0)}",
                            *([f"etag;desc={etag_desc}"] if etag_desc else ())))
                        message["headers"].append((b"server-timing", timing.encode("ascii")))
            await send(message)
        return await self.app(scope, receive, headers)


class SelectiveGZip:
    """Gzip owner-visible dashboard traffic; never agent tooling, MCP, or an
    SSE stream (terminals, the change feed at /api/root/events).

    `GZipMiddleware` has no path filter of its own, so this selects between a
    shared `GZipMiddleware` instance and the plain app by `scope["path"]`
    before either one ever calls `send`. The terminal family
    (`transport/terminals.py`) is matched with the same path segments
    `HTTPAPI._dispatch` uses to route to it (`bazarlyk/transport/http.py`,
    `parts[:1] == ["agent"]`, `parts[:1] == ["root"] and parts[1:2] in
    (...)`, `tail[:1] in (["terminal"], ["terminals"])`), so the exclusion
    holds by construction for every current terminal action (including the
    GET .../stream route that returns `StreamingResponse`), not only the
    literal string "stream". This is deliberately independent of Starlette's
    own `text/event-stream` content-type exclusion (present in the pinned
    version's `GZipMiddleware` but not something this codebase controls):
    the terminal stream must never even reach `GZipMiddleware`.
    """

    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._compressible(scope.get("path", "")):
            return await self.compressed(scope, receive, send)
        return await self.app(scope, receive, send)

    @staticmethod
    def _compressible(path):
        if path in ("/", "/app.css") or path.startswith("/js/") or path.startswith("/css/"):
            return True
        if not path.startswith("/api/"):
            return False  # /mcp, /mcp/owner, /health/*, the agent browser proxy.
        parts = path[len("/api/"):].strip("/").split("/")
        if parts[:1] == ["agent"]:
            return False
        if parts[:1] == ["root"] and parts[1:2] in (["terminal"], ["terminals"], ["workspace-terminal"], ["events"]):
            return False
        if parts[:3] == ["root", "files", "download"]:
            return False  # Exact bytes with their own Content-Length, streamed from the opened file.
        if parts[:1] == ["biz"] and parts[2:3] in (["terminal"], ["terminals"]):
            return False
        return True


def create_app(runtime, *, origins, bootstrap_token="", dashboard_root=None,
               authentication=None, compatibility=None, terminals=None, manage_lifecycle=True,
               agent_only=False, integrations=None, integration_client=None):
    origins = OriginPolicy(origins)
    viewbox = getattr(getattr(runtime, "domains", None), "viewbox", None)
    if viewbox is not None:
        viewbox.configure_origins(origins.origins)
    authentication = authentication or Authentication(runtime.database, runtime.paths, bootstrap_token=bootstrap_token)
    api = HTTPAPI(runtime, authentication, origins, compatibility=compatibility, terminals=terminals,
                  integrations=integrations)
    proxy = OutreachProxy(integrations, client=integration_client) if integrations is not None else None
    mcp = MCPTransport(runtime, authentication, origins)
    owner_mcp = None
    if not agent_only:
        from bazarlyk.transport.owner_mcp import OwnerSessionCaller
        owner_mcp = MCPTransport(runtime, authentication, origins,
                                 caller=OwnerSessionCaller(runtime, authentication))
    if dashboard_root is None:
        from bazarlyk.installation import component
        dashboard_root = component(runtime.paths, "dashboard")
    dashboard_root = Path(dashboard_root).resolve()

    @asynccontextmanager
    async def lifespan(app):
        if manage_lifecycle:
            await anyio.to_thread.run_sync(runtime.start)
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(mcp.manager.run())
                if owner_mcp is not None:
                    await stack.enter_async_context(owner_mcp.manager.run())
                yield
        finally:
            if manage_lifecycle:
                await anyio.to_thread.run_sync(runtime.stop)

    async def index(request):
        if "token" in request.query_params:
            return RedirectResponse("/", status_code=303)
        # no-store, not the app.css/js/css revalidation below: the initial
        # platform commit set this explicitly and stricter, and PR5 widening
        # it to no-cache was an overreach beyond "add revalidation" that this
        # restores.
        return FileResponse(dashboard_root / "index.html", headers={"Cache-Control": "no-store"})

    async def tables_view(request):
        """Owner's standalone read-only table page. The shell holds no data; it reads through the owner API."""
        allowed, query = {"table_id", "business_id"}, request.query_params
        keys = [key for key, _ in query.multi_items()]
        valid = (len(keys) == len(set(keys)) and set(keys) <= allowed and "table_id" in keys
                 and all(UUID_SELECTOR.fullmatch(query[key]) for key in keys))
        if not valid:
            return JSONResponse({"status": "error", "error": {"code": "invalid_request",
                "message": "Use /tables/view?table_id=<UUID> with an optional business_id=<UUID>."}},
                status_code=400, headers={"Cache-Control": "no-store"})
        return FileResponse(dashboard_root / "tables-view.html", headers={"Cache-Control": "no-store"})

    async def app_css(request):
        return FileResponse(dashboard_root / "app.css", media_type="text/css", headers={"Cache-Control": "no-cache"})

    async def file_viewer_page(request):
        # The static shell only: the page reads files through the owner-only /api/root/files routes. Its script is
        # the dashboard's own file, and its policy is inherited by the sandboxed HTML preview frame.
        page = dashboard_root / "file-viewer.html"
        if not page.is_file():
            return failure(DomainError("not_found", "The file viewer is not installed"))
        return FileResponse(page, headers={"Cache-Control": "no-store", "Content-Security-Policy": FILE_VIEWER_POLICY})

    async def liveness(request):
        return JSONResponse({"live": True}, headers={"Cache-Control": "no-store"})

    async def readiness(request):
        try:
            await anyio.to_thread.run_sync(runtime.require_authority)
            from bazarlyk.installation import app_manifest
            manifest=app_manifest()
            return response(OperationResult.ok({"ready": True, "service": "bazarlyk",
                "release_id":manifest['release_id'] if manifest else None}, request_id=new_id()))
        except DomainError as exc:
            return failure(exc)

    async def api_request(request):
        return await api(request)

    async def agent_request(request):
        # An explicit allowlist avoids exposing future owner or host-control APIs.
        request.path_params["path"] = request.url.path.removeprefix("/api/")
        return await api(request)

    if agent_only:
        agent_paths = [
            "/api/agent/readiness",
            "/api/agent/bootstrap", "/api/agent/bootstrap/receipt",
            "/api/agent/technical-observation",
            "/api/agent/delegation/claim",
            "/api/agent/delegation/poll", "/api/agent/delegation/fail",
            "/api/agent/delegation/browser",
            "/api/agent/inbox/watch", "/api/agent/inbox/release"]
        if getattr(authentication, "business_id", None) and runtime.browser_access is not None:
            agent_paths.append("/api/agent/browser")
        if getattr(authentication, "business_id", None) and runtime.provider_tokens is not None:
            agent_paths.append("/api/agent/provider-token")
        routes = [Route(path, agent_request, methods=["POST"]) for path in agent_paths]
    else:
        routes = [Route("/", index), Route("/tables/view", tables_view), Route("/app.css", app_css), Route(FILE_VIEWER_PATH, file_viewer_page),
            Route("/health/live", liveness), Route("/health/ready", readiness),
            Route("/api/{path:path}", api_request, methods=["GET", "POST", "DELETE"]),
            Mount("/js", StaticFiles(directory=dashboard_root / "js")), Mount("/css", StaticFiles(directory=dashboard_root / "css"))]
    # Route an exact ASGI endpoint: Mount('/mcp') would redirect the required /mcp path.
    from starlette.routing import BaseRoute, Match
    class MCPRoute(BaseRoute):
        def __init__(self, transport, paths):
            self.transport, self.paths = transport, paths

        def matches(self, scope):
            return (Match.FULL, {}) if scope["type"] == "http" and scope["path"] in self.paths else (Match.NONE, {})

        async def handle(self, scope, receive, send):
            await self.transport(scope, receive, send)

        def url_path_for(self, name, **path_params):
            from starlette.routing import NoMatchFound
            raise NoMatchFound(name, path_params)
    class BrowserRoute(BaseRoute):
        def matches(self, scope):
            import re
            business_id = getattr(authentication, "business_id", None)
            available = (agent_only and business_id and runtime.browser_access is not None
                         and scope["type"] in {"http", "websocket"}
                         and re.match(r"^/[a-f0-9]{64}/", scope["path"]))
            return (Match.FULL, {}) if available else (Match.NONE, {})

        async def handle(self, scope, receive, send):
            await runtime.browser_access.proxy(authentication.business_id, scope, receive, send)

        def url_path_for(self, name, **path_params):
            from starlette.routing import NoMatchFound
            raise NoMatchFound(name, path_params)

    mcp_routes = [MCPRoute(mcp, {"/mcp", "/mcp/"})]
    if owner_mcp is not None:
        mcp_routes.append(MCPRoute(owner_mcp, {"/mcp/owner", "/mcp/owner/"}))
    class IntegrationProxyRoute(BaseRoute):
        """One fixed prefix; the proxy itself refuses every other destination."""

        def matches(self, scope):
            return (Match.FULL, {}) if scope["type"] == "http" and proxy.owns(scope.get("path", "")) else (Match.NONE, {})

        async def handle(self, scope, receive, send):
            await proxy(scope, receive, send)

        def url_path_for(self, name, **path_params):
            from starlette.routing import NoMatchFound
            raise NoMatchFound(name, path_params)
    proxy_routes = [IntegrationProxyRoute()] if proxy is not None else []
    content_routes = []
    if not agent_only:
        from bazarlyk.content_sessions.http import ContentCanvas
        canvas = ContentCanvas(api)
        content_routes = [Route('/content/sessions/{session_id}/{content_path:path}', canvas.__call__, methods=['GET','HEAD','POST'])]

    application = Starlette(routes=[*mcp_routes, BrowserRoute(), *proxy_routes, *content_routes, *routes], lifespan=lifespan)
    application.state.runtime = runtime
    application.state.authentication = authentication
    application.state.mcp = mcp
    application.state.owner_mcp = owner_mcp
    application.state.http_api = api
    application.state.integration_proxy = proxy
    application.add_middleware(ServiceBoundary, origins=origins,
        frame_prefix=proxy.prefix if proxy is not None else None,
        frame_origin=integrations.dashboard_origin if proxy is not None else None)
    # Added after ServiceBoundary: Starlette's add_middleware inserts at the
    # front of its stack, and the stack is built outermost-last, so the
    # middleware added last ends up outermost. Gzip must wrap ServiceBoundary
    # (not the reverse) so it compresses the final bytes, security headers
    # included, rather than something ServiceBoundary re-wraps afterward.
    application.add_middleware(SelectiveGZip)
    return application
