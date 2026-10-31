"""Verify provider content against one immutable approved outgoing payload."""

import hashlib

from .fastmail import ProviderError


def recipients(data, name):
    return sorted(item["emailAddress"]["address"].lower() for item in data.get(name, []))


def verify_approved_message(provider, mailbox, message, observed, *, is_draft=True):
    normalize = lambda value: value.replace("\r\n", "\n").rstrip("\n")
    if (observed.get("isDraft") is not is_draft or observed.get("subject") != message.subject
            or observed.get("from", {}).get("emailAddress", {}).get("address", "").lower() != message.from_address
            or message.from_name and observed.get("from", {}).get("emailAddress", {}).get("name", "") != message.from_name
            or normalize(observed.get("body", {}).get("content", "")) != normalize(message.body)
            or observed.get("bodyTruncated") or observed.get("requestId") != message.request_id
            or recipients(observed, "replyTo") != ([message.reply_to] if message.reply_to else [])
            or any(recipients(observed, key) != sorted(getattr(message, field)) for key, field in
                (("toRecipients", "to"), ("ccRecipients", "cc"), ("bccRecipients", "bcc")))):
        raise ProviderError("provider_draft_changed")
    if message.reply_message_id:
        parent = provider.get_message(mailbox, message.reply_message_id)
        expected = (parent.get("internetMessageId") or "").strip("<>")
        if not expected or observed.get("inReplyTo") != [expected]:
            raise ProviderError("provider_reply_changed")
    elif observed.get("inReplyTo"):
        raise ProviderError("provider_reply_changed")
    actual = []
    for item in provider.attachments(mailbox, observed["id"]):
        content = provider.attachment(mailbox, observed["id"], item["id"])
        actual.append((item["name"], item["contentType"].lower(), hashlib.sha256(content).hexdigest()))
    expected = [(item.filename, item.content_type, item.sha256) for item in message.attachments]
    if sorted(actual) != sorted(expected):
        raise ProviderError("provider_attachments_changed")
