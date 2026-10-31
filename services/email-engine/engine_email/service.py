"""Business-folder synchronization and the approved-send lifecycle."""

import hashlib
import json
import threading
from datetime import datetime, timezone

from .fastmail import ProviderError
from .history import PrivateHistory
from .models import Outgoing
from .store import Store

ACCOUNT_CURSOR = "@account"


class EmailService:
    def __init__(self, settings, store, provider):
        self.settings, self.store, self.provider = settings, store, provider
        self.history = PrivateHistory(settings.history, provider.account_id, settings.database) if settings.history else None
        if any(sender.enabled for sender in settings.shared_senders) and self.history is None:
            raise ValueError("Enabled shared sending requires private history")
        store.history = self.history
        store.flush_history()
        store.bind_provider(provider.account_id)
        store.bind_senders(settings.shared_senders)
        if store.bind_mailboxes(settings.mailboxes):
            self.reset_sync()
        self.send_lock, self.sync_lock = threading.Lock(), threading.Lock()
        self.history_lock = threading.Lock()

    def collect_history(self):
        if self.history is None:
            return {"state": "disabled"}
        if not self.history_lock.acquire(blocking=False):
            return {"state": "running"}
        try:
            self.store.flush_history()
            return self.provider.collect_history(self.history)
        except ProviderError as exc:
            cursor = self.history.cursor()
            identity = hashlib.sha256((cursor + ":" + exc.code + ":" + str(exc.status)).encode()).hexdigest()
            self.history.append("collection-gap:" + identity, "provider.collection_gap",
                {"cursor": cursor, "error": exc.code, "provider_status": exc.status})
            # Never reset an expired history cursor into a silent fresh baseline.
            raise
        finally:
            self.history_lock.release()

    def reset_sync(self):
        for mailbox in self.settings.mailboxes:
            self.store.reset_folder(mailbox.ref, mailbox.folder_id)
        self.store.reset_folder(ACCOUNT_CURSOR, "all")

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            return {"state": "running"}
        try:
            for mailbox in self.settings.mailboxes:
                self.store.mailbox_status(mailbox.ref, "syncing")
            self.provider.folders()
            saved = self.store.folder(ACCOUNT_CURSOR, "all")
            cursor = saved["cursor"] if saved else None
            seen = set()
            for _ in range(20):
                if cursor and cursor in seen:
                    raise ProviderError("repeated_jmap_continuation")
                seen.add(cursor)
                data = self.provider.delta(cursor)
                touched = {item["id"] for item in data["items"]} | set(data["destroyed"])
                for mailbox in self.settings.mailboxes:
                    owned = {item["id"]: item for item in data["items"] if self.provider.owner(item) == mailbox}
                    items = [owned.get(identity, {"id": identity, "@removed": {}}) for identity in sorted(touched)]
                    self.store.sync_page(mailbox.ref, mailbox.folder_id, items, data["cursor"], complete=data["complete"])
                # Advance only after all business caches commit; replay is harmless if interrupted.
                self.store.sync_page(ACCOUNT_CURSOR, "all", [], data["cursor"], complete=data["complete"])
                cursor = data["cursor"]
                if data["complete"]:
                    for mailbox in self.settings.mailboxes:
                        self.store.mailbox_status(mailbox.ref, "synced")
                    return {"state": "synced", "errors": []}
            return {"state": "syncing", "errors": []}
        except ProviderError as exc:
            if exc.status == 410:
                self.reset_sync()
            for mailbox in self.settings.mailboxes:
                self.store.mailbox_status(mailbox.ref, "partial", exc.code)
            return {"state": "partial", "errors": [{"error": exc.code}]}
        finally:
            self.sync_lock.release()

    @staticmethod
    def _recipients(data, name):
        return sorted(item["emailAddress"]["address"].lower() for item in data.get(name, []))

    def verify_draft(self, mailbox, message, draft):
        normalize = lambda value: value.replace("\r\n", "\n").rstrip("\n")
        if (draft.get("isDraft") is not True or draft.get("subject") != message.subject
                or draft.get("from", {}).get("emailAddress", {}).get("address", "").lower() != message.from_address
                or message.from_name and draft.get("from", {}).get("emailAddress", {}).get("name", "") != message.from_name
                or normalize(draft.get("body", {}).get("content", "")) != normalize(message.body)
                or draft.get("bodyTruncated") or draft.get("requestId") != message.request_id
                or self._recipients(draft, "replyTo") != ([message.reply_to] if message.reply_to else [])
                or any(self._recipients(draft, key) != sorted(getattr(message, field)) for key, field in
                    (("toRecipients", "to"), ("ccRecipients", "cc"), ("bccRecipients", "bcc")))):
            raise ProviderError("provider_draft_changed")
        if message.reply_message_id:
            parent = self.provider.get_message(mailbox, message.reply_message_id)
            expected = (parent.get("internetMessageId") or "").strip("<>")
            if not expected or draft.get("inReplyTo") != [expected]:
                raise ProviderError("provider_reply_changed")
        elif draft.get("inReplyTo"):
            raise ProviderError("provider_reply_changed")
        actual = []
        for item in self.provider.attachments(mailbox, draft["id"]):
            content = self.provider.attachment(mailbox, draft["id"], item["id"])
            actual.append((item["name"], item["contentType"].lower(), hashlib.sha256(content).hexdigest()))
        expected = [(item.filename, item.content_type, item.sha256) for item in message.attachments]
        if sorted(actual) != sorted(expected):
            raise ProviderError("provider_attachments_changed")

    def submit(self, message):
        mailbox = self.settings.mailbox(message.mailbox)
        sender = self.settings.sender(mailbox, message)
        with self.send_lock:
            row, created = self.store.begin_send(message, queued=sender is not None)
            if not created:
                return self.reconcile(row)
            return self.start_send(mailbox, message, row, sender)

    @staticmethod
    def expired(message):
        return message.expires_at is not None and datetime.fromisoformat(message.expires_at) <= datetime.now(timezone.utc)

    def start_send(self, mailbox, message, row, sender):
        if self.expired(message):
            return Store.receipt(self.store.save_send(message.request_id, "failed",
                result={"error": "approval_expired", "not_sent": True}))
        if sender:
            reserved, row = self.store.reserve(message.request_id, sender)
            if not reserved:
                return Store.receipt(row)
        self.store.flush_history()
        try:
            draft = self.provider.create_draft(mailbox, message)
            identity = draft.get("id")
            if not isinstance(identity, str) or not identity:
                raise ProviderError("missing_provider_draft", uncertain=True)
            self.store.save_send(message.request_id, "draft", draft_id=identity)
            return self.prepare_and_send(mailbox, message, identity)
        except ProviderError as exc:
            return self.record_error(message.request_id, exc)

    def prepare_and_send(self, mailbox, message, identity):
        try:
            sender = self.settings.sender(mailbox, message)
        except ValueError as exc:
            raise ProviderError("mailbox_configuration_changed") from exc
        if self.expired(message):
            raise ProviderError("approval_expired", status=400)
        if sender:
            reserved, row = self.store.reserve(message.request_id, sender)
            if not reserved:
                return Store.receipt(row)
        self.verify_draft(mailbox, message, self.provider.get_message(mailbox, identity))
        # No recovery path resubmits after this durable boundary.
        self.store.save_send(message.request_id, "sending")
        self.store.flush_history()
        submission = self.provider.send_draft(mailbox, identity, message)
        # RFC 8621 permits immediate removal of submission objects after sending.
        return self.reconcile(self.store.save_send(message.request_id, "accepted",
            result={"submission": submission}))

    def record_error(self, request_id, exc):
        row = self.store.send(request_id)
        transient = exc.uncertain or exc.status == 429 or exc.status is not None and exc.status >= 500 or exc.code == "fastmail_connection_failed"
        if row["state"] in {"sending", "accepted"} and (transient or exc.status is None):
            state = "uncertain"
        elif row["state"] in {"creating", "draft"} and transient:
            state = row["state"]
        else:
            state = "failed"
        return Store.receipt(self.store.save_send(request_id, state,
            result={"error": exc.code, "provider_status": exc.status,
                **({"not_sent": True} if state == "failed" and row["state"] in {"queued", "creating", "draft"} else {})}))

    def reconcile(self, row):
        if row["state"] in {"sent", "failed", "canceled"}:
            return Store.receipt(row)
        try:
            mailbox = self.settings.mailbox(row["mailbox"])
            original = Outgoing.model_validate_json(row["payload"])
            if row["state"] in {"queued", "creating", "draft"}:
                try:
                    sender = self.settings.sender(mailbox, original)
                except ValueError:
                    return self.record_error(row["request_id"], ProviderError("mailbox_configuration_changed", status=400))
                if self.expired(original):
                    return Store.receipt(self.store.save_send(row["request_id"], "failed",
                        result={"error": "approval_expired", "not_sent": True}))
                if row["state"] == "queued":
                    return self.start_send(mailbox, original, row, sender)
            if not row["draft_id"]:
                draft = self.provider.find_draft(mailbox, row["request_id"])
                if draft is None:
                    return Store.receipt(row)
                row = self.store.save_send(row["request_id"], "draft" if row["state"] == "creating" else "uncertain", draft_id=draft["id"])
            if row["state"] == "draft":
                try:
                    return self.prepare_and_send(mailbox, original, row["draft_id"])
                except ProviderError as exc:
                    return self.record_error(row["request_id"], exc)
            submission = json.loads(row["result"]).get("submission")
            if not submission or submission.get("undoStatus") not in {"final", "canceled"}:
                submission = self.provider.submission(mailbox, row["draft_id"], original)
                if submission:
                    row = self.store.save_send(row["request_id"], row["state"], result={"submission": submission})
            if submission and submission.get("undoStatus") == "final":
                message = self.provider.get_message(mailbox, row["draft_id"])
                row = self.store.save_send(row["request_id"], "sent", result={
                    "message_id": message["id"], "internet_message_id": message.get("internetMessageId"),
                    "conversation_id": message.get("conversationId"), "sent_at": submission.get("sendAt"),
                    "submission_id": submission["id"], "url": message.get("webLink"), "delivery": "submission_confirmed"})
            elif submission and submission.get("undoStatus") == "canceled":
                row = self.store.save_send(row["request_id"], "failed", result={"error": "provider_submission_canceled"})
            return Store.receipt(row)
        except (ProviderError, ValueError):
            return Store.receipt(row)

    def reconcile_pending(self):
        with self.send_lock:
            self.store.flush_history()
            results = [self.reconcile(row) for row in self.store.pending_sends()]
            self.store.flush_history()
            return results

    def cancel(self, mailbox, request_id):
        with self.send_lock:
            result = Store.receipt(self.store.cancel_queued(request_id, mailbox))
            self.store.flush_history()
            return result
