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
from qorqut.transport.auth import Authentication, OriginPolicy
from qorqut.transport.http import HTTPAPI
from qorqut.transport.mcp import MCPTransport
from qorqut.transport.results import failure, response


UUID_SELECTOR = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# 'unsafe-inline' styles are needed only so the sandboxed, script-less HTML preview frame (srcdoc, which inherits this
# policy) can keep its own inline styles; scripts come from this origin's own files alone.
FILE_VIEWER_PATH = "/files/view"
FILE_VIEWER_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                      "connect-src 'self'; frame-src 'none'; base-uri 'none'; form-action 'none'")
# Core's own URL namespaces: API, MCP, health, dashboard static files and extension assets. No extension route sits in one.
CORE_NAMESPACES = ("/api", "/mcp", "/health", "/js", "/css", "/icons", "/extensions")


def route_claim(owner, route):
    """What one extension route reserves, as ``(path, subtree)``. A fixed path reserves itself; a parameterized path
    (``{name}``), a ``Mount`` or a route that declares ``prefix`` reserves its static prefix and everything beneath it. A route
    without an inspectable absolute path, or one that would reserve the whole site, is refused."""
    path = getattr(route, "path", None)
    if not isinstance(path, str) or not path.startswith("/") or "//" in path or ".." in path.split("/"):
        raise DomainError("invalid_installation", f"{owner} has a route without an inspectable absolute path")
    subtree, static = bool(getattr(route, "prefix", False)) or isinstance(route, Mount), []
    for segment in path.split("/")[1:]:
        if "{" in segment or "}" in segment:
            if not (segment.startswith("{") and segment.endswith("}")):
                raise DomainError("invalid_installation", f"{owner} route {path} mixes a parameter into a path segment")
            subtree = True
            break
        static.append(segment)
    claim = "/" + "/".join(static)
    claim = claim.rstrip("/") if subtree else claim
    if subtree and not claim:
        raise DomainError("invalid_installation", f"{owner} route {path} would reserve the whole site")
    return claim, subtree


def within(path, prefix):
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def overlaps(first, second):
    (path, subtree), (other, other_subtree) = first, second
    if subtree and other_subtree:
        return within(path, other) or within(other, path)
    if subtree or other_subtree:
        return within(other, path) if subtree else within(path, other)
    return path == other


def reserve_extension_routes(extensions, contributions, core_routes):
    """Refuse an extension route that overlaps core's namespaces or pages (taken from the routes core serves) or another
    route, and a frame rule that the same contribution's own routes do not cover. Coverage keeps framing inside the route
    space the contribution won, so it cannot reach core's or another extension's paths."""
    claimed = [("core " + namespace, (namespace, True)) for namespace in CORE_NAMESPACES]
    claimed += [("core " + route.path, route_claim("core", route)) for route in core_routes]
    for extension, contribution in zip(extensions, contributions):
        owner, own = "Extension " + extension.id, []
        for route in contribution.routes:
            claim = route_claim(owner, route)
            for name, other in claimed:
                if overlaps(claim, other):
                    raise DomainError("invalid_installation", f"{owner} route {claim[0]} overlaps {name}")
            claimed.append((owner + " route " + claim[0], claim))
            own.append(claim)
        for rule in contribution.framing:
            if not any(within(rule.path, path) if subtree else not rule.prefix and rule.path == path for path, subtree in own):
                raise DomainError("invalid_installation", f"{owner} frame rule {rule.path} is not covered by its own routes")


class ServiceBoundary:
    def __init__(self, app, origins, *, frame_rules=()):
        self.app, self.origins = app, origins
        # Exact paths and prefixes the selected extensions may be framed on, each with its one allowed origin.
        self.frame_rules = tuple(frame_rules)

    # The standalone owner viewers the Viewbox shows in a same-origin frame. Owner APIs, downloads and every other path
    # stay unframeable, and there is no prefix rule for either viewer.
    SAME_ORIGIN_FRAMED = frozenset({FILE_VIEWER_PATH, "/tables/view"})

    def framed(self, path):
        """The allowed frame origin of a path an extension declared framable, else None."""
        for rule in self.frame_rules:
            if path == rule.path or (rule.prefix and path.startswith(rule.path + "/")):
                return rule.origin
        return None

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
                frame_origin = self.framed(path)
                if frame_origin:
                    framing = [(b"content-security-policy", b"frame-ancestors " + frame_origin.encode("ascii"))]
                elif path in self.SAME_ORIGIN_FRAMED:
                    # Only the standalone file/table viewers may sit in a same-origin frame. Owner APIs, downloads and
                    # every other path stay unframeable; there is no prefix rule.
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
                    operation = scope.get("qorqut.transport_operation", "")
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
                        etag_desc = scope.get("qorqut.etag")
                        timing = ", ".join((
                            f"total;dur={scope.get('qorqut.total_ms', 0.0):.1f}",
                            f"db;dur={scope.get('qorqut.db_ms', 0.0):.1f}",
                            f"units;dur={scope.get('qorqut.units', 0)}",
                            f"body;dur={scope.get('qorqut.response_bytes', 0)}",
                            # Sums over the request's storage units, in ms; units can nest or overlap, so they are
                            # neither SQL time nor a share of `total`.
                            f"db_wait;dur={scope.get('qorqut.db_wait_ms', 0.0):.1f}",
                            f"db_read;dur={scope.get('qorqut.db_read_ms', 0.0):.1f}",
                            f"db_write;dur={scope.get('qorqut.db_write_ms', 0.0):.1f}",
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
    `HTTPAPI._dispatch` uses to route to it (`qorqut/transport/http.py`,
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
        if path in ("/", "/app.css") or path.startswith(("/js/", "/css/", "/extensions/")):
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
               agent_only=False):
    origins = OriginPolicy(origins)
    viewbox = getattr(getattr(runtime, "domains", None), "viewbox", None)
    if viewbox is not None:
        viewbox.configure_origins(origins.origins)
    authentication = authentication or Authentication(runtime.database, runtime.paths, bootstrap_token=bootstrap_token,
                                                      integration_adapter=runtime.selection.integration_adapter)
    api = HTTPAPI(runtime, authentication, origins, compatibility=compatibility, terminals=terminals)
    mcp = MCPTransport(runtime, authentication, origins)
    owner_mcp = None
    if not agent_only:
        from qorqut.transport.owner_mcp import OwnerSessionCaller
        owner_mcp = MCPTransport(runtime, authentication, origins,
                                 caller=OwnerSessionCaller(runtime, authentication))
    if dashboard_root is None:
        from qorqut.installation import component
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
                if manage_lifecycle:
                    async def prepare_agent_browsers():
                        from qorqut.browser.setup import ensure_all
                        backoff = {}
                        while True:
                            try:
                                await anyio.to_thread.run_sync(ensure_all, runtime, backoff, abandon_on_cancel=True)
                            except Exception:
                                logger.exception("Automatic agent browser setup failed")
                            await anyio.sleep(120)

                    browser_tasks = await stack.enter_async_context(anyio.create_task_group())
                    browser_tasks.start_soon(prepare_agent_browsers)
                yield
                if manage_lifecycle:
                    browser_tasks.cancel_scope.cancel()
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
            from qorqut.installation import app_manifest
            manifest=app_manifest()
            return response(OperationResult.ok({"ready": True, "service": "qorqut",
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
            "/api/agent/delegation/browser"]
        routes = [Route(path, agent_request, methods=["POST"]) for path in agent_paths]
    else:
        routes = [Route("/", index), Route("/tables/view", tables_view), Route("/app.css", app_css), Route(FILE_VIEWER_PATH, file_viewer_page),
            Route("/health/live", liveness), Route("/health/ready", readiness),
            Route("/api/{path:path}", api_request, methods=["GET", "POST", "DELETE"]),
            Mount("/icons/task-types", StaticFiles(directory=dashboard_root / "icons" / "task-types", check_dir=False)),
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
    mcp_routes = [MCPRoute(mcp, {"/mcp", "/mcp/"})]
    if owner_mcp is not None:
        mcp_routes.append(MCPRoute(owner_mcp, {"/mcp/owner", "/mcp/owner/"}))
    # The selected extensions' ASGI routes (an embedded proxy, a canvas) go before the wildcard API route. A route
    # contribution grants no authority: each extension handler applies the owner and bearer checks it needs.
    contributions = [extension.web(api, agent_only=agent_only) for extension in runtime.selection.extensions]
    extension_routes = [route for contribution in contributions for route in contribution.routes]
    reserve_extension_routes(runtime.selection.extensions, contributions, routes)
    frame_rules = [rule for contribution in contributions for rule in contribution.framing]
    asset_routes = []
    if not agent_only:
        async def extension_asset(request):
            """A declared, package-owned asset beneath /extensions/<id>/assets/ only; read-only, no directory mount."""
            path = runtime.selection.asset_file(request.path_params["extension_id"], request.path_params["asset_name"])
            if path is None:
                return failure(DomainError("not_found", "Endpoint is not registered"))
            kind = "text/javascript" if path.suffix == ".js" else "text/css" if path.suffix == ".css" else "application/octet-stream"
            return FileResponse(path, media_type=kind, headers={"Cache-Control": "no-cache"})
        asset_routes = [Route("/extensions/{extension_id}/assets/{asset_name}", extension_asset, methods=["GET", "HEAD"])]

    application = Starlette(routes=[*mcp_routes, *extension_routes, *asset_routes, *routes], lifespan=lifespan)
    application.state.runtime = runtime
    application.state.authentication = authentication
    application.state.mcp = mcp
    application.state.owner_mcp = owner_mcp
    application.state.http_api = api
    application.add_middleware(ServiceBoundary, origins=origins, frame_rules=frame_rules)
    # Added after ServiceBoundary: Starlette's add_middleware inserts at the
    # front of its stack, and the stack is built outermost-last, so the
    # middleware added last ends up outermost. Gzip must wrap ServiceBoundary
    # (not the reverse) so it compresses the final bytes, security headers
    # included, rather than something ServiceBoundary re-wraps afterward.
    application.add_middleware(SelectiveGZip)
    return application
