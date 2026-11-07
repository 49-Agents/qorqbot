"""Durable fan-out messages and a small scheduler that never starts agents.

A broadcast keeps its owning business (or Global) scope and its timing; its recipients are global identities. Each selected
agent is addressed in the broadcast's own scope, so one identity means one delivery per occurrence. A recipient must be an
active supported global that reaches that business now, with no agent membership, saved scope list or business home read;
reach is not a permission, and the direct-message contact rules still decide who may be addressed. An agent sender acts,
manages and delivers only while it is itself an active supported global.

The host decides who is an active global, where one reaches and who is directed to the owner; it passes those four facts in
as functions, called again whenever they are needed and never cached. ``BroadcastDispatch`` (``dispatch``) delivers what is due.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain, utc_now
from qorqut_operations.application.plans import PlanBuilder, apply_records, page, require_revision, schema
from qorqut_operations.background.schedule import next_slot, zone, parse_cron, RoutineError

UUID = {"type": "string", "format": "uuid"}
SCOPE = {"scope": {"enum": ["business", "global"]}}
TIMING = {"type": "object", "properties": {
    "kind": {"enum": ["once", "interval", "cron"]}, "at": {"type": "string", "format": "date-time"},
    "every": {"type": "integer", "minimum": 1, "maximum": 525600}, "unit": {"enum": ["minutes", "hours", "days"]},
    "schedule": {"type": "string", "maxLength": 100}, "timezone": {"type": "string", "maxLength": 100}},
    "required": ["kind"], "additionalProperties": False}
DEFINITION = {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120},
    "delivery": {"enum": ["inbox", "input"]}, "message": {"type": "string", "minLength": 1, "maxLength": 16000},
    "recipients": {"type": "array", "minItems": 1, "maxItems": 500, "uniqueItems": True, "items": UUID},
    "timing": TIMING, "max_occurrences": {"type": "integer", "minimum": 1, "maximum": 100000}},
    "required": ["name", "delivery", "message", "recipients", "timing"], "additionalProperties": False}


def _stamp(value):
    if isinstance(value, str): value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None: raise DomainError("validation_error", "Time must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Broadcasts:
    def __init__(self, inbox, *, active_global, global_profile_data, reaches_business, owner_directed_target,
                 clock=lambda: datetime.now(timezone.utc)):
        self.inbox, self.app, self.clock = inbox, inbox.app, clock
        self.active_global, self.global_profile_data = active_global, global_profile_data
        self.reaches_business, self.owner_directed_target = reaches_business, owner_directed_target

    def _scope(self, ctx, args):
        requested = args.get("scope") == "global"
        if requested: return None, "global"
        return ctx.business_id, "business:" + (ctx.business_id or "")

    def _role(self, identity, uow):
        data = self.active_global(uow.connection, identity)
        return data["role"]["id"] if data else None

    def _current_sender(self, ctx, uow):
        if ctx.principal_kind == "agent" and self.active_global(uow.connection, ctx.principal_id) is None:
            raise DomainError("forbidden", "Broadcasts require an active global agent")

    def _input_allowed(self, ctx, uow):
        return self._sender_can_input(ctx.principal_id, uow)

    def _sender_can_input(self, identity, uow):
        principal = uow.repository("principals").get(identity)
        if not principal or not principal["active"]:
            return False
        if principal["kind"] == "human":
            installation = uow.repository("installation").get("service")
            return bool(installation and installation["owner_principal_id"] == identity)
        return False  # Agent direct input requires exact terminal-control authorization.

    def _authorized_recipient(self, ctx, recipient, business_id, uow, *, allow_self=True):
        """``recipient`` addressed in the broadcast's own scope ``business_id`` (None for Global), never its residence."""
        if self.active_global(uow.connection, recipient) is None: raise DomainError("not_found", "Recipient is unavailable")
        if business_id is None and "read_global_context" not in ctx.global_capabilities:
            raise DomainError("forbidden", "Global recipient is outside your scope")
        if business_id is not None and business_id not in ctx.allowed_business_ids:
            raise DomainError("forbidden", "Recipient project is outside your scope")
        if business_id is not None and not self.reaches_business(uow.connection, recipient, business_id):
            raise DomainError("not_found", "Recipient project is unavailable")
        if recipient == ctx.principal_id and allow_self: return {"agent_id": recipient, "business_id": business_id}
        selected = replace(ctx, business_id=business_id)
        # Keep direct-message scope/contact policy as the authoritative peer test.
        self.inbox.messages.peer(selected, recipient, uow)
        if self.owner_directed_target(selected, recipient, uow): raise DomainError("not_found", "Recipient is owner-directed")
        # Cross-business coordination is reserved for Ordubashy and humans.
        if ctx.principal_kind == "agent" and business_id != ctx.business_id and self._role(ctx.principal_id, uow) != "ordubashy":
            raise DomainError("forbidden", "Cross-business broadcasts require global peer authority")
        return {"agent_id": recipient, "business_id": business_id}

    def _recipients(self, ctx, args, options, uow):
        """The global agents a broadcast in the selected scope may address, each listed once.

        ``all_businesses`` is still accepted but fans out nothing: there are no business rosters, so every scope offers the
        same globals. The business fields describe the selected message scope, not where an agent lives."""
        ctx = self.app.current_context(ctx, uow)
        self._current_sender(ctx, uow)
        target, _ = self._scope(ctx, args)
        business = uow.repository("businesses").get(target) if target else None
        values = []
        for row in uow.connection.execute(
                "SELECT a.agent_id,a.name,a.home_business_id,a.status,a.profile_json,p.kind,p.active FROM agent_profiles a "
                "JOIN principals p ON p.id=a.agent_id WHERE a.status='active' AND a.home_business_id IS NULL ORDER BY a.name,a.agent_id"):
            profile = self.global_profile_data(row)
            if profile is None: continue
            role = profile["role"]["id"]
            if args.get("role_id") and role != args["role_id"]: continue
            try: self._authorized_recipient(ctx, row["agent_id"], target, uow)
            except DomainError: continue
            values.append({"agent_id": row["agent_id"], "name": row["name"], "role_id": role, "business_id": target,
                "display_name": profile.get('display_name', row['name']),
                "presentation": profile.get('presentation', {}), "role": {"id": role},
                "business_slug": business["slug"] if business else None,
                "business_name": business["name"] if business else None,
                "group": "Global"})
        return {"items": values}

    def _timing(self, value):
        kind = value["kind"]
        if kind == "once":
            if set(value) != {"kind", "at"}: raise DomainError("validation_error", "Once timing requires only at")
            return {"kind": kind, "at": _stamp(value["at"]), "next": _stamp(value["at"])}
        if kind == "interval":
            if set(value) - {"kind", "every", "unit", "at"} or "every" not in value or "unit" not in value:
                raise DomainError("validation_error", "Interval timing requires every and unit")
            seconds = value["every"] * {"minutes": 60, "hours": 3600, "days": 86400}[value["unit"]]
            start = _stamp(value.get("at") or self.clock() + timedelta(seconds=seconds))
            return {"kind": kind, "every_seconds": seconds, "at": start, "next": start}
        if kind == "cron":
            if set(value) != {"kind", "schedule", "timezone"}: raise DomainError("validation_error", "Cron timing requires schedule and timezone")
            parse_cron(value["schedule"]); zone(value["timezone"])
            following = next_slot(value['schedule'], self.clock(), value['timezone'])
            while following.astimezone(zone(value['timezone'])).fold:
                following = next_slot(value['schedule'], following, value['timezone'])
            return {"kind": kind, "schedule": value["schedule"], "timezone": value["timezone"], "next": _stamp(following)}
        raise DomainError("validation_error", "Unknown timing")

    def _definition(self, ctx, data, uow, *, scope):
        ctx = self.app.current_context(ctx, uow)
        self._current_sender(ctx, uow)
        if not data['message'].strip() or not data['name'].strip():
            raise DomainError('validation_error', 'Enter a message and name')
        if data["delivery"] == "input" and not self._input_allowed(ctx, uow):
            raise DomainError("forbidden", "Input broadcasts require the installation owner; use authorized terminal-control tools")
        scope_business, _ = self._scope(ctx, {"scope": scope})
        # Every recipient is bound to the broadcast's own scope: one selected identity, one delivery per occurrence.
        recipients = [self._authorized_recipient(ctx, item, scope_business, uow) for item in data["recipients"]]
        if len({x['agent_id'] for x in recipients}) != len(recipients): raise DomainError("validation_error", "Recipients must be unique")
        try:
            timing = self._timing(data['timing'])
        except (ValueError, OverflowError, RoutineError) as exc:
            raise DomainError('validation_error', 'Invalid reminder timing: ' + str(exc)) from exc
        return recipients, timing

    def _row(self, ctx, args, uow):
        business, _ = self._scope(ctx, args)
        self._current_sender(ctx, uow)
        row = uow.repository("broadcasts", business_id=business).get(args["broadcast_id"], required=True)
        if ctx.principal_id in (row["sender_id"], row['created_by']): return row
        if ctx.principal_kind == "human":
            installation = uow.repository("installation").get("service")
            member = uow.repository("business_memberships", business_id=business).get(ctx.principal_id) if business else None
            if installation and installation["owner_principal_id"] == ctx.principal_id or member and member["active"] and member["is_owner"]: return row
        role = self._role(ctx.principal_id, uow) if ctx.principal_kind == "agent" else None
        if role == "ordubashy" and (business is None or business in ctx.allowed_business_ids):
            if all(target['business_id'] is None or target['business_id'] in ctx.allowed_business_ids
                   for target in row['recipients_json']):
                return row
        raise DomainError("forbidden", "You cannot manage this broadcast")

    def _can_manage(self, ctx, row, uow):
        try:
            self._row(ctx, {"scope": "global" if row["business_id"] is None else "business", "broadcast_id": row["id"]}, uow)
            return True
        except DomainError:
            return False

    def prepare(self, name, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        business, namespace = self._scope(ctx, args)
        builder = PlanBuilder(ctx, name, args, name.replace("_", " ").capitalize(), namespace=namespace,
            affected_scope={"kind": "global"} if business is None else {"kind": "business", "business_id": business})
        if name == "broadcasts_create":
            recipients, timing = self._definition(ctx, args["definition"], uow, scope=args.get("scope"))
            if args.get("send_now"):
                timing = {"kind": "once", "at": _stamp(self.clock()), "next": _stamp(self.clock())}
            identity = new_id(); data = args["definition"]
            builder.record("broadcasts", identity, None, {"id": identity, "sender_id": ctx.principal_id, "created_by": ctx.principal_id,
                "name": data["name"], "delivery": data["delivery"],
                "message": data["message"], "recipients_json": recipients, "timing_json": timing, "status": "active",
                "max_occurrences": data.get("max_occurrences"), "occurrence_count": 0, "manual_count": 0, "next_due_at": timing["next"]}, business_id=business)
        else:
            row = self._row(ctx, args, uow); require_revision(row, options.expected_revision)
            if row["delivery"] == "input" and ctx.principal_kind != "human" and name not in {"broadcasts_pause", "broadcasts_cancel"}:
                raise DomainError("forbidden", "Only the owner may edit, resume or resend terminal-input broadcasts")
            if row["status"] == "cancelled" and name != "broadcasts_cancel":
                raise DomainError("invalid_transition", "Cancelled broadcasts cannot be changed or sent")
            if name == "broadcasts_update":
                if row["status"] == "cancelled":
                    raise DomainError("invalid_transition", "Cancelled broadcasts cannot be updated")
                recipients, timing = self._definition(ctx, args["definition"], uow, scope=args.get("scope"))
                data = args["definition"]
                if data.get("max_occurrences") is not None and data["max_occurrences"] < row["occurrence_count"]:
                    raise DomainError("validation_error", "max occurrences cannot be below completed scheduled occurrences")
                # The editor authors future instructions; never impersonate the
                # original creator. Already claimed occurrences retain their snapshot.
                values = {"sender_id": ctx.principal_id, "name": data["name"], "delivery": data["delivery"], "message": data["message"],
                    "recipients_json": recipients, "timing_json": timing, "max_occurrences": data.get("max_occurrences"),
                    "next_due_at": timing["next"], "status": "paused" if row["status"] == "paused" else "active"}
            elif name == "broadcasts_send_now":
                values = {"manual_count": row["manual_count"] + 1}
            else:
                if name == "broadcasts_resume" and row["max_occurrences"] is not None and row["occurrence_count"] >= row["max_occurrences"]:
                    raise DomainError("limit_reached", "Increase or remove max occurrences before resuming")
                if name == "broadcasts_resume" and row["next_due_at"] is None and not row["manual_count"]:
                    raise DomainError("invalid_transition", "This schedule has completed; update it with a future time before resuming")
                values = {"status": "active" if name == "broadcasts_resume" else ("cancelled" if name == "broadcasts_cancel" else "paused")}
                if name in {"broadcasts_pause", "broadcasts_cancel"}:
                    values["manual_count"] = 0
            builder.record("broadcasts", row["id"], row, values, business_id=business)
        return builder.build()

    def validate_control(self, ctx, plan, uow):
        """A prepared broadcast change is authorized again when it applies: an agent caller must still be an active global, and
        a change to an existing broadcast is rechecked from its current authors and roles."""
        args = plain(plan.arguments)
        ctx = self.app.current_context(ctx, uow)
        self._current_sender(ctx, uow)
        if args.get("broadcast_id"):
            self._row(ctx, args, uow)

    def apply(self, ctx, plan, uow):
        records = apply_records(ctx, plan, uow)
        if plan.operation_name in {"broadcasts_pause", "broadcasts_cancel"}:
            identity = plan.arguments["broadcast_id"]
            uow.connection.execute("UPDATE broadcast_deliveries SET state='denied',detail='Broadcast paused or cancelled',finished_at=? WHERE broadcast_id=? AND state='pending'",
                (utc_now(), identity))
        return {"records": records}

    def list(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow); business, _ = self._scope(ctx, args)
        rows = uow.repository("broadcasts", business_id=business).list(limit=200, order_by=("created_at", "id"), descending=True)
        rows = [x for x in rows if self._can_manage(ctx, x, uow)]
        items, cursor = page(rows, args, business, "broadcasts_list")
        return {"items": items, "next_cursor": cursor}

    def get(self, ctx, args, options, uow):
        row = self._row(self.app.current_context(ctx, uow), args, uow)
        records = [dict(x) for x in uow.connection.execute("SELECT * FROM broadcast_deliveries WHERE broadcast_id=? ORDER BY occurrence,recipient_id", (row["id"],))]
        return {"broadcast": row, "deliveries": records}

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=lambda ctx,a: "global" if a.get("scope") == "global" else "business:" + (ctx.business_id or ""))
        self.app.registry.register(OperationDescriptor("broadcast_recipients", schema({**SCOPE, "all_businesses": {"type":"boolean",
            "description": instruction_text('broadcasts-accepted-for-existing-callers-it-adds-no-per')},
            "role_id": {"type":"string", "maxLength":100}}), effect_kind="read", read=self._recipients, **common))
        self.app.registry.register(OperationDescriptor("broadcasts_list", schema({**SCOPE, "cursor":{"type":"string"}, "limit":{"type":"integer","minimum":1,"maximum":200}}), effect_kind="read", read=self.list, **common))
        self.app.registry.register(OperationDescriptor("broadcasts_get", schema({**SCOPE, "broadcast_id": UUID}, ("broadcast_id",)), effect_kind="read", read=self.get, **common))
        for name in ("broadcasts_create", "broadcasts_update", "broadcasts_pause", "broadcasts_resume", "broadcasts_cancel", "broadcasts_send_now"):
            fields, required = dict(SCOPE), []
            if name == "broadcasts_create":
                fields["definition"], fields["send_now"], required = DEFINITION, {"type":"boolean"}, ["definition"]
            else:
                fields["broadcast_id"], required = UUID, ["broadcast_id"]
                if name == "broadcasts_update": fields["definition"], required = DEFINITION, ["broadcast_id", "definition"]
            self.app.registry.register(OperationDescriptor(name, schema(fields, required), prepare=lambda c,a,o,u,n=name: self.prepare(n,c,a,o,u), apply=self.apply,
                validate_targets=self.validate_control, **common))
        return self
