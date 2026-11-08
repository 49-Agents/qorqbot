"""Retained learning state, append-only history and bounded read helpers.

The standalone procedural engine's issue and work machinery is gone. These helpers are the vocabulary shared by the
factual Context route and the read-only historical view over the rows that route still preserves: the state names, the
one append-only history record, the exact reasoning attribution, the bounded content cursor and the candidate agenda.
They read only the five shared learning tables; no removed issue or engine table is referenced here.
"""

import base64
import json

from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, new_id, plain
from qorqut_operations.application.plans import page

# The stored state vocabulary, including the states only historical procedural rows still carry.
STATES = ("open", "accepted", "in-progress", "implemented", "rejected", "rejected-in-work")

# One bounded agenda response: the selected page never exceeds this serialized size.
AGENDA_RESPONSE_BYTES = 48000
AGENDA_MAX_CANDIDATES = 20000
RECENT_WORK_REASONING = 20

# A chunk of one serialized response page; the client concatenates the chunks in order.
CONTENT_CHUNK_CHARS = 6000


def history_snapshot(item):
    return {key: plain(item.get(key)) for key in (
        "revision", "state", "decision", "scope", "bucket", "tag", "text", "agent_note", "targets", "outcome", "filing",
        "reconciled_into", "reconciliations", "reconciliation_reviewed", "superseded_by", "supersession")}


def record_history(builder, candidate_id, before, after, work_reasoning=None):
    """Append one audit record. ``work_reasoning`` is preserved for the historical records that carry it; no retained
    mutator writes a new work action, and the existing field stays in its own JSON."""
    identity = new_id()
    builder.record("learning_history", identity, None, {
        "id": identity, "candidate_id": candidate_id, "event": builder.operation,
        "actor_id": builder.ctx.principal_id, "before_json": history_snapshot(before),
        "after_json": {**history_snapshot(after), **({"work_reasoning": work_reasoning} if work_reasoning else {})},
        "created_at": builder.at,
    })


def content_cursor(binding, offset):
    """One position in a serialized response page; the binding proves it belongs to that exact page."""
    return base64.urlsafe_b64encode(canonical_bytes(
        {"kind": "content", "version": 2, "binding": binding, "offset": offset})).decode("ascii")


def compact_history_event(event):
    """A history row with oversized snapshots replaced by an explicit preview, hash and truncation marker.

    Full snapshots stay in the append-only audit. The owner's reasoning is exact text, bounded at its own documented
    limit, and is never abbreviated.
    """
    value = plain(event)
    for side in ("before_json", "after_json"):
        snapshot = value[side]
        for key, content in list(snapshot.items()):
            if key != "work_reasoning" and len(canonical_bytes(content)) > 2000:
                snapshot[key] = {"preview": canonical_bytes(content)[:1500].decode("utf-8", errors="ignore"),
                                 "truncated": True, "hash": canonical_hash(content)}
    return value


def _history_query(uow, business_id, candidate_id, *, columns="*", limit=None, where=""):
    statement = ("SELECT " + columns + " FROM learning_history WHERE business_id IS ? AND candidate_id = ? "
                 + (" AND (" + where + ") " if where else " ") + "ORDER BY created_at DESC, rowid DESC")
    parameters = [business_id, candidate_id]
    if limit is not None:
        statement += " LIMIT ?"
        parameters.append(limit)
    return uow.connection.execute(statement, parameters).fetchall()


def work_reasoning_of(uow, business_id, candidate_id, limit=RECENT_WORK_REASONING):
    """The owner's reasoning recorded with each historical work action on one item, newest first."""
    found = _history_query(uow, business_id, candidate_id,
                           columns="id, created_at, json_extract(after_json, '$.work_reasoning') AS reasoning",
                           where="json_extract(after_json, '$.work_reasoning') IS NOT NULL", limit=limit + 1)
    return [{**json.loads(raw["reasoning"]), "history_id": raw["id"], "at": raw["created_at"]}
            for raw in found[:limit]], len(found) <= limit


def feedback_source(uow, business_id, candidate_id, note):
    """How the decision's current feedback was given, as the newest history record that carries a source says."""
    raw = uow.connection.execute(
        "SELECT json_extract(after_json, '$.decision.reasoning_source') AS source, "
        "json_extract(after_json, '$.decision.note') AS note FROM learning_history "
        "WHERE business_id IS ? AND candidate_id = ? AND json_extract(after_json, '$.decision.reasoning_source') IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (business_id, candidate_id)).fetchone()
    return raw["source"] if raw and raw["note"] == note else None


def agent_rationale_of(uow, business_id, candidate_id):
    """The recording agent's own rationale, stored separately from the owner's reasoning in the item's history."""
    raw = uow.connection.execute(
        "SELECT id, created_at, json_extract(after_json, '$.decision.agent_rationale') AS rationale FROM learning_history "
        "WHERE business_id IS ? AND candidate_id = ? AND json_extract(after_json, '$.decision.agent_rationale') IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (business_id, candidate_id)).fetchone()
    if not raw:
        return None
    return {"text": raw["rationale"], "source": "agent_rationale", "history_id": raw["id"], "at": raw["created_at"]}


def reasoning_projection(uow, business_id, item):
    """The owner's reasoning, the agent's own rationale and any historical work reasoning, each attributed separately.

    Feedback older than any recorded source stays visible but is never relabelled as owner reasoning.
    """
    shown = {}
    decision = item.get("decision") or {}
    if decision.get("note"):
        source = feedback_source(uow, business_id, item["id"], decision["note"])
        shown["reasoning"] = {"text": decision["note"], "source": source or "legacy_feedback_unattributed",
                              "actor_id": decision.get("actor"), "at": decision.get("created")}
    rationale = agent_rationale_of(uow, business_id, item["id"])
    if rationale:
        shown["agent_rationale"] = rationale
    if item.get("state") in {"implemented", "rejected-in-work"} and item.get("agent_note"):
        shown["outcome_reasoning"] = {"text": item["agent_note"], "source": "agent_work_explanation", "state": item["state"]}
    work, complete = work_reasoning_of(uow, business_id, item["id"])
    if work:
        shown["work_reasoning"] = work
        if not complete:
            shown["work_reasoning_complete"] = False
    return shown


def agenda(ctx, args, options, uow):
    """The factual review agenda across the caller's authorized scopes.

    Only stored summaries are projected in SQLite: no extraction evidence, variants or unrelated buckets are decoded into
    the agenda, and no issue or engine projection is added. Filed and collapsed items are excluded. The default agenda and
    its counts are factual Context candidates only; historical deltas appear only when the caller explicitly selects that
    bucket.
    """
    businesses = [ctx.business_id] if ctx.business_id else sorted(ctx.allowed_business_ids)
    include_global = not ctx.business_id and 'read_global_context' in ctx.global_capabilities
    states = args.get("states", ["open"])
    business_marks = ",".join("?" for _ in businesses) or "NULL"
    state_marks = ",".join("?" for _ in states)
    clauses, parameters = ["state IN (" + state_marks + ")"], [*businesses, *states]
    clauses.extend(["filed=0", "reconciled=0"])
    if "bucket" not in args:
        clauses.append("bucket='context'")
    for key in ("tag", "scope", "bucket"):
        if key in args:
            clauses.append(key + "=?")
            parameters.append(args[key])
    projected = ["c.id", "c.business_id", "c.revision", "COALESCE(c.work_state,d.state,'open') AS state",
                 "substr(c.agent_note,1,300) AS agent_note",
                 "CASE WHEN c.filing_json='{}' THEN 0 ELSE 1 END AS filed",
                 "CASE WHEN json_extract(c.candidate_json,'$.reconciled_into') IS NULL THEN 0 ELSE 1 END AS reconciled"]
    for key in ("bucket", "tag", "scope", "text"):
        value = "COALESCE(json_extract(d.changes_json,'$." + key + "'),json_extract(c.candidate_json,'$." + key + "'))"
        projected.append(("substr(" + value + ",1,600)" if key == "text" else value) + " AS " + key)
    values = [dict(row) for row in uow.connection.execute(
        "SELECT * FROM (SELECT " + ",".join(projected) + " FROM learning_candidates c LEFT JOIN learning_decisions d "
        "ON d.business_id IS c.business_id AND d.candidate_id=c.id WHERE (c.business_id IN (" + business_marks + ")" +
        (" OR c.business_id IS NULL" if include_global else "") + ")) WHERE " + " AND ".join(clauses) +
        " LIMIT ?", [*parameters, AGENDA_MAX_CANDIDATES + 1])]
    if len(values) > AGENDA_MAX_CANDIDATES:
        raise DomainError("result_too_large", "Narrow the learning agenda by business, state, tag or scope")
    # Bind pagination to the complete authorized scope, including revocations.
    paging = {**args, "_factual": "context-only-v2", "limit": min(args.get("limit", 20), 50)}
    selected, cursor = page(values, paging, [businesses, include_global], "learnings_review_list",
                            key=lambda x: (x["business_id"] or "", x["id"]), reverse=False)
    size, count = 0, 0
    for item in selected:
        length = len(canonical_bytes(item))
        if count and size + length > AGENDA_RESPONSE_BYTES:
            break
        size, count = size + length, count + 1
    if count < len(selected):
        selected, cursor = page(values, {**paging, "limit": count}, [businesses, include_global], "learnings_review_list",
                                key=lambda x: (x["business_id"] or "", x["id"]), reverse=False)
    return {"items": selected, "counts": {state: sum(x["state"] == state for x in values) for state in states},
            "next_cursor": cursor}