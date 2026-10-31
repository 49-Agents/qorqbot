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


class Outgoing(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    mailbox: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    from_address: str
    from_name: str = Field(default="", max_length=160)
    sender_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    reply_to: str | None = None
    expires_at: str | None = None
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=50_000)
    reply_message_id: str | None = Field(default=None, min_length=1, max_length=1000)
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
        if sum(len(a.content()) for a in self.attachments) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the combined size limit")
        if len(self.envelope_recipients()) > 50:
            raise ValueError("At most 50 total recipients are allowed")
        return self

    def envelope_recipients(self):
        return list(dict.fromkeys([*self.to, *self.cc, *self.bcc]))

    def digest(self):
        # Preserve pre-extension request digests, including recovery of saved drafts.
        data = self.model_dump()
        for field in ("sender_ref", "reply_to", "expires_at"):
            if data[field] is None:
                del data[field]
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
    allowed_business_ids: list[str] = Field(min_length=1, max_length=100)
    enabled: bool = False
    rolling_limit: int = Field(default=30, ge=1, le=10000)

    _address = field_validator("address")(address)
    _name = field_validator("from_name")(header)


class HistoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    database: str = Field(min_length=1)
    public_key_file: str = Field(min_length=1)
    start_cursor: str = Field(min_length=1, max_length=2000)
    private_folder_ids: list[str] = Field(default_factory=list, max_length=100)


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

    def __post_init__(self):
        refs, addresses, limits = set(), set(), {}
        businesses = {box.business_id for box in self.mailboxes}
        for sender in self.shared_senders:
            if sender.ref in refs or sender.address in addresses:
                raise ValueError("Shared sender refs and addresses must be unique")
            if not set(sender.allowed_business_ids) <= businesses:
                raise ValueError("Shared sender business must have a registered mailbox")
            if sender.account_ref in limits and limits[sender.account_ref] != sender.rolling_limit:
                raise ValueError("Shared account aliases must use the same rolling limit")
            refs.add(sender.ref)
            addresses.add(sender.address)
            limits[sender.account_ref] = sender.rolling_limit
        if any(sender.enabled for sender in self.shared_senders) and self.history is None:
            raise ValueError("Enabled shared senders require private email history")
        if self.history and set(self.history.private_folder_ids) & {box.folder_id for box in self.mailboxes}:
            raise ValueError("Private history folders cannot be business mailboxes")

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
            mailboxes=mailboxes, account_id=os.environ.get("FASTMAIL_ACCOUNT_ID", ""),
            sync_seconds=max(30, int(os.environ.get("EMAIL_SYNC_SECONDS", "120"))),
            shared_senders=tuple(SharedSender.model_validate(item) for item in
                json.loads(Path(os.environ["EMAIL_SHARED_SENDERS_FILE"]).read_text()))
                if os.environ.get("EMAIL_SHARED_SENDERS_FILE") else (),
            history=HistoryConfig.model_validate_json(Path(os.environ["EMAIL_HISTORY_FILE"]).read_text())
                if os.environ.get("EMAIL_HISTORY_FILE") else None)

    def mailbox(self, ref):
        found = next((box for box in self.mailboxes if box.ref == ref), None)
        if found is None:
            raise ValueError("Mailbox is not registered")
        return found

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
            if not configured.enabled or mailbox.business_id not in configured.allowed_business_ids:
                raise ValueError("Shared sender is unavailable for this business")
            if not message.expires_at:
                raise ValueError("Shared sending requires an approval execution deadline")
        return configured
