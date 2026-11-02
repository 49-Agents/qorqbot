"""Fastmail JMAP operations. A POST is never implicitly retried."""

import json
import re
import base64
import hashlib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP, default
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses
from urllib.parse import quote, urlsplit

import httpx

from .models import MAX_MESSAGE_BYTES, MAX_ATTACHMENT_BYTES

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
VACATION = "urn:ietf:params:jmap:vacationresponse"
MASKED_EMAIL = "https://www.fastmail.com/dev/maskedemail"
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
ACCOUNT_READ_BODY_BYTES = 128 * 1024
ACCOUNT_MAX_RESULTS = 100
# Wire form for composed account mail. CRLF line ends are required by RFC 5322 (section 2.1) and are what the earlier
# serialization lacked; JMAP Email/import refuses a message that is not valid RFC 5322 (RFC 8621 section 4.8,
# ``invalidEmail``). The remaining choices are conservative interoperability hardening, not requirements: RFC 8621 allows
# internationalized headers and RFC 5322 only recommends a Message-ID, so 7-bit output (non-ASCII text goes through
# RFC 2047 / quoted-printable or base64) and the 998-octet line ceiling (a long References or Message-ID token is never
# RFC 2047 encoded) simply keep the bytes acceptable to the strictest reader.
ACCOUNT_MIME_POLICY = SMTP.clone(max_line_length=998, cte_type="7bit")
DIAGNOSTIC_TEXT_LIMIT = 300
DIAGNOSTIC_PROPERTIES_LIMIT = 10


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


def diagnostic_text(value, limit=DIAGNOSTIC_TEXT_LIMIT):
    """Bounded single-line provider text: printable characters only, whitespace collapsed, never a stack of headers."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return text[:limit] or None


def provider_detail(data, kind=None):
    """The safe, bounded part of a provider rejection (JMAP SetError/method error or RFC 7807 problem document).

    Keeps the typed error name, a bounded human description and the names of the rejected properties. It never carries
    request or response bodies, headers or credentials, and stays private to the durable engine receipt."""
    if not isinstance(data, dict):
        return None
    detail = {}
    name = diagnostic_text(kind or data.get("type"), 120)
    if name:
        detail["type"] = name
    description = diagnostic_text(data.get("description") or data.get("detail") or data.get("title"))
    if description:
        detail["description"] = description
    properties = data.get("properties")
    if isinstance(properties, list):
        names = [diagnostic_text(item, 60) for item in properties[:DIAGNOSTIC_PROPERTIES_LIMIT] if isinstance(item, str)]
        names = [item for item in names if item]
        if names:
            detail["properties"] = names
    return detail or None


class ProviderError(Exception):
    def __init__(self, code, *, status=None, uncertain=False, retry_after=None, detail=None):
        super().__init__(code)
        self.code, self.status, self.uncertain, self.retry_after = code, status, uncertain, retry_after
        # Bounded provider diagnostics for the private durable receipt only (see provider_detail).
        self.detail = detail


def jmap_utc_date(value):
    """Serialize provider dates at the JMAP boundary (RFC 8620 section 1.4)."""
    try:
        if not isinstance(value, str):
            raise ValueError()
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError()
        return instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ProviderError("invalid_import_received_at") from exc


class Fastmail:
    def __init__(self, settings, *, client=None, clock=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout, follow_redirects=False, trust_env=False)
        self._session = None
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
                    problem = None
                    if len(data) <= 16 * 1024:
                        try:
                            problem = provider_detail(json.loads(data))
                        except (ValueError, UnicodeDecodeError):
                            problem = None  # an HTML/plain error page is never retained
                    raise ProviderError("fastmail_request_failed", status=response.status_code,
                        uncertain=write and response.status_code >= 500,
                        retry_after=min(int(retry), 3600) if retry.isdigit() else None, detail=problem)
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

    def call(self, method, arguments=None, *, write=False, using=None):
        session = self.session()
        args = {**(arguments or {}), "accountId": session["accountId"]}
        response = self.request("POST", session["apiUrl"], body={"using": using or [CORE, MAIL, SUBMISSION],
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

    def account_folder_catalog(self):
        """Return folders plus the JMAP state used by optimistic writes."""
        result = self.call("Mailbox/get", {"ids": None,
            "properties": ["id", "name", "parentId", "role", "sortOrder", "isSubscribed", "myRights"]})
        values = result.get("list")
        if not isinstance(values, list) or result.get("notFound") or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in values):
            raise ProviderError("invalid_account_folder_catalog")
        return {"state": result.get("state"), "folders": values}

    def account_folders(self):
        """Preserve the list-only helper used by provider internals."""
        return self.account_folder_catalog()["folders"]

    def account_identities(self):
        result = self.call("Identity/get")
        values = result.get("list")
        if not isinstance(values, list):
            raise ProviderError("invalid_account_identity_catalog")
        # Do not return provider settings such as auto-Bcc or reply-to overrides.
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in values):
            raise ProviderError("invalid_account_identity_catalog")
        return [{key: item.get(key) for key in ("id", "name", "email", "replyTo", "bcc")} for item in values]

    def account_search(self, params, *, limit=50, position=0):
        conditions = []
        mapping = {"q": "text", "from": "from", "to": "to", "subject": "subject",
            "in_mailbox": "inMailbox", "has_keyword": "hasKeyword", "before": "before",
            "after": "after"}
        for key, value in params.items():
            if key not in mapping:
                continue
            if key in {"before", "after"}:
                value = value
            conditions.append({mapping[key]: value})
        filter_value = conditions[0] if len(conditions) == 1 else {"operator": "AND", "conditions": conditions}
        result = self.call("Email/query", {"filter": filter_value, "sort": [{"property": "receivedAt", "isAscending": False}],
            "position": position, "limit": limit, "calculateTotal": True})
        if not isinstance(result.get("ids"), list) or type(result.get("total")) is not int:
            raise ProviderError("invalid_account_search_response")
        return {"ids": result["ids"], "position": result.get("position", position),
            "total": result["total"], "query_state": result.get("queryState")}

    def account_thread(self, identity, *, limit=50, position=0):
        if not isinstance(identity, str) or not identity or not 1 <= limit <= ACCOUNT_MAX_RESULTS or position < 0:
            raise ProviderError("invalid_account_thread_page", status=400)
        # Thread/get has no query pagination arguments. Bound the returned
        # email IDs locally after fetching the named thread.
        result = self.call("Thread/get", {"ids": [identity], "properties": ["id", "emailIds"]})
        values = result.get("list", [])
        if len(values) != 1 or values[0].get("id") != identity:
            raise ProviderError("account_thread_not_found", status=404)
        email_ids = values[0].get("emailIds")
        if not isinstance(email_ids, list) or any(not isinstance(item, str) for item in email_ids):
            raise ProviderError("invalid_account_thread_response")
        return {**values[0], "emailIds": email_ids[position:position + limit],
            "position": position, "total": len(email_ids),
            "has_more": position + limit < len(email_ids)}

    def account_message(self, identity, *, body=True):
        if not isinstance(identity, str) or not identity:
            raise ProviderError("invalid_account_message_id", status=400)
        properties = [*FIELDS, "blobId"] if body else ["id", "threadId", "mailboxIds", "keywords", "subject",
            "from", "to", "cc", "replyTo", "receivedAt", "sentAt", "hasAttachment", "attachments"]
        args = {"ids": [identity], "properties": properties}
        if body:
            args.update(fetchAllBodyValues=True, maxBodyValueBytes=ACCOUNT_READ_BODY_BYTES)
        result = self.call("Email/get", args)
        values = result.get("list", [])
        if len(values) != 1 or values[0].get("id") != identity:
            raise ProviderError("account_message_not_found", status=404)
        return {"message": self.normalize(values[0]), "expected_state": result.get("state")}

    def account_raw(self, identity, *, metadata=False):
        args = {"ids": [identity], "properties": ["id", "mailboxIds", "keywords", "attachments"] if metadata else [*FIELDS, "blobId"]}
        if not metadata:
            args.update(fetchAllBodyValues=True, maxBodyValueBytes=ACCOUNT_READ_BODY_BYTES)
        result = self.call("Email/get", args)
        values = result.get("list", [])
        if len(values) != 1 or values[0].get("id") != identity:
            raise ProviderError("account_message_not_found", status=404)
        return values[0]

    def account_attachment(self, identity, attachment_id):
        raw = self.account_raw(identity, metadata=True)
        found = next((item for item in raw.get("attachments", []) if item.get("blobId") == attachment_id), None)
        if found is None:
            raise ProviderError("account_attachment_not_found", status=404)
        return self.download_blob(attachment_id)

    def _account_folder(self, folder_id):
        found = next((item for item in self.account_folders() if item["id"] == folder_id), None)
        if found is None:
            raise ProviderError("account_folder_not_found", status=404)
        return found

    def account_move(self, identity, destination, *, expected_state=None, protected=()):
        raw = self.account_raw(identity, metadata=True)
        folder = self._account_folder(destination)
        if not folder.get("myRights", {}).get("mayAddItems"):
            raise ProviderError("account_folder_write_denied", status=403)
        current = raw.get("mailboxIds") or {}
        if not current:
            raise ProviderError("account_message_mailbox_missing")
        updates = {"mailboxIds/" + value: (True if value == destination else None) for value in current}
        updates["mailboxIds/" + destination] = True
        args = {"update": {identity: updates}}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Email/set", args, write=True)
        return {"message_id": identity, "destination_folder_id": destination, "provider": result,
            **({"partial": True} if result.get("notUpdated") else {})}

    def account_update_message(self, identity, action, *, expected_state=None):
        raw = self.account_raw(identity, metadata=True)
        updates = {}
        for keyword in action.add_keywords:
            updates["keywords/" + keyword] = True
        for keyword in action.remove_keywords:
            updates["keywords/" + keyword] = None
        for name, value in (("$seen", action.seen), ("$flagged", action.flagged), ("$answered", action.answered)):
            if value is not None:
                updates["keywords/" + name] = bool(value) or None
        destination = action.move_to_folder_id
        if action.trash is True or action.restore is True:
            role = "trash" if action.trash is True else "inbox"
            destination = next((item["id"] for item in self.account_folders() if item.get("role") == role), None)
            if destination is None:
                raise ProviderError("account_destination_folder_missing", status=503)
        if destination:
            folder = self._account_folder(destination)
            if not folder.get("myRights", {}).get("mayAddItems"):
                raise ProviderError("account_folder_write_denied", status=403)
            for value in raw.get("mailboxIds", {}):
                updates["mailboxIds/" + value] = None
            updates["mailboxIds/" + destination] = True
        args = {"update": {identity: updates}}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Email/set", args, write=True)
        return {"message_id": identity, "provider": result,
            **({"partial": True} if result.get("notUpdated") else {})}

    def account_set_flags(self, identity, *, seen=None, flagged=None, answered=None, expected_state=None):
        updates = {}
        for name, value in (("$seen", seen), ("$flagged", flagged), ("$answered", answered)):
            if value is not None:
                updates["keywords/" + name] = bool(value) or None
        args = {"update": {identity: updates}}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Email/set", args, write=True)
        return {"message_id": identity, "flags": {key[9:]: value for key, value in updates.items()}, "provider": result,
            **({"partial": True} if result.get("notUpdated") else {})}

    def account_create_folder(self, name, *, parent_id=None):
        result = self.call("Mailbox/set", {"create": {"prepared": {"name": name, **({"parentId": parent_id} if parent_id else {})}}}, write=True)
        return {"folder": self.created(result), "provider": result,
            **({"partial": True} if result.get("notCreated") else {})}

    def account_rename_folder(self, folder_id, name, *, expected_state=None, protected=()):
        self._account_folder(folder_id)
        if folder_id in protected:
            raise ProviderError("account_protected_folder", status=409)
        args = {"update": {folder_id: {"name": name}}}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Mailbox/set", args, write=True)
        return {"folder_id": folder_id, "name": name, "provider": result,
            **({"partial": True} if result.get("notUpdated") else {})}

    def account_delete_folder(self, folder_id, *, expected_state=None, protected=()):
        folder = self._account_folder(folder_id)
        if folder_id in protected or folder.get("role"):
            raise ProviderError("account_protected_folder", status=409)
        args = {"destroy": [folder_id]}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Mailbox/set", args, write=True)
        return {"folder_id": folder_id, "provider": result,
            **({"partial": True} if result.get("notDestroyed") else {})}

    def vacation_available(self):
        session = self.session()
        account = session.get("accounts", {}).get(session["accountId"], {})
        return VACATION in session.get("capabilities", {}) and VACATION in account.get("accountCapabilities", {})

    def account_set_vacation(self, value):
        if not self.vacation_available():
            raise ProviderError("account_vacation_unavailable", status=501)
        # The API intentionally accepts only the provider's typed vacation fields.
        if not isinstance(value, dict) or set(value) - {"id", "isEnabled", "fromDate", "toDate", "subject", "textBody"}:
            raise ProviderError("invalid_vacation_fields", status=400)
        result = self.call("VacationResponse/set", {"update": {"singleton": value}}, write=True,
            using=[CORE, VACATION])
        return {"provider": result, **({"partial": True} if result.get("notUpdated") else {})}

    def masked_email_available(self):
        return MASKED_EMAIL in self.session().get("capabilities", {})

    def account_set_masked_email(self, value):
        if not self.masked_email_available():
            raise ProviderError("account_masked_email_unavailable", status=501)
        if not isinstance(value, dict) or not value.get("id") or set(value) - {"id", "state", "description", "forDomain", "url", "emailPrefix"}:
            raise ProviderError("invalid_masked_email_fields", status=400)
        result = self.call("MaskedEmail/set", {"update": {value["id"]: value}}, write=True,
            using=[CORE, MASKED_EMAIL])
        return {"provider": result, **({"partial": True} if result.get("notUpdated") else {})}

    def _account_composed_content(self, action):
        """Build the immutable body/attachment expectation for account compose."""
        body = action.body
        attachments = []
        if action.forward_message_id:
            parent = self.account_raw(action.forward_message_id)
            text_parts = parent.get("textBody") or parent.get("htmlBody") or []
            values = parent.get("bodyValues") or {}
            if any(not isinstance(part, dict) or not isinstance(part.get("partId"), str)
                    or not isinstance(values.get(part["partId"]), dict)
                    or values[part["partId"]].get("isTruncated")
                    or values[part["partId"]].get("isEncodingProblem")
                    for part in text_parts):
                raise ProviderError("account_source_body_truncated", status=409)
            normalized = self.normalize(parent)
            sender = normalized.get("from", {}).get("emailAddress", {}).get("address", "")
            subject = normalized.get("subject", "")
            quoted = normalized.get("body", {}).get("content", "")
            body = (action.body + "\n\n---------- Forwarded message ----------\n"
                f"From: {sender}\nSubject: {subject}\n\n{quoted}")
            for source in parent.get("attachments") or []:
                blob_id = source.get("blobId")
                content_type = source.get("type") or "application/octet-stream"
                filename = source.get("name") or "attachment"
                if not isinstance(blob_id, str) or "/" not in content_type:
                    raise ProviderError("account_source_attachment_invalid", status=409)
                content = self.download_blob(blob_id)
                if source.get("size") is not None and source.get("size") != len(content):
                    raise ProviderError("account_source_attachment_changed", status=409)
                attachments.append({"name": filename, "type": content_type,
                    "content": content, "sha256": hashlib.sha256(content).hexdigest()})
        for attachment in action.attachments:
            content = attachment.content()
            attachments.append({"name": attachment.filename, "type": attachment.content_type,
                "content": content, "sha256": attachment.sha256})
        if sum(len(item["content"]) for item in attachments) > MAX_ATTACHMENT_BYTES:
            raise ProviderError("account_attachments_too_large", status=413)
        return body, attachments

    def _account_identity(self, action):
        identities = self.account_identities()
        if action.identity_id:
            identity = next((item for item in identities if item.get("id") == action.identity_id), None)
        else:
            matches = [item for item in identities
                if item.get("email", "").lower() == action.from_address.lower()]
            identity = matches[0] if len(matches) == 1 else None
        if (identity is None or identity.get("email", "").lower() != action.from_address.lower()
                or identity.get("bcc") or identity.get("replyTo")):
            raise ProviderError("account_sender_identity_denied", status=403)
        return identity

    @staticmethod
    def account_message_id(account_id, action):
        """The RFC 5322 msg-id of one account request. Stable per account request, so a replayed request names the same
        Message-ID; the domain is the approved sender's, which the request validator already restricts to ASCII."""
        digest = hashlib.sha256(f"{account_id}\0{action.request_id}".encode()).hexdigest()[:40]
        identity = internet_message_id(f"qorqut.account.{digest}@{action.from_address.rsplit('@', 1)[1]}")
        if identity is None:
            raise ProviderError("account_message_id_invalid", status=400)
        return identity

    def account_mime(self, action, body, attachments, *, message_id, in_reply_to=None, references=()):
        """Serialize the approved account message as RFC 5322 bytes (CRLF line ends, 7-bit, plus a stable Message-ID and a
        consistent Date for correlation and interoperability), then prove the bytes carry exactly the approved sender,
        recipients, subject, body and attachments before anything is uploaded."""
        mail = EmailMessage(policy=ACCOUNT_MIME_POLICY)
        sent_at = self.clock()
        mail["From"] = formataddr((action.from_name, action.from_address))
        mail["To"] = ", ".join(action.to)
        if action.cc:
            mail["Cc"] = ", ".join(action.cc)
        if action.bcc:
            mail["Bcc"] = ", ".join(action.bcc)
        mail["Subject"], mail["Date"] = action.subject, sent_at
        mail["Message-ID"] = message_id
        mail["X-Qorqut-Request"] = action.request_id
        if in_reply_to:
            mail["In-Reply-To"] = in_reply_to
            mail["References"] = " ".join([*references, in_reply_to])
        mail.set_content(body)
        for attachment in attachments:
            main, sub = attachment["type"].split("/", 1)
            mail.add_attachment(attachment["content"], maintype=main, subtype=sub, filename=attachment["name"])
        content = mail.as_bytes()
        self._verify_account_mime(content, action, body, attachments, message_id=message_id, sent_at=sent_at)
        return content

    @staticmethod
    def _verify_account_mime(content, action, body, attachments, *, message_id, sent_at):
        def invalid(reason):
            raise ProviderError("account_mime_invalid", status=400, detail={"description": reason})
        if re.search(rb"(?<!\r)\n|\r(?!\n)", content):
            invalid("bare CR or LF line ending")
        if any(octet > 127 for octet in content) or b"\0" in content:
            invalid("non-ASCII or NUL octet")
        if max(map(len, content.split(b"\r\n"))) > 998:
            invalid("line longer than 998 octets")
        parsed = BytesParser(policy=SMTP).parsebytes(content)
        senders = parsed["From"].addresses if parsed["From"] is not None else ()
        if (len(senders) != 1 or senders[0].addr_spec.lower() != action.from_address.lower()
                or senders[0].display_name != action.from_name):
            invalid("sender differs from the approved sender")
        for key, approved in (("To", action.to), ("Cc", action.cc), ("Bcc", action.bcc)):
            found = [item.addr_spec.lower() for item in parsed[key].addresses] if parsed[key] is not None else []
            if found != [value.lower() for value in approved]:
                invalid(key.lower() + " recipients differ from the approved recipients")
        if str(parsed["Subject"]) != action.subject:
            invalid("subject differs from the approved subject")
        if str(parsed["Message-ID"]) != message_id or str(parsed["X-Qorqut-Request"]) != action.request_id:
            invalid("request correlation headers differ")
        date = parsed["Date"].datetime if parsed["Date"] is not None else None
        if date is None or date.utcoffset() is None or date != sent_at.replace(microsecond=0):
            invalid("Date is missing, naive or inconsistent")
        canonical = lambda value: (value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        text = parsed.get_body(preferencelist=("plain",))
        if text is None or canonical(text.get_content()) != canonical(body):
            invalid("body differs from the approved body")
        found = list(parsed.iter_attachments())
        if len(found) != len(attachments) or any(
                part.get_filename() != expected["name"] or part.get_content_type() != expected["type"].lower()
                or hashlib.sha256(part.get_payload(decode=True) or b"").hexdigest() != expected["sha256"]
                for part, expected in zip(found, attachments)):
            invalid("attachments differ from the approved attachments")

    def account_create_draft(self, action):
        self._account_identity(action)
        body, composed_attachments = self._account_composed_content(action)
        in_reply_to, references = None, []
        if action.reply_message_id:
            parent = self.account_raw(action.reply_message_id)
            message_ids = parent.get("messageId") or []
            if not message_ids:
                raise ProviderError("account_source_missing_message_id", status=409)
            in_reply_to = internet_message_id(message_ids[0])
            if in_reply_to is None:
                raise ProviderError("account_source_invalid_message_id", status=409)
            references = [value for value in (internet_message_id(item) for item in parent.get("references") or [])
                if value is not None]
        content = self.account_mime(action, body, composed_attachments,
            message_id=self.account_message_id(self.account_id, action), in_reply_to=in_reply_to, references=references)
        upload = self.session()["uploadUrl"].replace("{accountId}", quote(self.account_id, safe=""))
        blob = self.request("POST", upload, content=content, content_type="message/rfc822", write=True)
        if not blob.get("blobId"):
            raise ProviderError("invalid_account_upload", uncertain=True)
        folders = {item["id"]: item for item in self.account_folders()}
        drafts = next((item["id"] for item in folders.values() if item.get("role") == "drafts"), None)
        if drafts is None:
            raise ProviderError("account_drafts_folder_missing", status=503)
        result = self.call("Email/import", {"emails": {"prepared": {"blobId": blob["blobId"],
            "mailboxIds": {drafts: True}, "keywords": {"$draft": True}}},
            **({"ifInState": action.expected_state} if action.action == "update_draft" else {})}, write=True)
        return {"draft": self.created(result), "expected_state": result.get("newState")}

    def account_update_draft(self, action):
        # RFC 8621: content is immutable. Import a replacement before deleting
        # the old draft, using successive account states to detect concurrent edits.
        raw = self.account_raw(action.message_id)
        if not raw.get("keywords", {}).get("$draft"):
            raise ProviderError("account_not_a_draft", status=409)
        replacement = self.account_create_draft(action)
        if not replacement.get("draft", {}).get("id") or not replacement.get("expected_state"):
            raise ProviderError("account_draft_replacement_unconfirmed", uncertain=True)
        try:
            deleted = self.account_delete_message(action.message_id, expected_state=replacement["expected_state"])
        except ProviderError:
            return {**replacement, "replaces": action.message_id, "old_draft_retained": True}
        return {**replacement, "replaces": action.message_id,
            "old_draft_retained": bool(deleted.get("partial"))}

    def account_find_draft(self, request_id):
        result = self.call("Email/query", {"filter": {"header": ["X-Qorqut-Request", request_id]},
            "limit": 2, "calculateTotal": True})
        if result.get("total", 0) > 1:
            raise ProviderError("account_draft_ambiguous", status=409)
        ids = result.get("ids") or []
        if not ids:
            return None
        raw = self.account_raw(ids[0], metadata=True)
        if not raw.get("keywords", {}).get("$draft"):
            raise ProviderError("account_draft_not_draft", status=409)
        return raw

    def account_send(self, draft_id, identity_id=None, *, action=None):
        identity = next((item for item in self.account_identities() if item.get("id") == identity_id), None)
        if identity is None or identity.get("bcc") or identity.get("replyTo"):
            raise ProviderError("account_sender_identity_denied", status=403)
        if action is not None and identity.get("email", "").lower() != action.from_address.lower():
            raise ProviderError("account_sender_identity_denied", status=403)
        draft = self.account_raw(draft_id, metadata=True)
        if not draft.get("keywords", {}).get("$draft"):
            raise ProviderError("account_message_is_not_draft", status=409)
        if action is not None:
            expected_body, expected_attachments = self._account_composed_content(action)
            full = self.account_raw(draft_id)
            sender_item = (full.get("from") or [{}])[0]
            sender = sender_item.get("email", "").lower()
            recipients = {
                key: [item.get("email", "").lower() for item in full.get(key) or []]
                for key in ("to", "cc", "bcc")}
            normalized = self.normalize(full)
            body = normalized.get("body", {}).get("content", "")
            canonical = lambda value: (value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
            if (sender != action.from_address or (sender_item.get("name") or "") != action.from_name
                    or recipients["to"] != [value.lower() for value in action.to]
                    or recipients["cc"] != [value.lower() for value in action.cc]
                    or recipients["bcc"] != [value.lower() for value in action.bcc]
                    or full.get("subject", "") != action.subject or canonical(body) != canonical(expected_body)):
                raise ProviderError("account_draft_content_changed", status=409)
            attachments = full.get("attachments") or []
            if len(attachments) != len(expected_attachments):
                raise ProviderError("account_draft_content_changed", status=409)
            for expected, found in zip(expected_attachments, attachments):
                if (found.get("name") != expected["name"] or found.get("type") != expected["type"]
                        or found.get("size") != len(expected["content"])):
                    raise ProviderError("account_draft_content_changed", status=409)
                if hashlib.sha256(self.download_blob(found["blobId"])).hexdigest() != expected["sha256"]:
                    raise ProviderError("account_draft_content_changed", status=409)
        folders = self.account_folders()
        sent = next((item["id"] for item in folders if item.get("role") == "sent"), None)
        if sent is None:
            raise ProviderError("account_sent_folder_missing", status=503)
        envelope = {"mailFrom": {"email": action.from_address if action is not None else identity["email"]},
            "rcptTo": [{"email": value} for value in ([*(action.to if action is not None else []),
                *(action.cc if action is not None else []), *(action.bcc if action is not None else [])])]}
        updates = {"keywords/$draft": None, **{"mailboxIds/" + folder: None for folder in draft.get("mailboxIds", {})},
            "mailboxIds/" + sent: True}
        result = self.call("EmailSubmission/set", {"create": {"prepared": {"emailId": draft_id,
            "identityId": identity["id"], "envelope": envelope}},
            "onSuccessUpdateEmail": {"#prepared": updates}}, write=True)
        return {"submission": self.created(result), **({"partial": True} if result.get("notCreated") else {})}

    def account_submission(self, draft_id, *, action=None):
        result = self.call("EmailSubmission/query", {"filter": {"emailIds": [draft_id]},
            "limit": 2, "calculateTotal": True})
        if result.get("total", 0) > 1:
            raise ProviderError("account_submission_ambiguous", status=409)
        ids = result.get("ids") or []
        if not ids:
            return None
        found = self.call("EmailSubmission/get", {"ids": ids}).get("list", [])
        if len(found) != 1:
            return None
        submission = found[0]
        if submission.get("emailId") != draft_id or submission.get("undoStatus") in {"canceled", "pending"}:
            return None
        if submission.get("undoStatus") != "final":
            return None
        if action is not None:
            if submission.get("identityId") != action.identity_id:
                raise ProviderError("account_submission_identity_changed", status=409)
            envelope = submission.get("envelope") or {}
            expected = [*action.to, *action.cc, *action.bcc]
            actual = [item.get("email", "").lower() for item in envelope.get("rcptTo", [])]
            if (envelope.get("mailFrom", {}).get("email", "").lower() != action.from_address.lower()
                    or sorted(actual) != sorted(value.lower() for value in expected)):
                raise ProviderError("account_submission_envelope_changed", status=409)
        return submission

    def account_delete_message(self, identity, *, expected_state=None):
        args = {"destroy": [identity]}
        if expected_state:
            args["ifInState"] = expected_state
        result = self.call("Email/set", args, write=True)
        return {"message_id": identity, "provider": result,
            **({"partial": True} if result.get("notDestroyed") else {})}

    @staticmethod
    def raise_error(data, *, uncertain=False):
        kind = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z]{1,60}", kind):
            kind = "unknown"
        raise ProviderError("jmap_" + kind, uncertain=uncertain and kind in {"serverFail", "unknown"},
            status=429 if kind == "rateLimit" else 410 if kind == "cannotCalculateChanges"
                else None if kind in {"serverFail", "unknown"} else 400,
            detail=provider_detail(data, kind))

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
            "keywords": raw.get("keywords", {}), "requestId": raw.get(REQUEST_HEADER), "isDraft": bool(raw.get("keywords", {}).get("$draft")),
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

    def identity(self, mailbox, sender, message=None, *, identities=None):
        if message is not None:
            try:
                self.settings.sender(mailbox, message)
            except ValueError as exc:
                raise ProviderError("sender_outside_business") from exc
        elif not mailbox.accepts_sender(sender):
            raise ProviderError("sender_outside_business")
        if identities is None:
            identities = self.call("Identity/get")["list"]
        matches = [item for item in identities if item.get("email", "").lower() == sender]
        if len(matches) != 1:
            raise ProviderError("fastmail_sender_identity_missing_or_ambiguous")
        # Provider-side extra recipients would bypass the approved envelope.
        if matches[0].get("bcc") or matches[0].get("replyTo"):
            raise ProviderError("fastmail_identity_remove_auto_bcc_and_reply_to")
        return matches[0]["id"]

    def composed_business_message(self, mailbox, message):
        if not message.forward_message_id:
            return message
        from .business import BusinessMailService
        from .models import Attachment
        BusinessMailService(self.settings, self)._scoped_metadata(
            mailbox, message.forward_message_id, [message.forward_message_id])
        body, attachments = self._account_composed_content(message)
        return message.model_copy(update={"body": body, "attachments": [Attachment(
            filename=item["name"], content_type=item["type"], sha256=item["sha256"],
            data_base64=base64.b64encode(item["content"]).decode()) for item in attachments]})

    def create_draft(self, mailbox, message):
        self.identity(mailbox, message.from_address, message)
        self.folders()
        parent = self.get_message(mailbox, message.reply_message_id) if message.reply_message_id else None
        content = self.mime(self.composed_business_message(mailbox, message), parent)
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
        # A sent message can legitimately have both the authorized business
        # Bcc copy and an owner/foreign-folder copy.  Resolve ownership from
        # metadata before downloading any MIME so a foreign message body is
        # never read or used to bind a business receipt.
        owned = [item for item in matches if self.owner(item) == mailbox]
        if len(owned) > 1:
            raise ProviderError("outlook_import_lookup_ambiguous")
        if not owned:
            raise ProviderError("outlook_import_conflicting_owner")
        selected = owned[0]
        if not selected.get("blobId"):
            raise ProviderError("outlook_import_lookup_incomplete")
        existing = self.download_blob(selected["blobId"])
        if mime_content_digest(existing) != mime_content_digest(content):
            raise ProviderError("outlook_import_content_conflict")
        return self.get_message(mailbox, selected["id"])

    def import_observed_mime(self, mailbox, content, *, message_id, received_at=None, is_read=False):
        """Copy complete provider MIME into its proven business folder; never submit mail."""
        if mailbox not in self.settings.mailboxes or not isinstance(content, bytes) or len(content) > MAX_MESSAGE_BYTES:
            raise ProviderError("outlook_import_invalid_content")
        mail = BytesParser(policy=default).parsebytes(content)
        identity = internet_message_id(message_id)
        if identity is None or len(mail.get_all("Message-ID", [])) != 1 or internet_message_id(str(mail["Message-ID"])) != identity:
            raise ProviderError("outlook_import_message_id_changed")
        # Outlook's internal dates use +00:00; JMAP UTCDate requires literal Z.
        # Validate before uploading so a bad date cannot leave a provider write.
        if received_at is not None:
            received_at = jmap_utc_date(received_at)
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
