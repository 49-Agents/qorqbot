"""SQL-only immutable message and separate delivery/handling views."""

from ..contracts import CoreError
from .models import DeliveryView, HandlingView, MessageView


def message(tx, message_id):
    row = tx.connection.execute("SELECT * FROM messages WHERE message_id=?", (str(message_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "Message not found")
    return MessageView.model_validate(dict(row))


def thread(tx, thread_id):
    row = tx.connection.execute("SELECT * FROM message_threads WHERE thread_id=?", (str(thread_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "Message thread not found")
    return row


def delivery(tx, message_id):
    row = tx.connection.execute("SELECT * FROM message_deliveries WHERE message_id=?", (str(message_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "Message delivery not found")
    return DeliveryView.model_validate(dict(row))


def handling(tx, message_id):
    row = tx.connection.execute("SELECT * FROM message_handling WHERE message_id=?", (str(message_id),)).fetchone()
    return HandlingView.model_validate(dict(row)) if row else None
