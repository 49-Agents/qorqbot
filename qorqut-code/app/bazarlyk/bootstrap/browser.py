"""Explicitly invoked, workspace-bound agent-browser adapter (not a sandbox)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
import re
import secrets
import stat
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage import safe_io
from .client import BootstrapClient, load_registration, path_has_symlink, write_json_private

from . import agent_browser

CLI_VERSION = agent_browser.VERSION
RUNTIME_WRAPPER = "run-agent-browser"
SKILL_NAME = "bazarlyk-role-browser-control"
COMMANDS = frozenset("""
attach detach goto type click dblclick fill drag hover select upload check
uncheck snapshot find eval dialog-accept dialog-dismiss resize go-back go-forward
reload press screenshot pdf tab-list tab-new tab-close tab-select console observe
""".split())
GLOBAL_OPTIONS = {"--session", "-s", "--cdp", "--endpoint", "--extension", "--config",
                  "--browser", "--profile", "--persistent", "--headed", "--isolated"}

OBSERVATION_DEFAULT_BUDGET = 4000
OBSERVATION_MIN_BUDGET = 1000
OBSERVATION_MAX_BUDGET = 24000
OBSERVATION_DEFAULT_DEPTH = 3
OBSERVATION_MAX_DEPTH = 20
OBSERVATION_EVIDENCE_LIMIT = 32 * 1024 * 1024
_OBSERVATION_HANDLE = re.compile(r"^observation-[0-9a-f]{16}\.yml$")
_OBSERVATION_REF = re.compile(r"^[A-Za-z][0-9]+(?:e[0-9]+)?$")
_REF_IN_LINE = re.compile(r"\[ref=([^\]]+)\]")
_ROLE_IN_LINE = re.compile(r"^([^\s\[\]:]+)")
_QUOTED_IN_LINE = re.compile(r'"(?:\\.|[^"\\])*"')
_ATTR_IN_LINE = re.compile(r"\[([^\[\]]+)\]")
_CRITICAL_ROLES = frozenset({"alert", "alertdialog", "dialog", "status"})
_STATE_ATTRS = frozenset({"checked", "selected", "disabled", "invalid", "expanded", "pressed", "required", "readonly"})
_CONTROL_ROLES = frozenset({
    "button", "checkbox", "combobox", "gridcell", "link", "listbox", "menuitem", "option",
    "radio", "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
})
_STRUCTURAL_ROLES = frozenset({
    "alert", "alertdialog", "columnheader", "dialog", "form", "heading", "main", "navigation",
    "region", "row", "rowgroup", "status", "table",
})


class BrowserError(ValueError):
    pass


class BrowserTimeout(BrowserError):
    """The CLI was killed at its bound; the page may still have changed."""


@dataclass(frozen=True)
class ObservationRequest:
    mode: str
    query: str | None = None
    budget: int = OBSERVATION_DEFAULT_BUDGET
    offset: int = 0
    depth: int = OBSERVATION_DEFAULT_DEPTH
    region: str | None = None
    ref: str | None = None
    observation: str | None = None


@dataclass
class _SnapshotItem:
    index: int
    indent: int
    depth: int
    raw: str
    role: str
    name: str
    ref: str | None
    value: str | None
    attrs: dict[str, str | bool]
    parent: int | None
    region: str = ""
    critical: bool = False


def _option_value(tokens: list[str], index: int, name: str) -> tuple[str, int]:
    token = tokens[index]
    prefix = name + "="
    if token.startswith(prefix):
        value = token[len(prefix):]
        if not value:
            raise BrowserError(f"{name} requires a value.")
        return value, index
    if token == name:
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("-"):
            raise BrowserError(f"{name} requires a value.")
        return tokens[index + 1], index + 1
    raise AssertionError("option name mismatch")


def parse_observation_args(args: list[str]) -> ObservationRequest:
    if len(args) < 2 or args[0] != "observe":
        raise BrowserError("observe requires a mode: overview, find, region, values or table.")
    mode = args[1].lower()
    if mode not in {"overview", "find", "region", "values", "table"}:
        raise BrowserError("observe mode must be overview, find, region, values or table.")
    query = None
    budget, offset, depth = OBSERVATION_DEFAULT_BUDGET, 0, OBSERVATION_DEFAULT_DEPTH
    region = ref = observation = None
    tokens = args[2:]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            if token.split("=", 1)[0] in GLOBAL_OPTIONS or token.startswith("-s="):
                raise BrowserError("Browser target, config and session overrides are disabled.")
            if token in {"--budget"} or token.startswith("--budget="):
                value, index = _option_value(tokens, index, "--budget")
                try:
                    budget = int(value)
                except ValueError as exc:
                    raise BrowserError("Observation budget must be an integer.") from exc
                if not OBSERVATION_MIN_BUDGET <= budget <= OBSERVATION_MAX_BUDGET:
                    raise BrowserError(f"Observation budget must be between {OBSERVATION_MIN_BUDGET} and {OBSERVATION_MAX_BUDGET} characters.")
            elif token in {"--offset"} or token.startswith("--offset="):
                value, index = _option_value(tokens, index, "--offset")
                try:
                    offset = int(value)
                except ValueError as exc:
                    raise BrowserError("Observation offset must be an integer.") from exc
                if offset < 0:
                    raise BrowserError("Observation offset must not be negative.")
            elif token in {"--depth"} or token.startswith("--depth="):
                value, index = _option_value(tokens, index, "--depth")
                try:
                    depth = int(value)
                except ValueError as exc:
                    raise BrowserError("Observation depth must be an integer.") from exc
                if not 0 <= depth <= OBSERVATION_MAX_DEPTH:
                    raise BrowserError(f"Observation depth must be between 0 and {OBSERVATION_MAX_DEPTH}.")
            elif token in {"--region"} or token.startswith("--region="):
                region, index = _option_value(tokens, index, "--region")
            elif token in {"--ref"} or token.startswith("--ref="):
                ref, index = _option_value(tokens, index, "--ref")
                if not _OBSERVATION_REF.fullmatch(ref):
                    raise BrowserError("Observation ref must look like e2 or f1e3.")
            elif token in {"--observation"} or token.startswith("--observation="):
                observation, index = _option_value(tokens, index, "--observation")
                if not _OBSERVATION_HANDLE.fullmatch(observation):
                    raise BrowserError("Observation handle is invalid; use a handle returned by observe.")
            else:
                raise BrowserError(f"Unsupported observe option: {token}")
        elif query is None:
            query = token
        else:
            raise BrowserError("observe accepts at most one positional query.")
        index += 1
    if mode == "find" and not query:
        raise BrowserError("observe find requires a query.")
    if mode == "region" and not (query or region or ref):
        raise BrowserError("observe region requires a query, --region or --ref.")
    if observation and args[1].lower() not in {"overview", "find", "region", "values", "table"}:
        raise BrowserError("Observation handle can only be used with a supported observe mode.")
    return ObservationRequest(mode, query, budget, offset, depth, region, ref, observation)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, str) else value[1:-1]
        except ValueError:
            return value[1:-1]
    return value


def _split_snapshot_value(text: str, role: str) -> tuple[str, str | None]:
    """Split a CLI accessibility line without treating colons in names as values."""
    bracket_depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "[":
            bracket_depth += 1
        elif character == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif character == ":" and bracket_depth == 0:
            prefix = text[:index].rstrip()
            value = text[index + 1:].strip()
            # Playwright represents a text node as `text: Label`; the colon is
            # part of that node's descriptor rather than a value separator.
            if role == "text" and prefix == role:
                return text, None
            return prefix, value or None
    return text.strip(), None


def _snapshot_attrs(text: str) -> dict[str, str | bool]:
    attrs: dict[str, str | bool] = {}
    for match in _ATTR_IN_LINE.finditer(text):
        token = match.group(1).strip()
        if token.startswith("ref="):
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            value = _unquote(value)
            if value.lower() in {"true", "false"}:
                attrs[key] = value.lower() == "true"
            else:
                attrs[key] = value
        elif token:
            attrs[token] = True
    return attrs


def _parse_snapshot_item(content: str, index: int, indent: int, depth: int, parent: int | None) -> _SnapshotItem:
    match = _ROLE_IN_LINE.match(content)
    role = match.group(1).lower() if match else "generic"
    descriptor, value = _split_snapshot_value(content, role)
    if role == "text" and content.startswith("text:"):
        name = content.partition(":")[2].strip()
        value = None
    elif role == "text" and descriptor.strip() == "text":
        name = value or ""
        value = None
    else:
        quoted = _QUOTED_IN_LINE.search(descriptor)
        if quoted:
            name = _unquote(quoted.group(0))
        else:
            remainder = descriptor[len(match.group(0)):].strip() if match else descriptor
            remainder = _ATTR_IN_LINE.sub("", remainder).strip()
            name = remainder
    ref_match = _REF_IN_LINE.search(content)
    ref = ref_match.group(1) if ref_match else None
    return _SnapshotItem(index=index, indent=indent, depth=depth, raw=content,
                         role=role, name=name, ref=ref, value=_unquote(value) if value is not None else None,
                         attrs=_snapshot_attrs(content), parent=parent)


def _snapshot_label(item: _SnapshotItem) -> str:
    if not item.name and item.role in {"generic", "group", "none"}:
        return ""
    return item.role + ((":" + item.name) if item.name else "")


def _parse_snapshot(text: str) -> list[_SnapshotItem]:
    items: list[_SnapshotItem] = []
    stack: list[tuple[int, int]] = []
    for line in text.splitlines():
        match = re.match(r"^(?P<indent>[ \t]*)-\s+(?P<content>.*)$", line)
        if not match:
            continue
        indent_text = match.group("indent").replace("\t", "    ")
        indent = len(indent_text)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else None
        depth = items[parent].depth + 1 if parent is not None else 0
        item = _parse_snapshot_item(match.group("content"), len(items), indent, depth, parent)
        items.append(item)
        stack.append((indent, item.index))
    for item in items:
        ancestors: list[_SnapshotItem] = []
        parent = item.parent
        critical_ancestor = False
        while parent is not None:
            ancestor = items[parent]
            ancestors.append(ancestor)
            critical_ancestor = critical_ancestor or ancestor.role in _CRITICAL_ROLES
            parent = ancestor.parent
        item.region = " / ".join(label for ancestor in reversed(ancestors)
                                  if (label := _snapshot_label(ancestor)))
        item.critical = (item.role in _CRITICAL_ROLES or critical_ancestor or
                         item.attrs.get("invalid") is True or "invalid" in item.attrs)
    # A selected option is the useful value of a combobox even though the CLI
    # prints it on a nested option line.
    for item in items:
        if item.role not in {"combobox", "listbox"} or item.value:
            continue
        for child in items[item.index + 1:]:
            if child.depth <= item.depth:
                break
            if child.role == "option" and child.attrs.get("selected") is True:
                item.value = child.name
                break
    return items


def _item_matches(item: _SnapshotItem, query: str) -> bool:
    needle = query.casefold()
    return needle in "\n".join(filter(None, (item.raw, item.role, item.name, item.value or "", item.region))).casefold()


def _descendants(items: list[_SnapshotItem], root: int) -> set[int]:
    if root < 0 or root >= len(items):
        return set()
    depth = items[root].depth
    result = {root}
    for item in items[root + 1:]:
        if item.depth <= depth:
            break
        result.add(item.index)
    return result


def _ancestors(items: list[_SnapshotItem], indexes: set[int]) -> set[int]:
    result = set(indexes)
    for index in tuple(indexes):
        parent = items[index].parent
        while parent is not None:
            result.add(parent)
            parent = items[parent].parent
    return result


def _table_roots(items: list[_SnapshotItem], indexes: set[int]) -> set[int]:
    roots: set[int] = set()
    for index in indexes:
        current = index
        while current is not None:
            if items[current].role == "table":
                roots.add(current)
                break
            current = items[current].parent
    return roots


def _select_snapshot_items(items: list[_SnapshotItem], request: ObservationRequest) -> tuple[list[_SnapshotItem], int]:
    if request.ref and not any(item.ref == request.ref for item in items):
        raise BrowserError(f"Observation ref {request.ref} was not found in the evidence artifact.")
    selected: set[int]
    if request.mode == "overview":
        selected = {item.index for item in items
                    if item.depth <= request.depth or item.role in _CONTROL_ROLES or
                    item.role in _STRUCTURAL_ROLES or item.critical}
        selected = _ancestors(items, selected)
    elif request.mode == "find":
        selected = {item.index for item in items if _item_matches(item, request.query or "")}
        selected = _ancestors(items, selected)
    elif request.mode == "region":
        selected = set()
        query = request.query or request.region
        if request.ref:
            root = next((item.index for item in items if item.ref == request.ref), None)
            if root is not None:
                selected.update(_descendants(items, root))
        if query:
            matches = {item.index for item in items
                       if query.casefold() in (_snapshot_label(item) + " / " + item.region).casefold()}
            for index in matches:
                selected.update(_descendants(items, index))
        selected = _ancestors(items, selected)
    elif request.mode == "values":
        selected = {item.index for item in items if item.value is not None or any(key in item.attrs for key in _STATE_ATTRS)}
        if request.query:
            matches = {item.index for item in items if _item_matches(item, request.query or "")}
            scope = set().union(*(_descendants(items, root) for root in matches)) if matches else set()
            selected &= scope
            selected = _ancestors(items, selected)
    else:  # table
        if request.query:
            matches = {item.index for item in items if _item_matches(item, request.query)}
            roots = _table_roots(items, matches)
            selected = set().union(*(_descendants(items, root) for root in roots)) if roots else matches
        else:
            roots = {item.index for item in items if item.role == "table"}
            selected = set().union(*(_descendants(items, root) for root in roots)) if roots else set()
        selected = _ancestors(items, selected)
    if request.ref and request.mode != "region":
        root = next((item.index for item in items if item.ref == request.ref), None)
        selected &= _descendants(items, root) if root is not None else set()
    if request.region and request.mode != "region":
        region_matches = {item.index for item in items
                          if request.region.casefold() in (_snapshot_label(item) + " / " + item.region).casefold()}
        region_scope = set().union(*(_descendants(items, root) for root in region_matches)) if region_matches else set()
        selected &= region_scope
    # Dialogs, alerts, statuses and invalid controls remain visible even when a
    # narrow scope excludes them; their refs/errors are action-critical.
    critical = {item.index for item in items if item.critical}
    ordered = [items[index] for index in sorted(critical)] + [items[index] for index in sorted(selected - critical)]
    return ordered, len(items) - len(selected)


def _public_snapshot_item(item: _SnapshotItem) -> dict:
    result = {"ref": item.ref, "role": item.role, "name": item.name, "region": item.region}
    if item.ref is None:
        result.pop("ref")
    if item.value is not None:
        result["value"] = item.value
    for key in _STATE_ATTRS:
        if key in item.attrs:
            result[key] = item.attrs[key]
    if item.critical:
        result["critical"] = True
    return result


def _read_private_text(path: Path, *, limit: int = OBSERVATION_EVIDENCE_LIMIT) -> str:
    if path_has_symlink(path):
        raise BrowserError("Observation evidence crosses a symbolic link.")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise BrowserError("Observation evidence is missing; request a fresh observe command.") from exc
    except OSError as exc:
        raise BrowserError("Observation evidence cannot be opened safely.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BrowserError("Observation evidence is not an ordinary file.")
        if info.st_mode & 0o077:
            raise BrowserError("Observation evidence is not private.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise BrowserError("Observation evidence exceeds the safe size limit; the full artifact remains available for operator recovery.")
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BrowserError("Observation evidence is not valid UTF-8.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_artifact(path: Path, content: bytes) -> None:
    safe_io.atomic_write(path, content, mode=0o600, enforce_mode=True)


def _artifact_reference(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _generated_observation_paths(directory: Path, handle: str) -> tuple[Path, Path]:
    if not _OBSERVATION_HANDLE.fullmatch(handle):
        raise BrowserError("Observation handle is invalid; use a handle returned by observe.")
    evidence = directory / handle
    metadata = directory / (handle[:-4] + ".meta.json")
    if path_has_symlink(evidence) or path_has_symlink(metadata):
        raise BrowserError("Observation evidence crosses a symbolic link.")
    return evidence, metadata


def _cli_metadata(output: str) -> dict[str, str]:
    metadata = {"url": "", "title": ""}
    for line in output.splitlines():
        if line.startswith("- Page URL:"):
            metadata["url"] = line.partition(":")[2].strip()
        elif line.startswith("- Page Title:"):
            metadata["title"] = line.partition(":")[2].strip()
    return metadata


def _captured_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _write_error_artifact(directory: Path, output: str) -> Path | None:
    path = directory / ("observation-error-" + secrets.token_hex(8) + ".txt")
    try:
        _private_artifact(path, output.encode("utf-8", errors="replace"))
        return path
    except (DomainError, OSError, ValueError):
        return None


def _encode_json(payload: dict) -> str:
    if "budget" not in payload:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    budget = payload["budget"]
    for _ in range(8):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        chars, estimated = len(encoded), (len(encoded) + 3) // 4
        if budget.get("chars") == chars and budget.get("estimatedTokens") == estimated:
            return encoded
        budget["chars"], budget["estimatedTokens"] = chars, estimated
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _emit_observation_error(workspace: Path, directory: Path, request: ObservationRequest,
                            message: str, *, details: str = "", code: int = 2,
                            evidence: Path | None = None, capture=False):
    artifact = _write_error_artifact(directory, details or message)
    payload = {"status": "failed", "mode": request.mode, "code": "observation_failed",
               "exit_code": code,
               "message": message[:512], "message_omitted": len(message) > 512,
               "error_omitted": bool(details), "budget": {"maxChars": request.budget, "chars": 0,
               "estimatedTokens": 0, "estimator": "ceil(chars/4)"}}
    if artifact is not None:
        payload["error_artifact"] = _artifact_reference(workspace, artifact)
    else:
        payload["error_artifact"] = None
        payload["error_artifact_error"] = "The complete error output could not be saved safely."
    if evidence is not None:
        payload["evidence"] = _artifact_reference(workspace, evidence)
    encoded = _encode_json(payload)
    # Error metadata is deliberately small, but keep the contract bounded even
    # if a future path/error field grows unexpectedly.
    if len(encoded) > request.budget:
        payload["message"] = "Observation failed; inspect the error artifact."
        payload["message_omitted"] = True
        encoded = _encode_json(payload)
    payload["budget"]["estimatedTokens"] = (len(encoded) + 3) // 4
    encoded = _encode_json(payload)
    if capture:
        return code, encoded, ""
    print(encoded)
    return code


def _page_metadata(metadata: dict, budget: int) -> tuple[dict, list[str]]:
    page: dict[str, str] = {}
    omitted: list[str] = []
    for key in ("url", "title"):
        value = metadata.get(key, "")
        if not isinstance(value, str):
            value = str(value)
        if len(value) > 512:
            page[key] = "[omitted; see evidence]"
            omitted.append(key)
        else:
            page[key] = value
    return page, omitted


def _observation_payload(workspace: Path, evidence: Path, request: ObservationRequest,
                         metadata: dict, items: list[_SnapshotItem], omitted: int, excluded: int,
                         total: int, next_offset: int | None, critical_omitted: int,
                         oversized_item: _SnapshotItem | None = None) -> dict:
    page, page_omitted = _page_metadata(metadata, request.budget)
    payload = {"status": "ok", "mode": request.mode, "observation": evidence.name,
               "page": page, "items": [_public_snapshot_item(item) for item in items],
               "offset": request.offset, "omitted": omitted, "excluded": excluded, "total": total,
               "evidence": _artifact_reference(workspace, evidence),
               "budget": {"maxChars": request.budget, "chars": 0, "estimatedTokens": 0,
                          "estimator": "ceil(chars/4)"}}
    if next_offset is not None:
        payload["next"] = next_offset
    if omitted:
        payload["omitted_reason"] = ("item exceeds the available budget; inspect evidence or retry with a larger --budget"
                                      if oversized_item else
                                      "budget; whole items are retained in the evidence artifact")
    if critical_omitted:
        payload["critical_omitted"] = critical_omitted
    if oversized_item is not None:
        payload["oversized_item"] = {"index": oversized_item.index, "role": oversized_item.role,
                                     **({"ref": oversized_item.ref} if oversized_item.ref else {}),
                                     "recovery": "Inspect evidence or retry with a larger --budget."}
        payload["pagination_stalled"] = True
    if page_omitted:
        payload["page_omitted"] = page_omitted
    encoded = _encode_json(payload)
    # There should be ample room at the enforced minimum budget. If metadata
    # ever grows past it, remove only non-actionable page metadata explicitly.
    if len(encoded) > request.budget and not items:
        payload["page"] = {}
        payload["page_omitted"] = sorted(set(payload.get("page_omitted", [])) | {"url", "title"})
        encoded = _encode_json(payload)
    payload["budget"]["estimatedTokens"] = (len(encoded) + 3) // 4
    return payload


def _render_observation(workspace: Path, evidence: Path, request: ObservationRequest,
                        metadata: dict, all_items: list[_SnapshotItem]) -> str:
    scoped, excluded = _select_snapshot_items(all_items, request)
    offset = min(request.offset, len(scoped))
    candidates = scoped[offset:]
    page: list[_SnapshotItem] = []
    for item in candidates:
        trial = page + [item]
        omitted = len(candidates) - len(trial)
        next_offset = offset + len(trial) if omitted else None
        critical_omitted = sum(1 for remaining in candidates[len(trial):] if remaining.critical)
        payload = _observation_payload(workspace, evidence, request, metadata, trial, omitted,
                                        excluded, len(all_items), next_offset, critical_omitted)
        if len(_encode_json(payload)) > request.budget:
            break
        page = trial
    omitted = len(scoped) - offset - len(page)
    oversized_item = candidates[0] if candidates and not page else None
    next_offset = offset + len(page) if omitted and oversized_item is None else None
    critical_omitted = sum(1 for remaining in scoped[offset + len(page):] if remaining.critical)
    payload = _observation_payload(workspace, evidence, request, metadata, page, omitted, excluded,
                                   len(all_items), next_offset, critical_omitted, oversized_item)
    return _encode_json(payload)


def _run_observation(workspace: Path, directory: Path, executable: Path, session: str,
                     request: ObservationRequest, env: dict[str, str], capture=False):
    evidence = None
    if request.observation:
        try:
            evidence, metadata_path = _generated_observation_paths(directory, request.observation)
            snapshot = _read_private_text(evidence)
            metadata = json.loads(_read_private_text(metadata_path, limit=1024 * 1024))
            expected = metadata.get("sha256") if isinstance(metadata, dict) else None
            if not isinstance(metadata, dict) or not isinstance(expected, str) or hashlib.sha256(snapshot.encode()).hexdigest() != expected:
                raise BrowserError("Observation evidence changed; request a fresh observe command.")
            page = metadata.get("page", {})
            if not isinstance(page, dict):
                raise BrowserError("Observation metadata is invalid; request a fresh observe command.")
        except (BrowserError, OSError, ValueError, UnicodeError) as exc:
            return _emit_observation_error(workspace, directory, request, str(exc), capture=capture)
    else:
        handle = "observation-" + secrets.token_hex(8) + ".yml"
        command = agent_browser.command(executable, session, env["QORQUT_OBSERVATION_CDP"], directory, ["snapshot"])
        command.insert(1, "--json")
        stdout = stderr = ""
        try:
            evidence, metadata_path = _generated_observation_paths(directory, handle)
            completed = subprocess.run(command, cwd=directory, env=env, timeout=45, check=False,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return_code = getattr(completed, "returncode", 2)
            stdout, stderr = _captured_text(getattr(completed, "stdout", "")), _captured_text(getattr(completed, "stderr", ""))
        except subprocess.TimeoutExpired as exc:
            details = _captured_text(getattr(exc, "stdout", "")) + "\n--- stderr ---\n" + _captured_text(getattr(exc, "stderr", ""))
            return _emit_observation_error(workspace, directory, request,
                                           "Browser observation timed out; inspect the page before retrying.",
                                           details=details, code=2)
        except OSError as exc:
            return _emit_observation_error(workspace, directory, request,
                                           "Could not execute the installed browser runtime.", details=str(exc), code=2)
        if return_code:
            details = stdout + "\n--- stderr ---\n" + stderr
            preview = stderr.strip() or stdout.strip() or "agent-browser snapshot failed."
            return _emit_observation_error(workspace, directory, request, preview, details=details, code=return_code)
        try:
            payload = json.loads(stdout)
            if not payload.get("success"):
                raise BrowserError(str(payload.get("error", "Snapshot failed")))
            snapshot_text = payload.get("data", {}).get("snapshot")
            if not isinstance(snapshot_text, str):
                raise BrowserError("agent-browser returned no snapshot")
            with evidence.open("x", encoding="utf-8") as output:
                output.write(snapshot_text)
            if path_has_symlink(evidence):
                raise BrowserError("Browser observation wrote a symbolic link instead of evidence.")
            descriptor = os.open(evidence, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise BrowserError("Browser observation did not write an ordinary evidence file.")
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = None
                    content = stream.read(OBSERVATION_EVIDENCE_LIMIT + 1)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            if len(content) > OBSERVATION_EVIDENCE_LIMIT:
                raise BrowserError("Browser observation evidence exceeds the safe size limit.")
            snapshot = content.decode("utf-8")
            if not snapshot.strip():
                raise BrowserError("Browser observation produced an empty evidence artifact.")
            page = _cli_metadata(stdout)
            metadata = {"schema_version": 1, "observation": handle,
                        "sha256": hashlib.sha256(content).hexdigest(), "page": page}
            write_json_private(metadata_path, metadata)
        except (BrowserError, DomainError, OSError, UnicodeError, ValueError) as exc:
            details = stdout + "\n--- stderr ---\n" + stderr + "\n--- observation error ---\n" + str(exc)
            return _emit_observation_error(workspace, directory, request, str(exc), details=details, code=2,
                                           capture=capture)
    try:
        parsed = _parse_snapshot(snapshot)
        if not parsed:
            raise BrowserError("Browser observation evidence is empty or not a recognized accessibility snapshot.")
        encoded = _render_observation(workspace, evidence, request, page, parsed)
        if capture:
            return 0, encoded, ""
        print(encoded)
        return 0
    except (BrowserError, OSError, ValueError, UnicodeError) as exc:
        return _emit_observation_error(workspace, directory, request, str(exc), evidence=evidence,
                                       capture=capture)


def load_target(workspace: Path) -> tuple[str, Path]:
    """No fallback browser: an operator must explicitly configure this workspace."""
    try:
        target = json.loads((workspace / ".bazarlyk/browser.json").read_text())
    except (OSError, ValueError) as exc:
        raise BrowserError("Browser is not configured. Ask the operator to install this workspace's browser target.") from exc
    if (not isinstance(target, dict) or set(target) != {"schema_version", "cdp_endpoint", "runtime_dir"}
            or type(target["schema_version"]) is not int or target["schema_version"] != 1):
        raise BrowserError("Invalid browser target configuration (expected schema version 1).")
    endpoint, runtime = target["cdp_endpoint"], target["runtime_dir"]
    if not isinstance(endpoint, str) or not isinstance(runtime, str):
        raise BrowserError("Browser endpoint and runtime directory must be strings.")
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}
                 and url.port is not None and url.port > 0 and not url.username
                 and not url.password and url.path in {"", "/"} and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise BrowserError("Browser CDP must be an explicit loopback HTTP endpoint with a port.")
    return endpoint.rstrip("/"), runtime_executable(runtime)


def runtime_executable(runtime):
    runtime_path = Path(runtime)
    if not runtime_path.is_absolute():
        raise BrowserError("Browser runtime directory must be absolute.")
    try:
        package = json.loads((runtime_path / "node_modules/agent-browser/package.json").read_text())
        if package.get("name") != "agent-browser" or not isinstance(package.get("version"),str):
            raise ValueError("invalid package metadata")
    except (OSError, ValueError, AttributeError) as exc:
        raise BrowserError("Browser runtime package metadata is unavailable.") from exc
    wrapper = runtime_path / RUNTIME_WRAPPER
    if os.path.lexists(wrapper):
        # A runtime that ships the wrapper is a bundled-Node runtime: an incomplete one fails closed
        # rather than letting agent-browser's `#!/usr/bin/env node` pick up whatever Node the host has.
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise BrowserError("The browser runtime's agent-browser wrapper is not an executable file.")
        node = runtime_path / "node/bin/node"
        if not node.is_file() or not os.access(node, os.X_OK):
            raise BrowserError("The browser runtime's bundled Node is missing or not executable; rebuild the runtime "
                               "(tools/browser/build_runtime.py). The host Node is never used.")
        if not (runtime_path / "node_modules/agent-browser/bin/agent-browser.js").is_file():
            raise BrowserError("The browser runtime's agent-browser package is incomplete; rebuild the runtime.")
        from .features import browser_supported
        if not browser_supported(wrapper,runtime_path):raise BrowserError("Browser runtime lacks the scoped command contract")
        return wrapper
    executable = runtime_path / "node_modules/.bin/agent-browser"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BrowserError("The installed agent-browser executable is unavailable.")
    from .features import browser_supported
    if not browser_supported(executable,runtime_path):raise BrowserError("Browser runtime lacks the scoped command contract")
    return executable


def runtime_directory(executable):
    """The runtime directory ``runtime_executable`` selected this executable from."""
    executable = Path(executable)
    return executable.parent if executable.name == RUNTIME_WRAPPER else executable.parent.parent.parent


def preflight(endpoint: str) -> None:
    # An empty initial target makes Playwright wait indefinitely for navigation.
    # Inspect metadata only; never navigate/close an owner's tab to repair it.
    try:
        with build_opener(ProxyHandler({})).open(endpoint + "/json/list", timeout=5) as response:
            pages = json.load(response)
        if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
            raise ValueError("invalid target list")
    except (OSError, ValueError) as exc:
        raise BrowserError("Configured Chrome is unavailable or CDP did not respond. No browser was launched.") from exc
    if any(page.get("type") == "page" and page.get("url") in {"", ":"} for page in pages):
        raise BrowserError("Chrome has an uninitialized empty-URL tab that blocks Playwright attachment. "
                           "Ask the owner to navigate that tab to about:blank, then retry attach. No tabs were changed.")


def validate_arguments(args: list[str]) -> None:
    if not args or args[0] not in COMMANDS:
        raise BrowserError("Use attach, detach, or a supported page command; browser launch, shutdown and global management are disabled.")
    if args[0] in {"attach", "detach"} and len(args) != 1:
        raise BrowserError("attach/detach take no arguments; the workspace selects the target.")
    for arg in args[1:]:
        if arg.split("=", 1)[0] in GLOBAL_OPTIONS or arg.startswith("-s="):
            raise BrowserError("Browser target, config and session overrides are disabled.")
    if args[0] == "observe":
        parse_observation_args(args)


def run(workspace: Path, args: list[str], *, target=None, instance="", capture=False):
    validate_arguments(args)
    observation_request = parse_observation_args(args) if args[0] == "observe" else None
    endpoint, executable = target or load_target(workspace)
    identity = hashlib.sha256((str(workspace.resolve()) + "\n" + endpoint + instance).encode()).hexdigest()[:20]
    session = "qorqut-" + identity
    directory = workspace / "work/browser" / session
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path_has_symlink(directory):
        raise BrowserError("Browser work directory crosses a symbolic link.")
    env = agent_browser.environment(os.environ)
    with (directory / "command.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BrowserError("Another browser command is running in this workspace; wait for it to finish.") from exc
        if args[0] == "attach":
            preflight(endpoint)
        if observation_request is not None:
            env["QORQUT_OBSERVATION_CDP"] = endpoint
            return _run_observation(workspace, directory, executable, session, observation_request, env, capture=capture)
        try:
            command = agent_browser.command(executable, session, endpoint, directory, args)
        except ValueError as exc:
            raise BrowserError(str(exc)) from exc
        try:
            completed = subprocess.run(command, cwd=directory, env=env, timeout=45, check=False,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if capture:
                return completed.returncode, completed.stdout, completed.stderr
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            return completed.returncode
        except subprocess.TimeoutExpired as exc:
            raise BrowserTimeout("Browser command timed out; its outcome may be uncertain. Inspect the page before retrying an action.") from exc
        except OSError as exc:
            raise BrowserError("Could not execute the installed browser runtime.") from exc


def run_business(workspace, args):
    validate_arguments(args)
    registration = load_registration(workspace / ".bazarlyk/registration.json")
    client = BootstrapClient(registration, timeout=15)
    directory = workspace / "work/browser"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "business-command.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BrowserError("Another browser command is running in this workspace.") from exc
        receipt = directory / "business-attachment.json"
        try:
            saved = json.loads(receipt.read_text()) if receipt.exists() else {}
        except (OSError, ValueError) as exc:
            raise BrowserError("Browser attachment state needs operator recovery.") from exc
        token = saved.get("token")
        if not token:
            if args[0] != "attach":
                raise BrowserError("Attach to this business browser first.")
            token = secrets.token_hex(32)
            # Persist before acquire: a lost HTTP response must not strand an
            # ownership token that only existed in this short-lived process.
            saved = {"token": token}
            write_json_private(receipt, saved)
        def control(action):
            return client.post("/api/agent/browser", {"registration_id": registration["registration_id"],
                "action": action, "token": token})
        if args[0] == "detach":
            if saved.get("target"):
                target = saved["target"]
                result = run(workspace, args, target=(target["cdp_endpoint"], runtime_executable(target["runtime_dir"])),
                             instance=target["instance"])
                if result:
                    return result  # Do not hand a still-connected CLI to another task.
            control("release")
            write_json_private(receipt, {})
            return 0
        target = control("acquire" if args[0] == "attach" else "check")
        if args[0] != "attach" and saved.get("target") != target:
            raise BrowserError("Business browser changed; attach again before continuing.")
        write_json_private(receipt, {"token": token, "target": target})
        return run(workspace, args, target=(target["cdp_endpoint"], runtime_executable(target["runtime_dir"])),
                   instance=target["instance"])


def run_owner(workspace, args):
    # The caller must supply its own native session ID, never a shared latest-session file.
    import uuid
    if len(args) < 3 or args[0] != '--consumer-id':
        raise BrowserError('Ordubashy: pass --consumer-id <your session inbox UUID> before the browser command.')
    try:
        consumer = str(uuid.UUID(args[1]))
    except ValueError as exc:
        raise BrowserError('Invalid session inbox consumer UUID.') from exc
    arguments = args[2:]
    validate_arguments(arguments)
    registration = load_registration(workspace / '.bazarlyk/registration.json')
    client = BootstrapClient(registration, timeout=10)
    # .bazarlyk/browser is the prepared executable launcher, not a directory.
    # Keep native task receipts beside it under a distinct managed directory.
    receipt = workspace / '.bazarlyk/browser-owner' / (consumer + '.json')
    try:
        saved = json.loads(safe_io.read_bytes(receipt, limit=8192, private=True))
    except (OSError, ValueError, TypeError):
        saved = {}
    if arguments[0] == 'detach':
        if saved.get('turn_id'):
            client.post('/api/agent/focus/browser', {
                'registration_id': registration['registration_id'], 'consumer_id': consumer,
                'command': saved.get('command', 'detach'), 'command_id': saved.get('command_id', new_id()),
                'phase': 'finished', 'turn_id': saved['turn_id'], 'lease_id': saved.get('lease_id')})
        write_json_private(receipt, {})
        return 0
    body = {'registration_id': registration['registration_id'], 'consumer_id': consumer,
        'command': arguments[0], 'command_id': new_id()}
    phase = 'active' if saved.get('turn_id') else 'running'
    if phase == 'active':
        body.update({'turn_id': saved['turn_id'], 'lease_id': saved.get('lease_id')})
    started = client.post('/api/agent/focus/browser', {**body, 'phase': phase})
    browser_turn = started.get('browser_turn') if isinstance(started, dict) else None
    if browser_turn is None and saved.get('turn_id'):
        browser_turn = {'turn': {'id': saved['turn_id'], 'lease_id': saved.get('lease_id')},
                        'target': saved.get('target')}
    if isinstance(browser_turn, dict):
        write_json_private(receipt, {"turn_id": browser_turn["turn"]["id"],
            "lease_id": browser_turn["turn"]["lease_id"], "command": body["command"],
            "command_id": body["command_id"], "target": browser_turn.get("target")})
    code = 2
    try:
        target = browser_turn.get('target') if isinstance(browser_turn, dict) else None
        code = run(workspace, arguments,
                   target=(target['cdp_endpoint'], runtime_executable(target['runtime_dir'])) if target else None,
                   instance=target.get('instance', '') if target else '')
        return code
    finally:
        try:
            finished = {**body, 'phase': 'active' if code == 0 else 'failed'}
            if isinstance(browser_turn, dict):
                finished.update({'turn_id': browser_turn['turn']['id'], 'lease_id': browser_turn['turn']['lease_id']})
            client.post('/api/agent/focus/browser', finished)
            if code != 0:
                write_json_private(receipt, {})
        except DomainError:
            # Do not turn a successful external action into a retryable action failure.
            print('Browser: activity update unavailable; inspect the page before retrying any action.', file=sys.stderr)


def main(workspace: str, args: list[str] | None = None, *, business=False) -> int:
    arguments = sys.argv[1:] if args is None else args
    if arguments == ["--help"]:
        print("Workspace Chrome control. Use only on explicit owner request.\n"
              "Commands: " + " ".join(sorted(COMMANDS)) + "\n"
              + ("Target: this agent's registered business browser." if business else
                 "Use --consumer-id <session inbox UUID> <command>. Configure .bazarlyk/browser.json; no target is selected by default."))
        return 0
    try:
        return run_business(Path(workspace), arguments) if business else run_owner(Path(workspace), arguments)
    except (BrowserError, DomainError) as exc:
        print(f"Browser: {exc}", file=sys.stderr)
        return 2
