"""Discovery contract for MCP orchestration, not a domain mutation handler."""

from .registry import OperationDescriptor

NAME = "operations_batch"
MAX_OPERATIONS = 20
INPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["operations"], "properties": {
    "operations": {"type": "array", "minItems": 1, "maxItems": MAX_OPERATIONS, "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "operation", "arguments"], "properties": {
            "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$", "minLength": 1, "maxLength": 64,
                   "description": "Unique result label within this request."},
            "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "maxLength": 100},
            "arguments": {"type": "object", "description": "Exact ordinary MCP arguments. Each write needs its own stable idempotency_key; scope and response_detail belong here."}}}},
    "on_non_ok": {"enum": ["stop", "continue"], "default": "stop",
        "description": "Stop on error, pending approval or async acceptance. Continue only for independently justified entries; pending is not applied."}}}


def descriptor():
    return OperationDescriptor(NAME, INPUT_SCHEMA, effect_kind="compound", supports_signoff=False,
        exposure=frozenset({"mcp"}), scope_kinds=frozenset({"agent_workspace"}), transport_only=True,
        external_read=True, description=(
            "Execute 1–20 fully specified existing Qorqut calls in order in one MCP request. "
            "Use for multiple task creations or mixed operations; each keeps its own permissions, "
            "scope, approval and idempotency key. Not atomic: earlier successes remain on failure. "
            "Inspect every labeled result; retry unchanged writes with the same keys. No nested batches "
            "or result references. Stops starting new entries after 30 seconds; unfinished entries are explicit. "
            "Native parallel tool calls remain useful for independent reads already grouped by your client."))
