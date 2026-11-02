"""Authority metadata of the inbox, direct-message and broadcast operations, as data in the order the host catalog lists them.

The three runs are not contiguous in the host catalog (the host's own email, management and owner-conversation entries sit
between them), so the host splices each tuple at its existing position instead of receiving one list. Names, effect kinds and
exposure are the deployed ones; only the handlers are installed separately, when the inbox service, ``Messages`` and
``Broadcasts`` register."""

from __future__ import annotations

from qorqut_operations.application.registry import CatalogEntry

# Inbox and message mutations, then the broadcast mutations: one run inside the host's ``records`` entries.
MUTATION_ENTRIES = tuple(CatalogEntry(name, False, "records") for name in (
    "inbox_acknowledge", "inbox_subscribe", "inbox_unsubscribe", "agents_message_send",
    "broadcasts_create", "broadcasts_update", "broadcasts_pause", "broadcasts_resume", "broadcasts_cancel", "broadcasts_send_now"))

# The inbox and direct-message reads, which precede the host's management reads.
READ_ENTRIES = tuple(CatalogEntry(name, False, "read") for name in (
    "inbox_list", "inbox_subscriptions_list", "agents_messages_list", "agents_message_recipients"))

# The broadcast reads, a separate run of the host catalog.
BROADCAST_READ_ENTRIES = tuple(CatalogEntry(name, False, "read") for name in (
    "broadcasts_list", "broadcasts_get", "broadcast_recipients"))
