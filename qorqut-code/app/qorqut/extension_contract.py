"""The extension checker: reads one extension folder and reports every way it breaks the written contract.

The rules are those of ``docs/EXTENSION-CONTRACT.md``: section 5 (the folder), 6 (the manifest), 7 (the plugin part), 8
(hooks and nudges), 9 (record types) and the rule numbers of section 12. This piece reads files only. It never loads
extension code, so the rules that need the loaded code or the built bundle (``NOT_CHECKED``) are not checked here. A
folder that passes every other rule is also built into its agentic-layer package (``qorqut.extension_package``) and
loaded by the layer's own catalog loader, in memory.

``load_manifest`` stops at the first problem with one ``ContractError``; ``check_folder`` returns every finding as one
plain sentence; ``check_folders`` adds the findings that need more than one folder. Run as
``python -m qorqut.extension_contract [--load] <folder> [<folder> ...]``; with ``--load`` the code part of each folder
that passes is also loaded, in a process of its own (``load_findings``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import cache, wraps
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
from types import SimpleNamespace
from typing import Any

from qorqut.catalog.packages import artifact_file, skill_file_problem, skill_metadata_problem
from qorqut.catalog.sources import load_sources
from qorqut.extensions import EXTENSION_ID, FACTORY, SOURCE_KEY
from qorqut_agentic.authoring import read_snapshot
from qorqut_agentic.errors import AgenticError
from qorqut_agentic.files import OverlaySource
from qorqut_agentic.layout import import_dir
from qorqut_agentic.schemas import WORKFLOW_SCHEMA, schema_issues
from qorqut_agentic.catalog import load_catalog, workflow_semantic_issues
from qorqut_agentic.util import load_yaml, split_frontmatter
from qorqut_operations.contracts import OPERATION_NAME
from qorqut_operations.errors import DomainError
from qorqut_operations.global_agents import SUPPORTED_GLOBAL_ROLES
from qorqut_operations.tasks.models import FIELD_TYPES, NAME as FIELD_NAME, RESERVED_FIELD_NAMES
from qorqut_operations.tasks.types import TYPES

MANIFEST = "extension.yaml"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# The app versions an extension works with: comparisons against a three-number version, joined by commas (">=1.7.0",
# ">=1.7.0,<2.0.0"). Checked here and not with a library, so every interpreter gives the same answer.
_COMPARISON = r"(?:>=|<=|==|!=|>|<)[0-9]+\.[0-9]+\.[0-9]+"
SPECIFIER = re.compile(rf"^{_COMPARISON}(?:,{_COMPARISON})*$")
EVERY = re.compile(r"^([0-9]+)([mhd])$")
UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
DELIVERIES = ("built-in", "downloadable")
PLATFORMS = ("linux", "mac", "windows")
SETUP_KINDS = ("none", "key", "sign-in", "pairing")
HOOK_POINTS = ("start", "owner_message", "after_tool", "stop", "timed")
HOOK_KEYS = frozenset({"id", "at", "text", "tools", "every", "if_used", "unless_used"})
CONDITION_POINTS = ("after_tool", "stop")  # Only these points have a turn whose tool calls a condition can look at.
RECORD_KEYS = frozenset({"id", "based_on", "label", "plural", "fields"})
RECORD_FIELD_KEYS = frozenset({"id", "label", "type", "values"})
RECORD_BASES = tuple(name for name in TYPES if name != "email")  # contract section 15: not on `email`
MANIFEST_KEYS = frozenset({"contract", "id", "name", "summary", "version", "delivery", "platforms", "requires", "code",
                           "setup", "agents"})
CHECKED = ("1, 2, 3, 4, 6, 7, the README and plugin-part layout, the hook text files, the workflow files, the skill "
           "files, symbolic links, skill names taken by packages that ship with Qorqbot and that the plugin part loads "
           "in the agentic layer")
NOT_CHECKED = "5, 8, 9, 10, 11, 12, 13"
APP = Path(__file__).resolve().parents[1]

Reader = Callable[[str], "bytes | None"]


class ContractError(Exception):
    """The first way an extension breaks the contract."""


@dataclass(frozen=True)
class SetupField:
    id: str
    label: str
    secret: bool = False


@dataclass(frozen=True)
class Manifest:
    contract: int
    id: str
    name: str
    summary: str
    version: str
    delivery: str
    platforms: tuple[str, ...]
    requires_qorqbot: str
    requires_extensions: tuple[str, ...]
    factory: str | None
    setup_kind: str
    setup_fields: tuple[SetupField, ...]
    agents_allowed: tuple[str, ...]
    agents_on_after_setup: tuple[str, ...]


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _names(value: Any, form: re.Pattern, *, nonempty: bool) -> bool:
    """A list of strings of one form."""
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(item, str) and form.fullmatch(item) for item in value))


def _role_findings(where: str, roles: list) -> list[str]:
    """The role names of ``roles`` that are not role ids Qorqbot has, compared exactly."""
    known = ", ".join(sorted(SUPPORTED_GLOBAL_ROLES))
    return [f"{where} names {role!r}, which is not a role Qorqbot has ({known})." for role in roles
            if role not in SUPPORTED_GLOBAL_ROLES]


def _strings(value: Any, *, nonempty: bool) -> bool:
    return isinstance(value, list) and (bool(value) or not nonempty) and all(isinstance(item, str) for item in value)


def skill_name(folder: str) -> str:
    """The managed skill name a skill folder of an extension gets (the resolver writes the same name inline)."""
    return f"qorqut-plugin-{folder}"


def skill_names_of(sources) -> frozenset[str]:
    """The managed skill names of the packages of a loaded source map (``qorqut.catalog.sources.load_sources``), in the
    older per-agent format, named as ``qorqut.catalog.packages`` names them (``qorqut-<kind>-<skill id>``)."""
    names = set()
    for kind, root, manifest in sources.values():
        try:
            data = load_yaml((root / manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DomainError("invalid_configuration", f"Cannot read package manifest: {root / manifest}") from exc
        names.update(f"qorqut-{kind}-{entry['id']}" for entry in data.get("skills", ()))
    return frozenset(names)


@cache
def shipped_skill_names() -> frozenset[str]:
    """The managed skill names of the packages that ship with Qorqbot, read from the ``package-sources.json`` of the
    source tree this module is in: what the command line checks against. Where the module runs as an installed
    package there is no map beside it and this is empty; the service gives the names of its own map instead
    (``check_folders(..., shipped=...)``)."""
    path = APP / "package-sources.json"
    if not path.is_file():
        return frozenset()
    return skill_names_of(load_sources(APP.parents[1], config_path=path))


def _read(path: Path) -> tuple[Any, str | None]:
    """The YAML of ``path`` as ``(data, None)``, or ``(None, sentence)`` when it cannot be read."""
    try:
        return load_yaml(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name} cannot be read: {exc.__class__.__name__}."
    except ValueError as exc:
        return None, f"{path.name} is not valid YAML: {exc}"


def _unknown(where: str, data: Mapping, allowed: Iterable[str]) -> list[str]:
    extra = sorted(str(key) for key in set(data) - set(allowed))
    return [f"{where} has the unknown field {key!r}." for key in extra]


def _mapping(parent: Mapping, key: str, where: str, found: list[str], *, required: bool = True) -> Mapping | None:
    if key not in parent:
        if required:
            found.append(f"{where} is missing.")
        return None
    if not isinstance(parent[key], dict):
        found.append(f"{where} must be a mapping.")
        return None
    return parent[key]


def _manifest_findings(data: Any, folder: Path) -> list[str]:
    """Rules 1 to 3 for the content of ``extension.yaml``; the folder is read only for its name and ``code/``."""
    if not isinstance(data, dict):
        return [f"{MANIFEST} must be a mapping of fields."]
    found = _unknown(MANIFEST, data, MANIFEST_KEYS)
    has_code = (folder / "code").is_dir()
    for key in ("contract", "id", "name", "summary", "version", "delivery", "platforms"):
        if key not in data:
            found.append(f"{MANIFEST} is missing the field {key!r}.")
    ext_id = data.get("id")
    if "id" in data:
        if not isinstance(ext_id, str) or not EXTENSION_ID.fullmatch(ext_id):
            found.append(f"The id {ext_id!r} is not lowercase letters, digits and underscores, starting with a letter, "
                         "at most 32 characters.")
        if ext_id != folder.name:
            found.append(f"The folder name {folder.name!r} does not equal the id {ext_id!r}.")
    if "contract" in data and not (type(data["contract"]) is int and data["contract"] == 1):
        found.append("`contract` must be exactly 1.")
    for key in ("name", "summary"):
        if key in data and not _text(data[key]):
            found.append(f"`{key}` must be a non-empty string.")
    if "version" in data and not (isinstance(data["version"], str) and VERSION.fullmatch(data["version"])):
        found.append("`version` must be three numbers separated by dots, such as 1.0.0.")
    if "delivery" in data and data["delivery"] not in DELIVERIES:
        found.append("`delivery` must be 'built-in' or 'downloadable'.")
    platforms = data.get("platforms")
    if "platforms" in data and not (
            isinstance(platforms, list) and platforms and all(item in PLATFORMS for item in platforms)
            and len(set(platforms)) == len(platforms)):
        found.append("`platforms` must be a non-empty list of linux, mac and windows, each at most once.")

    requires = _mapping(data, "requires", "`requires`", found)
    if requires is not None:
        found.extend(_unknown("`requires`", requires, ("qorqbot", "extensions")))
        spec = requires.get("qorqbot")
        if "qorqbot" not in requires:
            found.append("`requires` is missing `qorqbot`.")
        elif not _text(spec):
            found.append("`requires.qorqbot` must be a non-empty string.")
        elif not SPECIFIER.fullmatch(spec.replace(" ", "")):
            found.append(f"`requires.qorqbot` {spec!r} must be comparisons against a version, such as '>=1.7.0' or "
                         "'>=1.7.0,<2.0.0'.")
        if "extensions" in requires:
            needed = requires["extensions"]
            if not _names(needed, EXTENSION_ID, nonempty=False):
                found.append("`requires.extensions` must be a list of extension ids.")
            elif isinstance(ext_id, str) and ext_id in needed:
                found.append("`requires.extensions` names the extension itself.")

    code = _mapping(data, "code", "`code`", found, required=False)
    if has_code and "code" not in data:
        found.append("The folder has a `code/` directory, so the manifest needs `code.factory`.")
    if not has_code and "code" in data:
        found.append("The manifest has `code` but the folder has no `code/` directory.")
    if code is not None:
        found.extend(_unknown("`code`", code, ("factory",)))
        factory = code.get("factory")
        match = FACTORY.fullmatch(factory) if isinstance(factory, str) else None
        if "factory" not in code:
            found.append("`code` is missing `factory`.")
        elif not match:
            found.append(f"`code.factory` {factory!r} is not of the form module.path:function.")
        elif isinstance(ext_id, str):
            package = f"qorqut_{ext_id}"
            if match[1] != package and not match[1].startswith(package + "."):
                found.append(f"`code.factory` must be in the package {package!r}.")

    setup = _mapping(data, "setup", "`setup`", found)
    if setup is not None:
        found.extend(_unknown("`setup`", setup, ("kind", "fields")))
        kind = setup.get("kind")
        if "kind" not in setup:
            found.append("`setup` is missing `kind`.")
        elif kind not in SETUP_KINDS:
            found.append("`setup.kind` must be none, key, sign-in or pairing.")
        if kind == "key" and "fields" not in setup:
            found.append("`setup.fields` is required when the setup kind is key.")
        if kind != "key" and "fields" in setup:
            found.append("`setup.fields` is allowed only when the setup kind is key.")
        if "fields" in setup:
            found.extend(_setup_fields(setup["fields"]))

    agents = _mapping(data, "agents", "`agents`", found)
    if agents is not None:
        found.extend(_unknown("`agents`", agents, ("allowed", "on_after_setup")))
        allowed = agents.get("allowed")
        if "allowed" not in agents:
            found.append("`agents` is missing `allowed`.")
        elif not _strings(allowed, nonempty=True):
            found.append("`agents.allowed` must be a non-empty list of role names.")
        else:
            found.extend(_role_findings("`agents.allowed`", allowed))
        if "on_after_setup" in agents:
            later = agents["on_after_setup"]
            if not _strings(later, nonempty=False):
                found.append("`agents.on_after_setup` must be a list of role names.")
            else:
                found.extend(_role_findings("`agents.on_after_setup`", later))
                for role in later:
                    if isinstance(allowed, list) and role in SUPPORTED_GLOBAL_ROLES and role not in allowed:
                        found.append(f"`agents.on_after_setup` names {role!r}, which is not in `agents.allowed`.")
    return found


def _setup_fields(fields: Any) -> list[str]:
    if not isinstance(fields, list) or not fields:
        return ["`setup.fields` must be a non-empty list."]
    found = []
    seen = []
    for number, entry in enumerate(fields, 1):
        where = f"`setup.fields` entry {number}"
        if not isinstance(entry, dict):
            found.append(f"{where} must be a mapping.")
            continue
        found.extend(_unknown(where, entry, ("id", "label", "secret")))
        for key in ("id", "label"):
            if not _text(entry.get(key)):
                found.append(f"{where} needs a non-empty `{key}`.")
        if "secret" in entry and type(entry["secret"]) is not bool:
            found.append(f"{where} has a `secret` that is not true or false.")
        seen.append(entry.get("id"))
    found.extend(f"`setup.fields` repeats the id {name!r}." for name, count in Counter(
        item for item in seen if isinstance(item, str)).items() if count > 1)
    return found


def load_manifest(folder: Path) -> Manifest:
    """The manifest of ``folder``. Raises ``ContractError`` for the first problem found."""
    folder = Path(folder).resolve()
    path = folder / MANIFEST
    if not path.is_file():
        raise ContractError(f"{MANIFEST} is missing.")
    data, problem = _read(path)
    if problem:
        raise ContractError(problem)
    found = _manifest_findings(data, folder)
    if found:
        raise ContractError(found[0])
    return Manifest(
        contract=data["contract"], id=data["id"], name=data["name"], summary=data["summary"], version=data["version"],
        delivery=data["delivery"], platforms=tuple(data["platforms"]),
        requires_qorqbot=data["requires"]["qorqbot"], requires_extensions=tuple(data["requires"].get("extensions", ())),
        factory=data.get("code", {}).get("factory"), setup_kind=data["setup"]["kind"],
        setup_fields=tuple(SetupField(item["id"], item["label"], item.get("secret", False))
                           for item in data["setup"].get("fields", ())),
        agents_allowed=tuple(data["agents"]["allowed"]),
        agents_on_after_setup=tuple(data["agents"].get("on_after_setup", ())))


def _code_findings(folder: Path, ext_id: str) -> list[str]:
    code = folder / "code"
    if not code.is_dir():
        return []
    found = []
    if not (code / "pyproject.toml").is_file():
        found.append("`code/` has no pyproject.toml.")
    package = f"qorqut_{ext_id}"
    if not (code / package / "extension.py").is_file():
        found.append(f"`code/` has no {package}/extension.py.")
    return found


def _files(top: Path) -> list[Path]:
    """The files under ``top``, without following or listing symbolic links (``_link_findings`` reports those)."""
    found = []
    for directory, dirs, names in os.walk(top):
        dirs[:] = sorted(name for name in dirs if not Path(directory, name).is_symlink())
        found.extend(Path(directory, name) for name in sorted(names) if not Path(directory, name).is_symlink())
    return found


def _link_findings(folder: Path) -> list[str]:
    found = []
    for directory, dirs, names in os.walk(folder):
        dirs.sort()
        for name in sorted([*dirs, *names]):
            path = Path(directory, name)
            if path.is_symlink():
                found.append(f"{path.relative_to(folder).as_posix()} is a symbolic link, which an extension folder "
                             "may not hold.")
    return found


def _skill_folders(plugin: Path) -> list[Path]:
    skills = plugin / "skills"
    if not skills.is_dir() or skills.is_symlink():
        return []
    return sorted(path for path in skills.iterdir() if path.is_dir() and not path.is_symlink())


def _skill_file_findings(plugin: Path, folder: Path) -> list[str]:
    """The rules Qorqbot applies to the files of a managed skill (``qorqut.catalog.packages``): the file's size through
    ``artifact_file``, its kind and content through ``skill_file_problem``, and no executable file, which that function
    leaves to whoever reads the files from disk."""
    root = plugin.resolve()
    found = []
    for path in _files(folder):
        name = path.relative_to(plugin).as_posix()
        if path.stat().st_mode & 0o111:
            found.append(f"The skill file {name} is executable.")
        try:
            problem = skill_file_problem(name, artifact_file(root, name))
        except DomainError as exc:
            problem = exc.message
        if problem:
            found.append(f"The skill file {name} is refused: {problem}.")
    return found


def _skill_findings(plugin: Path, ext_id: str, shipped: frozenset[str]) -> list[str]:
    found = []
    for folder in _skill_folders(plugin):
        where = f"The skill {folder.name!r}"
        if folder.name != ext_id and not folder.name.startswith(ext_id + "-"):
            found.append(f"{where} must be named {ext_id!r}, or {ext_id!r} followed by '-' and more.")
        if skill_name(folder.name) in shipped:
            found.append(f"{where} gives the managed skill name {skill_name(folder.name)!r}, which a package that "
                         "ships with Qorqbot already uses.")
        found.extend(_skill_file_findings(plugin, folder))
        path = folder / "SKILL.md"
        if not path.is_file():
            found.append(f"{where} has no SKILL.md.")
            continue
        try:
            meta, problem = split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            found.append(f"{where} has a SKILL.md that cannot be read.")
            continue
        if problem or meta is None:
            found.append(f"{where} has no usable front matter in SKILL.md" + (f": {problem}." if problem else "."))
            continue
        own = []
        if set(meta) != {"name", "description", "read_for"}:
            own.append(f"{where} must have exactly the front matter lines name, description and read_for.")
        if "name" in meta and meta["name"] != skill_name(folder.name):
            own.append(f"{where} must be named {skill_name(folder.name)!r} in its front matter.")
        if "description" in meta and not _text(meta["description"]):
            own.append(f"{where} has an empty description.")
        found.extend(own)
        # Then the front matter rules every managed skill is held to, where the rules above leave nothing to say.
        problem = None if own else skill_metadata_problem(meta, skill_name(folder.name), path.relative_to(plugin))
        if problem:
            found.append(f"{where} is refused as a managed skill: {problem}.")
    return found


def _folder_reader(plugin: Path) -> Reader:
    """A reader over the plugin part of a folder: the bytes of a file inside it, or None."""
    root = plugin.resolve()

    def read(relative: str) -> bytes | None:
        target = (root / relative).resolve()
        if (target == root or root in target.parents) and target.is_file():
            return target.read_bytes()
        return None
    return read


def _never_raises(name: str):
    """A public reader runs on a copy that can be edited, so whatever the bytes are and whatever ``read`` does, it gives
    findings and never raises. The rules below handle every shape they know; this is the last guard."""
    def decorate(function):
        @wraps(function)
        def reader(read: Reader, *, extension_id: str) -> tuple[list[dict], list[str]]:
            try:
                return function(read, extension_id=extension_id)
            except Exception as exc:
                return [], [f"{name} cannot be read: {exc.__class__.__name__}."]
        return reader
    return decorate


def _list_file(read: Reader, name: str, kind: str) -> tuple[list | None, list[str]]:
    """The entries of an optional list file: ``(None, [])`` when it is absent."""
    try:
        data = read(name)
    except Exception as exc:  # The layer's reader refuses some paths and sizes by raising.
        return None, [f"{name} cannot be read: {exc.__class__.__name__}."]
    if data is None:
        return None, []
    try:
        value = load_yaml(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return None, [f"{name} cannot be read: {exc.__class__.__name__}."]
    except RecursionError:
        return None, [f"{name} cannot be read as a list: it is nested too deeply."]
    except ValueError as exc:
        return None, [f"{name} is not valid YAML: {exc}"]
    except Exception as exc:
        return None, [f"{name} cannot be read as a list: {exc.__class__.__name__}."]
    if not isinstance(value, list):
        return None, [f"{name} must be a list of {kind}."]
    return value, []


def _tool_list(value: Any, *, nonempty: bool) -> bool:
    """Tool names are held to the rule the operation registry has for operation names and aliases."""
    return _names(value, OPERATION_NAME, nonempty=nonempty)


def _every_seconds(count: str, unit: str) -> int | None:
    """The seconds of ``every``, or None when the number cannot be a length of time."""
    try:
        seconds = int(count) * UNIT_SECONDS[unit]
        timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        return None
    return seconds


@_never_raises("hooks.yaml")
def read_hooks(read: Reader, *, extension_id: str) -> tuple[list[dict], list[str]]:
    """The entries of an extension's ``hooks.yaml`` and every way the file breaks the contract (section 8).

    ``read(path)`` gives the bytes of a file of the plugin part (``hooks.yaml``, ``instructions/x.md``), or None when there
    is none: a folder on disk or the agentic layer's copy. Each entry is ``{"extension_id", "id", "at", "text",
    "tools", "every_seconds", "if_used", "unless_used"}``, in file order, with ``text`` read as a string, the three tool
    lists as lists (empty when not given) and ``every_seconds`` an int at ``timed``, else None. An entry with a finding,
    and every entry of a repeated id, is left out. A file that is absent gives ``([], [])``.
    """
    items, found = _list_file(read, "hooks.yaml", "entries")
    seen: list[str] = []
    entries: list[dict] = []
    for number, entry in enumerate(items or (), 1):
        label = f"hooks.yaml entry {number}"
        if not isinstance(entry, dict):
            found.append(f"{label} must be a mapping.")
            continue
        mine = _unknown(label, entry, HOOK_KEYS)
        if not _text(entry.get("id")):
            mine.append(f"{label} needs an `id`.")
        else:
            seen.append(entry["id"])
            label = f"hooks.yaml entry {entry['id']!r}"
        at = entry.get("at")
        if at not in HOOK_POINTS:
            mine.append(f"{label} must have `at` as start, owner_message, after_tool, stop or timed.")
        text, problems = _hook_text(read, entry, label)
        mine.extend(problems)
        if at == "after_tool" and "tools" not in entry:
            mine.append(f"{label} is at after_tool and needs `tools`.")
        if "tools" in entry:
            if at != "after_tool":
                mine.append(f"{label} has `tools`, which is allowed only at after_tool.")
            if not _tool_list(entry["tools"], nonempty=True):
                mine.append(f"{label} must have `tools` as a non-empty list of tool names.")
        if at == "timed" and "every" not in entry:
            mine.append(f"{label} is timed and needs `every`.")
        every = None
        if "every" in entry:
            if at != "timed":
                mine.append(f"{label} has `every`, which is allowed only at timed.")
            match = EVERY.fullmatch(entry["every"]) if isinstance(entry["every"], str) else None
            if not match or not match[1].strip("0"):
                mine.append(f"{label} must have `every` as a whole number above zero and m, h or d, such as 7d.")
            else:
                every = _every_seconds(match[1], match[2])
                if every is None:
                    mine.append(f"{label} has an `every` too long to be a length of time.")
        for key in ("if_used", "unless_used"):
            if key in entry:
                if at not in CONDITION_POINTS:
                    mine.append(f"{label} has `{key}`, which is allowed only at after_tool and stop.")
                if entry[key] == []:
                    # Given empty, if_used would never apply and unless_used always would.
                    mine.append(f"{label} has an empty `{key}`; name at least one tool, or leave `{key}` out.")
                elif not _tool_list(entry[key], nonempty=False):
                    mine.append(f"{label} must have `{key}` as a list of tool names.")
        if _tool_list(entry.get("if_used"), nonempty=False) and _tool_list(entry.get("unless_used"), nonempty=False):
            for tool in sorted(set(entry["if_used"]) & set(entry["unless_used"])):
                mine.append(f"{label} names the tool {tool!r} in both `if_used` and `unless_used`.")
        found.extend(mine)
        if not mine:
            entries.append({"extension_id": extension_id, "id": entry["id"], "at": at, "text": text,
                            "tools": list(entry.get("tools", ())), "every_seconds": every,
                            "if_used": list(entry.get("if_used", ())), "unless_used": list(entry.get("unless_used", ()))})
    repeated = {name for name, count in Counter(seen).items() if count > 1}
    found.extend(f"hooks.yaml repeats the id {name!r}." for name in sorted(repeated, key=seen.index))
    return [entry for entry in entries if entry["id"] not in repeated], found


def _hook_text(read: Reader, entry: dict, label: str) -> tuple[str | None, list[str]]:
    """The text an entry names, read through ``read``, or the findings about it."""
    text = entry.get("text")
    if "text" not in entry:
        return None, [f"{label} needs `text`."]
    if not _text(text):
        return None, [f"{label} must have `text` as a path."]
    if text.startswith(("/", "\\")) or PurePosixPath(text).is_absolute() or Path(text).is_absolute():
        return None, [f"{label} has an absolute `text` path."]
    relative = posixpath.normpath(text)
    if relative == ".." or relative.startswith("../"):
        return None, [f"{label} has a `text` path that leaves the plugin directory."]
    if not relative.startswith("instructions/"):
        # Contract section 7: an extension's texts are in instructions/ and reach an agent only through an entry.
        return None, [f"{label} names the text file {text!r}, which is not in instructions/."]
    try:
        data = read(relative)
    except Exception as exc:  # The layer's reader refuses some paths and sizes by raising.
        return None, [f"{label} names the text file {text!r}, which cannot be read: {exc.__class__.__name__}."]
    if data is None:
        return None, [f"{label} names the text file {text!r}, which does not exist in the plugin directory."]
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, [f"{label} names the text file {text!r}, which is not UTF-8."]
    if not content.strip():
        return None, [f"{label} names the text file {text!r}, which is empty."]
    if content.lstrip().startswith("<"):
        # Chat and the hooks read a message that begins with an opening tag as a system notice.
        return None, [f"{label} names the text file {text!r}, which begins with '<'; a text may not begin with a tag."]
    return content, []


@_never_raises("records.yaml")
def read_record_types(read: Reader, *, extension_id: str) -> tuple[list[dict], list[str]]:
    """The record types of an extension's ``records.yaml`` and every way the file breaks the contract (section 9).

    ``read`` is as for ``read_hooks``. Each type is ``{"extension_id", "id", "based_on", "label", "plural", "fields"}``,
    in file order, with ``fields`` a list in file order of ``{"id", "label", "type", "values"}`` (``values`` a list for an
    enum, else None). A type with a finding, and every type of a repeated id, is left out. A file that is absent gives
    ``([], [])``.
    """
    items, found = _list_file(read, "records.yaml", "record types")
    seen: list[str] = []
    types: list[dict] = []
    for number, entry in enumerate(items or (), 1):
        label = f"records.yaml entry {number}"
        if not isinstance(entry, dict):
            found.append(f"{label} must be a mapping.")
            continue
        mine = _unknown(label, entry, RECORD_KEYS)
        if "id" not in entry:
            mine.append(f"{label} needs an `id`.")
        elif not isinstance(entry["id"], str) or not SOURCE_KEY.fullmatch(entry["id"]):
            mine.append(f"{label} has an `id` that is not lowercase letters, digits and underscores.")
        else:
            seen.append(entry["id"])
            label = f"The record type {entry['id']!r}"
        if entry.get("based_on") not in RECORD_BASES:
            mine.append(f"{label} must be based_on one of {', '.join(RECORD_BASES)}.")
        for key in ("label", "plural"):
            if not _text(entry.get(key)):
                mine.append(f"{label} needs a non-empty `{key}`.")
        if "fields" in entry:
            mine.extend(_record_field_findings(entry["fields"], label))
        found.extend(mine)
        if not mine:
            types.append({"extension_id": extension_id, "id": entry["id"], "based_on": entry["based_on"],
                          "label": entry["label"], "plural": entry["plural"],
                          "fields": [{"id": field["id"], "label": field["label"], "type": field["type"],
                                      "values": list(field["values"]) if field["type"] == "enum" else None}
                                     for field in entry.get("fields", ())]})
    repeated = {name for name, count in Counter(seen).items() if count > 1}
    found.extend(f"records.yaml repeats the record type id {name!r}." for name in sorted(repeated, key=seen.index))
    return [item for item in types if item["id"] not in repeated], found


def _record_field_findings(fields: Any, label: str) -> list[str]:
    if not isinstance(fields, list):
        return [f"{label} must have `fields` as a list."]
    found = []
    seen = []
    for number, field in enumerate(fields, 1):
        where = f"{label}, field {number}"
        if not isinstance(field, dict):
            found.append(f"{where} must be a mapping.")
            continue
        found.extend(_unknown(where, field, RECORD_FIELD_KEYS))
        for key in ("id", "label"):
            if not _text(field.get(key)):
                found.append(f"{where} needs a non-empty `{key}`.")
        if isinstance(field.get("id"), str):
            seen.append(field["id"])
            # The extra fields are ordinary custom fields, so their ids follow the rule Qorqbot has for those.
            if field["id"].strip() and (not FIELD_NAME.fullmatch(field["id"]) or field["id"] in RESERVED_FIELD_NAMES):
                found.append(f"{where} has the id {field['id']!r}, which Qorqbot does not accept as a custom field name.")
        kind = field.get("type")
        if kind not in FIELD_TYPES:
            found.append(f"{where} must have `type` as one of {', '.join(FIELD_TYPES)}.")
        values = field.get("values")
        if kind == "enum" and not (isinstance(values, list) and values and all(isinstance(v, str) for v in values)):
            found.append(f"{where} is an enum and needs `values` as a non-empty list of strings.")
        if kind != "enum" and "values" in field:
            found.append(f"{where} has `values`, which is allowed only for an enum.")
    found.extend(f"{label} repeats the field id {name!r}." for name, count in Counter(seen).items() if count > 1)
    return found


def _workflow_files(plugin: Path) -> list[Path]:
    root = plugin / "workflows"
    return _files(root) if root.is_dir() and not root.is_symlink() else []


def _workflow_findings(plugin: Path, ext_id: str) -> list[str]:
    """Each file is read as the agentic layer reads one (``qorqut_agentic.catalog``). A step may refer only to an entry
    the generated package has: a skill (by folder name) or a workflow (by file name) of this extension."""
    entries = {f"{ext_id}/{folder.name}" for folder in _skill_folders(plugin)}
    entries |= {f"{ext_id}/{path.stem}" for path in _workflow_files(plugin) if path.suffix == ".yaml"}
    found = []
    for path in _workflow_files(plugin):
        name = path.relative_to(plugin).as_posix()
        if path.suffix != ".yaml":
            found.append(f"The workflow file {name} must end in .yaml.")
            continue
        data, problem = _read(path)
        if problem:
            found.append(f"The workflow file {name}: {problem}")
            continue
        issues = schema_issues(data, WORKFLOW_SCHEMA)
        issues = issues or workflow_semantic_issues(data)
        found.extend(f"The workflow file {name} is not a valid workflow: {issue}." for issue in issues)
        if issues:
            continue
        if data["id"] != path.stem:
            found.append(f"The workflow file {name} has the id {data['id']!r}, which must equal its file name without "
                         f".yaml, {path.stem!r}.")
        for step in data["steps"]:
            for key in ("instruction_ref", "verifier"):
                if key in step and step[key] not in entries:
                    found.append(f"The workflow file {name}, step {step['id']!r}, has `{key}` {step[key]!r}, which is "
                                 f"not '{ext_id}/' followed by a skill folder or workflow file name of this extension.")
    return found


def _package_findings(folder: Path) -> list[str]:
    """The generated package, loaded by the agentic layer's catalog loader on an in-memory source holding only it."""
    from qorqut import extension_package  # It imports this module.
    try:
        manifest = load_manifest(folder)
        snap = extension_package.snapshot(folder, manifest)
        if snap is None:
            return []
        files = read_snapshot(snap)
    except ContractError as exc:
        return [str(exc)]
    except AgenticError as exc:
        return [f"The plugin part cannot be made into an agentic-layer package: {exc.message}."]
    directory = import_dir(extension_package.package_id(manifest.id))
    empty = SimpleNamespace(prefix=None, legacy_nonempty=False, files={}, special={})
    catalog = load_catalog(OverlaySource(empty, {f"{directory}/{path}": data for path, (data, _mode) in files.items()}))
    return [f"The plugin part does not load in the agentic layer: {issue.message}." for issue in catalog.errors()]


def _identity(folder: Path) -> str:
    """The id to hold the folder to: the manifest's when it is a string, else the folder name."""
    path = folder / MANIFEST
    if path.is_file():
        data, problem = _read(path)
        if not problem and isinstance(data, dict) and isinstance(data.get("id"), str):
            return data["id"]
    return folder.name


def check_folder(folder: Path, *, shipped: frozenset[str] | None = None) -> list[str]:
    """Every way ``folder`` breaks the rules this module checks, one sentence each. Empty means it passes. The
    agentic-layer load is checked only once every other rule passes. ``shipped`` are the managed skill names of the
    packages that ship with Qorqbot (``skill_names_of``); without them, those of this source tree."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        return [f"{folder} is not a folder."]
    found = _link_findings(folder)
    if found:
        # Nothing is read through a link: the findings about the links are the whole answer.
        return found
    path = folder / MANIFEST
    if not path.is_file():
        found.append(f"{MANIFEST} is missing.")
    else:
        data, problem = _read(path)
        found.extend([problem] if problem else _manifest_findings(data, folder))
    ext_id = _identity(folder)
    if not (folder / "README.md").is_file():
        found.append("README.md is missing at the top of the folder.")
    plugin = folder / "plugin"
    if not plugin.is_dir() and not (folder / "code").is_dir():
        found.append("The extension has neither a `plugin/` directory nor a `code/` directory.")
    found.extend(_code_findings(folder, ext_id))
    if plugin.is_dir():
        found.extend(_skill_findings(plugin, ext_id, shipped_skill_names() if shipped is None else shipped))
        found.extend(read_hooks(_folder_reader(plugin), extension_id=ext_id)[1])
        found.extend(read_record_types(_folder_reader(plugin), extension_id=ext_id)[1])
        found.extend(_workflow_findings(plugin, ext_id))
    if not found:
        # Only for an otherwise clean folder, so the layer does not repeat what the rules above already say.
        found.extend(_package_findings(folder))
    return found


def _record_ids(folder: Path, ext_id: str) -> set[str]:
    types, _ = read_record_types(_folder_reader(folder / "plugin"), extension_id=ext_id)
    return {item["id"] for item in types}


def check_folders(folders: Iterable[Path], *, shipped: frozenset[str] | None = None) -> dict[str, list[str]]:
    """``check_folder`` for each folder, keyed by folder name, plus the findings that need several folders."""
    folders = [Path(folder).resolve() for folder in folders]
    names = Counter(folder.name for folder in folders)
    keys = [folder.name if names[folder.name] == 1 else str(folder) for folder in folders]
    result = {key: check_folder(folder, shipped=shipped) for key, folder in zip(keys, folders)}
    linked = {key for key, folder in zip(keys, folders) if _link_findings(folder)}  # Read nothing through a link.
    ids = {key: folder.name if key in linked else _identity(folder) for key, folder in zip(keys, folders)}
    records = {key: set() if key in linked else _record_ids(folder, ids[key]) for key, folder in zip(keys, folders)}
    for key in keys:
        for other in keys:
            if other == key:
                continue
            if ids[other] == ids[key]:
                result[key].append(f"The extension id {ids[key]!r} is also used by the folder {other!r}.")
            for record in sorted(records[key] & records[other]):
                result[key].append(f"The record type id {record!r} is also used by the extension {ids[other]!r}.")
    return result


LOADED = ("with --load also 5 (the tool names), 8 and 10, by loading the code part in a process of its own, and the "
          "release's rule (b) of the names where the checkout has it")
NOT_CHECKED_LOADED = "5 (that a skill's read_for covers each tool), 9, 11, 12, 13"


@cache
def _names_tool():
    """The release's name rules (``tools/distribution/extension_names.py``) of the checkout this app is in, or None in
    an installed app, which has no checkout: rule (b) is then not checked."""
    path = APP.parent / "tools" / "distribution" / "extension_names.py"
    if not path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("extension_contract_names", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_findings(folder: Path) -> list[str]:
    """The findings of loading the code part of a folder that passes the file rules, as a start loads it, in a process
    of its own (``qorqut.bootstrap.extension_tables.declared``): its factory, the parts it uses, its tool names and the
    tables it declares (``tables()``, whose statements are checked as a recorded row is). A folder without
    ``code.factory`` has none. Nothing of its code is imported into this process."""
    from qorqut_operations.contracts import DomainError
    from qorqut.bootstrap.extension_tables import declared
    from qorqut.composition.extension_tables import contribution_of
    from qorqut.extension_registry import TABLES_REFUSED
    manifest = load_manifest(folder)
    names = _names_tool()
    if names is not None:
        # Rule (b): no name Qorqbot can put into a database may start with the id followed by _.
        taken = names.taken_prefix(manifest.id)
        if taken is not None:
            sentence = names.taken_sentence(manifest.id, taken)
            return [sentence[0].upper() + sentence[1:]]
    if manifest.factory is None:
        return []
    row, finding = declared(folder)
    if finding is not None:
        return [finding]
    if row is not None:
        try:
            contribution_of(row)
        except DomainError as exc:
            return [TABLES_REFUSED.format(exc.message)]
    return []


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    load = "--load" in arguments
    folders = [Path(item) for item in arguments if item != "--load"]
    if not folders:
        print("Usage: python -m qorqut.extension_contract [--load] <folder> [<folder> ...]", file=sys.stderr)
        return 2
    findings = check_folders(folders)
    if load:
        for folder, name in zip(folders, findings):
            if not findings[name]:
                findings[name] = load_findings(folder)
    for name, items in findings.items():
        for item in items:
            print(f"{name}: {item}")
    print(f"Contract rules checked: {CHECKED}{'; ' + LOADED if load else ''}. "
          f"Not checked yet: rules {NOT_CHECKED_LOADED if load else NOT_CHECKED} of section 12.")
    return 1 if any(findings.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
