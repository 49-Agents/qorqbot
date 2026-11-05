"""Trusted internal owner -> agent delivery into the canonical Messages store.

This is deliberately not ``agents_message_send`` and not the Dev-only terminal capability: only the Telegram bridge,
after it has authenticated the exact owner ``user.id`` in the exact bound chat, may record a message whose sender is
the installation owner. Nothing here is exposed as a tool.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.conversations import normalize_summary

SUMMARY_LIMIT = 160
MAX_BODY = 16000


def deliver_owner_message(uow, *, owner_id, agent_id, body, created_at, message_id=None, summary=None):
    """Insert one canonical owner->agent message and its inbox delivery. Returns ``(message_id, event_id)``.

    ``owner_id`` is the Qorqut owner the bridge is pinned to. The message is attributed to that principal and to no other:
    if the installation now names a different owner, nothing is written and the sender is never silently changed.
    """
    installation = uow.repository("installation").get("service")
    if not installation:
        raise DomainError("operation_unavailable", "Installation identity is missing")
    if installation["owner_principal_id"] != owner_id:
        raise DomainError("owner_binding_mismatch", "The installation owner is not the owner the Telegram bridge is bound to")
    owner = uow.repository("principals").get(owner_id)
    agent = uow.repository("principals").get(agent_id)
    profile = uow.repository("agent_profiles", business_id=None).get(agent_id)
    if (not owner or not owner["active"] or owner["kind"] != "human" or not agent or not agent["active"]
            or agent["kind"] != "agent" or not profile or profile["status"] != "active"):
        raise DomainError("agent_unavailable", "The owner or the target agent is not active")
    if not body or len(body) > MAX_BODY:
        raise DomainError("validation_error", "A canonical message needs 1-16000 characters")
    identity = message_id or new_id()
    summary = (summary or body)[:SUMMARY_LIMIT]
    event_id = uow.connection.execute(
        "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)",
        (profile["home_business_id"], "agent_messages", identity, owner_id, "direct_message", summary, created_at)).lastrowid
    uow.connection.execute(
        "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,"
        "current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, event_id, profile["home_business_id"], owner_id, agent_id, None, identity, body, created_at,
         normalize_summary(None), owner_id, agent_id))
    uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (agent_id, event_id))
    return identity, event_id
