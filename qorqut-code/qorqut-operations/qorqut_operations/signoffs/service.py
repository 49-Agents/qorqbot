"""Scope-specific proposal reads, owner decisions, and approved-work recovery."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import base64
from contextlib import nullcontext
import json
from dataclasses import replace
from threading import Event, RLock, Thread
from time import monotonic

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.engine import ApprovalBinding
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationOptions, OperationResult,
                                         canonical_bytes, canonical_hash, new_id, plain, utc_now)
from qorqut_operations.storage.assembly import AssemblyError, StorageContribution
from qorqut_operations.storage.executor import application_claim
from qorqut_operations.storage.locks import LockPaths
from qorqut_operations.storage.receipts import ApplyReceipts, IdempotencyReceipts
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.storage.specs import TableSpec
from .inputs import ACKNOWLEDGE_SQL, InputRequests, acknowledges, validate_response
from . import questions
from .drafts import SCHEMA as DRAFT_SCHEMA, DraftReviews, is_draft, validate_values
from .notes import SignoffNotes, eligible
from .notifications import SignoffNotifications
from .ports import ABSENT, SignoffPorts, checked_narrowing
from .rejection import Rejection
from .sources import SignoffSources

CATEGORIES = ("internal_change", "platform_action", "input_request", "side_chat")

STATES = ("pending", "approved", "applied", "rejected", "conflicted", "failed", "answered", "cancelled")
CONFLICTS = frozenset({"revision_conflict", "forbidden", "policy_denied", "inactive_agent",
                       "invalid_principal", "context_refresh_required", "plugin_unavailable",
                       "operation_unavailable", "profile_conflict", "document_conflict",
                       "artifact_conflict"})
FILE_RECOVERY_INTERVAL = 5.0
PLATFORM_UNAVAILABLE = "Platform action domain is not installed"
UUID_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"}
_UNSET = object()


# Authority metadata of the Operations-owned Signoff operations (deployed v1 catalog lines, unchanged). The
# host declares any platform-specific names with its own platform contribution.
SIGNOFF_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in ("signoffs_list", "signoffs_get", "signoffs_notes_list")),
    *(CatalogEntry(name, False, "records") for name in (
        "signoffs_comment", "signoffs_reject", "signoffs_input_create", "signoffs_input_cancel",
        "signoffs_attention_set", "signoffs_input_answer_delegated", "signoffs_source_link", "signoffs_source_unlink")),
    *(CatalogEntry(name, False, "records", frozenset({"dashboard", "worker"}), True)
      for name in ("signoffs_review", "signoffs_retry_apply")),
)
# Host tables the engine's Signoff SQL reads or writes directly (see docs/EXTRACTION.md).
SIGNOFF_REQUIRED_TABLES = frozenset({"inbox_changes"})


def storage_contribution() -> StorageContribution:
    """The Signoff machinery's storage requirements, for the host's assembly: the engine owns the Signoff tables
    (``signoffs``, ``signoff_notes``, ``signoff_attention``, ``signoff_notifications``, ...) in its core schema and
    contributes immutable draft versions here. These host-created tables must also exist in the composed target."""
    return StorageContribution("signoffs", {"signoff_draft_revisions": TableSpec(("signoff_id", "revision"), mutable=False, revision_column=None)},
                               schema=DRAFT_SCHEMA, required_tables=SIGNOFF_REQUIRED_TABLES)


class SignoffService:
    """Assembly installs reads in the registry and starts/stops this service's worker.

    Owner HTTP handlers call review/retry with their authenticated context. These
    commands own their short transactions and never recurse through agent policy.

    Construction is inert and takes the explicit assembly inputs: the ``Database``, the typed ``SignoffPorts``,
    the explicit ``LockPaths`` for per-Signoff application claims and (optionally) the ``FileApplier``. The
    ``Application`` needs this service's descriptors in its frozen catalog and the service needs the
    ``Application`` to authorize and apply, so the trusted host calls ``bind(application)`` exactly once after
    constructing the application; nothing request-driven works before that and it cannot be rebound.
    """

    def __init__(self, database, ports: SignoffPorts, *, locks: LockPaths, file_applier=None, clock=utc_now,
                 id_factory=new_id):
        if not isinstance(ports, SignoffPorts):
            raise AssemblyError("The Signoff service needs a SignoffPorts assembled by trusted code")
        if not isinstance(locks, LockPaths):
            raise AssemblyError("The Signoff service needs explicit LockPaths for its application claims")
        self.database, self.ports, self.locks = database, ports, locks
        self._app = None
        self.file_applier = file_applier
        self.clock, self.id_factory = clock, id_factory
        self._wake, self._stop = Event(), Event()
        self._thread = None
        self._run_lock = RLock()
        self.last_worker_error = None
        self.notifications = SignoffNotifications(self)
        self.notes = SignoffNotes(self)
        self.inputs = InputRequests(self)
        self.sources = SignoffSources(self)
        self.drafts = DraftReviews(self)
        self.rejection = Rejection(self)
        self._next_notification_tick = 0
        self._next_file_recovery_tick = 0

    @property
    def app(self):
        if self._app is None:
            raise AssemblyError("The Signoff service is not bound to an application")
        return self._app

    def bind(self, application):
        """Attach the one ``Application`` this service stages and applies through (trusted assembly, once)."""
        if self._app is not None:
            raise AssemblyError("The Signoff service is already bound to an application")
        if application.database is not self.database or application.signoffs is not self:
            raise AssemblyError("The application must use this database and name this service as its Signoff stager")
        self._app = application
        return self

    def descriptors(self):
        """The Operations-owned Signoff descriptors, for a ``CatalogContribution``. An operation whose optional
        domain is absent is not installed (calling it is ``operation_unavailable``)."""
        common = {"review_business_id": UUID_SCHEMA, "scope": {"enum": ["business", "global"]}}
        scopes = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                      resolve_namespace=lambda ctx, args: "global" if args.get("scope") == "global" else "business:" + (ctx.business_id or ""))
        return (
            OperationDescriptor("signoffs_get", {"type": "object",
                "required": ["signoff_id"], "properties": {**common, "signoff_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "draft_history_before": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807}}},
                **scopes, effect_kind="read", supports_signoff=False, read=self._get_operation,
                description=instruction_text('service-read-a-proposal-and-owner-feedback-in-the')),
            OperationDescriptor("signoffs_list", {"type": "object", "properties": {
                **common, "responded": {"type": "boolean"}, "include_managed": {"type": "boolean"}, "category": {"enum": list(CATEGORIES)},
                "execution_states": {"type": "array", "items": {"enum": list(self.ports.execution.execution_states)}, "uniqueItems": True},
                "states": {"type": "array", "items": {"enum": list(STATES)}, "uniqueItems": True},
                "proposer_principal_id": UUID_SCHEMA, "initiated_by_principal_id": UUID_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "cursor": {"type": "string", "maxLength": 4096},
                "updated_after": {"oneOf": [{"const": 0}, {"type": "string", "maxLength": 4096}]} }},
                **scopes, effect_kind="read", supports_signoff=False, read=self._list_operation,
                description=instruction_text('service-list-visible-proposals-in-the-selected-project-or')),
            *self.notes.descriptors(), *self.inputs.descriptors(), *self.sources.descriptors(), *self.rejection.descriptors())

    def contribution(self) -> CatalogContribution:
        """The engine's Signoff catalog contribution (authority metadata plus installed descriptors)."""
        return CatalogContribution("signoffs", SIGNOFF_ENTRIES, self.descriptors())

    def _context(self, context, uow, *, owner=False):
        context = self.app.current_context(context, uow)
        business = context.business_id
        if business is None:
            if context.principal_kind == "human":
                installation = uow.repository("installation").get("service")
                if not installation or installation["owner_principal_id"] != context.principal_id:
                    raise DomainError("forbidden", "Global review requires the installation owner")
            elif owner or not context.global_capabilities:
                raise DomainError("forbidden", "Global review is outside the principal's scope")
            if owner and context.source not in ("dashboard", "worker"):
                raise DomainError("forbidden", "Owner decisions are unavailable through agent MCP")
            return context
        if business not in context.allowed_business_ids:
            raise DomainError("forbidden", "Select an authorized review project")
        if owner or context.principal_kind == "human":
            member = uow.repository("business_memberships", business_id=business).get(context.principal_id)
            if (context.principal_kind != "human" or not member or not member["active"]
                    or (owner and not member["is_owner"])):
                raise DomainError("forbidden", "This decision requires the current project owner")
        if owner and context.source not in ("dashboard", "worker"):
            raise DomainError("forbidden", "Owner decisions are unavailable through agent MCP")
        return context

    def _visibility(self, context, uow):
        # All fragments are fixed SQL. Caller values are exclusively parameters.
        caps = sorted(context.global_capabilities)
        businesses = sorted(context.allowed_business_ids)
        cap_marks = ",".join("?" for _ in caps) or "NULL"
        biz_marks = ",".join("?" for _ in businesses) or "NULL"
        cap = "json_extract(p.value, '$.binding.global_capability')"
        forbidden_cap = f"{cap} NOT IN ({cap_marks})" if caps else "1"
        scope = ("((s.review_scope_kind = ? AND s.review_business_id IS NULL) OR "
                 "(s.review_scope_kind = ? AND s.review_business_id IN (" + biz_marks + ")))" if context.business_id is None else
                 "(s.review_scope_kind = ? AND s.review_business_id IS ?)")
        scope_params = (["global", "business", *businesses] if context.business_id is None
                        else ["business", context.business_id])
        clause = (scope + " AND (json_extract(s.affected_scope_json, '$.kind') = 'business' OR "
            "(json_extract(s.affected_scope_json, '$.kind') = 'global' AND "
            "EXISTS (SELECT 1 FROM json_each(s.policy_binding_json, '$.operations') p "
            f"WHERE {cap} IS NOT NULL) AND NOT EXISTS "
            f"(SELECT 1 FROM json_each(s.policy_binding_json, '$.operations') p WHERE {cap} IS NOT NULL AND {forbidden_cap}))) "
            "AND NOT EXISTS (SELECT 1 FROM json_each(s.expected_targets_json) t "
            "WHERE json_extract(t.value, '$.resource.business_id') IS NOT NULL "
            f"AND json_extract(t.value, '$.resource.business_id') NOT IN ({biz_marks}))")
        params=[*scope_params, *caps, *businesses]
        email = self.ports.email
        if email is not ABSENT:
            extra,values=email.visibility(context,uow)
            clause+=extra;params.extend(values)
        narrowing = self.ports.narrowing
        if narrowing is not ABSENT:
            # Opt-in host narrowing: one checked predicate appended last, so it can only remove rows from every reader.
            extra, values = checked_narrowing(*narrowing.narrow(context, uow))
            clause += extra; params.extend(values)
        return clause, params

    def _row(self, context, signoff_id, uow):
        clause, params = self._visibility(context,uow)
        row = decode_row(uow.connection.execute(
            f"SELECT s.* FROM signoffs s WHERE {clause} AND s.id = ?", [*params, signoff_id]).fetchone())
        if row is None:
            raise DomainError("not_found", "Proposal does not exist in this review scope")
        return row

    def _dto(self, row, uow, *, detail=True, draft_history_before=None):
        names = {item["id"]: item["display_name"] for item in uow.connection.execute(
            "SELECT id, display_name FROM principals WHERE id IN (?, ?)",
            (row["proposer_principal_id"], row["initiated_by_principal_id"]))}
        keys = ("id", "review_business_id", "proposal_revision", "payload_hash", "status_revision",
                "state", "operation_name", "operation_version", "proposer_principal_id", "proposer_agent_id",
                "initiated_by_principal_id", "created_at", "updated_at", "updated_sequence",
                "supersedes_id", "superseded_by_id")
        value = {key: row[key] for key in keys}
        value["rejection_requires_message"] = not eligible(uow, row["id"])
        value["note_count"] = uow.connection.execute("SELECT count(*) FROM signoff_notes WHERE signoff_id=?", (row["id"],)).fetchone()[0]
        value["email_context"] = (self.ports.email.email_context(row,uow)
                                  if row["category"]=="input_request" and self.ports.email is not ABSENT else None)
        value["category"] = row["category"]
        value["input"] = row["preview_json"].get("input")
        if value["input"]:
            value["input_kind"] = value["input"].get("kind")
        attention = uow.connection.execute("SELECT * FROM signoff_attention WHERE signoff_id=?", (row["id"],)).fetchone()
        value["attention"] = dict(attention) if attention else {"notify": False, "urgent": False, "blocking": False}
        if attention and attention["priority_updated_by"]:
            actor = uow.connection.execute('SELECT display_name FROM principals WHERE id=?', (attention['priority_updated_by'],)).fetchone()
            value['attention']['priority_updated_by_name'] = actor['display_name'] if actor else None
        decision = (row["decision_json"] or {}).get("kind")
        value["decision_state"] = {"approve": "approved", "reject": "rejected", "respond": "answered"}.get(decision, "unknown" if row["legacy_evidence_json"] is not None else "pending")
        if decision == "respond" and acknowledges(value["input"]):
            # Read, not answered: an acknowledgement says nothing and approves nothing.
            value["decision_state"] = "acknowledged"
        value["actionable"] = row["category"] != "side_chat" and row["state"] == "pending" and not row["superseded_by_id"]
        value["execution_state"] = self.ports.execution.execution_state(row, uow)
        if row["category"] == "platform_action":
            if self.ports.platform is ABSENT:
                # Removing an optional integration must not make retained Signoffs,
                # owner attention, or their discussion unreadable. This projection
                # grants no execution authority; mutations still require the port.
                value["actionable"] = False
                value["platform"] = {"available": False, "execution": {"historical": True}}
            else:
                value["platform"] = self.ports.platform.view(row, uow)
        value["review_scope"] = ({"kind": "business", "business_id": row["review_business_id"]}
                                 if row["review_business_id"] else {"kind": "global"})
        value["legacy_review_routing"] = (row["review_scope_kind"] == "business"
                                            and row["affected_scope_json"].get("kind") == "global")
        value.update(title=row["display_title"] or row["preview_json"]["title"], affected_scope=row["affected_scope_json"],
                     decision=row["decision_json"], proposer_display_name=names.get(row["proposer_principal_id"], ""),
                     initiated_by_display_name=names.get(row["initiated_by_principal_id"]),
                     job_id=row["policy_binding_json"].get("job_id"))
        if value['decision'] and value['decision'].get('message_id'):
            message=uow.connection.execute('SELECT body FROM signoff_notes WHERE id=?',(value['decision']['message_id'],)).fetchone()
            if message:
                field='notes' if value['decision']['kind']=='respond' else 'feedback'
                value['decision']={**value['decision'],field:message[0]}
        if detail and row["state"] == "pending" and not row["superseded_by_id"] and row["legacy_evidence_json"] is None:
            try:
                descriptor = self.app.registry.get(row["operation_name"])
            except DomainError:
                descriptor = None
            if descriptor and descriptor.revise:
                value["editable_fields"] = list(descriptor.editable_fields)
        if detail:
            value.update(preview=row["preview_json"], expected_targets=row["expected_targets_json"],
                         applied_at=row["applied_at"], apply_result=row["apply_result_json"], error=row["error_json"])
            if is_draft(row):
                value['draft_review'] = self.drafts.current(row, uow)
                value['draft_history'] = self.drafts.history(row, uow, before=draft_history_before)
        # Optional host read model of the asynchronous work this approval started: the bounded summary in a list row, the
        # full model in a detail read. Built only for a row the caller's scope and visibility already admitted, from
        # records the host links to it; it never decides anything.
        outcome = getattr(self.ports.execution, "outcome", None)
        outcome = outcome(row, uow, detail=detail) if callable(outcome) else None
        if outcome is not None:
            value["outcome"] = plain(outcome)
        return value

    def _get_operation(self, context, arguments, options, uow):
        if arguments.get("scope") == "global":
            context = replace(context, business_id=None, review_business_id=None)
        context = self._context(context, uow)
        self._review_selector(context, arguments)
        return {"signoff": self._dto(self._row(context, arguments["signoff_id"], uow), uow,
                                     draft_history_before=arguments.get('draft_history_before'))}

    @staticmethod
    def _review_selector(context, arguments):
        if arguments.get("review_business_id", context.business_id) != context.business_id:
            raise DomainError("forbidden", "Review selector differs from the selected project")

    @staticmethod
    def _cursor(kind, binding, position):
        return base64.urlsafe_b64encode(canonical_bytes({"kind": kind, "binding": binding,
                                                       "position": position})).decode().rstrip("=")

    @staticmethod
    def _position(cursor, kind, binding):
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
                raise ValueError
            value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
            if set(value) != {"kind", "binding", "position"} or value["kind"] != kind or value["binding"] != binding:
                raise ValueError
            return value["position"]
        except (ValueError, TypeError, KeyError) as exc:
            raise DomainError("validation_error", "Cursor does not match this caller, review project, or query") from exc

    def _list_operation(self, context, arguments, options, uow):
        if arguments.get("scope") == "global":
            context = replace(context, business_id=None, review_business_id=None)
        context = self._context(context, uow)
        self._review_selector(context, arguments)
        if "cursor" in arguments and "updated_after" in arguments:
            raise DomainError("validation_error", "Browsing and outcome cursors are mutually exclusive")
        clause, params = self._visibility(context,uow)
        if context.principal_kind == 'human' and not arguments.get('include_managed') and self.ports.management is not ABSENT:
            clause += ' AND NOT ('+self.ports.management.managed_input_sql+')'
        if "responded" in arguments:
            clause += " AND s.decision_json IS " + ("NOT NULL" if arguments["responded"] else "NULL")
        if "category" in arguments:
            clause += " AND s.category = ?"
            params.append(arguments["category"])
        else:
            clause += " AND s.category != 'side_chat'"
        if arguments.get("execution_states"):
            clause += " AND " + self.ports.execution.execution_sql("s") + " IN (" + ",".join("?" for _ in arguments["execution_states"]) + ")"
            params.extend(sorted(arguments["execution_states"]))
        counts = dict.fromkeys(STATES, 0)
        for row in uow.connection.execute(f"SELECT s.state, COUNT(*) n FROM signoffs s WHERE {clause} GROUP BY s.state", params):
            counts[row["state"]] = row["n"]
        # Pending read-and-acknowledge requests inside ``counts["pending"]``: something to read, not a decision to make.
        to_read = uow.connection.execute(f"SELECT COUNT(*) FROM signoffs s WHERE {clause} AND s.state='pending' "
            f"AND s.superseded_by_id IS NULL AND {ACKNOWLEDGE_SQL}", params).fetchone()[0]
        filters = {key: arguments[key] for key in ("states", "proposer_principal_id", "initiated_by_principal_id", "category", "execution_states", "responded", "include_managed") if key in arguments}
        if "states" in filters:
            filters["states"] = sorted(filters["states"])
            if filters["states"]:
                clause += " AND s.state IN (" + ",".join("?" for _ in filters["states"]) + ")"
                params.extend(filters["states"])
        for name in ("proposer_principal_id", "initiated_by_principal_id"):
            if name in filters:
                clause += f" AND s.{name} = ?"
                params.append(filters[name])
        binding = canonical_hash({"principal": context.principal_id, "business": context.business_id, "filters": filters})
        limit = arguments.get("limit", 50)
        high_water = uow.connection.execute("SELECT value FROM change_sequences WHERE name = 'signoffs'").fetchone()[0]
        updated_cursor = self._cursor("updated", binding, high_water)
        if "updated_after" in arguments:
            previous = arguments["updated_after"]
            position = 0 if previous in (0, "0") else self._position(previous, "updated", binding)
            if type(position) is not int or position < 0:
                raise DomainError("validation_error", "Invalid outcome cursor position")
            clause += " AND s.updated_sequence > ?"
            params.append(position)
            order = "s.updated_sequence, s.id"
        else:
            order = "CASE WHEN s.state = 'pending' THEN 0 ELSE 1 END, s.created_at, s.id"
            if arguments.get("cursor"):
                position = self._position(arguments["cursor"], "browse", binding)
                if (not isinstance(position, list) or len(position) != 3 or type(position[0]) is not int
                        or position[0] not in (0, 1) or not all(isinstance(x, str) for x in position[1:])):
                    raise DomainError("validation_error", "Invalid browsing cursor position")
                clause += " AND (CASE WHEN s.state = 'pending' THEN 0 ELSE 1 END, s.created_at, s.id) > (?, ?, ?)"
                params.extend(position)
        rows = [decode_row(row) for row in uow.connection.execute(
            f"SELECT s.* FROM signoffs s WHERE {clause} ORDER BY {order} LIMIT ?", [*params, limit + 1])]
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if "updated_after" in arguments:
            updated_cursor = self._cursor("updated", binding, rows[-1]["updated_sequence"] if rows else position)
        elif rows and has_more:
            last = rows[-1]
            next_cursor = self._cursor("browse", binding, [0 if last["state"] == "pending" else 1,
                                                         last["created_at"], last["id"]])
        return {"review_scope": {"kind": "business", "business_id": context.business_id} if context.business_id else {"kind": "global"},
                "review_business_id": context.business_id, "items": [self._dto(row, uow, detail=False) for row in rows],
                "counts": counts, "to_read": to_read, "next_cursor": next_cursor, "updated_cursor": updated_cursor, "has_more": has_more}

    def recent_outcomes(self, context, uow, *, since, limit, until=None, side_chats=True):
        """Newest decided proposals updated since a time (and before ``until``), with signoffs_list visibility."""
        where = "s.state IN ('approved','applied','rejected','answered','failed')" + ("" if side_chats else " AND s.category != 'side_chat'")
        return self._recent(context, uow, where, "updated_at", since, until, limit)

    def recent_proposals(self, context, uow, *, since, limit, until=None):
        """Newest proposals created since a time (and before ``until``), whatever became of them, with signoffs_list
        visibility. A side chat is a conversation, not a proposal."""
        return self._recent(context, uow, "s.category != 'side_chat'", "created_at", since, until, limit)

    def _recent(self, context, uow, where, column, since, until, limit):
        context = self._context(context, uow)
        clause, params = self._visibility(context, uow)
        clause += f" AND {where} AND s.{column}>=?"
        params.append(since)
        if until is not None:
            clause += f" AND s.{column}<?"
            params.append(until)
        count = uow.connection.execute(f"SELECT COUNT(*) FROM signoffs s WHERE {clause}", params).fetchone()[0]
        rows = uow.connection.execute(f"SELECT s.* FROM signoffs s WHERE {clause} ORDER BY s.{column} DESC, s.id DESC LIMIT ?",
                                      [*params, limit]).fetchall()
        return {"count": count, "items": [self._dto(decode_row(row), uow, detail=False) for row in rows]}

    def get(self, context, signoff_id, **arguments):
        return self.app.invoke(context, "signoffs_get", {**arguments, "signoff_id": signoff_id, **({"scope": "global"} if context.business_id is None else {})})

    def list(self, context, **arguments):
        return self.app.invoke(context, "signoffs_list", {**({"scope": "global"} if context.business_id is None else {}), **arguments})

    def _update(self, row, changes, uow):
        updated = uow.repository("signoffs", business_id=row["review_business_id"]).update(row["id"],
            {**changes, "updated_sequence": uow.next_sequence("signoffs")}, expected_revision=row["status_revision"])
        if row["category"] == "platform_action":
            self.ports.require("platform", PLATFORM_UNAVAILABLE).transition(row, updated, uow)
        if self.ports.focus is not ABSENT:
            self.ports.focus.release_signoff(row, updated, uow)
        return updated

    def stage(self, context, descriptor, plan, options, uow):
        uow.require_write()
        if (plan.principal_id != context.principal_id or (plan.review_business_id is None and plan.review_scope != {"kind": "global"})
                or (plan.review_business_id is not None and plan.review_business_id not in context.allowed_business_ids)
                or plan.proposer_agent_id != context.agent_id or plan.initiated_by_principal_id != context.initiated_by_principal_id):
            raise DomainError("forbidden", "Prepared proposal identity or review scope is invalid")
        if not descriptor.supports_signoff:
            raise DomainError("invalid_policy", "Operation cannot support exact-change review")
        if not isinstance(plan.preview.get("title"), str) or not plan.preview["title"].strip():
            raise DomainError("invalid_plan", "A proposal requires its prepared preview")
        old = None
        revision = 1
        if options.supersedes_signoff_id:
            old = uow.repository("signoffs", business_id=plan.review_business_id).get(options.supersedes_signoff_id, required=True)
            if old["proposer_principal_id"] != context.principal_id or old["operation_name"] != descriptor.name:
                raise DomainError("forbidden", "Only the original proposer can replace this operation")
            replaceable = old["state"] in ("pending", "rejected", "conflicted") or (
                self.ports.platform is not ABSENT and self.ports.platform.replaceable_after_cancel(old, uow))
            if (old["status_revision"] != options.expected_superseded_status_revision
                    or not replaceable or old["superseded_by_id"]):
                raise DomainError("stale_signoff", "Proposal is no longer eligible for replacement")
            receipt = uow.repository("apply_receipts").list(filters={"signoff_id": old["id"]}, limit=1)
            if receipt and uow.connection.execute("SELECT 1 FROM resource_claims WHERE apply_id = ? LIMIT 1", (receipt[0]["id"],)).fetchone():
                raise DomainError("apply_incomplete", "Resolve the previous application's resource claims first")
            revision = old["proposal_revision"] + 1
        # Without the platform-action domain no prepared plan can be a platform submission.
        if self.ports.platform is not ABSENT and self.ports.platform.is_email_submission(plan):
            descriptor = replace(descriptor, signoff_category="platform_action")
        platform = {}
        if descriptor.signoff_category == "platform_action":
            platform = self.ports.require("platform", PLATFORM_UNAVAILABLE).staging_values(context, plan, old, uow)
        row = uow.repository("signoffs", business_id=plan.review_business_id).insert({
            "id": platform.get("id") or self.id_factory(), "category": descriptor.signoff_category,
            "execution_job_id": platform.get("execution_job_id"), "review_scope_kind": "global" if plan.review_business_id is None else "business",
            "proposer_principal_id": context.principal_id,
            "proposer_agent_id": context.agent_id, "initiated_by_principal_id": context.initiated_by_principal_id,
            "operation_name": plan.operation_name, "operation_version": plan.operation_version,
            "proposal_revision": revision, "payload_hash": plan.payload_hash, "change_set": plan.canonical,
            "preview_json": plain(plan.preview), "expected_targets_json": plain(plan.expected_targets),
            "policy_binding_json": plain(plan.policy_binding), "affected_scope_json": plain(plan.affected_scope),
            "state": "pending", "updated_sequence": uow.next_sequence("signoffs"),
            "supersedes_id": old["id"] if old else None, "created_at": self.clock(), "updated_at": self.clock()})
        if descriptor.name == "business_archive":
            self.inputs.attention(row["id"], {"notify": True, "urgent": True, "blocking": False,
                "reason": "Project archival requires the owner’s decision", "deadline": None}, uow, actor_id=context.principal_id)
        if descriptor.signoff_category == "platform_action":
            self.ports.platform.staged(context, row, plan, old, uow)
        if old:
            change = {"superseded_by_id": row["id"]}
            if old["state"] == "pending":
                change.update(state="conflicted", error_json={"code": "superseded", "message": "A replacement proposal is available", "retryable": False})
            self._update(old, change, uow)
        summary = {key: row[key] for key in ("id", "state", "proposal_revision", "status_revision", "payload_hash", "review_business_id")}
        summary["review_scope"] = plain(plan.review_scope)
        summary["category"] = row["category"]
        return OperationResult(context.request_id, "signoff_required", signoff=summary)

    def _proposer_context(self, plan, uow):
        loader = getattr(self.app.policy, "context_for", None)
        if loader is None:
            raise DomainError("operation_unavailable", "Prepared execution requires a current principal resolver")
        business = plan.target_namespace.removeprefix("business:") if plan.target_namespace.startswith("business:") else None
        return loader(plan.principal_id, business_id=business, review_business_id=plan.review_business_id,
                      initiated_by_principal_id=plan.initiated_by_principal_id,
                      job_id=plan.policy_binding.get("job_id"), uow=uow)

    def _validate(self, row, uow, *, approved=False):
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        context = self._proposer_context(plan, uow)
        authority = (row["decision_json"] or {}).get("authority")
        if authority is not None:
            # A recorded standing authority this engine no longer issues is never treated as an owner approval.
            raise DomainError("forbidden", "The standing authority this proposal was approved under no longer exists")
        receipt_id = row["id"] if approved else None
        self.app.validate_prepared(context, plan, uow,
            approval=ApprovalBinding(row["id"], plan.payload_hash) if approved else None,
            receipt_id=receipt_id, allow_unverified_files=approved, require_approval=approved)
        if not approved and any(change.kind == "file" for change in plan.changes):
            if self.file_applier is None:
                raise DomainError("operation_unavailable", "Managed file executor is not installed")
            for change in plan.changes:
                if change.kind == "file" and self.file_applier.observed_hash(change) != change.values["before_hash"]:
                    raise DomainError("revision_conflict", "Managed file changed since preparation")
        return context, plan

    def _decision_result(self, context, row, uow, *, replayed=False):
        return OperationResult(context.request_id, "accepted" if row["state"] == "approved" else "ok",
                               data={"signoff": self._dto(row, uow), "replayed": replayed})

    def _stale(self, context, row, uow, message, cause=None):
        details = {"signoff": self._dto(row, uow)} | ({"cause": cause} if cause else {})
        return DomainError("stale_signoff", message, details=details)

    @staticmethod
    def _binding_matches(row, proposal_revision, payload_hash):
        return row["proposal_revision"] == proposal_revision and row["payload_hash"] == payload_hash

    @staticmethod
    def decision_text(decision, uow):
        if decision.get('message_id'):
            note = uow.connection.execute('SELECT body FROM signoff_notes WHERE id=?', (decision['message_id'],)).fetchone()
            return note[0] if note else None
        return decision.get('feedback')

    def amend(self, context, signoff_id, *, changes, proposal_revision, payload_hash,
              expected_status_revision, idempotency_key):
        """Owner-authored revision of a pending proposal; never approves or applies it.

        The domain rebuilds the exact plan under the original proposer's current authority.
        The owner's identity is independently sealed in its preview and recorded in its notes.
        Replacement and receipt commit together, so retries cannot create another proposal.
        """
        try:
            InvocationOptions(expected_revision=expected_status_revision, idempotency_key=idempotency_key)
            if not idempotency_key or type(proposal_revision) is not int or proposal_revision < 1 or not isinstance(changes, dict):
                raise DomainError("validation_error", "Editing requires the current proposal binding and changed fields")
            input_hash = canonical_hash({"id": signoff_id, "changes": changes, "proposal_revision": proposal_revision,
                "payload_hash": payload_hash, "expected_status_revision": expected_status_revision})
            with self.database.transaction() as uow:
                context = self._context(context, uow, owner=True)
                self.app.ensure_writes_open(uow)
                row = self._row(context, signoff_id, uow)
                receipts = IdempotencyReceipts(uow)
                namespace = "business:" + context.business_id if context.business_id else "global"
                replay = receipts.lookup(context.principal_id, namespace, "signoffs_amend", idempotency_key, input_hash)
                if replay:
                    revised = self._row(context, replay.data["signoff"]["id"], uow)
                    return self._decision_result(context, revised, uow, replayed=True)
                if (row["legacy_evidence_json"] is not None or row["state"] != "pending" or row["superseded_by_id"]
                        or row["status_revision"] != expected_status_revision
                        or not self._binding_matches(row, proposal_revision, payload_hash)):
                    raise self._stale(context, row, uow, "This proposal changed; reload it before editing")
                descriptor = self.app.registry.get(row["operation_name"])
                if not descriptor.revise or not changes or set(changes) - set(descriptor.editable_fields):
                    raise DomainError("validation_error", "These proposal fields cannot be edited")
                proposer, original = self._validate(row, uow)
                plan = descriptor.revise(proposer, original, changes, uow)
                if (plan.operation_name != original.operation_name or plan.principal_id != original.principal_id
                        or plan.target_namespace != original.target_namespace):
                    raise DomainError("invalid_plan", "Amended proposal changed its operation or owner")
                decisions = self.app._decisions(proposer, descriptor, plan.expected_targets, plan.changes,
                                               uow, namespace=plan.target_namespace)
                plan = replace(plan, policy_binding=self.app._binding(proposer, decisions),
                    review_scope=self.app._review_scope(proposer, plan), review_business_id=original.review_business_id,
                    encoding_version=2, proposer_agent_id=proposer.agent_id,
                    initiated_by_principal_id=proposer.initiated_by_principal_id,
                    preview={**plain(plan.preview), "owner_edit": {
                    "by": context.principal_id, "at": self.clock(), "previous_id": row["id"]}})
                self.app.validate_prepared(proposer, plan, uow, require_approval=False)
                staged = self.stage(proposer, descriptor, plan, InvocationOptions(
                    supersedes_signoff_id=row["id"], expected_superseded_status_revision=row["status_revision"]), uow)
                revised = self._row(context, staged.signoff["id"], uow)
                self.notes.append(context, revised, "Edited the proposed message. Approval is still required.", uow)
                result = self._decision_result(context, revised, uow)
                receipts.save(context.principal_id, namespace, "signoffs_amend", idempotency_key, input_hash, result)
            return result
        except DomainError as exc:
            return OperationResult.failure(exc, request_id=context.request_id)

    def review(self, context, signoff_id, *, decision, proposal_revision, payload_hash,
               expected_status_revision, idempotency_key, feedback=None):
        return self._decision(context, signoff_id, decision=decision, proposal_revision=proposal_revision,
            payload_hash=payload_hash, expected_status_revision=expected_status_revision,
            idempotency_key=idempotency_key, feedback=feedback)

    def retry(self, context, signoff_id, *, proposal_revision, payload_hash,
              expected_status_revision, idempotency_key):
        return self._decision(context, signoff_id, decision="retry", proposal_revision=proposal_revision,
            payload_hash=payload_hash, expected_status_revision=expected_status_revision,
            idempotency_key=idempotency_key)

    def _decision(self, context, signoff_id, *, decision, proposal_revision, payload_hash,
                  expected_status_revision, idempotency_key, feedback=None):
        try:
            if decision not in ("approve", "reject", "retry"):
                raise DomainError("validation_error", "Unknown owner decision")
            InvocationOptions(expected_revision=expected_status_revision, idempotency_key=idempotency_key)
            if not idempotency_key or type(proposal_revision) is not int or proposal_revision < 1:
                raise DomainError("validation_error", "Decision requires its current binding and an idempotency key")
            if decision != "retry":
                if feedback is not None and (not isinstance(feedback, str) or len(feedback) > 8000):
                    raise DomainError("validation_error", "Feedback must contain at most 8000 characters")
                feedback = feedback if feedback and feedback.strip() else None
            else:
                feedback = None
            input_hash = canonical_hash({"id": signoff_id, "decision": decision, "proposal_revision": proposal_revision,
                "payload_hash": payload_hash, "expected_status_revision": expected_status_revision, "feedback": feedback})
            operation = "signoffs_retry_apply" if decision == "retry" else "signoffs_review"
            with self.database.transaction() as uow:
                context = self._context(context, uow, owner=True)
                self.app.ensure_writes_open(uow)
                row = self._row(context, signoff_id, uow)
                if row["legacy_evidence_json"] is not None:
                    raise DomainError("historical_record", "Migrated history cannot be approved or retried")
                receipts = IdempotencyReceipts(uow)
                namespace = "business:" + context.business_id if context.business_id else "global"
                replay = receipts.lookup(context.principal_id, namespace, operation, idempotency_key, input_hash)
                if replay:
                    if replay.status == "error":
                        return self.app.result(context, replay)
                    return self._decision_result(context, row, uow, replayed=True)
                if not self._binding_matches(row, proposal_revision, payload_hash):
                    raise self._stale(context, row, uow, "Prepared proposal binding changed")
                if row["category"] == "input_request":
                    raise DomainError("invalid_transition", "Input requests must be answered, not approved or rejected")
                if row["category"] == "side_chat":
                    raise DomainError("invalid_transition", "Only the owner can close a side chat, using Close side chat")
                previous = row["decision_json"]
                if decision != "retry" and previous:
                    if previous["kind"] != decision or self.decision_text(previous, uow) != feedback:
                        raise self._stale(context, row, uow, "This proposal already has a different owner decision")
                    result = self._decision_result(context, row, uow, replayed=True)
                else:
                    required_state = "failed" if decision == "retry" else "pending"
                    if row["state"] != required_state or row["status_revision"] != expected_status_revision:
                        raise self._stale(context, row, uow, "Proposal state changed; read its current outcome")
                    if decision == "retry" and (not previous or previous["kind"] != "approve"
                                                  or not (row["error_json"] or {}).get("retryable")):
                        raise self._stale(context, row, uow, "This application cannot be safely retried")
                    if decision == "reject":
                        message_id = self.notes.decision_message(context, row, feedback, uow, rejecting=True)
                        row = self._update(row, {"state": "rejected", "decision_json": {
                            "kind": "reject", "by": context.principal_id, "at": self.clock(), "message_id": message_id}}, uow)
                        result = self._decision_result(context, row, uow)
                    else:
                        try:
                            if decision == "retry":
                                # Inspection does not require an approved state; final apply always does.
                                plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
                                proposer = self._proposer_context(plan, uow)
                                self.app.validate_prepared(proposer, plan, uow, receipt_id=row["id"],
                                    allow_unverified_files=True, require_approval=False)
                            else:
                                _, plan = self._validate(row, uow)
                        except DomainError as exc:
                            if decision != "retry" and exc.code in CONFLICTS:
                                row = self._update(row, {"state": "conflicted", "error_json": exc.to_dict()}, uow)
                                # The owner sees why, for example a changed assignee or membership.
                                stale = self._stale(context, row, uow, "Prepared targets or authority changed: " + exc.message,
                                                    cause=exc.to_dict())
                                result = OperationResult.failure(stale, request_id=context.request_id)
                            else:
                                raise
                        else:
                            message_id = self.notes.decision_message(context, row, feedback, uow) if decision != "retry" else None
                            ApplyReceipts(uow).create(plan, receipt_id=row["id"], signoff_id=row["id"])
                            row = self._update(row, {"state": "approved", "error_json": None,
                                "decision_json": previous or {"kind": "approve", "by": context.principal_id,
                                                                "at": self.clock(), "message_id": message_id}}, uow)
                            result = self._decision_result(context, row, uow)
                receipts.save(context.principal_id, namespace, operation, idempotency_key, input_hash, result)
            self._wake.set()
            return result
        except DomainError as exc:
            return OperationResult.failure(exc, request_id=context.request_id)

    def save_draft(self, context, signoff_id, *, values, proposal_revision, payload_hash,
                   expected_status_revision, idempotency_key):
        """Save an owner version without answering, approving or applying anything."""
        try:
            InvocationOptions(expected_revision=expected_status_revision, idempotency_key=idempotency_key)
            if not idempotency_key or type(proposal_revision) is not int or proposal_revision < 1:
                raise DomainError('validation_error', 'Saving requires the current binding and an idempotency key')
            with self.database.transaction() as uow:
                context = self._context(context, uow, owner=True)
                self.app.ensure_writes_open(uow)
                row = self._row(context, signoff_id, uow)
                values = validate_values(row, values)
                input_hash = self.drafts.input_hash(signoff_id, values, proposal_revision, payload_hash, expected_status_revision)
                namespace = 'business:' + context.business_id if context.business_id else 'global'
                receipts = IdempotencyReceipts(uow)
                if receipts.lookup(context.principal_id, namespace, 'signoffs_draft_save', idempotency_key, input_hash):
                    return self._decision_result(context, row, uow, replayed=True)
                self.drafts.validate_binding(context, row, uow, proposal_revision, payload_hash, expected_status_revision)
                before = self.drafts.current(row, uow)['revision']
                saved = self.drafts.save(context, row, values, uow)
                if saved['revision'] != before:
                    row = self._update(row, {}, uow)
                result = self._decision_result(context, row, uow)
                receipts.save(context.principal_id, namespace, 'signoffs_draft_save', idempotency_key, input_hash, result)
                return result
        except DomainError as exc:
            return OperationResult.failure(exc, request_id=context.request_id)

    def respond(self, context, signoff_id, *, proposal_revision, payload_hash,
                expected_status_revision, idempotency_key, notes=_UNSET, selected_choices=_UNSET,
                draft_values=_UNSET, answers=_UNSET):
        try:
            InvocationOptions(expected_revision=expected_status_revision, idempotency_key=idempotency_key)
            if not idempotency_key or type(proposal_revision) is not int or proposal_revision < 1:
                raise DomainError("validation_error", "Response requires the current binding and an idempotency key")
            with self.database.transaction() as uow:
                context = self._context(context, uow, owner=True)
                self.app.ensure_writes_open(uow)
                row = self._row(context, signoff_id, uow)
                if row['category'] != 'input_request':
                    raise DomainError('invalid_transition', 'Use approve or reject for a prepared action')
                form = row['preview_json']['input']
                grouped = questions.is_questionnaire(form)
                if grouped:
                    if any(value is not _UNSET for value in (notes, selected_choices, draft_values)):
                        raise DomainError('validation_error', 'Question answers cannot be mixed with single-question or draft-review fields')
                    answers = questions.validate_answers(form, answers)
                    messages = questions.answer_messages(form, answers)
                    for message in messages:
                        self.notes.validate(message)
                    response_input = {'answers': answers}
                else:
                    if answers is not _UNSET:
                        raise DomainError('validation_error', 'Answers require a version-2 question form')
                    notes = '' if notes is _UNSET else notes
                    selected_choices = [] if selected_choices is _UNSET or selected_choices is None else selected_choices
                    draft_values = None if draft_values is _UNSET else draft_values
                    notes, selected = validate_response(form, notes, selected_choices)
                    if is_draft(row):
                        if draft_values is None:
                            raise DomainError('validation_error', 'Returning a draft requires its complete edited fields')
                        draft_values = validate_values(row, draft_values, final=True)
                    elif draft_values is not None:
                        raise DomainError('validation_error', 'Draft values are only accepted for draft reviews')
                    response_input = {'notes': notes, 'selected': selected,
                        **({'draft_values': draft_values} if draft_values is not None else {})}
                input_hash = canonical_hash({'id': signoff_id, 'proposal_revision': proposal_revision,
                    'payload_hash': payload_hash, 'revision': expected_status_revision, **response_input})
                receipts = IdempotencyReceipts(uow)
                namespace = 'business:' + context.business_id if context.business_id else 'global'
                replay = receipts.lookup(context.principal_id, namespace, 'signoffs_respond', idempotency_key, input_hash)
                if replay:
                    return self._decision_result(context, row, uow, replayed=True)
                if (not self._binding_matches(row, proposal_revision, payload_hash)
                        or row['status_revision'] != expected_status_revision or row['state'] != 'pending' or row['superseded_by_id']):
                    raise self._stale(context, row, uow, 'This input request has changed or already received a response')
                if grouped:
                    submitted = [{**answer, 'message_id': self.notes.decision_message(context, row, message, uow)}
                                 for answer, message in zip(answers, messages)]
                    decision = {'schema_version': 2, 'answers': submitted}
                else:
                    decision = {'message_id': self.notes.decision_message(context, row, notes, uow), 'selected_choices': selected}
                    if is_draft(row):
                        saved = self.drafts.save(context, row, draft_values, uow)
                        decision.update(draft_values=draft_values, draft_revision=saved['revision'])
                row = self._update(row, {'state': 'answered', 'decision_json': {'kind': 'respond',
                    'by': context.principal_id, 'at': self.clock(), **decision}}, uow)
                result = self._decision_result(context, row, uow)
                receipts.save(context.principal_id, namespace, 'signoffs_respond', idempotency_key, input_hash, result)
                return result
        except DomainError as exc:
            return OperationResult.failure(exc, request_id=context.request_id)

    def _record_failure(self, signoff_id, error):
        with self.database.read() as uow:
            row = decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id = ?", (signoff_id,)).fetchone())
            if not row or row["state"] != "approved":
                return
            try:
                plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
            except DomainError:
                plan = None
        if plan is None:
            with self.database.transaction() as uow:
                row = uow.repository("signoffs", business_id=row["review_business_id"]).get(signoff_id, required=True)
                if row["state"] == "approved":
                    self._update(row, {"state": "failed", "error_json": {"code": "invalid_plan", "message": "Stored prepared content failed verification",
                        "retryable": False, "effect_status": "partial_or_uncertain"}}, uow)
            return
        file_changes = [x for x in plan.changes if x.kind == "file"]
        unchanged = not file_changes
        recognized = unchanged
        if file_changes and self.file_applier:
            try:
                states = [self.file_applier.effect_state(signoff_id, x) for x in file_changes]
                unchanged = all(state[0] for state in states)
                recognized = all(state[1] for state in states)
            except (OSError, DomainError):
                unchanged = recognized = False
            if unchanged:
                self.file_applier.release_unchanged(signoff_id, error)
        authority_conflict = error.code in CONFLICTS
        state = "conflicted" if authority_conflict and unchanged else "failed"
        retryable = recognized and not authority_conflict and error.code in {"busy", "file_apply_failed", "operation_failed", "storage_error", "maintenance"}
        details = error.to_dict()
        details.update(retryable=retryable, effect_status="none" if unchanged else "partial_or_uncertain")
        with self.database.transaction() as uow:
            row = decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id = ?", (signoff_id,)).fetchone())
            if row["state"] != "approved":
                return
            if not file_changes:
                ApplyReceipts(uow).release_unapplied(signoff_id, error, verified_before={})
            else:
                ApplyReceipts(uow).fail(signoff_id, error)
            self._update(row, {"state": state, "error_json": details}, uow)

    def run_one(self, signoff_id):
        """Internal service work only. Never called through agent MCP."""
        admission = self.app.writer_admission.writer() if self.app.writer_admission is not None else nullcontext()
        try:
            with admission:
                return self._run_one_admitted(signoff_id)
        except DomainError as exc:
            if exc.code == "maintenance":
                return None  # Leave approved work pending; no failed receipt is created.
            raise

    def _run_one_admitted(self, signoff_id):
        with self._run_lock:
            try:
                with application_claim(self.locks, signoff_id):
                    return self._run_claimed(signoff_id)
            except DomainError as exc:
                if exc.code == "busy":
                    return None
                raise

    def _run_claimed(self, signoff_id):
        try:
            with self.database.transaction() as uow:
                row = decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id = ?", (signoff_id,)).fetchone())
                if row is None or row["state"] != "approved":
                    return None
                context, plan = self._validate(row, uow, approved=True)
                has_files = any(change.kind == "file" for change in plan.changes)
                if has_files and self.file_applier is None:
                    raise DomainError("operation_unavailable", "Managed file executor is not installed")
                ApplyReceipts(uow).claim_resources(signoff_id)
            if has_files:
                self.file_applier.apply(signoff_id)
            with self.database.transaction() as uow:
                row = uow.repository("signoffs", business_id=plan.review_business_id).get(signoff_id, required=True)
                if row["state"] != "approved":
                    return None
                result = self.app.apply_prepared(context, plan, uow,
                    approval=ApprovalBinding(signoff_id, plan.payload_hash), receipt_id=signoff_id)
                ApplyReceipts(uow).finish(signoff_id, result.to_dict())
                row = self._update(row, {"state": "applied", "applied_at": self.clock(),
                    "apply_result_json": result.to_dict(), "error_json": None}, uow)
                return self._dto(row, uow)
        except DomainError as exc:
            self._record_failure(signoff_id, exc)
        except Exception as exc:
            self.last_worker_error = type(exc).__name__
            self._record_failure(signoff_id, DomainError("operation_failed", "Prepared application failed"))
        return None

    def run_pending(self, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Application batch must contain 1-100 proposals")
        with self.database.read() as uow:
            ids = [row[0] for row in uow.connection.execute(
                "SELECT id FROM signoffs WHERE state = 'approved' ORDER BY updated_sequence, id LIMIT ?", (limit,))]
        for signoff_id in ids:
            if self._stop.is_set():
                break
            self.run_one(signoff_id)
        return len(ids)

    def _recover_pending_files(self, *, limit=50):
        """Retry retained file applications while the service worker is alive.

        The executor owns the durable receipt and per-application claim.  The
        surrounding admission keeps recovery inside the same installation
        writer barrier as approved signoff work, so this periodic pass cannot
        race maintenance or another local writer.
        """
        executor = self.app.file_executor
        if executor is None:
            return 0
        writer_admission = self.app.writer_admission
        admission = writer_admission.writer() if writer_admission is not None else nullcontext()
        with admission:
            return len(executor.recover_pending(self.app, limit=limit))

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._next_file_recovery_tick = 0
        self._thread = Thread(target=self._work, name="operations-signoffs", daemon=True)
        self._thread.start()

    def _work(self):
        while not self._stop.is_set():
            try:
                count = self.run_pending()
                now = monotonic()
                if now >= self._next_file_recovery_tick:
                    # Advance the deadline before recovery so a transient
                    # failure cannot turn the worker into a tight retry loop.
                    self._next_file_recovery_tick = now + FILE_RECOVERY_INTERVAL
                    self._recover_pending_files()
                if monotonic() >= self._next_notification_tick:
                    self.notifications.tick()
                    self._next_notification_tick = monotonic() + 15
                self.last_worker_error = None
            except Exception as exc:
                count = 0
                self.last_worker_error = type(exc).__name__
            self._wake.wait(0.05 if count >= 50 else 0.5)
            self._wake.clear()

    def stop(self, *, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Signoff worker did not finish its current application")
            self._thread = None
