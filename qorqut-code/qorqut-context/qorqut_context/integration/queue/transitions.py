"""Driver-facing queue transitions for the backend system principal.

Each helper changes durable state with a compare-and-set inside the caller's transaction, so a repeated tick, recovery
or racing control cannot produce a second outcome or a second notification. None of them calls Context, Git or a
model: the driver does that outside any operational transaction and records the observed result here.

These are internal calls, not registered operations. ``driver_id`` must be an active registered ``system`` principal
(the Context worker); nothing a business agent, the owner or a public caller can invoke reaches them, and a public
caller cannot supply publication or attempt results.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from qorqut_operations.contracts import DomainError, utc_now
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


def used_event_ids(uow, *, driver_id: str) -> set[str]:
    """Event IDs already persisted for any input or attempt, never to be reissued."""
    _system(uow, driver_id, write=False)
    return ({row[0] for row in uow.connection.execute(
        "SELECT context_event_id FROM context_inputs WHERE context_event_id IS NOT NULL")}
        | {row[0] for row in uow.connection.execute(
            "SELECT context_event_id FROM context_input_attempts WHERE context_event_id IS NOT NULL")})


def _attempt_control(uow, row):
    """The oldest-unfinished check and, for a blocked head, the explicit retry control a new attempt consumes."""
    _expect(row, "queued", "blocked")
    if uow.connection.execute("SELECT 1 FROM context_inputs WHERE sequence < ? AND state NOT IN (?,?)",
                              (row["sequence"], *TERMINAL_STATES)).fetchone():
        raise DomainError("invalid_transition", "Only the oldest unfinished input may start")
    if row["state"] != "blocked":
        return None
    control = uow.connection.execute(
        "SELECT * FROM context_input_controls WHERE input_id=? AND intent='retry' AND status='pending'", (row["id"],)).fetchone()
    if control is None:
        raise DomainError("invalid_transition", "A blocked input restarts only after an explicit retry request")
    return control


def _resolve_control(uow, control, result: Mapping[str, Any]) -> None:
    if control:
        uow.connection.execute("UPDATE context_input_controls SET status='applied',resolved_at=?,result_json=? WHERE id=?",
                               (utc_now(), encode(result), control["id"]))


def start_attempt(uow, *, driver_id: str, input_id: str, context_event_id: str) -> dict:
    """Move the queue head to processing with its persisted Context event ID.

    A queued head starts its first attempt. A blocked head restarts only after an explicit retry request, which this
    transition consumes. The ID is saved before the driver submits anything to Context."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    control = _attempt_control(uow, row)
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


def record_preflight_failure(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any], ordubashy_id: str) -> dict:
    """Block the head because Context could not be checked before an attempt could safely start.

    The same oldest-unfinished and explicit-retry rules as ``start_attempt`` apply. One real attempt is saved already
    finished and blocked, with the input's existing Context event ID or none (an ID is never manufactured); it consumes
    only the retry control that requested it, and one ``context.input_blocked`` notice is emitted for it."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    control = _attempt_control(uow, row)
    summary = _concise(error)
    saved = {**{key: value for key, value in error.items() if isinstance(key, str)}, **summary}
    attempt, event_id, now = row["attempt"] + 1, row["context_event_id"], utc_now()
    _cas(uow, row, "blocked", attempt=attempt, error_json=encode(saved))
    uow.connection.execute(
        "INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,control_id,started_at,outcome,finished_at,"
        "error_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (input_id, attempt, event_id, "retry" if control else "queue", control["id"] if control else None, now, "blocked",
         now, encode(saved)))
    _resolve_control(uow, control, {"outcome": "preflight_failed", "attempt": attempt})
    _notify_blocked(uow, driver_id, input_id, event_id, attempt, summary, ordubashy_id)
    return detail(uow, _row(uow, input_id))


def _notify(uow, driver_id, ordubashy_id, input_id, attempt, event_type, text) -> int:
    """One required Global notice to the registered coordinator about an input; its detail is read from the input."""
    recipient = uow.connection.execute(
        "SELECT 1 FROM principals p JOIN agent_profiles a ON a.agent_id=p.id WHERE p.id=? AND p.kind='agent' AND p.active=1 "
        "AND a.status='active'", (ordubashy_id,)).fetchone()
    event = emit_event(uow, business_id=None, source_type=SOURCE_TYPE, source_id=input_id, source_revision=attempt,
                       actor_id=driver_id, event_type=event_type, summary=text, required=(ordubashy_id,)) if recipient else None
    if event is None:
        raise DomainError("validation_error", "The Context handoff recipient must be an active agent that can receive Global notices")
    return event


def _notify_blocked(uow, driver_id, input_id, context_event_id, attempt, error, ordubashy_id) -> int:
    """The single intervention notice of one attempt; ``context_event_id`` is null when none was ever allocated."""
    return _notify(uow, driver_id, ordubashy_id, input_id, attempt, "context.input_blocked",
                   f"Context input {input_id} is blocked on attempt {attempt}"
                   f"{f' (event {context_event_id})' if context_event_id else ''}: {error['code']}: {error['message'][:240]}")


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
    and notifies nobody. A discard request that found the event already published is closed as published, never as a
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
    if note["disposition"] == "prepared":
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


def record_blocked(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any], ordubashy_id: str) -> dict:
    """Block the head after a failed or interrupted attempt; later inputs wait.

    Exactly one intervention notice exists per attempt: the processing state is left only once. The draft note, if any,
    is deliberately not in the notice."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    _expect(row, "processing")
    summary = _concise(error)
    saved = {**{key: value for key, value in error.items() if isinstance(key, str)}, **summary}
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


def record_discard_failure(uow, *, driver_id: str, input_id: str, error: Mapping[str, Any]) -> dict:
    """Keep a requested discard visible when Context could not complete it yet."""
    _system(uow, driver_id)
    row = _row(uow, input_id)
    _expect(row, "discard_requested")
    _cas(uow, row, "discard_requested", error_json=encode(_concise(error)))
    return detail(uow, _row(uow, input_id))
