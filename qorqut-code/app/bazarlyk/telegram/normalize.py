"""Reduce a raw Bot API update to the few facts the bridge uses. Raw update JSON is never persisted.

Message text is kept here only so the caller can route it; a caller that classifies it as sensitive removes it
before the normalized update is stored.
"""

from __future__ import annotations

from datetime import datetime, timezone

ATTACHMENT_KEYS = ("photo", "document", "video", "audio", "voice", "video_note", "sticker", "animation", "contact",
                   "location", "venue", "poll", "dice", "game", "story", "paid_media", "checklist")
SERVICE_KEYS = ("new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo", "delete_chat_photo",
                "group_chat_created", "supergroup_chat_created", "channel_chat_created", "migrate_to_chat_id",
                "migrate_from_chat_id", "pinned_message", "message_auto_delete_timer_changed", "forum_topic_created",
                "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened", "general_forum_topic_hidden",
                "general_forum_topic_unhidden", "video_chat_started",
                "video_chat_ended", "video_chat_participants_invited", "boost_added", "chat_background_set",
                "write_access_allowed", "users_shared", "chat_shared", "giveaway_created", "giveaway_completed")
NON_MESSAGE_KEYS = ("edited_message", "channel_post", "edited_channel_post", "business_connection", "business_message",
                    "edited_business_message", "deleted_business_messages", "message_reaction_count", "inline_query",
                    "chosen_inline_result", "callback_query", "shipping_query", "pre_checkout_query", "poll",
                    "poll_answer", "my_chat_member", "chat_member", "chat_join_request", "chat_boost", "removed_chat_boost")


def _int(value):
    return value if type(value) is int else None


def _instant(seconds):
    if type(seconds) is not int or seconds < 0:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def topic_of(message):
    """Validated forum-topic metadata of a message: ``(thread_id, invalid)``.

    ``thread_id`` is the topic's numeric ID only for a message that is marked as a topic message and carries a valid thread ID;
    otherwise it is None (a whole-chat or General message, or a reply thread of an ordinary supergroup). Malformed metadata is
    ``invalid`` and is never read as General or as a whole chat: a thread ID that is not an exact positive integer, a topic marker
    that is not a boolean, or a topic message that names no thread.
    """
    thread, marker = message.get("message_thread_id"), message.get("is_topic_message")
    if "message_thread_id" in message and (type(thread) is not int or thread <= 0):
        return None, True
    if "is_topic_message" in message and type(marker) is not bool:
        return None, True
    if marker is True:
        return (thread, False) if thread is not None else (None, True)
    return None, False


def _emoji(reactions):
    result = []
    for item in reactions if isinstance(reactions, list) else []:
        if isinstance(item, dict) and item.get("type") == "emoji" and isinstance(item.get("emoji"), str):
            result.append(item["emoji"])
        else:
            result.append("other")
    return result


def normalize_update(raw):
    """Return a normalized update dict, or None when the update has no usable ``update_id``."""
    if not isinstance(raw, dict) or type(raw.get("update_id")) is not int or raw["update_id"] < 0:
        return None
    update_id = raw["update_id"]
    ignored = {"update_id": update_id, "kind": "ignored", "reason": "unsupported_update_type"}
    if "message" in raw:
        message = raw["message"]
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            return {**ignored, "reason": "malformed"}
        chat = message["chat"]
        sender = message.get("from") if isinstance(message.get("from"), dict) else None
        text = message.get("text") if isinstance(message.get("text"), str) else (
            message.get("caption") if isinstance(message.get("caption"), str) else None)
        reply = message.get("reply_to_message")
        thread_id, topic_invalid = topic_of(message)
        # Telegram presents an ordinary topic message as a reply to the topic's own creation message, which is the thread's anchor
        # and not something the owner replied to: it is not a reply, and the topic root is never a message the bridge knows.
        if isinstance(reply, dict) and (("forum_topic_created" in reply) or (thread_id is not None and _int(reply.get("message_id")) == thread_id)):
            reply = None
        result = {
            "update_id": update_id, "kind": "message", "chat_id": _int(chat.get("id")), "chat_type": chat.get("type"),
            "message_id": _int(message.get("message_id")), "actor_id": _int(sender.get("id")) if sender else None,
            "sent_at": _instant(message.get("date")), "from_is_bot": bool(sender and sender.get("is_bot")),
            "anonymous": sender is None or "sender_chat" in message,
            "service": any(key in message for key in SERVICE_KEYS),
            "forwarded": any(key in message for key in ("forward_origin", "forward_from", "forward_from_chat", "forward_date")),
            "message_thread_id": thread_id, "topic_invalid": topic_invalid,
            "text": text, "attachments": [key for key in ATTACHMENT_KEYS if key in message],
            "reply": {"message_id": _int(reply.get("message_id"))} if isinstance(reply, dict) and _int(reply.get("message_id")) else None,
        }
        if result["chat_id"] is None or result["message_id"] is None:
            return {**ignored, "reason": "malformed"}
        return result
    if "message_reaction" in raw:
        reaction = raw["message_reaction"]
        if not isinstance(reaction, dict) or not isinstance(reaction.get("chat"), dict):
            return {**ignored, "reason": "malformed"}
        user = reaction.get("user") if isinstance(reaction.get("user"), dict) else None
        result = {
            "update_id": update_id, "kind": "message_reaction", "chat_id": _int(reaction["chat"].get("id")),
            "chat_type": reaction["chat"].get("type"), "message_id": _int(reaction.get("message_id")),
            "actor_id": _int(user.get("id")) if user else None, "sent_at": _instant(reaction.get("date")),
            "from_is_bot": bool(user and user.get("is_bot")),
            "anonymous": user is None or "actor_chat" in reaction,
            "old": _emoji(reaction.get("old_reaction")), "new": _emoji(reaction.get("new_reaction")),
        }
        if result["chat_id"] is None or result["message_id"] is None:
            return {**ignored, "reason": "malformed"}
        return result
    for key in NON_MESSAGE_KEYS:
        if key in raw:
            return {**ignored, "reason": key}
    return ignored
