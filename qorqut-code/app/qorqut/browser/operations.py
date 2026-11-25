"""MCP registration of the restricted browser surface: the agents' browser, managed Global desktops and assignment turns."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from .agent_browsers import ACTIONS

from qorqut_operations.application import OperationDescriptor


BROWSER_OPERATION = "browser_turn"
AGENT_BROWSER_MANAGE = "agent_browser_manage"
WEBSITE_SIGN_IN = "website_sign_in"


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
                                 "release", "artifact", "reconcile", "list"]},
            "protocol": {"enum": ["handles", "legacy"], "default": "legacy", "description": instruction_text('operations-use-handles-on-acquire-for-compact-replies-and')},
            "turn_id": {**_UUID, "description": instruction_text('operations-legacy-callers-only-omit-when-using-handle')},
            "lease_id": {**_UUID, "description": instruction_text('operations-legacy-callers-only-omit-when-using-handle')},
            "turn_token": {"type": ["string", "null"], "maxLength": 128, "description": instruction_text('operations-legacy-callers-only-omit-when-using-handle')},
            "consumer_id": {**_UUID, "description": instruction_text('operations-optional-uuid-of-your-own-choosing-given-on')},
            "browser_id": {"type": ["string", "null"], "pattern": "^[a-z0-9][a-z0-9-]{0,62}$", "description": instruction_text('operations-ordubashy-only-on-acquire-with-protocol-handles-the')},
            "browser_key": {"type": ["string", "null"], "maxLength": 200, "description": instruction_text('operations-durable-workers-use-the-exact-assignment-target-native')},
            "handle": {"type": ["string", "null"], "pattern": "^b[a-f0-9]{12}$", "description": instruction_text('operations-short-browser-handle-returned-by-acquire-use-it')},
            "action_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200, "description": instruction_text('operations-stable-action-identity-reused-exactly-for-reconciliation-defaults')}, "command": {"type": ["array", "null"], "items": {"type": "string", "maxLength": 2000}, "maxItems": 32,
                        "description": "One command, each word its own item. Commands: read, observe, snapshot, get, click, dblclick, "
                                       "fill, type, press, hover, focus, check, uncheck, select, upload, drag, find, scroll, wait, "
                                       "goto, go-back, go-forward, reload, tab-list, tab-new, tab-select, tab-close, frame, eval, "
                                       "screenshot, pdf, console, resize, dialog-accept, dialog-dismiss, captcha, tab-reference. The browser skill "
                                       "gives their arguments."},
            "commands": {"type": ["array", "null"], "minItems": 1, "maxItems": 10,
                         "items": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "string", "maxLength": 2000}},
                         # A plain string, not a tool-instructions key: the wording ships with the code that enforces it.
                         "description": "Several commands in one call, 1 to 10, in place of command; give one of the two. Not for "
                                        "a delegated assignment, which runs one command per call. They run in order and the run "
                                        "stops at the first command that exits non-zero, times out or cannot start, or when the "
                                        "request's time is nearly used up. Every command is checked first: if one is refused, none runs. The reply has results (one entry per command that "
                                        "ran: command, exit_code, stdout, stderr when not empty, truncated when stdout was cut) and, when the run stopped, "
                                        "stopped (index, reason, message, not_run). A timeout is reported in stopped, not "
                                        "raised: look at the page before repeating anything."},
            "checkpoint": {"type": ["object", "null"]}, "last_action": _TEXT,
            "pending_outcome": {"type": ["object", "null"]}, "next_action": _TEXT,
            "lease_seconds": {"type": ["integer", "null"], "minimum": 30, "maximum": 900},
            "reason": _TEXT, "artifact_handle": {"type": ["string", "null"], "maxLength": 1000},
            "offset": {"type": ["integer", "null"], "minimum": 0},
            "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 65536},
        },
        "required": ["action"],
    }, version=2, effect_kind="records", supports_signoff=False, scope_kinds=frozenset({"global"}),
        # Every operable Global agent holds read_global_context; the role's browser_turn policy and browser-control skill
        # are the actual gate. Browser use is never a delegation capability.
        exposure=frozenset({"mcp"}), global_capability="read_global_context",
        resolve_namespace=resolve_namespace,
        description=instruction_text('operations-use-a-browser-over-trusted-mcp-native-callers'),
        prepare=unavailable, apply=unavailable))
    return runtime


def register_agent_browser_management(runtime):
    """``agent_browser_manage``: Ordubashy's standing authority over the browser the agents use, MCP only.

    Like ``browser_turn`` it is dispatched by the authenticated MCP bridge, which authorizes it in a short read unit and then
    runs the host effects outside any database transaction. Who may call it is decided by the host policy rule and the
    manager's own check that the caller is the configured Ordubashy, never by a role file or a caller-supplied value."""
    def resolve_namespace(_context, _arguments):
        return "global"

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("agent_browser_manage must be dispatched by the authenticated MCP bridge")

    runtime.registry.register(OperationDescriptor(AGENT_BROWSER_MANAGE, {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"enum": list(ACTIONS)},
        },
        "required": ["action"],
    }, version=1, effect_kind="records", supports_signoff=False, scope_kinds=frozenset({"global"}),
        exposure=frozenset({"mcp"}), global_capability="read_global_context", resolve_namespace=resolve_namespace,
        description=instruction_text('operations-set-up-diagnose-repair-restart-or-stop-an'),
        prepare=unavailable, apply=unavailable))
    runtime.registry.register(OperationDescriptor(WEBSITE_SIGN_IN, {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"enum": ["open", "status", "sign_out"]},
            "site": {"type": "string", "maxLength": 300, "description": "A website such as slack.com, or its https sign-in address. Required for open and sign_out."},
        },
        "required": ["action"],
    }, version=1, effect_kind="records", supports_signoff=False, scope_kinds=frozenset({"global"}),
        exposure=frozenset({"mcp"}), global_capability="read_global_context", resolve_namespace=resolve_namespace,
        description="Connect the owner's account on a website through the agent browser (Ordubashy only). Use this when the owner "
                    "asks to connect a site such as Slack, or when work needs their account there. Start here, not with browser_turn or "
                    "another browser tool. First say in Chat exactly which "
                    "website you are opening, then call open: the site's sign-in page opens in the Shared browser and nothing is paused. "
                    "Tell the owner to open the Browser tab the reply names in the dashboard and sign in there themselves, then wait "
                    "until they say it is done, then open the site and check. Only if the owner cannot use the live view, ask for the "
                    "password or code you do not have, type it yourself, and never repeat it or write it anywhere else. status lists "
                    "connected sites; session_saved only means the site left cookies, and many sites do that before login, so open "
                    "the site to confirm the owner is really signed in. sign_out removes one. Before sending or "
                    "changing anything on a signed-in site, get the owner's approval in a Signoff. Nothing else enforces that.",
        prepare=unavailable, apply=unavailable))
    return runtime
