"""Owner filing diagnostics, the focused Context instruction and the explicit graph edit: the source-side Context operations.

The shared Context reads (`context_home`, `context_object`, `context_expand`, `context_find`,
`context_changes`) are registered by `qorqut_context.integration.reading` over the published graph and are not
registered here. `context_filings` and `context_upload_map` are owner-only projections over the Operations
source versions, filings, queue inputs and attempts: exact source, input, attempt, event and publication facts,
never model logs. `context_instruct` is a focused correction surface granted only to approved role policies, in the
selected business or the Global target; it files one attributed instruction as its own frozen source through the same
admission, queue, publication and completion path as uploads. `context_edit` queues one explicit
`qorqut-graph-edit-v1` plan through the same shared queue (`qorqut_context.integration.graph_edits.graph_edit`): the host decides its
targets and authority, the Operations `context_graph_edits` binding makes it durable, and the queue driver runs exactly that
plan later with no model. Its supervised variant (a `qorqut-supervised-edit-v1` plan with an exact Markdown document, notes
included) is not queued: `qorqut_context.integration.graph_edits.supervised_edit` previews it purely or publishes it directly, through the
descriptor's prepare / external / apply lifecycle. `context_filing_costs`, the owner's usage read, lives in
`qorqut_context.integration.usage`.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import json

from qorqut_context.integration.graph_edits.supervised_edit import (EDIT_INPUT_SCHEMA as EDIT_INPUT, EDIT_OUTPUT_SCHEMA as EDIT_OUTPUT,
                                                                     SupervisedEdits, is_supervised, require_variant)
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.application.plans import IDENTITY, TEXT, PlanBuilder, apply_records, schema
from qorqut_operations.contracts import DomainError

INSTRUCTION = {"type": "string", "minLength": 1, "maxLength": 20000}
PEOPLE = {"type": "array", "items": TEXT, "maxItems": 20}
CURSOR = {"type": "string", "maxLength": 1024}
READS = {
    "context_filings": ({"upload_id": IDENTITY, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ()),
    "context_upload_map": ({"upload_id": IDENTITY}, ("upload_id",)),
}
DESCRIPTIONS = {
    "context_filings": instruction_text('source-operations-owner-view-recent-context-filings-or-every'),
    "context_upload_map": instruction_text('source-operations-owner-view-the-source-to-publication-chain'),
    "context_instruct": instruction_text('source-operations-focused-attributed-correction-to-context-you-are'),
    "context_edit": instruction_text('source-operations-explicit-typed-edit-of-the-published-context'),
}

# The authority metadata of these operations (declared once, here; the host's registry takes it from ``registration``).
ENTRIES = (
    CatalogEntry("context_filings", False, "read", frozenset({"dashboard", "worker"}), True),
    CatalogEntry("context_upload_map", False, "read", frozenset({"dashboard", "worker"}), True),
    CatalogEntry("context_instruct", False, "compound", frozenset({"mcp", "dashboard", "worker"})),
    CatalogEntry("context_edit", False, "compound", frozenset({"mcp", "dashboard", "worker"})),
)


class ContextSourceOperations:
    """``filing`` is the composed ``ContextFiling`` and ``graph_edits`` the controlled graph-edit authority; either may be None
    (those operations are then registered but unavailable). ``clock`` builds every plan. ``instruct_roles`` is the set of roles
    eligible to instruct and edit the one shared Context, in any business or the Global target, through the same validated
    operations, policy and source checks. Eligibility grants nothing: a worker has no default write grant and needs the
    ordinary explicit permission, rechecked at execution."""

    def __init__(self, *, filing, clock, graph_edits, instruct_roles):
        self.filing, self.clock, self.graph_edits = filing, clock, graph_edits
        self.instruct_roles = frozenset(instruct_roles)
        self.supervised = None if graph_edits is None else SupervisedEdits(graph_edits)

    def require_filing(self):
        if self.filing is None:
            raise DomainError("operation_unavailable", "Context filing is not composed on this installation")
        return self.filing

    def register(self, registry):
        shared = {"scope_kinds": {"business", "global"}, "global_capability": "read_global_context",
                  "resolve_namespace": lambda c, a: "business:" + c.business_id if c.business_id else "global"}
        for name, (properties, required) in READS.items():
            registry.register(OperationDescriptor(name, schema(properties, required), effect_kind="read", supports_signoff=False,
                exposure={"dashboard", "worker"}, owner_only=True,
                description=DESCRIPTIONS[name], read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow), **shared))
        registry.register(OperationDescriptor("context_instruct", schema({"text": INSTRUCTION, "people": PEOPLE, "rationale": TEXT}, ("text",)),
            effect_kind="compound", exposure={"mcp", "dashboard", "worker"}, owner_only=False, supports_signoff=False, description=DESCRIPTIONS["context_instruct"],
            owner_session=True,
            prepare=self.prepare, apply=self.apply, **shared))
        registry.register(OperationDescriptor("context_edit", EDIT_INPUT, EDIT_OUTPUT,
            effect_kind="compound", exposure={"mcp", "dashboard", "worker"}, owner_only=False, supports_signoff=False,
            description=DESCRIPTIONS["context_edit"], prepare=self.prepare_edit, external=self.external_edit, apply=self.apply_edit,
            **shared))
        return self

    def read(self, operation, ctx, args, uow):
        if operation in ("context_filings", "context_upload_map"):
            if ctx.principal_kind != "human":
                raise DomainError("forbidden", "Filing facts are an owner view")
            if operation == "context_filings":
                return self.filings(ctx, uow, upload_id=args.get("upload_id"), limit=args.get("limit", 20))
            return self.upload_map(ctx, uow, args["upload_id"])
        raise DomainError("operation_unavailable", "Context operation is not implemented")

    def _facts(self, uow, ctx, where, parameters, limit):
        """Filing facts of this namespace, newest first: one row per accepted source version."""
        found = uow.connection.execute(
            "SELECT f.id AS filing_id, f.job_id, f.program_id, f.program_version, f.request_id, v.source_kind, v.source_id, "
            "v.source_revision, v.operation, v.source_path, v.source_digest, i.id AS input_id, i.sequence, i.state AS input_state, "
            "i.attempt, i.context_event_id, i.error_json AS input_error, i.created_at AS queued_at, j.state AS job_state, "
            "c.id AS completion_id, c.outcome, c.published_commit, c.state AS completion_state, c.application_json "
            "FROM context_filings f JOIN context_source_versions v ON v.id = f.source_version_id "
            "JOIN context_inputs i ON i.id = f.input_id LEFT JOIN jobs j ON j.id = f.job_id "
            "LEFT JOIN context_completions c ON c.filing_id = f.id "
            "WHERE f.business_id IS ? " + where + " ORDER BY i.sequence DESC LIMIT ?", (ctx.business_id, *parameters, limit)).fetchall()
        facts = []
        for row in found:
            row = dict(row)
            attempts = [{"attempt": item["attempt"], "context_event_id": item["context_event_id"], "cause": item["cause"],
                         "started_at": item["started_at"], "outcome": item["outcome"], "finished_at": item["finished_at"],
                         "error": json.loads(item["error_json"]) if item["error_json"] else None}
                        for item in uow.connection.execute(
                            "SELECT attempt, context_event_id, cause, started_at, outcome, finished_at, error_json "
                            "FROM context_input_attempts WHERE input_id=? ORDER BY attempt", (row["input_id"],))]
            facts.append({
                "filing_id": row["filing_id"], "request_id": row["request_id"],
                "source": {"kind": row["source_kind"], "id": row["source_id"], "revision": row["source_revision"],
                           "operation": row["operation"], "path": row["source_path"], "digest": row["source_digest"]},
                "job": {"id": row["job_id"], "program": row["program_id"], "version": row["program_version"], "state": row["job_state"]},
                "input": {"id": row["input_id"], "sequence": row["sequence"], "state": row["input_state"], "attempt": row["attempt"],
                          "queued_at": row["queued_at"], "error": json.loads(row["input_error"]) if row["input_error"] else None},
                "attempts": attempts,
                "publication": None if row["outcome"] != "published" else {
                    "context_event_id": row["context_event_id"], "published_commit": row["published_commit"]},
                "completion": None if row["completion_id"] is None else {
                    "receipt_id": row["completion_id"], "outcome": row["outcome"], "state": row["completion_state"],
                    "application": json.loads(row["application_json"]) if row["application_json"] else None}})
        return facts

    def filings(self, ctx, uow, *, upload_id, limit):
        """Recent filings of the selected namespace, or every version of one upload."""
        where, parameters = ("AND v.source_kind='document' AND v.source_id=?", (upload_id,)) if upload_id else ("", ())
        return {"filings": self._facts(uow, ctx, where, parameters, limit)}

    def upload_map(self, ctx, uow, upload_id):
        """The source-to-publication chain of one upload: its recorded facts, oldest version first. Never an outline."""
        versions = list(reversed(self._facts(uow, ctx, "AND v.source_kind='document' AND v.source_id=?", (upload_id,), 100)))
        if not versions:
            raise DomainError("not_found", "No Context filing is recorded for this upload")
        return {"upload_id": upload_id, "versions": versions,
                "published": [item["publication"] | {"source_revision": item["source"]["revision"]} for item in versions if item["publication"]]}

    def prepare(self, ctx, args, options, uow):
        if ctx.principal_kind == "agent":
            role_id = getattr(ctx.role_artifact_ref, "id", None)
            if role_id not in self.instruct_roles:
                raise DomainError("forbidden", "This role cannot issue context corrections")
        elif ctx.principal_kind != "human":
            raise DomainError("forbidden", "Only an owner or an authorized context role may instruct the context base")
        else:
            owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
            if not owner or owner["owner_principal_id"] != ctx.principal_id:
                raise DomainError("forbidden", "Only the installation owner may issue human context instructions")
        namespace = "business:" + ctx.business_id if ctx.business_id else "global"
        scope = {"kind": "business", "business_id": ctx.business_id} if ctx.business_id else {"kind": "global"}
        builder = PlanBuilder(ctx, "context_instruct", args, "Context Instruct", clock=self.clock, namespace=namespace, affected_scope=scope)
        self.require_filing().admit_instruction(builder, uow, args, self.attribution(ctx, uow))
        builder.effects.append("Files an attributed, focused context correction; it does not launch tasks or approve external actions.")
        return builder.build()

    @staticmethod
    def attribution(ctx, uow):
        """The caller's frozen attribution: who instructed, as what, in which scope (the registration, never the text)."""
        actor_id = ctx.initiated_by_principal_id or ctx.principal_id
        actor = uow.connection.execute("SELECT id, kind, display_name FROM principals WHERE id=?", (actor_id,)).fetchone()
        role_id = None
        if actor and actor["kind"] == "agent":
            profile = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (actor_id,)).fetchone()
            if profile:
                role_id = (json.loads(profile["profile_json"]).get("role") or {}).get("id")
        return {"principal_id": actor_id, "principal_kind": actor["kind"] if actor else "unknown",
                "display_name": actor["display_name"] if actor else None, "role_id": role_id}

    def apply(self, ctx, plan, uow):
        records = apply_records(ctx, plan, uow)
        jobs = [record for record in records if isinstance(record, dict) and "kind" in record and "state" in record]
        filings = [record for record in records if isinstance(record, dict) and "source_version_id" in record]
        return {"job": {"id": jobs[0]["id"], "kind": jobs[0]["kind"], "state": jobs[0]["state"]} if jobs else None,
                "filing": None if not filings else {"filing_id": filings[0]["id"], "input_id": filings[0]["input_id"]}}

    def require_edits(self):
        if self.graph_edits is None:
            raise DomainError("operation_unavailable", "Graph edits are not composed on this installation")
        return self.graph_edits

    def require_supervised(self):
        if self.supervised is None:
            raise DomainError("operation_unavailable", "Supervised Context edits are not composed on this installation")
        return self.supervised

    def prepare_edit(self, ctx, args, options, uow):
        require_variant(args)
        if is_supervised(args):
            return self.require_supervised().prepare(ctx, args, options, uow, self.clock)
        return self.require_edits().prepare(ctx, args, options, uow, self.clock)

    def external_edit(self, ctx, plan):
        if is_supervised(plan.arguments):
            self.require_supervised().external(ctx, plan)

    def apply_edit(self, ctx, plan, uow):
        if is_supervised(plan.arguments):
            return self.require_supervised().apply(ctx, plan, uow)
        return self.require_edits().apply(ctx, plan, uow)
