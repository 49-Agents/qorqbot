"""The extension checker: reads one extension folder and reports every way it breaks the written contract.

The rules are those of ``docs/EXTENSION-CONTRACT.md``: section 5 (the folder), 6 (the manifest), 7 (the plugin part), 8
(hooks and nudges), 9 (record types) and the rule numbers of section 12. This piece reads files only. It never loads
extension code, so the rules that need the loaded code or the built bundle (``NOT_CHECKED``) are not checked here.

``load_manifest`` stops at the first problem with one ``ContractError``; ``check_folder`` returns every finding as one
plain sentence; ``check_folders`` adds the findings that need more than one folder. Run as
``python -m qorqut.extension_contract <folder> [<folder> ...]``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any

from qorqut.extensions import EXTENSION_ID, FACTORY, SOURCE_KEY
from qorqut_agentic.schemas import WORKFLOW_SCHEMA, schema_issues
from qorqut_agentic.catalog import workflow_semantic_issues
from qorqut_agentic.util import load_yaml, split_frontmatter
from qorqut_operations.tasks.models import FIELD_TYPES, NAME as FIELD_NAME, RESERVED_FIELD_NAMES
from qorqut_operations.tasks.types import TYPES

MANIFEST = "extension.yaml"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# The app versions an extension works with: comparisons against a three-number version, joined by commas (">=1.7.0",
# ">=1.7.0,<2.0.0"). Checked here and not with a library, so every interpreter gives the same answer.
_COMPARISON = r"(?:>=|<=|==|!=|>|<)[0-9]+\.[0-9]+\.[0-9]+"
SPECIFIER = re.compile(rf"^{_COMPARISON}(?:,{_COMPARISON})*$")
EVERY = re.compile(r"^([0-9]+)([mhd])$")
DELIVERIES = ("built-in", "downloadable")
PLATFORMS = ("linux", "mac", "windows")
SETUP_KINDS = ("none", "key", "sign-in", "pairing")
HOOK_POINTS = ("start", "owner_message", "after_tool", "stop", "timed")
HOOK_KEYS = frozenset({"id", "at", "text", "tools", "every", "if_used", "unless_used"})
RECORD_KEYS = frozenset({"id", "based_on", "label", "plural", "fields"})
RECORD_FIELD_KEYS = frozenset({"id", "label", "type", "values"})
RECORD_BASES = tuple(name for name in TYPES if name != "email")  # contract section 15: not on `email`
MANIFEST_KEYS = frozenset({"contract", "id", "name", "summary", "version", "delivery", "platforms", "requires", "code",
                           "setup", "agents"})
CHECKED = "1, 2, 3, 4, 6, 7, the README and plugin-part layout, and the workflow files"
NOT_CHECKED = "5, 8, 9, 10, 11, 12, 13"


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
    """A list of strings of one form (roles are checked as ``extensions.py`` does, with ``-`` read as ``_``)."""
    return (isinstance(value, list) and (bool(value) or not nonempty)
            and all(isinstance(item, str) and form.fullmatch(item) for item in value))


def _role(value: Any) -> bool:
    return isinstance(value, str) and SOURCE_KEY.fullmatch(value.replace("-", "_")) is not None


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
        elif not (isinstance(allowed, list) and allowed and all(_role(role) for role in allowed)):
            found.append("`agents.allowed` must be a non-empty list of role names.")
        if "on_after_setup" in agents:
            later = agents["on_after_setup"]
            if not (isinstance(later, list) and all(_role(role) for role in later)):
                found.append("`agents.on_after_setup` must be a list of role names.")
            elif isinstance(allowed, list):
                for role in later:
                    if role not in allowed:
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


def _skill_findings(plugin: Path, ext_id: str) -> list[str]:
    skills = plugin / "skills"
    if not skills.is_dir():
        return []
    found = []
    for folder in sorted(path for path in skills.iterdir() if path.is_dir()):
        where = f"The skill {folder.name!r}"
        if folder.name != ext_id and not folder.name.startswith(ext_id + "-"):
            found.append(f"{where} must be named {ext_id!r}, or {ext_id!r} followed by '-' and more.")
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
        if set(meta) != {"name", "description", "read_for"}:
            found.append(f"{where} must have exactly the front matter lines name, description and read_for.")
        if "name" in meta and meta["name"] != f"qorqut-plugin-{folder.name}":
            found.append(f"{where} must be named 'qorqut-plugin-{folder.name}' in its front matter.")
        if "description" in meta and not _text(meta["description"]):
            found.append(f"{where} has an empty description.")
        if "read_for" in meta:
            line = meta["read_for"]
            # The same rule as qorqut.catalog.packages: one line of 1 to 160 characters (not importable as a helper).
            if not isinstance(line, str) or "\n" in line.strip() or not 1 <= len(line.strip()) <= 160:
                found.append(f"{where} must have read_for as one line of 1 to 160 characters.")
    return found


def _list_file(path: Path, kind: str) -> tuple[list | None, list[str]]:
    """The entries of an optional list file: ``(None, [])`` when it is absent."""
    if not path.is_file():
        return None, []
    data, problem = _read(path)
    if problem:
        return None, [problem]
    if not isinstance(data, list):
        return None, [f"{path.name} must be a list of {kind}."]
    return data, []


def _tool_list(value: Any, *, nonempty: bool) -> bool:
    return _names(value, SOURCE_KEY, nonempty=nonempty)


def _hook_findings(plugin: Path) -> list[str]:
    entries, found = _list_file(plugin / "hooks.yaml", "entries")
    seen: list[str] = []
    for number, entry in enumerate(entries or (), 1):
        label = f"hooks.yaml entry {number}"
        if not isinstance(entry, dict):
            found.append(f"{label} must be a mapping.")
            continue
        found.extend(_unknown(label, entry, HOOK_KEYS))
        if not _text(entry.get("id")):
            found.append(f"{label} needs an `id`.")
        else:
            seen.append(entry["id"])
            label = f"hooks.yaml entry {entry['id']!r}"
        at = entry.get("at")
        if at not in HOOK_POINTS:
            found.append(f"{label} must have `at` as start, owner_message, after_tool, stop or timed.")
        found.extend(_hook_text(plugin, entry, label))
        if at == "after_tool" and "tools" not in entry:
            found.append(f"{label} is at after_tool and needs `tools`.")
        if "tools" in entry:
            if at != "after_tool":
                found.append(f"{label} has `tools`, which is allowed only at after_tool.")
            if not _tool_list(entry["tools"], nonempty=True):
                found.append(f"{label} must have `tools` as a non-empty list of tool names.")
        if at == "timed" and "every" not in entry:
            found.append(f"{label} is timed and needs `every`.")
        if "every" in entry:
            if at != "timed":
                found.append(f"{label} has `every`, which is allowed only at timed.")
            match = EVERY.fullmatch(entry["every"]) if isinstance(entry["every"], str) else None
            if not match or int(match[1]) < 1:
                found.append(f"{label} must have `every` as a whole number above zero and m, h or d, such as 7d.")
        for key in ("if_used", "unless_used"):
            if key in entry and not _tool_list(entry[key], nonempty=False):
                found.append(f"{label} must have `{key}` as a list of tool names.")
    found.extend(f"hooks.yaml repeats the id {name!r}." for name, count in Counter(seen).items() if count > 1)
    return found


def _hook_text(plugin: Path, entry: dict, label: str) -> list[str]:
    text = entry.get("text")
    if "text" not in entry:
        return [f"{label} needs `text`."]
    if not _text(text):
        return [f"{label} must have `text` as a path."]
    if text.startswith(("/", "\\")) or Path(text).is_absolute():
        return [f"{label} has an absolute `text` path."]
    root = plugin.resolve()
    target = (plugin / text).resolve()
    if target != root and root not in target.parents:
        return [f"{label} has a `text` path that leaves the plugin directory."]
    if not target.is_file():
        return [f"{label} names the text file {text!r}, which does not exist in the plugin directory."]
    return []


def _record_findings(plugin: Path) -> list[str]:
    entries, found = _list_file(plugin / "records.yaml", "record types")
    seen: list[str] = []
    for number, entry in enumerate(entries or (), 1):
        label = f"records.yaml entry {number}"
        if not isinstance(entry, dict):
            found.append(f"{label} must be a mapping.")
            continue
        found.extend(_unknown(label, entry, RECORD_KEYS))
        if "id" not in entry:
            found.append(f"{label} needs an `id`.")
        elif not isinstance(entry["id"], str) or not SOURCE_KEY.fullmatch(entry["id"]):
            found.append(f"{label} has an `id` that is not lowercase letters, digits and underscores.")
        else:
            seen.append(entry["id"])
            label = f"The record type {entry['id']!r}"
        if entry.get("based_on") not in RECORD_BASES:
            found.append(f"{label} must be based_on one of {', '.join(RECORD_BASES)}.")
        for key in ("label", "plural"):
            if not _text(entry.get(key)):
                found.append(f"{label} needs a non-empty `{key}`.")
        if "fields" in entry:
            found.extend(_record_field_findings(entry["fields"], label))
    found.extend(f"records.yaml repeats the record type id {name!r}." for name, count in Counter(seen).items()
                 if count > 1)
    return found


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


def _workflow_findings(plugin: Path) -> list[str]:
    """Each file is read as the agentic layer reads one (``qorqut_agentic.catalog``), without its package context."""
    root = plugin / "workflows"
    if not root.is_dir():
        return []
    found = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
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
    return found


def _identity(folder: Path) -> str:
    """The id to hold the folder to: the manifest's when it is a string, else the folder name."""
    path = folder / MANIFEST
    if path.is_file():
        data, problem = _read(path)
        if not problem and isinstance(data, dict) and isinstance(data.get("id"), str):
            return data["id"]
    return folder.name


def check_folder(folder: Path) -> list[str]:
    """Every way ``folder`` breaks the rules this module checks, one sentence each. Empty means it passes."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        return [f"{folder} is not a folder."]
    found: list[str] = []
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
        found.extend(_skill_findings(plugin, ext_id))
        found.extend(_hook_findings(plugin))
        found.extend(_record_findings(plugin))
        found.extend(_workflow_findings(plugin))
    return found


def _record_ids(folder: Path) -> set[str]:
    path = folder / "plugin" / "records.yaml"
    data, problem = _read(path) if path.is_file() else (None, None)
    if problem or not isinstance(data, list):
        return set()
    return {item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)}


def check_folders(folders: Iterable[Path]) -> dict[str, list[str]]:
    """``check_folder`` for each folder, keyed by folder name, plus the findings that need several folders."""
    folders = [Path(folder).resolve() for folder in folders]
    names = Counter(folder.name for folder in folders)
    keys = [folder.name if names[folder.name] == 1 else str(folder) for folder in folders]
    result = {key: check_folder(folder) for key, folder in zip(keys, folders)}
    ids = {key: _identity(folder) for key, folder in zip(keys, folders)}
    records = {key: _record_ids(folder) for key, folder in zip(keys, folders)}
    for key in keys:
        for other in keys:
            if other == key:
                continue
            if ids[other] == ids[key]:
                result[key].append(f"The extension id {ids[key]!r} is also used by the folder {other!r}.")
            for record in sorted(records[key] & records[other]):
                result[key].append(f"The record type id {record!r} is also used by the extension {ids[other]!r}.")
    return result


def main(argv: list[str] | None = None) -> int:
    folders = [Path(item) for item in (sys.argv[1:] if argv is None else argv)]
    if not folders:
        print("Usage: python -m qorqut.extension_contract <folder> [<folder> ...]", file=sys.stderr)
        return 2
    findings = check_folders(folders)
    for name, items in findings.items():
        for item in items:
            print(f"{name}: {item}")
    print(f"Contract rules checked: {CHECKED}. Not checked yet: rules {NOT_CHECKED} of section 12.")
    return 1 if any(findings.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
