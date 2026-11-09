"""The shared host reminder service: one transport-independent store for the owner, Ordubashy and other registered agents.

A reminder is a definition row (recipient, text, optional link, one-off time or recurrence, snooze, the next durable due
boundary and the one current due occurrence). Each due moment is an immutable occurrence row that earns at most one durable
delivery receipt. Seven operations manage them; the inbox reconcile hook (``reconcile``) turns due boundaries into
occurrences and hands each to ``ReminderDelivery``, which writes the existing inbox event or selected connector receipt in the same
transaction. The service never sends anything itself.

Authority: the installation owner and the configured Ordubashy manage every reminder; any other agent manages only the
reminders addressed to itself. ``creator_id`` stays the origin; ``authority_principal_id`` is whoever last authorized the
schedule, recipient or a snooze, and is the principal delivery rechecks. Recipient, scheduling authority and the linked item's
readability for both are rechecked when a delivery is published, when a queued owner delivery is admitted and when an inbox event
is read; delegated identities never manage reminders."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
import base64
import json
import logging
import time

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, page_position, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, canonical_bytes, new_id, plain
from qorqut.agents.global_scope import active_global
from qorqut.management.identity import configured_ordubashy, is_active_installation_owner

from . import timing
from .views import legacy, record, stored_link

LOG = logging.getLogger(__name__)

CREATE, GET, LIST, UPDATE, SNOOZE, DISMISS, CANCEL = (
    "reminders_create", "reminders_get", "reminders_list", "reminders_update", "reminders_snooze", "reminders_dismiss", "reminders_cancel")
OPERATIONS = (CREATE, GET, LIST, UPDATE, SNOOZE, DISMISS, CANCEL)
STATES = ("scheduled", "due", "dismissed", "cancelled")
ACTIVE = ("scheduled", "due")
DEFAULT_PAGE, MAX_PAGE, SCAN_CHUNK, SCAN_LIMIT = 25, 100, 100, 500
# One reconcile pass is bounded: due boundaries turned into occurrences, publishes per recipient and in all, and wall time.
FIRE_BATCH, PUBLISH_PER_RECIPIENT, PUBLISH_BATCH, PASS_SECONDS = 20, 5, 25, 1.5
RETRY_BASE_SECONDS, RETRY_CAP_SECONDS, DEFER_SECONDS = 30, 900, 600
DUE_SORT = "COALESCE(CASE WHEN state='due' THEN due_at END, next_at, due_at, created_at)"

ID = {"type": "string", "minLength": 1, "maxLength": 200}
UUID = {"type": "string", "format": "uuid"}
REVISION = {"type": "integer", "minimum": 1}
TEXT = {"type": "string", "minLength": 1, "maxLength": 240,
        "description": instruction_text('service-one-concise-sentence-the-recipient-will-read-when')}
AT = {"type": "string", "format": "date-time", "description": instruction_text('service-a-time-with-its-timezone-offset-for-example')}
RECURRENCE = {"anyOf": [schema({"schedule": {"type": "string", "minLength": 9, "maxLength": 200,
                                              "description": instruction_text('service-five-field-cron-minute-hour-day-of-month')},
                                "timezone": {"type": "string", "minLength": 1, "maxLength": 100,
                                             "description": instruction_text('service-iana-timezone-name-for-example-america-new-york')}},
                               ("schedule", "timezone")), {"type": "null"}]}
LINK = {"anyOf": [schema({"type": ID, "id": ID, "business_id": {"anyOf": [UUID, {"type": "null"}]}}, ("type", "id", "business_id")),
                  {"type": "null"}]}


@dataclass(frozen=True)
class Access:
    """The refreshed caller and what it may manage."""
    ctx: object
    kind: str  # owner | coordinator | agent

    @property
    def manager(self):
        return self.kind in ("owner", "coordinator")


@contextmanager
def savepoint(uow, name):
    uow.connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        uow.connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        uow.connection.execute(f"RELEASE SAVEPOINT {name}")
        raise
    uow.connection.execute(f"RELEASE SAVEPOINT {name}")


class ReminderService:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application
        self.clock = getattr(runtime.inbox, "clock", None) or utc_clock
        self.delivery = None

    # ----- registration ------------------------------------------------------------------------------------------

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                      exposure=frozenset({"mcp", "dashboard", "worker"}), resolve_namespace=lambda ctx, args: self.namespace(ctx))
        write_fields = {
            CREATE: ({"text": TEXT, "recipient_id": ID, "at": AT, "recurrence": RECURRENCE, "time_critical": {"type": "boolean"},
                      "linked_item": LINK}, ("text",),
                     instruction_text('service-create-a-reminder-for-yourself-the-owner-and')),
            UPDATE: ({"reminder_id": UUID, "revision": REVISION, "text": TEXT, "recipient_id": ID, "at": AT, "recurrence": RECURRENCE,
                      "time_critical": {"type": "boolean"}, "linked_item": LINK}, ("reminder_id", "revision"),
                     instruction_text('service-change-a-reminder-using-its-current-revision-a')),
            SNOOZE: ({"reminder_id": UUID, "revision": REVISION, "at": AT}, ("reminder_id", "revision", "at"),
                     instruction_text('service-defer-a-scheduled-or-due-reminder-to-a')),
            DISMISS: ({"reminder_id": UUID, "revision": REVISION, "occurrence_id": UUID}, ("reminder_id", "revision", "occurrence_id"),
                      instruction_text('service-clear-the-current-due-occurrence-using-its-occurrence')),
            CANCEL: ({"reminder_id": UUID, "revision": REVISION}, ("reminder_id", "revision"),
                     instruction_text('service-stop-the-whole-reminder-or-series-and-discard')),
        }
        for name, (fields, required, description) in write_fields.items():
            self.app.registry.register(OperationDescriptor(name, schema(fields, required), **common, description=description,
                replay_validator=lambda ctx, args, replay, uow, op=name: self.replay(op, ctx, args, replay, uow),
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, uow), apply=self.apply))
        self.app.registry.register(OperationDescriptor(GET, schema({"reminder_id": UUID}, ("reminder_id",)), **common, effect_kind="read",
            description=instruction_text('service-read-one-reminder-you-may-see-its-state'), read=self.get))
        self.app.registry.register(OperationDescriptor(LIST, schema({
            "recipient_id": ID, "all_recipients": {"type": "boolean"}, "all_scopes": {"type": "boolean"},
            "states": {"type": "array", "items": {"enum": list(STATES)}, "maxItems": 4, "uniqueItems": True},
            "cursor": {"type": "string", "maxLength": 2000}, "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}),
            **common, effect_kind="read", description=instruction_text('service-list-your-reminders-scheduled-and-due-by-default'),
            read=self.list))
        from .legacy import LegacyReminders
        self.legacy = LegacyReminders(self).register()
        from .delivery import ReminderDelivery
        self.delivery = ReminderDelivery(self.runtime, self)
        self.app.reminders = self
        self.runtime.reminders = self
        return self

    # ----- identity and authority ----------------------------------------------------------------------------------

    @staticmethod
    def namespace(ctx):
        return "business:" + ctx.business_id if ctx.business_id else "global"

    def caller(self, ctx, uow):
        ctx = self.app.current_context(ctx, uow)
        if ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "Delegated identities cannot manage reminders")
        if ctx.principal_kind == "human":
            if not is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind):
                raise DomainError("forbidden", "Only the installation owner and registered agents manage reminders")
            return Access(ctx, "owner")
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Only the installation owner and registered agents manage reminders")
        return Access(ctx, "coordinator" if ctx.principal_id == configured_ordubashy(uow) else "agent")

    def authorize_operation(self, ctx, operation, uow):
        """The normal policy decision for ``operation`` in the caller's scope; an explicit deny or a missing grant refuses."""
        decision = self.app.authorize(ctx, self.app.registry.get(operation), (), uow, namespace=self.namespace(ctx))
        if decision.mode != "allow":
            raise DomainError("forbidden", "That operation is not permitted by the active policy")

    def replay(self, operation, ctx, args, replay, uow):
        """Before a saved reminder result is returned again: the caller must still be allowed the operation, still reach the
        reminder (a readdressed reminder is no longer an agent's), and still read the linked item the saved view carries."""
        access = self.caller(ctx, uow)
        self.authorize_operation(access.ctx, operation, uow)
        self.check_saved(access, plain(args), plain(replay.data or {}).get("reminder"), uow)

    def check_saved(self, access, args, saved, uow):
        reminder_id = (saved or {}).get("reminder_id") or args.get("reminder_id")
        if not reminder_id:
            return
        self.required(access, reminder_id, uow)
        link = (saved or {}).get("linked_item")
        if link is not None and self.read_link(access.ctx, link, uow) is None:
            raise DomainError("forbidden", "The linked item is no longer readable; read the reminder again")

    def require_recipient(self, access, recipient_id):
        if not access.manager and recipient_id != access.ctx.principal_id:
            raise DomainError("forbidden", "Only the owner and Ordubashy may set reminders for another recipient")

    def recipient_ok(self, uow, recipient_id, business_id, memo=None):
        """The recipient is the active owner, or an active agent that can act in the reminder's scope."""
        key = (recipient_id, business_id)
        if memo is not None and key in memo:
            return memo[key]
        found = self._recipient_ok(uow, recipient_id, business_id)
        if memo is not None:
            memo[key] = found
        return found

    def _recipient_ok(self, uow, recipient_id, business_id):
        principal = uow.connection.execute("SELECT kind,active FROM principals WHERE id=?", (recipient_id,)).fetchone()
        if principal is None or not principal["active"]:
            return False
        if principal["kind"] == "human":
            return is_active_installation_owner(uow, recipient_id, "human")
        if principal["kind"] != "agent":
            return False
        if business_id is None:
            return active_global(uow.connection, recipient_id) is not None
        try:
            ctx = self.app.policy.context_for(recipient_id, business_id=business_id, review_business_id=None, uow=uow)
        except DomainError:
            return False
        return business_id in ctx.allowed_business_ids

    def permitted(self, uow, principal_id, business_id, operation):
        """Whether ``principal_id`` may *currently* run ``operation`` in the scope: an explicit deny stays effective."""
        try:
            ctx = self.app.policy.context_for(principal_id, business_id=business_id, review_business_id=None, uow=uow)
            decision = self.app.authorize(ctx, self.app.registry.get(operation), (), uow,
                                          namespace="business:" + business_id if business_id else "global")
        except DomainError:
            return False
        return decision.mode == "allow"

    def authority_ok(self, uow, row):
        """The principal that last authorized the schedule or recipient may still do so: the owner, Ordubashy, or the agent
        itself for its own reminder, and not denied. ``creator_id`` is origin only and grants nothing over a new recipient."""
        authority = row["authority_principal_id"]
        principal = uow.connection.execute("SELECT kind,active FROM principals WHERE id=?", (authority,)).fetchone()
        if principal is None or not principal["active"]:
            return False
        if principal["kind"] == "human":
            return is_active_installation_owner(uow, authority, "human")
        if principal["kind"] != "agent":
            return False
        if authority != row["recipient_id"] and authority != configured_ordubashy(uow):
            return False
        return self.permitted(uow, authority, row["business_id"], CREATE)

    def link_ok(self, uow, row):
        """The saved linked item, when there is one, is readable *now* by the authorizing principal and the recipient."""
        link = stored_link(row)
        if link is None:
            return True
        for principal_id in sorted({row["authority_principal_id"], row["recipient_id"]}):
            ctx = self.principal_context(uow, principal_id)
            if ctx is None or self.read_link(ctx, link, uow) is None:
                return False
        return True

    def deliverable(self, uow, row):
        """The one shared delivery-eligibility rule; delivery and connector admission only consult it through the occurrence."""
        return (self.recipient_ok(uow, row["recipient_id"], row["business_id"]) and self.authority_ok(uow, row)
                and self.link_ok(uow, row))

    def reachable(self, access, row, uow, memo=None):
        """Managers reach every reminder; any other agent only its own, while it can still act in that scope."""
        return access.manager or (row["recipient_id"] == access.ctx.principal_id
                                  and self.recipient_ok(uow, row["recipient_id"], row["business_id"], memo))

    # ----- linked items and views ------------------------------------------------------------------------------------

    def read_link(self, ctx, link, uow):
        """The Focus card of a reference as ``ctx`` may read it, or None. The existing item resolver decides readability."""
        focus = getattr(self.runtime, "focus", None)
        if focus is None:
            return None
        try:
            return focus.resolve(ctx, {"type": link["type"], "id": link["id"], "business_id": link["business_id"]}, uow)
        except (DomainError, KeyError, TypeError, ValueError, LookupError):
            return None

    def check_link(self, access, recipient_id, link, uow):
        """Both the creator and the recipient must be able to read the linked item."""
        if self.read_link(access.ctx, link, uow) is None:
            raise DomainError("validation_error", "The linked item is unavailable to you")
        if recipient_id != access.ctx.principal_id:
            peer = self.principal_context(uow, recipient_id)
            if peer is None or self.read_link(peer, link, uow) is None:
                raise DomainError("validation_error", "The linked item is unavailable to the recipient")

    def principal_context(self, uow, principal_id):
        """The principal's current invocation context from stored authority, or None when it cannot act."""
        try:
            return self.app.policy.context_for(principal_id, business_id=None, review_business_id=None, uow=uow)
        except DomainError:
            return None

    def names(self, uow, rows, memo):
        wanted = {identity for row in rows for identity in (row["recipient_id"], row["creator_id"]) if identity not in memo}
        for identity in wanted:
            found = uow.connection.execute("SELECT display_name FROM principals WHERE id=?", (identity,)).fetchone()
            memo[identity] = found["display_name"] if found else None
        return memo

    def view(self, ctx, row, uow, memo):
        """The caller's view; the linked reference and label appear only while this caller can read the item right now."""
        link, available = stored_link(row), True
        if link is not None:
            key = (ctx.principal_id, link["type"], link["id"], link["business_id"])
            if key not in memo["links"]:
                memo["links"][key] = self.read_link(ctx, link, uow)
            card = memo["links"][key]
            available = card is not None
            # A reference this viewer cannot read now is not shown at all, only flagged unavailable.
            link = {**link, "label": card.get("label")} if card is not None else None
        result = record(row, names=self.names(uow, [row], memo["names"]), link=link, link_available=available)
        installation = uow.repository("installation").get("service")
        if (installation and row["recipient_id"] == installation["owner_principal_id"]
                and not any(extension.owner_notifications() is not None for extension in self.runtime.selection.extensions)):
            result["notification_delivery"] = {"state": "unavailable", "reason": "owner_transport_not_installed"}
        return result

    @staticmethod
    def memo():
        return {"names": {}, "links": {}, "recipients": {}}

    def load(self, uow, reminder_id):
        row = uow.connection.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        return dict(row) if row else None

    def required(self, access, reminder_id, uow):
        row = self.load(uow, reminder_id)
        if row is None or not self.reachable(access, row, uow):
            raise DomainError("not_found", "No such reminder")
        return row

    @staticmethod
    def revision(row, expected):
        if row["revision"] != expected:
            raise DomainError("revision_conflict", "Reminder changed; read it again and use the current revision",
                              current_revision=row["revision"])

    # ----- reads -----------------------------------------------------------------------------------------------------

    def get(self, ctx, args, options, uow):
        access = self.caller(ctx, uow)
        row = self.required(access, args["reminder_id"], uow)
        return {"reminder": self.view(access.ctx, row, uow, self.memo())}

    def list(self, ctx, args, options, uow):
        access = self.caller(ctx, uow)
        ctx = access.ctx
        everyone = bool(args.get("all_recipients"))
        if everyone and args.get("recipient_id"):
            raise DomainError("validation_error", "Choose a recipient_id or all_recipients, not both")
        recipient = args.get("recipient_id") or ctx.principal_id
        if (everyone or recipient != ctx.principal_id) and not access.manager:
            raise DomainError("forbidden", "Agents may list only their own reminders")
        states = list(args.get("states") or ACTIVE)
        where, values = ["state IN (" + ",".join("?" for _ in states) + ")"], list(states)
        if not everyone:
            where.append("recipient_id=?")
            values.append(recipient)
        if not args.get("all_scopes"):
            where.append("business_id IS ?")
            values.append(ctx.business_id)
        binding, after = page_position(args, self.namespace(ctx) + "/" + ctx.principal_id, LIST)
        memo = self.memo()
        items, cursor = self.scan(uow, " AND ".join(where), values, DUE_SORT, False, binding, after, args.get("limit", DEFAULT_PAGE),
                                  lambda row: self.reachable(access, row, uow, memo["recipients"]))
        return {"reminders": [self.view(ctx, row, uow, memo) for row in items], "next_cursor": cursor}

    def scan(self, uow, where, values, key, descending, binding, after, want, accept):
        """A bounded keyset page over ``reminders``: at most SCAN_LIMIT rows are examined, whatever the visibility filter drops."""
        if after is not None and (len(after) != 2 or any(not isinstance(item, str) for item in after)):
            raise DomainError("validation_error", "Invalid or differently scoped reminder cursor")
        order, comparison = ("DESC", "<") if descending else ("ASC", ">")
        found, scanned, exhausted, last = [], 0, False, after
        while len(found) <= want and scanned < SCAN_LIMIT:
            clause, parameters = where, list(values)
            if last is not None:
                clause += f" AND ({key} {comparison} ? OR ({key} = ? AND id {comparison} ?))"
                parameters += [last[0], last[0], last[1]]
            rows = uow.connection.execute(f"SELECT *, {key} AS sort_at FROM reminders WHERE {clause} ORDER BY {key} {order}, id {order} LIMIT ?",
                                          [*parameters, SCAN_CHUNK]).fetchall()
            for row in rows:
                scanned += 1
                last = [row["sort_at"], row["id"]]
                if accept(row):
                    found.append(dict(row))
                if len(found) > want:
                    break
            if len(rows) < SCAN_CHUNK and len(found) <= want:
                exhausted = True
                break
        page = found[:want]
        if len(found) > want:
            end = [page[-1]["sort_at"], page[-1]["id"]]
        elif exhausted:
            end = None
        else:
            end = last
        cursor = (base64.urlsafe_b64encode(canonical_bytes({"binding": binding, "after": end})).decode("ascii")
                  if end is not None else None)
        return page, cursor

    # ----- writes ------------------------------------------------------------------------------------------------------

    def prepare(self, operation, ctx, args, uow):
        access = self.caller(ctx, uow)
        self.mutate(operation, access, args, uow, apply=False)
        return PlanBuilder(access.ctx, operation, args, "Manage reminder", namespace=self.namespace(access.ctx)).build()

    def apply(self, ctx, plan, uow):
        return self.mutate(plan.operation_name, self.caller(ctx, uow), plain(plan.arguments), uow, apply=True)

    def mutate(self, operation, access, args, uow, *, apply):
        """Validate against current state; with ``apply`` also write. The same checks run at prepare and at apply."""
        now = self.clock()
        if operation == CREATE:
            row = self.new_row(access, args, now, uow)
            if apply:
                self.insert(uow, row)
            return {"reminder": self.view(access.ctx, row, uow, self.memo())} if apply else None
        row = self.required(access, args["reminder_id"], uow)
        self.revision(row, args["revision"])
        if operation == DISMISS:
            changes = self.dismissal(row, args)
        elif operation == CANCEL:
            changes = self.cancellation(row)
        elif operation == SNOOZE:
            changes = self.snoozing(row, args, now, access.ctx.principal_id)
        else:
            changes = self.edit(access, row, args, now, uow)
        if not apply or not changes:
            return {"reminder": self.view(access.ctx, row, uow, self.memo())} if apply else None
        close = changes.pop("close", None)
        if close and row["current_occurrence_id"]:
            self.close_occurrence(uow, row["current_occurrence_id"], close, timing.stamp(now))
        row = self.save(uow, row, changes, timing.stamp(now))
        return {"reminder": self.view(access.ctx, row, uow, self.memo())}

    def new_row(self, access, args, now, uow):
        ctx = access.ctx
        recipient = args.get("recipient_id") or ctx.principal_id
        self.require_recipient(access, recipient)
        if not self.recipient_ok(uow, recipient, ctx.business_id):
            raise DomainError("validation_error", "That recipient cannot receive reminders in this scope")
        text = self.clean_text(args["text"])
        schedule, name, upcoming = self.first_boundary(args, now)
        link = args.get("linked_item")
        if link is not None:
            self.check_link(access, recipient, link, uow)
        stamp = timing.stamp(now)
        return {"id": new_id(), "revision": 1, "business_id": ctx.business_id, "creator_id": ctx.principal_id,
                "authority_principal_id": ctx.principal_id, "recipient_id": recipient, "text": text,
                "linked_item_json": json.dumps(link, sort_keys=True) if link is not None else None,
                "time_critical": int(bool(args.get("time_critical"))), "schedule": schedule, "timezone": name, "state": "scheduled",
                "generation": 1, "next_at": timing.stamp(upcoming), "snoozed_until": None, "due_at": None,
                "current_occurrence_id": None, "defer_until": None, "created_at": stamp, "updated_at": stamp}

    @staticmethod
    def clean_text(value):
        text = value.strip()
        if not 1 <= len(text) <= 240:
            raise DomainError("validation_error", "Reminder text must be 1 to 240 characters")
        return text

    @staticmethod
    def future(value, now, label="Reminder time"):
        when = timing.parse_at(value, label)
        if when <= now:
            raise DomainError("validation_error", "Choose a time in the future")
        return when

    def first_boundary(self, args, now):
        """(schedule, timezone, first future boundary). A recurrence wins over a stray ``at``."""
        if args.get("recurrence"):
            schedule, name = timing.normalize_recurrence(args["recurrence"])
            return schedule, name, timing.first_slot_after(schedule, name, now)
        if not args.get("at"):
            raise DomainError("validation_error", "Give at for a one-off reminder or recurrence to repeat it")
        return None, None, self.future(args["at"], now)

    def edit(self, access, row, args, now, uow):
        if row["state"] not in ACTIVE:
            raise DomainError("invalid_transition", "Only an active reminder can be changed")
        changes, retime, stamp = {}, False, timing.stamp(now)
        if "text" in args:
            changes["text"] = self.clean_text(args["text"])
        if "time_critical" in args:
            changes["time_critical"] = int(bool(args["time_critical"]))
        recipient = args.get("recipient_id", row["recipient_id"])
        moved = recipient != row["recipient_id"]
        if moved:
            self.require_recipient(access, recipient)
            if not self.recipient_ok(uow, recipient, row["business_id"]):
                raise DomainError("validation_error", "That recipient cannot receive reminders in this scope")
            changes["recipient_id"] = recipient
        link = args["linked_item"] if "linked_item" in args else stored_link(row)
        if link is not None and ("linked_item" in args or moved):
            self.check_link(access, recipient, link, uow)
        if "linked_item" in args:
            changes["linked_item_json"] = json.dumps(link, sort_keys=True) if link is not None else None
        schedule, name = row["schedule"], row["timezone"]
        if args.get("recurrence"):
            wanted = timing.normalize_recurrence(args["recurrence"])
            if wanted != (schedule, name):
                schedule, name, retime = wanted[0], wanted[1], True
        elif "recurrence" in args:
            if schedule is not None:
                if not args.get("at"):
                    raise DomainError("validation_error", "A one-off reminder needs at")
                schedule, name, retime = None, None, True
        if not args.get("recurrence") and args.get("at"):
            if schedule is not None:
                raise DomainError("validation_error", "Snooze a repeating reminder, or set recurrence to null with at to make it one-off")
            when = timing.stamp(self.future(args["at"], now))
            if when != row["next_at"] or row["state"] == "due":
                changes["next_at"], retime = when, True
        if retime or moved:
            # Origin stays creator_id; the actor who authorized this schedule or recipient is the one delivery rechecks.
            changes["authority_principal_id"] = access.ctx.principal_id
        if retime:
            changes.update(schedule=schedule, timezone=name, state="scheduled", current_occurrence_id=None, snoozed_until=None,
                           generation=row["generation"] + 1, close="rescheduled")
            if schedule is not None:
                changes["next_at"] = timing.stamp(timing.first_slot_after(schedule, name, now))
        elif moved:
            changes["generation"] = row["generation"] + 1
            if row["state"] == "due":
                changes.update(state="scheduled", current_occurrence_id=None, next_at=stamp, close="recipient_changed")
        if not any(key != "close" and row.get(key) != value for key, value in changes.items()):
            return {}
        return changes

    @staticmethod
    def snoozing(row, args, now, actor):
        if row["state"] not in ACTIVE:
            raise DomainError("invalid_transition", "Only an active reminder can be snoozed")
        until = ReminderService.future(args["at"], now, "Snooze time")
        return {"state": "scheduled", "snoozed_until": timing.stamp(until), "next_at": timing.stamp(until),
                "current_occurrence_id": None, "generation": row["generation"] + 1, "defer_until": None,
                "authority_principal_id": actor, "close": "snoozed"}

    @staticmethod
    def dismissal(row, args):
        if row["state"] != "due" or row["current_occurrence_id"] != args["occurrence_id"]:
            raise DomainError("invalid_transition", "That reminder occurrence is no longer current; refresh before dismissing")
        # Only this occurrence clears: a repeating reminder keeps its next boundary, a one-off is finished.
        return {"state": "scheduled" if row["next_at"] else "dismissed", "current_occurrence_id": None, "close": "dismissed"}

    @staticmethod
    def cancellation(row):
        if row["state"] not in ACTIVE:
            raise DomainError("invalid_transition", "This reminder is already finished")
        return {"state": "cancelled", "current_occurrence_id": None, "snoozed_until": None, "defer_until": None, "close": "cancelled"}

    def insert(self, uow, row):
        columns = list(row)
        uow.connection.execute(f"INSERT INTO reminders({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(row.values()))

    def save(self, uow, row, changes, stamp):
        changes = {**changes, "revision": row["revision"] + 1, "updated_at": stamp}
        cursor = uow.connection.execute(
            "UPDATE reminders SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=? AND revision=?",
            [*changes.values(), row["id"], row["revision"]])
        if cursor.rowcount != 1:
            raise DomainError("revision_conflict", "Reminder changed; read it again and use the current revision")
        return self.load(uow, row["id"])

    @staticmethod
    def close_occurrence(uow, occurrence_id, reason, stamp):
        uow.connection.execute("UPDATE reminder_occurrences SET closed_at=?, close_reason=? WHERE id=? AND close_reason IS NULL",
                               (stamp, reason, occurrence_id))

    # ----- occurrence validity, for delivery ---------------------------------------------------------------------------

    def payload(self, row, occurrence):
        return {"reminder_id": row["id"], "occurrence_id": occurrence["id"], "revision": row["revision"],
                "recipient_id": row["recipient_id"], "creator_id": row["creator_id"], "business_id": row["business_id"],
                "text": row["text"], "due_at": occurrence["due_at"], "linked_item": stored_link(row),
                "time_critical": bool(row["time_critical"]), "recurring": row["schedule"] is not None}

    def current_occurrence(self, uow, occurrence_id):
        """The canonical payload while the occurrence is still the reminder's current due one, its recipient and scheduling
        authority remain allowed and both can still read the saved link; None for anything stale, superseded, snoozed, dismissed,
        cancelled or revoked. The due record and pending delivery are kept while it is None. An internal validity lookup, not
        public authorization."""
        occurrence = uow.connection.execute("SELECT * FROM reminder_occurrences WHERE id=?", (occurrence_id,)).fetchone()
        if occurrence is None or occurrence["close_reason"] is not None:
            return None
        row = self.load(uow, occurrence["reminder_id"])
        if (row is None or row["state"] != "due" or row["current_occurrence_id"] != occurrence_id
                or row["recipient_id"] != occurrence["recipient_id"] or not self.deliverable(uow, row)):
            return None
        return self.payload(row, occurrence)

    def visible_occurrence(self, ctx, uow, occurrence_id):
        """The payload for the occurrence's own recipient under current permissions; None when the recipient cannot currently read
        the saved link (``current_occurrence`` has already required the same of the authorizing principal). Manager read rights
        never make another recipient's notice visible."""
        payload = self.current_occurrence(uow, occurrence_id)
        if payload is None or ctx.principal_id != payload["recipient_id"]:
            return None
        try:
            ctx = self.app.current_context(ctx, uow)
            if ctx.delegation_assignment_id is not None:
                return None
            business = None if ctx.principal_kind == "human" else payload["business_id"]
            selected = self.app.current_context(replace(ctx, business_id=business, review_business_id=business), uow)
            decision = self.app.authorize(selected, self.app.registry.get(GET), (), uow,
                                          namespace="business:" + business if business else "global")
        except DomainError:
            return None
        if decision.mode != "allow":
            return None
        link = payload["linked_item"]
        if link is not None and self.read_link(ctx, link, uow) is None:
            return None
        return {**payload, "link_available": True}

    # ----- the bounded reconcile pass -------------------------------------------------------------------------------------

    def reconcile(self, uow):
        """Turn due boundaries into occurrences and publish pending ones, within the caller's write unit and a small budget.
        Idle cost is two indexed probes. A failing row is isolated by a savepoint and deferred; it never aborts the pass."""
        stamp = timestamp(self.clock)
        started = time.monotonic()
        now = timing.parse_stamp(stamp)
        fired = self.fire_due(uow, now, stamp, started)
        return {"fired": fired, "published": self.publish_pending(uow, stamp, started) if self.delivery is not None else 0}

    def fire_due(self, uow, now, stamp, started):
        rows = uow.connection.execute(
            "SELECT * FROM reminders WHERE state IN ('scheduled','due') AND next_at IS NOT NULL AND next_at <= ? "
            "AND (defer_until IS NULL OR defer_until <= ?) ORDER BY next_at, id LIMIT ?", (stamp, stamp, FIRE_BATCH)).fetchall()
        fired = 0
        for row in map(dict, rows):
            if time.monotonic() - started > PASS_SECONDS:
                break
            try:
                with savepoint(uow, "reminder_fire"):
                    self.fire(uow, row, now, stamp)
                fired += 1
            except DomainError as error:
                if error.code in {"authority_invalid", "maintenance"}:
                    raise
                self.defer(uow, row["id"], now, error)
            except Exception as error:
                self.defer(uow, row["id"], now, error)
        return fired

    def defer(self, uow, reminder_id, now, error):
        LOG.warning("Reminder %s could not be made due: %s", reminder_id, getattr(error, "code", type(error).__name__))
        uow.connection.execute("UPDATE reminders SET defer_until=? WHERE id=?",
                               (timing.stamp(now + timedelta(seconds=DEFER_SECONDS)), reminder_id))

    def fire(self, uow, row, now, stamp):
        """Create the one occurrence of the latest eligible due moment, superseding an uncleared earlier one."""
        due = timing.parse_stamp(row["next_at"])
        upcoming = None
        if row["schedule"]:
            # Latest-only catch-up after downtime or a snooze, then the original calendar resumes after now.
            due = max(due, timing.latest_slot(row["schedule"], row["timezone"], now))
            upcoming = timing.stamp(timing.first_slot_after(row["schedule"], row["timezone"], now))
        if row["current_occurrence_id"]:
            self.close_occurrence(uow, row["current_occurrence_id"], "superseded", stamp)
        occurrence_id = new_id()
        uow.connection.execute(
            "INSERT INTO reminder_occurrences(id,reminder_id,business_id,recipient_id,generation,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (occurrence_id, row["id"], row["business_id"], row["recipient_id"], row["generation"], timing.stamp(due), stamp))
        self.save(uow, row, {"state": "due", "current_occurrence_id": occurrence_id, "due_at": timing.stamp(due), "next_at": upcoming,
                             "snoozed_until": None, "defer_until": None}, stamp)

    def publish_pending(self, uow, stamp, started):
        """Hand pending current occurrences to delivery, a few per recipient so one unconfigured oldest row cannot starve others."""
        pending = [row["id"] for row in uow.connection.execute(
            "SELECT id FROM (SELECT o.id AS id, o.created_at AS created_at, ROW_NUMBER() OVER (PARTITION BY o.recipient_id "
            "ORDER BY COALESCE(o.retry_at, o.created_at), o.id) AS rn FROM reminder_occurrences o "
            "WHERE o.receipt_json IS NULL AND o.close_reason IS NULL AND (o.retry_at IS NULL OR o.retry_at <= ?)) "
            "WHERE rn <= ? ORDER BY rn, created_at, id LIMIT ?", (stamp, PUBLISH_PER_RECIPIENT, PUBLISH_BATCH))]
        published = 0
        for occurrence_id in pending:
            if time.monotonic() - started > PASS_SECONDS:
                break
            receipt = None
            try:
                with savepoint(uow, "reminder_publish"):
                    payload = self.current_occurrence(uow, occurrence_id)
                    receipt = self.delivery.publish(uow, payload) if payload is not None else None
                    if receipt is not None:
                        self.record_receipt(uow, occurrence_id, receipt, stamp)
            except DomainError as error:
                if error.code in {"authority_invalid", "maintenance"}:
                    raise
                LOG.warning("Reminder delivery deferred for %s: %s", occurrence_id, error.code)
                receipt = None
            except Exception:
                LOG.warning("Reminder delivery deferred for %s", occurrence_id, exc_info=True)
                receipt = None
            if receipt is not None:
                published += 1
                continue
            attempts = uow.connection.execute("SELECT attempts FROM reminder_occurrences WHERE id=?", (occurrence_id,)).fetchone()["attempts"]
            wait = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** min(attempts, 6))
            uow.connection.execute("UPDATE reminder_occurrences SET attempts=attempts+1, retry_at=? WHERE id=?",
                                   (timing.stamp(timing.parse_stamp(stamp) + timedelta(seconds=wait)), occurrence_id))
        return published

    def record_receipt(self, uow, occurrence_id, receipt, stamp):
        if not isinstance(receipt, dict) or receipt.get("kind") not in {"inbox", *(extension.id for extension in self.runtime.selection.extensions if extension.owner_notifications() is not None)}:
            raise DomainError("invalid_receipt", "Reminder delivery returned an unrecognized receipt")
        uow.connection.execute("UPDATE reminder_occurrences SET receipt_json=?, published_at=?, retry_at=NULL WHERE id=? AND receipt_json IS NULL",
                               (json.dumps(plain(receipt), sort_keys=True), stamp, occurrence_id))

    # ----- shared with the legacy wrapper ---------------------------------------------------------------------------------

    def legacy_view(self, row):
        return legacy(row)
