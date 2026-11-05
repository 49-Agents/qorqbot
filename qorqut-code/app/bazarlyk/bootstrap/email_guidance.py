"""Email discovery rendering and validation of the imported canonical procedural guide.

Original instance-template files are inactive provenance. Installation imports fresh
seeds or an explicit offline guide snapshot. Preparation never copies another guide.
"""

from __future__ import annotations

from pathlib import Path

from bazarlyk.agents.plugin_defaults import EMAIL_MAJOR, EMAIL_PLUGIN
from bazarlyk.catalog.packages import declared_plugin_tools, parse_yaml
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic.text import read_resource, render_text
from bazarlyk.agentic.support import relative

PLUGIN_ID = EMAIL_PLUGIN
SKILL_ID = "email-management"
SUPPORTED_MAJOR = EMAIL_MAJOR
PLACEHOLDER = "{{EMAIL_MANAGEMENT_HOME}}"
SUBTREE = "skills/email-management"
TEMPLATE_DIRECTORY = "instance-template"
TEMPLATE_FILES = frozenset({"SKILL.md", "instructions.md", "inboxes/README.md", "providers/gmail.md", "providers/fastmail.md"})
RULES = {"channel": "email", "approval": "owner_input"}
MAX_FILE_BYTES = 262_144
EMAIL_COMPOSITION = {
    "schema_version": 1,
    "parameters": ["EMAIL_MANAGEMENT_HOME"],
    "discovery_source": "skills/email-management/SKILL.md",
    "canonical_package": "imported-email-management",
    "canonical_path": "guidance/email/SKILL.md",
}


def _invalid(message: str):
    raise DomainError("invalid_artifact", "Email Management package: " + message)


def _version(value) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except ValueError:
        return ()


def validate_package(package) -> None:
    """Accept exactly the instruction-only v3 contract; refuse any tool, provider rule or unknown template file."""
    if package.id != PLUGIN_ID or _version(package.version)[:1] != (SUPPORTED_MAJOR,):
        _invalid(f"requires {PLUGIN_ID} version {SUPPORTED_MAJOR}.x")
    if declared_plugin_tools(package):
        _invalid("declares no tools")
    if parse_yaml(package.files[package.manifest["rules"]].decode()) != RULES:
        _invalid("rules must be exactly channel email with owner_input approval")
    if len(package.skills) != 1 or package.skills[0]["id"] != SKILL_ID or package.skills[0]["subtree"] != SUBTREE:
        _invalid(f"declares exactly one skill rooted at {SUBTREE}")
    entry = package.files.get(SUBTREE + "/SKILL.md", b"").decode("utf-8", "replace")
    if PLACEHOLDER not in entry:
        _invalid(f"native skill must carry the {PLACEHOLDER} placeholder")
    prefix = f"{SUBTREE}/{TEMPLATE_DIRECTORY}/"
    template = {name[len(prefix):] for name in package.files if name.startswith(prefix)}
    if template != TEMPLATE_FILES:
        _invalid("instance-template must contain exactly " + ", ".join(sorted(TEMPLATE_FILES)))
    for name in sorted(package.files):
        if name.startswith(SUBTREE + "/"):
            try:
                body = package.files[name].decode("utf-8")
            except UnicodeDecodeError:
                _invalid(f"{name} is not UTF-8")
            if len(body) > MAX_FILE_BYTES or "{{" in body.replace(PLACEHOLDER, ""):
                _invalid(f"{name} is oversized or carries an unknown placeholder")


def is_package(package) -> bool:
    return getattr(package, "kind", None) == "plugin" and package.id == PLUGIN_ID


def home(paths, *, agentic_view):
    """Validate the active canonical guide and return its directory under the opened layer."""
    if agentic_view is None:
        raise DomainError("missing_agentic_view", "Email guidance requires an agentic view")
    resource = agentic_view.layer.catalog.read_resource("imported-email-management", "guidance/email/SKILL.md")
    if resource.get("encoding") != "utf-8" or not isinstance(resource.get("content"), str) or not resource["content"].strip():
        raise DomainError("invalid_email_guidance", "Canonical email guide must be nonempty UTF-8")
    if len(resource["content"].encode("utf-8")) > MAX_FILE_BYTES:
        raise DomainError("invalid_email_guidance", "Canonical email guide exceeds its byte limit")
    expected = "plugins/imported-email-management/guidance/email/SKILL.md"
    root_path = resource.get("root_path")
    if relative(root_path) != expected:
        raise DomainError("invalid_email_guidance", "Canonical email guide returned an unexpected path")
    return Path(agentic_view.layer.root) / Path(root_path).parent


def render(body: bytes, paths, *, agentic_view) -> bytes:
    """Resolve only EMAIL_MANAGEMENT_HOME, preserving Unicode and actual newlines."""
    text = body.decode("utf-8", "strict")
    parameters = ("EMAIL_MANAGEMENT_HOME",) if PLACEHOLDER in text else ()
    values = {"EMAIL_MANAGEMENT_HOME": str(home(paths, agentic_view=agentic_view))} if parameters else {}
    return render_text(text, parameters, values).encode("utf-8")


def skill_files(package, paths, *, agentic_view) -> dict[str, bytes]:
    """Read the managed discovery files; the canonical guide is never seeded during preparation."""
    home(paths, agentic_view=agentic_view)
    prefix = SUBTREE + "/"
    template = prefix + TEMPLATE_DIRECTORY + "/"
    return {name[len(prefix):]: render(read_resource(agentic_view, "email", name)["content"].encode("utf-8"),
                                      paths, agentic_view=agentic_view)
            for name in package.files if name.startswith(prefix) and not name.startswith(template)}
