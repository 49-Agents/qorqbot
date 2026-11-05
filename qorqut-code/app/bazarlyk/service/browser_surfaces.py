"""Operator-controlled owner viewer bindings for Global agents' own browsers and each business's one browser.

`<operations>/.runtime/browser-surfaces.json` is deployment configuration,
like each workspace's browser.json. It only selects which exact viewer URL the
owner sees for a Global agent's own browsers and Desktop, or for one business's
browser. It grants no automation rights, and it never carries cookies, profile
data, passwords, CDP endpoints or executable hooks. Schema 2 is the only accepted
document; an older registry is converted by the explicit v1.2 migration, never here.
"""

import json
import re
import uuid

from bazarlyk.agents import roles
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io

FILENAME = "browser-surfaces.json"
SCHEMA_VERSION = 2
SURFACE_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
KINDS = frozenset({"browser", "desktop"})
MAX_BYTES = 65536
MAX_SURFACES = 64
MAX_AGENTS = 64
MAX_BUSINESSES = 64
MAX_BROWSERS = 16
# The supported Global roles whose registered agents may have own viewer surfaces.
GLOBAL_ROLES = roles.SUPPORTED_GLOBAL_ROLES
# A sandboxed business's viewer is its sandbox owner viewer, under this fixed synthetic ID.
SANDBOX_SURFACE_ID = "business-sandbox-browser"


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


def _identity(value, what):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise _invalid(what + " keys must be canonical UUIDs") from None
    return value


def parse(value):
    """Validate one complete schema-2 document; any defect rejects all of it."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "surfaces", "agents", "businesses"}
            or value["schema_version"] != SCHEMA_VERSION):
        raise _invalid("expected schema_version 2 with surfaces, agents and projects; "
                       "an older registry must be converted by the explicit migration")
    surfaces, agents, businesses = value["surfaces"], value["agents"], value["businesses"]
    if (not isinstance(surfaces, dict) or len(surfaces) > MAX_SURFACES or not isinstance(agents, dict)
            or len(agents) > MAX_AGENTS or not isinstance(businesses, dict) or len(businesses) > MAX_BUSINESSES):
        raise _invalid("surfaces, agents and projects must be bounded objects")
    parsed = {}
    for identity, entry in surfaces.items():
        if not isinstance(identity, str) or not SURFACE_ID.fullmatch(identity):
            raise _invalid("surface IDs use lowercase letters, digits and hyphens")
        parsed[identity] = _surface(identity, entry)
    bindings = {}
    for agent_id, binding in agents.items():
        _identity(agent_id, "agent")
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
    owned = {identity for binding in bindings.values() for identity in binding["browsers"]}
    claimed = {}
    for business_id, binding in businesses.items():
        _identity(business_id, "business")
        if not isinstance(binding, dict) or set(binding) != {"browser"}:
            raise _invalid("business " + business_id + " binding needs exactly one browser")
        identity = binding["browser"]
        if not isinstance(identity, str) or parsed.get(identity, {}).get("kind") != "browser":
            raise _invalid("business " + business_id + " refers to an unknown browser surface")
        if identity in owned:
            raise _invalid("surface " + identity + " cannot be both a project browser and an agent's own browser")
        if identity in {item["browser"] for item in claimed.values()}:
            raise _invalid("surface " + identity + " cannot belong to more than one project")
        claimed[business_id] = {"browser": identity}
    return {"surfaces": parsed, "agents": bindings, "businesses": claimed}


def surfaces_path(paths):
    return paths.runtime / FILENAME


def read(paths):
    """The parsed registry, or None when the operator has not configured one."""
    try:
        content = safe_io.read_bytes(surfaces_path(paths), limit=MAX_BYTES, private=True)
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


def global_profile(profile):
    """A stored profile of one of the supported Global roles with ecosystem scope; retired business roles never qualify."""
    data = json.loads(profile) if isinstance(profile, str) else profile
    return (isinstance(data, dict) and (data.get("scope") or {}).get("kind") == "ecosystem"
            and (data.get("role") or {}).get("id") in GLOBAL_ROLES)


def business_surfaces(registry, business_id, *, sandbox=None):
    """Exactly one business's viewer; never an agent's own browser or another business's.

    A host or isolated browser is the business's one registered surface. A sandboxed business is described only
    by ``sandbox`` (``configured``, ``label`` and the ready ``viewer_url`` or None) and never consults the
    registry, so an unavailable sandbox viewer yields no browser rather than some other one."""
    if sandbox is not None:
        browsers = ([{"id": SANDBOX_SURFACE_ID, "label": sandbox["label"], "kind": "browser", "viewer_url": sandbox["viewer_url"]}]
                    if sandbox["configured"] and sandbox["viewer_url"] else [])
        return {"business_id": business_id, "configured": sandbox["configured"], "browsers": browsers, "desktop": None}
    binding = (registry or {"businesses": {}})["businesses"].get(business_id)
    browsers = [dict(registry["surfaces"][binding["browser"]])] if binding else []
    return {"business_id": business_id, "configured": binding is not None, "browsers": browsers, "desktop": None}
