"""Declared UTF-8 instruction fragments with literal, one-pass substitution.

The copied-source index is a native contract, never an authored authority file.
Only bundled() reads seed bytes; render() always requires the injected public view.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import stat

from qorqut_operations.contracts import DomainError

from .support import ImportFailure, relative

MAX_TEXT_BYTES = 262_144
TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
PARAMETER = re.compile(r"[A-Z][A-Z0-9_]*\Z")
INDEX_PATHS = {"fastmail": "qorqut_fastmail/resources/index.json",
               "host": "app/bazarlyk/agentic/assets/index.json",
               "paper": "qorqut_paper/agentic-assets/index.json",
               "telegram": "qorqut_telegram/resources/index.json",
               "whatsapp": "qorqut_whatsapp/index.json"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate index key: {key}")
        result[key] = value
    return result


def parse_index(raw: bytes) -> list[dict]:
    """Validate a bounded declaration without importing any indexed source code."""
    try:
        if len(raw) > MAX_TEXT_BYTES:
            raise ValueError("Index exceeds its byte limit")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        if not isinstance(data, dict) or set(data) != {"schema_version", "entries"} or (type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise ValueError("Expected schema_version 1 and entries")
        if not isinstance(data["entries"], list) or len(data["entries"]) > 512:
            raise ValueError("Expected a bounded entries list")
        paths, identifiers = set(), set()
        for entry in data["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"id", "path", "parameters", "source_site"}:
                raise ValueError("Each declaration needs id, path, parameters, source_site")
            identifier = entry["id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", identifier):
                raise ValueError("Invalid declaration id")
            path = relative(entry["path"])
            if not path.endswith(".md") or path in paths or identifier in identifiers:
                raise ValueError("Declarations must name distinct Markdown files and IDs")
            parameters = entry["parameters"]
            if (not isinstance(parameters, list) or any(not isinstance(p, str) or not PARAMETER.fullmatch(p) for p in parameters)
                    or len(parameters) != len(set(parameters))):
                raise ValueError("Parameters must be distinct UPPER_NAME strings")
            site = entry["source_site"]
            if not isinstance(site, str) or site.count(":") != 1:
                raise ValueError("source_site must be source/path.py:symbol")
            source, symbol = site.split(":")
            if not relative(source).endswith(".py") or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", symbol):
                raise ValueError("Invalid source site")
            paths.add(path); identifiers.add(identifier)
        return data["entries"]
    except (ValueError, TypeError, UnicodeError, ImportFailure) as exc:
        raise ImportFailure("invalid_text_index", str(exc)) from exc


def render_text(text: str, parameters, values) -> str:
    """Require exactly the declared parameters and replace each original token once."""
    required = set(parameters)
    tokens = set(TOKEN.findall(text))
    if "{{" in TOKEN.sub("", text) or "}}" in TOKEN.sub("", text) or tokens != required:
        raise DomainError("invalid_instruction_template", "Instruction tokens must exactly match declared parameters",
                          details={"declared": sorted(required), "tokens": sorted(tokens)})
    if set(values) != required or any(not isinstance(value, str) for value in values.values()):
        raise DomainError("invalid_instruction_parameters", "Supply exactly the declared string parameters",
                          details={"missing": sorted(required - set(values)), "unknown": sorted(set(values) - required)})
    result = TOKEN.sub(lambda match: values[match[1]], text)
    if len(result.encode("utf-8")) > MAX_TEXT_BYTES:
        raise DomainError("instruction_limit", "Rendered instruction exceeds its byte limit")
    return result


def _root(source_id):
    if source_id == "host":
        return Path(__file__).resolve().parents[2]
    if source_id in {"paper", "telegram", "whatsapp", "fastmail"}:
        spec = importlib.util.find_spec("qorqut_" + source_id)
        if spec and spec.submodule_search_locations:
            return Path(next(iter(spec.submodule_search_locations))).parent
    raise DomainError("unknown_instruction_source", f"No installed instruction contract for {source_id}")


def _read_local(source_id, path):
    """Read one explicitly bounded seed/index path without following links."""
    relative(path)
    from qorqut_agentic.behavior.sources import location, seed_bytes
    if location(source_id, path) is not None:
        body = seed_bytes(source_id, path)
        if len(body) > MAX_TEXT_BYTES:
            raise DomainError("instruction_limit", "Instruction resource exceeds its byte limit")
        return body
    # Host source paths include app/, whereas installed distributions start at bazarlyk/.
    root = _root(source_id)
    if source_id == "host":
        path = path.removeprefix("app/")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        parts = (*root.parts[1:], *path.split("/"))
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            state = os.fstat(file)
            if not stat.S_ISREG(state.st_mode) or state.st_size > MAX_TEXT_BYTES:
                raise DomainError("invalid_instruction_resource", "Instruction resource must be a bounded regular file")
            with os.fdopen(file, "rb", closefd=False) as stream:
                data = stream.read(MAX_TEXT_BYTES + 1)
            if len(data) > MAX_TEXT_BYTES:
                raise DomainError("instruction_limit", "Instruction resource exceeds its byte limit")
            return data
        finally:
            os.close(file)
    finally:
        os.close(descriptor)


def declaration(source_id, source_path):
    relative(source_path)
    if source_id not in INDEX_PATHS:
        raise DomainError("unknown_instruction_source", f"No instruction index for {source_id}")
    entries = parse_index(_read_local(source_id, INDEX_PATHS[source_id]))
    for entry in entries:
        if entry["path"] == source_path:
            return entry
    raise DomainError("undeclared_instruction", f"Instruction is absent from the native index: {source_id}:{source_path}")


def read_resource(view, source_id, source_path, *, symbol=None) -> dict:
    """Read current text only through the public view; missing resources never fall back."""
    relative(source_path)
    if view is None:
        raise DomainError("missing_agentic_view", "Active instruction composition requires an agentic view")
    resource = view.resource(source_id, source_path, **({"symbol": symbol} if symbol is not None else {}))
    text = resource.get("content")
    if resource.get("encoding") != "utf-8" or not isinstance(text, str):
        raise DomainError("invalid_instruction_resource", f"Instruction must be UTF-8: {source_id}:{source_path}")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise DomainError("instruction_limit", f"Instruction exceeds its byte limit: {source_path}")
    return resource


def render(view, source_id, source_path, **values) -> str:
    contract = declaration(source_id, source_path)
    return render_text(read_resource(view, source_id, source_path)["content"], contract["parameters"], values)


def bundled(source_id, source_path, **values) -> str:
    """Explicit offline seed rendering, never a runtime fallback for supplied views."""
    contract = declaration(source_id, source_path)
    return render_text(_read_local(source_id, source_path).decode("utf-8", "strict"), contract["parameters"], values)
