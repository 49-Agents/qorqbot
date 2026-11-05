"""Opt-in result revalidation, never a data cache or an authorization shortcut.

Every request executes the ordinary authorized read. A token describes that
fresh canonical result and its exact caller/session/query/view. Only repeated
delivery is suppressed; no database work, permission check or evidence is
inferred from a previously issued token. Restarting the transport resets tokens.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text
import base64
import hashlib
import hmac
import re
import secrets

from qorqut_operations.contracts import DomainError, canonical_bytes

# Keep this deliberately small. Focus supports board lists only: its item view
# can contain approval evidence or private conversations, which stay complete.
# Never suppress mandatory context, provider reads or activity freshness.
OPERATIONS = frozenset({"focus_get", "tasks_get", "tasks_list"})
TOKEN_PATTERN = r"^(?:|r1\.[A-Za-z0-9_-]{43})$"
TOKEN_SCHEMA = {"type": "string", "maxLength": 46, "pattern": TOKEN_PATTERN,
    "description": instruction_text('mcp-reads-optional-revalidation-focus-board-lists-only-send')}


def split(name, arguments, detail):
    arguments = dict(arguments)
    if "if_read_token" not in arguments:
        return arguments, None
    token = arguments.pop("if_read_token")
    if name not in OPERATIONS or detail == "text":
        raise DomainError("validation_error", "if_read_token supports JSON Focus/task reads only; never context, approvals or messages")
    if name == "focus_get" and (arguments.get("item_id") or arguments.get("conversation")):
        raise DomainError("validation_error", "Focus revalidation is for board lists only; item and conversation evidence must be read in full")
    if not isinstance(token, str) or re.fullmatch(TOKEN_PATTERN, token) is None:
        raise DomainError("validation_error", "if_read_token must be empty or an issued r1 token")
    return arguments, token


class ReadRevalidation:
    def __init__(self, *, key=None):
        self.key = key if key is not None else secrets.token_bytes(32)

    def present(self, *, requested, binding, canonical, presented):
        if requested is None or canonical.get("status") != "ok":
            return presented
        # Deliver attention signals in full, even if a caller echoes an older
        # token. Never turn warnings or refresh requirements into a cache hit.
        if canonical.get("warnings") or canonical.get("context_refresh_required"):
            return presented
        snapshot = {key: value for key, value in canonical.items() if key != "request_id"}
        digest = hmac.new(self.key, canonical_bytes({"version": 1, "binding": binding,
            "canonical": snapshot, "presented_data": presented.get("data")}), hashlib.sha256).digest()
        token = "r1." + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        unchanged = hmac.compare_digest(requested, token)
        if unchanged:
            # The caller already supplied this token. Do not echo another long
            # opaque string on every unchanged response; retain it client-side.
            return {**presented, "data": {"not_modified": True}, "read_state": {"unchanged": True}}
        return {**presented, "read_state": {"token": token, "unchanged": False}}
