"""The identity-only references of ``request_user_input``: one pure contract for the tool, the archive projection and the owner view.

A reference names an existing Task, Signoff, Project, configured browser, delta or open browser tab by
identity only. It carries no label, state, URL or decision; those are resolved from current records at the owner boundary,
never taken from the model. Nothing here reads a database, a configuration or an archive."""

from __future__ import annotations

import re
from typing import Any

TYPE_TASK, TYPE_SIGNOFF, TYPE_BROWSER, TYPE_PROJECT, TYPE_DELTA, TYPE_TAB = (
    "task", "signoff", "browser", "project", "delta", "tab")
TYPES = (TYPE_TASK, TYPE_SIGNOFF, TYPE_BROWSER, TYPE_PROJECT, TYPE_DELTA, TYPE_TAB)
# The agent reference ({type: "agent", id}) is retired: the tool refuses it, and an archived presentation that carries one
# is still shown, without it.
RETIRED_TYPES = ("agent",)
VERSION = 1
MAX_MESSAGE_CHARS = 4000
MAX_REFERENCES = 8
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A Task id has no fixed form: a short generated code (``qorqut_operations.tasks.identity``), or a UUID or slug on an older
# Task. It is held to what any stored id is: at most 200 characters, no blanks or control characters.
TASK_ID = re.compile(r"^[^\s\x00-\x1f\x7f-\x9f]{1,200}$")
# The configured browser surface IDs of ``browser-surfaces.json`` (``qorqut.service.browser_surfaces.SURFACE_ID``).
SURFACE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# A tab of an agent's managed Chrome: the agent's UUID, a colon and Chrome's own id of the tab (``browser.live.TAB_ID``: 32
# uppercase hex digits). The ``tab-reference`` browser command prints exactly this.
TAB = re.compile(UUID.pattern[:-1] + r":[0-9A-F]{32}$")

# A reference as a hashable key: (type, id, project_id).
Key = tuple[str, str, "str | None"]


class InputError(ValueError):
    """The input is not a valid ``request_user_input`` argument; the message is safe to show the caller."""


def _reference(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"type", "id", "project_id"} or not {"type", "id"} <= set(raw):
        raise InputError("Each reference is {type, id} with an optional project_id")
    kind, identity, project = raw["type"], raw["id"], raw.get("project_id")
    if kind == TYPE_PROJECT and "project_id" not in raw:
        project = identity  # A Project names itself.
    if kind not in TYPES:
        raise InputError("Reference type must be task, signoff, browser, project, delta or tab")
    pattern = SURFACE_ID if kind == TYPE_BROWSER else TASK_ID if kind == TYPE_TASK else TAB if kind == TYPE_TAB else UUID
    if not isinstance(identity, str) or not pattern.fullmatch(identity):
        raise InputError("A task reference id is its Task id; a signoff, project or delta reference id is its UUID; a "
                         "browser reference id is a managed Global browser id from browser_turn list, never an agent ID; a tab "
                         "reference id is the reference printed by the browser command tab-reference")
    if kind in (TYPE_DELTA, TYPE_TAB) and project is not None:
        raise InputError("A delta or tab reference is {type, id} only; its project_id is absent or null")
    if project is not None and (not isinstance(project, str) or not UUID.fullmatch(project)):
        raise InputError("project_id is the record's owning project UUID, or null for Global")
    if kind == TYPE_PROJECT and project != identity:
        raise InputError("A project reference is {type: project, id: PROJECT_UUID}; a stated project_id is that same UUID")
    return {"type": kind, "id": identity, "project_id": project}


def tab_parts(identity: str) -> tuple[str, str]:
    """The agent's UUID and the Chrome tab id of a valid ``tab`` reference id."""
    agent, _, tab = identity.partition(":")
    return agent, tab


def key(reference: dict) -> Key:
    return reference["type"], reference["id"], reference["project_id"]


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
    evidence, so anything that is not that exact shape (extra fields, another version, a bad reference) is not a presentation.
    A reference of a retired type, as the tool once returned it, is left out and the rest is kept."""
    if not isinstance(value, dict) or set(value) != {"version", "message", "references"} or value["version"] != VERSION:
        return None
    stored = value["references"]
    if not isinstance(stored, list) or len(stored) > MAX_REFERENCES:
        return None
    kept = [raw for raw in stored if not _retired(raw)]
    try:
        normal = normalize(value["message"], kept)
    except InputError:
        return None
    # The tool coalesces duplicates and trims, so a faithful result is already in that form.
    if normal["message"] != value["message"] or normal["references"] != kept:
        return None
    return normal


def _retired(raw: Any) -> bool:
    """A stored reference of a retired type in the exact form the tool returned: ``{type, id, project_id: null}``, a UUID id."""
    return (isinstance(raw, dict) and set(raw) == {"type", "id", "project_id"} and raw["type"] in RETIRED_TYPES
            and isinstance(raw["id"], str) and bool(UUID.fullmatch(raw["id"])) and raw["project_id"] is None)


def unavailable(kind: str, identity: str, project_id: str | None) -> dict:
    """A reference whose target is missing, deleted or not readable: its identity only, no target details."""
    return {"type": kind, "id": identity, "project_id": project_id, "label": f"Unavailable {kind}", "available": False}
