"""Standalone browser MCP and private HTTP service; imports have no effects."""

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from uuid import UUID

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import McpError
from starlette.responses import JSONResponse
from starlette.routing import Route, Router
import uvicorn

from .config import ServiceConfig, load_config
from .contracts import BrowserError, Command, Receipt, Reconcile, canonical
from .control import Gate
from .handoff import HandoffClient, HandoffReceipt, InputRequest
from .state import Store
from .supervisor import check_ready
from .tools import AgentBrowser
from .transport import (BlockingWork, MAX_BODY_BYTES, PRINCIPAL_SCOPE_KEY, LocalTransport, Principal,
                        error_value, failure, sanitize_sdk_logs)

ACTION_NAMES = ("snapshot", "navigate", "click", "fill", "screenshot")
IDENTITY_URI = "browser://identity"


class _MCPApplication:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


def create_app(config: ServiceConfig, *, readiness=check_ready, executor=None):
    """Tests may inject the accepted seams; the CLI has no fake-mode settings."""
    handoff = (HandoffClient(config.handoff)
               if config.handoff is not None else None)
    store = Store(config.browser)
    effect = executor if executor is not None else AgentBrowser(
        config.browser, config.helper_executable, readiness=readiness)
    gate = Gate(store, effect, readiness=readiness)
    work = BlockingWork()
    sanitize_sdk_logs()
    mcp = Server("qorqut-browser", version="0.1.0")
    host = f"127.0.0.1:{config.browser.service_port}"
    manager = StreamableHTTPSessionManager(mcp, stateless=True, json_response=True,
        max_request_body_size=MAX_BODY_BYTES,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[host], allowed_origins=[f"http://{host}"]))

    @asynccontextmanager
    async def lifespan(app):
        # Existing state only. startup fences prior service identity and leaves
        # admission closed, even when the previous process was fully ready.
        try:
            await work.run(gate.startup)
        except Exception:
            raise RuntimeError("Browser service could not open its provisioned state") from None
        async with manager.run():
            yield

    async def identity():
        control = await work.run(store.status)
        return {"protocol": 1, "identity": config.browser.binding.identity.model_dump(mode="json"),
                "control": control.model_dump(mode="json"), "actions": list(ACTION_NAMES),
                "handoff_enabled": handoff is not None}

    def receipt(command_id):
        with store.connection() as db:
            saved = Store.get_receipt(db, command_id)
        if saved is None:
            raise BrowserError("not_found", "Unknown command")
        return saved

    async def http_identity(request):
        return JSONResponse(await identity())

    async def http_command(request):
        command = Command.model_validate_json(await request.body())
        principal = request.scope[PRINCIPAL_SCOPE_KEY]
        result = await work.run(gate.execute, command, actor_id=principal.actor_id)
        return JSONResponse(result.model_dump(mode="json"))

    async def http_receipt(request):
        saved = await work.run(receipt, UUID(request.path_params["command_id"]))
        return JSONResponse(saved.model_dump(mode="json"))

    async def http_control(request):
        principal = request.scope[PRINCIPAL_SCOPE_KEY]
        reconcile = Reconcile.model_validate_json(await request.body())
        result = await work.run(gate.reconcile, reconcile, actor_id=principal.actor_id)
        return JSONResponse(result.model_dump(mode="json"))

    @mcp.list_resources()
    async def resources():
        return [types.Resource(uri=IDENTITY_URI, name="Browser identity", mimeType="application/json",
                               description="Bound browser and current control observation; each command is checked again.")]

    @mcp.read_resource()
    async def read_resource(uri):
        try:
            if str(uri) != IDENTITY_URI:
                raise BrowserError("not_found", "Unknown resource")
            return [ReadResourceContents(content=canonical(await identity()), mime_type="application/json")]
        except Exception as error:
            _, value = error_value(error)
            raise McpError(types.ErrorData(code=-32000, message=value["error"]["message"], data=value)) from None

    @mcp.list_tools()
    async def tools():
        inventory = [types.Tool(name="browser_turn", inputSchema=Command.model_json_schema(),
            outputSchema=Receipt.model_json_schema(), description=(
                "Take a snapshot, navigate, click, fill a field, or capture a screenshot in the bound browser. "
                "Use the exact browser/control binding. Retain the command ID, binding and action on retry; "
                "an uncertain receipt may represent an action that occurred and must not be replayed with a new ID. "
                "Large output is returned as owned artifact references."))]
        if handoff is not None:
            inventory.append(types.Tool(name="browser_request_input", inputSchema=InputRequest.model_json_schema(),
                outputSchema=HandoffReceipt.model_json_schema(), description=(
                    "Request an action from the user in your browser. Describe the needed action and retain "
                    "the same idempotency key and arguments on retry. A saved request does not prove the user "
                    "acted or that control is ready. Inspect the browser again after completion.")))
        return inventory

    @mcp.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            # The SDK carries each HTTP request into its own request context.
            # Never store a current actor on the server or a reusable session.
            request = mcp.request_context.request
            if request is None or PRINCIPAL_SCOPE_KEY not in request.scope:
                raise BrowserError("invalid_argument", "No request attribution")
            principal = request.scope[PRINCIPAL_SCOPE_KEY]
            if name == "browser_turn":
                command = Command.model_validate(arguments)
                result = await work.run(gate.execute, command, actor_id=principal.actor_id)
            elif name == "browser_request_input" and handoff is not None:
                intent = InputRequest.model_validate(arguments)
                result = await work.run(handoff.request, intent, actor_id=principal.actor_id,
                                        agent_id=principal.agent_id)
            else:
                raise BrowserError("unsupported_capability", "Tool unavailable")
            value = result.model_dump(mode="json")
            is_error = False  # a saved failed/uncertain receipt is a valid result
        except Exception as error:
            _, value = error_value(error)
            is_error = True
        return types.CallToolResult(content=[types.TextContent(type="text", text=canonical(value))],
                                    structuredContent=value, isError=is_error)

    async def unknown(scope, receive, send):
        await failure(BrowserError("not_found", "Unknown route"))(scope, receive, send)

    app = Router(routes=[
        # GET's optional server-initiated SSE stream is unsupported in this
        # stateless JSON service. Streamable HTTP permits a 405 for that path.
        Route("/mcp", endpoint=_MCPApplication(manager), methods=["POST", "DELETE"]),
        Route("/v1/identity", http_identity, methods=["GET"]),
        Route("/v1/browser/command", http_command, methods=["POST"]),
        Route("/v1/browser/commands/{command_id}", http_receipt, methods=["GET"]),
        Route("/v1/browser/control", http_control, methods=["POST"]),
    ], lifespan=lifespan, default=unknown, redirect_slashes=False)
    return LocalTransport(app, Principal(config.actor_id, config.agent_id), config.browser.service_port)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private browser MCP/HTTP service")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        app = create_app(config)
    except Exception:
        print("Browser service configuration is unavailable or invalid", file=sys.stderr)
        return 1
    uvicorn.run(app, host="127.0.0.1", port=config.browser.service_port, workers=1,
                reload=False, proxy_headers=False, access_log=False, server_header=False,
                log_level="warning", timeout_keep_alive=5, timeout_graceful_shutdown=60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
