"""Admission, inspection and control of the one durable Context input queue.

GENERAL submissions and accepted Action revisions share one sequence. Admission never waits for Context or a model;
a separate backend driver advances inputs through ``transitions`` and records each outcome. This module holds the
validation and storage rules; the registered operations in ``operations`` carry them through the v1 prepare/apply
authority, and ``admit_action_revision`` is the ``on_revision`` port the composition hands the Action writer: it
queues the writer's exact stored revision before the writer's own transaction commits.

Every input has one owning namespace (``business_id``, NULL for Global) and an attributed submitter. Reads and
controls are bound to one namespace: nothing here lists across businesses.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import PurePosixPath
from typing import Any

from qorqut_operations.application.plans import page, page_position
from qorqut_operations.contracts import DomainError, canonical_bytes, new_id, utc_now
from qorqut_operations.tags.catalog import snapshot_order

STATES = ("queued", "processing", "blocked", "published", "discard_requested", "discarded")
TERMINAL_STATES = ("published", "discarded")
OPEN_HEAD_STATES = ("processing", "blocked", "discard_requested")
OPERATIONS = ("add", "edit", "remove")
# The official Action evidence namespace and its source metadata belong to the Action mode alone; a GENERAL claim
# cannot select or imitate it.
RESERVED_PREFIX = "foundation/actions/"
# The document and instruction adapters own these paths, and the controlled graph-edit admission owns ``EDIT_PREFIX``. A
# public GENERAL submission cannot name them, so an unrelated input can never seize a document's or an instruction's
# source or claim a deterministic edit; only the adapter's own admission (a filing with its real prepared job, or a graph
# edit with its own companion binding) creates an input here. The prefixes are not a directory inference: scope is the
# stored binding.
EDIT_PREFIX = "foundation/edits/"
CONTROLLED_PREFIXES = ("foundation/documents/", "foundation/instructions/", EDIT_PREFIX)
_METADATA_REQUIRED = frozenset({"kind", "title", "origin"})
_METADATA_FIELDS = _METADATA_REQUIRED | {"origin_ref", "created_at"}
OMITTED = object()
INPUT_COLUMNS = ("id", "sequence", "input_kind", "operation", "item_id", "revision", "submitted_by", "created_at",
                 "state", "context_event_id", "attempt")


def invalid(message: str):
    raise DomainError("validation_error", message)


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        invalid(f"{field} must be a nonempty string")
    return value


def encode(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def scope_of(business_id) -> dict:
    return {"kind": "global"} if business_id is None else {"kind": "business", "business_id": business_id}


# -- GENERAL source admission -------------------------------------------------------------------------------------------

def source_path(path: Any, *, controlled: bool = False) -> str:
    """Relative Context source path, mirroring the engine's basic path rules.

    ``controlled`` is true only for a path read back from a stored input or built by a filing adapter's own admission;
    a public submission never passes it, and the controlled adapter prefixes are refused to it."""
    text(path, "path")
    parts = path.split("/")
    if (len(parts) < 2 or parts[0] != "foundation" or any(part in ("", ".", "..", ".git") for part in parts)
            or "\\" in path or "\x00" in path or PurePosixPath(path).suffix.lower() not in (".md", ".txt")):
        invalid("Source paths must stay under foundation/ and end in .md or .txt")
    if path.startswith(RESERVED_PREFIX):
        invalid("foundation/actions/ is reserved for accepted Action evidence")
    if path.startswith(CONTROLLED_PREFIXES) and not controlled:
        invalid("foundation/documents/, foundation/instructions/ and foundation/edits/ belong to their controlled adapters")
    return path


def _metadata(value: Any, operation: str):
    if value is None:
        if operation == "add":
            invalid("A source addition requires kind, title and origin metadata")
        return None
    if not isinstance(value, dict) or set(value) - _METADATA_FIELDS:
        invalid("Metadata allows only kind, title, origin, origin_ref and created_at")
    if operation == "add" and not _METADATA_REQUIRED <= set(value):
        invalid("A source addition requires kind, title and origin metadata")
    for field, member in value.items():
        text(member, f"metadata {field}")
    if value.get("kind") == "accepted_action" or value.get("origin") == "controlled_caller":
        invalid("Metadata cannot claim the official Action input mode")
    return dict(value)


def general_arguments(operation: Any, path: Any, content: Any, metadata: Any, max_content_bytes: int | None, *,
                      controlled: bool = False) -> dict:
    """The validated, canonical source change of a GENERAL submission.

    ``controlled`` is the filing adapters' own admission only (the public ``context_queue_submit`` never sets it). The
    graph-edit admission does not use this function: its fixed controlled metadata is exactly what ``_metadata`` refuses."""
    if not isinstance(operation, str) or operation not in OPERATIONS:
        invalid("operation must be add, edit or remove")
    source_path(path, controlled=controlled)
    if operation == "remove":
        if content is not None:
            invalid("Removal must not supply content")
    else:
        text(content, "content")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError:
            invalid("content must be valid UTF-8 text")
        if max_content_bytes is not None and size > max_content_bytes:
            invalid(f"content exceeds the configured limit of {max_content_bytes} bytes")
    return {"operation": operation, "path": path, "content": content, "metadata": _metadata(metadata, operation)}


def general_values(input_id: str, arguments: dict, submitted_by: str, at: str) -> dict:
    """The stored row of a GENERAL input. The payload is exactly the donor's accepted source envelope."""
    return {"id": input_id, "input_kind": "general", "operation": arguments["operation"],
            "payload_json": {**arguments, "submitted_by": submitted_by}, "submitted_by": submitted_by, "created_at": at}


# -- accepted Action revisions ------------------------------------------------------------------------------------------

def tag_envelope(snapshots) -> list[dict]:
    """The revision's frozen tags exactly as the Context Action mode takes them: ``{id, name, business_id}`` each, in
    one deterministic order. Ordinary tags are shared labels, so ``business_id`` is None; it is carried, never inferred."""
    return snapshot_order({"id": tag["id"], "name": tag["name"], "business_id": tag["business_id"]} for tag in snapshots)


def action_payload(revision: dict) -> dict:
    """The exact envelope of one stored Action revision (``revision`` is its decoded ``action_revisions`` row).

    It is derived only from what the revision stored: nothing is read from the live Action afterwards, so a later
    correction cannot change queued evidence. A withdrawal carries no text and no related objects."""
    snapshot, operation, key = revision["snapshot_json"], revision["operation"], revision["action_key"]
    payload = {"action_ref": key, "revision_ref": f"{key}:{revision['revision']}", "operation": operation,
               "submitted_by": revision["recorded_by"], "tags": tag_envelope(snapshot["tag_snapshots"]),
               "related_objects": [] if operation == "remove" else snapshot["related_objects"],
               "scope": scope_of(revision["business_id"]),
               "provenance": {"performed_by": revision["performed_by"], "recorded_by": revision["recorded_by"],
                              "origin": snapshot.get("origin"), "category": snapshot.get("category"),
                              **(revision["provenance_json"] or {}),
                              "action_metadata": deepcopy({
                                  "version": 1, "occurred": snapshot.get("occurred"),
                                  "refs": snapshot.get("refs", {}),
                                  "business_ids": snapshot.get("business_ids", []),
                                  "recorded_at": revision["created_at"],
                                  "withdrawn": operation == "remove"})}}
    if operation != "remove":
        payload["text"] = snapshot["text"]
    return payload


def admit_action_revision(ctx, revision: dict, uow, *, evidence=None) -> str:
    """The Action writer's ``on_revision`` port: queue one stored Action revision and return the exact input ID.

    ``revision`` is the decoded ``action_revisions`` row ``settle`` just read. It is admitted in the caller's open
    transaction (no transaction, hook or retry of its own), so a failure here rolls back the Action, its revision, its
    input and its events together, and a retried operation finds no earlier input to duplicate."""
    return enqueue_action(uow, action_id=revision["action_key"], revision=revision["revision"],
                          operation=revision["operation"], payload=action_payload(revision),
                          capture_evidence=None if evidence is None else lambda stored: evidence(ctx, stored, uow))


def enqueue_action(uow, *, action_id: str, revision: int, operation: str, payload: dict, capture_evidence=None) -> str:
    """Queue one accepted Action revision and return the input ID (reached through ``admit_action_revision``).

    It runs after the immutable revision exists and before the same transaction commits. The stored revision is
    authoritative: the input's owning scope and submitter come from it, and ``payload`` must be exactly the envelope that
    revision implies. Only the composed, authorized evidence reader can supplement that envelope, after validation.
    Its versioned excerpts are frozen here, never reread by the driver or a retry."""
    uow.require_write()
    if operation not in OPERATIONS or not isinstance(payload, dict):
        invalid("An Action Context input requires its exact accepted revision payload")
    row = uow.connection.execute("SELECT * FROM action_revisions WHERE action_key=? AND revision=?",
                                 (action_id, revision)).fetchone()
    if row is None:
        raise DomainError("not_found", "The accepted Action revision does not exist")
    stored = {key: json.loads(value) if key.endswith("_json") and value is not None else value for key, value in dict(row).items()}
    if stored["operation"] != operation or payload != action_payload(stored):
        invalid("An Action Context input requires its exact accepted revision payload")
    payload = deepcopy(payload)
    if capture_evidence is not None and operation != "remove":
        payload["provenance"]["action_metadata"]["evidence"] = capture_evidence(stored)
    input_id = new_id()
    uow.connection.execute(
        "INSERT INTO context_inputs(id,business_id,input_kind,item_id,revision,operation,payload_json,submitted_by,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (input_id, stored["business_id"], "action", action_id, revision, operation, encode(payload),
         stored["recorded_by"], utc_now()))
    return input_id


# -- reads --------------------------------------------------------------------------------------------------------------

def input_view(row, *, payload: bool = False, execution: Any = OMITTED) -> dict:
    """The public view of a stored input. ``execution`` is the controlled-execution facts of the input (or None for an
    ordinary one) as the composed ``executions`` contribution reads them; an internal caller omits it and gets no key."""
    result = {key: row[key] for key in INPUT_COLUMNS}
    result["scope"] = scope_of(row["business_id"])
    result["result"] = decode(row["result_json"])
    result["error"] = decode(row["error_json"])
    content = json.loads(row["payload_json"])
    result["summary"] = ({"path": content["path"]} if row["input_kind"] == "general"
                         else {"action_ref": content["action_ref"], "revision_ref": content["revision_ref"]})
    if execution is not OMITTED:
        result["execution"] = execution
    if payload:
        result["payload"] = content
    return result


def no_executions(uow, row):
    """The default ``executions`` contribution: no controlled executions are composed, so an ordinary input has none and a
    row that claims the reserved graph-edit path (which only a composed graph-edit binding may create) is refused loudly."""
    if row["input_kind"] == "general" and json.loads(row["payload_json"])["path"].startswith(EDIT_PREFIX):
        raise DomainError("composition_error", "A graph-edit input exists but graph edits are not composed into the queue reads")
    return None


def detail(uow, row, executions=None) -> dict:
    result = input_view(row, payload=True, **({} if executions is None else {"execution": executions(uow, row)}))
    result["attempts"] = [
        {**{key: attempt[key] for key in ("attempt", "context_event_id", "cause", "control_id", "started_at", "outcome",
                                          "finished_at")},
         "result": decode(attempt["result_json"]), "error": decode(attempt["error_json"])}
        for attempt in uow.connection.execute(
            "SELECT * FROM context_input_attempts WHERE input_id=? ORDER BY attempt", (row["id"],))]
    result["controls"] = [
        {**{key: control[key] for key in ("id", "intent", "expected_state", "requested_by", "requested_at", "status",
                                          "resolved_at")}, "result": decode(control["result_json"])}
        for control in uow.connection.execute(
            "SELECT * FROM context_input_controls WHERE input_id=? ORDER BY sequence", (row["id"],))]
    return result


def list_inputs(uow, business_id, arguments: dict, operation: str, principal_id: str, executions=None) -> dict:
    """One page of the namespace's inputs in queue order. ``queue_blocked`` is the single shared fact that something
    ahead of every later input is blocked; the blocked input itself is named only to a reader of its namespace."""
    state = arguments.get("state")
    if state is not None and state not in STATES:
        invalid("state must be a known queue state")
    limit = arguments.get("limit", 50)
    normalized = {"principal_id": principal_id, "limit": limit, **({"state": state} if state else {}),
                  **({"cursor": arguments["cursor"]} if arguments.get("cursor") else {})}
    _, after = page_position(normalized, business_id, operation)
    clauses, parameters = ["business_id IS ?"], [business_id]
    if state:
        clauses.append("state=?")
        parameters.append(state)
    if after:
        clauses.append("sequence>?")
        parameters.append(after[0])
    rows = uow.connection.execute(
        "SELECT * FROM context_inputs WHERE " + " AND ".join(clauses) + " ORDER BY sequence LIMIT ?",
        [*parameters, limit + 1]).fetchall()
    selected, cursor = page([dict(row) for row in rows], normalized, business_id, operation,
                            key=lambda row: (row["sequence"],), reverse=False)
    blocked = uow.connection.execute("SELECT id, business_id FROM context_inputs WHERE state='blocked'").fetchone()
    return {"items": [input_view(row, **({} if executions is None else {"execution": executions(uow, row)})) for row in selected],
            "next_cursor": cursor, "queue_blocked": blocked is not None,
            "blocked_input_id": blocked["id"] if blocked and blocked["business_id"] == business_id else None}


def lookup(uow, business_id, input_id: str):
    text(input_id, "input_id")
    row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=? AND business_id IS ?", (input_id, business_id)).fetchone()
    if row is None:
        raise DomainError("not_found", "Context input does not exist in this namespace")
    return row
