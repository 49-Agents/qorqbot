"""The identity-only references of ``request_user_input``: one pure contract for the tool, the archive projection and the owner view.

A reference names an existing Task, Signoff, Project, configured browser or registered agent by identity only. It carries no label, state, URL or decision;
those are resolved from current records at the owner boundary, never taken from the model. Nothing here reads a database, a
configuration or an archive."""

from __future__ import annotations

import re
from typing import Any

TYPE_TASK, TYPE_SIGNOFF, TYPE_BROWSER, TYPE_AGENT, TYPE_PROJECT = "task", "signoff", "browser", "agent", "project"
TYPES = (TYPE_TASK, TYPE_SIGNOFF, TYPE_BROWSER, TYPE_AGENT, TYPE_PROJECT)
VERSION = 1
MAX_MESSAGE_CHARS = 4000
MAX_REFERENCES = 8
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# The configured browser surface IDs of ``browser-surfaces.json`` (``bazarlyk.service.browser_surfaces.SURFACE_ID``).
SURFACE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

# A reference as a hashable key: (type, id, business_id).
Key = tuple[str, str, "str | None"]


class InputError(ValueError):
    """The input is not a valid ``request_user_input`` argument; the message is safe to show the caller."""


def _reference(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"type", "id", "business_id"} or not {"type", "id"} <= set(raw):
        raise InputError("Each reference is {type, id} with an optional business_id")
    kind, identity, business = raw["type"], raw["id"], raw.get("business_id")
    if kind not in TYPES:
        raise InputError("Reference type must be task, signoff, browser, agent or project")
    pattern = SURFACE_ID if kind == TYPE_BROWSER else UUID
    if not isinstance(identity, str) or not pattern.fullmatch(identity):
        raise InputError("A task, signoff or agent reference id is its UUID; a browser reference id is a configured browser id from "
                         "browser_turn list")
    if kind == TYPE_AGENT and business is not None:
        raise InputError("An agent reference is {type, id} only; its business_id is absent or null")
    if business is not None and (not isinstance(business, str) or not UUID.fullmatch(business)):
        raise InputError("business_id is the record's owning business UUID, or null for Global")
    if kind == TYPE_PROJECT and business != identity:
        raise InputError("A project reference is {type: project, id: PROJECT_UUID, business_id: PROJECT_UUID}")
    return {"type": kind, "id": identity, "business_id": business}


def key(reference: dict) -> Key:
    return reference["type"], reference["id"], reference["business_id"]


def normalize(message: Any, references: Any = None) -> dict:
    """The presentation for a valid input, with identity-only references in order and exact duplicates coalesced; raises
    :class:`InputError` otherwise. The message keeps its text, trimmed of surrounding blanks."""
    if not isinstance(message, str) or not message.strip():
        raise InputError("A message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise InputError(f"The message is at most {MAX_MESSAGE_CHARS} characters")
    if references is None:
        references = []
    if not isinstance(references, list) or len(references) > MAX_REFERENCES:
        raise InputError(f"references is a list of at most {MAX_REFERENCES} references")
    found: dict[Key, dict] = {}
    for raw in references:
        reference = _reference(raw)
        found.setdefault(key(reference), reference)
    return {"version": VERSION, "message": message.strip(), "references": list(found.values())}


def presentation(value: Any) -> dict | None:
    """A stored ``user_input`` result re-validated to exactly the shape the tool returns, or ``None``. The archive is read-only
    evidence, so anything that is not that exact shape (extra fields, another version, a bad reference) is not a presentation."""
    if not isinstance(value, dict) or set(value) != {"version", "message", "references"} or value["version"] != VERSION:
        return None
    try:
        normal = normalize(value["message"], value["references"])
    except InputError:
        return None
    # The tool coalesces duplicates and trims, so a faithful result is already in that form.
    if normal["message"] != value["message"] or normal["references"] != value["references"]:
        return None
    return normal


def unavailable(kind: str, identity: str, business_id: str | None) -> dict:
    """A reference whose target is missing, deleted or not readable: its identity only, no target details."""
    return {"type": kind, "id": identity, "business_id": business_id, "label": f"Unavailable {kind}", "available": False}
