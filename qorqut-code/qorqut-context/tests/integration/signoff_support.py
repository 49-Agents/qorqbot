"""A synthetic host for the extracted Signoff machinery: fake ports, a tiny domain and real engine parts.

Everything real here is the engine (Application, PolicyResolver, receipts, FileApplier, SignoffService); everything
fake is what a trusted host would contribute (the ports, one ``items``/``plans``/``mail`` domain, the agent registry).
No product code, provider, network or live data is involved."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from qorqut_operations import signoffs
from qorqut_operations.application import (Application, CatalogContribution, CatalogEntry, OperationDescriptor,
                                           compose_catalog)
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, InvocationContext,
                                         PreparedChange, PreviewResource, Principal, ResourceRef, TextPreview, new_id)
from qorqut_operations.global_agents import effective_business_ids
from qorqut_operations.policy import AgentPolicyBasis, PolicyResolver, PolicyRules, WorkerProfile
from qorqut_operations.signoffs import ABSENT, ReferenceTarget, SignoffPorts, SignoffService
from qorqut_operations.storage import (FileApplier, LockPaths, RegisteredFileTarget, StorageContribution, TableSpec,
                                       file_values)
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.files import file_hash
from qorqut_operations.storage.repositories import decode_row
from support import AGENT, BUSINESS, HUMAN, WORKER, Demo, database_at

COORDINATOR = "00000000-0000-4000-8000-0000000000c1"   # the configured Ordubashy
OTHER_WORKER = "00000000-0000-4000-8000-0000000000c2"  # another global worker with no standing authority
STRANGER = "00000000-0000-4000-8000-0000000000c3"      # another human who owns nothing
DOCUMENT = "00000000-0000-4000-8000-0000000000d1"
INSTALLATION = "11111111-1111-4111-8111-111111111111"
PLUGIN, TOOL = "email-management", "email.send"

HOST_SCHEMA = (
    """CREATE TABLE inbox_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, source_type TEXT NOT NULL,
        action TEXT NOT NULL, actor_id TEXT, before_json TEXT, after_json TEXT)""",
    """CREATE TABLE host_routing (id INTEGER PRIMARY KEY AUTOINCREMENT, signoff_id TEXT NOT NULL UNIQUE,
        coordinator_id TEXT NOT NULL, attention_destination TEXT NOT NULL, management_allowed INTEGER NOT NULL)""",
)
HOST_TABLES = {"inbox_changes": TableSpec(("id",), mutable=False, revision_column=None),
               "host_routing": TableSpec(("id",), mutable=False, revision_column=None)}


# -- fake ports ----------------------------------------------------------------------------------------------------

class FakeExecution:
    execution_states = ("not_applicable", "not_started", "applied", "conflicted", "failed", "awaiting_approval")

    def execution_state(self, row, uow=None):
        if row["category"] == "input_request":
            return "not_applicable"
        return row["state"] if row["state"] in {"applied", "conflicted", "failed"} else "not_started"

    def execution_sql(self, alias):
        assert alias == "s"
        return ("(CASE WHEN s.category = 'input_request' THEN 'not_applicable' "
                "WHEN s.state IN ('applied','conflicted','failed') THEN s.state ELSE 'not_started' END)")


class FakePeers:
    def __init__(self):
        self.ordubashy, self.calls = COORDINATOR, []

    def configured_ordubashy(self, uow, *, require_codex):
        self.calls.append(("ordubashy", require_codex))
        return self.ordubashy


class FakeTasks:
    def __init__(self):
        self.count, self.calls = 2, []

    def open_assigned_count(self, uow, business_id, principal_id, now):
        self.calls.append((business_id, principal_id, now))
        return self.count


class FakeReferences:
    targets = {"signoff": ReferenceTarget("signoffs_get", "signoff_id"), "item": ReferenceTarget("items_get", "item_id")}

    @staticmethod
    def task_namespace(ctx, args):
        if args.get("scope") == "global" or (not ctx.business_id and args.get("scope") != "business"):
            return "global"
        return "business:" + (ctx.business_id or "")


class FakePlatform:
    """The platform-action domain: records its calls in the transaction that makes them, never sends anything."""

    def __init__(self):
        self.calls, self.fail_staged, self.replaceable, self.submission = [], False, False, (PLUGIN, TOOL)

    def _log(self, uow, kind, detail=""):
        self.calls.append(kind)
        uow.connection.execute("INSERT INTO demo_history(kind, detail) VALUES (?, ?)", (f"platform:{kind}", detail))

    def staging_values(self, context, plan, old, uow):
        self._log(uow, "staging_values")
        return {"id": new_id(), "execution_job_id": new_id()}

    def staged(self, context, row, plan, old, uow):
        # The proposal row already exists in this very transaction.
        assert uow.connection.execute("SELECT 1 FROM signoffs WHERE id=?", (row["id"],)).fetchone()
        self._log(uow, "staged", row["id"])
        if self.fail_staged:
            raise DomainError("operation_failed", "synthetic staging failure")

    def transition(self, row, updated, uow):
        self._log(uow, "transition", f"{row['state']}>{updated['state']}")

    def replaceable_after_cancel(self, old, uow):
        return self.replaceable

    def view(self, row, uow=None):
        return {"submission": {"plugin_id": self.submission[0], "tool": self.submission[1]}}

    def is_email_submission(self, plan):
        return plan.arguments.get("submission") == "email.send"


class FakeEmail:
    def __init__(self):
        self.hidden = ""

    def visibility(self, context, uow):
        return (self.hidden, []) if self.hidden else ("", [])

    def email_context(self, row, uow):
        return {"thread": "synthetic"}


class FakeManagement:
    managed_input_sql = ("s.category='input_request' AND EXISTS (SELECT 1 FROM host_routing r WHERE r.signoff_id=s.id "
                         "AND r.attention_destination='management' AND r.management_allowed=1)")

    def __init__(self, coordinator):
        self.coordinator_id, self.route_new = coordinator, True

    def coordinator(self, uow):
        if not self.coordinator_id:
            raise DomainError("management_unavailable", "No management coordinator is configured")
        return self.coordinator_id

    def auto_route_input(self, context, row, uow):
        if self.route_new and row["preview_json"]["input"].get("response_policy") == "management_allowed":
            uow.connection.execute("INSERT INTO host_routing(signoff_id, coordinator_id, attention_destination, "
                                   "management_allowed) VALUES (?,?,?,1)", (row["id"], self.coordinator_id, "management"))

    def routing_for(self, uow, signoff_id):
        row = uow.connection.execute("SELECT * FROM host_routing WHERE signoff_id=?", (signoff_id,)).fetchone()
        return dict(row) if row else None


class FakeFocus:
    fields = {"card": {"type": "string", "maxLength": 80}}

    def __init__(self):
        self.calls = []

    def creation_access(self, application, context, focus, uow):
        assert isinstance(application, Application)
        self.calls.append(("access", dict(focus)))

    def present_created(self, application, context, focus, source, uow):
        assert isinstance(application, Application)
        self.calls.append(("present", dict(focus), source["type"]))

    def release_signoff(self, row, updated, uow):
        self.calls.append(("release", updated["state"]))


# -- a fake agent registry (the host's AgentAuthority), reading the same profile rows the engine revalidates -------------

class FakeAgents:
    def __init__(self):
        self.defaults = {name: "allow" for name in (
            "signoffs_get", "signoffs_list", "signoffs_notes_list", "signoffs_comment", "signoffs_input_create",
            "signoffs_input_cancel", "signoffs_attention_set", "signoffs_reject", "signoffs_input_answer_delegated",
            "items_get", "email_send")}
        self.defaults.update(items_create="signoff", plans_set="signoff", mail_post="signoff")
        self.overrides, self.revision = {}, 1

    def refresh_invocation_context(self, ctx, uow):
        """The host contract: a global agent's businesses are the active directory (``effective_business_ids``), never its
        stored list. A retained business-home registration is returned unchanged so the engine's own refusal is exercised."""
        row = uow.connection.execute("SELECT home_business_id, revision, profile_json FROM agent_profiles "
                                     "WHERE agent_id=?", (ctx.principal_id,)).fetchone()
        scope = json.loads(row["profile_json"]).get("scope", {})
        home = row["home_business_id"]
        return replace(ctx, home_business_id=home, profile_revision=row["revision"],
                       allowed_business_ids=frozenset({home}) if home else effective_business_ids(uow.connection, ctx.principal_id),
                       global_capabilities=frozenset(scope.get("global_capabilities", ())))

    def policy_basis(self, ctx, uow):
        return AgentPolicyBasis(self.defaults, self.overrides, {"id": "role-a", "version": "1"}, self.revision)

    def plugin_declares(self, ctx, basis, descriptor, uow):
        return True


# -- the demo domain: items, a managed file, and mail ----------------------------------------------------------------------

def item_prepare(ctx, args, options, uow):
    builder = PlanBuilder(ctx, "items_create", args, "Create item")
    builder.record("items", args["id"], None, {"id": args["id"], "title": args.get("title", "Item"), "status": "todo",
                                              "created_by": ctx.principal_id})
    return builder.build()


def mail_prepare(operation):
    def prepare(ctx, args, options, uow):
        builder = PlanBuilder(ctx, operation, args, "Send mail")
        builder.record("items", args["id"], None, {"id": args["id"], "title": "mail", "status": "sent",
                                                  "created_by": ctx.principal_id})
        return builder.build()
    return prepare


def file_prepare(root):
    def prepare(ctx, args, options, uow):
        document = uow.repository("documents", business_id=ctx.business_id).get(args["document_id"], required=True)
        path = root / document["path"]
        before = path.read_bytes()
        resource = ResourceRef("documents", document["id"], ctx.business_id)
        label = PreviewResource("documents", document["id"], "Plan")
        scope = {"kind": "business", "business_id": ctx.business_id}
        return ChangeSet("plans_set", 1, ctx.principal_id, "business:" + ctx.business_id, args, scope,
            (ExpectedTarget(resource, revision=document["revision"], content_hash=file_hash(path), exists=True),),
            (PreparedChange("file", resource, "write", file_values(args["text"].encode(), before_hash=file_hash(path))),),
            ChangePreview("Update plan", "", scope, (label,),
                          (TextPreview(label, before.decode(), args["text"], "exact test diff"),)))
    return prepare


def record_apply(ctx, plan, uow):
    return {"records": [row["id"] for row in apply_records(ctx, plan, uow)]}


def file_apply(ctx, plan, uow):
    change, target = plan.changes[0], plan.expected_targets[0]
    uow.repository("documents", business_id=ctx.business_id).update(
        change.resource.resource_id, {"content_hash": change.values["after_hash"]}, expected_revision=target.revision)
    return {"applied": plan.arguments["document_id"]}


class SyntheticHost:
    """One composed synthetic host. ``ports`` carries the fakes; ``service``/``app`` are the real engine parts."""

    def __init__(self, tmp_path, *, platform=True, email=False, management=True, focus=True,
                 narrowing=ABSENT, now="2026-10-12T11:59:00.000000Z"):
        self.tmp_path = Path(tmp_path)
        self.now = now
        self.root = self.tmp_path / "authored"
        self.root.mkdir()
        self.agents, self.peers, self.tasks = FakeAgents(), FakePeers(), FakeTasks()
        self.platform = FakePlatform() if platform else ABSENT
        self.email = FakeEmail() if email else ABSENT
        self.management = FakeManagement(COORDINATOR) if management else ABSENT
        self.focus = FakeFocus() if focus else ABSENT
        self.ports = SignoffPorts(execution=FakeExecution(), peers=self.peers, tasks=self.tasks,
                                  references=FakeReferences(), platform=self.platform, email=self.email,
                                  management=self.management, focus=self.focus, narrowing=narrowing)
        self.database = database_at(self.tmp_path, demo=Demo(), extra=(
            StorageContribution("host", HOST_TABLES, schema=HOST_SCHEMA), signoffs.storage_contribution()))
        self.locks = LockPaths.under(self.tmp_path / "locks")
        self.seed()
        self.file_applier = FileApplier(self.database, lambda resource: RegisteredFileTarget(self.root, "PLAN.md"),
                                        locks=self.locks)
        self.service = SignoffService(self.database, self.ports, locks=self.locks, file_applier=self.file_applier,
                                      clock=lambda: self.now)
        self.catalog = compose_catalog([CatalogContribution("demo", self.entries(), self.descriptors()),
                                        self.service.contribution()], include_batch=False)
        self.policy = PolicyResolver(self.catalog.registry, self.agents, rules=PolicyRules(),
                                     worker_profiles={WORKER: WorkerProfile({}, frozenset({BUSINESS}))})
        self.admission = RecoveryAdmission()
        self.app = Application(self.database, self.catalog.registry, policy=self.policy, signoffs=self.service,
                               file_executor=PreparedFileExecutor(self.database, self.file_applier),
                               writer_admission=self.admission, review_exceptions=self.catalog.review_exceptions)
        self.service.bind(self.app)

    # -- seeding ---------------------------------------------------------------------------------------------------

    def seed(self):
        with self.database.transaction() as uow:
            principals = uow.repository("principals")
            for identity, kind, name in ((AGENT, "agent", "Proposer"), (COORDINATOR, "agent", "Ordubashy"),
                                         (OTHER_WORKER, "agent", "Other worker"), (STRANGER, "human", "Stranger"),
                                         (WORKER, "system", "Worker")):
                principals.insert({"id": identity, "kind": kind, "display_name": name})
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION,
                "owner_principal_id": HUMAN, "management_coordinator_id": COORDINATOR})
            # Every acting agent is global; AGENT and OTHER_WORKER hold no global capability and no membership row.
            plain = {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []}
            profiles = {
                AGENT: (None, {"status": "active", "role": {"id": "global-worker"}, "scope": plain}),
                COORDINATOR: (None, {"status": "active", "role": {"id": "ordubashy"}, "runtime": "codex", "scope": {
                    "kind": "ecosystem", "allowed_business_ids": [BUSINESS], "global_capabilities": ["read_global_context"]}}),
                OTHER_WORKER: (None, {"status": "active", "role": {"id": "global-worker"}, "scope": plain})}
            for identity, (home, profile) in profiles.items():
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, "
                    "profile_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                    (identity, identity, home, home or "ecosystem", identity[-4:], str(self.tmp_path / identity[-4:]),
                     json.dumps(profile)))
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": STRANGER})
            uow.repository("documents", business_id=BUSINESS).insert({"id": DOCUMENT, "kind": "plan", "path": "PLAN.md",
                "content_hash": hashlib.sha256(b"Before\n").hexdigest()})
        (self.root / "PLAN.md").write_bytes(b"Before\n")

    # -- the demo domain -------------------------------------------------------------------------------------------

    @staticmethod
    def entries():
        return (CatalogEntry("items_create", True, "records"), CatalogEntry("items_get", False, "read"),
                CatalogEntry("plans_set", True, "files"), CatalogEntry("email_send", True, "records"),
                CatalogEntry("mail_post", True, "records"))

    def descriptors(self):
        item = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "title": {"type": "string"}}}
        mail = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"},
                                                                   "submission": {"type": "string"}}}
        return (
            OperationDescriptor("items_create", item, prepare=item_prepare, apply=record_apply),
            OperationDescriptor("items_get", {"type": "object", "properties": {"item_id": {"type": "string"},
                "signoff_id": {"type": "string"}}}, effect_kind="read", supports_signoff=False,
                read=lambda ctx, args, options, uow: {"item": uow.repository("items", business_id=ctx.business_id).get(
                    args["item_id"])} if "item_id" in args else {}),
            OperationDescriptor("plans_set", {"type": "object", "required": ["document_id", "text"], "properties": {
                "document_id": {"type": "string"}, "text": {"type": "string"}}}, effect_kind="files",
                prepare=file_prepare(self.root), apply=file_apply),
            OperationDescriptor("email_send", mail, prepare=mail_prepare("email_send"), apply=record_apply,
                                signoff_category="platform_action"),
            OperationDescriptor("mail_post", mail, prepare=mail_prepare("mail_post"), apply=record_apply),
        )

    # -- contexts --------------------------------------------------------------------------------------------------

    @staticmethod
    def agent(principal=AGENT, **kwargs):
        return InvocationContext(Principal(principal, "agent"), business_id=BUSINESS,
            allowed_business_ids=frozenset({BUSINESS}), profile_revision=1, source="mcp", **kwargs)

    @staticmethod
    def owner(**kwargs):
        return InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS,
                                 allowed_business_ids=frozenset({BUSINESS}), source="dashboard", **kwargs)

    @staticmethod
    def coordinator(**kwargs):
        return InvocationContext(Principal(COORDINATOR, "agent"), business_id=BUSINESS,
            allowed_business_ids=frozenset({BUSINESS}), global_capabilities=frozenset({"read_global_context"}),
            profile_revision=1, source="mcp", **kwargs)

    @staticmethod
    def stranger():
        return InvocationContext(Principal(STRANGER, "human"), business_id=BUSINESS,
                                 allowed_business_ids=frozenset({BUSINESS}), source="dashboard")

    # -- helpers ---------------------------------------------------------------------------------------------------

    def stage(self, operation="items_create", arguments=None, *, ctx=None, key=None):
        if arguments is None:
            arguments = {"id": "item-1", "title": "First"}
        return self.app.invoke(ctx or self.agent(), operation, arguments, {"idempotency_key": key or new_id()})

    def row(self, signoff_id):
        with self.database.read() as uow:
            return decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone())

    def decide(self, row, decision="approve", *, ctx=None, key=None, feedback=None, **overrides):
        values = dict(decision=decision, proposal_revision=row["proposal_revision"], payload_hash=row["payload_hash"],
                      expected_status_revision=row["status_revision"], idempotency_key=key or new_id(), feedback=feedback)
        values.update(overrides)
        return self.service.review(ctx or self.owner(), row["id"], **values)

    def items(self):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute("SELECT * FROM items ORDER BY id")]

    def count(self, table, where="1"):
        with self.database.read() as uow:
            return uow.connection.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]
