"""Owner-bound Outlook web sending; business reads stay in Fastmail.

The operational tables contain identifiers and recovery state only. Full provider
snapshots and MIME are appended to the independent encrypted history before use.
"""
import base64
import fcntl
import hashlib
import json
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.policy import default
from email.utils import getaddresses
from pathlib import Path

from .fastmail import Fastmail, ProviderError, internet_message_id
from .models import MAX_MESSAGE_BYTES, Outgoing
from .outlook_collection import OutlookCollection, timestamp
from .store import now

PROVIDER = "outlook-web"


def native_item(raw, *, after_cutover=False, is_sent=False):
    return {"id": raw.get("ItemId", {}).get("Id"),
        "raw": {**raw, "MimeContent": {key: value for key, value in raw.get("MimeContent", {}).items() if key != "Value"}},
        "mime_base64": raw.get("MimeContent", {}).get("Value"),
        "is_sent": is_sent, "is_read": raw.get("IsRead") is True,
        "sent_at": raw.get("DateTimeSent"), "received_at": raw.get("DateTimeReceived"),
        "created_at": raw.get("DateTimeCreated"), "changed_at": raw.get("LastModifiedTime"),
        "after_cutover": after_cutover}


def decode_message(raw):
    try:
        content = base64.b64decode(raw["MimeContent"]["Value"], validate=True)
        if not content or len(content) > MAX_MESSAGE_BYTES:
            raise ValueError()
        mail = BytesParser(policy=default).parsebytes(content)
        if mail.defects:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ProviderError("outlook_complete_mime_unavailable") from None
    return content, mail


def normalize(raw):
    _, mail = decode_message(raw)
    def addresses(header):
        return [{"emailAddress": {"address": addr.lower(), "name": name}}
            for name, addr in getaddresses([str(value) for value in mail.get_all(header, [])])]
    parts, attachments = {}, []
    body = mail.get_body(preferencelist=("plain", "html"))
    if (body is None or body.get_content_type() != "text/plain"
            or any(part is not body and not part.is_multipart()
                and part.get_content_disposition() != "attachment" and not part.get_filename()
                for part in mail.walk())):
        raise ProviderError("outlook_unapproved_body_part")
    for index, part in enumerate(mail.walk()):
        if part.is_multipart() or part is body:
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            identity = str(index)
            content = part.get_payload(decode=True) or b""
            parts[identity] = content
            attachments.append({"id": identity, "name": part.get_filename() or "attachment",
                "contentType": part.get_content_type(), "size": len(content),
                "isInline": part.get_content_disposition() == "inline"})
    try:
        body_value = body.get_content() if body is not None else ""
    except (UnicodeError, LookupError):
        raise ProviderError("outlook_body_encoding_invalid") from None
    if not isinstance(body_value, str):
        raise ProviderError("outlook_body_invalid")
    extract = lambda key: [value.strip("<>") for line in mail.get_all(key, [])
        for value in re.findall(r"<[^<>\s]+>", str(line))]
    identifiers = mail.get_all("Message-ID", [])
    request_ids = mail.get_all("X-Qorqut-Request", [])
    senders = addresses("From")
    if len(senders) != 1 or len(identifiers) != 1 or len(request_ids) > 1:
        raise ProviderError("outlook_message_headers_invalid")
    result = {"id": raw.get("ItemId", {}).get("Id"), "conversationId": raw.get("ConversationId", {}).get("Id"),
        "subject": str(mail.get("Subject", "")), "from": senders[0],
        **{key: addresses(header) for key, header in (("toRecipients", "To"), ("ccRecipients", "Cc"),
            ("bccRecipients", "Bcc"), ("replyTo", "Reply-To"))},
        "body": {"contentType": "Text" if body is None or body.get_content_subtype() == "plain" else "HTML", "content": body_value},
        "bodyTruncated": False, "requestId": str(request_ids[0]) if request_ids else None,
        "isDraft": raw.get("IsDraft") is True, "isRead": raw.get("IsRead") is True,
        "hasAttachments": bool(attachments), "attachments": attachments,
        "internetMessageId": internet_message_id(str(identifiers[0])),
        "references": extract("References"), "inReplyTo": extract("In-Reply-To"),
        "receivedDateTime": raw.get("DateTimeReceived"), "sentDateTime": raw.get("DateTimeSent"),
        "webLink": raw.get("WebClientReadFormQueryString")}
    if not result["id"] or not result["conversationId"] or not result["internetMessageId"]:
        raise ProviderError("outlook_message_identifiers_missing")
    return result, parts


class OutlookWeb:
    def __init__(self, config, *, store, history, fastmail, bridge=None):
        if history is None:
            raise ValueError("Outlook requires private history")
        self.config, self.store, self.history, self.fastmail = config, store, history, fastmail
        self._bridge_override, self._lock, self._cache = bridge, threading.RLock(), {}
        self._status = {"available": False, "error": None, "last_checked": None}
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS outlook_web_sends (
                request_id TEXT PRIMARY KEY, business_id TEXT NOT NULL, mailbox TEXT NOT NULL,
                internet_message_id TEXT NOT NULL UNIQUE, draft_id TEXT, native_id TEXT,
                copy_state TEXT NOT NULL, fastmail_id TEXT, fastmail_thread_id TEXT)""")
        self.collector = OutlookCollection(store, account_address=config.address, start_at=config.start_at,
            account_ref=config.account_ref, capture=lambda cursor: self._call("capture", cursor=cursor),
            retrieve=self._retrieve, archive=history, fastmail=fastmail, mailboxes=fastmail.settings.mailboxes)

    def close(self):
        self._cache.clear()

    def _call(self, command, **args):
        try:
            with self._lock:
                if self._bridge_override:
                    result = self._bridge_override(command, args)
                else:
                    # Serializes independent collector/HTTP processes using this ledger.
                    lock_path = self.store.path.with_name(self.store.path.name + ".outlook.lock")
                    with lock_path.open("a+b") as lock:
                        lock_path.chmod(0o600)
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        request = json.dumps({"config": self.config.model_dump(), "command": command, "args": args})
                        try:
                            process = subprocess.run(["node", str(Path(__file__).with_name("outlook_bridge.mjs"))],
                                input=request.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=self.config.timeout_seconds + 60, check=False)
                        except (OSError, subprocess.TimeoutExpired):
                            raise ProviderError("outlook_bridge_unavailable", uncertain=command in {"create", "send"}) from None
                        try:
                            response = json.loads(process.stdout)
                            if not isinstance(response, dict):
                                raise ValueError()
                        except (ValueError, UnicodeError):
                            raise ProviderError("outlook_bridge_response_invalid", uncertain=command in {"create", "send"}) from None
                        if response.get("ok") is not True:
                            code = response.get("error", "")
                            if not isinstance(code, str) or not re.fullmatch(r"outlook_[a-z_]{1,80}", code):
                                code = "outlook_bridge_failed"
                            raise ProviderError(code, uncertain=bool(response.get("uncertain")))
                        result = response.get("result")
            self._status = {"available": True, "error": None, "last_checked": now()}
            return result
        except ProviderError as exc:
            self._status = {"available": False, "error": exc.code, "last_checked": now()}
            raise

    def status(self):
        return {**self._status, "collector": self.collector.status()}

    def check_status(self):
        result = self._call("status")
        if result.get("account_address") != self.config.address or result.get("available") is not True:
            self._status.update(available=False, error="outlook_account_changed")
            raise ProviderError("outlook_account_changed")
        return self.status()

    def collect_history(self, archive):
        if archive is not self.history:
            raise ValueError("Outlook archive cannot be replaced")
        return self.collector.run()

    def refresh_quota(self):
        # The rolling window can start before the body-capture cutover. Read only
        # native Sent metadata to seed those earlier sends before reserving a slot.
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        page = self._call("quota", since=since)
        if (not isinstance(page, dict) or page.get("complete") is not True
                or page.get("account_address") != self.config.address or timestamp(page.get("since")) != timestamp(since)
                or not isinstance(page.get("items"), list)):
            raise ProviderError("outlook_quota_response_invalid")
        observations = []
        for raw in page["items"]:
            try:
                native_id = raw["ItemId"]["Id"]
                message_id = internet_message_id(raw["InternetMessageId"])
                sent_at = timestamp(raw["DateTimeSent"])
                valid = (raw.get("IsDraft") is False and bool(native_id) and bool(message_id)
                    and raw["From"]["Mailbox"]["EmailAddress"].lower() == self.config.address
                    and datetime.fromisoformat(sent_at) >= datetime.fromisoformat(since)
                    and "MimeContent" not in raw and "Body" not in raw)
            except (KeyError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise ProviderError("outlook_quota_response_invalid")
            observations.append((native_id, message_id, sent_at))
        self.history.external_observation(PROVIDER, self.config.address, since, page,
            kind="provider.quota_observation", provenance={"scope": "sent_metadata_only", "rolling_hours": 24})
        for native_id, message_id, sent_at in observations:
            self.store.observe_external_send(self.config.account_ref, native_id, message_id, sent_at)
        return {"observed": len(observations), "since": since}

    def _retrieve(self, identities):
        if len(identities) > 10:
            raise ProviderError("outlook_retrieve_limit")
        return self._call("retrieve", ids=identities)

    def _binding(self, mailbox, *, identity=None, request_id=None):
        with self.store.connect() as db:
            if request_id:
                row = db.execute("SELECT * FROM outlook_web_sends WHERE request_id=?", (request_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM outlook_web_sends WHERE draft_id=? OR native_id=?", (identity, identity)).fetchone()
        if row and (row["business_id"] != mailbox.business_id or row["mailbox"] != mailbox.ref):
            raise ProviderError("message_outside_business", status=404)
        return row

    def _approved(self, mailbox, message):
        row = self.store.send(message.request_id)
        if (row is None or row["digest"] != message.digest() or row["transport"] != "outlook_web"
                or row["business_id"] != mailbox.business_id or row["mailbox"] != mailbox.ref
                or message.from_address != self.config.address):
            raise ProviderError("outlook_approved_send_missing")
        return row

    def _observe(self, raw, row, *, sent=False):
        result, attachments = normalize(raw)
        # Exchange removes our custom X header on submission and resolves display
        # names from its directory. The digest-based RFC ID survives. A missing
        # X header is allowed only in Sent Items after checking the saved payload.
        request_matches = result["requestId"] == row["request_id"] or sent and result["requestId"] is None
        if (result["internetMessageId"] != row["internet_message_id"] or not request_matches
                or result["from"]["emailAddress"]["address"] != self.config.address
                or raw.get("From", {}).get("Mailbox", {}).get("EmailAddress", "").lower() != self.config.address):
            raise ProviderError("outlook_message_identity_changed")
        if sent:
            self._verify_sent_content(row, result, attachments)
        snapshot = native_item(raw, after_cutover=True, is_sent=sent)
        self.history.external_observation(PROVIDER, self.config.address, result["id"], snapshot,
            provenance={"request_id": row["request_id"], "business_id": row["business_id"], "mailbox": row["mailbox"]})
        with self.store.connect() as db:
            db.execute("UPDATE outlook_web_sends SET native_id=?,draft_id=COALESCE(draft_id,?) WHERE request_id=?",
                (result["id"], result["id"] if result["isDraft"] else None, row["request_id"]))
        self._cache[row["request_id"]] = (raw, result, attachments)
        return result

    def _verify_sent_content(self, row, result, attachments):
        saved = self.store.send(row["request_id"])
        if saved is None or saved["transport"] != "outlook_web" or saved["state"] not in {"sending", "accepted", "uncertain", "sent"}:
            raise ProviderError("outlook_sent_approval_missing", uncertain=True)
        approved = Outgoing.model_validate_json(saved["payload"])
        normalize_body = lambda value: value.replace("\r\n", "\n").rstrip("\n")
        recipients = lambda key: sorted(value["emailAddress"]["address"] for value in result[key])
        # Exchange can prefix the sent MIME Subject with one folding tab.
        # Accept only that observed formatting difference, after sent identity proof.
        subject_matches = result["subject"] in (approved.subject, "\t" + approved.subject)
        if (not subject_matches or normalize_body(result["body"]["content"]) != normalize_body(approved.body)
                or result["bodyTruncated"] or recipients("replyTo") != ([approved.reply_to] if approved.reply_to else [])
                or any(recipients(key) != sorted(getattr(approved, field)) for key, field in
                    (("toRecipients", "to"), ("ccRecipients", "cc"), ("bccRecipients", "bcc")))):
            raise ProviderError("outlook_sent_content_changed", uncertain=True)
        actual = sorted((item["name"], item["contentType"], hashlib.sha256(attachments[item["id"]]).hexdigest()) for item in result["attachments"])
        expected = sorted((item.filename, item.content_type, item.sha256) for item in approved.attachments)
        if actual != expected:
            raise ProviderError("outlook_sent_attachments_changed", uncertain=True)

    def create_draft(self, mailbox, message):
        self._approved(mailbox, message)
        self.fastmail.folders()
        parent = self.fastmail.get_message(mailbox, message.reply_message_id) if message.reply_message_id else None
        content = Fastmail.mime(message, parent)
        message_id = str(BytesParser(policy=default).parsebytes(content)["Message-ID"])
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO outlook_web_sends(request_id,business_id,mailbox,internet_message_id,copy_state) VALUES(?,?,?,?,'pending')",
                (message.request_id, mailbox.business_id, mailbox.ref, message_id))
        row = self._binding(mailbox, request_id=message.request_id)
        if row["internet_message_id"] != message_id:
            raise ProviderError("outlook_saved_message_changed")
        raw = self._call("create", mime_base64=base64.b64encode(content).decode())
        return self._observe(raw, row)

    def find_draft(self, mailbox, request_id):
        row = self._binding(mailbox, request_id=request_id)
        if row is None:
            raise ProviderError("outlook_saved_message_missing")
        raw = self._call("find", internet_message_id=row["internet_message_id"])
        return self._observe(raw, row) if raw else None

    def _sent(self, row):
        raw = self._call("find", internet_message_id=row["internet_message_id"], sent_only=True)
        if raw is None:
            return None
        if raw.get("IsDraft") is True or not raw.get("DateTimeSent"):
            raise ProviderError("outlook_sent_evidence_missing", uncertain=True)
        self._observe(raw, row, sent=True)
        return raw

    def get_message(self, mailbox, identity):
        row = self._binding(mailbox, identity=identity)
        if row is None:
            return self.fastmail.get_message(mailbox, identity)
        try:
            raw = self._call("get", id=row["native_id"] or identity)
        except ProviderError as exc:
            if exc.code != "outlook_item_not_found":
                raise
            raw = self._call("find", internet_message_id=row["internet_message_id"])
            if raw is None:
                raise ProviderError("outlook_message_unconfirmed", uncertain=True) from None
        if raw.get("IsDraft") is True:
            return self._observe(raw, row)
        raw = self._sent(row)
        if raw is None:
            raise ProviderError("outlook_sent_evidence_missing", uncertain=True)
        return self._copy(mailbox, row, raw)

    def _copy(self, mailbox, row, raw):
        content, _ = decode_message(raw)
        found = self.fastmail.find_observed_mime(mailbox, row["internet_message_id"], content)
        if found is None:
            if row["copy_state"] in {"importing", "uncertain", "imported"}:
                raise ProviderError("outlook_import_unconfirmed", uncertain=True)
            with self.store.connect() as db:
                db.execute("UPDATE outlook_web_sends SET copy_state='importing' WHERE request_id=?", (row["request_id"],))
            try:
                found = self.fastmail.import_observed_mime(mailbox, content,
                    message_id=row["internet_message_id"], received_at=timestamp(raw["DateTimeSent"]), is_read=True)
            except ProviderError as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE outlook_web_sends SET copy_state=? WHERE request_id=?",
                        ("uncertain" if exc.uncertain else "pending", row["request_id"]))
                raise
        if not found.get("id") or not found.get("conversationId"):
            raise ProviderError("outlook_import_result_invalid", uncertain=True)
        with self.store.connect() as db:
            db.execute("UPDATE outlook_web_sends SET copy_state='imported',fastmail_id=?,fastmail_thread_id=? WHERE request_id=?",
                (found["id"], found["conversationId"], row["request_id"]))
        evidence = {"outlookMessageId": raw["ItemId"]["Id"],
            "outlookConversationId": raw.get("ConversationId", {}).get("Id"),
            "nativeInternetMessageId": row["internet_message_id"]}
        self.history.external_observation(PROVIDER, self.config.address, row["request_id"],
            {"request_id": row["request_id"], "business_id": mailbox.business_id, "mailbox": mailbox.ref,
                "fastmail_message_id": found["id"], "fastmail_thread_id": found["conversationId"], **evidence},
            kind="provider.business_copy")
        return {**found, **evidence}

    def attachments(self, mailbox, message_id):
        row = self._binding(mailbox, identity=message_id)
        if row is None:
            return self.fastmail.attachments(mailbox, message_id)
        if row["request_id"] not in self._cache:
            self.get_message(mailbox, message_id)
        return self._cache[row["request_id"]][1]["attachments"]

    def attachment(self, mailbox, message_id, attachment_id):
        row = self._binding(mailbox, identity=message_id)
        if row is None:
            return self.fastmail.attachment(mailbox, message_id, attachment_id)
        self.attachments(mailbox, message_id)
        content = self._cache[row["request_id"]][2].get(attachment_id)
        if content is None:
            raise ProviderError("attachment_outside_business", status=404)
        return content

    def send_draft(self, mailbox, draft_id, message):
        self._approved(mailbox, message)
        row = self._binding(mailbox, identity=draft_id)
        cached = self._cache.get(message.request_id)
        if row is None or row["request_id"] != message.request_id or cached is None:
            raise ProviderError("outlook_verified_draft_missing")
        raw = cached[0]
        content, _ = decode_message(raw)
        result = self._call("send", id=draft_id, internet_message_id=row["internet_message_id"],
            change_key=raw["ItemId"].get("ChangeKey"), mime_sha256=hashlib.sha256(content).hexdigest(), subject=message.subject)
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise ProviderError("outlook_submission_unconfirmed", uncertain=True)
        if result.get("provider_response") is not None:
            self.history.external_observation(PROVIDER, self.config.address, message.request_id,
                result["provider_response"], kind="provider.submission_result")
        return {"id": "outlook-web:" + message.request_id, "undoStatus": "pending"}

    def submission(self, mailbox, draft_id, message):
        row = self._binding(mailbox, identity=draft_id)
        if row is None or row["request_id"] != message.request_id:
            raise ProviderError("outlook_saved_message_missing")
        raw = self._sent(row)
        if raw is None:
            return None
        return {"id": "outlook-web:" + message.request_id, "undoStatus": "final", "sendAt": timestamp(raw["DateTimeSent"])}
