"""The standing policy of the inbox operations, as data.

Every agent may use these operations whatever its role; each one still checks the caller's current scope and authorization
when it runs, so the default only removes the need for a role grant. The host reads ``INBOX_OPERATIONS`` at the position it
already holds among its standing defaults (after an explicit override, before the shared-context reads) and keeps no copy of the
names; ``core_default`` is the same decision as data."""

from __future__ import annotations

from qorqut_operations.contracts import PolicyDecision

INBOX_OPERATIONS = frozenset({
    "inbox_list", "inbox_acknowledge", "inbox_subscribe",
    "inbox_unsubscribe", "inbox_subscriptions_list",
    "agents_message_send", "agents_messages_list", "agents_message_recipients",
    "broadcasts_create", "broadcasts_update", "broadcasts_pause", "broadcasts_resume", "broadcasts_cancel", "broadcasts_send_now",
    "broadcasts_list", "broadcasts_get", "broadcast_recipients",
})


def core_default(name):
    """The standing allow for an inbox operation, or None when ``name`` is not one."""
    return PolicyDecision("allow", "core_default") if name in INBOX_OPERATIONS else None
