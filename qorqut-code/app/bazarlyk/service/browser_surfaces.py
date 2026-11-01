"""Operator-controlled owner viewer bindings for dedicated browser desktops.

`<businesses-root>/.bazarlyk/browser-surfaces.json` is deployment configuration,
like each workspace's browser.json. It only selects which exact viewer URL the
owner's agent pane shows. It grants no automation rights, and it never carries
cookies, profile data, passwords, CDP endpoints or executable hooks.
"""

import json
from pathlib import Path
import re
import uuid

from bazarlyk.contracts import DomainError
from bazarlyk.storage import safe_io

RELATIVE_PATH = ".bazarlyk/browser-surfaces.json"
SURFACE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
KINDS = frozenset({"browser", "desktop"})
MAX_BYTES = 65536
MAX_SURFACES = 64
MAX_AGENTS = 64
MAX_BROWSERS = 16


def _invalid(message):
    return DomainError("invalid_configuration", "Browser surface configuration is invalid: " + message)


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _invalid(name + " must be short plain text")
    return value


def _url(value, name):
    from bazarlyk.inbox.focus import valid_desktop_url
    if not isinstance(value, str) or len(value) > 2048 or not valid_desktop_url(value):
        raise _invalid(name + " must be an absolute http(s) URL without credentials")
    return value


def _dimension(value, name):
    if type(value) is not int or not 1 <= value <= 32768:
        raise _invalid(name + " must be an integer from 1 to 32768")
    return value


def _surface(identity, entry):
    if not isinstance(entry, dict) or set(entry) - {"label", "kind", "viewer_url", "width", "height"}:
        raise _invalid("surface " + identity + " has unsupported fields")
    if entry.get("kind") not in KINDS:
        raise _invalid("surface " + identity + " kind must be browser or desktop")
    surface = {"id": identity, "label": _text(entry.get("label"), "surface " + identity + " label", 80),
               "kind": entry["kind"], "viewer_url": _url(entry.get("viewer_url"), "surface " + identity + " viewer_url")}
    if ("width" in entry) != ("height" in entry):
        raise _invalid("surface " + identity + " needs both width and height, or neither")
    if "width" in entry:
        surface["width"] = _dimension(entry["width"], "surface " + identity + " width")
        surface["height"] = _dimension(entry["height"], "surface " + identity + " height")
    return surface


def parse(value):
    """Validate one complete version-1 document; any defect rejects all of it."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "surfaces", "agents"} or value["schema_version"] != 1:
        raise _invalid("expected schema_version 1 with surfaces and agents")
    surfaces, agents = value["surfaces"], value["agents"]
    if not isinstance(surfaces, dict) or len(surfaces) > MAX_SURFACES or not isinstance(agents, dict) or len(agents) > MAX_AGENTS:
        raise _invalid("surfaces and agents must be bounded objects")
    parsed = {}
    for identity, entry in surfaces.items():
        if not isinstance(identity, str) or not SURFACE_ID.fullmatch(identity):
            raise _invalid("surface IDs use lowercase letters, digits and hyphens")
        parsed[identity] = _surface(identity, entry)
    bindings = {}
    for agent_id, binding in agents.items():
        try:
            if not isinstance(agent_id, str) or str(uuid.UUID(agent_id)) != agent_id:
                raise ValueError
        except ValueError:
            raise _invalid("agent keys must be registered agent UUIDs") from None
        if not isinstance(binding, dict) or set(binding) - {"browsers", "desktop"} or "browsers" not in binding:
            raise _invalid("agent " + agent_id + " binding needs a browsers list and optional desktop")
        browsers, desktop = binding["browsers"], binding.get("desktop")
        if (not isinstance(browsers, list) or len(browsers) > MAX_BROWSERS
                or len(set(map(str, browsers))) != len(browsers)):
            raise _invalid("agent " + agent_id + " browsers must be a unique bounded list")
        for identity in browsers:
            if not isinstance(identity, str) or parsed.get(identity, {}).get("kind") != "browser":
                raise _invalid("agent " + agent_id + " refers to an unknown browser surface")
        if desktop is not None and (not isinstance(desktop, str) or parsed.get(desktop, {}).get("kind") != "desktop"):
            raise _invalid("agent " + agent_id + " refers to an unknown desktop surface")
        bindings[agent_id] = {"browsers": list(browsers), "desktop": desktop}
    return {"surfaces": parsed, "agents": bindings}


def read(root):
    """The parsed registry, or None when the operator has not configured one."""
    try:
        content = safe_io.read_bytes(Path(root) / RELATIVE_PATH, limit=MAX_BYTES, private=True)
    except FileNotFoundError:
        return None
    except DomainError as error:
        raise _invalid("the file must be a private (0600) ordinary file within its size limit") from error
    except OSError:
        raise DomainError("invalid_configuration", "Browser surface configuration cannot be read") from None
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid("the file is not JSON") from None
    return parse(value)


def agent_surfaces(registry, agent_id, *, desktop_allowed, fallback_desktop_url=None):
    """Exactly the configured surfaces for one agent; never another agent's."""
    binding = (registry or {"agents": {}})["agents"].get(agent_id)
    browsers = [dict(registry["surfaces"][identity]) for identity in binding["browsers"]] if binding else []
    desktop = None
    if desktop_allowed:
        if binding and binding["desktop"]:
            desktop = dict(registry["surfaces"][binding["desktop"]])
        elif fallback_desktop_url:
            # The existing DESKTOP_CONTROL_URL remains Ordubashy's full desktop
            # until an explicit desktop binding replaces it.
            desktop = {"id": "desktop-control-url", "label": "Desktop", "kind": "desktop", "viewer_url": fallback_desktop_url}
    return {"agent_id": agent_id, "configured": registry is not None, "browsers": browsers, "desktop": desktop}
