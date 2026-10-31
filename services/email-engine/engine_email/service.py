"""Mailbox synchronization and the approved-send lifecycle."""

import hashlib
import threading

from .graph import CORRELATION, ProviderError
from .models import Outgoing
from .store import Store


class EmailService:
    def __init__(self, settings, store, graph):
        self.settings, self.store, self.graph = settings, store, graph
        store.bind_mailboxes(settings.mailboxes)
        self.send_lock, self.sync_lock = threading.Lock(), threading.Lock()

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            return {"state": "running"}
        failures = []
        pending = False
        try:
            for mailbox in self.settings.mailboxes:
                self.store.mailbox_status(mailbox.ref, "syncing")
                mailbox_errors, incomplete = [], False
                try:
                    folders = self.graph.folders(mailbox)
                except ProviderError as exc:
                    self.store.mailbox_status(mailbox.ref, "partial", exc.code)
                    failures.append({"mailbox": mailbox.ref, "error": exc.code})
                    continue
                self.store.retain_folders(mailbox.ref, {folder["id"] for folder in folders})
                for folder in folders:
                    identity = folder["id"]
                    saved = self.store.folder(mailbox.ref, identity)
                    cursor = saved["cursor"] if saved else None
                    seen = set()
                    try:
                        # Bound one pass; a saved nextLink continues on the next pass.
                        for _ in range(20):
                            if cursor and cursor in seen:
                                raise ProviderError("repeated_graph_continuation")
                            if cursor:
                                seen.add(cursor)
                            data = self.graph.delta(mailbox, identity, cursor)
                            next_link, delta_link = data.get("@odata.nextLink"), data.get("@odata.deltaLink")
                            if bool(next_link) == bool(delta_link):
                                raise ProviderError("invalid_graph_delta_page")
                            cursor = next_link or delta_link
                            self.graph.validate_url(mailbox, cursor)
                            for item in data["value"]:
                                if "@removed" not in item and item.get("hasAttachments"):
                                    item["attachments"] = self.graph.attachments(mailbox, item["id"])
                            self.store.sync_page(mailbox.ref, identity, data["value"], cursor, complete=not next_link)
                            if not next_link:
                                break
                        else:
                            incomplete = pending = True
                    except ProviderError as exc:
                        if exc.status == 410:
                            self.store.reset_folder(mailbox.ref, identity)
                        self.store.sync_error(mailbox.ref, identity, exc.code)
                        mailbox_errors.append(exc.code)
                        failures.append({"mailbox": mailbox.ref, "folder": identity, "error": exc.code})
                self.store.mailbox_status(mailbox.ref, "partial" if mailbox_errors else "syncing" if incomplete else "synced",
                    mailbox_errors[0] if mailbox_errors else None)
            return {"state": "partial" if failures else "syncing" if pending else "synced", "errors": failures}
        finally:
            self.sync_lock.release()

    @staticmethod
    def _recipients(data, name):
        return sorted(item["emailAddress"]["address"].lower() for item in data.get(name, []))

    def verify_content(self, message, draft):
        normalize = lambda value: value.replace("\r\n", "\n").rstrip("\n")
        properties = {item["id"]: item["value"] for item in draft.get("singleValueExtendedProperties", [])}
        if (draft.get("isDraft") is not True or draft.get("subject") != message.subject
                or draft.get("from", {}).get("emailAddress", {}).get("address", "").lower() != message.from_address
                or message.from_name and draft.get("from", {}).get("emailAddress", {}).get("name", "") != message.from_name
                or normalize(draft.get("body", {}).get("content", "")) != normalize(message.body)
                or properties.get(CORRELATION) != message.request_id
                or any(self._recipients(draft, key) != sorted(getattr(message, field)) for key, field in
                    (("toRecipients", "to"), ("ccRecipients", "cc"), ("bccRecipients", "bcc")))):
            raise ProviderError("provider_draft_changed")

    def attachment_signatures(self, mailbox, draft_id):
        actual_values = []
        for item in self.graph.attachments(mailbox, draft_id):
            content = self.graph.attachment(mailbox, draft_id, item["id"])
            actual_values.append((item["name"], item["contentType"].lower(), hashlib.sha256(content).hexdigest()))
        return actual_values

    def verify_draft(self, mailbox, message, draft):
        self.verify_content(message, draft)
        actual = self.graph.attachments(mailbox, draft["id"])
        if len(actual) != len(message.attachments):
            raise ProviderError("provider_attachments_changed")
        expected = sorted((item.filename, item.content_type, item.sha256) for item in message.attachments)
        actual_values = self.attachment_signatures(mailbox, draft["id"])
        if sorted(actual_values) != expected:
            raise ProviderError("provider_attachments_changed")

    def submit(self, message):
        mailbox = self.settings.mailbox(message.mailbox)
        if mailbox.business_id != message.business_id or not mailbox.accepts_sender(message.from_address):
            raise ValueError("Sender or business is outside the registered mailbox")
        with self.send_lock:
            row, created = self.store.begin_send(message)
            if not created:
                return self.reconcile(row)
            try:
                draft = self.graph.create_draft(mailbox, message)
                identity = draft.get("id")
                if not isinstance(identity, str) or not identity:
                    raise ProviderError("missing_provider_draft", uncertain=True)
                self.store.save_send(message.request_id, "draft", draft_id=identity)
                return self.prepare_and_send(mailbox, message, identity)
            except ProviderError as exc:
                return self.record_error(message.request_id, exc)

    def prepare_and_send(self, mailbox, message, identity):
        if mailbox.business_id != message.business_id or not mailbox.accepts_sender(message.from_address):
            raise ProviderError("mailbox_configuration_changed")
        self.verify_content(message, self.graph.get_message(mailbox, identity))
        missing = list(message.attachments)
        for signature in self.attachment_signatures(mailbox, identity):
            match = next((item for item in missing if (item.filename, item.content_type, item.sha256) == signature), None)
            if match is None:
                raise ProviderError("provider_attachments_changed")
            missing.remove(match)
        for attachment in missing:
            self.graph.add_attachment(mailbox, identity, attachment)
        self.verify_draft(mailbox, message, self.graph.get_message(mailbox, identity))
        # Persist before the network call. No recovery path sends again after this point.
        self.store.save_send(message.request_id, "sending")
        self.graph.send_draft(mailbox, identity)
        return self.reconcile(self.store.save_send(message.request_id, "accepted"))

    def record_error(self, request_id, exc):
        row = self.store.send(request_id)
        transient = exc.uncertain or exc.status == 429 or exc.status is not None and exc.status >= 500 or exc.code == "graph_connection_failed"
        if row["state"] in {"sending", "accepted"} and (transient or exc.status is None):
            state = "uncertain"
        elif row["state"] in {"creating", "draft"} and transient:
            state = row["state"]
        else:
            state = "failed"
        return Store.receipt(self.store.save_send(request_id, state,
            result={"error": exc.code, "provider_status": exc.status}))

    def reconcile(self, row):
        if row["state"] in {"sent", "failed"}:
            return Store.receipt(row)
        try:
            mailbox = self.settings.mailbox(row["mailbox"])
        except ValueError:
            return Store.receipt(row)
        try:
            if not row["draft_id"]:
                draft = self.graph.find_draft(mailbox, row["request_id"])
                if draft is None:
                    return Store.receipt(row)
                row = self.store.save_send(row["request_id"], "draft" if row["state"] == "creating" else "uncertain", draft_id=draft["id"])
            if row["state"] == "draft":
                try:
                    return self.prepare_and_send(mailbox, Outgoing.model_validate_json(row["payload"]), row["draft_id"])
                except ProviderError as exc:
                    return self.record_error(row["request_id"], exc)
            message = self.graph.get_message(mailbox, row["draft_id"])
            if message.get("isDraft") is False and message.get("parentFolderId") == self.graph.sent_folder(mailbox):
                row = self.store.save_send(row["request_id"], "sent", result={
                    "message_id": message["id"], "internet_message_id": message.get("internetMessageId"),
                    "conversation_id": message.get("conversationId"), "sent_at": message.get("sentDateTime"),
                    "url": message.get("webLink"), "delivery": "sent_items_confirmed"})
            # A remaining draft after a send timeout is not proof that sending never started.
            return Store.receipt(row)
        except ProviderError:
            return Store.receipt(row)

    def reconcile_pending(self):
        with self.send_lock:
            return [self.reconcile(row) for row in self.store.pending_sends()]
