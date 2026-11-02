"""The ``fastmail_*`` account operations: policy, validation and the durable job boundary around the engine's account API.

Account mail has no Project. Every operation is Global, requires the ``manage_fastmail`` capability, and names the Fastmail
account it means; sending and drafting name a real provider identity (``identity_id`` and the matching ``from_address``), which
is checked against the account's own identity list before a job exists and again by the engine before anything is sent. An
alias is simply an identity of the account: what an alias is for lives in the owner's Email Management instructions.

Mutations are not staged for exact-change Signoff (``supports_signoff=False``): the owner's instruction package decides when to
ask the owner first (the ``owner_input`` model), while the agent's actual tool grants, the Global capability and the account
authority below still decide who may call them. A mutation queues one named background job; the job id is the request id of
the engine's idempotent ledger, so the outcome is read with ``fastmail_receipt_get`` (``jobs_get`` needs a routine grant)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import TEXT, PlanBuilder, apply_records, prepare_job, schema
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError, FieldChange, PreviewResource, StructuredPreview, plain

CAPABILITY = "manage_fastmail"
PROGRAM = "fastmail.account_mutation"
MAX_INLINE_ATTACHMENT_BASE64 = 1 * 1024 * 1024          # below the 2 MiB MCP request ceiling; no separate upload protocol
MAX_RESULT_BYTES = 12 * 1024 * 1024
MAX_ATTACHMENT_CHUNK = 5 * 1024 * 1024

_ACCOUNT = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"^[A-Za-z0-9_-]+$",
            "description": "The Fastmail account id reported by fastmail_capabilities_get."}
_REF = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"^[A-Za-z0-9_.:@/-]+$"}
_KEYWORDS = {"type": "array", "maxItems": 20, "uniqueItems": True,
             "items": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": r"^\$?[A-Za-z0-9_-]+$"}}
_ADDRESS = {"type": "string", "minLength": 3, "maxLength": 254, "pattern": r"^[A-Za-z0-9.!#$%&'*+/=?^_{}|~-]+@[A-Za-z0-9.-]+$"}
_EXPECTED = {"expected_state": {"type": "string", "minLength": 1, "maxLength": 200}}
_ATTACHMENT = schema({
    "filename": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"^[^/\\\x00\r\n]+$"},
    "content_base64": {"type": "string", "maxLength": MAX_INLINE_ATTACHMENT_BASE64},
    "content_type": {"type": "string", "minLength": 3, "maxLength": 150,
                     "pattern": r"^[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+$"},
    "sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"}}, ("filename", "content_base64", "content_type", "sha256"))
_COMPOSE_FIELDS = {
    "account_id": _ACCOUNT,
    "identity_id": {**_REF, "description": "An identity id from fastmail_identities_list; its email must equal from_address."},
    "from_address": _ADDRESS,
    "to": {"type": "array", "minItems": 1, "maxItems": 50, "items": _ADDRESS},
    "cc": {"type": "array", "maxItems": 50, "items": _ADDRESS},
    "bcc": {"type": "array", "maxItems": 50, "items": _ADDRESS},
    "subject": {"type": "string", "minLength": 1, "maxLength": 998},
    "body": {"type": "string", "minLength": 1, "maxLength": 50_000},
    "attachments": {"type": "array", "maxItems": 10, "items": _ATTACHMENT,
                    "description": "Inline attachment data is limited to 1 MiB base64 across the whole request."}}
_COMPOSE_REQUIRED = ("account_id", "identity_id", "from_address", "to", "subject", "body")
_VACATION = schema({"id": _REF, "isEnabled": {"type": "boolean"}, "fromDate": TEXT, "toDate": TEXT,
                    "subject": {"type": "string", "maxLength": 998}, "textBody": {"type": "string", "maxLength": 50_000}})
_MASKED_EMAIL = schema({"id": _REF, "state": {"type": "string", "enum": ["enabled", "disabled"]},
                        "description": {"type": "string", "maxLength": 255},
                        "forDomain": {"type": "string", "maxLength": 255, "pattern": r"^[A-Za-z0-9.-]+$"},
                        "url": {"type": "string", "maxLength": 2000},
                        "emailPrefix": {"type": "string", "maxLength": 64, "pattern": r"^[A-Za-z0-9._+-]+$"}})

READS = {
    "fastmail_capabilities_get": (schema(), "The configured Fastmail account: its id, supported actions, folders and identities."),
    "fastmail_identities_list": (schema(), "The account's sending identities (id, name, email). An alias is an identity here."),
    "fastmail_folders_list": (schema(), "The account's folders with their ids and state."),
    "fastmail_messages_search": (schema({
        "q": TEXT, "from": _ADDRESS, "to": _ADDRESS, "subject": TEXT, "in_mailbox": _REF,
        "has_keyword": {"type": "string", "minLength": 1, "maxLength": 64}, "before": TEXT, "after": TEXT,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "position": {"type": "integer", "minimum": 0}}),
        "Search messages in the account; at most 100 per page."),
    "fastmail_message_get": (schema({"message_id": _REF}, ("message_id",)), "Read one message with its expected_state."),
    "fastmail_thread_get": (schema({"thread_id": _REF, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                    "position": {"type": "integer", "minimum": 0}}, ("thread_id",)),
                            "Read one thread page; at most 100 messages per page."),
    "fastmail_attachment_get": (schema({"attachment_id": _REF, "message_id": _REF, "offset": {"type": "integer", "minimum": 0},
                                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ATTACHMENT_CHUNK}},
                                       ("attachment_id", "message_id")),
                                "Read one attachment range (at most 5 MiB per call); follow next_offset until it equals total."),
    "fastmail_receipt_get": (schema({"request_id": _REF}, ("request_id",)),
                             "Look up a mutation by its request id (the job id returned when it was queued): the engine's receipt, "
                             "if it holds one, and the job's own state, which is also how a mutation that has not reached the "
                             "engine yet (queued, retrying or unconfirmed) is observed."),
}
MUTATIONS = {
    "fastmail_message_update": (schema({
        "account_id": _ACCOUNT, "message_id": _REF, "add_keywords": _KEYWORDS, "remove_keywords": _KEYWORDS,
        "move_to_folder_id": _REF, "trash": {"type": "boolean"}, "restore": {"type": "boolean"}, "seen": {"type": "boolean"},
        "flagged": {"type": "boolean"}, "answered": {"type": "boolean"}, **_EXPECTED},
        ("account_id", "message_id", "expected_state")), "message_update",
        "Mark, move, trash or restore one message (ordinary organizing)."),
    "fastmail_message_delete": (schema({"account_id": _ACCOUNT, "message_id": _REF, **_EXPECTED},
                                       ("account_id", "message_id", "expected_state")), "message_delete",
                                "Delete one message. The delete itself is a provider action with no permanent-delete flag."),
    "fastmail_folder_create": (schema({"account_id": _ACCOUNT, "name": {"type": "string", "minLength": 1, "maxLength": 100}},
                                      ("account_id", "name")), "folder_create", "Create a folder."),
    "fastmail_folder_update": (schema({"account_id": _ACCOUNT, "folder_id": _REF, "name": {"type": "string", "minLength": 1,
                                       "maxLength": 100}, **_EXPECTED}, ("account_id", "folder_id", "name", "expected_state")),
                               "folder_update", "Rename a folder."),
    "fastmail_folder_delete": (schema({"account_id": _ACCOUNT, "folder_id": _REF, **_EXPECTED},
                                      ("account_id", "folder_id", "expected_state")), "folder_delete", "Delete a folder."),
    "fastmail_draft_create": (schema({**_COMPOSE_FIELDS, "reply_message_id": _REF, "forward_message_id": _REF}, _COMPOSE_REQUIRED),
                              "create_draft", "Save a draft from a real account identity; nothing is sent."),
    "fastmail_draft_update": (schema({**_COMPOSE_FIELDS, "message_id": _REF, "reply_message_id": _REF, "forward_message_id": _REF,
                                      **_EXPECTED}, (*_COMPOSE_REQUIRED, "message_id", "expected_state")),
                              "update_draft", "Replace one draft's content; nothing is sent."),
    "fastmail_send": (schema(_COMPOSE_FIELDS, _COMPOSE_REQUIRED), "send",
                      "Send a new message from a real account identity. The request id makes a repeat idempotent."),
    "fastmail_reply": (schema({**_COMPOSE_FIELDS, "reply_message_id": _REF}, (*_COMPOSE_REQUIRED, "reply_message_id")), "send",
                       "Send a reply to one message from a real account identity."),
    "fastmail_forward": (schema({**_COMPOSE_FIELDS, "forward_message_id": _REF}, (*_COMPOSE_REQUIRED, "forward_message_id")), "send",
                         "Forward one message, with its original body and attachments, from a real account identity."),
    "fastmail_vacation_set": (schema({"account_id": _ACCOUNT, "vacation": _VACATION}, ("account_id", "vacation")), "vacation_set",
                              "Set the account's vacation responder, if the account advertises it."),
    "fastmail_masked_email_set": (schema({"account_id": _ACCOUNT, "masked_email": _MASKED_EMAIL}, ("account_id", "masked_email")),
                                  "masked_email_set", "Create or update a masked email, if the account advertises it."),
}
COMPOSING = frozenset({"fastmail_draft_create", "fastmail_draft_update", "fastmail_send", "fastmail_reply", "fastmail_forward"})
ACTIONS = {name: entry[1] for name, entry in MUTATIONS.items()}      # operation -> engine action
EXPOSURE = frozenset({"mcp", "dashboard", "worker"})


def catalog_entries() -> tuple[CatalogEntry, ...]:
    """Operation metadata, declared before the registry opens (the descriptors themselves are built at install)."""
    return (*(CatalogEntry(name, False, "read", EXPOSURE, False) for name in READS),
            *(CatalogEntry(name, False, "compound", EXPOSURE, False) for name in MUTATIONS))


def native_payload(operation: str, payload: dict) -> dict:
    """The typed engine action fields: the tool names inline attachment data ``content_base64``, the engine ``data_base64``."""
    result = dict(payload)
    result.pop("account_id", None)
    if "attachments" in result:
        result["attachments"] = [{**{key: value for key, value in item.items() if key != "content_base64"},
                                  "data_base64": item["content_base64"]} for item in result["attachments"]]
    return result


def account_authority(ctx, uow, resolver):
    """The one account gate: the installation owner, the configured ecosystem Ordubashy or an explicitly granted global worker."""
    from bazarlyk.agents.worker_access import is_worker
    if ctx.business_id is not None or ctx.home_business_id is not None or CAPABILITY not in ctx.global_capabilities:
        raise DomainError("forbidden", "Fastmail requires the explicit Global account grant")
    installation = uow.repository("installation").get("service")
    if ctx.principal_kind == "human":
        if not installation or installation["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "The Fastmail account belongs to the installation owner")
        return
    if ctx.principal_kind != "agent":
        raise DomainError("forbidden", "The Fastmail account is available only to the owner or an authorized global agent")
    if not installation:
        raise DomainError("forbidden", "Installation identity is unavailable")
    # A global capability is necessary but deliberately insufficient on its own: a generic ecosystem profile must not become a
    # private-mail proxy. Resolve from the authoritative profile, never from a caller field.
    profile, _ = resolver.profile(ctx.principal, connection=uow.connection)
    from bazarlyk.management import ManagementService
    coordinator = profile.data["role"]["id"] == "ordubashy" and ManagementService._coordinator(uow) == ctx.principal_id
    if (profile.data["status"] != "active" or profile.scope.kind != "ecosystem"
            or not (coordinator or is_worker(uow.connection, ctx.principal_id))):
        raise DomainError("forbidden", "Fastmail requires the configured ecosystem Ordubashy or an authorized global worker")


def check_sender(identities, identity_id: str, from_address: str) -> dict:
    """The account identity that may send as ``from_address``; a mismatch, an unknown identity or one the engine would refuse
    (an automatic Bcc or Reply-To override) is denied before anything is queued."""
    identity = next((item for item in identities if isinstance(item, dict) and item.get("id") == identity_id), None)
    if identity is None:
        raise DomainError("sender_identity_denied", "identity_id is not an identity of this Fastmail account")
    if str(identity.get("email", "")).lower() != from_address.lower():
        raise DomainError("sender_identity_mismatch", "from_address does not match the email of the chosen identity")
    if identity.get("bcc") or identity.get("replyTo"):
        raise DomainError("sender_identity_denied", "This identity has an automatic Bcc or Reply-To and cannot be used")
    return identity


def check_attachments(attachments) -> None:
    total = 0
    for item in attachments or ():
        total += len(item["content_base64"])
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (binascii.Error, ValueError):
            raise DomainError("validation_error", f"Attachment {item['filename']} is not valid base64") from None
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise DomainError("validation_error", f"Attachment {item['filename']} does not match its sha256")
    if total > MAX_INLINE_ATTACHMENT_BASE64:
        raise DomainError("validation_error", "Combined inline attachment data exceeds the 1 MiB base64 limit")


class FastmailOperations:
    """Registers the descriptors on the open registry and serves their handlers through the lazily built engine client."""

    def __init__(self, runtime, provider):
        self.runtime, self.provider = runtime, provider
        self.programs = runtime.programs

    # -- access ----------------------------------------------------------------------------------------------------
    def access(self, ctx, uow):
        account_authority(ctx, uow, self.runtime.resolver)

    def client(self):
        client = self.provider.client()
        if client is None:
            raise DomainError("provider_unavailable", "The Fastmail engine is not configured on this host")
        return client

    def account(self, client, wanted=None) -> dict:
        """The engine's capability view, checked to be the configured account and, when named, the account the caller meant."""
        capabilities = client.capabilities()
        account_id = capabilities.get("account_id") if isinstance(capabilities, dict) else None
        if not isinstance(account_id, str) or not account_id:
            raise DomainError("external_result_invalid", "The Fastmail engine returned no configured account identity")
        pinned = self.provider.config.account_id
        if pinned is not None and pinned != account_id:
            raise DomainError("account_mismatch", "The engine reports a different Fastmail account than this host is configured for")
        if wanted is not None and wanted != account_id:
            raise DomainError("account_mismatch", "account_id is not the configured Fastmail account")
        return capabilities

    # -- registration ----------------------------------------------------------------------------------------------
    def register(self, registry) -> None:
        for name, (arguments, text) in READS.items():
            registry.register(OperationDescriptor(
                name, arguments, effect_kind="read", supports_signoff=False, scope_kinds={"global"},
                resolve_namespace=lambda c, a: "global", global_capability=CAPABILITY, external_read=True, default_mode="allow",
                description="Read the owner's Fastmail account through the bounded engine API. " + text,
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        for name, (arguments, _, text) in MUTATIONS.items():
            registry.register(OperationDescriptor(
                name, arguments, effect_kind="compound", supports_signoff=False, scope_kinds={"global"},
                resolve_namespace=lambda c, a: "global", global_capability=CAPABILITY, default_mode="allow",
                description=text + " Queues one named job for the engine; read the outcome with fastmail_receipt_get (jobs_get needs a different grant). "
                            "Whether to ask the owner first follows the Email Management instructions.",
                prepare=lambda ctx, args, options, uow, op=name: self.builder(op, ctx, args, options, uow).build(),
                apply=self.apply, validate_targets=lambda c, p, u: self.access(c, u)))

    # -- reads -----------------------------------------------------------------------------------------------------
    def read(self, op, ctx, args, uow):
        self.access(ctx, uow)
        client = self.client()
        if op == "fastmail_capabilities_get":
            result = self.account(client)
        elif op == "fastmail_identities_list":
            result = client.identities()
        elif op == "fastmail_folders_list":
            result = client.folders()
        elif op == "fastmail_messages_search":
            result = client.search(args)
        elif op == "fastmail_message_get":
            result = client.message(args["message_id"])
        elif op == "fastmail_thread_get":
            result = client.thread(args["thread_id"], position=args.get("position"), limit=args.get("limit"))
        elif op == "fastmail_receipt_get":
            result = self.receipt(client, args["request_id"], uow)
        else:
            result = self.attachment(client, args)
        if not isinstance(result, dict) or len(json.dumps(plain(result), default=str)) > MAX_RESULT_BYTES:
            raise DomainError("external_result_invalid", "The Fastmail engine returned an invalid or oversized result")
        return plain(result)

    @staticmethod
    def job_state(uow, request_id):
        """The state of one of this extension's mutation jobs, without its arguments, or None for any other id."""
        row = uow.connection.execute("SELECT state, attempts, error_json, definition_ref_json, created_at, updated_at FROM jobs "
                                     "WHERE id=? AND kind=?", (request_id, PROGRAM)).fetchone()
        if row is None:
            return None
        error, ref = row["error_json"], row["definition_ref_json"]
        error = json.loads(error) if isinstance(error, (str, bytes)) else error
        ref = json.loads(ref) if isinstance(ref, (str, bytes)) else ref
        return {"state": row["state"], "attempts": row["attempts"], "operation": (ref or {}).get("fastmail_operation"),
                "error_code": error.get("code") if isinstance(error, dict) else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def receipt(self, client, request_id, uow):
        job = self.job_state(uow, request_id)
        try:
            receipt = client.receipt(request_id)
        except DomainError as exc:
            if job is None or exc.code != "operation_unavailable":
                raise
            receipt = None          # the engine cannot be asked now; the job's own state is still worth reporting
        if receipt is None and job is None:
            raise DomainError("not_found", "The engine has no receipt for this request id")
        return {"request_id": request_id, "receipt": receipt, "job": job}

    @staticmethod
    def attachment(client, args):
        chunk = client.attachment(args["attachment_id"], args["message_id"], offset=args.get("offset", 0),
                                  limit=args.get("limit", MAX_ATTACHMENT_CHUNK))
        if not isinstance(chunk, dict) or not isinstance(chunk.get("content"), bytes):
            raise DomainError("external_result_invalid", "The Fastmail engine returned an invalid attachment chunk")
        headers = {str(key).lower(): value for key, value in (chunk.get("headers") or {}).items()}
        try:
            total, offset = int(headers["x-attachment-total"]), int(headers["x-attachment-offset"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("external_result_invalid", "The Fastmail engine returned incomplete attachment range metadata") from None
        if total < 0 or offset < 0 or offset > total or offset + len(chunk["content"]) > total:
            raise DomainError("external_result_invalid", "The Fastmail engine returned invalid attachment range metadata")
        return {"message_id": args["message_id"], "attachment_id": args["attachment_id"], "total": total, "offset": offset,
                "next_offset": offset + len(chunk["content"]), "content_base64": base64.b64encode(chunk["content"]).decode()}

    # -- mutations -------------------------------------------------------------------------------------------------
    def builder(self, op, ctx, args, options, uow):
        self.access(ctx, uow)
        if args.get("reply_message_id") and args.get("forward_message_id"):
            raise DomainError("validation_error", "A compose action cannot be both a reply and a forward")
        check_attachments(args.get("attachments"))
        client = self.client()
        capabilities = self.account(client, args["account_id"])
        advertised = capabilities.get("actions")
        action = ACTIONS[op]
        if not isinstance(advertised, list) or action not in advertised:
            raise DomainError("operation_unavailable", "This Fastmail account does not advertise the requested capability")
        if op in COMPOSING:
            check_sender(capabilities.get("identities") or [], args["identity_id"], args["from_address"])
        builder = PlanBuilder(ctx, op, args, op.replace("_", " ").title(), namespace="global", affected_scope={"kind": "global"})
        label = PreviewResource("fastmail_account", args["account_id"], op)
        builder.resources.append(label)
        shown = {key: value for key, value in plain(args).items() if key not in {"body", "attachments", "vacation", "masked_email"}}
        if "attachments" in args:
            shown["attachments"] = [{key: item[key] for key in ("filename", "content_type", "sha256")} for item in args["attachments"]]
        builder.previews.append(StructuredPreview(label, tuple(FieldChange(key, None, value) for key, value in shown.items())))
        job = {"scope": "global", "account_id": args["account_id"], "operation": op, "arguments": plain(args)}
        prepare_job(builder, uow, PROGRAM, job, programs=self.programs, definition_extra={"fastmail_operation": op})
        builder.effects.append("The email engine receives one named, idempotent account action after current authorization is "
                               "rechecked; an uncertain outcome is recovered from its receipt, never by sending again.")
        return builder

    @staticmethod
    def apply(ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return {"job_id": next(change.resource.resource_id for change in plan.changes if change.resource.resource_type == "jobs")}
