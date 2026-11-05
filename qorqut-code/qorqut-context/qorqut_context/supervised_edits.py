"""Supervised edits: one preserved supporting Markdown document plus explicit node/edge/note operations.

The batch is executed as a controlled GENERAL event with no model. The document is the add-only source
``foundation/supervised-edits/{event_id}.md``, kept byte for byte; the normalized plan and the document digest
are retained in the event's execution metadata so the intended edits can be verified independently of the prose.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re

from . import input_modes as modes
from .schema import (ContextError, change_details, digest, encode, identifier, note_record,
                     object_record, record_path, text)

FORMAT = "qorqut-supervised-edit-v1"
PREFIX = "foundation/supervised-edits/"
EXECUTION = "deterministic_supervised_edit"
NOUN = "supervised edit"
HANDOFF = {"disposition": "empty", "text": ""}
PUTS = {"put_node": "node", "put_edge": "edge", "put_note": "note"}
REMOVES = {"remove_node": "node", "remove_edge": "edge", "remove_note": "note"}
_PLAN = {"format", "expected_revision", "rationale", "operations"}
_PUT = {"op", "record", "explanation", "citations", "support_after"}
_REMOVE = {"op", "id", "explanation", "citations"}
_MARKER = {"kind", "plan_digest", "document_digest", "plan"}


def source_path(event_id):
    return PREFIX + identifier(event_id) + ".md"


def metadata(event_id):
    return {"kind": "supervised_edit", "title": f"Supervised edit {identifier(event_id)}",
            "origin": "controlled_caller"}


def kind(verb):
    return {**PUTS, **REMOVES}[verb]


def object_id(operation):
    return operation["record"]["id"] if operation["op"] in PUTS else operation["id"]


def object_ids(plan):
    return {object_id(operation) for operation in plan["operations"]}


def document(content, limit=modes.SOURCE_BYTES):
    """The supporting document bytes, validated as nonempty UTF-8 within the source ceiling."""
    if not isinstance(content, bytes):
        raise ContextError("The supervised supporting document must be bytes")
    if len(content) > limit:
        raise ContextError("The supervised supporting document exceeds the configured byte limit")
    try:
        body = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextError("The supervised supporting document must be UTF-8 text") from error
    if not body.strip():
        raise ContextError("The supervised supporting document must not be empty")
    return content


def document_digest(content):
    return hashlib.sha256(content).hexdigest()


def normalize(plan, limit=None):
    """The canonical plan, rejecting unknown fields, unsupported verbs and invalid records before any staging.

    Existing record, note, explanation, citation and support validators are reused; nothing is allocated,
    inferred or merged. ``limit`` is the byte ceiling a note's text must obey (``None`` when unknown).
    """
    if not isinstance(plan, dict) or set(plan) != _PLAN:
        raise ContextError(f"A supervised edit requires exactly {sorted(_PLAN)}")
    if plan["format"] != FORMAT:
        raise ContextError(f"Unsupported supervised edit format; expected {FORMAT}")
    expected_revision = modes.full_commit(plan["expected_revision"], "expected_revision")
    rationale = text(plan["rationale"], "rationale")
    operations = plan["operations"]
    if not isinstance(operations, list) or not operations:
        raise ContextError("A supervised edit requires a nonempty operations list")
    seen, result = set(), []
    for item in operations:
        if not isinstance(item, dict):
            raise ContextError("Every supervised edit operation must be an object")
        verb = item.get("op")
        if not isinstance(verb, str) or verb not in PUTS and verb not in REMOVES:
            raise ContextError("Unsupported supervised edit verb; use put_/remove_ node, edge or note")
        if verb in PUTS:
            if set(item) != _PUT:
                raise ContextError(f"{verb} requires exactly {sorted(_PUT)}")
            record = (note_record(item["record"], limit) if PUTS[verb] == "note"
                      else object_record(PUTS[verb], item["record"]))
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
            raise ContextError("A record ID may appear once in a supervised edit")
        seen.add(target)
        result.append(entry)
    return {"format": FORMAT, "expected_revision": expected_revision, "rationale": rationale,
            "operations": result}


def canonical(plan):
    try:
        return encode(plan)
    except UnicodeEncodeError as error:
        raise ContextError("A supervised edit must be valid UTF-8") from error


def marker(plan, content):
    """The execution metadata: the normalized plan and its digest, and the digest of the exact document bytes."""
    return {"kind": EXECUTION, "plan_digest": digest(plan), "document_digest": document_digest(content),
            "plan": deepcopy(plan)}


def draft(plan):
    """The exact draft a plan stages: each operation's record or removal with its attribution and no developments."""
    result = {}
    for operation in plan["operations"]:
        put = operation["op"] in PUTS
        result[f"{kind(operation['op'])}:{object_id(operation)}"] = {
            "kind": kind(operation["op"]), "id": object_id(operation),
            "record": deepcopy(operation["record"]) if put else None,
            "explanation": operation["explanation"], "citations": list(operation["citations"]),
            "support_after": list(operation["support_after"]) if put else [], "developments": []}
    return result


def expected_files(plan):
    """The exact bytes each operation leaves at its record path: the encoded normalized record, ``None`` for a removal."""
    return {record_path(kind(operation["op"]), object_id(operation)):
            encode(operation["record"]) if operation["op"] in PUTS else None
            for operation in plan["operations"]}


def check_commit(plan, read):
    """Verify, in the commit that introduced a supervised edit, every put's record and every removal's absence.

    ``read(paths)`` returns the bytes each path holds in that commit's tree (``None`` where absent). Only that
    commit is compared, so a later legitimate edit or removal of the same record never invalidates it.
    """
    expected = expected_files(plan)
    if read(sorted(expected)) != expected:
        raise ContextError("The supervised edit's records do not match its plan in the commit that published it")


def stage(context, event_id, operation):
    """Stage one operation through the ordinary draft primitives."""
    details = {"explanation": operation["explanation"], "citations": list(operation["citations"])}
    if operation["op"] in PUTS:
        getattr(context, operation["op"])(event_id, deepcopy(operation["record"]),
                                          support_after=list(operation["support_after"]), **details)
    else:
        getattr(context, operation["op"])(event_id, operation["id"], **details)


def binding(record):
    """Shape check of a pending job or published event; ``True`` when it is a supervised edit.

    The reserved source prefix and the execution marker select this route only together, on an add-only
    GENERAL event with the fixed source metadata. Text, a filename or a claim of approval selects nothing.
    """
    source = record["source"]
    reserved = isinstance(source.get("path"), str) and source["path"].startswith(PREFIX)
    value = record.get("execution")
    mine = isinstance(value, dict) and value.get("kind") == EXECUTION
    if not reserved and not mine:
        return False
    if (not reserved or not mine or set(value) != _MARKER or not isinstance(value["plan"], dict) or
            not all(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key])
                    for key in ("plan_digest", "document_digest"))):
        raise ContextError("The supervised-edit source prefix and execution marker must appear together and be valid")
    if (record.get("input_mode") != modes.GENERAL or record.get("operation") != "add" or
            "action" in record or "action_associations" in record or
            source.get("path") != source_path(record["id"]) or
            {key: member for key, member in source.items() if key not in ("id", "path")} != metadata(record["id"])):
        raise ContextError("The supervised execution marker is not bound to a supervised-edit source")
    return True


def bound_plan(record, content):
    """The plan of a supervised record after matching its marker to the exact document bytes and base; ``None`` if ordinary."""
    if not binding(record):
        return None
    if not isinstance(content, bytes):
        raise ContextError("The supervised supporting document bytes are missing")
    value = record["execution"]
    plan = normalize(value["plan"], record.get("source_bytes"))
    if (encode(plan) != encode(value["plan"]) or digest(plan) != value["plan_digest"] or
            document_digest(content) != value["document_digest"] or
            record.get("input_digest") != value["document_digest"]):
        raise ContextError("The execution marker does not match the normalized plan and document bytes")
    if plan["expected_revision"] != record["base_revision"]:
        raise ContextError("The supervised plan is not for this event's base")
    return plan


def check_event(event, files):
    """Validate a published supervised edit: marker, document bytes, base and exact recorded changes."""
    plan = bound_plan(event, files.get(event["source"]["path"]))
    if plan is None:
        return
    expected = draft(plan)
    changes = event["changes"]
    if (encode(event.get("handoff_note")) != encode(HANDOFF) or len(changes) != len(expected) or
            len({(change["kind"], change["id"]) for change in changes}) != len(changes)):
        raise ContextError("The published supervised edit does not match its plan")
    for change in changes:
        wanted = expected.get(f"{change['kind']}:{change['id']}")
        removed = wanted is not None and wanted["record"] is None
        if (wanted is None or (change["operation"] == "remove") != removed or
                encode({key: member for key, member in change.items() if key != "operation"}) !=
                encode({key: member for key, member in wanted.items() if key != "record"})):
            raise ContextError("The published supervised edit does not match its plan")
