"""Official stateless MCP transport over the same immutable operation registry."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import mcp as sdk
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response

from ..contracts import OperationResult, canonical
from .boundary import origins
from .configuration import MAX_INPUT_BYTES


def install_logging_boundary():
    """Sanitize SDK diagnostics before any handler formats them; never on import.

    MCP 1.30.0 also logs through the root logger in shared.session, so matching
    only logger names misses malformed requests and notifications. Keep levels,
    handlers, provenance and unrelated factory behavior intact.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_qorqut_sdk_boundary", False):
        return
    sdk_root = Path(sdk.__file__).parent

    def private_record(*args, **kwargs):
        record = previous(*args, **kwargs)
        if (record.name == "mcp" or record.name.startswith("mcp.")
                or Path(record.pathname).is_relative_to(sdk_root)):
            record.msg = "MCP transport diagnostic (payload omitted)"
            record.args = ()
            record.message = record.msg
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    private_record._qorqut_sdk_boundary = True
    logging.setLogRecordFactory(private_record)


class MCP:
    def __init__(self, dispatcher, registry, port: int):
        self.server = Server("qorqut-core", version="0.1.0")
        hosts, allowed_origins = origins(port)
        self.manager = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True,
            max_request_body_size=MAX_INPUT_BYTES,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=hosts, allowed_origins=allowed_origins))

        @self.server.list_tools()
        async def list_tools():
            return [types.Tool(name=op.name, description=op.name.replace("_", " "),
                inputSchema=op.arguments.model_json_schema(), outputSchema=OperationResult.model_json_schema(),
                annotations=types.ToolAnnotations(readOnlyHint=not op.mutation)) for op in registry.values()]

        # Domain validation belongs to Application.invoke. SDK protocol validation
        # remains enabled, including validation of the tools/call message itself.
        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            context = self.server.request_context.request.scope["qorqut.invocation"]
            result = await dispatcher.invoke(name, context, arguments)
            return types.CallToolResult(structuredContent=result.model_dump(mode="json"),
                content=[types.TextContent(type="text", text=canonical(result))], isError=not result.ok)

    async def __call__(self, scope, receive, send):
        # JSON response mode permits sanitizing SDK protocol diagnostics without
        # changing its validation decisions, error codes, IDs or HTTP statuses.
        start = None
        chunks = []

        async def safe_send(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            headers = dict(start["headers"])
            if body and headers.get(b"content-type", b"").startswith(b"application/json"):
                payload = json.loads(body)
                if isinstance(payload, dict) and "error" in payload:
                    code = payload["error"].get("code", types.INTERNAL_ERROR)
                    payload["error"] = {"code": code, "message": "MCP protocol request failed"}
                    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
                elif isinstance(payload, dict) and isinstance(payload.get("result"), dict):
                    result = payload["result"]
                    if result.get("isError") and "structuredContent" not in result:
                        result["content"] = [{"type": "text", "text": "MCP tool request failed"}]
                        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
            response = Response(body, status_code=start["status"], headers={
                k.decode("latin-1"): v.decode("latin-1") for k, v in headers.items() if k != b"content-length"})
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)

        await self.manager.handle_request(scope, receive, safe_send)
