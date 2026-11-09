"""The owner's exact approval, enforced in code, before an agent's Claude connector call changes anything.

The agent's PreToolUse hook sends every connector call that is not a plain read here. Qorqbot writes the Signoff itself from the
blocked call, so the agent cannot describe it differently from what would run. One approval opens one identical call by the
same agent, for a limited time.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading

from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain, utc_now
from qorqut_operations.storage.repositories import decode_row
from qorqut.agent_trust import _atomic_write
from qorqut.management.identity import installation_owner

TOOL = re.compile(r"^mcp__claude_ai_([A-Za-z0-9_-]{1,80}?)__([A-Za-z0-9_-]{1,120})$")
APPROVAL_LIFETIME = timedelta(hours=24)
MAX_SHOWN = 7000
USED_FILE = "connector-gate-used.json"
_lock = threading.Lock()


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def decide(rows, owner, used, now):
    """What the newest unused request for one exact call says: ``(outcome, row)``, newest first in ``rows``."""
    for row in rows:
        if row["id"] in used:
            continue
        if row["state"] == "pending":
            return "pending", row
        if row["state"] != "answered":
            continue
        decision = row["decision_json"] or {}
        answered = _parse_time(decision.get("at"))
        if answered is None or now - answered > APPROVAL_LIFETIME or not owner or decision.get("by") != owner:
            continue
        return ("approved" if decision.get("selected_choices") == ["approve"] else "declined"), row
    return "none", None


def _read_used(path, now):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # An approval expires, so a used record older than twice its lifetime can no longer matter.
    return {key: value for key, value in data.items()
            if (at := _parse_time(value)) is not None and now - at <= APPROVAL_LIFETIME * 2}


FIRST_FIELDS = ("to", "cc", "bcc", "channel", "recipient", "subject", "title", "text", "message", "body")


def _plain_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(item, str) and "," not in item for item in value):
        return ", ".join(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def readable(tool_input):
    """The exact submission as labeled lines. Later lines of a value are indented, so a value cannot pass as another field."""
    order = sorted(tool_input, key=lambda key: (FIRST_FIELDS.index(key.lower()) if key.lower() in FIRST_FIELDS else len(FIRST_FIELDS), key))
    fields = [f"{key.replace('_', ' ').capitalize()}: " + _plain_value(tool_input[key]).replace("\n", "\n    ") for key in order]
    return "\n".join(fields) or "(no details)"


def _form(service, action, shown):
    return {"title": f"Approve {service}: {action}"[:200], "kind": "request", "selection_mode": "single",
            "response_policy": "owner_only", "selection_required": True,
            "explanation": f"An agent wants to use {service} ({action}). Nothing happens unless you approve. "
                           f"Exactly what it will submit:\n\n{shown}",
            "choices": [{"id": "approve", "label": "Approve", "explanation": "Allow this exact action once."},
                        {"id": "decline", "label": "Decline", "explanation": "Do not do this."}]}


def _deny(reason):
    return {"decision": "deny", "reason": reason}


def check(runtime, context, tool_name, tool_input):
    """``{"decision": "allow"}``, or ``{"decision": "deny", "reason": ...}`` telling the agent what to do next."""
    match = TOOL.fullmatch(tool_name) if isinstance(tool_name, str) else None
    if match is None or not isinstance(tool_input, dict):
        raise DomainError("validation_error", "This is not a Claude connector call")
    service, action = match.group(1).replace("_", " "), match.group(2).replace("_", " ")
    shown = readable(tool_input)
    if len(shown) > MAX_SHOWN:
        return _deny(f"This {service} action is too long for the owner to review in a Signoff, so it was blocked. "
                     "Shorten it and try again.")
    request_key = "connector-gate:" + canonical_hash({"tool": tool_name, "input": plain(tool_input)})
    path = Path(runtime.paths.runtime) / USED_FILE
    with _lock:
        now = datetime.now(timezone.utc)
        used = _read_used(path, now)
        with runtime.database.read() as uow:
            owner = installation_owner(uow)
            found = uow.connection.execute(
                "SELECT * FROM signoffs WHERE proposer_principal_id=? AND request_key=? AND category='input_request' "
                "AND superseded_by_id IS NULL ORDER BY created_at DESC LIMIT 20",
                (context.principal_id, request_key)).fetchall()
        outcome, row = decide([decode_row(item) for item in found], owner, used, now)
        if outcome == "approved":
            # Recorded before the call is released: a lost reply needs a new approval rather than risking a second send.
            _atomic_write(path, json.dumps({**used, row["id"]: utc_now()}).encode(), mode=0o600)
            return {"decision": "allow"}
        if outcome == "declined":
            return _deny(f"The owner did not approve this {service} action (Signoff {row['id']}). Do not try it another "
                         "way. Read their response with signoffs_get.")
        if outcome == "none":
            result = runtime.application.invoke(context, "signoffs_input_create",
                                                {"request_key": request_key, "scope": "global",
                                                 "form": _form(service, action, shown)},
                                                {"idempotency_key": "connector-gate:" + new_id()})
            if result.status != "ok":
                return _deny(f"This {service} action was blocked because Qorqbot could not ask the owner to approve it. "
                             "Tell the owner and try again later.")
            signoff = result.data["signoff"]["id"]
        else:
            signoff = row["id"]
    return _deny(f"This {service} action needs the owner's approval, so it was blocked. Signoff {signoff} shows them exactly "
                 "what it will submit. Tell the owner in Chat, and do something else until they answer. After they approve, "
                 "make this identical call again. A changed call needs a new approval.")
