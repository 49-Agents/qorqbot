"""Driver-facing queue transitions for the backend system principal.

Each helper changes durable state with a compare-and-set inside the caller's transaction, so a repeated tick, recovery
or racing control cannot produce a second outcome or a second notification. None of them calls Context, Git or a
model: the driver does that outside any operational transaction and records the observed result here.

These are internal calls, not registered operations. ``driver_id`` must be an active registered ``system`` principal
(the Context worker); nothing an agent, the owner or a public caller can invoke reaches them, and a public
caller cannot supply publication or attempt results.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from qorqut_operations.contracts import DomainError, new_id, utc_now
from qorqut_operations.discussion.events import emit_event
from qorqut_context.integration.queue.outcomes import TerminalOutcomes, require_outcomes
from qorqut_context.integration.queue.queue import TERMINAL_STATES, detail, encode, invalid

MESSAGE_LIMIT = 2000
SOURCE_TYPE = "context_inputs"
_ID_COLLISION = "A Context event ID is already used by another input or attempt"


def _system(uow, driver_id, *, write: bool = True) -> None:
    if write:
        uow.require_write()
    if not isinstance(driver_id, str) or uow.connection.execute(
            "SELECT 1 FROM principals WHERE id=? AND kind='system' AND active=1", (driver_id,)).fetchone() is None:
        raise DomainError("forbidden", "Only the backend system principal may advance the Context queue")


def _row(uow, input_id: str):
    row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (input_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Context input does not exist")
    return row


def _expect(row, *states: str) -> None:
    if row["state"] not in states:
        raise DomainError("invalid_transition", f"A {row['state']} Context input cannot take this transition",
                          details={"current_state": row["state"]})


def _cas(uow, row, state: str, **columns: Any) -> None:
    assignments = ",".join(f"{name}=?" for name in ("state", *columns))
    cursor = uow.connection.execute(
        f"UPDATE context_inputs SET {assignments} WHERE id=? AND state=? AND attempt=?",
        (state, *columns.values(), row["id"], row["state"], row["attempt"]))
    if cursor.rowcount != 1:
        raise DomainError("state_conflict", "The Context input changed before this transition was saved")


def _concise(error: Mapping[str, Any]) -> dict:
    """A short error mapping: code, message, retryable and optional phase."""
    if not isinstance(error, Mapping) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
        invalid("A queue error requires a code and message")
    result = {"code": error["code"], "message": error["message"][:MESSAGE_LIMIT], "retryable": error.get("retryable") is True}
    if isinstance(error.get("phase"), str):
        result["details"] = {"phase": error["phase"]}
    return result


def head(uow, *, driver_id: str) -> dict | None:
    """The oldest input which is neither published nor discarded, with its history."""
    _system(uow, driver_id, write=False)
    row = uow.connection.execute(
        "SELECT * FROM context_inputs WHERE state NOT IN (?,?) ORDER BY sequence LIMIT 1", TERMINAL_STATES).fetchone()
    return None if row is None else detail(uow, row)


def get(uow, *, driver_id: str, input_id: str) -> dict:
    """One input with its history, for the driver."""
    _system(uow, driver_id, write=False)
    return detail(uow, _row(uow, input_id))


def source_key(row) -> tuple[str, str]:
    """The source an input changes, the same identity ``context_filing.sources`` binds: an Action input's Action, a
    GENERAL input's canonical Context path. ``row`` is a stored row or the lightweight row ``order`` reads."""
    if row["input_kind"] == "action":
        return ("action", row["item_id"])
    path = row["path"] if "path" in row.keys() else json.loads(row["payload_json"])["path"]
    return ("general", path)


def automatic_retry(row) -> dict | None:
    """The automatic retry a blocked input is waiting for (``{retry, of, due_at}``), or None once none is planned."""
    if row["state"] != "blocked" or row["error_json"] is None:
        return None
    planned = json.loads(row["error_json"]).get("automatic_retry")
    return planned if isinstance(planned, Mapping) and isinstance(planned.get("due_at"), str) else None


def order(uow, *, driver_id: str) -> dict:
    """What the driver may do next, read from stored state in one pass over the unfinished inputs in queue order.

    * ``processing``: the one processing input (an interrupted attempt when no live driver holds the lock).
    * ``discard``: the oldest input with a requested discard; a discard only resolves that input's own draft.
    * ``retry``: the oldest blocked input with a pending retry control (an explicit retry, or a due automatic one).
    * ``next``: the oldest input that may go forward in order: a queued input, or a blocked input still waiting for an
      automatic retry, which holds every later input until it is retried.
    * ``blocked``: every blocked input, in queue order.

    A blocked input with no retry planned or requested steps aside: later inputs go on without it, except an input for the
    same source (``source_key``), which waits behind every earlier unfinished input of that source whatever its state."""
    _system(uow, driver_id, write=False)
    pending = {row["input_id"]: row["intent"] for row in uow.connection.execute(
        "SELECT input_id, intent FROM context_input_controls WHERE status='pending'")}
    result = {"processing": None, "discard": None, "retry": None, "next": None, "blocked": []}
    earlier = set()
    for row in uow.connection.execute(
            "SELECT sequence, id, project_id, input_kind, item_id, state, attempt, error_json, "
            "json_extract(payload_json, '$.path') AS path FROM context_inputs WHERE state NOT IN (?,?) ORDER BY sequence",
            TERMINAL_STATES):
        key = source_key(row)
        free = key not in earlier
        earlier.add(key)
        state = row["state"]
        if state == "processing":
            result["processing"] = result["processing"] or dict(row)
        elif state == "discard_requested":
            result["discard"] = result["discard"] or dict(row)
        elif state == "blocked":
            result["blocked"].append(dict(row))
            if pending.get(row["id"]) == "retry":
                if free and result["retry"] is None:
                    result["retry"] = dict(row)
            elif free and result["next"] is None and automatic_retry(row) is not None:
                result["next"] = dict(row)
        elif free and result["next"] is None:
            result["next"] = dict(row)
    return result


def used_event_ids(uow, *, driver_id: str) -> set[str]:
    """Event IDs already persisted for any input or attempt, never to be reissued."""
    _system(uow, driver_id, write=False)
    return ({row[0] for row in uow.connection.execute(
        "SELECT context_event_id FROM context_inputs WHERE context_event_id IS NOT NULL")}
        | {row[0] for row in uow.connection.execute(
            "SELECT context_event_id FROM context_input_attempts WHERE context_event_id IS NOT NULL")})


def _attempt_control(uow, row, driver_id):
    """The ordering check and, for a blocked input, the retry control a new attempt consumes.

    A queued input starts only when it is ``order``'s next input; a blocked input restarts only on its pending retry
    control (explicit, or automatic once due) and only when it is ``order``'s retry. Nothing starts while another input is
    processing."""
    _expect(row, "queued", "blocked")
    now = order(uow, driver_id=driver_id)
    if now["processing"] is not None:
        raise DomainError("invalid_transition", "Another Context input is processing")
    if row["state"] != "blocked":
        if now["next"] is None or now["next"]["id"] != row["id"]:
            raise DomainError("invalid_transition", "Only the next input in queue order may start")
        return None
    control = uow.connection.execute(
        "SELECT * FROM context_input_controls WHERE input_id=? AND intent='retry' AND status='pending'", (row["id"],)).fetchone()
    if control is None:
        raise DomainError("invalid_transition", "A blocked input restarts only after a retry request")
    if now["retry"] is None or now["retry"]["id"] != row["id"]:
        raise DomainError("invalid_transition", "An earlier input for the same source or an earlier retry goes first")
    return control


def _resolve_control(uow, control, result: Mapping[str, Any]) -> None:
    if control:
        uow.connection.execute("UPDATE context_input_controls SET status='applied',resolved_at=?,result_json=? WHERE id=?",
                               (utc_now(), encode({**result, **_request_evidence(control)}), control["id"]))


def request_automatic_retry(uow, *, driver_id: str, input_id: str, at: str) -> dict:
    """Record the driver's own retry request for a blocked input whose planned automatic retry is due at ``at``.

    It is an ordinary pending retry control, requested by the system principal, with ``request_evidence.automatic`` and
    the retry's number, so the next attempt starts exactly as after an explicit retry and its history names it automatic.
    A pending control of any kind, or a retry that is not planned or not yet due, refuses."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    _expect(row, "blocked")
    planned = automatic_retry(row)
    if planned is None or planned["due_at"] > at:
        raise DomainError("invalid_transition", "No automatic retry of this input is due")
    if uow.connection.execute("SELECT 1 FROM context_input_controls WHERE input_id=? AND status='pending'", (input_id,)).fetchone():
        raise DomainError("control_pending", "A control request for this input is already pending")
    evidence = {"automatic": True, "retry": planned["retry"], "of": planned.get("of"), "after_attempt": row["attempt"],
                "due_at": planned["due_at"]}
    uow.connection.execute(
        "INSERT INTO context_input_controls(id,project_id,input_id,intent,expected_state,requested_by,requested_at,status,"
        "result_json) VALUES (?,?,?,'retry','blocked',?,?,'pending',?)",
        (new_id(), row["project_id"], input_id, driver_id, at, encode({"request_evidence": evidence})))
    return detail(uow, _row(uow, input_id))


def start_attempt(uow, *, driver_id: str, input_id: str, context_event_id: str) -> dict:
    """Move the next input to processing with its persisted Context event ID.

    A queued input starts its first attempt. A blocked input restarts only after a retry request (explicit, or the
    driver's automatic one), which this transition consumes. The ID is saved before the driver submits anything to Context."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    control = _attempt_control(uow, row, driver_id)
    if uow.connection.execute(
            "SELECT 1 FROM context_inputs WHERE context_event_id=? AND id!=? UNION "
            "SELECT 1 FROM context_input_attempts WHERE context_event_id=? AND input_id!=?",
            (context_event_id, input_id, context_event_id, input_id)).fetchone():
        raise DomainError("id_collision", _ID_COLLISION)
    attempt = row["attempt"] + 1
    _cas(uow, row, "processing", attempt=attempt, context_event_id=context_event_id, error_json=None)
    uow.connection.execute(
        "INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,control_id,started_at) VALUES (?,?,?,?,?,?)",
        (input_id, attempt, context_event_id, "retry" if control else "queue", control["id"] if control else None, utc_now()))
    _resolve_control(uow, control, {"outcome": "attempt_started", "attempt": attempt})
    return detail(uow, _row(uow, input_id))


def _saved(error: Mapping[str, Any], summary: dict, automatic: Mapping[str, Any] | None) -> dict:
    saved = {**{key: value for key, value in error.items() if isinstance(key, str) and key != "automatic_retry"}, **summary}
    if automatic is not None:
        if not summary["retryable"] or not isinstance(automatic.get("due_at"), str):
            invalid("Only a retryable failure can plan an automatic retry, and it needs its due time")
        saved["automatic_retry"] = dict(automatic)
    return saved


def record_preflight_failure(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any], ordubashy_id: str,
                             automatic_retry: Mapping[str, Any] | None = None) -> dict:
    """Block an input because Context could not be checked before an attempt could safely start.

    The same ordering and retry rules as ``start_attempt`` apply. One real attempt is saved already finished and blocked,
    with the input's existing Context event ID or none (an ID is never manufactured); it consumes only the retry control
    that requested it. ``automatic_retry`` (``{retry, of, due_at}``) plans the driver's own next retry of a retryable
    failure; otherwise one ``context.input_blocked`` notice is emitted for the attempt."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    control = _attempt_control(uow, row, driver_id)
    summary = _concise(error)
    saved = _saved(error, summary, automatic_retry)
    attempt, event_id, now = row["attempt"] + 1, row["context_event_id"], utc_now()
    _cas(uow, row, "blocked", attempt=attempt, error_json=encode(saved))
    uow.connection.execute(
        "INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,control_id,started_at,outcome,finished_at,"
        "error_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (input_id, attempt, event_id, "retry" if control else "queue", control["id"] if control else None, now, "blocked",
         now, encode(saved)))
    _resolve_control(uow, control, {"outcome": "preflight_failed", "attempt": attempt})
    if automatic_retry is None:
        _notify_blocked(uow, driver_id, input_id, event_id, attempt, summary, ordubashy_id)
    return detail(uow, _row(uow, input_id))


# How much of the error text the notice carries, so the whole notice fits an inbox summary; the full error is on the input.
BLOCKED_ERROR_LIMIT = 180
# What the coordinator does with a blocked input, in the words of its own skill (context-relationships).
BLOCKED_NEXT_STEP = ("Yours to resolve: context_queue_get, one context_queue_retry, then a blocking Signoff for the owner "
                     "if it stays blocked.")


def _notify(uow, driver_id, ordubashy_id, input_id, attempt, event_type, text) -> int:
    """One required Global notice to the registered coordinator about an input; its detail is read from the input."""
    recipient = uow.connection.execute(
        "SELECT 1 FROM principals p JOIN agent_profiles a ON a.agent_id=p.id WHERE p.id=? AND p.kind='agent' AND p.active=1 "
        "AND a.status='active'", (ordubashy_id,)).fetchone()
    event = emit_event(uow, project_id=None, source_type=SOURCE_TYPE, source_id=input_id, source_revision=attempt,
                       actor_id=driver_id, event_type=event_type, summary=text, required=(ordubashy_id,)) if recipient else None
    if event is None:
        raise DomainError("validation_error", "The Context handoff recipient must be an active agent that can receive Global notices")
    return event


def _notify_blocked(uow, driver_id, input_id, context_event_id, attempt, error, ordubashy_id) -> int:
    """The single intervention notice of one attempt; ``context_event_id`` is null when none was ever allocated.

    It is sent once the driver plans no automatic retry. The blocked input then steps aside: later inputs go on, and only
    later inputs for the same source wait behind it. The notice says whose it is and what follows: the coordinator
    resolves it, and when one retry does not clear it the owner sees it as a blocking Signoff. The instruction comes
    before the error text, which is the part a length limit may cut."""
    return _notify(uow, driver_id, ordubashy_id, input_id, attempt, "context.input_blocked",
                   f"Context input {input_id} is blocked on attempt {attempt}"
                   f"{f' (event {context_event_id})' if context_event_id else ''}; later inputs for the same source wait. "
                   f"{BLOCKED_NEXT_STEP} Error: {error['code']}: {error['message'][:BLOCKED_ERROR_LIMIT]}")


def _handoff(note: Any) -> dict:
    """The committed Context event's exact handoff disposition; nothing is inferred or defaulted."""
    if (not isinstance(note, Mapping) or set(note) != {"disposition", "text"} or note["disposition"] not in ("prepared", "empty")
            or not isinstance(note["text"], str) or (note["disposition"] == "prepared") != bool(note["text"])):
        invalid("A publication requires the committed handoff_note with disposition prepared and nonempty text, "
                "or disposition empty and empty text")
    return {"disposition": note["disposition"], "text": note["text"]}


def _pending_discard(uow, input_id: str):
    return uow.connection.execute(
        "SELECT * FROM context_input_controls WHERE input_id=? AND intent='discard' AND status='pending'", (input_id,)).fetchone()


def _request_evidence(control) -> dict:
    """What the pending control carries from its request: its recorded ``request_evidence`` object and its own ID, which
    resolution keeps instead of replacing. A control requested without evidence adds nothing."""
    if control is None or control["result_json"] is None:
        return {}
    evidence = json.loads(control["result_json"]).get("request_evidence")
    return {} if evidence is None else {"request_evidence": evidence, "control_id": control["id"]}


def record_published(uow, *, driver_id: str, input_id: str, result: Mapping[str, Any], handoff_note: Mapping[str, Any],
                     ordubashy_id: str, outcomes: TerminalOutcomes) -> dict:
    """Record the verified published event and notify the coordinator of a prepared note.

    ``result`` is the verified publication: the exact event and its actual Git ``published_commit``. ``handoff_note`` is
    the committed event's exact ``{disposition, text}``, kept in the saved result. An explicit empty handoff is recorded
    and notifies nobody. Neither does the note of an Action input: the Action was recorded by an agent who already knows
    what it did, so a notice about filing it only wakes the coordinator to read its own news back. The note stays in the
    saved result for anyone who reads the input. A discard request that found the event already published is closed as published, never as a
    discarded input. The terminal-outcome contribution records the input's completion receipt in this same transaction."""
    _system(uow, driver_id)
    outcomes = require_outcomes(outcomes)
    row = _row(uow, input_id)
    _expect(row, "processing", "discard_requested")
    note = _handoff(handoff_note)
    if (not isinstance(result, Mapping) or result.get("context_event_id") != row["context_event_id"]
            or not isinstance(result.get("published_commit"), str) or not result["published_commit"]):
        invalid("A publication result requires its exact event and published commit")
    notice = None
    if note["disposition"] == "prepared" and row["input_kind"] != "action":
        notice = _notify(uow, driver_id, ordubashy_id, input_id, row["attempt"], "context.handoff_published",
                         f"Context input {input_id} published as event {row['context_event_id']}: a handoff note is prepared")
    saved = {**result, "handoff_note": note, "handoff_event_id": notice}
    now = utc_now()
    if row["state"] == "processing":
        cursor = uow.connection.execute(
            "UPDATE context_input_attempts SET outcome='published',finished_at=?,result_json=? "
            "WHERE input_id=? AND attempt=? AND outcome IS NULL", (now, encode(saved), input_id, row["attempt"]))
        if cursor.rowcount != 1:
            raise DomainError("state_conflict", "The processing attempt is not open")
    else:
        uow.connection.execute(
            "UPDATE context_input_controls SET status='rejected',resolved_at=?,result_json=? "
            "WHERE input_id=? AND intent='discard' AND status='pending'",
            (now, encode({"outcome": "already_published", "context_event_id": row["context_event_id"],
                          **_request_evidence(_pending_discard(uow, input_id))}), input_id))
    _cas(uow, row, "published", result_json=encode(saved), error_json=None)
    final = _row(uow, input_id)
    outcomes.record(uow, final)
    return detail(uow, final)


def record_blocked(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any], ordubashy_id: str,
                   automatic_retry: Mapping[str, Any] | None = None) -> dict:
    """Block the processing input after a failed or interrupted attempt.

    ``automatic_retry`` (``{retry, of, due_at}``) plans the driver's own next retry of a retryable failure: the input
    waits for it, holding every later input, and no notice is sent. Without it the input steps aside (only later inputs
    for the same source wait) and exactly one intervention notice exists for the attempt: the processing state is left
    only once. The draft note, if any, is deliberately not in the notice."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    _expect(row, "processing")
    summary = _concise(error)
    saved = _saved(error, summary, automatic_retry)
    if automatic_retry is None:
        _notify_blocked(uow, driver_id, input_id, row["context_event_id"], row["attempt"], summary, ordubashy_id)
    cursor = uow.connection.execute(
        "UPDATE context_input_attempts SET outcome='blocked',finished_at=?,error_json=? WHERE input_id=? AND attempt=? "
        "AND outcome IS NULL", (utc_now(), encode(saved), input_id, row["attempt"]))
    if cursor.rowcount != 1:
        raise DomainError("state_conflict", "The processing attempt is not open")
    _cas(uow, row, "blocked", error_json=encode(saved))
    return detail(uow, _row(uow, input_id))


def record_discarded(uow, *, driver_id: str, input_id: str, result: Mapping[str, Any], outcomes: TerminalOutcomes) -> dict:
    """Close a requested discard after the mechanical Context discard completed, recording the input's completion
    receipt through the terminal-outcome contribution in the same transaction."""
    _system(uow, driver_id)
    outcomes = require_outcomes(outcomes)
    row = _row(uow, input_id)
    _expect(row, "discard_requested")
    saved = {**result, "outcome": "discarded", **_request_evidence(_pending_discard(uow, input_id))}
    uow.connection.execute(
        "UPDATE context_input_controls SET status='applied',resolved_at=?,result_json=? "
        "WHERE input_id=? AND intent='discard' AND status='pending'", (utc_now(), encode(saved), input_id))
    _cas(uow, row, "discarded", result_json=encode(saved), error_json=None)
    final = _row(uow, input_id)
    outcomes.record(uow, final)
    return detail(uow, final)


def record_refusal_discarded(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any], ordubashy_id: str,
                             caller_id: str | None, outcomes: TerminalOutcomes) -> dict:
    """End the input at once for a preflight refusal that no retry can clear (a graph edit refused as ``stale_base``).

    Everything happens in this one transaction and through the ordinary transitions: the refused attempt is saved finished
    and blocked exactly as ``record_preflight_failure`` saves it (consuming a pending retry control), the driver itself
    records a discard request (an ordinary ``discard`` control requested by the system principal, carrying the refusal as
    its ``request_evidence``), the input moves blocked -> discard_requested, and ``record_discarded`` closes it with the
    terminal-outcome contribution. No Context work exists to undo. Instead of the blocked notice, one required
    ``context.input_discarded`` notice goes to the coordinator and, when it is another addressable agent, to the original
    caller, with the refusal reason; later inputs proceed."""
    _system(uow, driver_id)
    outcomes = require_outcomes(outcomes)
    row = _row(uow, input_id)
    control = _attempt_control(uow, row, driver_id)
    summary = {**_concise(error), "retryable": False}
    saved = {**{key: value for key, value in error.items() if isinstance(key, str)}, **summary}
    attempt, event_id, now = row["attempt"] + 1, row["context_event_id"], utc_now()
    _cas(uow, row, "blocked", attempt=attempt, error_json=encode(saved))
    uow.connection.execute(
        "INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,control_id,started_at,outcome,finished_at,"
        "error_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (input_id, attempt, event_id, "retry" if control else "queue", control["id"] if control else None, now, "blocked",
         now, encode(saved)))
    _resolve_control(uow, control, {"outcome": "preflight_failed", "attempt": attempt})
    uow.connection.execute(
        "INSERT INTO context_input_controls(id,project_id,input_id,intent,expected_state,requested_by,requested_at,status,"
        "result_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id(), row["project_id"], input_id, "discard", "blocked", driver_id, now, "pending",
         encode({"request_evidence": {"reason": "refused_unretryable", "error": summary}})))
    _cas(uow, _row(uow, input_id), "discard_requested")
    text = (f"Context input {input_id} was refused and discarded on attempt {attempt}; nothing was published and Context "
            f"continues. A retry cannot fix this: read the Context again and submit a new change if it is still wanted. "
            f"Error: {summary['code']}: {summary['message'][:BLOCKED_ERROR_LIMIT]}")
    recipient = uow.connection.execute(
        "SELECT 1 FROM principals p JOIN agent_profiles a ON a.agent_id=p.id WHERE p.id=? AND p.kind='agent' AND p.active=1 "
        "AND a.status='active'", (ordubashy_id,)).fetchone()
    notice = emit_event(uow, project_id=None, source_type=SOURCE_TYPE, source_id=input_id, source_revision=attempt,
                        actor_id=driver_id, event_type="context.input_discarded", summary=text,
                        required=tuple(dict.fromkeys(p for p in (ordubashy_id, caller_id) if p))) if recipient else None
    if notice is None:
        raise DomainError("validation_error", "The Context handoff recipient must be an active agent that can receive Global notices")
    return record_discarded(uow, driver_id=driver_id, input_id=input_id, outcomes=outcomes,
                            result={"context_event_id": event_id, "context": "no_event" if event_id is None else "no_draft",
                                    "mechanical": True, "refusal": summary, "notice_event_id": notice})


def record_discard_failure(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any]) -> dict:
    """Keep a requested discard visible when Context could not complete it yet."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    _expect(row, "discard_requested")
    _cas(uow, row, "discard_requested", error_json=encode(_concise(error)))
    return detail(uow, _row(uow, input_id))
