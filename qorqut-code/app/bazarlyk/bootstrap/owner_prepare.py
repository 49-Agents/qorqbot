"""Owner-invoked code-folder preparation for owner-directed Claude/Codex sessions.

The owner runs this once per machine and prepared root
(docs/plans/OWNER-DIRECTED-CODE-SESSIONS.md section 5). It writes only its own
files inside the explicitly given roots: a private credential, a headers helper,
a registration receipt, one ``bazarlyk-owner`` MCP entry, one marked instruction
block, one ``.gitignore`` line, and a Codex configuration at the top root.

Unrelated MCP servers, authored instructions, ignore entries and client settings
are preserved; conflicts are detected across every root before anything is
written; removal deletes only owned entries. Nothing here edits the user's
global Claude/Codex configuration, changes an approval mode, installs hooks or
filesystem policy, starts a service, or makes a network call. Configuration is
not assumed to propagate into nested repositories, so each prepared root gets
its own copies with its own absolute helper path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import platform
import re
import shlex
import sys
import tomllib

import tomli_w

from bazarlyk import owner_sessions
from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage import safe_io
from .client import path_has_symlink, service_url

DIRECTORY = ".bazarlyk-owner"
CREDENTIAL = DIRECTORY + "/credential"
HEADERS = DIRECTORY + "/headers"
REGISTRATION = DIRECTORY + "/registration.json"
GITIGNORE_ENTRY = DIRECTORY + "/"
SERVER = "bazarlyk-owner"
ROUTE = "/mcp/owner"
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
BLOCK_START = "<!-- bazarlyk:owner-session:start -->"
BLOCK_END = "<!-- bazarlyk:owner-session:end -->"
CODEX_CONFIG = ".codex/config.toml"
CODEX_MARKER = "# Managed by bazarlyk owner-session preparation"
CODEX_END = "# End bazarlyk owner-session preparation"
CODEX_NEWLINE = "# Original config final newline:"
MODES = {CREDENTIAL: 0o600, HEADERS: 0o700}
# Every file preparation may create or rewrite in a prepared root, and the folders it may create for them.
MANAGED_FILES = (CREDENTIAL, HEADERS, REGISTRATION, ".mcp.json", *INSTRUCTION_FILES, ".gitignore", CODEX_CONFIG)
MANAGED_DIRECTORIES = (DIRECTORY, CODEX_CONFIG.split("/")[0])
VERSION = 1
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,512}$")

# A POSIX sh helper keeps the header command free of any Python dependency, so
# a prepared root works without this installation's interpreter or checkout.
HEADERS_SCRIPT = """#!/bin/sh
# Prints the Qorqut owner-session MCP authorization header for this root.
set -e
printf '{"Authorization":"Bearer %s"}' "$(tr -d '[:space:]' < "$(dirname "$0")/credential")"
"""

REQUIRED_TEXT = (
    "You act on behalf of {owner} in this project. Other sessions are independent. "
    "Follow the owner's current request; do not seek autonomous work, start an inbox/work loop, or claim, "
    "announce, hand off or report work on your own. "
    "A specific owner instruction, such as \"create these tasks\" or \"complete this task\", authorizes that "
    "Qorqut operation without a second confirmation; ask only for missing scope or a materially different action. "
    "Tool access alone never authorizes unrelated Qorqut writes. "
    "Post freeform object notes only in the owner/Ordubashy private conversation. "
    "Your first action in every session must be calling `owner_session_bootstrap`; if it fails, "
    "stop and diagnose the bootstrap before doing other work. If it reports missing diagnostics, "
    "disclose them and request the needed context before relying on it.")
OWNER_FALLBACK = "the installation owner"


def owner_label(name) -> str | None:
    """A registered owner display name safe for one Markdown line, or None."""
    value = " ".join(str(name).split()) if isinstance(name, str) else ""
    if not value or len(value) > 200 or "<!--" in value or "-->" in value:
        return None
    return value


def required_text(owner_name=None) -> str:
    label = owner_label(owner_name)
    return REQUIRED_TEXT.format(owner=f"{label} (the installation owner)" if label else OWNER_FALLBACK)


@dataclass(frozen=True)
class Projection:
    files: dict  # {prepared root: {relative name: bytes}}; the credential is written separately.
    metadata: dict


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _text(path: Path, *, strict=True) -> str | None:
    """Existing UTF-8 content, or None when absent (or unusable during removal)."""
    if path_has_symlink(path):
        if strict:
            raise DomainError("projection_conflict", f"Managed file {path.name} cannot follow a symbolic link")
        return None
    try:
        data = safe_io.read_bytes(path)
    except FileNotFoundError:
        return None
    except DomainError:
        if strict:
            raise
        return None
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        if strict:
            raise DomainError("projection_conflict", f"Existing {path.name} is not UTF-8 text") from exc
        return None


def _require_directory(path: Path, kind: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise DomainError("validation_error", f"The {kind} must be an absolute path without parent components")
    if path_has_symlink(path):
        raise DomainError("projection_conflict", f"The {kind} must not contain a symbolic link")
    if not path.is_dir():
        raise DomainError("validation_error", f"The {kind} must be an existing local directory")


def resolve_roots(code_root, nested=(), outside_roots=()) -> list[Path]:
    """Ordered prepared roots, top code root first; every root is explicit."""
    top = Path(code_root)
    _require_directory(top, "code root")
    roots = [top]
    for entry in nested:
        relative = Path(entry)
        if relative.is_absolute() or ".." in relative.parts or not str(entry).strip():
            raise DomainError("validation_error", "A nested root must be a relative path inside the code root")
        _require_directory(top / relative, "nested root")
        roots.append(top / relative)
    for entry in outside_roots:
        # A checkout outside the code tree is prepared only through this
        # explicit setup decision, never by discovery.
        path = Path(entry)
        _require_directory(path, "prepared root")
        roots.append(path)
    if len(set(roots)) != len(roots):
        raise DomainError("validation_error", "Each prepared root must be given once")
    return roots


def resolve_business(database, business) -> dict:
    value = str(business or "").strip()
    if not value:
        raise DomainError("validation_error", "A project UUID or slug is required")
    with database.read() as uow:
        rows = uow.connection.execute(
            "SELECT id, slug, name, archived_at FROM businesses WHERE id=? OR slug=?", (value, value)).fetchall()
    row = next((row for row in rows if row["id"] == value), rows[0] if rows else None)
    if row is None:
        raise DomainError("not_found", "No project matches this identifier")
    return {"id": row["id"], "slug": row["slug"], "name": row["name"]}


def resolve_owner_name(database) -> str | None:
    """The installation owner's registered display name, when usable in generated text."""
    with database.read() as uow:
        row = uow.connection.execute(
            "SELECT p.display_name FROM installation i JOIN principals p ON p.id=i.owner_principal_id "
            "WHERE i.id='service' AND p.kind='human'").fetchone()
    return owner_label(row["display_name"]) if row else None


def instruction_block(business: dict, endpoint: str, owner_name=None) -> str:
    builder = owner_label(owner_name) or OWNER_FALLBACK
    return "\n".join([
        BLOCK_START,
        f"# Qorqut owner session: {business['name']}",
        "",
        f"Business: {business['name']} (`{business['slug']}`), UUID `{business['id']}`.",
        f"Qorqut service: {endpoint} — MCP server `{SERVER}` at `{endpoint + ROUTE}`.",
        "",
        required_text(owner_name),
        "",
        "- Start by calling the `owner_session_bootstrap` tool for project orientation and context pointers.",
        "- Fetch deeper context on demand with `context_list`, `context_search` and `context_get`; project",
        "  strategy and marketing notes are listed with `notes_list` and read with `notes_get`.",
        "- Project direction belongs to the owner with Ordubashy, who coordinates execution through the permanent",
        "  workers. Projects have no resident agents.",
        "- Use `agents_message_recipients`, `agents_message_send` and `agents_messages_list` for direct",
        "  same-business messages; use `tasks_create` and `tasks_assign` for durable work. Direct messages are",
        "  not the same as owner private conversations on objects.",
        "- Tasks: `tasks_create`, `tasks_update`, close with `tasks_complete`, reopen with `tasks_update` to an open",
        "  status. A work group is an ordinary parent Task (`parent_id`); related items link with `relationships_create`.",
        "- Project Settings and credentials are bound to this project. Use `environment_list` for names",
        "  and retrieve only needed values with `environment_get`; never put secrets in bootstrap, tasks or messages.",
        "- Freeform commentary goes through `owner_conversation_send`; task note fields refuse the owner",
        "  with `channel_required`.",
        "- Qorqut writes need the current revision and an idempotency key; retry a lost response with the same key",
        "  and reread after `revision_conflict`.",
        f"- Software and design building stays with {builder}; ordinary local coding and design work keeps your",
        "  usual client settings.",
        BLOCK_END,
    ])


def _instruction_file(path: Path, block: str) -> bytes:
    text = _text(path)
    if text is None or not text.strip():
        return (block + "\n").encode()
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if (text.count(BLOCK_START) > 1 or text.count(BLOCK_END) > 1
            or (start < 0) != (end < 0) or (start >= 0 and end < start)):
        raise DomainError("projection_conflict",
                          f"Existing {path.name} has an unbalanced owner-session block and needs reconciliation")
    if start >= 0:
        return (text[:start] + block + text[end + len(BLOCK_END):]).encode()
    return (text.rstrip("\n") + "\n\n" + block + "\n").encode()


def _mcp_file(path: Path, endpoint: str, helper: str) -> bytes:
    entry = {"type": "http", "url": endpoint + ROUTE, "headersHelper": helper}
    text = _text(path)
    document = {}
    if text is not None and text.strip():
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise DomainError("projection_conflict",
                              "Existing .mcp.json is not valid JSON and needs reconciliation") from exc
        if not isinstance(document, dict):
            raise DomainError("projection_conflict", "Existing .mcp.json must be a JSON object")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise DomainError("projection_conflict", "Existing .mcp.json has an unusable mcpServers value")
    existing = servers.get(SERVER)
    if existing is not None:
        # JSON has no comment/marker channel.  The helper path is the local
        # ownership receipt for this one server entry; a different entry must
        # remain untouched rather than being silently replaced.
        if (not isinstance(existing, dict) or existing.get("type") != "http"
                or existing.get("headersHelper") != helper):
            raise DomainError("projection_conflict",
                              f"Existing .mcp.json already owns the {SERVER} server name")
    return _json_bytes({**document, "mcpServers": {**servers, SERVER: entry}})


def _gitignore_file(path: Path) -> bytes | None:
    text = _text(path)
    if text is not None and GITIGNORE_ENTRY in text.splitlines():
        return None
    previous = (text or "").rstrip("\n")
    return ((previous + "\n" if previous.strip() else "") + GITIGNORE_ENTRY + "\n").encode()


def _managed_directory_conflict(root: Path, business: dict) -> None:
    directory = root / DIRECTORY
    if not directory.exists():
        return
    if path_has_symlink(directory) or not directory.is_dir():
        raise DomainError("projection_conflict", "The owner-session directory is not a managed directory")
    registration = directory / REGISTRATION.rsplit("/", 1)[-1]
    managed_files = [directory / name.rsplit("/", 1)[-1] for name in (CREDENTIAL, HEADERS, REGISTRATION)]
    if not any(path.exists() for path in managed_files):
        return
    text = _text(registration)
    try:
        value = json.loads(text) if text is not None else None
    except ValueError as exc:
        raise DomainError("projection_conflict",
                          "Existing .bazarlyk-owner registration is not valid JSON") from exc
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or value.get("business_id") != business["id"]):
        raise DomainError("projection_conflict",
                          "Existing .bazarlyk-owner files belong to another preparation")


def _codex_block(endpoint: str, helper: str, *, original_final_newline: bool | None = None) -> str:
    # No hooks, permissions or filesystem policy: owner sessions keep the
    # owner's ordinary client behavior.
    config = {"mcp_servers": {SERVER: {"url": endpoint + ROUTE,
                                        "http_headers_helper": helper, "required": True}}}
    newline = "unknown" if original_final_newline is None else ("yes" if original_final_newline else "no")
    return (CODEX_MARKER + "\n" + CODEX_NEWLINE + " " + newline + "\n"
            + tomli_w.dumps(config).rstrip("\n") + "\n" + CODEX_END + "\n")


def _codex_file(path: Path, endpoint: str, helper: str) -> bytes:
    text = _text(path)
    block = _codex_block(endpoint, helper)
    if text is None or not text.strip():
        return block.encode()

    # Parse before planning any replacement.  This catches malformed TOML
    # while preserving the original bytes on the conflict path.
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DomainError("projection_conflict",
                          "Existing .codex/config.toml is not valid TOML and needs reconciliation") from exc

    start, end = text.find(CODEX_MARKER), text.find(CODEX_END)
    if (text.count(CODEX_MARKER) > 1 or text.count(CODEX_END) > 1
            or (start < 0 and end >= 0) or (start >= 0 and end >= 0 and end < start)):
        raise DomainError("projection_conflict",
                          "Existing .codex/config.toml has an unbalanced owner-session block and needs reconciliation")
    if start >= 0:
        # Older preparation versions had only a start marker.  They generated
        # a file containing just this server, so recognize that shape for a
        # safe reprepare and make future edits marker-scoped.
        if end < 0:
            if set(parsed) != {"mcp_servers"} or set(parsed.get("mcp_servers", {})) != {SERVER}:
                raise DomainError("projection_conflict",
                                  "Existing owner-session Codex configuration needs reconciliation")
            return block.encode()
        existing_block = text[start:end + len(CODEX_END)]
        match = re.search(r"^" + re.escape(CODEX_NEWLINE) + r" (yes|no)$", existing_block, re.MULTILINE)
        preserved_newline = match and match.group(1) == "yes"
        block = _codex_block(endpoint, helper, original_final_newline=preserved_newline)
        candidate = text[:start] + block + text[end + len(CODEX_END) + 1:]
        try:
            tomllib.loads(candidate)
        except tomllib.TOMLDecodeError as exc:
            raise DomainError("projection_conflict",
                              "Existing .codex/config.toml cannot accept the owner-session block") from exc
        return candidate.encode()

    servers = parsed.get("mcp_servers", {})
    if isinstance(servers, dict) and SERVER in servers:
        raise DomainError("projection_conflict",
                          f"Existing .codex/config.toml already owns the {SERVER} server name")
    candidate = (text + ("\n" if text.endswith("\n") else "\n\n")
                 + _codex_block(endpoint, helper, original_final_newline=text.endswith("\n")))
    try:
        tomllib.loads(candidate)
    except tomllib.TOMLDecodeError as exc:
        raise DomainError("projection_conflict",
                          "Existing .codex/config.toml cannot accept the owner-session block") from exc
    return candidate.encode()


def build_projection(*, business, endpoint, roots, label, prepared_at=None, owner_name=None) -> Projection:
    """Plan every root's managed bytes, raising before any file is written.

    The credential itself is not part of the projection: it is issued once and
    written beside the other files by :func:`prepare`.
    """
    endpoint = service_url(endpoint)
    roots = [Path(root) for root in roots]
    prepared_at = prepared_at or utc_now()
    block = instruction_block(business, endpoint, owner_name)
    files = {}
    for index, root in enumerate(roots):
        helper = shlex.quote(str(root / HEADERS))
        _managed_directory_conflict(root, business)
        for name in (CREDENTIAL, HEADERS, REGISTRATION):
            if path_has_symlink(root / name):
                raise DomainError("projection_conflict", "Owner-session files cannot follow a symbolic link")
        ignore = _gitignore_file(root / ".gitignore")
        previous_receipt = _text(root / REGISTRATION, strict=False)
        try:
            previous_registration = json.loads(previous_receipt) if previous_receipt is not None else None
        except ValueError:
            previous_registration = None
        gitignore_added = (ignore is not None
                           or (isinstance(previous_registration, dict)
                               and previous_registration.get("gitignore_added") is True))
        planned = {
            HEADERS: HEADERS_SCRIPT.encode(),
            REGISTRATION: _json_bytes({"version": VERSION, "endpoint": endpoint, "business_id": business["id"],
                                       "business_slug": business["slug"], "business_name": business["name"],
                                       "label": label, "root": str(root), "prepared_at": prepared_at,
                                       "gitignore_added": gitignore_added}),
            ".mcp.json": _mcp_file(root / ".mcp.json", endpoint, helper),
        }
        for name in INSTRUCTION_FILES:
            planned[name] = _instruction_file(root / name, block)
        if ignore is not None:
            planned[".gitignore"] = ignore
        # Codex discovers project-local config from each explicit launch root;
        # keeping one per root avoids an isolated CODEX_HOME and gives nested
        # checkouts their own local header helper.
        planned[CODEX_CONFIG] = _codex_file(root / CODEX_CONFIG, endpoint, helper)
        files[root] = planned
    metadata = {"version": VERSION, "endpoint": endpoint, "label": label, "prepared_at": prepared_at,
                "business_id": business["id"], "business_slug": business["slug"],
                "top_root": str(roots[0]), "roots": [str(root) for root in roots]}
    return Projection(files, metadata)


def _write(path: Path, data: bytes, mode: int) -> None:
    if path_has_symlink(path):
        raise DomainError("projection_conflict", "Owner-session preparation cannot follow a symbolic link")
    safe_io.atomic_write(path, data, mode=mode, enforce_mode=mode != 0o644)


def _validate_token(token) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise DomainError("invalid_bundle", "Owner-session bundle has an invalid credential token")
    return token


def _bundle_business(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"id", "slug", "name"}:
        raise DomainError("invalid_bundle", "Owner-session bundle has invalid project identity")
    if any(not isinstance(value[key], str) or not value[key].strip() for key in value):
        raise DomainError("invalid_bundle", "Owner-session bundle has invalid project identity")
    return value


def _load_bundle(path) -> dict:
    path = Path(path)
    if path_has_symlink(path) or not path.is_file():
        raise DomainError("invalid_bundle", "Owner-session bundle is missing")
    try:
        raw = safe_io.read_bytes(path, limit=64 * 1024, private=True)
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise DomainError("invalid_bundle", "Owner-session bundle is not valid JSON") from exc
    required = {"version", "endpoint", "business", "label", "token"}
    if (not isinstance(value, dict) or value.get("version") != VERSION
            or not required <= set(value) or set(value) - required - {"owner_name"}):
        raise DomainError("invalid_bundle", "Unsupported owner-session bundle")
    owner_name = value.get("owner_name")
    if owner_name is not None and owner_label(owner_name) is None:
        raise DomainError("invalid_bundle", "Owner-session bundle has an invalid owner name")
    try:
        endpoint = service_url(value["endpoint"])
    except (DomainError, TypeError) as exc:
        raise DomainError("invalid_bundle", "Owner-session bundle has an invalid endpoint") from exc
    business = _bundle_business(value["business"])
    label = value["label"]
    if not isinstance(label, str) or not label.strip() or len(label) > 200 or "\n" in label:
        raise DomainError("invalid_bundle", "Owner-session bundle has an invalid machine label")
    return {"version": VERSION, "endpoint": endpoint, "business": business, "label": label.strip(),
            "token": _validate_token(value["token"]), "owner_name": owner_label(owner_name)}


def _bundle_path(path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise DomainError("validation_error", "The bundle path must be an absolute file path without parent components")
    if path_has_symlink(path):
        raise DomainError("projection_conflict", "The bundle path must not follow a symbolic link")
    _require_directory(path.parent, "bundle parent")
    current = path.parent
    while True:
        marker = current / ".git"
        if marker.is_file() or marker.is_symlink() or (marker.is_dir() and any(marker.iterdir())):
            raise DomainError("validation_error", "The private bundle must be written outside a Git worktree")
        if current.parent == current:
            break
        current = current.parent
    if path.exists():
        raise DomainError("projection_conflict", "The bundle output already exists; choose a new private path")
    return path


def issue_bundle(database, *, business, endpoint, label=None, output) -> dict:
    """Issue a credential on the Qorqut host and save a portable private bundle."""
    endpoint = service_url(endpoint)
    label = label or platform.node()
    output = _bundle_path(output)
    record = resolve_business(database, business)
    token = owner_sessions.issue_credential(database, business_id=record["id"], label=label,
                                            endpoint=endpoint)
    payload = {"version": VERSION, "endpoint": endpoint,
               "business": {key: record[key] for key in ("id", "slug", "name")},
               "label": label, "token": token}
    owner_name = resolve_owner_name(database)
    if owner_name:
        payload["owner_name"] = owner_name
    _write(output, (_json_bytes(payload)), 0o600)
    return {"version": VERSION, "endpoint": endpoint, "business_id": record["id"],
            "business_slug": record["slug"], "business_name": record["name"],
            "label": label, "bundle": str(output)}


def _install_projection(projection: Projection, token: str) -> None:
    for root, planned in projection.files.items():
        safe_io.mkdir(root / DIRECTORY)
        for name, body in sorted(planned.items()):
            _write(root / name, body, MODES.get(name, 0o644))
        _write(root / CREDENTIAL, (token + "\n").encode(), MODES[CREDENTIAL])


def install_bundle(bundle, *, code_root, nested=(), outside_roots=()) -> dict:
    """Install a DB-free issuance bundle into explicitly selected local roots."""
    bundle = _load_bundle(bundle)
    roots = resolve_roots(code_root, nested, outside_roots)
    projection = build_projection(business=bundle["business"], endpoint=bundle["endpoint"],
                                  roots=roots, label=bundle["label"], owner_name=bundle["owner_name"])
    _install_projection(projection, bundle["token"])
    return {**projection.metadata, "business_name": bundle["business"]["name"],
            "launch": launch_instructions(roots)}


def launch_instructions(roots) -> dict:
    return {"claude": [f"cd {shlex.quote(str(root))} && claude" for root in roots],
            "codex": [f"cd {shlex.quote(str(root))} && codex" for root in roots]}


def prepare(database, *, business, code_root, nested=(), outside_roots=(), endpoint, label=None) -> dict:
    """Prepare every given root for owner-directed sessions of one business."""
    endpoint = service_url(endpoint)
    label = label or platform.node()
    roots = resolve_roots(code_root, nested, outside_roots)
    record = resolve_business(database, business)
    projection = build_projection(business=record, endpoint=endpoint, roots=roots, label=label,
                                  owner_name=resolve_owner_name(database))
    # Issuance enforces the enabled business and owner membership, and revokes
    # this label's previous credential; conflicts are already ruled out above.
    token = owner_sessions.issue_credential(database, business_id=record["id"], label=label,
                                            endpoint=endpoint, roots=[str(root) for root in roots])
    _install_projection(projection, token)
    return {**projection.metadata, "business_name": record["name"], "launch": launch_instructions(roots)}


def _remove_empty(path: Path) -> None:
    try:
        safe_io.remove_empty_tree(path)
    except (OSError, DomainError):
        pass


def _remove_mcp_entry(root: Path) -> None:
    path = root / ".mcp.json"
    text = _text(path, strict=False)
    if text is None:
        return
    try:
        document = json.loads(text)
    except ValueError:
        return  # Foreign or damaged content is never rewritten during removal.
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    if not isinstance(servers, dict) or SERVER not in servers:
        return
    entry = servers[SERVER]
    if (not isinstance(entry, dict) or entry.get("type") != "http"
            or entry.get("headersHelper") != shlex.quote(str(root / HEADERS))):
        return
    servers = {name: value for name, value in servers.items() if name != SERVER}
    if not servers and set(document) == {"mcpServers"}:
        safe_io.unlink(path, missing_ok=True)
        return
    _write(path, _json_bytes({**document, "mcpServers": servers}), 0o644)


def _remove_block(path: Path) -> None:
    text = _text(path, strict=False)
    if text is None:
        return
    start, end = text.find(BLOCK_START), text.find(BLOCK_END)
    if start < 0 or end < start:
        return
    head, tail = text[:start], text[end + len(BLOCK_END):]
    if not (head + tail).strip():
        safe_io.unlink(path, missing_ok=True)
        return
    # The block was appended at the end: drop the blank-line separator we added.
    rest = head.rstrip("\n") + "\n" if not tail.strip() else head + tail
    _write(path, rest.encode(), 0o644)


def _remove_gitignore_entry(root: Path) -> None:
    path = root / ".gitignore"
    text = _text(path, strict=False)
    if text is None or GITIGNORE_ENTRY not in text.splitlines():
        return
    receipt = _text(root / REGISTRATION, strict=False)
    try:
        registration = json.loads(receipt) if receipt is not None else None
    except ValueError:
        return
    if not isinstance(registration, dict) or registration.get("gitignore_added") is not True:
        return
    lines = [line for line in text.splitlines() if line != GITIGNORE_ENTRY]
    if not any(line.strip() for line in lines):
        safe_io.unlink(path, missing_ok=True)
        return
    _write(path, ("\n".join(lines) + "\n").encode(), 0o644)


def _remove_codex(root: Path) -> bool:
    path = root / CODEX_CONFIG
    text = _text(path, strict=False)
    if text is None:
        return False
    start, end = text.find(CODEX_MARKER), text.find(CODEX_END)
    if start >= 0 and end < 0:
        # Remove the legacy preparation file only when its parsed shape proves
        # it was generated by this module.
        if start != 0:
            return False
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return False
        if set(parsed) != {"mcp_servers"} or set(parsed.get("mcp_servers", {})) != {SERVER}:
            return False
        safe_io.unlink(path, missing_ok=True)
        _remove_empty(root / ".codex")
        return True
    if start < 0:
        return False
    if text.count(CODEX_MARKER) > 1 or text.count(CODEX_END) > 1 or end < start:
        return False
    head = text[:start]
    tail = text[end + len(CODEX_END):]
    block = text[start:end + len(CODEX_END)]
    match = re.search(r"^" + re.escape(CODEX_NEWLINE) + r" (yes|no)$", block, re.MULTILINE)
    if head.endswith("\n\n"):
        # One newline was added when the original config already ended in a
        # newline; two were added when it did not.  The marker records which
        # case applies so removal restores bytes exactly.
        head = head[:-(1 if match and match.group(1) == "yes" else 2)]
    if tail.startswith("\n"):
        tail = tail[1:]
    remainder = head + tail
    if not remainder.strip():
        safe_io.unlink(path, missing_ok=True)
        _remove_empty(root / ".codex")
    else:
        _write(path, remainder.encode(), 0o644)
    return True


def remove(*, code_root, nested=(), outside_roots=()) -> dict:
    """Delete only owned files and entries; foreign content is always kept."""
    roots = resolve_roots(code_root, nested, outside_roots)
    removed = []
    for index, root in enumerate(roots):
        _remove_mcp_entry(root)
        for name in INSTRUCTION_FILES:
            _remove_block(root / name)
        _remove_gitignore_entry(root)
        for name in (CREDENTIAL, HEADERS, REGISTRATION):
            path = root / name
            if not path_has_symlink(path) and path.is_file():
                safe_io.unlink(path, missing_ok=True)
                removed.append(str(path))
        _remove_empty(root / DIRECTORY)
        if _remove_codex(root):
            removed.append(str(root / CODEX_CONFIG))
    return {"roots": [str(root) for root in roots], "removed": removed}


def revoke(database, *, business, label) -> dict:
    """Revoke this label's owner-session credentials for one business."""
    from qorqut_operations.application.engine import Application

    record = resolve_business(database, business)
    revoked = 0
    with database.transaction() as uow:
        Application.ensure_writes_open(uow)
        installation = uow.repository("installation").get("service")
        owner_id = installation["owner_principal_id"] if installation else None
        if not owner_id:
            raise DomainError("unauthenticated", "An active installation owner must be registered")
        for row in uow.connection.execute(
                "SELECT id, metadata_json, revision FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
                (owner_id,)).fetchall():
            binding = owner_sessions.credential_binding(row["metadata_json"])
            if binding and binding["business_id"] == record["id"] and (label is None or binding.get("label") == label):
                uow.repository("credentials").update(row["id"], {"revoked_at": utc_now()},
                                                     expected_revision=row["revision"])
                revoked += 1
    return {"business_id": record["id"], "business_slug": record["slug"], "label": label, "revoked": revoked}


def _common(parser, *names) -> None:
    if "businesses-root" in names:
        from bazarlyk.service.instance_arguments import add_instance_arguments
        add_instance_arguments(parser)
    if "business" in names:
        parser.add_argument("--business", required=True, help="Project UUID or slug")
    if "root" in names:
        parser.add_argument("--root", required=True, help="Absolute top code root to prepare")
        parser.add_argument("--nested", action="append", default=[],
                            help="Relative nested repository root inside the code root (repeatable)")
        parser.add_argument("--outside-root", action="append", default=[],
                            help="Absolute checkout outside the code tree (repeatable, explicit)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bazarlyk.bootstrap.owner_prepare",
                                     description="Owner-invoked preparation for owner-directed code sessions")
    commands = parser.add_subparsers(dest="command", required=True)
    prepared = commands.add_parser("prepare", help="Prepare Claude and Codex integration for the given roots")
    _common(prepared, "businesses-root", "business", "root")
    prepared.add_argument("--endpoint", required=True, help="Qorqut service URL, for example http://host:1049")
    prepared.add_argument("--label", default=platform.node(), help="Machine label for this credential")
    issued = commands.add_parser("issue", aliases=["issue-bundle"],
                                 help="Issue a portable private bundle on the Qorqut host")
    _common(issued, "businesses-root", "business")
    issued.add_argument("--endpoint", required=True, help="Qorqut service URL, for example http://host:1049")
    issued.add_argument("--label", default=platform.node(), help="Machine label for this credential")
    issued.add_argument("--output", required=True, help="Absolute private bundle path outside the code tree")
    installed = commands.add_parser("install", aliases=["install-bundle"],
                                    help="Install a previously issued bundle without a database")
    _common(installed, "root")
    installed.add_argument("--bundle", required=True, help="Absolute path to the private issuance bundle")
    removal = commands.add_parser("remove", help="Remove only the owned files and entries")
    _common(removal, "root")
    revocation = commands.add_parser("revoke", help="Revoke this label's owner-session credentials")
    _common(revocation, "businesses-root", "business")
    revocation.add_argument("--label", default=platform.node(), help="Machine label to revoke")
    args = parser.parse_args(argv)
    from bazarlyk.composition.assembly import host_assembly, open_database
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.instance_arguments import instance_paths_from_arguments

    # Preparation validates the existing instance database; it never
    # initializes or migrates storage and never starts the service.
    def database():
        return open_database(instance_paths_from_arguments(args, required=()), host_assembly(ChangeFeed()))

    try:
        if args.command == "prepare":
            result = prepare(database(), business=args.business, code_root=args.root,
                             nested=args.nested, outside_roots=args.outside_root,
                             endpoint=args.endpoint, label=args.label)
            print(f"Prepared {result['business_slug']} ({result['business_id']}) as {result['label']}.")
            for root in result["roots"]:
                print("  root: " + root)
            for provider in ("claude", "codex"):
                print(f"Launch ({provider}):")
                for line in result["launch"][provider]:
                    print("  " + line)
        elif args.command in {"issue", "issue-bundle"}:
            result = issue_bundle(database(), business=args.business, endpoint=args.endpoint,
                                  label=args.label, output=args.output)
            print(f"Issued {result['business_slug']} ({result['business_id']}) as {result['label']}.")
            print("  private bundle: " + result["bundle"])
        elif args.command in {"install", "install-bundle"}:
            result = install_bundle(args.bundle, code_root=args.root,
                                    nested=args.nested, outside_roots=args.outside_root)
            print(f"Installed {result['business_slug']} ({result['business_id']}) as {result['label']}.")
            for root in result["roots"]:
                print("  root: " + root)
        elif args.command == "remove":
            result = remove(code_root=args.root, nested=args.nested, outside_roots=args.outside_root)
            print(f"Removed owner-session files from {len(result['roots'])} root(s).")
        else:
            result = revoke(database(), business=args.business, label=args.label)
            print(f"Revoked {result['revoked']} owner-session credential(s) for {result['business_slug']}.")
    except DomainError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
