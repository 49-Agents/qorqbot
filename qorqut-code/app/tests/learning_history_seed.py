"""Synthetic retained learning history for the browser fixture and its behavioral checks.

The target core rejects procedural-learning mutations on historical deltas. This module therefore writes the
candidate, decision, work state and audit snapshots directly through the repositories, using the retained work
semantics: a decision advances the record to revision 2, work starts at revision 3 and terminates at revision 4,
and every audit ``before_json`` is exactly the previous ``after_json``. No application/operation is invoked.
"""
from qorqut_operations.contracts import new_id

DECISION_KEYS = ("accepted", "active", "rejected", "dropped", "done")
WORK = {
    "active": [("work_start", "in-progress", "Agent progress active", {})],
    "dropped": [
        ("work_start", "in-progress", "", {}),
        ("work_drop", "rejected-in-work", "Owner chose another approach <literal>", {}),
    ],
    "done": [
        ("work_start", "in-progress", "", {}),
        ("work_done", "implemented", "Existing behavior verified; no changes needed.",
         {"kind": "already-covered", "reason": "Verified in the fixture"}),
    ],
}
STAMPS = ("2026-09-08T01:00:00Z", "2026-09-08T02:00:00Z", "2026-09-08T03:00:00Z")


def seed_learning_history(database, business_id, principal_id, *, ids=None, application=None):
    """Seed the browser fixture's local retained history and return the identity map.

    ``application`` is accepted only so a behavioral check can pass a refusing fake and prove this routine never
    attempts an operation. It is deliberately unused.
    """
    identities = dict(ids or {})
    for key in ("open", "accepted", "active", "rejected", "dropped", "done", "context"):
        identities.setdefault(key, new_id())
    for key, identity in identities.items():
        if key not in {"open", "accepted", "active", "rejected", "dropped", "done", "context"}:
            continue
        is_context = key == "context"
        text = "Learning fixture " + key
        tag = "question" if is_context else "rule"
        bucket = "context" if is_context else "delta"
        candidate = {"text": text, "scope": "biz", "tag": tag, "bucket": bucket,
                     "created": "2026-09-08T00:00:00Z"}
        with database.transaction() as uow:
            uow.repository("learning_candidates", business_id=business_id).insert(
                {"id": identity, "candidate_json": candidate})
        if key in {"open", "context"}:
            continue

        decision_state = "rejected" if key == "rejected" else "accepted"
        note = ("Owner rejected " + key) if key == "rejected" else ("Owner accepted " + key)
        base = {"revision": 1, "state": "open", "decision": None, "scope": "biz", "bucket": "delta",
                "tag": tag, "text": text}
        decided = {**base, "revision": 2, "state": decision_state,
                   "decision": {"state": decision_state, "note": note}}
        with database.transaction() as uow:
            uow.repository("learning_decisions", business_id=business_id).insert({
                "candidate_id": identity, "state": decision_state, "feedback": note,
                "decided_by": principal_id, "created_at": STAMPS[0], "updated_at": STAMPS[0]})
            uow.repository("learning_history", business_id=business_id).insert({
                "id": new_id(), "candidate_id": identity, "event": "learnings_review",
                "actor_id": principal_id, "before_json": base, "after_json": decided,
                "created_at": STAMPS[0]})
            candidates = uow.repository("learning_candidates", business_id=business_id)
            candidates.update(identity, {}, expected_revision=1, force_revision=True)

        snapshot = decided
        for index, (event, work_state, agent_note, outcome) in enumerate(WORK.get(key, ()), start=1):
            with database.transaction() as uow:
                candidates = uow.repository("learning_candidates", business_id=business_id)
                row = candidates.get(identity)
                candidates.update(identity, {"work_state": work_state, "agent_note": agent_note,
                    "work_outcome_json": outcome}, expected_revision=row["revision"])
                revision = row["revision"] + 1
                after = {**snapshot, "revision": revision, "state": work_state,
                         "agent_note": agent_note, **({"outcome": outcome} if outcome else {})}
                uow.repository("learning_history", business_id=business_id).insert({
                    "id": new_id(), "candidate_id": identity, "event": event,
                    "actor_id": principal_id, "before_json": snapshot, "after_json": after,
                    "created_at": STAMPS[index]})
                snapshot = after
    return identities