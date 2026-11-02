"""Safe reminder views: the shared operation shape, and the plain shape the legacy owner reminder tools return."""
import json


def stored_link(row):
    """The scoped reference saved with a reminder, or None."""
    value = row["linked_item_json"]
    if value is None:
        return None
    link = json.loads(value) if isinstance(value, str) else value
    return {"type": link["type"], "id": link["id"], "business_id": link.get("business_id")}


def record(row, *, names, link=None, link_available=True):
    """One reminder as callers see it. ``link`` is the already-authorized reference (with its label when readable)."""
    due = row["state"] == "due"
    recurrence = {"schedule": row["schedule"], "timezone": row["timezone"]} if row["schedule"] else None
    upcoming = row["next_at"] if row["state"] in ("scheduled", "due") else None
    return {
        "reminder_id": row["id"], "revision": row["revision"], "text": row["text"],
        "recipient_id": row["recipient_id"], "recipient_name": names.get(row["recipient_id"]),
        "creator_id": row["creator_id"], "creator_name": names.get(row["creator_id"]),
        "business_id": row["business_id"],
        "at": row["due_at"] if due else (upcoming or row["next_at"] or row["due_at"]),
        "state": row["state"], "recurrence": recurrence, "time_critical": bool(row["time_critical"]),
        "linked_item": link, "link_available": link_available,
        "occurrence_id": row["current_occurrence_id"] if due else None,
        "due_at": row["due_at"] if due else None, "next_at": upcoming,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def legacy(row):
    """The plain shape of the retired Telegram-backed owner reminder tools; the id and revision are the shared ones."""
    due = row["state"] == "due"
    return {"reminder_id": row["id"], "revision": row["revision"], "text": row["text"],
            "at": row["due_at"] if due else (row["next_at"] or row["due_at"]), "state": row["state"]}
