"""The Email Management instruction package: validation, one-time instance seeding and native skill rendering.

Email Management is guidance only. The v3 package declares no tools and the ``owner_input`` approval rule; it carries a small
native discovery skill and an ``instance-template/`` that preparation copies once into the instance-owned directory
``<operations>/extensions/email-management``. That directory is the owner-editable source of mailbox, provider and inbox
guidance, shared by every agent granted the package. Re-preparation never overwrites it. The generated ``.claude/skills`` and
``.agents/skills`` files are a discovery entry whose ``{{EMAIL_MANAGEMENT_HOME}}`` placeholder names that explicit directory.
Mail and calendar provider code lives in optional extension packages, never here."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat

from bazarlyk.agents.plugin_defaults import EMAIL_MAJOR, EMAIL_PLUGIN
from bazarlyk.catalog.packages import declared_plugin_tools, parse_yaml
from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage import safe_io

PLUGIN_ID = EMAIL_PLUGIN
SKILL_ID = "email-management"
SUPPORTED_MAJOR = EMAIL_MAJOR
PLACEHOLDER = "{{EMAIL_MANAGEMENT_HOME}}"
SUBTREE = "skills/email-management"
TEMPLATE_DIRECTORY = "instance-template"
TEMPLATE_FILES = frozenset({"SKILL.md", "instructions.md", "inboxes/README.md", "providers/gmail.md", "providers/fastmail.md"})
RULES = {"channel": "email", "approval": "owner_input"}
# Recorded inside the seeded directory, so the record and the seeding are one atomic rename.
ORIGIN_FILE = ".origin.json"
MAX_FILE_BYTES = 262_144


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


def home(paths):
    """The explicit instance directory, validated by InstancePaths (absolute, no traversal, no symlink)."""
    return paths.extension_home(PLUGIN_ID)


def render(body: bytes, paths) -> bytes:
    """Resolve the placeholder to the explicit instance directory; no other text changes."""
    return body.replace(PLACEHOLDER.encode(), str(home(paths)).encode())


def skill_files(package, paths) -> dict[str, bytes]:
    """The generated native skill's files relative to its directory: the discovery entry only, placeholder resolved.

    The ``instance-template/`` is a seed for the instance directory, not part of the generated skill."""
    prefix = SUBTREE + "/"
    template = prefix + TEMPLATE_DIRECTORY + "/"
    return {name[len(prefix):]: render(body, paths) for name, body in package.files.items()
            if name.startswith(prefix) and not name.startswith(template)}


def _entry_state(directory, name):
    try:
        return os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return None


def seed(package, paths) -> bool:
    """Copy the instance template into the instance directory once; return whether it was created now.

    An existing directory is never touched. The template is written into a hidden staging directory beside it and renamed
    into place, so an interrupted seeding leaves no half-populated owner directory."""
    target = home(paths)
    staging_name = f".{PLUGIN_ID}.seeding-{secrets.token_hex(8)}"
    prefix = f"{SUBTREE}/{TEMPLATE_DIRECTORY}/"
    safe_io.mkdir(target.parent)
    with safe_io.directory(target.parent) as parent:
        state = _entry_state(parent, target.name)
        if state is not None:
            if not stat.S_ISDIR(state.st_mode):
                raise DomainError("projection_conflict", "The Email Management instance path is not a directory")
            return False
        staging = target.parent / staging_name
        written = {}
        try:
            for name in sorted(TEMPLATE_FILES):
                body = render(package.files[prefix + name], paths)
                written[name] = hashlib.sha256(body).hexdigest()
                safe_io.atomic_write(staging / PurePosixPath(name), body, mode=0o644)
            origin = {"schema_version": 1, "package": package.ref, "seeded_at": utc_now(), "files": written,
                      "note": "Seeded once from the package template. Preparation never overwrites this directory."}
            safe_io.atomic_write(staging / ORIGIN_FILE, (json.dumps(origin, indent=2, sort_keys=True) + "\n").encode(), mode=0o644)
            try:
                os.rename(staging_name, target.name, src_dir_fd=parent, dst_dir_fd=parent)
            except OSError:
                # Another preparation seeded it first; the existing directory wins and stays untouched.
                if _entry_state(parent, target.name) is None:
                    raise
                return False
            return True
        finally:
            _discard(staging)


def _discard(staging: Path) -> None:
    """Remove this call's own staging directory if the rename did not consume it; never follows links."""
    try:
        with safe_io.directory(staging.parent) as parent:
            if _entry_state(parent, staging.name) is None:
                return
    except FileNotFoundError:
        return
    for directory, _, names in os.walk(staging, followlinks=False):
        for name in names:
            safe_io.unlink(Path(directory) / name, missing_ok=True)
    safe_io.remove_empty_tree(staging)
