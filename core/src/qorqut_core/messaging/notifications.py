"""The ASCII terminal pointer to an exact durable message, never its body."""

from uuid import UUID


def notification_text(message_id: UUID) -> str:
    """Return the complete terminal payload, without a newline or metadata."""
    if not isinstance(message_id, UUID):
        raise TypeError("message_id must be a UUID")
    return f"[Qorqut inbox {message_id}]"
