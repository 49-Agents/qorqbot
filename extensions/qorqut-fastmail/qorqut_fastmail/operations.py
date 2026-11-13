"""The ``fastmail_*`` account operations: policy, validation and the durable job boundary around the engine's account API.

Account mail has no Project. Every operation is Global and names the Fastmail account it means. A mutation requires the
``manage_fastmail`` capability; a read is also admitted with ``read_global_context`` (``READ_CAPABILITY``), so every global
worker reads the owner's mail as Ordubashy does, while only a ``manage_fastmail`` grant changes it; sending and drafting name a real provider identity (``identity_id`` and the matching ``from_address``), which
is checked against the account's own identity list before a job exists and again by the engine before anything is sent. An
alias is simply an identity of the account: what an alias is for lives in the owner's Email Management instructions.

Sending, replying and forwarding require exact owner Signoff. Subject and body can be revised through the shared message
editor; recipients, sender and attachments stay frozen. Other account mutations retain their existing policy. The caller's
tool grants, Global capability and account authority still decide who may call them. A mutation queues one named background job; the job id is the request id of
the engine's idempotent ledger, so the outcome is read with ``fastmail_receipt_get`` (``jobs_get`` needs a routine grant)."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import base64
import binascii
import hashlib
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import TEXT, PlanBuilder, apply_records, prepare_job, schema
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError, FieldChange, PreviewResource, StructuredPreview, canonical_hash, plain

CAPABILITY = "manage_fastmail"
# Read-only alternative to CAPABILITY: reading mail needs no write grant. Mutations never accept it.
READ_CAPABILITY = "read_global_context"
PROGRAM = "fastmail.account_mutation"
MAX_INLINE_ATTACHMENT_BASE64 = 1 * 1024 * 1024          # below the 2 MiB MCP request ceiling; no separate upload protocol
MAX_RESULT_BYTES = 12 * 1024 * 1024
MAX_ATTACHMENT_CHUNK = 5 * 1024 * 1024

_ACCOUNT = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": r"^[A-Za-z0-9_-]+$",
            "description": instruction_text('operations-the-fastmail-account-id-reported-by-fastmail-capabilities')}
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
    "identity_id": {**_REF, "description": instruction_text('operations-an-identity-id-from-fastmail-identities-list-its')},
    "from_address": _ADDRESS,
    "to": {"type": "array", "minItems": 1, "maxItems": 50, "items": _ADDRESS},
    "cc": {"type": "array", "maxItems": 50, "items": _ADDRESS},
    "bcc": {"type": "array", "maxItems": 50, "items": _ADDRESS},
    "subject": {"type": "string", "minLength": 1, "maxLength": 998},
    "body": {"type": "string", "minLength": 1, "maxLength": 50_000},
    "attachments": {"type": "array", "maxItems": 10, "items": _ATTACHMENT,
                    "description": instruction_text('operations-inline-attachment-data-is-limited-to-1-mib')}}
_COMPOSE_REQUIRED = ("account_id", "identity_id", "from_address", "to", "subject", "body")
_VACATION = schema({"id": _REF, "isEnabled": {"type": "boolean"}, "fromDate": TEXT, "toDate": TEXT,
                    "subject": {"type": "string", "maxLength": 998}, "textBody": {"type": "string", "maxLength": 50_000}})
_MASKED_EMAIL = schema({"id": _REF, "state": {"type": "string", "enum": ["enabled", "disabled"]},
                        "description": {"type": "string", "maxLength": 255},
                        "forDomain": {"type": "string", "maxLength": 255, "pattern": r"^[A-Za-z0-9.-]+$"},
                        "url": {"type": "string", "maxLength": 2000},
                        "emailPrefix": {"type": "string", "maxLength": 64, "pattern": r"^[A-Za-z0-9._+-]+$"}})

READS = {
    "fastmail_capabilities_get": (schema(), instruction_text('operations-the-configured-fastmail-account-its-id-supported-actions')),
    "fastmail_identities_list": (schema(), instruction_text('operations-the-account-s-sending-identities-id-name-email')),
    "fastmail_folders_list": (schema(), instruction_text('operations-the-account-s-folders-with-their-ids-and')),
    "fastmail_messages_search": (schema({
        "q": TEXT, "from": _ADDRESS, "to": _ADDRESS, "subject": TEXT, "in_mailbox": _REF,
        "has_keyword": {"type": "string", "minLength": 1, "maxLength": 64}, "before": TEXT, "after": TEXT,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "position": {"type": "integer", "minimum": 0}}),
        instruction_text('operations-search-messages-in-the-account-at-most-100')),
    "fastmail_message_get": (schema({"account_id": _ACCOUNT, "message_id": _REF}, ("message_id",)),
                             instruction_text('operations-read-one-message-with-its-expected-state-optional')),
    "fastmail_thread_get": (schema({"account_id": _ACCOUNT, "thread_id": _REF, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                    "position": {"type": "integer", "minimum": 0}}, ("thread_id",)),
                            instruction_text('operations-read-one-thread-page-at-most-100-messages')),
    "fastmail_attachment_get": (schema({"attachment_id": _REF, "message_id": _REF, "offset": {"type": "integer", "minimum": 0},
                                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ATTACHMENT_CHUNK}},
                                       ("attachment_id", "message_id")),
                                instruction_text('operations-read-one-attachment-range-at-most-5-mib')),
    "fastmail_receipt_get": (schema({"request_id": _REF}, ("request_id",)),
                             instruction_text('operations-look-up-a-mutation-by-its-request-id')),
}
MUTATIONS = {
    "fastmail_message_update": (schema({
        "account_id": _ACCOUNT, "message_id": _REF, "add_keywords": _KEYWORDS, "remove_keywords": _KEYWORDS,
        "move_to_folder_id": _REF, "trash": {"type": "boolean"}, "restore": {"type": "boolean"}, "seen": {"type": "boolean"},
        "flagged": {"type": "boolean"}, "answered": {"type": "boolean"}, **_EXPECTED},
        ("account_id", "message_id", "expected_state")), "message_update",
        instruction_text('operations-mark-move-trash-or-restore-one-message-ordinary')),
    "fastmail_message_delete": (schema({"account_id": _ACCOUNT, "message_id": _REF, **_EXPECTED},
                                       ("account_id", "message_id", "expected_state")), "message_delete",
                                instruction_text('operations-delete-one-message-the-delete-itself-is-a')),
    "fastmail_folder_create": (schema({"account_id": _ACCOUNT, "name": {"type": "string", "minLength": 1, "maxLength": 100}},
                                      ("account_id", "name")), "folder_create", instruction_text('operations-create-a-folder')),
    "fastmail_folder_update": (schema({"account_id": _ACCOUNT, "folder_id": _REF, "name": {"type": "string", "minLength": 1,
                                       "maxLength": 100}, **_EXPECTED}, ("account_id", "folder_id", "name", "expected_state")),
                               "folder_update", instruction_text('operations-rename-a-folder')),
    "fastmail_folder_delete": (schema({"account_id": _ACCOUNT, "folder_id": _REF, **_EXPECTED},
                                      ("account_id", "folder_id", "expected_state")), "folder_delete", instruction_text('operations-delete-a-folder')),
    "fastmail_draft_create": (schema({**_COMPOSE_FIELDS, "reply_message_id": _REF, "forward_message_id": _REF}, _COMPOSE_REQUIRED),
                              "create_draft", instruction_text('operations-save-a-draft-from-a-real-account-identity')),
    "fastmail_draft_update": (schema({**_COMPOSE_FIELDS, "message_id": _REF, "reply_message_id": _REF, "forward_message_id": _REF,
                                      **_EXPECTED}, (*_COMPOSE_REQUIRED, "message_id", "expected_state")),
                              "update_draft", instruction_text('operations-replace-one-draft-s-content-nothing-is-sent')),
    "fastmail_send": (schema(_COMPOSE_FIELDS, _COMPOSE_REQUIRED), "send",
                      instruction_text('operations-send-a-new-message-from-a-real-account')),
    "fastmail_reply": (schema({**_COMPOSE_FIELDS, "reply_message_id": _REF}, (*_COMPOSE_REQUIRED, "reply_message_id")), "send",
                       instruction_text('operations-send-a-reply-to-one-message-from-a')),
    "fastmail_forward": (schema({**_COMPOSE_FIELDS, "forward_message_id": _REF}, (*_COMPOSE_REQUIRED, "forward_message_id")), "send",
                         instruction_text('operations-forward-one-message-with-its-original-body-and')),
    "fastmail_vacation_set": (schema({"account_id": _ACCOUNT, "vacation": _VACATION}, ("account_id", "vacation")), "vacation_set",
                              instruction_text('operations-set-the-account-s-vacation-responder-if-the')),
    "fastmail_masked_email_set": (schema({"account_id": _ACCOUNT, "masked_email": _MASKED_EMAIL}, ("account_id", "masked_email")),
                                  "masked_email_set", instruction_text('operations-create-or-update-a-masked-email-if-the')),
}
SENDING = frozenset({"fastmail_send", "fastmail_reply", "fastmail_forward"})
COMPOSING = frozenset({"fastmail_draft_create", "fastmail_draft_update", "fastmail_send", "fastmail_reply", "fastmail_forward"})
ACTIONS = {name: entry[1] for name, entry in MUTATIONS.items()}      # operation -> engine action
EXPOSURE = frozenset({"mcp", "dashboard", "worker"})


def catalog_entries() -> tuple[CatalogEntry, ...]:
    """Operation metadata, declared before the registry opens (the descriptors themselves are built at install)."""
    return (*(CatalogEntry(name, False, "read", EXPOSURE, False) for name in READS),
            *(CatalogEntry(name, name in SENDING, "compound", EXPOSURE, False) for name in MUTATIONS))


def native_payload(operation: str, payload: dict) -> dict:
    """The typed engine action fields: the tool names inline attachment data ``content_base64``, the engine ``data_base64``."""
    result = dict(payload)
    result.pop("account_id", None)
    if "attachments" in result:
        result["attachments"] = [{**{key: value for key, value in item.items() if key != "content_base64"},
                                  "data_base64": item["content_base64"]} for item in result["attachments"]]
    return result


def forward_source(client, message_id):
    """Capture only immutable provider message content; folder/read flags do not affect approval."""
    response = client.message(message_id)
    message = response.get('message') if isinstance(response, dict) else None
    if (not isinstance(message, dict) or not isinstance(message.get('body'), dict)
            or not isinstance(message.get('from'), dict) or not isinstance(message['from'].get('emailAddress'), dict)
            or not isinstance(message.get('subject'), str) or message.get('id') != message_id or message.get('bodyTruncated') is not False
            or not isinstance(message.get('body', {}).get('content'), str)
            or not isinstance(message.get('attachments'), list)
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str) for item in message['attachments'])):
        raise DomainError('invalid_plan', 'The complete forwarded message is unavailable')
    return {key: message.get(key) for key in ('id', 'from', 'subject', 'body', 'attachments')}


def account_authority(ctx, uow, resolver, *, read=False):
    """The one account gate: the installation owner, the configured ecosystem Ordubashy or an active global worker.

    A mutation needs ``manage_fastmail``; a read (``read=True``) is also admitted with ``read_global_context``. The identity
    rule is the same for both."""
    from qorqut.agents.worker_access import is_worker
    granted = CAPABILITY in ctx.global_capabilities or (read and READ_CAPABILITY in ctx.global_capabilities)
    if ctx.business_id is not None or ctx.home_business_id is not None or not granted:
        raise DomainError("forbidden", (f"Fastmail reads require {CAPABILITY} or {READ_CAPABILITY}" if read
                                        else f"Fastmail changes require the explicit Global account grant {CAPABILITY}"),
                          details={"denial_reason": "capability_not_granted", "missing_capability": CAPABILITY})
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
    from qorqut.management import ManagementService
    coordinator = profile.data["role"]["id"] == "ordubashy" and ManagementService._coordinator(uow) == ctx.principal_id
    if (profile.data["status"] != "active" or profile.scope.kind != "ecosystem"
            or not (coordinator or is_worker(uow.connection, ctx.principal_id))):
        raise DomainError("forbidden", "Fastmail requires the configured ecosystem Ordubashy or an active global worker")


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
    def access(self, ctx, uow, *, read=False):
        account_authority(ctx, uow, self.runtime.resolver, read=read)

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
                global_read_capability_alternatives=frozenset({READ_CAPABILITY}),
                description=instruction_text('operations-read-the-owner-s-fastmail-account-through-the') + text,
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        for name, (arguments, _, text) in MUTATIONS.items():
            registry.register(OperationDescriptor(
                name, arguments, effect_kind="compound", supports_signoff=name in SENDING, scope_kinds={"global"},
                resolve_namespace=lambda c, a: "global", global_capability=CAPABILITY, default_mode="signoff" if name in SENDING else "allow",
                editable_fields=("subject", "body") if name in SENDING else (),
                revise=self.revise if name in SENDING else None,
                description=text + instruction_text('operations-queues-one-named-job-for-the-engine-read')
                            + (instruction_text('operations-sending-requires-owner-approval-of-the-exact-recipients')
                             if name in SENDING else instruction_text('operations-this-mutation-does-not-send-email')),
                prepare=lambda ctx, args, options, uow, op=name: self.builder(op, ctx, args, options, uow).build(),
                apply=self.apply, validate_targets=self.validate_targets))

    # -- reads -----------------------------------------------------------------------------------------------------
    def read(self, op, ctx, args, uow):
        self.access(ctx, uow, read=True)
        client = self.client()
        if op == "fastmail_capabilities_get":
            result = self.account(client)
        elif op == "fastmail_receipt_get":
            result = self.receipt(client, args["request_id"], uow)
        else:
            self.account(client, args.get("account_id"))  # prove the configured and any explicitly named account before fetching
            if op == "fastmail_identities_list":
                result = client.identities()
            elif op == "fastmail_folders_list":
                result = client.folders()
            elif op == "fastmail_messages_search":
                result = client.search(args)
            elif op == "fastmail_message_get":
                result = client.message(args["message_id"])
            elif op == "fastmail_thread_get":
                result = client.thread(args["thread_id"], position=args.get("position"), limit=args.get("limit"))
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
            self.account(client)
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
        if op in SENDING and ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Ask an authorized agent to propose the email, then approve its Signoff")
        if args.get("reply_message_id") and args.get("forward_message_id"):
            raise DomainError("validation_error", "A compose action cannot be both a reply and a forward")
        check_attachments(args.get("attachments"))
        if op in SENDING and ('\x00' in args['body'] or not args['body'].strip()
                or not args['subject'].strip() or any(c in args['subject'] for c in '\r\n\x00')):
            raise DomainError('validation_error', 'Email requires a single-line subject and a nonempty body')
        client = self.client()
        capabilities = self.account(client, args["account_id"])
        advertised = capabilities.get("actions")
        action = ACTIONS[op]
        if not isinstance(advertised, list) or action not in advertised:
            raise DomainError("operation_unavailable", "This Fastmail account does not advertise the requested capability")
        if op in COMPOSING:
            check_sender(capabilities.get("identities") or [], args["identity_id"], args["from_address"])
        title = {'fastmail_send': 'Send email', 'fastmail_reply': 'Reply by email', 'fastmail_forward': 'Forward email'}.get(op, op.replace('_', ' ').title())
        builder = PlanBuilder(ctx, op, args, title, namespace="global", affected_scope={"kind": "global"})
        label = PreviewResource("fastmail_account", args["account_id"], op)
        builder.resources.append(label)
        shown = {key: value for key, value in plain(args).items() if key not in {"attachments", "vacation", "masked_email"} and (op in SENDING or key != "body")}
        if "attachments" in args:
            shown["attachments"] = [{key: item[key] for key in ("filename", "content_type", "sha256")} for item in args["attachments"]]
        builder.previews.append(StructuredPreview(label, tuple(FieldChange(key, None, value) for key, value in shown.items())))
        job = {"scope": "global", "account_id": args["account_id"], "operation": op, "arguments": plain(args)}
        if op == 'fastmail_forward':
            source = forward_source(client, args['forward_message_id'])
            job['source_digest'] = canonical_hash(source)
            sender = (source.get('from') or {}).get('emailAddress', {}).get('address', '')
            builder.effects.extend([
                'Source body:\n' + f"From: {sender}\nSubject: {source.get('subject') or ''}\n\n" + source['body']['content'],
                'Source attachments: ' + (', '.join(item.get('name', 'attachment') for item in source['attachments']) or 'None')])
        shown_lengths = (len(builder.resources), len(builder.previews), len(builder.effects))
        prepare_job(builder, uow, PROGRAM, job, programs=self.programs, definition_extra={"fastmail_operation": op})
        if op in SENDING:
            # Keep the queued job in the frozen plan, without exposing its machinery as a second email preview.
            del builder.resources[shown_lengths[0]:], builder.previews[shown_lengths[1]:], builder.effects[shown_lengths[2]:]
        builder.effects.append("Approving queues this email for sending. Saving edits does not send it." if op in SENDING else
                               "The email engine receives one named, idempotent account action after current authorization is "
                               "rechecked; an uncertain outcome is recovered from its receipt, never by sending again.")
        return builder

    def validate_targets(self, ctx, plan, uow):
        self.access(ctx, uow)
        if plan.operation_name in SENDING:
            args = plain(plan.arguments)
            self.runtime.registry.bind_arguments(plan.operation_name, args)
            capabilities = self.account(self.client(), args['account_id'])
            check_sender(capabilities.get('identities') or [], args['identity_id'], args['from_address'])
            jobs = [c for c in plan.changes if c.resource.resource_type == 'jobs']
            expected = {'scope': 'global', 'account_id': args['account_id'], 'operation': plan.operation_name, 'arguments': args}
            if plan.operation_name == 'fastmail_forward':
                expected['source_digest'] = canonical_hash(forward_source(self.client(), args['forward_message_id']))
            if len(jobs) != 1 or plain(jobs[0].values.get('arguments_json')) != expected:
                raise DomainError('invalid_plan', 'Email job differs from the reviewed content')

    def revise(self, ctx, original, changes, uow):
        self.validate_targets(ctx, original, uow)
        args = {**plain(original.arguments), **changes}
        self.runtime.registry.bind_arguments(original.operation_name, args)
        return self.builder(original.operation_name, ctx, args, None, uow).build()

    @staticmethod
    def apply(ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return {"job_id": next(change.resource.resource_id for change in plan.changes if change.resource.resource_type == "jobs")}
