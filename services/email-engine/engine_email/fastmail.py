"""Fastmail JMAP operations. A POST is never implicitly retried."""

import json
import re
import base64
import hashlib
from email.message import EmailMessage
from email.policy import SMTP, default
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses
from urllib.parse import quote, urlsplit

import httpx

from .models import MAX_MESSAGE_BYTES

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
SESSION = "https://api.fastmail.com/jmap/session"
REQUEST_HEADER = "header:X-Qorqut-Request:asText"
FIELDS = ["id", "threadId", "mailboxIds", "keywords", "messageId", "inReplyTo", "references",
    "from", "to", "cc", "bcc", "replyTo", "subject", "sentAt", "receivedAt", "hasAttachment",
    "textBody", "htmlBody", "bodyValues", "attachments", REQUEST_HEADER]
HISTORY_FIELDS = ["id", "blobId", "threadId", "mailboxIds", "keywords", "size", "receivedAt",
    "headers", "messageId", "inReplyTo", "references", "sender", "from", "to", "cc", "bcc",
    "replyTo", "subject", "sentAt", "hasAttachment", "preview", "bodyStructure", "textBody",
    "htmlBody", "bodyValues", "attachments"]
HISTORY_BODY_FIELDS = ["partId", "blobId", "size", "name", "type", "charset", "disposition",
    "cid", "language", "location", "headers"]


def internet_message_id(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return "<" + value + ">" if re.fullmatch(r"[^<>\s@]+@[^<>\s@]+", value) else None


def mime_content_digest(content):
    """Delivery headers/BCC may change; approved addresses, body and attachments may not."""
    mail = BytesParser(policy=default).parsebytes(content)
    headers = {key: [str(value) for value in mail.get_all(key, [])]
        for key in ("subject", "message-id", "in-reply-to", "references")}
    # Outlook may prefix the sent Subject with a folding tab absent on delivery.
    headers["subject"] = [value[1:] if value.startswith(("\t", " ")) else value
        for value in headers["subject"]]
    # Exchange's Sent copy may unfold the same Message-ID with a tab while
    # SMTP delivery uses a space. Field-edge folding whitespace is not identity.
    for key in ("message-id", "in-reply-to", "references"):
        headers[key] = [value.strip() for value in headers[key]]
    for key in ("from", "to", "cc", "reply-to"):
        headers[key] = sorted((name, addr.lower()) for name, addr in getaddresses(mail.get_all(key, [])))
    parts = []
    for part in mail.walk():
        if part.is_multipart():
            continue
        body = part.get_payload(decode=True) or b""
        if part.get_content_maintype() == "text":
            body = body.replace(b"\r\n", b"\n")
        parts.append([part.get_content_type(), part.get_filename(), part.get_content_disposition(),
            str(part.get("content-id", "")), hashlib.sha256(body).hexdigest()])
    return hashlib.sha256(json.dumps([headers, parts], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProviderError(Exception):
    def __init__(self, code, *, status=None, uncertain=False, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.uncertain, self.retry_after = code, status, uncertain, retry_after


class Fastmail:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout, follow_redirects=False, trust_env=False)
        self._session = None

    def close(self):
        self.client.close()

    @staticmethod
    def validate_url(url):
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            trusted = host == "api.fastmail.com" or host.endswith((".api.fastmail.com", ".fastmailusercontent.com"))
            if (parsed.scheme != "https" or not trusted
                    or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_fastmail_url") from exc
        return url

    def request(self, method, url, *, body=None, content=None, content_type=None, binary=False, write=False):
        self.validate_url(url)
        headers = {"Authorization": "Bearer " + self.settings.api_token}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with self.client.stream(method, url, headers=headers, json=body, content=content) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_MESSAGE_BYTES * 2:
                        raise ProviderError("fastmail_response_too_large", uncertain=write)
                if response.status_code not in {200, 201}:
                    retry = response.headers.get("Retry-After", "")
                    raise ProviderError("fastmail_request_failed", status=response.status_code,
                        uncertain=write and response.status_code >= 500,
                        retry_after=min(int(retry), 3600) if retry.isdigit() else None)
                if binary:
                    return bytes(data)
                try:
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError()
                    return result
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProviderError("invalid_fastmail_response", uncertain=write) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("fastmail_connection_failed", uncertain=write) from exc

    def session(self):
        if self._session is None:
            data = self.request("GET", SESSION)
            account = self.settings.account_id or data.get("primaryAccounts", {}).get(MAIL)
            available = data.get("accounts", {}).get(account, {})
            if (not account or not {CORE, MAIL, SUBMISSION} <= set(data.get("capabilities", {}))
                    or not {MAIL, SUBMISSION} <= set(available.get("accountCapabilities", {}))
                    or available.get("isReadOnly")):
                raise ProviderError("fastmail_mail_and_send_access_required")
            for key in ("apiUrl", "uploadUrl", "downloadUrl"):
                self.validate_url(data.get(key))
            self._session = {**data, "accountId": account}
        return self._session

    @property
    def account_id(self):
        return self.session()["accountId"]

    def call(self, method, arguments=None, *, write=False):
        session = self.session()
        args = {**(arguments or {}), "accountId": session["accountId"]}
        response = self.request("POST", session["apiUrl"], body={"using": [CORE, MAIL, SUBMISSION],
            "methodCalls": [[method, args, "request"]]}, write=write)
        for entry in response.get("methodResponses", []):
            if not isinstance(entry, list) or len(entry) != 3 or entry[2] != "request":
                continue
            name, data, _ = entry
            if name == "error":
                self.raise_error(data, uncertain=write)
            if name == method and isinstance(data, dict) and data.get("accountId") == session["accountId"]:
                return data
        raise ProviderError("invalid_jmap_response", uncertain=write)

    @staticmethod
    def raise_error(data, *, uncertain=False):
        kind = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z]{1,60}", kind):
            kind = "unknown"
        raise ProviderError("jmap_" + kind, uncertain=uncertain and kind in {"serverFail", "unknown"},
            status=429 if kind == "rateLimit" else 410 if kind == "cannotCalculateChanges"
                else None if kind in {"serverFail", "unknown"} else 400)

    def created(self, result):
        value = (result.get("created") or {}).get("prepared")
        if not isinstance(value, dict) or not value.get("id"):
            error = (result.get("notCreated") or {}).get("prepared")
            if error:
                self.raise_error(error, uncertain=True)
            raise ProviderError("missing_jmap_created_id", uncertain=True)
        return value

    def folders(self):
        result = self.call("Mailbox/get", {"properties": ["id", "name", "role", "myRights"]})
        found = {item["id"]: item for item in result["list"]}
        self.validate_folders(found)
        return found

    def validate_folders(self, found):
        for box in self.settings.mailboxes:
            folder = found.get(box.folder_id)
            if not folder or folder.get("role") or not folder.get("myRights", {}).get("mayReadItems"):
                raise ProviderError("business_folder_missing_or_not_dedicated")
        for identity in self.settings.history.private_folder_ids if self.settings.history else []:
            folder = found.get(identity)
            if not folder or folder.get("role") or not folder.get("myRights", {}).get("mayReadItems"):
                raise ProviderError("private_history_folder_missing_or_not_dedicated")

    def owner(self, raw):
        if self.settings.history and any(raw.get("mailboxIds", {}).get(folder)
                for folder in self.settings.history.private_folder_ids):
            return None
        matches = [box for box in self.settings.mailboxes if raw.get("mailboxIds", {}).get(box.folder_id)]
        # Never expose a message filed into multiple business routes.
        return matches[0] if len(matches) == 1 else None

    def get_raw(self, ids, *, metadata=False, include_mime=False):
        args = {"ids": ids, "properties": ["id", "mailboxIds"] if metadata else [*FIELDS, *(["blobId"] if include_mime else [])]}
        if not metadata:
            args.update(fetchAllBodyValues=True, maxBodyValueBytes=MAX_MESSAGE_BYTES)
        return self.call("Email/get", args)

    def _scoped_raw(self, mailbox, identity, *, include_mime=False):
        metadata = self.get_raw([identity], metadata=True).get("list", [])
        if len(metadata) != 1 or self.owner(metadata[0]) != mailbox:
            raise ProviderError("message_outside_business", status=404)
        items = self.get_raw([identity], **({"include_mime": True} if include_mime else {})).get("list", [])
        if len(items) != 1 or self.owner(items[0]) != mailbox:
            raise ProviderError("message_outside_business", status=404)
        return items[0]

    def get_message(self, mailbox, identity):
        return self.normalize(self._scoped_raw(mailbox, identity))

    def get_message_mime(self, mailbox, identity):
        """Read one scoped immutable original and its MIME, never a foreign body."""
        raw = self._scoped_raw(mailbox, identity, include_mime=True)
        if not raw.get("blobId"):
            raise ProviderError("provider_message_mime_missing")
        return self.normalize(raw), self.download_blob(raw["blobId"])

    @staticmethod
    def normalize(raw):
        address = lambda item: {"emailAddress": {"address": item.get("email", ""), "name": item.get("name") or ""}}
        text = raw.get("textBody") or raw.get("htmlBody") or []
        values = raw.get("bodyValues", {})
        body = "\n".join(values.get(part["partId"], {}).get("value", "") for part in text)
        attachments = [{"id": part["blobId"], "name": part.get("name") or "attachment",
            "contentType": part.get("type") or "application/octet-stream", "size": part.get("size", 0),
            "isInline": part.get("disposition") == "inline"} for part in raw.get("attachments") or []]
        message_ids = raw.get("messageId") or []
        return {"id": raw["id"], "conversationId": raw["threadId"], "mailboxIds": raw["mailboxIds"],
            "subject": raw.get("subject") or "", "from": address((raw.get("from") or [{}])[0]),
            **{key: [address(item) for item in raw.get(source) or []] for key, source in
                (("toRecipients", "to"), ("ccRecipients", "cc"), ("bccRecipients", "bcc"), ("replyTo", "replyTo"))},
            "body": {"contentType": "Text" if raw.get("textBody") else "HTML", "content": body},
            "bodyTruncated": any(not values.get(part["partId"])
                or values[part["partId"]].get("isTruncated")
                or values[part["partId"]].get("isEncodingProblem") for part in text),
            "requestId": raw.get(REQUEST_HEADER), "isDraft": bool(raw.get("keywords", {}).get("$draft")),
            "isRead": bool(raw.get("keywords", {}).get("$seen")), "hasAttachments": bool(attachments),
            "attachments": attachments, "internetMessageId": "<" + message_ids[0].strip("<>") + ">" if message_ids else None,
            "references": raw.get("references") or [], "inReplyTo": raw.get("inReplyTo") or [],
            "receivedDateTime": raw.get("receivedAt"), "sentDateTime": raw.get("sentAt"), "webLink": None}

    def delta(self, cursor=None):
        saved = json.loads(cursor) if cursor else {}
        initial = not saved or saved.get("phase") == "initial"
        if initial:
            state = saved.get("state") or self.get_raw([], metadata=True)["state"]
            result = self.call("Email/query", {"filter": {"operator": "OR", "conditions": [
                {"inMailbox": box.folder_id} for box in self.settings.mailboxes]},
                "sort": [{"property": "receivedAt", "isAscending": True}], "position": saved.get("position", 0),
                "limit": 100, "calculateTotal": True})
            if saved.get("queryState") and saved["queryState"] != result["queryState"]:
                raise ProviderError("initial_query_changed", status=410)
            ids, destroyed = result["ids"], []
            position = result["position"] + len(ids)
            more = position < result["total"]
            if more and not ids:
                raise ProviderError("invalid_jmap_pagination")
            next_cursor = {"state": state, **({"phase": "initial", "position": position,
                "queryState": result["queryState"]} if more else {})}
        else:
            result = self.call("Email/changes", {"sinceState": saved["state"], "maxChanges": 100})
            ids = list(dict.fromkeys([*result["created"], *result["updated"]]))
            destroyed, more = result["destroyed"], result["hasMoreChanges"]
            next_cursor = {"state": result["newState"]}
        # Only fetch bodies after checking current folder ownership. Personal mail is not cached.
        metadata = self.get_raw(ids, metadata=True)["list"] if ids else []
        allowed = [item["id"] for item in metadata if self.owner(item)]
        bodies = self.get_raw(allowed)["list"] if allowed else []
        items = [self.normalize(item) for item in bodies if self.owner(item)]
        present = {item["id"] for item in items}
        return {"items": items, "destroyed": list(set(destroyed) | (set(ids) - present)),
            "cursor": json.dumps(next_cursor, sort_keys=True), "complete": not more}

    def collect_history(self, archive):
        """Only changes after the explicitly pinned start state; never initial import."""
        account_wide = archive.config.capture_scope == "account"
        cursor = archive.cursor()
        archive.metadata_observation("provider.capture_configuration", {
            "capture_scope": archive.config.capture_scope,
            "business_folder_ids": sorted(box.folder_id for box in self.settings.mailboxes),
            "private_folder_ids": sorted(archive.config.private_folder_ids)}, provenance={"cursor": cursor})
        catalog = self.call("Mailbox/get", {"ids": None})
        if (not isinstance(catalog.get("list"), list) or catalog.get("notFound")
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in catalog["list"])):
            raise ProviderError("history_mailbox_catalog_invalid")
        archive.metadata_observation("provider.mailbox_catalog", {
            "catalog": {**catalog, "list": sorted(catalog["list"], key=lambda item: item["id"])}},
            provenance={"cursor": cursor, "capture_scope": archive.config.capture_scope})
        if not account_wide:
            self.validate_folders({item["id"]: item for item in catalog["list"]})
        delta = self.call("Email/changes", {"sinceState": cursor, "maxChanges": 20})
        if (not isinstance(delta.get("newState"), str) or not delta["newState"]
                or not isinstance(delta.get("hasMoreChanges"), bool)
                or any(not isinstance(delta.get(key), list) or any(not isinstance(identity, str) for identity in delta[key])
                    for key in ("created", "updated", "destroyed"))):
            raise ProviderError("history_changes_invalid")
        provenance = {"capture_scope": archive.config.capture_scope, "since_state": cursor,
            "new_state": delta["newState"], "mailbox_state": catalog.get("state")}
        identities = list(dict.fromkeys([*delta["created"], *delta["updated"]]))
        for identity in identities:
            message_provenance = {**provenance, "change": "created" if identity in delta["created"] else "updated"}
            metadata_response = self.get_raw([identity], metadata=True)
            metadata = metadata_response.get("list", [])
            if not isinstance(metadata, list):
                raise ProviderError("history_message_metadata_invalid")
            if not metadata:
                if account_wide:
                    if identity not in metadata_response.get("notFound", []):
                        raise ProviderError("history_message_metadata_invalid")
                    archive.observation(identity, {"id": identity, "unavailable": True,
                        "reason": "not_found_after_change"}, provenance=message_provenance)
                    archive.collection_gap("history_message_unavailable", message_id=identity)
                elif archive.observed(identity):
                    archive.observation(identity, {"id": identity, "removed": True}, provenance=message_provenance)
                continue
            if len(metadata) != 1 or not isinstance(metadata[0], dict) or metadata[0].get("id") != identity:
                raise ProviderError("history_message_metadata_invalid")
            raw = metadata[0]
            private = any(raw.get("mailboxIds", {}).get(folder) for folder in archive.config.private_folder_ids)
            if not account_wide and not self.owner(raw) and not private:
                if archive.observed(identity):
                    archive.observation(identity, {"id": identity, "outside_capture_folders": True}, provenance=message_provenance)
                continue
            response = self.call("Email/get", {"ids": [identity], "properties": HISTORY_FIELDS,
                "bodyProperties": HISTORY_BODY_FIELDS, "fetchAllBodyValues": True,
                "maxBodyValueBytes": MAX_MESSAGE_BYTES})
            values = response.get("list", [])
            if not isinstance(values, list):
                raise ProviderError("history_message_metadata_invalid")
            if not values:
                raise ProviderError("history_message_disappeared")
            if len(values) != 1 or not isinstance(values[0], dict) or values[0].get("id") != identity:
                raise ProviderError("history_message_metadata_invalid")
            snapshot = values[0]
            # Scoped capture rechecks ownership after a possible folder move.
            private = any(snapshot.get("mailboxIds", {}).get(folder) for folder in archive.config.private_folder_ids)
            if not account_wide and not self.owner(snapshot) and not private:
                if archive.observed(identity):
                    archive.observation(identity, {"id": identity, "outside_capture_folders": True}, provenance=message_provenance)
                continue
            if not snapshot.get("blobId"):
                raise ProviderError("history_mime_blob_missing")
            mime = self.download_blob(snapshot["blobId"])
            snapshot = {**snapshot, "mime_base64": base64.b64encode(mime).decode(),
                "mime_sha256": hashlib.sha256(mime).hexdigest(), "mime_size": len(mime)}
            archive.observation(identity, snapshot, provenance={**message_provenance, "email_state": response.get("state")})
        for identity in delta["destroyed"]:
            if account_wide or archive.observed(identity):
                archive.observation(identity, {"id": identity, "removed": True},
                    provenance={**provenance, "change": "destroyed"})
        # Advance only after every observed message has a durable encrypted record.
        archive.advance(delta["newState"])
        return {"state": "partial" if delta["hasMoreChanges"] else "synced"}

    @staticmethod
    def mime(message, parent=None):
        # Keep long ASCII message-id tokens intact instead of RFC 2047 encoding.
        mail = EmailMessage(policy=SMTP.clone(max_line_length=998))
        mail["From"] = formataddr((message.from_name, message.from_address))
        for key, values in (("To", message.to), ("Cc", message.cc), ("Bcc", message.bcc)):
            if values:
                mail[key] = ", ".join(values)
        mail["Subject"], mail["Date"] = message.subject, formatdate(usegmt=True)
        mail["Message-ID"] = f"<qorqut.{message.digest()}@{message.from_address.split('@')[1]}>"
        mail["X-Qorqut-Request"] = message.request_id
        if message.reply_to:
            mail["Reply-To"] = message.reply_to
        if parent:
            identity = parent.get("internetMessageId")
            if not identity:
                raise ProviderError("reply_parent_missing_message_id")
            mail["In-Reply-To"] = identity
            mail["References"] = " ".join([*("<" + value.strip("<>") + ">" for value in parent.get("references", [])[-30:]), identity])
        mail.set_content(message.body)
        for attachment in message.attachments:
            main, sub = attachment.content_type.split("/", 1)
            mail.add_attachment(attachment.content(), maintype=main, subtype=sub, filename=attachment.filename)
        return mail.as_bytes()

    def identity(self, mailbox, sender, message=None):
        if message is not None:
            try:
                self.settings.sender(mailbox, message)
            except ValueError as exc:
                raise ProviderError("sender_outside_business") from exc
        elif not mailbox.accepts_sender(sender):
            raise ProviderError("sender_outside_business")
        identities = self.call("Identity/get")["list"]
        matches = [item for item in identities if item.get("email", "").lower() == sender]
        if len(matches) != 1:
            raise ProviderError("fastmail_sender_identity_missing_or_ambiguous")
        # Provider-side extra recipients would bypass the approved envelope.
        if matches[0].get("bcc") or matches[0].get("replyTo"):
            raise ProviderError("fastmail_identity_remove_auto_bcc_and_reply_to")
        return matches[0]["id"]

    def create_draft(self, mailbox, message):
        self.identity(mailbox, message.from_address, message)
        self.folders()
        parent = self.get_message(mailbox, message.reply_message_id) if message.reply_message_id else None
        content = self.mime(message, parent)
        upload = self.session()["uploadUrl"].replace("{accountId}", quote(self.account_id, safe=""))
        blob = self.request("POST", upload, content=content, content_type="message/rfc822", write=True)
        if blob.get("accountId") != self.account_id or not blob.get("blobId"):
            raise ProviderError("invalid_fastmail_upload", uncertain=True)
        draft = self.created(self.call("Email/import", {"emails": {"prepared": {"blobId": blob["blobId"],
            "mailboxIds": {mailbox.folder_id: True}, "keywords": {"$draft": True}}}}, write=True))
        return self.get_message(mailbox, draft["id"])

    def find_observed_mime(self, mailbox, message_id, content):
        """Reconcile an exact routed Outlook copy without reading a foreign body."""
        identity = internet_message_id(message_id)
        if identity is None:
            raise ProviderError("outlook_import_message_id_missing")
        result = self.call("Email/query", {"filter": {"header": ["Message-ID", identity]},
            "limit": 10, "calculateTotal": True})
        if (not isinstance(result.get("ids"), list) or type(result.get("total")) is not int
                or result["total"] != len(result["ids"]) or result["total"] > 10):
            raise ProviderError("outlook_import_lookup_ambiguous")
        if not result["ids"]:
            return None
        values = self.call("Email/get", {"ids": result["ids"], "properties": ["id", "blobId", "mailboxIds", "messageId"]})
        if values.get("notFound") or len(values.get("list", [])) != len(result["ids"]):
            raise ProviderError("outlook_import_lookup_incomplete")
        matches = [item for item in values["list"]
            if identity in {internet_message_id(value) for value in item.get("messageId", [])}]
        if not matches:
            return None
        if len(matches) != 1 or self.owner(matches[0]) != mailbox:
            raise ProviderError("outlook_import_conflicting_owner")
        existing = self.download_blob(matches[0]["blobId"])
        if mime_content_digest(existing) != mime_content_digest(content):
            raise ProviderError("outlook_import_content_conflict")
        return self.get_message(mailbox, matches[0]["id"])

    def import_observed_mime(self, mailbox, content, *, message_id, received_at=None, is_read=False):
        """Copy complete provider MIME into its proven business folder; never submit mail."""
        if mailbox not in self.settings.mailboxes or not isinstance(content, bytes) or len(content) > MAX_MESSAGE_BYTES:
            raise ProviderError("outlook_import_invalid_content")
        mail = BytesParser(policy=default).parsebytes(content)
        identity = internet_message_id(message_id)
        if identity is None or len(mail.get_all("Message-ID", [])) != 1 or internet_message_id(str(mail["Message-ID"])) != identity:
            raise ProviderError("outlook_import_message_id_changed")
        self.folders()
        upload = self.session()["uploadUrl"].replace("{accountId}", quote(self.account_id, safe=""))
        try:
            blob = self.request("POST", upload, content=content, content_type="message/rfc822", write=True)
        except ProviderError as exc:
            # Uploading a blob alone cannot create a message; a retry is safe.
            raise ProviderError(exc.code, status=exc.status, retry_after=exc.retry_after) from exc
        if blob.get("accountId") != self.account_id or not blob.get("blobId"):
            raise ProviderError("invalid_fastmail_upload", uncertain=True)
        values = {"blobId": blob["blobId"], "mailboxIds": {mailbox.folder_id: True},
            "keywords": {"$seen": True} if is_read else {}}
        if received_at is not None:
            values["receivedAt"] = received_at
        imported = self.created(self.call("Email/import", {"emails": {"prepared": values}}, write=True))
        try:
            return self.get_message(mailbox, imported["id"])
        except ProviderError as exc:
            # Import already succeeded, even if the readback is unavailable.
            raise ProviderError(exc.code, status=exc.status, uncertain=True) from exc

    def find_draft(self, mailbox, request_id):
        result = self.call("Email/query", {"filter": {"operator": "AND", "conditions": [
            {"inMailbox": mailbox.folder_id}, {"header": ["X-Qorqut-Request", request_id]}]},
            "limit": 2, "calculateTotal": True})
        if result["total"] > 1:
            raise ProviderError("ambiguous_provider_draft")
        return self.get_message(mailbox, result["ids"][0]) if result["ids"] else None

    def attachments(self, mailbox, message_id):
        return self.get_message(mailbox, message_id)["attachments"]

    def attachment(self, mailbox, message_id, attachment_id):
        selected = next((item for item in self.attachments(mailbox, message_id) if item["id"] == attachment_id), None)
        if selected is None:
            raise ProviderError("attachment_outside_business", status=404)
        return self.download_blob(attachment_id)

    def download_blob(self, blob_id):
        url = self.session()["downloadUrl"]
        for name, value in {"accountId": self.account_id, "blobId": blob_id,
                "name": "attachment", "type": "application/octet-stream"}.items():
            url = url.replace("{" + name + "}", quote(value, safe=""))
        return self.request("GET", url, binary=True)

    def send_draft(self, mailbox, draft_id, message):
        identity = self.identity(mailbox, message.from_address, message)
        return self.created(self.call("EmailSubmission/set", {"create": {"prepared": {
            "emailId": draft_id, "identityId": identity, "envelope": {
                "mailFrom": {"email": message.from_address},
                "rcptTo": [{"email": value} for value in message.envelope_recipients()]}}},
            "onSuccessUpdateEmail": {"#prepared": {"keywords/$draft": None}}}, write=True))

    def submission(self, mailbox, draft_id, message):
        result = self.call("EmailSubmission/query", {"filter": {"emailIds": [draft_id]}, "limit": 2, "calculateTotal": True})
        if result["total"] > 1:
            raise ProviderError("ambiguous_provider_submission")
        if not result["ids"]:
            return None
        found = self.call("EmailSubmission/get", {"ids": result["ids"]})["list"]
        if len(found) != 1:
            raise ProviderError("invalid_provider_submission")
        item = found[0]
        envelope = item.get("envelope") or {}
        if (item.get("emailId") != draft_id or envelope.get("mailFrom", {}).get("email", "").lower() != message.from_address
                or sorted(value["email"].lower() for value in envelope.get("rcptTo", [])) != sorted(message.envelope_recipients())):
            raise ProviderError("provider_submission_changed")
        return item
