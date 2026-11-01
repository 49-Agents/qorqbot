"""Explicit graph-edit plans: fixed typed operations executed as a controlled GENERAL event, with no model."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from . import input_modes as modes
from .schema import ContextError, change_details, digest, encode, identifier, object_record, text

FORMAT = "qorqut-graph-edit-v1"
PREFIX = "foundation/edits/"
EXECUTION = "deterministic_graph_edit"
METADATA = {"kind": "graph_edit", "title": "Explicit graph edit", "origin": "controlled_caller"}
HANDOFF = {"disposition": "empty", "text": ""}
PUTS = {"put_node": "node", "put_edge": "edge"}
REMOVES = {"remove_node": "node", "remove_edge": "edge"}
_PLAN = {"format", "expected_revision", "rationale", "operations"}
_PUT = {"op", "record", "explanation", "citations", "support_after"}
_REMOVE = {"op", "id", "explanation", "citations"}


def source_path(event_id):
    return PREFIX + identifier(event_id) + ".txt"


def kind(verb):
    return {**PUTS, **REMOVES}[verb]


def object_id(operation):
    return operation["record"]["id"] if operation["op"] in PUTS else operation["id"]


def object_ids(plan):
    return {object_id(operation) for operation in plan["operations"]}


def normalize(plan):
    """The canonical plan, rejecting unknown fields, unsupported verbs and invalid records before any staging.

    Existing record, explanation, citation and support validators are reused; nothing is allocated or merged.
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN:
        raise ContextError(f"A graph edit requires exactly {sorted(_PLAN)}")
    if plan["format"] != FORMAT:
        raise ContextError(f"Unsupported graph edit format; expected {FORMAT}")
    expected_revision = modes.full_commit(plan["expected_revision"], "expected_revision")
    rationale = text(plan["rationale"], "rationale")
    operations = plan["operations"]
    if not isinstance(operations, list) or not operations:
        raise ContextError("A graph edit requires a nonempty operations list")
    seen, result = set(), []
    for item in operations:
        if not isinstance(item, dict):
            raise ContextError("Every graph edit operation must be an object")
        verb = item.get("op")
        if not isinstance(verb, str) or verb not in PUTS and verb not in REMOVES:
            raise ContextError("Unsupported graph edit verb; use put_node, put_edge, remove_node or remove_edge")
        if verb in PUTS:
            if set(item) != _PUT:
                raise ContextError(f"{verb} requires exactly {sorted(_PUT)}")
            record = object_record(PUTS[verb], item["record"])
            details = change_details(item["explanation"], item["citations"], item["support_after"], [])
            entry = {"op": verb, "record": record, "explanation": details["explanation"],
                     "citations": details["citations"], "support_after": details["support_after"]}
            target = record["id"]
        else:
            if set(item) != _REMOVE:
                raise ContextError(f"{verb} requires exactly {sorted(_REMOVE)}")
            target = identifier(item["id"])
            details = change_details(item["explanation"], item["citations"], [], [])
            entry = {"op": verb, "id": target, "explanation": details["explanation"],
                     "citations": details["citations"]}
        if target in seen:
            raise ContextError("An object ID may appear once in a graph edit")
        seen.add(target)
        result.append(entry)
    return {"format": FORMAT, "expected_revision": expected_revision, "rationale": rationale,
            "operations": result}


def canonical(plan):
    try:
        return encode(plan)
    except UnicodeEncodeError as error:
        raise ContextError("A graph edit must be valid UTF-8") from error


def load(content):
    """The plan whose canonical encoding is exactly ``content``."""
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, ValueError) as error:
        raise ContextError("The graph-edit source is not a valid plan") from error
    plan = normalize(value)
    if canonical(plan) != content:
        raise ContextError("The graph-edit source is not the canonical plan encoding")
    return plan


def marker(plan):
    return {"kind": EXECUTION, "plan_digest": digest(plan)}


def draft(plan):
    """The exact draft a plan stages: each operation's record or removal with its attribution and no developments."""
    result = {}
    for operation in plan["operations"]:
        put = operation["op"] in PUTS
        record = deepcopy(operation["record"]) if put else None
        result[f"{kind(operation['op'])}:{object_id(operation)}"] = {
            "kind": kind(operation["op"]), "id": object_id(operation), "record": record,
            "explanation": operation["explanation"], "citations": list(operation["citations"]),
            "support_after": list(operation["support_after"]) if put else [], "developments": []}
    return result


def stage(context, event_id, operation):
    """Stage one operation through the ordinary draft primitives."""
    details = {"explanation": operation["explanation"], "citations": list(operation["citations"])}
    if operation["op"] in PUTS:
        getattr(context, operation["op"])(event_id, deepcopy(operation["record"]),
                                          support_after=list(operation["support_after"]), **details)
    else:
        getattr(context, operation["op"])(event_id, operation["id"], **details)


def binding(record):
    """Shape check of a pending job or published event; ``True`` when it is a controlled graph edit.

    The reserved source prefix and the execution marker must appear together, only on an add-only
    GENERAL event with the fixed source metadata.
    """
    source = record["source"]
    reserved = isinstance(source.get("path"), str) and source["path"].startswith(PREFIX)
    if "execution" not in record:
        if reserved:
            raise ContextError("The reserved graph-edit source prefix requires a deterministic execution marker")
        return False
    value = record["execution"]
    if (not isinstance(value, dict) or set(value) != {"kind", "plan_digest"} or value["kind"] != EXECUTION or
            not isinstance(value["plan_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["plan_digest"])):
        raise ContextError("Invalid deterministic execution marker")
    if (not reserved or record.get("input_mode") != modes.GENERAL or record.get("operation") != "add" or
            "action" in record or "action_associations" in record or
            source["path"] != source_path(record["id"]) or
            {key: member for key, member in source.items() if key not in ("id", "path")} != METADATA):
        raise ContextError("The deterministic execution marker is not bound to a controlled graph-edit source")
    return True


def bound_plan(record, content):
    """The plan of a controlled record after matching its marker to the canonical bytes and base; ``None`` if ordinary."""
    if not binding(record):
        return None
    if not isinstance(content, bytes):
        raise ContextError("The graph-edit source bytes are missing")
    plan = load(content)
    if (digest(plan) != record["execution"]["plan_digest"] or
            record.get("input_digest") != hashlib.sha256(content).hexdigest()):
        raise ContextError("The execution marker does not match the canonical plan bytes")
    if plan["expected_revision"] != record["base_revision"]:
        raise ContextError("The graph-edit plan is not for this event's base")
    return plan


def check_event(event, files):
    """Validate a published controlled edit: marker, canonical bytes, base and exact recorded changes."""
    plan = bound_plan(event, files.get(event["source"]["path"]))
    if plan is None:
        return
    expected = draft(plan)
    changes = event["changes"]
    if (encode(event.get("handoff_note")) != encode(HANDOFF) or len(changes) != len(expected) or
            len({(change["kind"], change["id"]) for change in changes}) != len(changes)):
        raise ContextError("The published graph edit does not match its plan")
    for change in changes:
        wanted = expected.get(f"{change['kind']}:{change['id']}")
        removed = wanted is not None and wanted["record"] is None
        if (wanted is None or (change["operation"] == "remove") != removed or
                encode({key: member for key, member in change.items() if key != "operation"}) !=
                encode({key: member for key, member in wanted.items() if key != "record"})):
            raise ContextError("The published graph edit does not match its plan")
