"""Fixed wire contracts. No arbitrary headers, paths, or executable settings."""

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from email.headerregistry import Address
from pathlib import Path
from uuid import UUID

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
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=50_000)
    reply_message_id: str | None = Field(default=None, min_length=1, max_length=1000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)

    _sender = field_validator("from_address")(address)
    _headers = field_validator("from_name", "subject")(header)

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
        raw = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


class Mailbox(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    object_id: str
    address: str
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    aliases: list[str] = Field(default_factory=list, max_length=100)

    _address = field_validator("address")(address)

    @field_validator("object_id")
    @classmethod
    def object_uuid(cls, value):
        return str(UUID(value))

    @field_validator("aliases")
    @classmethod
    def alias_addresses(cls, values):
        return list(dict.fromkeys(address(value) for value in values))

    def accepts_sender(self, value):
        return value in [self.address, *self.aliases]


@dataclass(frozen=True)
class Settings:
    database: Path
    read_token: str
    send_token: str
    tenant_id: str
    client_id: str
    client_secret: str
    mailboxes: tuple[Mailbox, ...]
    sync_seconds: int = 120
    timeout: int = 30

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
        refs, objects, addresses = set(), set(), set()
        for mailbox in mailboxes:
            owned = {mailbox.address, *mailbox.aliases}
            if mailbox.ref in refs or mailbox.object_id in objects or addresses & owned:
                raise ValueError("Mailbox references, objects, and addresses must be unique")
            refs.add(mailbox.ref)
            objects.add(mailbox.object_id)
            addresses.update(owned)
        return cls(database=Path(required("EMAIL_DATABASE")).expanduser().resolve(),
            read_token=read, send_token=send, tenant_id=str(UUID(required("EMAIL_TENANT_ID"))),
            client_id=str(UUID(required("EMAIL_CLIENT_ID"))), client_secret=required("EMAIL_CLIENT_SECRET"),
            mailboxes=mailboxes,
            sync_seconds=max(30, int(os.environ.get("EMAIL_SYNC_SECONDS", "120"))))

    def mailbox(self, ref):
        found = next((box for box in self.mailboxes if box.ref == ref), None)
        if found is None:
            raise ValueError("Mailbox is not registered")
        return found
