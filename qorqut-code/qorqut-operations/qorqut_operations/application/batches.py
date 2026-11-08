"""Discovery contract for MCP orchestration, not a domain mutation handler."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from .registry import OperationDescriptor

NAME = "operations_batch"
MAX_OPERATIONS = 20
INPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["operations"], "properties": {
    "operations": {"type": "array", "minItems": 1, "maxItems": MAX_OPERATIONS, "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "operation", "arguments"], "properties": {
            "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$", "minLength": 1, "maxLength": 64,
                   "description": instruction_text('batches-unique-result-label-within-this-request')},
            "operation": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$", "maxLength": 100},
            "arguments": {"type": "object", "description": instruction_text('batches-exact-ordinary-mcp-arguments-each-write-needs-its')}}}},
    "on_non_ok": {"enum": ["stop", "continue"], "default": "stop",
        "description": instruction_text('batches-stop-on-error-pending-approval-or-async-acceptance')}}}


def descriptor():
    return OperationDescriptor(NAME, INPUT_SCHEMA, effect_kind="compound", supports_signoff=False,
        exposure=frozenset({"mcp"}), scope_kinds=frozenset({"agent_workspace"}), transport_only=True,
        external_read=True, description=(
            instruction_text('batches-execute-1-20-fully-specified-existing-qorqut-calls')))
