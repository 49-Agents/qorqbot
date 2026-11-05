"""The two generic MCP/worker tools over the external question store: ``context_questions`` and ``context_question_edit``.

Both are Global-scope tools for the installation owner and the current Learning Applier only. Their policy basis is the
caller's current ``context_edit`` authorization (the host's policy rule, ``registration.question_rule``): there is no new capability, profile or
role publication, and an explicit ``context_edit`` deny or revocation cannot be bypassed through these tools. Every handler
re-authorizes that existing operation under the caller's refreshed identity and delegation, in the read, before the external
effect and again in the applying transaction.

A mutation follows the host's external-operation lifecycle: ``prepare`` validates and binds a request identity, ``external``
performs the store write exactly once per request key, and ``apply`` returns the recorded result. The store's mutation
receipt makes an uncertain retry a replay, never a second or a resurrecting effect. Results carry question IDs and links
only. Nothing here answers a question, changes a status, or touches the published Context, the graph or notes; resolving a
question is deleting its row."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
import hashlib
import sqlite3
import threading

from qorqut_context.integration import graph_edits as bindings
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, PreviewResource, plain

from .store import ACTIONS, DEFAULT_LIMIT, KINDS, MAX_LIMIT, QuestionConflict, QuestionError, QuestionNotFound, \
    QuestionStore, QuestionStoreError, normalize_mutation

READ = "context_questions"
EDIT = "context_question_edit"
AUTHORITY = "context_edit"
CAPABILITY = "read_global_context"
# The authority metadata of the two tools (declared once, here; the host's registry takes it from ``registration``).
ENTRIES = (
    CatalogEntry(READ, False, "read", frozenset({"mcp", "worker"})),
    CatalogEntry(EDIT, False, "compound", frozenset({"mcp", "worker"})),
)
OUTCOME_LIMIT = 256
OFFSET_LIMIT = 1_000_000

QUESTION_ID = {"type": "string", "minLength": 1, "maxLength": 64}
CONTEXT_ID = {"type": "string", "pattern": "^[a-z0-9]{5}$"}
QUESTION_TEXT = {"type": "string", "minLength": 1, "maxLength": 20000}
LINK = schema({"kind": {"enum": list(KINDS)}, "id": CONTEXT_ID}, ("kind", "id"))
LINKS = {"type": "array", "items": LINK, "maxItems": 20}
RESULT_LINKS = {"type": "array", "items": schema({"kind": {"enum": list(KINDS)}, "id": {"type": "string", "minLength": 1}},
                                                 ("kind", "id"))}

READ_INPUT = schema({
    "action": {"enum": ["list", "get"]},
    "question_id": QUESTION_ID,
    "related_kind": {"enum": list(KINDS)},
    "related_id": CONTEXT_ID,
    "offset": {"type": "integer", "minimum": 0, "maximum": OFFSET_LIMIT},
    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
}, ("action",))
EDIT_INPUT = schema({
    "action": {"enum": list(ACTIONS)},
    "question_id": QUESTION_ID,
    "text": QUESTION_TEXT,
    "related": LINKS,
}, ("action",))
EDIT_OUTPUT = schema({
    "action": {"enum": list(ACTIONS)}, "question_id": QUESTION_ID, "applied": {"type": "boolean"},
    "replayed": {"type": "boolean"}, "related": RESULT_LINKS,
}, ("action", "question_id", "applied", "replayed", "related"))

DESCRIPTIONS = {
    READ: "Learning Applier and owner only. List open Context review questions kept outside the published Context "
          "(action=list, optionally filtered by one related_kind and related_id, with offset and limit) or read one "
          "(action=get, question_id). Each question has its ID, text, origin event and the linked event, node, edge or "
          "note IDs. An empty list is not proof that no review is needed. Start a review only when the owner asks.",
    EDIT: "Learning Applier and owner only. Create, update or delete one open Context review question outside the "
          "published Context; this never edits the graph or notes. Review manually: read the questions, preview and "
          "propose a supervised context_edit, get the owner's approval, apply it, and delete only the questions the "
          "owner's answers resolved. When an answer is unknown leave the question unchanged. Results carry question IDs "
          "and links only; read a question with context_questions.",
}


def request_key(principal_id, idempotency_key):
    return hashlib.sha256(f"{principal_id}:{idempotency_key}".encode()).hexdigest()


def unavailable(error):
    return DomainError("operation_unavailable", "The question store cannot be used now; retry the same request",
                       retryable=True, details={"reason": type(error).__name__})


def refused(error):
    if isinstance(error, QuestionNotFound):
        return DomainError("not_found", str(error))
    if isinstance(error, QuestionConflict):
        return DomainError("conflict", str(error))
    return DomainError("validation_error", str(error))


class QuestionAuthority:
    """Who may use the question tools: the installation owner or the Learning Applier, and only while ``context_edit`` is
    currently authorized for them in the Global namespace."""

    def __init__(self, application, learning_applier_role):
        if not isinstance(learning_applier_role, str) or not learning_applier_role:
            raise ValueError("The question authority needs the host's Learning Applier role ID")
        self.application, self.learning_applier_role = application, learning_applier_role

    @property
    def descriptor(self):
        return self.application.registry.get(AUTHORITY)

    def current(self, ctx, uow):
        refreshed = self.application.policy.refresh_context(ctx, uow)
        delegation = bindings.delegation_of(ctx)
        carried = (refreshed.delegation_binding_id, refreshed.delegation_assignment_id, refreshed.delegation_generation)
        if delegation is None:
            if any(value is not None for value in carried):
                raise DomainError("invalid_principal", "A direct caller's context cannot carry a delegation")
            return refreshed
        frozen = (delegation["binding_id"], delegation["assignment_id"], delegation["generation"])
        if any(value is not None for value in carried) and carried != frozen:
            raise DomainError("invalid_principal", "Context refresh cannot replace a delegated worker binding")
        return replace(refreshed, delegation_binding_id=frozen[0], delegation_assignment_id=frozen[1],
                       delegation_generation=frozen[2])

    def check(self, ctx, uow):
        current = self.current(ctx, uow)
        if current.business_id or current.home_business_id:
            raise DomainError("forbidden", "Context questions are Global; no business can be selected")
        if current.principal_kind == "human":
            owner = uow.repository("installation").get("service") or {}
            if owner.get("owner_principal_id") != current.principal_id:
                raise DomainError("forbidden", "Only the installation owner may use Context questions")
        elif current.principal_kind == "agent":
            if getattr(current.role_artifact_ref, "id", None) != self.learning_applier_role:
                raise DomainError("forbidden", "Only the Learning Applier may use Context questions")
        else:
            raise DomainError("forbidden", "Only the owner or the Learning Applier may use Context questions")
        self.application.authorize(current, self.descriptor, (), uow, namespace="global")
        return current


class ContextQuestionOperations:
    def __init__(self, application, open_store: Callable[[], QuestionStore], *, learning_applier_role: str):
        self.application, self.open_store = application, open_store
        self.authority = QuestionAuthority(application, learning_applier_role)
        self.outcomes: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self._store: QuestionStore | None = None

    @property
    def store(self) -> QuestionStore:
        if self._store is None:
            try:
                self._store = self.open_store()
            except ValueError as error:
                raise unavailable(error) from error
        return self._store

    def register(self, registry):
        common = dict(supports_signoff=False, scope_kinds={"global"}, exposure={"mcp", "worker"},
                      global_capability=CAPABILITY, resolve_namespace=lambda ctx, args: "global")
        registry.register(OperationDescriptor(READ, READ_INPUT, schema({"items": {"type": "array"}, "question": {"type": "object"},
                                                                         "next_offset": {"type": ["integer", "null"]}}),
            effect_kind="read", description=DESCRIPTIONS[READ],
            read=lambda ctx, args, options, uow: self.read(ctx, args, uow), **common))
        registry.register(OperationDescriptor(EDIT, EDIT_INPUT, EDIT_OUTPUT, effect_kind="compound",
            description=DESCRIPTIONS[EDIT], prepare=self.prepare, external=self.external, apply=self.apply,
            validate_targets=lambda ctx, plan, uow: self.authority.check(ctx, uow),
            replay_validator=lambda ctx, args, result, uow: self.authority.check(ctx, uow), **common))
        return self

    def read(self, ctx, args, uow):
        self.authority.check(ctx, uow)
        arguments = plain(args)
        if arguments["action"] == "get":
            extra = sorted(set(arguments) - {"action", "question_id"})
            if "question_id" not in arguments or extra:
                raise DomainError("validation_error", "get takes exactly a question_id")
            try:
                question = self.store.read(arguments["question_id"])
            except (sqlite3.Error, OSError, QuestionStoreError) as error:
                raise unavailable(error) from error
            except QuestionError as error:
                raise refused(error) from error
            if question is None:
                raise DomainError("not_found", "No such open question")
            return {"question": question}
        if "question_id" in arguments:
            raise DomainError("validation_error", "list does not take a question_id")
        try:
            return self.store.list(related_kind=arguments.get("related_kind"), related_id=arguments.get("related_id"),
                                   offset=arguments.get("offset", 0), limit=arguments.get("limit", DEFAULT_LIMIT))
        except (sqlite3.Error, OSError, QuestionStoreError) as error:
            raise unavailable(error) from error
        except QuestionError as error:
            raise refused(error) from error

    @staticmethod
    def change(arguments):
        arguments = plain(arguments)
        try:
            return normalize_mutation(arguments["action"], arguments.get("question_id"), arguments.get("text"),
                                      arguments.get("related"))
        except QuestionError as error:
            raise refused(error) from error

    def prepare(self, ctx, args, options, uow):
        self.authority.check(ctx, uow)
        self.change(args)
        builder = PlanBuilder(ctx, EDIT, args, "Context question change", namespace="global",
                              affected_scope={"kind": "global"})
        builder.resources.append(PreviewResource("context_question_request", request_key(ctx.principal_id, options.idempotency_key),
                                                 "Question store request identity"))
        builder.effects.append("Changes the external question store only; the published Context, its graph and notes are not changed.")
        return builder.build()

    def external(self, ctx, plan):
        change = self.change(plan.arguments)
        with self.application.database.read() as uow:
            self.authority.check(ctx, uow)
        key = plan.preview["resources"][0]["resource_id"]
        try:
            outcome = self.store.mutate(key, change["action"], question_id=change["question_id"], text=change["text"],
                                        related=change["related"])
        except (sqlite3.Error, OSError, QuestionStoreError) as error:
            raise unavailable(error) from error
        except QuestionError as error:
            raise refused(error) from error
        with self.lock:
            self.outcomes[plan.payload_hash] = outcome
            while len(self.outcomes) > OUTCOME_LIMIT:
                self.outcomes.popitem(last=False)

    def apply(self, ctx, plan, uow):
        self.authority.check(ctx, uow)
        with self.lock:
            outcome = self.outcomes.pop(plan.payload_hash, None)
        if outcome is None:
            raise DomainError("operation_unavailable", "The question change was not performed; retry it with the same "
                              "idempotency key", retryable=True)
        return outcome
