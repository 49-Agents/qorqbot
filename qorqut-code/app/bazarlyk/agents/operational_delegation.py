"""Durable, generation-fenced orchestration state for bounded helper work.

This module deliberately stores assignment metadata and recovery receipts; it
does not launch arbitrary commands or proxy arbitrary operations. Runtime and
application adapters must still enforce the saved authority on every call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from qorqut_operations.contracts import (ChangeSet, DomainError, ExpectedTarget, PreparedChange, ResourceRef,
                                         canonical_bytes, new_id, plain, utc_now)

from .worker_models import LUNA_MODEL, LEGACY_LUNA_MODEL, WORKER_MODEL, worker_effort

_OPERATION = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_PARENT_ONLY_OPERATIONS = frozenset({
    "agent_work_update", "agents_create", "agents_update", "agents_deactivate",
    "permissions_set", "plugins_enable", "plugins_disable", "signoffs_approve",
    "signoffs_reject", "operations_batch", "owner_session_create",
})
WORKER_OPERATIONS = frozenset({"delegated_assignment_get", "delegated_assignment_checkpoint",
    "delegated_assignment_events_claim", "delegated_assignment_event_settle", "browser_turn"})
_STATES = frozenset({"queued", "running", "waiting", "blocked", "paused", "completed", "cancelled"})
_WORKER_STATES = frozenset({"running", "waiting", "blocked", "completed"})
_FINAL_STATES = frozenset({"completed", "cancelled"})


def _canonical_id(value, name):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DomainError("validation_error", f"{name} must be a canonical UUID") from None
    return value


def _json_value(value, name, *, kind, max_bytes):
    if not isinstance(value, kind):
        raise DomainError("validation_error", f"{name} must be a {kind.__name__}")
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("validation_error", f"{name} is not valid JSON: {exc}") from None
    if len(encoded) > max_bytes:
        raise DomainError("validation_error", f"{name} exceeds its size limit")
    return json.loads(encoded)


def _bounded_text(value, name, limit, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DomainError("validation_error", f"{name} must be non-empty text of at most {limit} characters")
    return value.strip()


def _scope(value, business_id):
    value = _json_value(value, "scope", kind=dict, max_bytes=16_000)
    if value.get("kind") not in {"business", "global"}:
        raise DomainError("validation_error", "scope.kind must be business or global")
    if value["kind"] == "business":
        bound = _canonical_id(value.get("business_id"), "scope.business_id")
        if business_id != bound:
            raise DomainError("validation_error", "Business scope and assignment business must match exactly")
    else:
        if business_id is not None or value.get("business_id") is not None:
            raise DomainError("validation_error", "Global scope cannot also select a business")
        allowed = value.get("allowed_business_ids", [])
        if not isinstance(allowed, list) or len(allowed) > 100:
            raise DomainError("validation_error", "Global scope requires a bounded allowed_business_ids list")
        value["allowed_business_ids"] = [_canonical_id(item, "scope.allowed_business_ids") for item in allowed]
        if len(set(value["allowed_business_ids"])) != len(value["allowed_business_ids"]):
            raise DomainError("validation_error", "Global scope business IDs must be unique")
    for boundary in ("record_ids", "source_ids"):
        entries = value.get(boundary, {})
        if not isinstance(entries, dict) or len(entries) > 100:
            raise DomainError("validation_error", f"scope.{boundary} must be a bounded object")
        normalized = {}
        for name, ids in entries.items():
            if not isinstance(name, str) or not _OPERATION.fullmatch(name) or not isinstance(ids, list) or len(ids) > 500:
                raise DomainError("validation_error", f"scope.{boundary} entries must be named bounded ID lists")
            if any(not isinstance(item, str) or not item or len(item) > 200 for item in ids):
                raise DomainError("validation_error", f"scope.{boundary} contains an invalid ID")
            if len(set(ids)) != len(ids):
                raise DomainError("validation_error", f"scope.{boundary} IDs must be unique")
            normalized[name] = ids
        value[boundary] = normalized
    return value


def _operations(value):
    value = _json_value(value, "allowed_operations", kind=list, max_bytes=10_000)
    if not 0 <= len(value) <= 100 or any(not isinstance(item, str) or not _OPERATION.fullmatch(item) for item in value):
        raise DomainError("validation_error", "allowed_operations must contain 0 to 100 registered operation names")
    if len(set(value)) != len(value):
        raise DomainError("validation_error", "allowed_operations must be unique")
    if set(value) & _PARENT_ONLY_OPERATIONS:
        raise DomainError("forbidden", "Assignments cannot grant parent-only or batch operations")
    return sorted(value)


def _links(value):
    value = _json_value(value or {}, "links", kind=dict, max_bytes=16_000)
    allowed = {"tasks", "stories", "documents", "message_threads"}
    if set(value) - allowed:
        raise DomainError("validation_error", "links may contain tasks, stories, documents, and message_threads only")
    result = {}
    for kind in sorted(allowed):
        ids = value.get(kind, [])
        if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(item, str) or not item or len(item) > 200 for item in ids):
            raise DomainError("validation_error", f"links.{kind} must be a bounded list of IDs")
        if len(set(ids)) != len(ids):
            raise DomainError("validation_error", f"links.{kind} IDs must be unique")
        result[kind] = ids
    return result


def _limits(value):
    defaults = {"max_turns": 40, "max_runtime_seconds": 3600, "max_actions": 100, "max_retries": 3}
    value = _json_value(value or defaults, "resource_bounds", kind=dict, max_bytes=2000)
    if set(value) - set(defaults):
        raise DomainError("validation_error", "resource_bounds contains an unsupported field")
    value = {**defaults, **value}
    limits = {"max_turns": (1, 500), "max_runtime_seconds": (30, 86_400),
              "max_actions": (1, 2000), "max_retries": (0, 19)}
    for name, (minimum, maximum) in limits.items():
        amount = value[name]
        if type(amount) is not int or not minimum <= amount <= maximum:
            raise DomainError("validation_error", f"resource_bounds.{name} is outside its supported range")
    return value


def _timestamp(value, name):
    if not isinstance(value, str):
        raise DomainError("validation_error", f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("validation_error", f"{name} must be a UTC timestamp") from None
    if parsed.tzinfo is None:
        raise DomainError("validation_error", f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _accounted_runtime(assignment, now):
    """Return bounded active execution time; waiting and expired lease time are excluded."""
    used = assignment.get("runtime_used_seconds", 0) or 0
    started = assignment.get("execution_started_at")
    if not started:
        return used
    end = _timestamp(now, "now")
    lease_until = assignment.get("lease_until")
    if lease_until:
        end = min(end, _timestamp(lease_until, "lease_until"))
    elapsed = max(0, int((end - _timestamp(started, "execution_started_at")).total_seconds()))
    maximum = assignment.get("resource_bounds_json", {}).get("max_runtime_seconds", 86_400)
    return min(maximum, used + elapsed)


def _failure_backoff_seconds(retry_count):
    return min(300, 5 * (2 ** min(max(retry_count - 1, 0), 6)))


def _turn_usage(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {
            "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}:
        raise DomainError("validation_error", "turn_usage contains unsupported fields")
    result = {}
    for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        amount = value.get(name, 0)
        if type(amount) is not int or not 0 <= amount <= 2**63 - 1:
            raise DomainError("validation_error", f"turn_usage.{name} must be a non-negative integer")
        result[name] = amount
    if result["cached_input_tokens"] > result["input_tokens"]:
        raise DomainError("validation_error", "cached input tokens cannot exceed input tokens")
    return result


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise DomainError("validation_error", "payload_hash must be a lowercase SHA-256 digest")
    return value


def supports_durable_delegation_version(version, role_id="ordubashy"):
    try:
        parts = tuple(int(part) for part in version.split("."))
    except (AttributeError, TypeError, ValueError):
        return False
    return role_id in {"ordubashy", "global-marketing"} and (parts + (0, 0, 0))[:3] >= ((1, 11, 0) if role_id == "global-marketing" else (1, 34, 0))


class OperationalDelegation:
    """Typed state transitions; callers supply already-authenticated principals."""

    def __init__(self, *, clock=utc_now,
                 dispatch_reason="Awaiting a compatible parent dispatcher to claim this assignment"):
        self.clock = clock
        self.dispatch_reason = dispatch_reason

    @staticmethod
    def _release_browser_turns(uow, assignment_id, *, generation=None,
                               reason="assignment released", now=None):
        """Release browser ownership whenever a worker generation is fenced."""
        from bazarlyk.browser.turns import BrowserTurnQueue
        queue = getattr(uow.database, "browser_turns", None) or BrowserTurnQueue()
        queue.release_assignment(uow, assignment_id, generation=generation, reason=reason, now=now)

    def _wake_browser_turn(self, uow, assignment, now, *, turn_id=None):
        """Make a browser-waiting assignment dispatchable without stealing its FIFO row."""
        updated = uow.repository("operational_assignments").update(assignment["id"], {
            "state": "queued", "lease_id": None, "lease_until": None,
            "wake_at": None, "wake_condition_json": {
                "kind": "browser_turn_available",
                "turn_id": turn_id,
            },
            "next_step": "Browser turn available; dispatcher will resume from the saved checkpoint",
            "execution_started_at": None,
            "runtime_used_seconds": assignment["runtime_used_seconds"],
        }, expected_revision=assignment["revision"])
        self.release_event_claims(uow, assignment["id"], now=now)
        self._event(uow, updated, assignment["parent_principal_id"], "browser_turn_available", {})
        return updated

    def _event(self, uow, assignment, actor_id, event_type, payload):
        row = uow.connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM operational_assignment_events WHERE assignment_id=?",
            (assignment["id"],)).fetchone()
        uow.repository("operational_assignment_events").insert({
            "id": new_id(), "assignment_id": assignment["id"], "sequence": row[0],
            "execution_generation": assignment["execution_generation"], "actor_principal_id": actor_id,
            "event_type": event_type, "payload_json": payload,
        })

    def create(self, uow, *, parent_principal_id, business_id, scope, objective, plan,
               allowed_operations, links=None, constraints=None, internal_recipients=None,
               completion_evidence=None, escalation_rules=None, resource_bounds=None,
               max_attempts=4, assignment_id=None):
        uow.require_write()
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        if business_id is not None:
            business_id = _canonical_id(business_id, "business_id")
        parent = uow.repository("principals").get(parent_principal_id)
        if parent is None or not parent["active"] or parent["kind"] != "agent":
            raise DomainError("forbidden", "Only an active registered agent can own a delegated assignment")
        scope = _scope(scope, business_id)
        objective = _bounded_text(objective, "objective", 10_000)
        plan = _json_value(plan, "plan", kind=dict, max_bytes=50_000)
        if not plan:
            raise DomainError("validation_error", "An approved plan must not be empty")
        allowed_operations = _operations(allowed_operations)
        links = _links(links)
        constraints = _json_value(constraints or {}, "constraints", kind=dict, max_bytes=20_000)
        recipients = _json_value(internal_recipients or [], "internal_recipients", kind=list, max_bytes=10_000)
        recipients = [_canonical_id(item, "internal_recipients") for item in recipients]
        if len(set(recipients)) != len(recipients):
            raise DomainError("validation_error", "internal_recipients must be unique")
        evidence = _json_value(completion_evidence or [], "completion_evidence", kind=list, max_bytes=10_000)
        rules = _json_value(escalation_rules or {}, "escalation_rules", kind=dict, max_bytes=10_000)
        bounds = _limits(resource_bounds)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise DomainError("validation_error", "max_attempts must be between 1 and 20")
        now = self.clock()
        assignment_id = _canonical_id(assignment_id, "assignment_id") if assignment_id is not None else new_id()
        created = uow.repository("operational_assignments").insert({
            "id": assignment_id, "parent_principal_id": parent_principal_id, "business_id": business_id,
            "scope_json": scope, "objective": objective, "plan_json": plan, "plan_revision": 1,
            "links_json": links, "allowed_operations_json": allowed_operations,
            "constraints_json": constraints, "internal_recipients_json": recipients,
            "completion_evidence_json": evidence, "escalation_rules_json": rules,
            "resource_bounds_json": bounds, "selected_model": WORKER_MODEL,
            "reasoning_effort": worker_effort(allowed_operations), "state": "queued",
            "dispatch_reason": self.dispatch_reason,
            "next_step": ("Awaiting compatible durable worker dispatch" if self.dispatch_reason else None),
            "checkpoint_json": {},
            "wake_condition_json": {}, "execution_generation": 0, "attempt_count": 0, "retry_count": 0,
            "max_attempts": max_attempts, "created_at": now, "updated_at": now,
        })
        self._event(uow, created, parent_principal_id, "created", {"plan_revision": 1, "state": "queued"})
        return created

    def get(self, uow, assignment_id, *, parent_principal_id=None):
        assignment_id = _canonical_id(assignment_id, "assignment_id")
        record = uow.repository("operational_assignments").get(assignment_id)
        if record is None or (parent_principal_id is not None and record["parent_principal_id"] != parent_principal_id):
            raise DomainError("not_found", "Assignment does not exist")
        return record

    def list_for_parent(self, uow, parent_principal_id, *, limit=50):
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        return uow.repository("operational_assignments").list(
            filters={"parent_principal_id": parent_principal_id}, limit=limit,
            order_by=("updated_at", "id"), descending=True)

    @staticmethod
    def routed_assignments(uow, parent_principal_id, event):
        """Return only exact, unambiguous assignment bindings for a parent event.

        Business scope alone is deliberately not a routing rule. Explicit
        worker attribution wins; otherwise a message thread or a linked
        canonical task/Story/document must match exactly.
        """
        source_type, source_id = event["source_type"], event["source_id"]
        message = None
        if source_type == "agent_messages":
            message = uow.connection.execute("SELECT * FROM agent_messages WHERE id=?", (source_id,)).fetchone()
            if (message is None or message["current_recipient_id"] != parent_principal_id
                    or (message["current_sender_id"] == parent_principal_id
                        and message["sender_id"] != message["recipient_id"])):  # collapsed history
                return []
        base = ("parent_principal_id=? AND state IN ('queued','running','waiting') AND "
                "(business_id IS ? OR (business_id IS NULL AND "
                "(? IS NULL OR EXISTS (SELECT 1 FROM json_each(scope_json,'$.allowed_business_ids') "
                "WHERE value=?))))")
        business = event["business_id"]
        parameters = (parent_principal_id, business, business, business)
        if message is not None and message["delegation_assignment_id"]:
            attributed = uow.connection.execute(
                "SELECT id FROM operational_assignments WHERE id=? AND " + base,
                (message["delegation_assignment_id"], *parameters)).fetchone()
            if attributed:
                return [uow.repository("operational_assignments").get(attributed["id"])]
        if message is not None:
            match_sql = ("EXISTS (SELECT 1 FROM json_each(links_json,'$.message_threads') "
                         "WHERE value=?)")
            match_parameter = message["thread_id"]
        else:
            key = {"tasks": "tasks", "stories": "stories", "documents": "documents"}.get(source_type)
            if key is None:
                return []
            match_sql = ("(EXISTS (SELECT 1 FROM json_each(links_json,'$." + key + "') WHERE value=?) "
                         "OR EXISTS (SELECT 1 FROM json_each(scope_json,'$.source_ids." + key + "') WHERE value=?))")
            match_parameter = source_id
        rows = uow.connection.execute("SELECT id FROM operational_assignments WHERE " + base + " AND " + match_sql,
            (*parameters, match_parameter, *((match_parameter,) if message is None else ()))).fetchall()
        return [uow.repository("operational_assignments").get(row["id"]) for row in rows]

    def claim_events(self, uow, assignment_id, *, actor_principal_id, generation,
                     lease_id, limit=20, readable=None, now=None):
        """Atomically claim exact, visible parent-delivery events for a worker."""
        uow.require_write()
        assignment = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("validation_error", "event limit must be between 1 and 100")
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if assignment["parent_principal_id"] != actor_principal_id:
            raise DomainError("forbidden", "Worker event claims must belong to the parent assignment")
        now_text = now or self.clock()
        cursor, result = 0, []
        while len(result) < limit:
            rows = uow.connection.execute(
                "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND e.id>? "
                "ORDER BY e.id LIMIT 250", (actor_principal_id, cursor)).fetchall()
            if not rows:
                break
            for event in rows:
                cursor = event["id"]
                if readable is not None and not readable(event):
                    continue
                matches = self.routed_assignments(uow, actor_principal_id, event)
                if len(matches) != 1 or matches[0]["id"] != assignment["id"]:
                    continue
                current = uow.connection.execute(
                    "SELECT * FROM operational_event_claims WHERE event_id=?", (event["id"],)).fetchone()
                if current and current["state"] == "handled":
                    continue
                if current and current["state"] == "claimed":
                    old_assignment = self.get(uow, current["assignment_id"])
                    old_live = (old_assignment["state"] == "running"
                        and old_assignment["execution_generation"] == current["execution_generation"]
                        and old_assignment["lease_until"] is not None
                        and _timestamp(old_assignment["lease_until"], "lease_until") > _timestamp(now_text, "now")
                        and current["lease_until"] is not None
                        and _timestamp(current["lease_until"], "lease_until") > _timestamp(now_text, "now"))
                    if old_live:
                        if (current["assignment_id"] == assignment["id"]
                                and current["execution_generation"] == generation):
                            result.append(event)
                        continue
                if current:
                    uow.connection.execute(
                        "UPDATE operational_event_claims SET assignment_id=?,execution_generation=?,state='claimed',"
                        "lease_until=?,outcome=NULL,handled_at=NULL,updated_at=?,revision=revision+1 WHERE event_id=?",
                        (assignment["id"], generation, assignment["lease_until"], now_text, event["id"]))
                else:
                    uow.connection.execute(
                        "INSERT INTO operational_event_claims(event_id,assignment_id,execution_generation,state,"
                        "lease_until,created_at,updated_at) VALUES(?,?,?,'claimed',?,?,?)",
                        (event["id"], assignment["id"], generation, assignment["lease_until"], now_text, now_text))
                result.append(event)
                if len(result) >= limit:
                    break
            if len(rows) < 250:
                break
        return result

    def settle_event(self, uow, assignment_id, event_id, *, actor_principal_id,
                     generation, lease_id, outcome, now=None):
        """Persist handling outcome and acknowledge the parent delivery together."""
        uow.require_write()
        assignment = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if assignment["parent_principal_id"] != actor_principal_id:
            raise DomainError("forbidden", "Worker event settlement must belong to the parent assignment")
        if type(event_id) is not int or event_id < 1:
            raise DomainError("validation_error", "event_id must be a positive integer")
        outcome = _bounded_text(outcome, "outcome", 2000)
        claim = uow.connection.execute("SELECT * FROM operational_event_claims WHERE event_id=?", (event_id,)).fetchone()
        if (claim is None or claim["assignment_id"] != assignment["id"]
                or claim["execution_generation"] != generation):
            raise DomainError("stale_worker", "Event is not claimed by this assignment generation")
        if claim["state"] == "handled":
            if claim["outcome"] != outcome:
                raise DomainError("idempotency_conflict", "Event already has a different handling outcome")
            return dict(claim)
        if (claim["state"] != "claimed" or claim["lease_until"] is None
                or _timestamp(claim["lease_until"], "lease_until") <= _timestamp(now or self.clock(), "now")):
            raise DomainError("lease_expired", "Event claim is no longer live", retryable=True)
        now_text = now or self.clock()
        delivery = uow.connection.execute(
            "SELECT acknowledged_at FROM inbox_deliveries WHERE agent_id=? AND event_id=?",
            (actor_principal_id, event_id)).fetchone()
        if delivery is None:
            raise DomainError("not_found", "Assigned inbox event is unavailable")
        if delivery["acknowledged_at"] is not None:
            raise DomainError("invalid_transition", "Inbox delivery was acknowledged outside its assignment claim")
        uow.connection.execute("UPDATE operational_event_claims SET state='handled',lease_until=NULL,outcome=?,"
            "handled_at=?,updated_at=?,revision=revision+1 WHERE event_id=? AND state='claimed'",
            (outcome, now_text, now_text, event_id))
        uow.connection.execute("UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? "
            "WHERE agent_id=? AND event_id=? AND acknowledged_at IS NULL",
            (now_text, outcome, actor_principal_id, event_id))
        self._event(uow, assignment, actor_principal_id, "inbox_event_handled",
            {"event_id": event_id, "outcome": outcome})
        return dict(uow.connection.execute(
            "SELECT * FROM operational_event_claims WHERE event_id=?", (event_id,)).fetchone())

    @staticmethod
    def release_event_claims(uow, assignment_id, *, now):
        uow.connection.execute("UPDATE operational_event_claims SET state='released',lease_until=NULL,"
            "updated_at=?,revision=revision+1 WHERE assignment_id=? AND state='claimed'",
            (now, assignment_id))

    def _current_worker(self, uow, assignment_id, generation, lease_id, *, now=None):
        assignment = self.get(uow, assignment_id)
        if assignment["state"] != "running" or assignment["execution_generation"] != generation or assignment["lease_id"] != lease_id:
            raise DomainError("stale_worker", "Worker binding is no longer current")
        now = _timestamp(now or self.clock(), "now")
        if _timestamp(assignment["lease_until"], "lease_until") <= now:
            raise DomainError("lease_expired", "Worker lease expired", retryable=True)
        if _accounted_runtime(assignment, now.isoformat().replace("+00:00", "Z")) >= assignment["resource_bounds_json"]["max_runtime_seconds"]:
            raise DomainError("resource_limit_reached", "Assignment runtime budget is exhausted")
        return assignment

    def claim(self, uow, assignment_id, *, actor_principal_id, lease_seconds=300,
              dispatcher_ready=False, event_available=None, now=None):
        uow.require_write()
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600:
            raise DomainError("validation_error", "lease_seconds must be between 10 and 3600")
        now_text = now or self.clock()
        now_dt = _timestamp(now_text, "now")
        assignment = self.get(uow, assignment_id)
        if self.dispatch_reason and not dispatcher_ready:
            if assignment["state"] == "running" and _timestamp(assignment["lease_until"], "lease_until") <= now_dt:
                updated = uow.repository("operational_assignments").update(assignment["id"], {
                    "state": "queued", "execution_generation": assignment["execution_generation"] + 1,
                    "lease_id": None, "lease_until": None, "dispatch_reason": self.dispatch_reason,
                    "next_step": "Awaiting compatible durable worker dispatch",
                }, expected_revision=assignment["revision"])
                self._event(uow, updated, actor_principal_id, "lease_recovered_dispatch_unavailable",
                            {"reason": self.dispatch_reason})
                return updated
            if assignment["state"] == "queued" and assignment["dispatch_reason"] != self.dispatch_reason:
                updated = uow.repository("operational_assignments").update(assignment["id"], {
                    "dispatch_reason": self.dispatch_reason,
                    "next_step": "Awaiting compatible durable worker dispatch",
                }, expected_revision=assignment["revision"])
                self._event(uow, updated, actor_principal_id, "dispatch_unavailable",
                            {"reason": self.dispatch_reason})
                return updated
            return assignment
        if assignment["dispatch_reason"] is not None:
            assignment = uow.repository("operational_assignments").update(assignment["id"], {
                "dispatch_reason": None,
            }, expected_revision=assignment["revision"])
        state = assignment["state"]
        eligible = state == "queued"
        retry_count = assignment["retry_count"]
        unreported_expiry = False
        if state == "running":
            reported_failure = uow.connection.execute(
                "SELECT 1 FROM operational_assignment_events WHERE assignment_id=? "
                "AND execution_generation=? AND event_type='worker_dispatch_failed' LIMIT 1",
                (assignment["id"], assignment["execution_generation"])).fetchone() is not None
            unreported_expiry = not reported_failure
            retry_after = retry_count + (1 if unreported_expiry else 0)
            lease_expiry = _timestamp(assignment["lease_until"], "lease_until")
            eligible = lease_expiry + timedelta(seconds=_failure_backoff_seconds(retry_after)) <= now_dt
        elif state == "waiting":
            eligible = bool(assignment["wake_at"] and _timestamp(assignment["wake_at"], "wake_at") <= now_dt)
            if not eligible and event_available is not None:
                eligible = bool(event_available(assignment))
        if not eligible:
            raise DomainError("not_ready", "Assignment is not queued or due for recovery", retryable=True)
        if state == "running" and unreported_expiry:
            retry_count += 1
        if retry_count >= assignment["max_attempts"]:
            updated = uow.repository("operational_assignments").update(assignment["id"], {
                "state": "blocked", "lease_id": None, "lease_until": None,
                "retry_count": retry_count,
                "execution_started_at": None,
                "runtime_used_seconds": _accounted_runtime(assignment, now_text),
                "next_step": "Escalate: bounded execution retries are exhausted",
                "wake_at": None, "wake_condition_json": {"kind": "parent_review"},
                "execution_generation": assignment["execution_generation"] + 1,
            }, expected_revision=assignment["revision"])
            uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
                "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, assignment["id"]))
            self.release_event_claims(uow, assignment["id"], now=now_text)
            self._release_browser_turns(uow, assignment["id"], generation=assignment["execution_generation"],
                                        reason="execution retry was fenced", now=now_text)
            self._event(uow, updated, actor_principal_id, "retry_limit_reached", {})
            return updated
        runtime_used = _accounted_runtime(assignment, now_text)
        runtime_limit = assignment["resource_bounds_json"]["max_runtime_seconds"]
        if runtime_used >= runtime_limit:
            updated = uow.repository("operational_assignments").update(assignment["id"], {
                "state": "blocked", "execution_generation": assignment["execution_generation"] + 1,
                "lease_id": None, "lease_until": None, "execution_started_at": None,
                "runtime_used_seconds": runtime_limit, "retry_count": retry_count,
                "next_step": "Escalate: the approved execution time limit was reached",
                "wake_at": None, "wake_condition_json": {"kind": "parent_review"},
            }, expected_revision=assignment["revision"])
            uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
                "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, assignment["id"]))
            self.release_event_claims(uow, assignment["id"], now=now_text)
            self._release_browser_turns(uow, assignment["id"], generation=assignment["execution_generation"],
                                        reason="runtime limit was reached", now=now_text)
            self._event(uow, updated, actor_principal_id, "runtime_limit_reached", {})
            return updated
        # Upgrade only when acquiring a new execution generation, never a live
        # lease or historical result. The driver passes this model on resume too.
        model_upgrade = assignment["selected_model"] == LEGACY_LUNA_MODEL
        selected_model = LUNA_MODEL if model_upgrade else assignment["selected_model"]
        generation = assignment["execution_generation"] + 1
        lease_id = new_id()
        lease_seconds = min(lease_seconds, runtime_limit - runtime_used)
        lease_until = (now_dt + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
            "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, assignment["id"]))
        updated = uow.repository("operational_assignments").update(assignment["id"], {
            "state": "running", "lease_id": lease_id, "lease_until": lease_until,
            "selected_model": selected_model,
            "execution_generation": generation, "attempt_count": assignment["attempt_count"] + 1,
            "retry_count": retry_count,
            "execution_started_at": now_text, "runtime_used_seconds": runtime_used,
            "wake_at": None, "wake_condition_json": {},
        }, expected_revision=assignment["revision"])
        if state in {"waiting", "queued"}:
            from bazarlyk.browser.turns import BrowserTurnQueue
            queue = getattr(uow.database, "browser_turns", None) or BrowserTurnQueue()
            queue.rebind_assignment_generation(uow, assignment["id"], assignment["execution_generation"], generation)
        if model_upgrade:
            self._event(uow, updated, actor_principal_id, "model_upgraded", {
                "previous_model": LEGACY_LUNA_MODEL, "selected_model": LUNA_MODEL,
                "reason": "Luna 5.6 retirement",
            })
        self._event(uow, updated, actor_principal_id, "claimed", {"lease_until": lease_until})
        return updated

    def block_runtime_limit(self, uow, assignment_id, *, parent_principal_id, generation, now=None):
        """Fence a live generation after its approved active-runtime budget is exhausted."""
        uow.require_write()
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        current = self.get(uow, assignment_id, parent_principal_id=parent_principal_id)
        if current["state"] != "running" or current["execution_generation"] != generation:
            raise DomainError("stale_worker", "Runtime limit belongs to a replaced assignment generation")
        now_text = now or self.clock()
        runtime_used = _accounted_runtime(current, now_text)
        runtime_limit = current["resource_bounds_json"]["max_runtime_seconds"]
        if runtime_used < runtime_limit:
            raise DomainError("not_ready", "Assignment runtime budget has not been exhausted", retryable=True)
        updated = uow.repository("operational_assignments").update(current["id"], {
            "state": "blocked", "execution_generation": generation + 1,
            "lease_id": None, "lease_until": None, "execution_started_at": None,
            "runtime_used_seconds": runtime_limit,
            "next_step": "Escalate: the approved execution time limit was reached",
            "wake_at": None, "wake_condition_json": {"kind": "parent_review"},
        }, expected_revision=current["revision"])
        uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
            "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, current["id"]))
        self.release_event_claims(uow, current["id"], now=now_text)
        self._release_browser_turns(uow, current["id"], generation=generation,
                                    reason="runtime limit was reached", now=now_text)
        self._event(uow, updated, parent_principal_id, "runtime_limit_reached", {
            "runtime_used_seconds": runtime_used, "max_runtime_seconds": runtime_limit})
        return updated

    def fence_parent_runtime(self, uow, parent_principal_id, *, actor_principal_id=None,
                             reason="Parent session ended", now=None):
        """Invalidate live workers when the owning session relinquishes dispatch."""
        uow.require_write()
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        actor_principal_id = _canonical_id(actor_principal_id or parent_principal_id, "actor_principal_id")
        reason = _bounded_text(reason, "reason", 500)
        now_text = now or self.clock()
        rows = uow.connection.execute(
            "SELECT id FROM operational_assignments WHERE parent_principal_id=? AND state='running'",
            (parent_principal_id,)).fetchall()
        fenced = []
        for row in rows:
            current = self.get(uow, row["id"])
            updated = uow.repository("operational_assignments").update(current["id"], {
                "state": "queued", "execution_generation": current["execution_generation"] + 1,
                "lease_id": None, "lease_until": None, "dispatch_reason": self.dispatch_reason,
                "runtime_used_seconds": _accounted_runtime(current, now_text), "execution_started_at": None,
                "next_step": "Resume from the saved checkpoint in the next trusted parent session",
            }, expected_revision=current["revision"])
            uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
                "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, current["id"]))
            self.release_event_claims(uow, current["id"], now=now_text)
            self._release_browser_turns(uow, current["id"], generation=current["execution_generation"],
                                        reason=reason, now=now_text)
            self._event(uow, updated, actor_principal_id, "parent_runtime_fenced", {"reason": reason})
            fenced.append(updated)
        return fenced

    def renew(self, uow, assignment_id, *, actor_principal_id, generation, lease_id,
              lease_seconds=300, expected_revision, now=None):
        uow.require_write()
        current = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 3600:
            raise DomainError("validation_error", "lease_seconds must be between 10 and 3600")
        now_text = now or self.clock()
        base = _timestamp(now_text, "now")
        remaining = current["resource_bounds_json"]["max_runtime_seconds"] - _accounted_runtime(current, now_text)
        if remaining <= 0:
            raise DomainError("resource_limit_reached", "Assignment runtime budget is exhausted")
        lease_seconds = min(lease_seconds, max(10, remaining))
        expiry = (base + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if current["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=current["revision"])
        # A liveness heartbeat is not a plan/checkpoint edit. Advancing its
        # revision makes a slow model's perfectly current checkpoint race every
        # heartbeat and can consume its entire execution budget.
        uow.connection.execute("UPDATE operational_assignments SET lease_until=? WHERE id=? AND revision=?",
            (expiry, current["id"], expected_revision))
        updated = uow.repository("operational_assignments").get(current["id"], required=True)
        uow.connection.execute("UPDATE operational_delegation_bindings SET expires_at=?,updated_at=?,revision=revision+1 "
            "WHERE assignment_id=? AND execution_generation=? AND revoked_at IS NULL",
            (expiry, now_text, current["id"], generation))
        self._event(uow, updated, _canonical_id(actor_principal_id, "actor_principal_id"), "lease_renewed", {"lease_until": expiry})
        return updated

    def checkpoint(self, uow, assignment_id, *, actor_principal_id, generation, lease_id,
                   expected_revision, checkpoint, next_step=None, state="running",
                   wake_condition=None, wake_at=None, result=None, now=None):
        uow.require_write()
        current = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        if current["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=current["revision"])
        if state not in _WORKER_STATES:
            raise DomainError("forbidden", "Workers cannot pause, cancel, or reopen assignments")
        checkpoint = _json_value(checkpoint, "checkpoint", kind=dict, max_bytes=32_000)
        # Browser progress is a durable sub-checkpoint. Ordinary assignment
        # checkpoints must not accidentally erase the tab/uncertain-outcome
        # record that the browser adapter saved immediately before a wait.
        saved_browser = current["checkpoint_json"].get("browser")
        if saved_browser is not None and "browser" not in checkpoint:
            checkpoint["browser"] = saved_browser
        next_step = _bounded_text(next_step, "next_step", 2000, optional=True)
        wake_condition = _json_value(wake_condition or {}, "wake_condition", kind=dict, max_bytes=5000)
        result = None if result is None else _json_value(result, "result", kind=dict, max_bytes=32_000)
        if state == "waiting" and not wake_condition and wake_at is None:
            raise DomainError("validation_error", "Waiting assignments require a wake condition")
        if wake_at is not None:
            _timestamp(wake_at, "wake_at")
        if state == "completed" and (result is None or not result.get("summary")):
            raise DomainError("validation_error", "Completed assignments require a bounded result summary")
        if state == "completed":
            unsettled = uow.connection.execute(
                "SELECT 1 FROM operational_event_claims WHERE assignment_id=? AND execution_generation=? "
                "AND state='claimed' LIMIT 1", (current["id"], generation)).fetchone()
            if unsettled:
                raise DomainError("unsettled_events", "Settle every claimed inbox event before completing the assignment")
        changes = {"checkpoint_json": checkpoint, "next_step": next_step,
                   "state": state, "wake_condition_json": wake_condition,
                   "wake_at": wake_at, "result_json": result}
        if state != "running":
            changes.update({"lease_id": None, "lease_until": None,
                "runtime_used_seconds": _accounted_runtime(current, now or self.clock()),
                "execution_started_at": None})
        updated = uow.repository("operational_assignments").update(current["id"], changes,
            expected_revision=expected_revision)
        browser_wait = (state == "waiting" and wake_condition.get("kind") == "browser_turn")
        if state != "running":
            self.release_event_claims(uow, current["id"], now=self.clock())
            if not browser_wait:
                self._release_browser_turns(uow, current["id"], generation=generation,
                                            reason="assignment is no longer running", now=now or self.clock())
        self._event(uow, updated, _canonical_id(actor_principal_id, "actor_principal_id"),
            "checkpointed" if state == "running" else state,
            {"next_step": next_step, "wake_condition": wake_condition,
             "has_result": result is not None})
        return updated

    def control(self, uow, assignment_id, *, parent_principal_id, expected_revision, action):
        uow.require_write()
        current = self.get(uow, assignment_id, parent_principal_id=parent_principal_id)
        if current["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=current["revision"])
        if action == "pause":
            if current["state"] in _FINAL_STATES:
                raise DomainError("invalid_transition", "Completed or cancelled assignments cannot be paused")
            state = "paused"
        elif action == "cancel":
            if current["state"] == "completed":
                raise DomainError("invalid_transition", "Completed assignments cannot be cancelled")
            state = "cancelled"
        elif action == "resume":
            if current["state"] not in {"paused", "blocked", "waiting"}:
                raise DomainError("invalid_transition", "Only paused, blocked, or waiting assignments can be resumed")
            state = "queued"
        else:
            raise DomainError("validation_error", "action must be pause, cancel, or resume")
        now_text = self.clock()
        updated = uow.repository("operational_assignments").update(current["id"], {
            "state": state, "execution_generation": current["execution_generation"] + 1,
            "lease_id": None, "lease_until": None, "wake_at": None,
            "wake_condition_json": {},
            "runtime_used_seconds": _accounted_runtime(current, now_text),
            "execution_started_at": None,
        }, expected_revision=expected_revision)
        uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?, updated_at=?, revision=revision+1 "
            "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, current["id"]))
        self.release_event_claims(uow, current["id"], now=now_text)
        self._release_browser_turns(uow, current["id"], generation=current["execution_generation"],
                                    reason="assignment control changed the execution generation", now=now_text)
        self._event(uow, updated, parent_principal_id, state, {})
        return updated

    def revise(self, uow, assignment_id, *, parent_principal_id, expected_revision,
               expected_plan_revision, objective, plan, allowed_operations,
               constraints=None, links=None, resource_bounds=None):
        uow.require_write()
        current = self.get(uow, assignment_id, parent_principal_id=parent_principal_id)
        # Journal coverage workers have a fixed source and read-only tool set.
        # The general parent revision API must not expand that contract later.
        if uow.connection.execute("SELECT 1 FROM journal_reconciliations WHERE assignment_id=?",
                                  (current["id"],)).fetchone():
            raise DomainError("forbidden", "Journal reconciliation plans are fixed; cancel and start a new review instead")
        if current["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=current["revision"])
        if current["plan_revision"] != expected_plan_revision:
            raise DomainError("revision_conflict", "Approved plan changed", current_revision=current["plan_revision"])
        if current["state"] in _FINAL_STATES:
            raise DomainError("invalid_transition", "Completed or cancelled assignments cannot be revised")
        new_objective = _bounded_text(objective, "objective", 10_000)
        new_plan = _json_value(plan, "plan", kind=dict, max_bytes=50_000)
        if not new_plan:
            raise DomainError("validation_error", "An approved plan must not be empty")
        operations = _operations(allowed_operations)
        links = _links(links if links is not None else current["links_json"])
        constraints = _json_value(constraints if constraints is not None else current["constraints_json"],
                                  "constraints", kind=dict, max_bytes=20_000)
        bounds = _limits(resource_bounds if resource_bounds is not None else current["resource_bounds_json"])
        now_text = self.clock()
        updated = uow.repository("operational_assignments").update(current["id"], {
            "objective": new_objective, "plan_json": new_plan, "plan_revision": current["plan_revision"] + 1,
            "allowed_operations_json": operations, "links_json": links, "constraints_json": constraints,
            "resource_bounds_json": bounds, "state": "queued", "reasoning_effort": worker_effort(operations),
            "execution_generation": current["execution_generation"] + 1,
            "lease_id": None, "lease_until": None, "wake_at": None, "wake_condition_json": {},
            "execution_started_at": None,
            "runtime_used_seconds": _accounted_runtime(current, now_text),
        }, expected_revision=expected_revision)
        uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?, updated_at=?, revision=revision+1 "
            "WHERE assignment_id=? AND revoked_at IS NULL", (now_text, now_text, current["id"]))
        self.release_event_claims(uow, current["id"], now=now_text)
        self._release_browser_turns(uow, current["id"], generation=current["execution_generation"],
                                    reason="assignment plan changed", now=now_text)
        self._event(uow, updated, parent_principal_id, "plan_revised", {"plan_revision": updated["plan_revision"]})
        return updated

    def bind_runtime(self, uow, assignment_id, *, parent_principal_id, generation,
                     lease_id, expected_revision, credential_hash, registration_id,
                     expires_at, runtime_thread_id=None, now=None):
        """Bind one subordinate credential to the currently leased generation."""
        uow.require_write()
        assignment = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        if assignment["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=assignment["revision"])
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        registration_id = _canonical_id(registration_id, "registration_id")
        if assignment["parent_principal_id"] != parent_principal_id:
            raise DomainError("forbidden", "Assignment belongs to another parent")
        credential_hash = _digest(credential_hash)
        expiry = _timestamp(expires_at, "expires_at")
        lease_expiry = _timestamp(assignment["lease_until"], "lease_until")
        if expiry > lease_expiry:
            raise DomainError("validation_error", "Worker credential cannot outlive its execution lease")
        registration = uow.repository("machine_registrations").get(registration_id)
        if registration is None or registration["principal_id"] != parent_principal_id:
            raise DomainError("forbidden", "Worker registration must belong to the assignment parent")
        if runtime_thread_id is not None:
            runtime_thread_id = _bounded_text(runtime_thread_id, "runtime_thread_id", 500)
        now_text = self.clock()
        binding = uow.repository("operational_delegation_bindings").insert({
            "id": new_id(), "assignment_id": assignment["id"],
            "parent_principal_id": parent_principal_id, "execution_generation": generation,
            "credential_hash": credential_hash, "registration_id": registration_id,
            "runtime_thread_id": runtime_thread_id, "expires_at": expires_at,
            "created_at": now_text, "updated_at": now_text,
        })
        uow.repository("operational_assignments").update(assignment["id"], {
            "runtime_thread_id": runtime_thread_id,
        }, expected_revision=expected_revision)
        self._event(uow, assignment, parent_principal_id, "runtime_bound", {
            "binding_id": binding["id"], "registration_id": registration_id,
            "has_runtime_thread": runtime_thread_id is not None,
        })
        return binding

    def issue_worker_binding(self, uow, assignment_id, *, parent_principal_id, generation,
                             lease_id, expected_revision, workspace, service_url,
                             expires_at, runtime_thread_id=None, parent_registration_id=None, now=None):
        """Create the dedicated registration and return a one-time worker token."""
        uow.require_write()
        assignment = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        if assignment["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=assignment["revision"])
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        profile = uow.connection.execute("SELECT revision,status FROM agent_profiles WHERE agent_id=?",
            (parent_principal_id,)).fetchone()
        registration_filter = "AND id=?" if parent_registration_id is not None else ""
        registration_args = ((parent_principal_id, _canonical_id(parent_registration_id, "parent_registration_id"))
                             if parent_registration_id is not None else (parent_principal_id,))
        parent_registration = uow.connection.execute(
            "SELECT id,workspace FROM machine_registrations WHERE principal_id=? " + registration_filter +
            " AND COALESCE(json_extract(compatibility_json,'$.kind'),'')!='delegated_worker' "
            "ORDER BY updated_at DESC LIMIT 1", registration_args).fetchone()
        if (assignment["parent_principal_id"] != parent_principal_id or profile is None
                or profile["status"] != "active" or parent_registration is None):
            raise DomainError("forbidden", "A current active parent registration is required")
        path = Path(workspace)
        if not path.is_absolute() or str(path) != str(path.resolve(strict=False)):
            raise DomainError("validation_error", "Worker workspace must be an absolute normalized path")
        if str(path) == parent_registration["workspace"]:
            raise DomainError("forbidden", "Worker workspace must be separate from the parent workspace")
        parsed = urlsplit(service_url) if isinstance(service_url, str) else None
        if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None):
            raise DomainError("validation_error", "Worker service_url must be an HTTP(S) origin without embedded credentials")
        token = secrets.token_urlsafe(48)
        now_text = self.clock()
        registration = uow.connection.execute(
            "SELECT * FROM machine_registrations WHERE principal_id=? "
            "AND json_extract(compatibility_json,'$.kind')='delegated_worker' "
            "AND json_extract(compatibility_json,'$.assignment_id')=? LIMIT 1",
            (parent_principal_id, assignment["id"])).fetchone()
        if registration is not None:
            if registration["workspace"] != str(path):
                raise DomainError("registration_conflict", "Delegated worker workspace changed for this assignment")
            registration_id = registration["id"]
            uow.repository("machine_registrations").update(registration_id, {
                "service_url": service_url, "profile_revision": profile["revision"],
                "compatibility_json": {"kind": "delegated_worker", "assignment_id": assignment["id"],
                                       "execution_generation": generation},
                "updated_at": now_text,
            }, expected_revision=registration["revision"])
        else:
            registration_id = new_id()
            uow.repository("machine_registrations").insert({
                "id": registration_id, "principal_id": parent_principal_id,
                "workspace": str(path), "service_url": service_url,
                "profile_revision": profile["revision"], "projection_json": {},
                "compatibility_json": {"kind": "delegated_worker", "assignment_id": assignment["id"],
                                       "execution_generation": generation},
                "created_at": now_text, "updated_at": now_text,
            })
        binding = self.bind_runtime(uow, assignment_id, parent_principal_id=parent_principal_id,
            generation=generation, lease_id=lease_id, expected_revision=expected_revision,
            credential_hash=hashlib.sha256(token.encode()).hexdigest(), registration_id=registration_id,
            expires_at=expires_at, runtime_thread_id=runtime_thread_id, now=now)
        return {"binding": binding, "registration_id": registration_id, "worker_token": token}

    def revoke_binding(self, uow, binding_id, *, parent_principal_id, expected_revision):
        uow.require_write()
        binding_id = _canonical_id(binding_id, "binding_id")
        parent_principal_id = _canonical_id(parent_principal_id, "parent_principal_id")
        binding = uow.repository("operational_delegation_bindings").get(binding_id, required=True)
        assignment = self.get(uow, binding["assignment_id"], parent_principal_id=parent_principal_id)
        if binding["revision"] != expected_revision:
            raise DomainError("revision_conflict", "Worker binding changed", current_revision=binding["revision"])
        if binding["revoked_at"] is not None:
            return binding
        updated = uow.repository("operational_delegation_bindings").update(binding_id, {
            "revoked_at": self.clock(),
        }, expected_revision=expected_revision)
        self._event(uow, assignment, parent_principal_id, "runtime_binding_revoked", {"binding_id": binding_id})
        return updated

    def save_runtime_thread(self, uow, assignment_id, *, actor_principal_id, generation,
                            lease_id, runtime_thread_id, now=None):
        """Persist the Codex thread before its first tool/effect can be lost."""
        uow.require_write()
        current = self.get(uow, assignment_id)
        if current["execution_generation"] != generation:
            raise DomainError("stale_worker", "Runtime thread belongs to a replaced assignment generation")
        if current["state"] == "running":
            current = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if current["parent_principal_id"] != actor_principal_id:
            raise DomainError("forbidden", "Runtime thread belongs to another parent")
        try:
            if not isinstance(runtime_thread_id, str) or str(UUID(runtime_thread_id)) != runtime_thread_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise DomainError("validation_error", "runtime_thread_id must be a canonical UUID") from None
        if current["runtime_thread_id"] not in (None, runtime_thread_id):
            raise DomainError("runtime_conflict", "Assignment already uses a different Codex thread")
        binding = uow.connection.execute(
            "SELECT * FROM operational_delegation_bindings WHERE assignment_id=? AND execution_generation=? "
            "AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (current["id"], generation)).fetchone()
        if binding is None:
            raise DomainError("forbidden", "Current worker registration is missing")
        if current["runtime_thread_id"] is None:
            updated = uow.repository("operational_assignments").update(current["id"],
                {"runtime_thread_id": runtime_thread_id}, expected_revision=current["revision"])
            uow.repository("operational_delegation_bindings").update(binding["id"],
                {"runtime_thread_id": runtime_thread_id}, expected_revision=binding["revision"])
            self._event(uow, updated, actor_principal_id, "runtime_thread_saved", {"generation": generation})
            return updated
        return current

    def record_worker_turn(self, uow, assignment_id, *, actor_principal_id, generation,
                           turn_number, usage):
        """Persist Codex-reported usage once per assignment thread turn."""
        uow.require_write()
        current = self.get(uow, assignment_id)
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if current["parent_principal_id"] != actor_principal_id or current["execution_generation"] != generation:
            raise DomainError("stale_worker", "Worker usage belongs to another assignment generation")
        if type(turn_number) is not int or not 1 <= turn_number <= current["resource_bounds_json"]["max_turns"]:
            raise DomainError("validation_error", "turn_number exceeds the assignment turn budget")
        usage = _turn_usage(usage)
        if usage is None:
            raise DomainError("validation_error", "turn_usage is required")
        prior = uow.connection.execute(
            "SELECT payload_json FROM operational_assignment_events WHERE assignment_id=? "
            "AND event_type='worker_turn_completed' AND json_extract(payload_json,'$.turn_number')=?",
            (assignment_id, turn_number)).fetchone()
        if prior is not None:
            saved = json.loads(prior["payload_json"])
            if any(saved.get(name) != value for name, value in usage.items()):
                raise DomainError("idempotency_conflict", "Worker turn usage was already recorded differently")
            return current
        maximum = uow.connection.execute(
            "SELECT COALESCE(MAX(json_extract(payload_json,'$.turn_number')),0) "
            "FROM operational_assignment_events WHERE assignment_id=? AND event_type='worker_turn_completed'",
            (assignment_id,)).fetchone()[0]
        if turn_number != maximum + 1:
            raise DomainError("revision_conflict", "Worker turn usage must be recorded in order",
                              current_revision=maximum)
        self._event(uow, current, actor_principal_id, "worker_turn_completed",
                    {"turn_number": turn_number, **usage})
        return current

    def reset_runtime_thread(self, uow, assignment_id, *, actor_principal_id, generation,
                             lease_id, now=None):
        """Clear an unavailable saved CLI thread before recreating from checkpoint."""
        uow.require_write()
        current = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if current["parent_principal_id"] != actor_principal_id:
            raise DomainError("forbidden", "Runtime thread reset belongs to another parent")
        if current["runtime_thread_id"] is None:
            return current
        binding = uow.connection.execute(
            "SELECT * FROM operational_delegation_bindings WHERE assignment_id=? AND execution_generation=? "
            "AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1", (current["id"], generation)).fetchone()
        if binding is None:
            raise DomainError("forbidden", "Current worker registration is missing")
        now_text = now or self.clock()
        updated = uow.repository("operational_assignments").update(current["id"],
            {"runtime_thread_id": None}, expected_revision=current["revision"])
        uow.repository("operational_delegation_bindings").update(binding["id"],
            {"runtime_thread_id": None}, expected_revision=binding["revision"])
        self._event(uow, updated, actor_principal_id, "runtime_thread_reset",
                    {"generation": generation})
        return updated

    @staticmethod
    def worker_usage(uow, assignment_id):
        row = uow.connection.execute(
            "SELECT count(*) turns, COALESCE(sum(json_extract(payload_json,'$.input_tokens')),0) input_tokens, "
            "COALESCE(sum(json_extract(payload_json,'$.cached_input_tokens')),0) cached_input_tokens, "
            "COALESCE(sum(json_extract(payload_json,'$.output_tokens')),0) output_tokens, "
            "COALESCE(sum(json_extract(payload_json,'$.reasoning_output_tokens')),0) reasoning_output_tokens "
            "FROM operational_assignment_events WHERE assignment_id=? AND event_type='worker_turn_completed'",
            (assignment_id,)).fetchone()
        return {"source": "codex_cli_turn_completed", **dict(row)}

    @staticmethod
    def authenticate_binding(uow, credential_hash, *, now=None):
        """Resolve a presented token digest only while its assignment lease is live."""
        credential_hash = _digest(credential_hash)
        now = _timestamp(now or utc_now(), "now")
        row = uow.connection.execute(
            "SELECT b.*, a.state assignment_state, a.execution_generation assignment_generation, "
            "a.lease_until, p.active principal_active, p.kind principal_kind, p.display_name, "
            "m.profile_revision registration_profile_revision, m.principal_id registration_principal_id, "
            "profile.status profile_status, profile.revision current_profile_revision "
            "FROM operational_delegation_bindings b "
            "JOIN operational_assignments a ON a.id=b.assignment_id "
            "JOIN principals p ON p.id=b.parent_principal_id "
            "JOIN machine_registrations m ON m.id=b.registration_id "
            "JOIN agent_profiles profile ON profile.agent_id=b.parent_principal_id "
            "WHERE b.credential_hash=?", (credential_hash,)).fetchone()
        if row is None:
            return None
        if (row["revoked_at"] is not None or row["principal_kind"] != "agent" or not row["principal_active"]
                or row["registration_principal_id"] != row["parent_principal_id"]
                or row["profile_status"] != "active"
                or row["registration_profile_revision"] != row["current_profile_revision"]
                or row["assignment_state"] != "running"
                or row["execution_generation"] != row["assignment_generation"]
                or _timestamp(row["expires_at"], "expires_at") <= now
                or not row["lease_until"] or _timestamp(row["lease_until"], "lease_until") <= now):
            raise DomainError("unauthenticated", "Delegated worker binding is expired, revoked, or stale")
        return dict(row)

    def record_committed_action(self, uow, assignment_id, *, actor_principal_id, generation,
                                lease_id, target_namespace, action_key, operation_name,
                                payload_hash, effect_ref, delegation_binding_id=None,
                                plan_revision=None, now=None):
        """Record an operation only when its ordinary app idempotency receipt exists.

        Application calls this in the same write transaction as the canonical
        effect and its idempotency receipt, so a crash cannot commit one without
        the other for database-backed operations.
        """
        uow.require_write()
        assignment = self._current_worker(uow, assignment_id, generation, lease_id, now=now)
        actor_principal_id = _canonical_id(actor_principal_id, "actor_principal_id")
        if assignment["parent_principal_id"] != actor_principal_id:
            raise DomainError("forbidden", "Worker action actor differs from its parent principal")
        if plan_revision is None:
            plan_revision = assignment["plan_revision"]
        if type(plan_revision) is not int or plan_revision != assignment["plan_revision"]:
            raise DomainError("stale_worker", "Worker action plan revision is no longer current")
        if delegation_binding_id is None:
            binding_row = uow.connection.execute(
                "SELECT id FROM operational_delegation_bindings WHERE assignment_id=? "
                "AND execution_generation=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (assignment["id"], generation)).fetchone()
            delegation_binding_id = binding_row["id"] if binding_row else None
        if delegation_binding_id is not None:
            binding = uow.repository("operational_delegation_bindings").get(delegation_binding_id)
            if (binding is None or binding["assignment_id"] != assignment["id"]
                    or binding["execution_generation"] != generation or binding["revoked_at"] is not None):
                raise DomainError("forbidden", "Worker action binding is missing, revoked, or mismatched")
        if operation_name not in assignment["allowed_operations_json"]:
            raise DomainError("forbidden", "Operation is outside the approved assignment")
        if operation_name in _PARENT_ONLY_OPERATIONS or not _OPERATION.fullmatch(operation_name):
            raise DomainError("forbidden", "Operation cannot be executed by a delegated worker")
        action_key = _bounded_text(action_key, "action_key", 200)
        payload_hash = _digest(payload_hash)
        effect_ref = _json_value(effect_ref, "effect_ref", kind=dict, max_bytes=5000)
        proof = uow.connection.execute(
            "SELECT input_hash FROM idempotency_receipts WHERE principal_id=? AND target_namespace=? "
            "AND operation_name=? AND idempotency_key=?",
            (actor_principal_id, target_namespace, operation_name, action_key)).fetchone()
        if proof is None or proof["input_hash"] != payload_hash:
            raise DomainError("effect_unverified", "A matching committed application receipt is required")
        existing = uow.connection.execute("SELECT * FROM operational_action_receipts WHERE assignment_id=? AND action_key=?",
            (assignment["id"], action_key)).fetchone()
        if existing:
            receipt = uow.repository("operational_action_receipts").get(existing["id"])
            if receipt["operation_name"] != operation_name or receipt["payload_hash"] != payload_hash:
                raise DomainError("idempotency_conflict", "Action key was already used for a different effect")
            if receipt["state"] == "applied":
                return receipt
            updated = uow.repository("operational_action_receipts").update(receipt["id"], {
                "execution_generation": generation, "state": "applied", "effect_ref_json": effect_ref,
                "parent_principal_id": actor_principal_id,
                "delegation_binding_id": delegation_binding_id,
                "plan_revision": plan_revision,
            }, expected_revision=receipt["revision"])
        else:
            updated = uow.repository("operational_action_receipts").insert({
                "id": new_id(), "assignment_id": assignment["id"], "action_key": action_key,
                "execution_generation": generation, "operation_name": operation_name,
                "payload_hash": payload_hash, "state": "applied", "effect_ref_json": effect_ref,
                "parent_principal_id": actor_principal_id,
                "delegation_binding_id": delegation_binding_id,
                "plan_revision": plan_revision,
            })
        self._event(uow, assignment, actor_principal_id, "action_applied",
                    {"action_key": action_key, "operation": operation_name,
                     "target_namespace": target_namespace, "binding_id": delegation_binding_id,
                     "plan_revision": plan_revision})
        return updated

    @staticmethod
    def hash_payload(payload):
        try:
            return hashlib.sha256(canonical_bytes(payload)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise DomainError("validation_error", f"Action payload is invalid: {exc}") from None


class OperationalDelegationOperations:
    """Application descriptors for parent control and bound worker checkpoints."""

    CAPABILITY = "manage_delegated_assignments"

    def __init__(self, store):
        self.store = store
        self.registry = None
        self.inbox = None
        self.application = None

    @staticmethod
    def _namespace(context, arguments):
        return "global"

    @staticmethod
    def _json_object():
        return {"type": "object"}

    def _require_parent(self, context):
        ref = context.role_artifact_ref
        if (ref is None or not supports_durable_delegation_version(ref.version, ref.id)
                or self.CAPABILITY not in context.global_capabilities
                or (ref.id == "global-marketing" and "coordinate_businesses" not in context.global_capabilities)):
            raise DomainError("forbidden", "Durable assignments require a compatible global peer role and explicit capabilities")
        if context.delegation_assignment_id is not None:
            raise DomainError("forbidden", "A delegated worker cannot create or control assignments")

    @staticmethod
    def _require_worker(context):
        if context.delegation_assignment_id is None:
            raise DomainError("forbidden", "This operation is available only to a bound assignment worker")

    def _change_set(self, context, operation, arguments, *, assignment_id, business_id,
                    expected_revision=None, exists=True, action, values):
        # Orchestration rows are global control-plane state even when they bind
        # to a business. Never make them business-owned document/task content.
        target = ResourceRef("operational_assignments", assignment_id, None, "global")
        expected = ExpectedTarget(target, revision=expected_revision if exists else None, exists=exists)
        return ChangeSet(operation, 1, context.principal_id, "global", arguments,
            {"kind": "operational_assignment", "assignment_id": assignment_id,
             "business_id": business_id}, (expected,),
            (PreparedChange("record", target, action, values),),
            {"title": operation.replace("_", " ").title(), "rationale": "Approved delegated-work control"})

    def register(self, registry, application):
        from qorqut_operations.application.registry import OperationDescriptor
        self.registry = registry
        self.application = application

        uuid = {"type": "string", "format": "uuid"}
        text = {"type": "string", "minLength": 1, "maxLength": 10_000}
        object_schema = {"type": "object"}
        list_schema = {"type": "array"}
        read_specs = {
            "delegated_assignment_get": ({"assignment_id": uuid}, ["assignment_id"]),
            "delegated_assignments_list": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        }
        for name, (properties, required) in read_specs.items():
            registry.register(OperationDescriptor(name,
                {"type": "object", "properties": properties, "required": required},
                effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"global"}),
                global_capability=self.CAPABILITY, resolve_namespace=self._namespace,
                description="Read compact durable delegated-assignment status.",
                read=lambda ctx, args, options, uow, operation=name: self.read(operation, ctx, args, uow)))

        create_properties = {
            "business_id": {"type": ["string", "null"], "format": "uuid"},
            "scope": object_schema, "objective": text, "plan": object_schema,
            "allowed_operations": {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 100},
            "links": object_schema, "constraints": object_schema,
            "internal_recipients": list_schema, "completion_evidence": list_schema,
            "escalation_rules": object_schema, "resource_bounds": object_schema,
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 20},
        }
        self._register_mutation(registry, "delegated_assignments_create",
            create_properties, ["scope", "objective", "plan", "allowed_operations"], self.prepare_create)
        self._register_mutation(registry, "delegated_assignments_control",
            {"assignment_id": uuid, "action": {"enum": ["pause", "resume", "cancel"]}},
            ["assignment_id", "action"], self.prepare_control)
        self._register_mutation(registry, "delegated_assignments_revise",
            {"assignment_id": uuid, "expected_plan_revision": {"type": "integer", "minimum": 1},
             "objective": text, "plan": object_schema,
             "allowed_operations": create_properties["allowed_operations"],
             "links": object_schema, "constraints": object_schema, "resource_bounds": object_schema},
            ["assignment_id", "expected_plan_revision", "objective", "plan", "allowed_operations"],
            self.prepare_revise)
        self._register_mutation(registry, "delegated_assignment_checkpoint",
            {"checkpoint": object_schema, "next_step": {"type": ["string", "null"], "maxLength": 2000},
             "state": {"enum": sorted(_WORKER_STATES)}, "wake_condition": object_schema,
             "wake_at": {"type": ["string", "null"], "format": "date-time"}, "result": object_schema},
            ["checkpoint", "state"], self.prepare_checkpoint)
        self._register_mutation(registry, "delegated_assignment_events_claim",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [], self.prepare_event_claim)
        self._register_mutation(registry, "delegated_assignment_event_settle",
            {"event_id": {"type": "integer", "minimum": 1},
             "outcome": {"type": "string", "minLength": 1, "maxLength": 2000}},
            ["event_id", "outcome"], self.prepare_event_settle)
        return self

    def _has_visible_routed_events(self, context, assignment, uow):
        cursor = 0
        while True:
            rows = uow.connection.execute(
                "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND e.id>? ORDER BY e.id LIMIT 250",
                (context.principal_id, cursor)).fetchall()
            if not rows:
                return False
            for event in rows:
                cursor = event["id"]
                if not self.inbox.event_visible(context, event, uow):
                    continue
                matches = self.store.routed_assignments(uow, context.principal_id, event)
                if len(matches) == 1 and matches[0]["id"] == assignment["id"]:
                    return True
            if len(rows) < 250:
                return False

    def claim_next_for_dispatch(self, context, registration_id, consumer_id):
        """Claim one assignment only for the current opted-in parent watcher."""
        if self.application is None or self.inbox is None:
            raise DomainError("operation_unavailable", "Durable worker dispatch is not installed")
        self._require_parent(context)
        registration_id = _canonical_id(registration_id, "registration_id")
        with self.application.database.transaction(operation="delegation.dispatch.claim") as uow:
            current_context = self.application.current_context(context, uow)
            self._require_parent(current_context)
            if current_context.session_registration_id != registration_id:
                raise DomainError("forbidden", "Dispatcher registration is not bound to this parent session")
            self.inbox.lease(current_context, consumer_id, uow)
            registration = uow.repository("machine_registrations").get(registration_id)
            if registration is None or registration["principal_id"] != current_context.principal_id:
                raise DomainError("forbidden", "Dispatcher registration belongs to another parent")
            if not self._dispatch_enabled(registration):
                raise DomainError("operation_unavailable", "This prepared parent registration does not enable durable delegation")
            workspace = Path(registration["workspace"])
            from bazarlyk.bootstrap.client import path_has_symlink
            if (not workspace.is_absolute() or str(workspace) != str(workspace.resolve(strict=False))
                    or path_has_symlink(workspace)):
                raise DomainError("registration_conflict", "Parent workspace is no longer at its registered path")
            now = self.store.clock()
            active = uow.connection.execute(
                "SELECT count(*) FROM operational_assignments WHERE parent_principal_id=? AND state='running' AND lease_until>?",
                (current_context.principal_id, now)).fetchone()[0]
            if active >= 3:
                return {"assignment": None, "reason": "three worker leases are already active"}
            candidates = uow.connection.execute(
                "SELECT id FROM operational_assignments WHERE parent_principal_id=? "
                "AND state IN ('queued','waiting','running') ORDER BY "
                "CASE state WHEN 'queued' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,updated_at,id LIMIT 500",
                (current_context.principal_id,)).fetchall()
            for candidate in candidates:
                record = self.store.get(uow, candidate["id"])
                try:
                    claimed = self.store.claim(uow, record["id"], actor_principal_id=current_context.principal_id,
                        lease_seconds=min(3600, record["resource_bounds_json"]["max_runtime_seconds"]),
                        dispatcher_ready=True,
                        event_available=lambda row: self._has_visible_routed_events(current_context, row, uow),
                        now=now)
                except DomainError as error:
                    if error.code in {"not_ready", "invalid_transition"}:
                        continue
                    raise
                if claimed["state"] != "running":
                    if claimed["state"] == "blocked":
                        self._notify_parent(uow, claimed, "blocked", claimed.get("next_step"))
                    continue
                usage = self.store.worker_usage(uow, claimed["id"])
                if usage["turns"] >= claimed["resource_bounds_json"]["max_turns"]:
                    blocked = self.store.checkpoint(uow, claimed["id"],
                        actor_principal_id=current_context.principal_id,
                        generation=claimed["execution_generation"], lease_id=claimed["lease_id"],
                        expected_revision=claimed["revision"], checkpoint=claimed["checkpoint_json"],
                        next_step="Escalate: the approved worker-turn limit was reached",
                        state="blocked", wake_condition={"kind": "parent_review"})
                    self._notify_parent(uow, blocked, "blocked", blocked["next_step"])
                    continue
                worker_workspace = self._worker_workspace(current_context.principal_id, claimed["id"])
                binding_data = self.store.issue_worker_binding(uow, claimed["id"],
                    parent_principal_id=current_context.principal_id,
                    generation=claimed["execution_generation"], lease_id=claimed["lease_id"],
                    expected_revision=claimed["revision"], workspace=str(worker_workspace),
                    service_url=registration["service_url"], expires_at=claimed["lease_until"],
                    runtime_thread_id=claimed["runtime_thread_id"],
                    parent_registration_id=registration_id, now=now)
                current = self.store.get(uow, claimed["id"])
                event_rows = self.store.claim_events(uow, current["id"],
                    actor_principal_id=current_context.principal_id,
                    generation=current["execution_generation"], lease_id=current["lease_id"],
                    readable=lambda event: self.inbox.event_visible(current_context, event, uow), now=now)
                return {"assignment": self._compact(current, uow), "generation": current["execution_generation"],
                    "lease_id": current["lease_id"], "worker_workspace": str(worker_workspace),
                    "service_url": registration["service_url"], "worker_registration_id": binding_data["registration_id"],
                    "worker_token": binding_data["worker_token"],
                    "events": [self.inbox.event_item(current_context, event, uow) for event in event_rows]}
            return {"assignment": None, "reason": None}

    def poll_worker(self, context, registration_id, consumer_id, assignment_id, generation,
                    runtime_thread_id=None, turn_number=None, turn_usage=None,
                    reset_runtime_thread=False):
        """Return current status and newly claimed events to the local supervisor."""
        if self.application is None or self.inbox is None:
            raise DomainError("operation_unavailable", "Durable worker dispatch is not installed")
        self._require_parent(context)
        registration_id = _canonical_id(registration_id, "registration_id")
        assignment_id = _canonical_id(assignment_id, "assignment_id")
        if type(generation) is not int or generation < 1:
            raise DomainError("validation_error", "generation must be a positive integer")
        turn_usage = _turn_usage(turn_usage)
        if turn_usage is None and turn_number is not None or turn_usage is not None and (
                type(turn_number) is not int or turn_number < 1):
            raise DomainError("validation_error", "turn_number is required only with turn_usage")
        if type(reset_runtime_thread) is not bool or reset_runtime_thread and (
                runtime_thread_id is not None or turn_usage is not None or turn_number is not None):
            raise DomainError("validation_error", "Runtime thread reset must be a standalone boolean action")
        with self.application.database.transaction(operation="delegation.dispatch.poll") as uow:
            current_context = self.application.current_context(context, uow)
            self._require_parent(current_context)
            if current_context.session_registration_id != registration_id:
                raise DomainError("forbidden", "Dispatcher registration is not bound to this parent session")
            self.inbox.lease(current_context, consumer_id, uow)
            parent_registration = uow.repository("machine_registrations").get(registration_id)
            if parent_registration is None or parent_registration["principal_id"] != current_context.principal_id:
                raise DomainError("forbidden", "Dispatcher registration belongs to another parent")
            if not self._dispatch_enabled(parent_registration):
                raise DomainError("operation_unavailable", "This prepared parent registration does not enable durable delegation")
            assignment = self.store.get(uow, assignment_id, parent_principal_id=current_context.principal_id)
            events = []
            if assignment["execution_generation"] != generation:
                raise DomainError("stale_worker", "Assignment generation changed")
            now_text = self.store.clock()
            if (assignment["state"] == "running"
                    and _accounted_runtime(assignment, now_text)
                    >= assignment["resource_bounds_json"]["max_runtime_seconds"]):
                if turn_usage is not None:
                    assignment = self.store.record_worker_turn(uow, assignment["id"],
                        actor_principal_id=current_context.principal_id, generation=generation,
                        turn_number=turn_number, usage=turn_usage)
                assignment = self.store.block_runtime_limit(uow, assignment["id"],
                    parent_principal_id=current_context.principal_id, generation=generation, now=now_text)
                self._notify_parent(uow, assignment, "blocked", assignment["next_step"])
                return {"assignment": self._compact(assignment, uow), "events": events}
            if reset_runtime_thread:
                assignment = self.store.reset_runtime_thread(uow, assignment["id"],
                    actor_principal_id=current_context.principal_id, generation=generation,
                    lease_id=assignment["lease_id"], now=now_text)
            if runtime_thread_id is not None:
                assignment = self.store.save_runtime_thread(uow, assignment["id"],
                    actor_principal_id=current_context.principal_id, generation=generation,
                    lease_id=assignment["lease_id"], runtime_thread_id=runtime_thread_id, now=now_text)
            if turn_usage is not None:
                assignment = self.store.record_worker_turn(uow, assignment["id"],
                    actor_principal_id=current_context.principal_id, generation=generation,
                    turn_number=turn_number, usage=turn_usage)
            if assignment["state"] == "running":
                now_text = self.store.clock()
                runtime_limit = assignment["resource_bounds_json"]["max_runtime_seconds"]
                if _accounted_runtime(assignment, now_text) >= runtime_limit:
                    assignment = self.store.block_runtime_limit(uow, assignment["id"],
                        parent_principal_id=current_context.principal_id, generation=generation, now=now_text)
                    self._notify_parent(uow, assignment, "blocked", assignment["next_step"])
                else:
                    assignment = self.store.renew(uow, assignment["id"],
                        actor_principal_id=current_context.principal_id, generation=generation,
                        lease_id=assignment["lease_id"], lease_seconds=3600,
                        expected_revision=assignment["revision"], now=now_text)
            if assignment["state"] == "running":
                # If this turn reached the configured bound, fence before the
                # worker can start another one.
                usage = self.store.worker_usage(uow, assignment["id"])
                if usage["turns"] >= assignment["resource_bounds_json"]["max_turns"]:
                    assignment = self.store.checkpoint(uow, assignment["id"],
                        actor_principal_id=current_context.principal_id,
                        generation=generation, lease_id=assignment["lease_id"],
                        expected_revision=assignment["revision"], checkpoint=assignment["checkpoint_json"],
                        next_step="Escalate: the approved worker-turn limit was reached",
                        state="blocked", wake_condition={"kind": "parent_review"})
                    self._notify_parent(uow, assignment, "blocked", assignment["next_step"])
            if assignment["state"] == "running" and assignment["execution_generation"] == generation:
                rows = self.store.claim_events(uow, assignment["id"],
                    actor_principal_id=current_context.principal_id, generation=generation,
                    lease_id=assignment["lease_id"],
                    readable=lambda event: self.inbox.event_visible(current_context, event, uow))
                events = [self.inbox.event_item(current_context, event, uow) for event in rows]
            return {"assignment": self._compact(assignment, uow), "events": events}

    @staticmethod
    def _dispatch_enabled(registration):
        compatibility = registration.get("compatibility_json")
        if isinstance(compatibility, str):
            try:
                compatibility = json.loads(compatibility)
            except (TypeError, ValueError):
                return False
        return isinstance(compatibility, dict) and compatibility.get("durable_delegation_enabled") is True

    @staticmethod
    def _worker_workspace(parent_principal_id, assignment_id):
        """Keep child sessions outside the parent's project/config discovery tree."""
        from pathlib import Path
        import os
        root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
        if not root.is_absolute() or str(root) != str(root.resolve(strict=False)):
            raise DomainError("registration_conflict", "Delegated worker state root must be an absolute normalized path")
        workspace = root / "bazarlyk" / "delegated-workers" / parent_principal_id / assignment_id
        from bazarlyk.bootstrap.client import path_has_symlink
        if path_has_symlink(workspace):
            raise DomainError("registration_conflict", "Delegated worker state cannot follow symlinks")
        return workspace

    def dispatch_failed(self, context, registration_id, consumer_id, assignment_id,
                        generation, *, reason="Codex worker execution failed"):
        """Expire and fence a failed local execution without losing its checkpoint."""
        if self.application is None or self.inbox is None:
            raise DomainError("operation_unavailable", "Durable worker dispatch is not installed")
        self._require_parent(context)
        registration_id = _canonical_id(registration_id, "registration_id")
        assignment_id = _canonical_id(assignment_id, "assignment_id")
        if type(generation) is not int or generation < 1:
            raise DomainError("validation_error", "generation must be a positive integer")
        reason = _bounded_text(reason, "reason", 500)
        with self.application.database.transaction(operation="delegation.dispatch.failure") as uow:
            current_context = self.application.current_context(context, uow)
            self._require_parent(current_context)
            if current_context.session_registration_id != registration_id:
                raise DomainError("forbidden", "Dispatcher registration is not bound to this parent session")
            self.inbox.lease(current_context, consumer_id, uow)
            registration = uow.repository("machine_registrations").get(registration_id)
            if registration is None or registration["principal_id"] != current_context.principal_id:
                raise DomainError("forbidden", "Dispatcher registration belongs to another parent")
            if not self._dispatch_enabled(registration):
                raise DomainError("operation_unavailable", "This prepared parent registration does not enable durable delegation")
            assignment = self.store.get(uow, assignment_id, parent_principal_id=current_context.principal_id)
            if assignment["state"] != "running" or assignment["execution_generation"] != generation:
                raise DomainError("stale_worker", "Failed worker generation is no longer current")
            previous_failure = uow.connection.execute(
                "SELECT 1 FROM operational_assignment_events WHERE assignment_id=? "
                "AND execution_generation=? AND event_type='worker_dispatch_failed' LIMIT 1",
                (assignment_id, generation)).fetchone()
            if previous_failure is not None:
                return self._compact(assignment, uow)
            now = self.store.clock()
            failed = uow.repository("operational_assignments").update(assignment_id, {
                "lease_until": now, "dispatch_reason": reason,
                "retry_count": assignment["retry_count"] + 1,
                "runtime_used_seconds": _accounted_runtime(assignment, now), "execution_started_at": None,
                "next_step": "Retry from the saved checkpoint after bounded backoff",
            }, expected_revision=assignment["revision"])
            uow.connection.execute("UPDATE operational_delegation_bindings SET revoked_at=?,updated_at=?,revision=revision+1 "
                "WHERE assignment_id=? AND execution_generation=? AND revoked_at IS NULL",
                (now, now, assignment_id, generation))
            self.store.release_event_claims(uow, assignment_id, now=now)
            self.store._release_browser_turns(uow, assignment_id, generation=generation,
                                               reason="worker dispatch failed", now=now)
            self.store._event(uow, failed, current_context.principal_id, "worker_dispatch_failed", {"reason": reason})
            self._notify_parent(uow, failed, "recovery", reason)
            return self._compact(failed, uow)

    @staticmethod
    def _notify_parent(uow, assignment, state, detail=None):
        summary = f"Delegated assignment {state}: {assignment['objective'][:180]}"
        if isinstance(detail, str) and detail.strip():
            summary += " — " + detail.strip()[:250]
        now = assignment["updated_at"]
        event_id = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (assignment["business_id"], "operational_assignments",
                assignment["id"], assignment["parent_principal_id"], state, summary[:500], now)).lastrowid
        uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)",
            (assignment["parent_principal_id"], event_id))

    def _validate_operations(self, names):
        selected = _operations(names)
        for name in selected:
            if name in WORKER_OPERATIONS:
                raise DomainError("validation_error", "Worker checkpoint, event and browser-turn controls are supplied automatically; omit them from allowed_operations")
            if name in {"browser_use", "browser_control"}:
                continue
            if not self.registry or not self.registry.known(name):
                raise DomainError("validation_error", "Assignments can grant only installed registered operations")
            descriptor = self.registry.get(name)
            if (descriptor.transport_only or descriptor.owner_only or "mcp" not in descriptor.exposure
                    or descriptor.signoff_category == "platform_action"
                    or name in WORKER_OPERATIONS or name.startswith(("signoffs_", "delegated_"))):
                raise DomainError("forbidden", "This operation is unavailable to delegated workers")
        return selected

    def _register_mutation(self, registry, name, properties, required, prepare):
        from qorqut_operations.application.registry import OperationDescriptor
        registry.register(OperationDescriptor(name,
            {"type": "object", "properties": properties, "required": required},
            effect_kind="records", scope_kinds=frozenset({"global"}),
            global_capability=self.CAPABILITY, resolve_namespace=self._namespace,
            description="Manage or checkpoint a durable bounded assignment.",
            prepare=lambda ctx, args, options, uow: prepare(ctx, args, options, uow),
            apply=self.apply))

    def read(self, operation, context, arguments, uow):
        if operation == "delegated_assignment_get":
            if context.delegation_assignment_id is not None:
                self._require_worker(context)
                if arguments["assignment_id"] != context.delegation_assignment_id:
                    raise DomainError("forbidden", "Worker may read only its own assignment")
            else:
                self._require_parent(context)
            return self._compact(self.store.get(uow, arguments["assignment_id"],
                parent_principal_id=context.principal_id), uow)
        if context.delegation_assignment_id is not None:
            raise DomainError("forbidden", "A delegated worker cannot list its parent's assignments")
        self._require_parent(context)
        rows = self.store.list_for_parent(uow, context.principal_id, limit=arguments.get("limit", 50))
        return {"assignments": [self._compact(row, uow) for row in rows]}

    @staticmethod
    def _compact(record, uow=None):
        compact = {key: record[key] for key in (
            "id", "business_id", "scope_json", "objective", "plan_json", "plan_revision",
            "links_json", "allowed_operations_json", "constraints_json", "internal_recipients_json",
            "completion_evidence_json", "escalation_rules_json", "resource_bounds_json",
            "state", "dispatch_reason", "checkpoint_json", "next_step", "wake_at", "wake_condition_json",
            "result_json", "selected_model", "reasoning_effort", "attempt_count", "max_attempts",
            "retry_count", "execution_generation", "runtime_thread_id", "execution_started_at", "runtime_used_seconds",
            "lease_until", "revision", "updated_at")}
        if uow is not None:
            compact["worker_usage"] = OperationalDelegation.worker_usage(uow, record["id"])
        return compact

    def _current_parent_record(self, context, assignment_id, options, uow):
        self._require_parent(context)
        record = self.store.get(uow, assignment_id, parent_principal_id=context.principal_id)
        if options.expected_revision != record["revision"]:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=record["revision"])
        return record

    def _current_worker_record(self, context, options, uow):
        self._require_worker(context)
        record = self.store.get(uow, context.delegation_assignment_id,
                                parent_principal_id=context.principal_id)
        if options.expected_revision != record["revision"]:
            raise DomainError("revision_conflict", "Assignment changed", current_revision=record["revision"])
        return record

    def prepare_create(self, context, arguments, options, uow):
        self._require_parent(context)
        self._validate_operations(arguments["allowed_operations"])
        assignment_id = new_id()
        business_id = arguments.get("business_id")
        return self._change_set(context, "delegated_assignments_create", arguments,
            assignment_id=assignment_id, business_id=business_id, exists=False,
            action="create_assignment", values={**arguments, "business_id": business_id, "assignment_id": assignment_id})

    def prepare_control(self, context, arguments, options, uow):
        record = self._current_parent_record(context, arguments["assignment_id"], options, uow)
        return self._change_set(context, "delegated_assignments_control", arguments,
            assignment_id=record["id"], business_id=record["business_id"],
            expected_revision=record["revision"], action="control_assignment", values=arguments)

    def prepare_revise(self, context, arguments, options, uow):
        record = self._current_parent_record(context, arguments["assignment_id"], options, uow)
        self._validate_operations(arguments["allowed_operations"])
        return self._change_set(context, "delegated_assignments_revise", arguments,
            assignment_id=record["id"], business_id=record["business_id"],
            expected_revision=record["revision"], action="revise_assignment", values=arguments)

    def prepare_checkpoint(self, context, arguments, options, uow):
        record = self._current_worker_record(context, options, uow)
        return self._change_set(context, "delegated_assignment_checkpoint", arguments,
            assignment_id=record["id"], business_id=record["business_id"],
            expected_revision=record["revision"], action="checkpoint_assignment", values=arguments)

    def prepare_event_claim(self, context, arguments, options, uow):
        record = self._current_worker_record(context, options, uow)
        if self.inbox is None:
            raise DomainError("operation_unavailable", "Assignment-scoped inbox delivery is not installed")
        return self._change_set(context, "delegated_assignment_events_claim", arguments,
            assignment_id=record["id"], business_id=record["business_id"],
            expected_revision=record["revision"], action="claim_assignment_events", values=arguments)

    def prepare_event_settle(self, context, arguments, options, uow):
        record = self._current_worker_record(context, options, uow)
        return self._change_set(context, "delegated_assignment_event_settle", arguments,
            assignment_id=record["id"], business_id=record["business_id"],
            expected_revision=record["revision"], action="settle_assignment_event", values=arguments)

    def apply(self, context, plan, uow):
        change = plan.changes[0]
        values = plain(change.values)
        if change.action == "create_assignment":
            record = self.store.create(uow, parent_principal_id=context.principal_id,
                assignment_id=values.pop("assignment_id"), **values)
        elif change.action == "control_assignment":
            record = self.store.control(uow, values.pop("assignment_id"),
                parent_principal_id=context.principal_id,
                expected_revision=plan.expected_targets[0].revision, action=values["action"])
        elif change.action == "revise_assignment":
            record = self.store.revise(uow, values.pop("assignment_id"),
                parent_principal_id=context.principal_id,
                expected_revision=plan.expected_targets[0].revision,
                expected_plan_revision=values.pop("expected_plan_revision"), **values)
        elif change.action == "checkpoint_assignment":
            current = self.store.get(uow, context.delegation_assignment_id,
                                     parent_principal_id=context.principal_id)
            record = self.store.checkpoint(uow, current["id"], actor_principal_id=context.principal_id,
                generation=context.delegation_generation, lease_id=current["lease_id"],
                expected_revision=plan.expected_targets[0].revision, **values)
            if record["state"] in {"waiting", "blocked", "completed"}:
                self._notify_parent(uow, record, record["state"],
                    (record.get("result_json") or {}).get("summary") if record["state"] == "completed"
                    else record.get("next_step"))
        elif change.action == "claim_assignment_events":
            current = self.store.get(uow, context.delegation_assignment_id,
                                     parent_principal_id=context.principal_id)
            if self.inbox is None:
                raise DomainError("operation_unavailable", "Assignment-scoped inbox delivery is not installed")
            rows = self.store.claim_events(uow, current["id"], actor_principal_id=context.principal_id,
                generation=context.delegation_generation, lease_id=current["lease_id"],
                limit=values.get("limit", 20),
                readable=lambda event: self.inbox.event_visible(context, event, uow))
            return {"events": [self.inbox.event_item(context, event, uow) for event in rows]}
        elif change.action == "settle_assignment_event":
            current = self.store.get(uow, context.delegation_assignment_id,
                                     parent_principal_id=context.principal_id)
            if self.inbox is None:
                raise DomainError("operation_unavailable", "Assignment-scoped inbox delivery is not installed")
            event = uow.connection.execute("SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND e.id=? AND d.acknowledged_at IS NULL",
                (context.principal_id, values["event_id"])).fetchone()
            if event is None or not self.inbox.event_visible(context, event, uow):
                raise DomainError("not_found", "Assigned inbox event is unavailable")
            record = self.store.settle_event(uow, current["id"], values["event_id"],
                actor_principal_id=context.principal_id, generation=context.delegation_generation,
                lease_id=current["lease_id"], outcome=values["outcome"])
            return {"event_id": values["event_id"], "state": record["state"],
                    "outcome": record["outcome"]}
        else:
            raise DomainError("invalid_plan", "Unknown delegated assignment action")
        return {"assignment": self._compact(record, uow)}
