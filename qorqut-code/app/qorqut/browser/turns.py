"""Durable exclusive browser turns for native helpers and delegated workers.

The queue owns access to a configured browser, not the Chrome process.  A turn
is deliberately longer-lived than one command: a worker keeps it while doing
one browser task and releases it only when it completes, waits, is cancelled,
or needs human input.  Every write rechecks the assignment generation so an
old worker cannot reacquire or use a browser after it has been fenced.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

from qorqut_operations.contracts import DomainError, new_id, utc_now
from qorqut_operations.global_agents import global_profile


_BROWSER_KEY = re.compile(r"^[A-Za-z0-9:_-]{1,200}$")
_REQUESTER_KINDS = frozenset({"assignment", "agent", "owner"})
_LIVE = frozenset({"queued", "owning"})


def _now(value=None):
    value = value or utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DomainError("validation_error", "Browser turn time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value, name, limit=500):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DomainError("validation_error", f"{name} must be non-empty text of at most {limit} characters")
    return value.strip()


def _json(value, name, limit=32_000):
    if not isinstance(value, dict):
        raise DomainError("validation_error", f"{name} must be an object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise DomainError("validation_error", f"{name} is not valid JSON") from exc
    if len(encoded.encode()) > limit:
        raise DomainError("validation_error", f"{name} exceeds its size limit")
    return json.loads(encoded)


def _decoded_object(value, name):
    """Accept either a repository-decoded JSON object or its storage form."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_state", f"{name} is not valid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise DomainError("invalid_state", f"{name} must be a JSON object")


class BrowserTurnQueue:
    """Storage-backed browser ownership with bounded leases and promotion."""

    def __init__(self, *, clock=utc_now, lease_seconds=300, delegation=None):
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        self.clock, self.lease_seconds = clock, lease_seconds
        # The assembly's OperationalDelegation store; a default one serves a queue built without it.
        self.delegation = delegation

    @staticmethod
    def _key(browser_key):
        if not isinstance(browser_key, str) or not _BROWSER_KEY.fullmatch(browser_key):
            raise DomainError("validation_error", "browser_key is invalid")
        return browser_key

    @staticmethod
    def _assignment(uow, assignment_id, parent_principal_id, generation):
        row = uow.connection.execute("SELECT * FROM operational_assignments WHERE id=?", (assignment_id,)).fetchone()
        if row is None or row["parent_principal_id"] != parent_principal_id:
            raise DomainError("not_found", "Assignment does not exist")
        if row["state"] != "running" or row["execution_generation"] != generation:
            raise DomainError("stale_worker", "Browser turn belongs to a replaced assignment generation")
        return row

    @staticmethod
    def _timestamp(value):
        return _now(value)

    def _expire(self, uow, browser_key, now):
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        rows = uow.connection.execute(
            "SELECT * FROM browser_turns WHERE browser_key=? AND state='owning' AND lease_until<=?",
            (browser_key, now_text)).fetchall()
        for row in rows:
            # Native configured browsers are lease-free now; their old turns fence nothing.
            legacy_native = row["requester_kind"] == "agent" and row["browser_key"].startswith("configured:")
            # An expired lease is not enough to hand Chrome to another worker:
            # an old command may still be executing. Keep this turn blocked
            # until its durable action is explicitly reconciled.
            unsettled = uow.connection.execute(
                "SELECT 1 FROM browser_turn_actions WHERE turn_id=? AND action_id=? "
                "AND state IN ('pending','running','uncertain') LIMIT 1",
                (row["id"], row["command_id"])).fetchone() if row["command_id"] else None
            if unsettled is not None and not legacy_native:
                continue
            if row["command_id"] and row["command_until"] and _now(row["command_until"]) > now and not legacy_native:
                continue
            uow.connection.execute(
                "UPDATE browser_turns SET state='released',lease_id=NULL,lease_until=NULL,command_id=NULL,command_until=NULL,"
                "reason=?,released_at=?,updated_at=?,revision=revision+1 WHERE id=? AND state='owning'",
                ("browser turn lease expired; worker was fenced", now_text, now_text, row["id"]))

    def _promote(self, uow, browser_key, now):
        now_text = now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        current = uow.connection.execute(
            "SELECT * FROM browser_turns WHERE browser_key=? AND state='owning' LIMIT 1", (browser_key,)).fetchone()
        if current is not None:
            return current
        while True:
            candidate = uow.connection.execute(
                "SELECT turns.* FROM browser_turns turns "
                "LEFT JOIN operational_assignments assignments ON assignments.id=turns.assignment_id "
                "WHERE turns.browser_key=? AND turns.state='queued' "
                "ORDER BY CASE WHEN turns.requester_kind='assignment' "
                "AND assignments.state='queued' "
                "AND json_extract(assignments.wake_condition_json,'$.kind')='browser_turn_available' "
                "AND json_extract(assignments.wake_condition_json,'$.turn_id')=turns.id "
                "AND assignments.execution_generation=turns.execution_generation THEN 0 ELSE 1 END, "
                "turns.created_at,turns.id LIMIT 1", (browser_key,)).fetchone()
            if candidate is None:
                return None
            if candidate["requester_kind"] == "assignment":
                assignment = uow.connection.execute(
                    "SELECT * FROM operational_assignments WHERE id=?",
                    (candidate["assignment_id"],)).fetchone()
                wake = json.loads(assignment["wake_condition_json"] or "{}") if assignment else {}
                reserved = (assignment is not None and assignment["state"] == "queued"
                            and wake.get("kind") == "browser_turn_available"
                            and wake.get("turn_id") == candidate["id"]
                            and assignment["execution_generation"] == candidate["execution_generation"])
                valid = (assignment is not None and (assignment["state"] in {"running", "waiting"} or reserved)
                         and assignment["execution_generation"] == candidate["execution_generation"]
                         and assignment["parent_principal_id"] == candidate["parent_principal_id"])
                if not valid:
                    uow.connection.execute(
                        "UPDATE browser_turns SET state='blocked',reason=?,updated_at=?,released_at=?,revision=revision+1 "
                        "WHERE id=? AND state='queued'",
                        ("assignment is paused, cancelled, revoked or stale", now_text, now_text, candidate["id"]))
                    continue
                if assignment["state"] == "waiting" and wake.get("kind") == "browser_turn" \
                        and wake.get("turn_id") == candidate["id"]:
                    from qorqut.agents.operational_delegation import OperationalDelegation
                    delegation = self.delegation or OperationalDelegation()
                    delegation._wake_browser_turn(uow, assignment, now_text, turn_id=candidate["id"])
                    return None
                if reserved:
                    # The dispatcher has not claimed the woken assignment yet;
                    # keep this FIFO row reserved and prevent a later request
                    # from taking the browser in the gap.
                    return None
            elif candidate["requester_kind"] == "agent" \
                    and global_profile(uow.connection, candidate["parent_principal_id"]) is None:
                # Only a current active Global agent may newly own a browser. The canonical parent identity decides,
                # never the requester label; the row is blocked so the next entry can advance.
                uow.connection.execute(
                    "UPDATE browser_turns SET state='blocked',reason=?,updated_at=?,released_at=?,revision=revision+1 "
                    "WHERE id=? AND state='queued'",
                    ("requesting agent is not an active Global agent", now_text, now_text, candidate["id"]))
                continue
            lease_id = new_id()
            until = (now + timedelta(seconds=self.lease_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            updated = uow.connection.execute(
                "UPDATE browser_turns SET state='owning',lease_id=?,lease_until=?,reason=NULL,updated_at=?,revision=revision+1 "
                "WHERE id=? AND state='queued'", (lease_id, until, now_text, candidate["id"]))
            if updated.rowcount == 1:
                return uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (candidate["id"],)).fetchone()

    def _compact(self, uow, row):
        checkpoint = row["checkpoint_json"]
        if isinstance(checkpoint, str):
            checkpoint = json.loads(checkpoint)
        pending = row["pending_outcome_json"]
        if isinstance(pending, str):
            pending = json.loads(pending)
        position = None
        if row["state"] == "queued":
            position = uow.connection.execute(
                "SELECT count(*) FROM browser_turns WHERE browser_key=? AND state='queued' "
                "AND (created_at<? OR (created_at=? AND id<=?))",
                (row["browser_key"], row["created_at"], row["created_at"], row["id"])).fetchone()[0]
        return {"id": row["id"], "browser_key": row["browser_key"], "business_id": row["business_id"],
                "assignment_id": row["assignment_id"], "execution_generation": row["execution_generation"],
                "requester_kind": row["requester_kind"], "state": row["state"],
                "lease_id": row["lease_id"], "lease_until": row["lease_until"],
                "checkpoint": checkpoint, "last_action": row["last_action"],
                "pending_outcome": pending,
                "command_id": row["command_id"], "command_until": row["command_until"],
                "reason": row["reason"], "position": position, "revision": row["revision"],
                "updated_at": row["updated_at"]}

    def acquire(self, uow, *, browser_key, business_id, parent_principal_id,
                requester_kind="assignment", requester_id=None, assignment_id=None,
                generation=None, now=None):
        uow.require_write()
        browser_key = self._key(browser_key)
        parent_principal_id = _text(parent_principal_id, "parent_principal_id", 200)
        if requester_kind not in _REQUESTER_KINDS:
            raise DomainError("validation_error", "requester_kind is unsupported")
        requester_id = _text(requester_id or (assignment_id if requester_kind == "assignment" else parent_principal_id),
                             "requester_id", 200)
        if requester_kind == "assignment":
            if not assignment_id or type(generation) is not int or generation < 1:
                raise DomainError("validation_error", "Assignment browser turns require assignment_id and generation")
            self._assignment(uow, assignment_id, parent_principal_id, generation)
        now_dt = _now(now or self.clock())
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        self._expire(uow, browser_key, now_dt)
        existing = uow.connection.execute(
            "SELECT * FROM browser_turns WHERE browser_key=? AND requester_kind=? AND requester_id=? "
            "AND COALESCE(assignment_id,'')=COALESCE(?,'') AND COALESCE(execution_generation,0)=COALESCE(?,0) "
            "AND state IN ('queued','owning') ORDER BY created_at DESC LIMIT 1",
            (browser_key, requester_kind, requester_id, assignment_id, generation)).fetchone()
        if existing is None:
            row = uow.repository("browser_turns").insert({
                "id": new_id(), "browser_key": browser_key, "business_id": business_id,
                "assignment_id": assignment_id, "parent_principal_id": parent_principal_id,
                "execution_generation": generation, "requester_kind": requester_kind,
                "requester_id": requester_id, "state": "queued", "checkpoint_json": {},
                "last_action": None, "pending_outcome_json": None, "reason": None,
                "created_at": now_text, "updated_at": now_text, "released_at": None,
            })
        else:
            row = existing
        promoted = self._promote(uow, browser_key, now_dt)
        # Report the row as stored now: promotion may have just blocked it (an agent that is not an active Global agent).
        selected = promoted if promoted and promoted["id"] == row["id"] else \
            uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (row["id"],)).fetchone()
        return self._compact(uow, selected)

    def wait(self, uow, turn_id, *, parent_principal_id, requester_id, generation,
             lease_id, checkpoint, next_action, now=None):
        """Park a running assignment while retaining its queued browser row."""
        uow.require_write()
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn is not owned by this requester")
        if row["state"] != "queued":
            raise DomainError("browser_busy", "Browser turn is not waiting behind another owner", retryable=True)
        if row["assignment_id"] is None or row["execution_generation"] != generation:
            raise DomainError("stale_worker", "Browser turn belongs to a replaced assignment generation")
        assignment = uow.connection.execute("SELECT * FROM operational_assignments WHERE id=?", (row["assignment_id"],)).fetchone()
        if assignment is None or assignment["state"] != "running" or assignment["execution_generation"] != generation:
            raise DomainError("stale_worker", "Browser turn assignment is no longer current")
        from qorqut.agents.operational_delegation import OperationalDelegation
        delegation = self.delegation or OperationalDelegation()
        saved_checkpoint = _decoded_object(assignment["checkpoint_json"], "assignment.checkpoint_json")
        updated = delegation.checkpoint(uow, row["assignment_id"], actor_principal_id=parent_principal_id,
            generation=generation, lease_id=assignment["lease_id"], expected_revision=assignment["revision"],
            checkpoint=saved_checkpoint, next_step=next_action, state="waiting",
            wake_condition={"kind": "browser_turn", "turn_id": turn_id, "browser_key": row["browser_key"]}, now=now)
        return self._compact(uow, uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()), updated

    def rebind_assignment_generation(self, uow, assignment_id, old_generation, new_generation):
        uow.require_write()
        uow.connection.execute("UPDATE browser_turns SET execution_generation=?,updated_at=?,revision=revision+1 "
            "WHERE assignment_id=? AND execution_generation=? AND state='queued'",
            (new_generation, self.clock(), assignment_id, old_generation))

    def begin_action(self, uow, turn_id, *, parent_principal_id, requester_id, generation, lease_id,
                     action_id, command, now=None):
        turn = self._owned(uow, turn_id, parent_principal_id=parent_principal_id, requester_id=requester_id,
                           generation=generation, lease_id=lease_id, now=now)
        if not isinstance(action_id, str) or not action_id or len(action_id) > 200:
            raise DomainError("validation_error", "action_id is required")
        if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
            raise DomainError("validation_error", "command must be a non-empty list of strings")
        encoded = json.dumps(command, sort_keys=True, separators=(",", ":"))
        existing = uow.connection.execute("SELECT * FROM browser_turn_actions WHERE action_id=?", (action_id,)).fetchone()
        if existing is not None:
            if existing["turn_id"] != turn_id or existing["command_json"] != encoded:
                raise DomainError("idempotency_conflict", "Browser action identity was already used differently")
            return dict(existing), False
        # A recorded intent above is only replayed; a new one is admitted only while the owning agent is still a
        # current active Global agent. Reconciliation of earlier intents never comes through here.
        if turn["requester_kind"] == "agent" and global_profile(uow.connection, turn["parent_principal_id"]) is None:
            raise DomainError("forbidden", "New browser actions are limited to active Global agents")
        now_dt = _now(now or self.clock())
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        command_until = (_now(now or self.clock()) + timedelta(seconds=60)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        fenced = uow.connection.execute("SELECT command_id FROM browser_turns WHERE id=? AND state='owning'",
                                         (turn_id,)).fetchone()
        if fenced is None or fenced["command_id"] is not None:
            raise DomainError("browser_busy", "Another browser command is already in flight", retryable=True)
        row = uow.repository("browser_turn_actions").insert({
            "id": new_id(), "turn_id": turn_id, "action_id": action_id,
            "command_json": command, "state": "pending", "outcome_json": None,
            "error": None, "created_at": now_text, "started_at": None,
            "updated_at": now_text, "finished_at": None,
        })
        uow.connection.execute("UPDATE browser_turns SET command_id=?,command_until=?,updated_at=?,revision=revision+1 "
                               "WHERE id=? AND state='owning' AND command_id IS NULL",
                               (action_id, command_until, now_text, turn_id))
        return row, True

    def _action_owned(self, uow, turn_id, *, parent_principal_id, requester_id, generation,
                      lease_id, action_id, now=None):
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row["parent_principal_id"] != parent_principal_id or row["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn is not owned by this requester")
        if row["state"] != "owning" or row["execution_generation"] != generation or row["lease_id"] != lease_id:
            raise DomainError("stale_worker", "Browser turn lease is no longer current")
        now_dt = _now(now or self.clock())
        if row["command_id"] != action_id or not row["command_until"] or _now(row["command_until"]) <= now_dt:
            raise DomainError("lease_expired", "Browser command fence expired", retryable=True)
        if row["assignment_id"] is not None:
            assignment = uow.connection.execute("SELECT * FROM operational_assignments WHERE id=?",
                                                 (row["assignment_id"],)).fetchone()
            if assignment is None or assignment["parent_principal_id"] != parent_principal_id \
                    or assignment["execution_generation"] != generation:
                raise DomainError("stale_worker", "Browser turn belongs to a replaced assignment generation")
        return row

    def settle_action(self, uow, turn_id, *, parent_principal_id, requester_id, generation, lease_id,
                      action_id, state, outcome, error=None, now=None):
        if state not in {"succeeded", "failed", "uncertain", "reconciled"}:
            raise DomainError("validation_error", "Invalid browser action outcome")
        self._action_owned(uow, turn_id, parent_principal_id=parent_principal_id, requester_id=requester_id,
                           generation=generation, lease_id=lease_id, action_id=action_id, now=now)
        row = uow.connection.execute("SELECT * FROM browser_turn_actions WHERE action_id=? AND turn_id=?",
                                     (action_id, turn_id)).fetchone()
        if row is None:
            raise DomainError("not_found", "Browser action intent does not exist")
        now_dt = _now(now or self.clock())
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        uow.connection.execute("UPDATE browser_turn_actions SET state=?,outcome_json=?,error=?,started_at=COALESCE(started_at,?),"
                               "updated_at=?,finished_at=?,revision=revision+1 WHERE id=?",
                               (state, json.dumps(outcome, sort_keys=True, separators=(",", ":")), error,
                                now_text, now_text, now_text, row["id"]))
        turn = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        # Uncertainty is a durable blocked state. Clearing this fence before a
        # recovery worker has reconciled the action could hand Chrome to a new
        # worker while the old command is still able to act.
        if state != "uncertain":
            uow.connection.execute("UPDATE browser_turns SET command_id=NULL,command_until=NULL,updated_at=?,revision=revision+1 "
                                   "WHERE id=? AND command_id=?",
                                   (now_text, turn_id, action_id))
        assignment = uow.connection.execute("SELECT state FROM operational_assignments WHERE id=?",
                                             (turn["assignment_id"],)).fetchone()
        if state != "uncertain" and ((assignment is not None and assignment["state"] != "running")
                or turn["lease_until"] is None or _now(turn["lease_until"]) <= now_dt):
            uow.connection.execute("UPDATE browser_turns SET state='released',lease_id=NULL,lease_until=NULL,"
                                   "command_id=NULL,command_until=NULL,reason=?,"
                                   "released_at=?,updated_at=?,revision=revision+1 WHERE id=? AND state='owning'",
                                   ("command settled after lease expiry", now_text, now_text, turn_id))
            self._promote(uow, turn["browser_key"], now_dt)
        return dict(uow.connection.execute("SELECT * FROM browser_turn_actions WHERE id=?", (row["id"],)).fetchone())

    def recover_rejected_action(self, uow, turn_id, *, parent_principal_id, requester_id,
                                generation, lease_id, action_id, now=None):
        """Recover only legacy intents proven rejected by pre-execution validation.

        Never use caller-supplied outcome claims to unlock possibly executed work.
        Expired leases retain identity; a replaced lease/generation cannot recover.
        """
        uow.require_write()
        turn = uow.connection.execute('SELECT * FROM browser_turns WHERE id=?', (turn_id,)).fetchone()
        if (turn is None or turn['parent_principal_id'] != parent_principal_id
                or turn['requester_id'] != requester_id or turn['state'] != 'owning'
                or turn['lease_id'] != lease_id or turn['execution_generation'] != generation):
            raise DomainError('forbidden', 'Rejected action recovery requires the exact current turn owner and lease')
        action = uow.connection.execute('SELECT * FROM browser_turn_actions WHERE turn_id=? AND action_id=?',
                                        (turn_id, action_id)).fetchone()
        if action is None or action['state'] != 'uncertain' or turn['command_id'] != action_id:
            return None
        from qorqut.bootstrap.browser import BrowserError, validate_arguments
        try:
            validate_arguments(json.loads(action['command_json']))
        except BrowserError as exc:
            if action['error'] != str(exc):
                return None
        else:
            return None
        timestamp = _now(now or self.clock()).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        outcome = json.dumps({'executed': False, 'recovery': 'verified pre-execution validation rejection'})
        uow.connection.execute("UPDATE browser_turn_actions SET state='failed',outcome_json=?,finished_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                               (outcome, timestamp, timestamp, action['id']))
        uow.connection.execute('UPDATE browser_turns SET command_id=NULL,command_until=NULL,updated_at=?,revision=revision+1 WHERE id=?',
                               (timestamp, turn_id))
        return dict(uow.connection.execute('SELECT * FROM browser_turn_actions WHERE id=?', (action['id'],)).fetchone())

    def reconcile_action(self, uow, turn_id, *, parent_principal_id, requester_id, generation, lease_id,
                         action_id, outcome, now=None):
        """Reconcile from the current generation, even after the old fence expired."""
        uow.require_write()
        action = uow.connection.execute(
            "SELECT * FROM browser_turn_actions WHERE turn_id=? AND action_id=?",
            (turn_id, action_id)).fetchone()
        if action is None:
            raise DomainError("not_found", "Browser action intent does not exist")
        turn = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if turn is None or turn["parent_principal_id"] != parent_principal_id \
                or turn["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn is not owned by this requester")
        if turn['requester_kind'] == 'agent':
            recovered = self.recover_rejected_action(uow, turn_id,
                parent_principal_id=parent_principal_id, requester_id=requester_id,
                generation=generation, lease_id=lease_id, action_id=action_id, now=now)
            if recovered is not None:
                return recovered
            raise DomainError('browser_reconcile', 'Native action may have executed; operator reconciliation is required')
        assignment = uow.connection.execute(
            "SELECT * FROM operational_assignments WHERE id=?", (requester_id,)).fetchone()
        if assignment is None or assignment["parent_principal_id"] != parent_principal_id \
                or assignment["state"] != "running" or assignment["execution_generation"] != generation:
            raise DomainError("stale_worker", "Recovery must come from the current assignment generation")
        if action["state"] == "reconciled":
            return dict(action)
        if action["state"] not in {"pending", "running", "uncertain"}:
            raise DomainError("invalid_transition", "Only unsettled browser actions can be reconciled")
        now_dt = _now(now or self.clock())
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        outcome = _json(outcome, "outcome", 32_000)
        uow.connection.execute(
            "UPDATE browser_turn_actions SET state='reconciled',outcome_json=?,error=NULL,"
            "started_at=COALESCE(started_at,?),updated_at=?,finished_at=?,revision=revision+1 WHERE id=?",
            (json.dumps(outcome, sort_keys=True, separators=(",", ":")), now_text, now_text, now_text, action["id"]))
        uow.connection.execute(
            "UPDATE browser_turns SET command_id=NULL,command_until=NULL,updated_at=?,revision=revision+1 "
            "WHERE id=? AND command_id=?", (now_text, turn_id, action_id))
        current = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if current["state"] == "owning":
            uow.connection.execute(
                "UPDATE browser_turns SET state='released',lease_id=NULL,lease_until=NULL,reason=?,"
                "released_at=?,updated_at=?,revision=revision+1 WHERE id=? AND state='owning'",
                ("uncertain browser action reconciled", now_text, now_text, turn_id))
        self._promote(uow, current["browser_key"], now_dt)
        return dict(uow.connection.execute("SELECT * FROM browser_turn_actions WHERE id=?", (action["id"],)).fetchone())

    def status(self, uow, turn_id, *, parent_principal_id, requester_id=None, now=None):
        uow.require_write()
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row["parent_principal_id"] != parent_principal_id:
            raise DomainError("not_found", "Browser turn does not exist")
        if requester_id is not None and row["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn belongs to another requester")
        now_dt = _now(now or self.clock())
        self._expire(uow, row["browser_key"], now_dt)
        self._promote(uow, row["browser_key"], now_dt)
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        return self._compact(uow, row)

    def _owned(self, uow, turn_id, *, parent_principal_id, requester_id, generation=None, lease_id=None, now=None):
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row["parent_principal_id"] != parent_principal_id or row["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn is not owned by this requester")
        if row["state"] != "owning":
            raise DomainError("browser_wait", "Browser turn is waiting for its exclusive turn", retryable=True)
        if generation is not None and row["execution_generation"] != generation:
            raise DomainError("stale_worker", "Browser turn belongs to a replaced assignment generation")
        if lease_id is not None and row["lease_id"] != lease_id:
            raise DomainError("stale_worker", "Browser turn lease is no longer current")
        if _now(row["lease_until"]) <= _now(now or self.clock()):
            raise DomainError("lease_expired", "Browser turn lease expired", retryable=True)
        if row["assignment_id"]:
            self._assignment(uow, row["assignment_id"], parent_principal_id, row["execution_generation"])
        return row

    def renew(self, uow, turn_id, *, parent_principal_id, requester_id, lease_id, generation=None,
              lease_seconds=None, now=None):
        uow.require_write()
        row = self._owned(uow, turn_id, parent_principal_id=parent_principal_id, requester_id=requester_id,
                          generation=generation, lease_id=lease_id, now=now)
        seconds = self.lease_seconds if lease_seconds is None else lease_seconds
        if type(seconds) is not int or not 30 <= seconds <= 900:
            raise DomainError("validation_error", "lease_seconds must be between 30 and 900")
        now_dt = _now(now or self.clock())
        until = (now_dt + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        uow.connection.execute("UPDATE browser_turns SET lease_until=?,updated_at=?,revision=revision+1 WHERE id=?",
                               (until, now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"), row["id"]))
        return self._compact(uow, uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (row["id"],)).fetchone())

    def checkpoint(self, uow, turn_id, *, parent_principal_id, requester_id, lease_id,
                   generation, checkpoint, last_action=None, pending_outcome=None, next_action=None, now=None):
        uow.require_write()
        row = self._owned(uow, turn_id, parent_principal_id=parent_principal_id, requester_id=requester_id,
                          generation=generation, lease_id=lease_id, now=now)
        checkpoint = _json(checkpoint, "checkpoint")
        pending_outcome = None if pending_outcome is None else _json(pending_outcome, "pending_outcome", 16_000)
        last_action = _text(last_action, "last_action", 200)
        next_action = _text(next_action, "next_action", 2000)
        now_text = _now(now or self.clock()).isoformat(timespec="microseconds").replace("+00:00", "Z")
        values = {"checkpoint_json": checkpoint, "last_action": last_action,
                  "pending_outcome_json": pending_outcome, "updated_at": now_text}
        updated = uow.repository("browser_turns").update(row["id"], values, expected_revision=row["revision"])
        if row["assignment_id"]:
            assignment = uow.connection.execute("SELECT * FROM operational_assignments WHERE id=?", (row["assignment_id"],)).fetchone()
            merged = _decoded_object(assignment["checkpoint_json"], "assignment.checkpoint_json")
            merged["browser"] = {"turn_id": row["id"], "browser_key": row["browser_key"],
                                  "generation": generation, "checkpoint": checkpoint,
                                  "last_action": last_action, "pending_outcome": pending_outcome,
                                  "next_action": next_action}
            changes = {"checkpoint_json": merged}
            if next_action is not None:
                changes["next_step"] = next_action
            uow.repository("operational_assignments").update(row["assignment_id"], changes,
                                                               expected_revision=assignment["revision"])
        return self._compact(uow, updated)

    def release(self, uow, turn_id, *, parent_principal_id, requester_id, reason="released", generation=None,
                lease_id=None, now=None):
        uow.require_write()
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or row["parent_principal_id"] != parent_principal_id or row["requester_id"] != requester_id:
            raise DomainError("forbidden", "Browser turn is not owned by this requester")
        if generation is not None and row["execution_generation"] != generation:
            raise DomainError("stale_worker", "Browser turn belongs to a replaced assignment generation")
        if row['state'] == 'owning' and row['command_id'] and lease_id is not None:
            self.recover_rejected_action(uow, turn_id, parent_principal_id=parent_principal_id,
                requester_id=requester_id, generation=generation, lease_id=lease_id,
                action_id=row['command_id'], now=now)
        now_dt = _now(now or self.clock())
        self._expire(uow, row["browser_key"], now_dt)
        row = uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (turn_id,)).fetchone()
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        if lease_id is not None and row["state"] == "owning" and row["lease_id"] != lease_id:
            raise DomainError("stale_worker", "Browser turn lease is no longer current")
        unsettled = uow.connection.execute(
            "SELECT 1 FROM browser_turn_actions WHERE turn_id=? AND action_id=? "
            "AND state IN ('pending','running','uncertain') LIMIT 1",
            (row["id"], row["command_id"])).fetchone() if row["command_id"] else None
        if row["state"] == "owning" and unsettled is not None:
            raise DomainError("browser_busy", "Cannot release a browser turn while a command is in flight", retryable=True)
        if row["state"] in _LIVE:
            uow.connection.execute("UPDATE browser_turns SET state='released',lease_id=NULL,lease_until=NULL,"
                                   "command_id=NULL,command_until=NULL,"
                                   "reason=?,released_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                                   (_text(reason, "reason"), now_text, now_text, row["id"]))
        self._promote(uow, row["browser_key"], now_dt)
        return self._compact(uow, uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (row["id"],)).fetchone())

    def release_assignment(self, uow, assignment_id, *, generation=None, reason="assignment released", now=None):
        uow.require_write()
        now_dt = _now(now or self.clock())
        now_text = now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
        filters = "assignment_id=? AND state IN ('queued','owning')"
        args = [assignment_id]
        if generation is not None:
            filters += " AND execution_generation=?"
            args.append(generation)
        rows = uow.connection.execute("SELECT * FROM browser_turns WHERE " + filters, args).fetchall()
        for row in rows:
            unsettled = uow.connection.execute(
                "SELECT 1 FROM browser_turn_actions WHERE turn_id=? AND action_id=? "
                "AND state IN ('pending','running','uncertain') LIMIT 1",
                (row["id"], row["command_id"])).fetchone() if row["command_id"] else None
            if row["state"] == "owning" and unsettled is not None:
                continue
            uow.connection.execute("UPDATE browser_turns SET state='released',lease_id=NULL,lease_until=NULL,"
                                   "command_id=NULL,command_until=NULL,"
                                   "reason=?,released_at=?,updated_at=?,revision=revision+1 WHERE id=? AND state IN ('queued','owning')",
                                   (_text(reason, "reason"), now_text, now_text, row["id"]))
        for key in {row["browser_key"] for row in rows}:
            self._promote(uow, key, now_dt)
        return len(rows)

    def recover(self, uow, *, now=None):
        uow.require_write()
        now_dt = _now(now or self.clock())
        keys = [row["browser_key"] for row in uow.connection.execute(
            "SELECT DISTINCT browser_key FROM browser_turns WHERE state='owning' AND lease_until<=?",
            (now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),))]
        for key in keys:
            self._expire(uow, key, now_dt)
            self._promote(uow, key, now_dt)
        return len(keys)
