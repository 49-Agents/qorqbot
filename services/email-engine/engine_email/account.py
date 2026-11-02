"""Explicit owner account mail controls, separate from business mail routing."""

import json
from email.message import EmailMessage
from email.utils import formatdate
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .models import Attachment, address, header, MAX_ATTACHMENT_BYTES, MAX_MESSAGE_BYTES
from .store import Store
from .fastmail import ProviderError


ACCOUNT_MAX_RESULTS = 100
ACCOUNT_MAX_BODY_BYTES = 128 * 1024


class AccountAction(BaseModel):
    """Small allowlisted command set. Provider method names never cross HTTP."""
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,255}$")
    action: Literal["message_update", "message_delete", "folder_create", "folder_update", "folder_delete",
        "create_draft", "update_draft", "send", "vacation_set", "masked_email_set"]
    message_id: str | None = Field(default=None, min_length=1, max_length=255)
    folder_id: str | None = Field(default=None, min_length=1, max_length=255)
    destination_folder_id: str | None = Field(default=None, min_length=1, max_length=255)
    move_to_folder_id: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    seen: bool | None = None
    flagged: bool | None = None
    answered: bool | None = None
    add_keywords: list[str] = Field(default_factory=list, max_length=20)
    remove_keywords: list[str] = Field(default_factory=list, max_length=20)
    trash: bool | None = None
    restore: bool | None = None
    from_address: str | None = None
    from_name: str = Field(default="", max_length=160)
    to: list[str] = Field(default_factory=list, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str | None = Field(default=None, min_length=1, max_length=998)
    body: str | None = Field(default=None, max_length=50_000)
    reply_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    forward_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    identity_id: str | None = Field(default=None, min_length=1, max_length=255)
    expected_state: str | None = Field(default=None, min_length=1, max_length=255)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)
    vacation: dict | None = None
    masked_email: dict | None = None

    _addresses = field_validator("from_address")(lambda value: address(value) if value is not None else None)
    _recipients = field_validator("to", "cc", "bcc")(
        lambda values: list(dict.fromkeys(address(value) for value in values)))
    _headers = field_validator("from_name", "subject", "name")(
        lambda value: header(value) if value is not None else None)

    @field_validator("add_keywords", "remove_keywords")
    @classmethod
    def keywords(cls, values):
        if any(not isinstance(value, str) or not value or len(value) > 100 or any(
                character in value for character in "\r\n/\x00") for value in values):
            raise ValueError("Invalid keyword")
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def valid_shape(self):
        if self.action == "message_update" and (not self.message_id or not any((
                self.add_keywords, self.remove_keywords, self.move_to_folder_id,
                self.trash is not None, self.restore is not None,
                self.seen is not None, self.flagged is not None, self.answered is not None))):
            raise ValueError("message_update requires a message change")
        if self.action == "message_delete" and not self.message_id:
            raise ValueError("message_delete requires message_id")
        if self.action == "folder_create" and not self.name:
            raise ValueError("folder_create requires name")
        if self.action == "folder_update" and (not self.folder_id or not self.name):
            raise ValueError("folder_update requires folder_id and name")
        if self.action == "folder_delete" and not self.folder_id:
            raise ValueError("folder_delete requires folder_id")
        if self.action == "update_draft" and (not self.message_id or not self.expected_state):
            raise ValueError("Draft updates require message_id and expected_state")
        if self.action in {"create_draft", "update_draft"} and (
                not self.from_address or not self.to or not self.subject or self.body is None):
            raise ValueError("Draft action requires sender, recipient, subject and body")
        if self.action == "send" and (not self.from_address or not self.identity_id or not self.to
                or not self.subject or self.body is None):
            raise ValueError("send requires full compose fields and identity_id")
        if self.reply_message_id and self.forward_message_id:
            raise ValueError("An action cannot be both a reply and a forward")
        if self.action == "vacation_set" and self.vacation is None:
            raise ValueError("vacation_set requires vacation")
        if self.action == "masked_email_set" and self.masked_email is None:
            raise ValueError("masked_email_set requires masked_email")
        if self.action in {"message_update", "message_delete", "folder_update", "folder_delete"} and not self.expected_state:
            raise ValueError("Existing resource changes require expected_state")
        if set(self.add_keywords) & set(self.remove_keywords):
            raise ValueError("A keyword cannot be both added and removed")
        if self.trash is True and self.restore is True:
            raise ValueError("Cannot trash and restore in one update")
        if sum(len(item.encode()) for item in (self.body or "",)) > ACCOUNT_MAX_BODY_BYTES:
            raise ValueError("Message body is too large")
        if len(self.to) + len(self.cc) + len(self.bcc) > 50:
            raise ValueError("At most 50 recipients are allowed")
        if sum(len(item.content()) for item in self.attachments) > MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachments exceed the combined size limit")
        return self


class AccountMailService:
    """Owner-authorized account surface backed by Fastmail's typed JMAP methods."""

    def __init__(self, settings, store, provider):
        self.settings, self.store, self.provider = settings, store, provider

    def _json(self, row):
        return Store.account_receipt(row)

    @staticmethod
    def _frozen_action(row):
        """Recover the exact, validated action recorded before provider effects."""
        try:
            return AccountAction.model_validate_json(row["payload"])
        except (TypeError, ValueError, ValidationError):
            return None

    def receipt(self, request_id):
        row = self.store.account_operation(request_id)
        if row is None:
            return None
        prior = json.loads(row["result"])
        # A receipt may outlive a provider/account reconfiguration. Only a
        # pending send needs provider access; terminal receipts remain readable
        # during an outage and are returned from the ledger as-is.
        if (row["action"] == "send" and row["state"] in {"prepared", "uncertain"}
                and prior.get("draft_id")):
            frozen = self._frozen_action(row)
            if frozen is not None and frozen.account_id == self.provider.account_id:
                query = getattr(self.provider, "account_submission", None)
                if query is not None:
                    try:
                        submission = query(prior["draft_id"], action=frozen)
                    except ProviderError:
                        # A lookup failure or a mismatched envelope is not
                        # proof of delivery. Keep the durable receipt unchanged
                        # and never submit again from a read/recovery path.
                        submission = None
                    if submission:
                        row = self.store.save_account_operation_if_state(request_id, {"prepared", "uncertain"}, "completed",
                            result={**prior, "submission": submission})
        return self._json(row)

    def metadata(self):
        session = self.provider.session()
        catalog = getattr(self.provider, "account_folder_catalog", None)
        catalog = catalog() if catalog else {"state": None, "folders": self.provider.account_folders()}
        folders = catalog["folders"]
        identities = self.provider.account_identities()
        return {"account_id": session["accountId"],
            "capabilities": sorted(session.get("capabilities", {})),
            "account_capabilities": sorted(session.get("accounts", {}).get(session["accountId"], {}).get("accountCapabilities", {})),
            "folders": folders, "folder_state": catalog.get("state"), "identities": identities,
            "actions": ["message_update", "message_delete", "folder_create", "folder_update", "folder_delete",
            "create_draft", "update_draft", "send"] + (["vacation_set"] if getattr(
                self.provider, "vacation_available", lambda: False)() else []) + (["masked_email_set"] if getattr(
                self.provider, "masked_email_available", lambda: False)() else [])}

    def folders(self):
        catalog = getattr(self.provider, "account_folder_catalog", None)
        return catalog() if catalog else {"state": None, "folders": self.provider.account_folders()}

    def search(self, params):
        allowed = {"q", "from", "to", "subject", "in_mailbox", "has_keyword", "before", "after", "limit", "position"}
        if set(params) - allowed:
            raise ValueError("Unsupported search parameter")
        limit = int(params.get("limit", 50))
        position = int(params.get("position", 0))
        if not 1 <= limit <= ACCOUNT_MAX_RESULTS or position < 0:
            raise ValueError("Invalid page")
        query = {key: value for key, value in params.items()
            if key not in {"limit", "position"} and value not in {None, ""}}
        return self.provider.account_search(query, limit=limit, position=position)

    def message(self, identity, *, body=True):
        return self.provider.account_message(identity, body=body)

    def attachment(self, identity, attachment_id):
        return self.provider.account_attachment(identity, attachment_id)

    def apply(self, action: AccountAction, *, request_digest=None):
        if action.account_id != self.provider.account_id:
            raise ProviderError("account_identity_mismatch", status=409)
        payload = action.model_dump_json(exclude_none=True)
        digest = request_digest or __import__("hashlib").sha256(payload.encode()).hexdigest()
        if not isinstance(digest, str) or len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ProviderError("invalid_account_request_digest", status=400)
        try:
            row, created = self.store.begin_account_operation(action.request_id, digest, action.action, payload)
        except ValueError as exc:
            raise ProviderError("account_request_conflict", status=409) from exc
        if not created:
            if row["state"] != "prepared":
                return self._json(row)
            # Never submit again for an existing request. A crash after Fastmail
            # accepted a submission is indistinguishable from a lost response.
            # Reconcile through the same GET-only path so both paths enforce
            # the frozen identity and recipient/envelope contract.
            return self.receipt(action.request_id)
        try:
            result = self._dispatch(action)
        except ProviderError as exc:
            state = "uncertain" if exc.uncertain else "failed"
            prior = json.loads(self.store.account_operation(action.request_id)["result"])
            # The bounded provider diagnostic belongs to this private ledger row only; it is never put in a log line.
            return self._json(self.store.save_account_operation(action.request_id, state,
                result={**prior, "error": exc.code, **({"provider_status": exc.status} if exc.status else {}),
                    **({"provider_error": exc.detail} if exc.detail else {})}))
        except (ValueError, KeyError) as exc:
            return self._json(self.store.save_account_operation(action.request_id, "failed",
                result={"error": str(exc)[:120] or "invalid_account_action"}))
        state = "failed" if isinstance(result, dict) and result.get("partial") else "completed"
        return self._json(self.store.save_account_operation(action.request_id, state, result=result))

    def _dispatch(self, action):
        if action.action == "message_update":
            return self.provider.account_update_message(action.message_id, action, expected_state=action.expected_state)
        if action.action == "folder_create":
            return self.provider.account_create_folder(action.name, **({"parent_id": action.parent_id} if action.parent_id else {}))
        if action.action == "folder_update":
            return self.provider.account_rename_folder(action.folder_id, action.name,
                expected_state=action.expected_state,
                protected={box.folder_id for box in self.settings.mailboxes} |
                    ({*self.settings.history.private_folder_ids} if self.settings.history else set()))
        if action.action == "folder_delete":
            return self.provider.account_delete_folder(action.folder_id,
                expected_state=action.expected_state,
                protected={box.folder_id for box in self.settings.mailboxes} |
                    ({*self.settings.history.private_folder_ids} if self.settings.history else set()))
        if action.action == "message_delete":
            return self.provider.account_delete_message(action.message_id, expected_state=action.expected_state)
        if action.action == "update_draft":
            return self.provider.account_update_draft(action)
        if action.action == "create_draft":
            return self.provider.account_create_draft(action)
        if action.action == "send":
            existing = self.provider.account_find_draft(action.request_id)
            if existing:
                raise ProviderError("account_draft_request_collision", status=409)
            draft = self.provider.account_create_draft(action)
            draft_id = draft["id"] if "id" in draft else draft.get("draft", {}).get("id")
            if not draft_id:
                raise ProviderError("account_draft_id_missing", uncertain=True)
            self.store.save_account_operation(action.request_id, "prepared", result={"draft_id": draft_id})
            return {"draft_id": draft_id, "submission": self.provider.account_send(draft_id, action.identity_id,
                action=action)}
        if action.action == "vacation_set":
            return self.provider.account_set_vacation(action.vacation)
        if action.action == "masked_email_set":
            return self.provider.account_set_masked_email(action.masked_email)
        raise ValueError("Unsupported account action")
