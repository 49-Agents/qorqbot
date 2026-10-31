"""Fixed wire contracts. No arbitrary headers, paths, or executable settings."""

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.headerregistry import Address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_HTTP_BYTES = 30 * 1024 * 1024


def address(value):
    if not isinstance(value, str) or not value.isascii() or any(c in value for c in "\r\n\x00"):
        raise ValueError("An ASCII email address is required")
    value = value.strip()
    parsed = Address(addr_spec=value)
    if not parsed.username or not parsed.domain or parsed.addr_spec != value or len(value) > 254:
        raise ValueError("Use a complete address without a display name")
    return value.lower()


def header(value):
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("Header values cannot contain line breaks")
    return value


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=150)
    data_base64: str = Field(max_length=28 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("filename")
    @classmethod
    def safe_name(cls, value):
        header(value)
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("Attachment name must not be a path")
        return value

    @field_validator("content_type")
    @classmethod
    def media_type(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", value):
            raise ValueError("Invalid attachment content type")
        return value.lower()

    def content(self):
        return base64.b64decode(self.data_base64, validate=True)

    @model_validator(mode="after")
    def valid_bytes(self):
        data = self.content()
        if len(data) > MAX_ATTACHMENT_BYTES or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("Attachment size or digest does not match")
        return self


class StopKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contact_key: str = Field(min_length=1, max_length=320)
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")


class Outgoing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    mailbox: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    from_address: str
    from_name: str = Field(default="", max_length=160)
    sender_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    transport: Literal["fastmail", "outlook_web"] | None = Field(default=None, exclude_if=lambda value: value is None)
    reply_to: str | None = None
    expires_at: str | None = None
    # Optional; omitted means conversational. Excluded from digest() like sender_ref/transport.
    category: Literal["conversational", "cold_outreach"] | None = Field(default=None, exclude_if=lambda value: value is None)
    # Legacy /sends cold-outreach label; see docs/INTEGRATION-CONTRACT.md.
    workflow_owner: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,60}$", exclude_if=lambda value: value is None)
    stop_key: StopKey | None = Field(default=None, exclude_if=lambda value: value is None)
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=50_000)
    reply_message_id: str | None = Field(default=None, min_length=1, max_length=1000)
    forward_message_id: str | None = Field(default=None, min_length=1, max_length=1000, exclude_if=lambda value: value is None)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)

    _sender = field_validator("from_address")(address)
    _headers = field_validator("from_name", "subject")(header)

    @field_validator("reply_to")
    @classmethod
    def reply_address(cls, value):
        return address(value) if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def expiry(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Expiry requires a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    @field_validator("to", "cc", "bcc")
    @classmethod
    def recipients(cls, values):
        return list(dict.fromkeys(address(value) for value in values))

    @model_validator(mode="after")
    def bounded(self):
        if self.reply_message_id and self.forward_message_id:
            raise ValueError("An email cannot be both a reply and a forward")
        if sum(len(a.content()) for a in self.attachments) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the combined size limit")
        if len(self.envelope_recipients()) > 50:
            raise ValueError("At most 50 total recipients are allowed")
        if self.effective_category() == "cold_outreach":
            if self.stop_key is None or self.workflow_owner is None:
                raise ValueError("cold_outreach requires workflow_owner and stop_key")
            if self.stop_key.business_id != self.business_id:
                raise ValueError("stop_key.business_id must match business_id")
        elif self.stop_key is not None or self.workflow_owner is not None:
            raise ValueError("workflow_owner and stop_key require category cold_outreach")
        return self

    def envelope_recipients(self):
        return list(dict.fromkeys([*self.to, *self.cc, *self.bcc]))

    def effective_category(self):
        return self.category or "conversational"

    def cold_outreach_label(self):
        """(workflow_owner, contact_key, business_id) when labelled for the stop guard, else None."""
        if self.effective_category() != "cold_outreach" or self.stop_key is None or self.workflow_owner is None:
            return None
        return (self.workflow_owner, self.stop_key.contact_key, self.stop_key.business_id)

    def digest(self):
        # Preserve pre-extension request digests, including recovery of saved drafts.
        data = self.model_dump()
        for field in ("sender_ref", "transport", "reply_to", "expires_at", "category",
                "workflow_owner", "stop_key"):
            if data.get(field) is None:
                data.pop(field, None)
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


class Mailbox(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    folder_id: str = Field(min_length=1, max_length=255)
    address: str
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    aliases: list[str] = Field(default_factory=list, max_length=100)

    _address = field_validator("address")(address)

    @field_validator("aliases")
    @classmethod
    def alias_addresses(cls, values):
        return list(dict.fromkeys(address(value) for value in values))

    def accepts_sender(self, value):
        return value in [self.address, *self.aliases]


class SharedSender(BaseModel):
    """Owner-configured grant, never an agent-selectable provider credential."""
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    address: str
    from_name: str = Field(default="", max_length=160)
    account_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    transport: Literal["fastmail", "outlook_web"] = "fastmail"
    business_scope: Literal["selected", "all"] = "selected"
    allowed_business_ids: list[str] = Field(default_factory=list, max_length=100)
    default_for_outreach: bool = False
    writing_instructions: str = Field(default="", max_length=10_000)
    required_footer: str = Field(default="", max_length=2000)
    enabled: bool = False
    rolling_limit: int = Field(default=250, ge=1, le=10000)

    _address = field_validator("address")(address)
    _name = field_validator("from_name")(header)
    _footer = field_validator("required_footer")(header)

    @model_validator(mode="after")
    def valid_scope(self):
        if self.business_scope == "selected" and not self.allowed_business_ids:
            raise ValueError("Selected shared senders require allowed business IDs")
        if self.default_for_outreach and self.business_scope != "all":
            raise ValueError("The outreach default must have all-business scope")
        return self

    def permits_business(self, business_id):
        """Scope grant only; sending also requires enablement and a registered mailbox."""
        return self.business_scope == "all" or business_id in self.allowed_business_ids


class IntegrationClient(BaseModel):
    """Scoped trusted-service caller (e.g. Outreach), never an agent credential."""
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    secret: str = Field(min_length=32, max_length=200)
    workflow_owner: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    allowed_mailboxes: list[str] = Field(min_length=1, max_length=100)
    business_ids: list[str] = Field(default_factory=list, max_length=200)
    handoff_destinations: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("secret")
    @classmethod
    def ascii_secret(cls, value):
        if not value.isascii():
            raise ValueError("Integration secret must be ASCII")
        return value

    @field_validator("allowed_mailboxes")
    @classmethod
    def mailbox_refs(cls, values):
        if not values or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) for value in values):
            raise ValueError("Invalid mailbox reference")
        return list(dict.fromkeys(values))

    @field_validator("business_ids")
    @classmethod
    def business_id_values(cls, values):
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value) for value in values):
            raise ValueError("Invalid business ID")
        return list(dict.fromkeys(values))

    @field_validator("handoff_destinations")
    @classmethod
    def handoff_destination_values(cls, values):
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value) for value in values):
            raise ValueError("Invalid handoff destination owner")
        return list(dict.fromkeys(values))


class IntentCorrelation(BaseModel):
    """Workflow context kept separate from the wrapped ``Outgoing`` payload."""
    model_config = ConfigDict(extra="forbid", strict=True)
    workflow_owner: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,60}$")
    account_id: str | None = Field(default=None, max_length=200)
    business_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,120}$")
    campaign_id: str | None = Field(default=None, max_length=200)
    enrollment_id: str | None = Field(default=None, max_length=200)
    step_index: int | None = Field(default=None, ge=0, le=1_000_000)
    manifest_digest: str | None = Field(default=None, max_length=200)
    stop_key: StopKey


class IntentSubmission(BaseModel):
    """``POST /integrations/intents`` envelope; see ``EmailService.submit_intent``."""
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    message: dict
    correlation: IntentCorrelation
    category: Literal["cold_outreach"]
    expires_at: str

    @field_validator("expires_at")
    @classmethod
    def expiry(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Envelope expiry requires a timezone")
        return parsed.astimezone(timezone.utc).isoformat()


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    workflow_owner: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,60}$")


class HistoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    database: str = Field(min_length=1)
    public_key_file: str = Field(min_length=1)
    start_cursor: str = Field(min_length=1, max_length=2000)
    capture_scope: Literal["scoped", "account"] = "scoped"
    private_folder_ids: list[str] = Field(default_factory=list, max_length=100)


class OutlookWebConfig(BaseModel):
    """Owner-selected local browser; never exposed in business metadata."""
    model_config = ConfigDict(extra="forbid", strict=True)
    cdp_url: str = Field(min_length=1, max_length=2000)
    address: str
    account_ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    start_at: str
    timeout_seconds: int = Field(default=60, ge=1, le=120)

    _address = field_validator("address")(address)

    @field_validator("cdp_url")
    @classmethod
    def local_browser(cls, value):
        import ipaddress
        parsed = urlsplit(value)
        local = parsed.hostname == "localhost"
        if not local:
            try:
                local = ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                pass
        if (parsed.scheme not in {"http", "ws"} or not local or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Outlook browser endpoint must be local HTTP or WebSocket")
        # Evaluate the port now so malformed values cannot reach the bridge.
        parsed.port
        return value

    @field_validator("start_at")
    @classmethod
    def explicit_cutover(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Outlook collection cutover requires a timezone")
        return parsed.astimezone(timezone.utc).isoformat()


class HistoryEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]{1,200}$")
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    content: dict


@dataclass(frozen=True)
class Settings:
    database: Path
    read_token: str
    send_token: str
    api_token: str
    mailboxes: tuple[Mailbox, ...]
    account_id: str = ""
    sync_seconds: int = 120
    timeout: int = 30
    shared_senders: tuple[SharedSender, ...] = ()
    history: HistoryConfig | None = None
    outlook_web: OutlookWebConfig | None = None
    admin_token: str = ""
    integrations: tuple[IntegrationClient, ...] = ()

    def __post_init__(self):
        if self.admin_token and (len(self.admin_token) < 32 or not self.admin_token.isascii()
                or self.admin_token in {self.read_token, self.send_token}):
            raise ValueError("Use a distinct private mailbox administration token of at least 32 characters")
        refs, folders, addresses = set(), set(), set()
        for box in self.mailboxes:
            owned = {box.address, *box.aliases}
            if box.ref in refs or box.folder_id in folders or addresses & owned:
                raise ValueError("Mailbox references, folders, and addresses must be unique")
            refs.add(box.ref)
            folders.add(box.folder_id)
            addresses.update(owned)
        refs, addresses, limits = set(), set(), {}
        businesses = {box.business_id for box in self.mailboxes}
        for sender in self.shared_senders:
            if sender.ref in refs or sender.address in addresses:
                raise ValueError("Shared sender refs and addresses must be unique")
            if sender.business_scope == "selected" and not set(sender.allowed_business_ids) <= businesses:
                raise ValueError("Shared sender business must have a registered mailbox")
            if sender.account_ref in limits and limits[sender.account_ref] != sender.rolling_limit:
                raise ValueError("Shared account aliases must use the same rolling limit")
            refs.add(sender.ref)
            addresses.add(sender.address)
            limits[sender.account_ref] = sender.rolling_limit
            if sender.enabled and sender.transport == "outlook_web" and (
                    self.outlook_web is None or self.outlook_web.address != sender.address
                    or self.outlook_web.account_ref != sender.account_ref):
                raise ValueError("Enabled Outlook sender requires matching browser configuration")
        if sum(sender.default_for_outreach for sender in self.shared_senders) > 1:
            raise ValueError("At most one shared sender can be the outreach default")
        if any(sender.enabled for sender in self.shared_senders) and self.history is None:
            raise ValueError("Enabled shared senders require private email history")
        if self.outlook_web and self.history is None:
            raise ValueError("Outlook browser collection requires private email history")
        if self.history and set(self.history.private_folder_ids) & {box.folder_id for box in self.mailboxes}:
            raise ValueError("Private history folders cannot be business mailboxes")
        mailbox_refs = {box.ref for box in self.mailboxes}
        secrets_by_token = {self.read_token, self.send_token, *([self.admin_token] if self.admin_token else [])}
        client_ids, secrets = set(), set()
        for client in self.integrations:
            if client.client_id in client_ids:
                raise ValueError("Integration client IDs must be unique")
            if client.secret in secrets_by_token or client.secret in secrets:
                raise ValueError("Integration client secrets must be distinct from every other credential")
            if not set(client.allowed_mailboxes) <= mailbox_refs:
                raise ValueError("Integration client mailboxes must be registered")
            allowed_businesses = {box.business_id for box in self.mailboxes if box.ref in client.allowed_mailboxes}
            if client.business_ids and not set(client.business_ids) <= allowed_businesses:
                raise ValueError("Integration client business IDs must match its allowed mailboxes")
            client_ids.add(client.client_id)
            secrets.add(client.secret)

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.environ.get(name, "")
            if not value:
                raise ValueError(f"Missing {name}")
            return value
        read, send = required("EMAIL_READ_TOKEN"), required("EMAIL_SEND_TOKEN")
        if len(read) < 32 or len(send) < 32 or read == send or not read.isascii() or not send.isascii():
            raise ValueError("Use distinct read and send tokens of at least 32 characters")
        raw = json.loads(Path(required("EMAIL_MAILBOXES_FILE")).read_text())
        if not isinstance(raw, list) or not raw:
            raise ValueError("EMAIL_MAILBOXES_FILE must contain a nonempty array")
        mailboxes = tuple(Mailbox.model_validate(item) for item in raw)
        refs, folders, addresses = set(), set(), set()
        for mailbox in mailboxes:
            owned = {mailbox.address, *mailbox.aliases}
            if mailbox.ref in refs or mailbox.folder_id in folders or addresses & owned:
                raise ValueError("Mailbox references, folders, and addresses must be unique")
            refs.add(mailbox.ref)
            folders.add(mailbox.folder_id)
            addresses.update(owned)
        return cls(database=Path(required("EMAIL_DATABASE")).expanduser().resolve(),
            read_token=read, send_token=send, api_token=required("FASTMAIL_API_TOKEN"),
            mailboxes=mailboxes, admin_token=os.environ.get("EMAIL_ADMIN_TOKEN", ""), account_id=os.environ.get("FASTMAIL_ACCOUNT_ID", ""),
            sync_seconds=max(30, int(os.environ.get("EMAIL_SYNC_SECONDS", "120"))),
            shared_senders=tuple(SharedSender.model_validate(item) for item in
                json.loads(Path(os.environ["EMAIL_SHARED_SENDERS_FILE"]).read_text()))
                if os.environ.get("EMAIL_SHARED_SENDERS_FILE") else (),
            history=HistoryConfig.model_validate_json(Path(os.environ["EMAIL_HISTORY_FILE"]).read_text())
                if os.environ.get("EMAIL_HISTORY_FILE") else None,
            outlook_web=OutlookWebConfig.model_validate_json(Path(os.environ["EMAIL_OUTLOOK_WEB_FILE"]).read_text())
                if os.environ.get("EMAIL_OUTLOOK_WEB_FILE") else None,
            integrations=tuple(IntegrationClient.model_validate(item) for item in
                json.loads(Path(os.environ["EMAIL_INTEGRATIONS_FILE"]).read_text()))
                if os.environ.get("EMAIL_INTEGRATIONS_FILE") else ())

    def mailbox(self, ref):
        found = next((box for box in self.mailboxes if box.ref == ref), None)
        if found is None:
            raise ValueError("Mailbox is not registered")
        return found

    def integration_client(self, secret):
        """Constant-time match against every configured client; never the read/send/admin pool."""
        import hmac
        supplied = secret.encode()
        matched = None
        for client in self.integrations:
            if hmac.compare_digest(supplied, client.secret.encode()):
                matched = client
        return matched

    def sender(self, mailbox, message):
        if mailbox.business_id != message.business_id:
            raise ValueError("Business is outside the registered mailbox")
        if message.reply_to is not None and message.reply_to != mailbox.address:
            raise ValueError("Reply-To must be the registered business address")
        configured = next((s for s in self.shared_senders if s.address == message.from_address), None)
        if message.sender_ref is not None:
            if configured is None or configured.ref != message.sender_ref:
                raise ValueError("Shared sender does not match From")
        elif not mailbox.accepts_sender(message.from_address):
            raise ValueError("Sender is outside the registered mailbox")
        if configured:
            if not configured.enabled or not configured.permits_business(mailbox.business_id):
                raise ValueError("Shared sender is unavailable for this business")
            if not message.expires_at:
                raise ValueError("Shared sending requires an approval execution deadline")
            if (message.transport or "fastmail") != configured.transport:
                raise ValueError("Approved transport no longer matches the shared sender")
            if configured.required_footer:
                lines = [line for line in message.body.splitlines() if line.strip()]
                if not lines or lines[-1] != configured.required_footer:
                    raise ValueError("Approved message must end with the exact required footer")
        elif (message.transport or "fastmail") != "fastmail":
            raise ValueError("Business mailbox sending requires Fastmail")
        return configured
