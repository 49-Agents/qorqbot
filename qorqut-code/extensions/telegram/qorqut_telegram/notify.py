"""Urgent Notifications: deterministic event dedupe, one known post per active event and one recovery update.

Only actionable failures, availability or data-loss problems and time-sensitive changes belong here (what happened,
impact, needed action). Identical active events are suppressed; a material change or a recovery updates the known post
when it has a provider message and is otherwise posted fresh. Nothing is ever dropped by a time-based rate limit: a
full backlog is reported back to the caller as retryable backpressure instead of silently discarding an event.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import re
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError, canonical_hash
from . import store
from .outbox import enqueue_edit, enqueue_text

BACKLOG_LIMIT = 100
TITLE_LIMIT, TEXT_LIMIT, REASON_LIMIT, LINK_LIMIT = 120, 3500, 500, 500
EVENT_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@#=+-]{0,199}$"
INPUT_SCHEMA = {
    "event_key": {"type": "string", "pattern": EVENT_KEY_PATTERN, "maxLength": 200,
                  "description": instruction_text('notify-stable-identifier-of-the-condition-the-same-key')},
    "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT},
    "text": {"type": "string", "minLength": 1, "maxLength": TEXT_LIMIT,
             "description": instruction_text('notify-what-happened-the-impact-and-the-action-needed')},
    "reason": {"type": "string", "minLength": 1, "maxLength": REASON_LIMIT, "description": instruction_text('notify-why-the-owner-must-know-now')},
    "link": {"type": "string", "maxLength": LINK_LIMIT, "description": instruction_text('notify-optional-credential-free-http-s-link')},
    "state": {"enum": ["firing", "resolved"]},
}
REQUIRED = ("event_key", "title", "text", "reason")
LIMITS = {"event_key": 200, "title": TITLE_LIMIT, "text": TEXT_LIMIT, "reason": REASON_LIMIT}
EVENT_KEY = re.compile(EVENT_KEY_PATTERN)


def clean_link(link):
    if link is None:
        return None
    if type(link) is not str or len(link) > LINK_LIMIT or any(ord(c) < 33 or ord(c) == 127 for c in link):
        raise DomainError("validation_error", "The link must be a short URL without spaces or control characters")
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise DomainError("validation_error", "The link must be a credential-free HTTP(S) URL")
    return link


def render(args, state):
    head = "URGENT" if state == "firing" else "RESOLVED"
    lines = [f"{head}: {args['title'].strip()}", "", args["text"].strip(), "", f"Why now: {args['reason'].strip()}"]
    if args.get("link"):
        lines.append(f"Details: {args['link']}")
    text = "\n".join(lines)
    if len(text) > store.MAX_UNICODE_CHARS or store.utf16_units(text) > store.MAX_UTF16_UNITS:
        raise DomainError("validation_error", "The notification is too long for one Telegram message; shorten the text")
    return text


def validate(args):
    if not isinstance(args, dict):
        raise DomainError("validation_error", "A notification needs event_key, title, text and reason")
    for name in REQUIRED:
        if type(args.get(name)) is not str or not args[name].strip():
            raise DomainError("validation_error", f"{name} must be non-empty text")
        if len(args[name]) > LIMITS[name]:
            raise DomainError("validation_error", f"{name} is longer than {LIMITS[name]} characters")
    if not EVENT_KEY.fullmatch(args["event_key"]):
        raise DomainError("validation_error", "event_key must be a short identifier of letters, digits and ._:/@#=+-")
    state = args.get("state", "firing")
    if state not in ("firing", "resolved"):
        raise DomainError("validation_error", "state must be firing or resolved")
    return {**{name: args[name] for name in REQUIRED}, "link": clean_link(args.get("link"))}, state


def source_key(producer, event_key, sequence):
    return f"{canonical_hash([producer, event_key])[:32]}:{sequence}"


def supersede(uow, message_id, producer, event_key, sequence):
    """Withdraw work of this event that is still queued for ``message_id`` and older than ``sequence``.

    Only the newest content may reach Telegram, so a queued original or a queued edit that failed before it was sent (and is
    waiting to retry) is cancelled instead of overwriting a newer change or recovery. Work that is already dispatching or
    uncertain is never touched here; ``admit`` rechecks whatever comes back later.
    """
    for row in uow.connection.execute(
            "SELECT * FROM telegram_outbox WHERE source_kind='notify' AND message_id=? AND state='queued' AND event_producer=? "
            "AND event_key=? AND event_sequence<?", (message_id, producer, event_key, sequence)).fetchall():
        store.set_outbox(uow, store.decode(row), state="cancelled", error_code="superseded",
                         error_message="A newer update of this event replaced it before it was sent")


def admit(uow, row):
    """Right before dispatch: older content never overwrites a newer material change or recovery of the same message.

    An owner ``retry`` of an old uncertain edit lands here too, so resolving an old receipt cannot resend obsolete content.
    """
    if row["event_sequence"] is None:
        return None
    event = uow.repository("telegram_events").get((row["event_producer"], row["event_key"]))
    if event is not None and event["message_id"] == row["message_id"] and event["occurrences"] > row["event_sequence"]:
        return "cancelled", "superseded", "A newer update of this event replaced it before it was sent"
    return None


def notify(bridge, uow, producer, args, *, gateway_producer=None):
    """Record one event transition. Returns ``{action, message_id, state}``; the caller has already authorized it.

    ``producer`` names the caller in the dedupe namespace (``owner``, ``principal:<id>`` or ``gateway:<id>``);
    ``gateway_producer`` is set only for a configured gateway credential so revocation can cancel its queued sends.
    """
    config = bridge.config
    if not config.enabled:
        raise DomainError("operation_unavailable", "The Telegram bridge is not enabled")
    bridge.check_binding(uow)
    clean, state = validate(args)
    text = render(clean, state)
    fingerprint = canonical_hash({**clean, "state": state})
    now = bridge.now()
    chat = config.chat("urgent")
    events = uow.repository("telegram_events")
    row = events.get((producer, clean["event_key"]))
    if row is not None and row["state"] == state and row["content_hash"] == fingerprint:
        last = store.outbox(uow, row["last_outbox_id"]) if row["last_outbox_id"] else None
        # An earlier attempt that failed or was cancelled never reached the owner; repeating the event posts it again.
        if last is None or last["state"] not in ("failed", "cancelled"):
            return {"action": "suppressed", "message_id": row["message_id"], "state": state}
    if row is None and state == "resolved":
        # A recovery for a condition never reported: nothing to update and nothing to announce.
        events.insert({"producer_id": producer, "event_key": clean["event_key"], "state": state, "content_hash": fingerprint,
                       "first_seen_at": now, "last_changed_at": now, "created_at": now, "updated_at": now})
        return {"action": "ignored", "message_id": None, "state": state}
    if store.pending_notify_count(uow, gateway_producer) >= BACKLOG_LIMIT:
        raise DomainError("busy", "The urgent notification backlog is full; retry after Telegram delivery recovers",
                          retryable=True)
    generation = (store.get_state(uow) or {}).get("config_generation", 1)
    sequence = (row["occurrences"] + 1) if row else 1
    key = source_key(producer, clean["event_key"], sequence)
    event = (producer, clean["event_key"], sequence)
    known = store.message(uow, row["message_id"]) if row and row["message_id"] else None
    # A post at another topic (or chat) than the one now configured for urgent events is never edited there: the event posts afresh.
    fresh = row is None or row["state"] == "resolved" or known is None or not store.in_destination(known, chat)
    if not fresh:
        # A queued original, or a queued edit waiting to retry, is withdrawn so only the current content is sent.
        supersede(uow, known["id"], producer, clean["event_key"], sequence)
    if not fresh and known["telegram_message_id"] is not None:
        outbox = enqueue_edit(bridge, uow, chat, known, text, source_kind="notify", source_key=key, producer_id=gateway_producer,
                              generation=generation, event=event)
        message_id, action = known["id"], "updated"
    else:
        message, outbox, _ = enqueue_text(bridge, uow, chat, text=text, purpose="urgent", source_kind="notify", source_key=key,
                                          producer_id=gateway_producer, event=event)
        message_id, action = message["id"], "posted" if fresh else "updated"
    values = {"state": state, "content_hash": fingerprint, "message_id": message_id, "last_outbox_id": outbox["id"],
              "occurrences": sequence, "last_changed_at": now}
    if row is None:
        events.insert({"producer_id": producer, "event_key": clean["event_key"], "first_seen_at": now, "created_at": now,
                       "updated_at": now, **values})
    else:
        events.update((producer, clean["event_key"]), values, expected_revision=row["revision"])
    bridge.wake()
    return {"action": action, "message_id": message_id, "state": state}
