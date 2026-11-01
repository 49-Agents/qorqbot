"""MCP registration for the restricted assignment-bound browser surface."""

from qorqut_operations.application import OperationDescriptor


BROWSER_OPERATION = "browser_turn"


_UUID = {"type": ["string", "null"], "format": "uuid"}
_TEXT = {"type": ["string", "null"], "maxLength": 2000}


def register_browser_operation(runtime):
    def resolve_namespace(_context, _arguments):
        return "global"

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("browser_turn must be dispatched by the authenticated MCP bridge")

    runtime.registry.register(OperationDescriptor(BROWSER_OPERATION, {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"enum": ["acquire", "status", "command", "checkpoint", "renew",
                                 "release", "artifact", "reconcile"]},
            "protocol": {"enum": ["handles", "legacy"], "default": "legacy", "description": "Use handles on acquire for compact replies and server-held credentials. Default legacy preserves unrefreshed callers."},
            "turn_id": {**_UUID, "description": "Legacy callers only; omit when using handle."},
            "lease_id": {**_UUID, "description": "Legacy callers only; omit when using handle."},
            "turn_token": {"type": ["string", "null"], "maxLength": 128, "description": "Legacy callers only; omit when using handle."},
            "consumer_id": {**_UUID, "description": "Native caller session UUID, supplied on acquire (or a handle-less command). Later calls use the returned handle."},
            "business_id": {**_UUID, "description": "Omit for your own configured browser (the default), including all business work. The configured Ordubashy or granted Global Marketing may name an authorized business here, and only on acquire with protocol=handles; it selects that business's shared browser under an exclusive lease. Later calls use the returned handle and cannot change the business. Durable workers use their assignment scope."},
            "browser_key": {"type": ["string", "null"], "maxLength": 200, "description": "Durable workers use the exact assignment target. Native helpers should omit this field; the server selects the parent configured browser."},
            "handle": {"type": ["string", "null"], "pattern": "^b[a-f0-9]{12}$", "description": "Short browser handle returned by acquire; use it for subsequent actions. Configured-browser handles never expire; a business handle lasts as long as its lease. Server retains lease credentials."},
            "action_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200, "description": "Stable action identity, reused exactly for reconciliation; defaults to the command idempotency_key."}, "command": {"type": ["array", "null"], "items": {"type": "string", "maxLength": 2000}, "maxItems": 32},
            "checkpoint": {"type": ["object", "null"]}, "last_action": _TEXT,
            "pending_outcome": {"type": ["object", "null"]}, "next_action": _TEXT,
            "lease_seconds": {"type": ["integer", "null"], "minimum": 30, "maximum": 900},
            "reason": _TEXT, "artifact_handle": {"type": ["string", "null"], "maxLength": 1000},
            "offset": {"type": ["integer", "null"], "minimum": 0},
            "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 65536},
        },
        "required": ["action"],
    }, version=2, effect_kind="records", supports_signoff=False, scope_kinds=frozenset({"global"}),
        exposure=frozenset({"mcp"}), global_capability="manage_delegated_assignments",
        resolve_namespace=resolve_namespace,
        description="Use a browser over trusted MCP. Native callers use their own configured browser lease-free: acquire returns a handle at once, command runs immediately, and release is a no-op; nothing waits or needs reconciling. A timed-out command returns an error: inspect the page before repeating a state-changing action. The configured Ordubashy or granted Global Marketing may instead acquire an authorized business's shared browser with business_id and protocol=handles: that lease is exclusive with the business's Onbashy, Dev and durable workers, a busy browser is refused (never taken), and the handle needs renew, release, and reconcile of an uncertain action before retry. Durable workers use an exclusive turn in their assignment scope. Acquire with protocol=handles, then use handle for command/release. Handle callers never pass lease IDs or tokens. Waiting releases execution capacity and never grants shell or arbitrary network access.",
        prepare=unavailable, apply=unavailable))
    return runtime
