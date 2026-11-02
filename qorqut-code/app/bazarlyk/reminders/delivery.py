"""ReminderDelivery: the one adapter between the shared reminder service and the existing durable delivery paths.

The reminder service (``bazarlyk.reminders.service``) decides which occurrence is current and hands each due one to ``publish``
inside its own write unit. Nothing here schedules, polls, restarts a client or calls a provider:

* an agent recipient receives an ordinary inbox event of source ``reminders``. It is written as a system event (no actor), so a
  reminder an agent created for itself is not dropped as an echo of its own change. The existing nudge backend announces it under
  the existing Focused/Unfocused relevance policy, session availability and pacing;
* the installation owner receives one text in the existing Telegram outbox when that transport is configured. Without it the
  occurrence stays unpublished and the service retries; the owner's due card never depends on Telegram.

What was queued is never trusted later. The service's ``current_occurrence`` (stale, snoozed, dismissed, cancelled, rescheduled or
re-addressed occurrences are gone) is consulted when the Telegram dispatcher is about to send, and its ``visible_occurrence`` is
consulted by the inbox for every read, nudge and acknowledgement, so an invalidated occurrence stops being announced or returned
even though its event or outbox row was already written. Reading or acknowledging the event changes nothing on the reminder.

The module also holds the few pure helpers ``HostInbox`` and ``Attention`` use to resolve a reminder event to its occurrence."""
from dataclasses import replace
import json

from qorqut_operations.contracts import DomainError, Principal, utc_now
from qorqut_operations.discussion import emit_event
from bazarlyk.telegram.outbox import enqueue_text

SOURCE_TYPE = "reminders"
EVENT_TYPE = "reminder_due"
OUTBOX_SOURCE = "reminder_occurrence"
# The owner's urgent destination, exactly where the retired owner reminders were sent.
OWNER_CHAT = "urgent"
# Telegram refusals that only say the transport cannot take a message now; the occurrence stays pending and is offered again.
TRANSPORT_UNAVAILABLE = frozenset({"operation_unavailable", "binding_invalid", "installation_binding_mismatch",
                                   "owner_binding_mismatch", "binding_changed"})
OUTSTANDING_LIMIT = 50
WITHDRAWN = "Reminder occurrence is no longer current"
POINTER_FIELDS = ("reminder_id", "occurrence_id", "revision", "text", "due_at", "time_critical", "linked_item",
                  "business_id", "creator_id")


def event_occurrence(row):
    """The occurrence id a reminder event carries in its visibility JSON, or None."""
    try:
        value = json.loads(row["visibility_json"] or "{}").get("occurrence_id")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def _own(payload, row, occurrence):
    """Whether the service's payload is the one this exact event names: same occurrence, reminder and scope. A payload that
    reports its link as withdrawn (``link_available`` false) is never treated as authorized."""
    return (isinstance(payload, dict) and payload.get("link_available") is not False and payload.get("occurrence_id") == occurrence
            and payload.get("reminder_id") == row["source_id"] and payload.get("business_id") == row["business_id"])


def current_payload(app, uow, row):
    """The canonical payload of the event's occurrence while it is still current, or None. A validity lookup only:
    it grants no access, so callers still check who the recipient is."""
    service, occurrence = getattr(app, "reminders", None), event_occurrence(row)
    if service is None or occurrence is None:
        return None
    payload = service.current_occurrence(uow, occurrence)
    return payload if _own(payload, row, occurrence) else None


def visible_payload(app, ctx, row, uow):
    """The occurrence's payload when ``ctx`` is the recipient, the occurrence is current and its link is readable by the
    recipient now; otherwise None. Manager rights over another recipient never make the notice visible, and a delegated
    assignment never receives its parent's reminders."""
    service, occurrence = getattr(app, "reminders", None), event_occurrence(row)
    if service is None or occurrence is None or ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
        return None
    try:
        payload = service.visible_occurrence(ctx, uow, occurrence)
    except DomainError:
        return None
    return payload if _own(payload, row, occurrence) and payload.get("recipient_id") == ctx.principal_id else None


def pointer(payload):
    """The safe fields an agent's inbox item shows for a reminder; never the linked record's own contents."""
    return {key: payload.get(key) for key in POINTER_FIELDS}


class ReminderDelivery:
    """``publish`` hands one current occurrence to the existing delivery path of its recipient and returns the durable receipt,
    or None while no durable handoff exists (the service offers it again). It performs no network call."""

    def __init__(self, runtime, service):
        self.runtime, self.service = runtime, service
        self.app = runtime.application
        self._attached = False
        self.attach()

    @property
    def bridge(self):
        return getattr(self.runtime, "telegram", None)

    def attach(self):
        """Register the pre-send recheck on the Telegram bridge once it exists (it is constructed before the service)."""
        bridge = self.bridge
        if bridge is not None and not self._attached:
            bridge.admit_hooks[OUTBOX_SOURCE] = self.admit
            self._attached = True

    # ----- handoff -----------------------------------------------------------------------------------------------

    def publish(self, uow, payload):
        uow.require_write()
        current = self.service.current_occurrence(uow, payload["occurrence_id"])
        if current is None:
            return None
        recipient = uow.repository("principals").get(current["recipient_id"])
        if not recipient or not recipient["active"]:
            return None
        if recipient["kind"] == "agent":
            return self.inbox_receipt(uow, current)
        installation = uow.repository("installation").get("service")
        if recipient["kind"] == "human" and installation and installation["owner_principal_id"] == current["recipient_id"]:
            return self.owner_receipt(uow, current)
        # Another person has no delivery route here; the occurrence stays due on the reminder record.
        return None

    def recipient_context(self, uow, agent_id):
        """The recipient's own inbox context, built as the nudge backend builds it, so the handoff check sees what the
        inbox read will see. None when the agent cannot read its inbox now."""
        inbox = getattr(self.app, "inbox", None)
        if inbox is None or getattr(inbox, "resolver", None) is None:
            return None
        try:
            ctx = inbox.context(inbox.resolver.runtime_context(Principal(agent_id, "agent")), uow)
            descriptor = self.app.registry.get("inbox_list")
            if ctx.business_id is not None:
                descriptor = replace(descriptor, global_capability=None)
            return ctx if self.app.policy.resolve(ctx, descriptor, (), uow).mode == "allow" else None
        except DomainError:
            return None

    def inbox_receipt(self, uow, payload):
        recipient, occurrence = payload["recipient_id"], payload["occurrence_id"]
        ctx = self.recipient_context(uow, recipient)
        try:
            visible = self.service.visible_occurrence(ctx, uow, occurrence) if ctx is not None else None
        except DomainError:
            return None
        if visible is None or visible.get("link_available") is False:
            return None
        event_id, stale = self.outstanding(uow, recipient, payload["reminder_id"], occurrence)
        if event_id is None:
            # The compact inbox view keeps only the summary and the source ids, so the summary carries what the agent needs
            # to recognise the reminder; the occurrence and its link are read from the reminder record.
            summary = (("Time-critical reminder" if payload.get("time_critical") else "Reminder")
                       + f" due {payload['due_at']}: {payload['text']}")
            # actor_id=None is a system event: the creator stays on the reminder record, and a self-reminder is delivered.
            event_id = emit_event(uow, business_id=payload["business_id"], source_type=SOURCE_TYPE,
                                  source_id=payload["reminder_id"], source_revision=payload["revision"], actor_id=None,
                                  event_type=EVENT_TYPE, summary=summary, required=[recipient],
                                  visibility={"occurrence_id": occurrence, "due_at": payload["due_at"]})
        if event_id is None:
            return None  # nobody is eligible to receive it now (inactive or out-of-scope agent): retry later
        self.retire(uow, recipient, stale)
        return {"kind": "inbox", "event_id": event_id}

    def outstanding(self, uow, recipient, reminder_id, occurrence):
        """The recipient's unacknowledged event of this exact occurrence (a retried handoff reuses it), and the ids of its
        unacknowledged events of occurrences that are no longer current. Bounded, through the pending-delivery index."""
        found, stale = None, []
        rows = uow.connection.execute(
            "SELECT e.id,e.visibility_json FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
            "WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND e.source_type=? AND e.source_id=? ORDER BY e.id LIMIT ?",
            (recipient, SOURCE_TYPE, reminder_id, OUTSTANDING_LIMIT)).fetchall()
        for row in rows:
            earlier = event_occurrence(row)
            if earlier == occurrence:
                found = row["id"]
            elif earlier is None or self.service.current_occurrence(uow, earlier) is None:
                stale.append(row["id"])
        return found, stale

    @staticmethod
    def retire(uow, recipient, event_ids):
        """Close the delivery of an event whose occurrence was superseded, so a recurring reminder never accumulates pending
        rows. The event is already invisible to every read; this only keeps the agent's pending set small."""
        if event_ids:
            uow.connection.executemany(
                "UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE agent_id=? AND event_id=? AND acknowledged_at IS NULL",
                [(utc_now(), WITHDRAWN, recipient, event_id) for event_id in event_ids])

    def owner_receipt(self, uow, payload):
        bridge = self.bridge
        if bridge is None:
            return None
        self.attach()
        try:
            # The occurrence id is the outbox source key: a repeated handoff returns the first row, never a second message.
            _, row, _ = enqueue_text(bridge, uow, bridge.config.chat(OWNER_CHAT), text=payload["text"].strip(), purpose="notice",
                                     source_kind=OUTBOX_SOURCE, source_key=payload["occurrence_id"])
        except DomainError as error:
            if error.code in TRANSPORT_UNAVAILABLE:
                return None  # unconfigured or unready Telegram: the owner's due card stands and the handoff is retried
            raise
        return {"kind": "telegram", "outbox_id": row["id"]}

    # ----- Telegram admission ------------------------------------------------------------------------------------

    def admit(self, uow, row):
        """Recheck the occurrence immediately before the dispatcher claims the queued owner text.

        A dismissed, snoozed, rescheduled, cancelled, superseded or re-addressed occurrence is closed unsent. A text already
        sent to Telegram cannot be withdrawn; this only stops one that has not been claimed."""
        try:
            payload = self.service.current_occurrence(uow, row["source_key"])
        except DomainError:
            return "cancelled", "reminder_unavailable", "The reminder occurrence could not be verified"
        if payload is None:
            return "cancelled", "reminder_invalidated", "The reminder occurrence is no longer current"
        installation = uow.repository("installation").get("service")
        if not installation or payload.get("recipient_id") != installation["owner_principal_id"]:
            return "cancelled", "recipient_changed", "The reminder is no longer addressed to the installation owner"
        return None


__all__ = ["EVENT_TYPE", "OUTBOX_SOURCE", "ReminderDelivery", "SOURCE_TYPE", "current_payload", "event_occurrence", "pointer",
           "visible_payload"]
