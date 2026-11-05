from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import difflib
import time

import pytest

from bazarlyk.agents.models import AgentProfile
from bazarlyk.agents.resolver import AgentConfigResolver, effective_tool_policy
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.application.engine import Application
from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.catalog.packages import Catalog, load_package
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, FieldChange,
    InvocationContext, PreparedChange, PreviewResource, Principal, ResourceRef, StructuredPreview,
    TextPreview, new_id, plain, utc_now)
from bazarlyk.policy import PolicyResolver, WorkerProfile
from bazarlyk.signoffs import PreparedFileExecutor
from qorqut_operations.signoffs import SignoffService
from qorqut_operations.storage.executor import application_claim
from bazarlyk.storage import Database
from bazarlyk.storage.files import FileApplier, RegisteredFileTarget, file_hash, file_values
from instance_support import PATHS, workspace_root

REPO = workspace_root()
NOW = "2026-09-08T12:00:00.000000Z"


class Scenario:
    def __init__(self, tmp_path):
        self.root = tmp_path / "businesses"
        self.root.mkdir()
        self.db = Database.for_root(self.root).initialize()
        from test_agent_configuration import role_ready_registry
        self.registry = role_ready_registry()
        self.business = new_id()
        self.other_business = new_id()
        self.owner = Principal(new_id(), "human", "Owner")
        self.viewer = Principal(new_id(), "human", "Member")
        self.preparations = 0
        self.default_text = "prepared"
        self.fail_apply = False
        with self.db.transaction() as uow:
            for biz, slug in ((self.business, "one"), (self.other_business, "two")):
                (self.root / slug / "hq").mkdir(parents=True)
                uow.repository("businesses").insert({"id": biz, "slug": slug, "name": slug,
                    "workspace": str(self.root / slug), "hq_path": str(self.root / slug / "hq")})
            for actor in (self.owner, self.viewer):
                uow.repository("principals").insert({"id": actor.id, "kind": actor.kind, "display_name": actor.display_name})
                for biz in (self.business, self.other_business):
                    uow.repository("business_memberships", business_id=biz).insert({"principal_id": actor.id,
                        "is_owner": actor == self.owner})
        self.catalog = Catalog(self.db, self.root, self.registry)
        self.roles = {}
        for role in ("business-agent", "global-marketing", "ordubashy"):
            manifest = f"roles/{role}/role.yaml"
            package = load_package(PATHS.component("roles"), manifest, "role", self.registry)
            target = self.catalog.artifact_path(package)
            for path, content in package.files.items():
                destination = target / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            with self.db.transaction() as uow:
                uow.repository("artifacts").insert({"kind": "role", "artifact_id": role, "version": package.version,
                    "digest": package.digest, "stored_path": str(target), "metadata_json": {"manifest_path": manifest}})
            self.roles[role] = package.ref
        self.resolver = AgentConfigResolver(self.db, self.root, self.registry, catalog=self.catalog)
        self.worker_profiles = {}
        self.policy = PolicyResolver(self.registry, self.resolver, worker_profiles=self.worker_profiles)
        self.app = Application(self.db, self.registry, policy=self.policy, context_resolver=self.policy.refresh_context)
        self._install_task_operations()
        self._install_documents()
        self.file_applier = FileApplier(self.db, self.file_target)
        self.app.file_executor = PreparedFileExecutor(self.db, self.file_applier)
        self.service = SignoffService(self.app, file_applier=self.file_applier, clock=lambda: NOW).register_operations()
        self.agent = self.add_agent("business-agent", {"tasks_create": "signoff", "tasks_update": "signoff", "context_document_update": "signoff"})
        self.owner_context = InvocationContext(self.owner, business_id=self.business,
            allowed_business_ids=frozenset({self.business, self.other_business}),
            global_capabilities=frozenset({"manage_shared_instructions", "read_global_context"}), source="dashboard")

    def add_agent(self, role="business-agent", overrides=None):
        actor = Principal(new_id(), "agent", role.title())
        scope = ({"kind": "ecosystem", "allowed_business_ids": [self.business, self.other_business],
                  "global_capabilities": ["manage_shared_instructions", "read_global_context"]}
                 if role == "ordubashy" else
                 {"kind": "ecosystem", "allowed_business_ids": [self.business, self.other_business], "global_capabilities": []}
                 if role == "global-marketing" else {"kind": "business", "business_id": self.business})
        profile = AgentProfile.from_dict({"schema_version": 2, "agent_id": actor.id, "profile_revision": 1,
            "name": "agent-" + actor.id, "display_name": actor.display_name, "status": "active", "runtime": "claude-code",
            "role": self.roles[role], "scope": scope, "assignment_brief": "Test prepared business changes.",
            "plugins": [], "tool_policy": {"overrides": overrides or {}}, "presentation": {}, "created_at": NOW}, self.registry)
        with self.db.transaction() as uow:
            uow.repository("principals").insert({"id": actor.id, "kind": "agent", "display_name": actor.display_name})
            uow.repository("business_memberships", business_id=self.business).insert({"principal_id": actor.id})
            uow.repository("agent_profiles", business_id=profile.scope.business_id).insert({"agent_id": actor.id,
                "principal_id": actor.id, "namespace": profile.scope.business_id or "ecosystem", "name": profile.data["name"],
                "workspace": str(self.root / "agents" / actor.id), "profile_json": profile.to_dict()})
        return actor

    def agent_context(self, actor=None, **kwargs):
        context = InvocationContext(actor or self.agent, business_id=self.business, **kwargs)
        with self.db.read() as uow:
            return self.policy.refresh_context(context, uow)

    def change_override(self, name, mode, actor=None):
        actor = actor or self.agent
        with self.db.transaction() as uow:
            profile, _ = self.resolver.profile(actor, connection=uow.connection)
            data = profile.to_dict()
            data["tool_policy"]["overrides"][name] = mode
            data["profile_revision"] += 1
            uow.repository("agent_profiles", business_id=profile.scope.business_id).update(actor.id,
                {"profile_json": data}, expected_revision=profile.revision)

    def _install_task_operations(self):
        schema = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
        def prepare(context, args, options, uow):
            self.preparations += 1
            resource = ResourceRef("tasks", new_id(), context.business_id)
            values = {"title": args["title"] + " " + self.default_text, "status": "open",
                      "created_by": context.principal_id}
            label = PreviewResource("tasks", resource.resource_id, values["title"])
            scope = {"kind": "business", "business_ids": [context.business_id]}
            return ChangeSet("tasks_create", 1, context.principal_id, "business:" + context.business_id, args,
                scope, (ExpectedTarget(resource, exists=False),), (PreparedChange("record", resource, "create", values),),
                ChangePreview("Create task", "Prepared task rationale", scope, (label,),
                    (StructuredPreview(label, (FieldChange("title", None, values["title"]),)),)))
        def apply(context, plan, uow):
            change = plan.changes[0]
            value = uow.repository("tasks", business_id=context.business_id).insert({"id": change.resource.resource_id,
                **plain(change.values)})
            if self.fail_apply:
                raise RuntimeError("Injected after DB write")
            return {"id": value["id"], "revision": value["revision"]}
        self.registry.register(OperationDescriptor("tasks_create", schema, prepare=prepare, apply=apply,
            aliases=("queue_work",), scope_kinds=frozenset({"business", "global"}), global_capability="manage_ecosystem_agents"))
        def prepare_update(context, args, options, uow):
            row = uow.repository("tasks", business_id=context.business_id).get(args["task_id"], required=True)
            resource = ResourceRef("tasks", row["id"], context.business_id)
            scope = {"kind": "business", "business_ids": [context.business_id]}
            return ChangeSet("tasks_update", 1, context.principal_id, "business:" + context.business_id, args, scope,
                (ExpectedTarget(resource, revision=options.expected_revision),),
                (PreparedChange("record", resource, "update", {"title": args["title"]}),),
                {"title": "Update task", "rationale": "", "affected_scope": scope, "resources": [], "changes": [], "effects": []})
        def apply_update(context, plan, uow):
            return uow.repository("tasks", business_id=context.business_id).update(plan.changes[0].resource.resource_id,
                plain(plan.changes[0].values), expected_revision=plan.expected_targets[0].revision)
        self.registry.register(OperationDescriptor("tasks_update", {"type": "object", "required": ["task_id", "title"],
            "properties": {"task_id": {"type": "string"}, "title": {"type": "string"}}}, prepare=prepare_update, apply=apply_update))

    def document(self, text="Before\n", *, global_scope=False):
        document_id = new_id()
        path = self.root / (document_id + ".md")
        path.write_text(text)
        business = None if global_scope else self.business
        with self.db.transaction() as uow:
            return uow.repository("documents", business_id=business).insert({"id": document_id,
                "namespace": "global" if global_scope else business, "kind": "shared_instruction" if global_scope else "context",
                "path": path.name, "content_hash": file_hash(path)})

    def file_target(self, resource):
        with self.db.read() as uow:
            row = uow.repository("documents", business_id=resource.business_id).get(resource.resource_id, required=True)
        return RegisteredFileTarget(self.root, row["path"])

    def _install_documents(self):
        def prepare(name, context, args, options, uow):
            self.preparations += 1
            global_scope = name == "shared_instructions_update"
            business = None if global_scope else context.business_id
            scope = {"kind": "global", "namespace": "shared_instructions"} if global_scope else {"kind": "business", "business_ids": [business]}
            targets, changes, previews = [], [], []
            for document_id in args["document_ids"]:
                row = uow.repository("documents", business_id=business).get(document_id, required=True)
                resource = ResourceRef("documents", document_id, business, "global" if global_scope else None)
                before = (self.root / row["path"]).read_text()
                after = args["text"]
                targets.append(ExpectedTarget(resource, revision=row["revision"], content_hash=row["content_hash"]))
                values = file_values(after.encode(), before_hash=row["content_hash"])
                changes.extend((PreparedChange("file", resource, "write", values),
                    PreparedChange("record", resource, "update", {"content_hash": values["after_hash"]})))
                label = PreviewResource("documents", document_id, row["path"])
                previews.append(TextPreview(label, before, after, "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True)))))
            return ChangeSet(name, 1, context.principal_id, "global" if global_scope else "business:" + business,
                args, scope, tuple(targets), tuple(changes), ChangePreview("Update document", "", scope,
                    tuple(x.resource for x in previews), tuple(previews)))
        def apply(context, plan, uow):
            revisions = {x.resource: x.revision for x in plan.expected_targets}
            ids = []
            for change in plan.changes:
                if change.kind == "record":
                    uow.repository("documents", business_id=change.resource.business_id).update(change.resource.resource_id,
                        plain(change.values), expected_revision=revisions[change.resource])
                    ids.append(change.resource.resource_id)
            return {"document_ids": ids}
        for name in ("context_document_update", "shared_instructions_update"):
            self.registry.register(OperationDescriptor(name, {"type": "object", "required": ["document_ids", "text"],
                "properties": {"document_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "text": {"type": "string"}}},
                effect_kind="compound", scope_kinds=frozenset({"global"} if name.startswith("shared") else {"business"}),
                global_capability="manage_shared_instructions" if name.startswith("shared") else None,
                prepare=lambda c,a,o,u,n=name: prepare(n,c,a,o,u), apply=apply))

    def propose(self, title="Task", *, actor=None, options=None):
        result = self.app.invoke(self.agent_context(actor), "tasks_create", {"title": title},
            {"idempotency_key": new_id(), **(options or {})})
        assert result.status == "signoff_required", result.to_dict()
        return dict(result.signoff)

    def decide(self, proposal, decision="approve", **kwargs):
        context = kwargs.pop("context", self.owner_context)
        return self.service.review(context, proposal["id"], decision=decision,
            proposal_revision=proposal["proposal_revision"], payload_hash=proposal["payload_hash"],
            expected_status_revision=proposal["status_revision"], idempotency_key=kwargs.pop("idempotency_key", new_id()), **kwargs)

    def detail(self, proposal, context=None):
        result = self.service.get(context or self.owner_context, proposal["id"])
        assert result.status == "ok", result.to_dict()
        return plain(result.data["signoff"])

    def count(self, table):
        assert table in ("tasks", "action_logs", "task_notes", "signoffs", "resource_claims", "apply_receipts",
                         "qbdv_document_versions", "qbdv_business_events")
        with self.db.read() as uow:
            return uow.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def scenario(tmp_path):
    value = Scenario(tmp_path)
    yield value
    value.service.stop()


def test_approval_applies_exact_plan_offline_without_repreparation(scenario):
    s = scenario
    proposal = s.propose()
    assert s.count("tasks") == 0
    preview = s.detail(proposal)["preview"]
    s.default_text = "changed after preparation"
    result = s.decide(proposal, feedback="Owner approval note")
    assert result.status == "accepted" and result.data["signoff"]["state"] == "approved"
    s.service.start()
    deadline = time.monotonic() + 4
    while s.detail(proposal)["state"] == "approved" and time.monotonic() < deadline:
        time.sleep(0.02)
    detail = s.detail(proposal)
    assert detail["state"] == "applied", detail
    assert detail["preview"] == preview and detail["decision"]["feedback"] == "Owner approval note"
    assert s.preparations == 1 and s.count("tasks") == 1
    with s.db.read() as uow:
        assert uow.repository("tasks", business_id=s.business).list()[0]["title"] == "Task prepared"
    assert s.count("action_logs") == s.count("task_notes") == 0


@pytest.mark.parametrize("feedback,expected", [("  retain spaces\nand newlines \n", "  retain spaces\nand newlines \n"), (" \n ", None), (None, None)])
def test_rejection_preserves_optional_feedback_for_agent(scenario, feedback, expected):
    s = scenario
    with s.db.transaction() as uow:
        uow.repository("installation").insert({"id":"service","installation_id":new_id(),"owner_principal_id":s.owner.id})
    proposal = s.propose()
    result = s.decide(proposal, "reject", feedback=feedback)
    if expected is None:
        assert result.error["code"] == "message_required"
        assert s.detail(proposal)["state"] == "pending"
        return
    assert result.status == "ok" and result.data["signoff"]["decision"]["feedback"] == expected
    listing = s.service.list(s.agent_context(), updated_after=0).to_dict()["data"]
    assert listing["items"][0]["decision"]["feedback"] == expected
    assert listing["items"][0]["proposer_display_name"] == "Business-Agent"
    assert s.count("tasks") == s.count("action_logs") == s.count("task_notes") == 0
    assert s.count("qbdv_document_versions") == s.count("qbdv_business_events") == 0


def test_owner_identity_membership_and_business_are_required(scenario):
    s = scenario
    proposal = s.propose()
    agent = replace(s.agent_context(), source="dashboard")
    member = replace(s.owner_context, principal=s.viewer)
    assert s.decide(proposal, context=agent).error["code"] == "forbidden"
    assert s.decide(proposal, context=member).error["code"] == "forbidden"
    wrong_business = replace(s.owner_context, business_id=s.other_business)
    assert s.decide(proposal, context=wrong_business).error["code"] == "not_found"
    with s.db.transaction() as uow:
        member = uow.repository("business_memberships", business_id=s.business).get(s.owner.id)
        uow.repository("business_memberships", business_id=s.business).update(s.owner.id, {"is_owner": False}, expected_revision=member["revision"])
    assert s.decide(proposal).error["code"] == "forbidden"


def test_duplicate_keys_matching_decisions_and_opposite_races(scenario):
    s = scenario
    proposal = s.propose()
    key = new_id()
    assert s.decide(proposal, idempotency_key=key).status == "accepted"
    assert s.decide(proposal, "reject").error["code"] == "stale_signoff"
    s.service.run_pending()
    replay = s.decide(proposal, idempotency_key=key)
    assert replay.data["replayed"] and replay.data["signoff"]["state"] == "applied"
    assert s.decide(proposal, idempotency_key=new_id()).data["replayed"]
    assert s.decide(proposal, "reject", idempotency_key=key).error["code"] == "idempotency_conflict"
    assert s.count("tasks") == 1
    second = s.propose("Race")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda decision: s.decide(second, decision), ("approve", "reject")))
    assert sum(result.status == "error" for result in results) == 1


def test_concurrent_mutation_retries_stage_one_proposal(scenario):
    s = scenario
    options = {"idempotency_key": new_id()}
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: s.app.invoke(s.agent_context(), "queue_work", {"title": "same"}, options), range(2)))
    assert all(x.status == "signoff_required" for x in results)
    assert results[0].signoff["id"] == results[1].signoff["id"]
    assert s.count("signoffs") == 1


@pytest.mark.parametrize("after_approve", [False, True])
def test_policy_revocation_conflicts_before_effects(scenario, after_approve):
    s = scenario
    proposal = s.propose()
    if after_approve:
        assert s.decide(proposal).status == "accepted"
    s.change_override("tasks_create", "deny")
    if after_approve:
        s.service.run_pending()
    else:
        key = new_id()
        assert s.decide(proposal, idempotency_key=key).error["code"] == "stale_signoff"
        assert s.decide(proposal, idempotency_key=key).error["code"] == "stale_signoff"
    assert s.detail(proposal)["state"] == "conflicted"
    assert s.count("tasks") == s.count("resource_claims") == 0


def test_changed_target_conflicts_and_does_not_lose_owner_edit(scenario):
    s = scenario
    created = s.app.invoke(s.owner_context, "tasks_create", {"title": "Owner"}, {"idempotency_key": new_id()})
    task_id = created.data["id"]
    staged = s.app.invoke(s.agent_context(), "tasks_update", {"task_id": task_id, "title": "Agent"},
        {"expected_revision": 1, "idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    with s.db.transaction() as uow:
        uow.repository("tasks", business_id=s.business).update(task_id, {"title": "Later owner edit"}, expected_revision=1)
    assert s.decide(dict(staged.signoff)).error["code"] == "stale_signoff"
    with s.db.read() as uow:
        assert uow.repository("tasks", business_id=s.business).get(task_id)["title"] == "Later owner edit"


def test_replacements_are_atomic_and_keep_rejected_feedback(scenario):
    s = scenario
    with s.db.transaction() as uow:
        uow.repository("installation").insert({"id":"service","installation_id":new_id(),"owner_principal_id":s.owner.id})
    first = s.propose()
    second = s.propose("replacement", options={"supersedes_signoff_id": first["id"], "expected_superseded_status_revision": 1})
    assert second["proposal_revision"] == 2
    assert s.detail(first)["state"] == "conflicted"
    assert s.detail(first)["superseded_by_id"] == second["id"]
    assert s.decide(first).error["code"] == "stale_signoff"
    rejected = s.decide(second, "reject", feedback="Keep this feedback").data["signoff"]
    third = s.propose("revised", options={"supersedes_signoff_id": second["id"],
                                         "expected_superseded_status_revision": rejected["status_revision"]})
    assert third["proposal_revision"] == 3
    assert s.detail(second)["decision"]["feedback"] == "Keep this feedback"
    assert s.decide(third).status == "accepted"
    attempt = s.app.invoke(s.agent_context(), "tasks_create", {"title": "invalid replacement"},
        {"idempotency_key": new_id(), "supersedes_signoff_id": third["id"], "expected_superseded_status_revision": 2})
    assert attempt.status == "error" and s.count("signoffs") == 3


def test_replacement_options_cannot_silently_become_direct_application(scenario):
    s = scenario
    proposal = s.propose()
    s.change_override("tasks_create", "allow")
    result = s.app.invoke(s.agent_context(), "tasks_create", {"title": "Replacement"},
        {"idempotency_key": new_id(), "supersedes_signoff_id": proposal["id"],
         "expected_superseded_status_revision": proposal["status_revision"]})
    assert result.status == "error" and result.error["code"] == "validation_error", result.to_dict()
    assert s.count("tasks") == 0 and s.count("signoffs") == 1
    assert s.detail(proposal)["state"] == "pending" and s.detail(proposal)["superseded_by_id"] is None


def test_global_proposal_does_not_leak_in_business_rows_or_counts(scenario):
    s = scenario
    with s.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": s.owner.id})
    s.owner_context = replace(s.owner_context, business_id=None)
    root_agent = s.add_agent("ordubashy")
    document = s.document(global_scope=True)
    staged = s.app.invoke(s.agent_context(root_agent), "shared_instructions_update",
        {"document_ids": [document["id"]], "text": "Shared after\n"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    proposal = dict(staged.signoff)
    ordinary = s.service.list(s.agent_context()).to_dict()["data"]
    assert ordinary["items"] == [] and sum(ordinary["counts"].values()) == 0
    assert s.service.get(s.agent_context(), proposal["id"]).error["code"] == "not_found"
    assert s.detail(proposal)["affected_scope"]["kind"] == "global"
    no_global = replace(s.owner_context, global_capabilities=frozenset())
    assert s.decide(proposal, context=no_global).error["code"] == "not_found"
    assert s.decide(proposal).status == "accepted"
    s.service.run_pending()
    assert s.detail(proposal)["state"] == "applied"


def test_updated_cursors_equal_timestamps_filters_and_supersession(scenario):
    s = scenario
    with s.db.transaction() as uow:
        uow.repository("installation").insert({"id":"service","installation_id":new_id(),"owner_principal_id":s.owner.id})
    proposals = [s.propose(str(i)) for i in range(5)]
    first = s.service.list(s.agent_context(), updated_after=0, limit=2).to_dict()["data"]
    assert len(first["items"]) == 2 and first["has_more"]
    second = s.service.list(s.agent_context(), updated_after=first["updated_cursor"], limit=2).to_dict()["data"]
    third = s.service.list(s.agent_context(), updated_after=second["updated_cursor"], limit=2).to_dict()["data"]
    assert {r["id"] for page in (first, second, third) for r in page["items"]} == {p["id"] for p in proposals}
    rejected = s.decide(proposals[0], "reject", feedback="old feedback").data["signoff"]
    updates = s.service.list(s.agent_context(), updated_after=third["updated_cursor"]).to_dict()["data"]
    assert updates["items"][0]["decision"]["feedback"] == "old feedback"
    s.propose("new", options={"supersedes_signoff_id": proposals[0]["id"], "expected_superseded_status_revision": rejected["status_revision"]})
    linked = s.service.list(s.agent_context(), updated_after=updates["updated_cursor"]).to_dict()["data"]
    assert len(linked["items"]) == 2
    assert next(x for x in linked["items"] if x["id"] == proposals[0]["id"])["decision"]["feedback"] == "old feedback"
    assert s.service.list(s.owner_context, updated_after=first["updated_cursor"]).error["code"] == "validation_error"
    assert s.service.list(s.agent_context(), updated_after=first["updated_cursor"], states=["rejected"]).error["code"] == "validation_error"


class SimulatedCrash(BaseException):
    pass


def test_mixed_file_crash_restart_applies_once(scenario):
    s = scenario
    documents = [s.document() for _ in range(2)]
    staged = s.app.invoke(s.agent_context(), "context_document_update",
        {"document_ids": [d["id"] for d in documents], "text": "Exact after\n"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    proposal = dict(staged.signoff)
    assert s.decide(proposal).status == "accepted"
    def crash(_resource):
        raise SimulatedCrash()
    s.file_applier.after_replace = crash
    with pytest.raises(SimulatedCrash):
        s.service.run_one(proposal["id"])
    assert s.detail(proposal)["state"] == "approved"
    assert s.count("resource_claims") == 2
    s.file_applier.after_replace = None
    restarted = SignoffService(s.app, file_applier=s.file_applier)
    restarted.run_pending()
    assert s.detail(proposal)["state"] == "applied", s.detail(proposal)
    for document in documents:
        assert (s.root / document["path"]).read_text() == "Exact after\n"
        with s.db.read() as uow:
            assert uow.repository("documents", business_id=s.business).get(document["id"])["revision"] == 2
    restarted.run_pending()
    assert s.count("resource_claims") == 0 and s.preparations == 1


def test_partial_file_unexpected_edit_is_preserved_and_not_retried(scenario):
    s = scenario
    documents = [s.document() for _ in range(2)]
    staged = s.app.invoke(s.agent_context(), "context_document_update",
        {"document_ids": [d["id"] for d in documents], "text": "After\n"}, {"idempotency_key": new_id()})
    proposal = dict(staged.signoff)
    assert s.decide(proposal).status == "accepted"
    def edit_other(_resource):
        (s.root / documents[1]["path"]).write_text("Human edit\n")
        s.file_applier.after_replace = None
    s.file_applier.after_replace = edit_other
    s.service.run_pending()
    result = s.detail(proposal)
    assert result["state"] == "failed", result
    assert result["error"]["retryable"] is False and result["error"]["effect_status"] == "partial_or_uncertain"
    assert (s.root / documents[1]["path"]).read_text() == "Human edit\n"
    assert s.count("resource_claims") == 2


def test_db_failure_rolls_back_and_safe_owner_retry_reuses_plan(scenario):
    s = scenario
    proposal = s.propose()
    assert s.decide(proposal).status == "accepted"
    s.fail_apply = True
    s.service.run_pending()
    failed = s.detail(proposal)
    assert failed["state"] == "failed" and failed["error"]["retryable"]
    assert s.count("tasks") == 0
    s.fail_apply = False
    retried = s.service.retry(s.owner_context, proposal["id"], proposal_revision=proposal["proposal_revision"],
        payload_hash=proposal["payload_hash"], expected_status_revision=failed["status_revision"], idempotency_key=new_id())
    assert retried.status == "accepted", retried.to_dict()
    s.service.run_pending()
    assert s.detail(proposal)["state"] == "applied" and s.preparations == 1 and s.count("tasks") == 1


def test_worker_proposer_is_not_an_agent_and_has_explicit_grants(scenario):
    s = scenario
    worker = Principal(new_id(), "system", "Filing worker")
    with s.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
        uow.repository("business_memberships", business_id=s.business).insert({"principal_id": worker.id})
    context = InvocationContext(worker, business_id=s.business, source="worker")
    assert s.app.invoke(context, "tasks_create", {"title": "No grant"}, {"idempotency_key": new_id()}).status == "error"
    s.worker_profiles[worker.id] = WorkerProfile({"tasks_create": "signoff"}, frozenset({s.business}))
    staged = s.app.invoke(context, "tasks_create", {"title": "Worker"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    proposal = dict(staged.signoff)
    dto = s.detail(proposal)
    assert dto["proposer_agent_id"] is None and dto["proposer_principal_id"] == worker.id
    assert dto["proposer_display_name"] == "Filing worker"
    assert s.decide(proposal, context=replace(context, allowed_business_ids=frozenset({s.business}))).error["code"] == "forbidden"
    assert s.decide(proposal).status == "accepted"
    s.service.run_pending()
    assert s.detail(proposal)["state"] == "applied"


def test_effective_config_and_runtime_share_the_same_mode(scenario):
    s = scenario
    profile, _ = s.resolver.profile(s.agent)
    role = s.catalog.get("role", plain(profile.data["role"]))
    row = next(x for x in effective_tool_policy(profile, role, s.registry) if x["canonical_operation"] == "tasks_create")
    assert row["effective_mode"] == "signoff" and row["source"]["kind"] == "profile_override"
    s.change_override("tasks_create", "allow")
    result = s.app.invoke(s.agent_context(), "tasks_create", {"title": "Automatic"}, {"idempotency_key": new_id()})
    assert result.status == "ok" and s.count("signoffs") == 0 and s.count("tasks") == 1


def test_compound_child_policy_stages_or_denies_entire_operation(scenario):
    s = scenario
    task_operation = s.registry.get("tasks_create")
    def prepare(context, args, options, uow):
        plan = task_operation.prepare(context, args, options, uow)
        return replace(plan, operation_name="logs_add", changes=tuple(
            replace(change, operation_name="tasks_create") for change in plan.changes))
    s.registry.register(OperationDescriptor("logs_add", task_operation.input_schema, prepare=prepare,
        apply=task_operation.apply, derived_effects=("tasks_create",)))
    staged = s.app.invoke(s.agent_context(), "logs_add", {"title": "Derived task"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required" and s.count("tasks") == 0
    s.change_override("tasks_create", "deny")
    denied = s.app.invoke(s.agent_context(), "logs_add", {"title": "Denied child"}, {"idempotency_key": new_id()})
    assert denied.status == "error" and s.count("tasks") == 0 and s.count("signoffs") == 1


def test_worker_discretionary_changes_recheck_initiating_agent(scenario):
    s = scenario
    worker = Principal(new_id(), "system", "Bounded worker")
    with s.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
        uow.repository("business_memberships", business_id=s.business).insert({"principal_id": worker.id})
    s.worker_profiles[worker.id] = WorkerProfile({"tasks_create": "allow"}, frozenset({s.business}))
    context = InvocationContext(worker, business_id=s.business, source="worker", initiated_by_principal_id=s.agent.id)
    staged = s.app.invoke(context, "tasks_create", {"title": "Initiated work"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    dto = s.detail(dict(staged.signoff))
    assert dto["initiated_by_principal_id"] == s.agent.id and dto["initiated_by_display_name"] == "Business-Agent"
    s.change_override("tasks_create", "deny")
    refused = s.app.invoke(context, "tasks_create", {"title": "Denied"}, {"idempotency_key": new_id()})
    assert refused.status == "error" and s.count("tasks") == 0


@pytest.mark.parametrize("kind,verified,expected", [
    ("agent", False, "error"), ("agent", True, "ok"), ("human", False, "ok")])
def test_worker_historical_attribution_never_grants_discretionary_authority(scenario, kind, verified, expected):
    s = scenario
    worker = Principal(new_id(), "system", "Registered result worker")
    initiator = s.agent if kind == "agent" else s.owner
    with s.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
        uow.repository("business_memberships", business_id=s.business).insert({"principal_id": worker.id})
        uow.repository("principals").update(initiator.id, {"active": False}, expected_revision=1)
    s.worker_profiles[worker.id] = WorkerProfile({"tasks_create": "allow"}, frozenset({s.business}))
    if verified:
        proof = {"job_id": new_id(), "definition_digest": "a" * 64}
        s.policy.job_consequence_verifier = lambda *_: proof
        s.policy.job_plan_validator = lambda c, d, p, u: p.policy_binding["job_id"] == proof["job_id"]
    context = InvocationContext(worker, business_id=s.business, source="worker", initiated_by_principal_id=initiator.id,
        job_id=proof["job_id"] if verified else None)
    result = s.app.invoke(context, "tasks_create", {"title": "Registered output"}, {"idempotency_key": new_id()})
    assert result.status == expected, result.to_dict()
    assert s.count("tasks") == (expected == "ok")


def test_worker_grant_does_not_replace_revoked_business_membership(scenario):
    s = scenario
    worker = Principal(new_id(), "system", "Membership worker")
    with s.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
        uow.repository("business_memberships", business_id=s.business).insert({"principal_id": worker.id, "active": False})
    s.worker_profiles[worker.id] = WorkerProfile({"tasks_create": "allow"}, frozenset({s.business}))
    context = InvocationContext(worker, business_id=s.business, source="worker")
    result = s.app.invoke(context, "tasks_create", {"title": "No membership"}, {"idempotency_key": new_id()})
    assert result.status == "error" and s.count("tasks") == 0


@pytest.mark.parametrize("definition_changed", [False, True])
def test_worker_job_binding_survives_approval_restart(scenario, definition_changed):
    s = scenario
    worker = Principal(new_id(), "system", "Exact job output worker")
    job_id = new_id()
    proof = {"job_id": job_id, "definition_digest": "a" * 64}
    with s.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
        uow.repository("business_memberships", business_id=s.business).insert({"principal_id": worker.id})
        uow.repository("principals").update(s.agent.id, {"active": False}, expected_revision=1)
    s.worker_profiles[worker.id] = WorkerProfile({"tasks_create": "signoff"}, frozenset({s.business}))
    s.policy.job_consequence_verifier = lambda context, *_: proof if context.job_id == job_id else None
    s.policy.job_plan_validator = lambda c, d, p, u: p.policy_binding["job_id"] == job_id
    context = InvocationContext(worker, business_id=s.business, source="worker", job_id=job_id,
                                initiated_by_principal_id=s.agent.id)
    staged = s.app.invoke(context, "tasks_create", {"title": "Frozen job output"}, {"idempotency_key": new_id()})
    assert staged.status == "signoff_required", staged.to_dict()
    proposal = dict(staged.signoff)
    assert s.detail(proposal)["job_id"] == job_id
    with s.db.read() as uow:
        row = uow.repository("signoffs", business_id=s.business).get(proposal["id"])
        assert row["policy_binding_json"]["job_id"] == job_id
    assert s.decide(proposal).status == "accepted"
    if definition_changed:
        proof["definition_digest"] = "b" * 64
    restarted = SignoffService(s.app, file_applier=s.file_applier)
    restarted.run_pending()
    assert s.detail(proposal)["state"] == ("conflicted" if definition_changed else "applied")
    assert s.count("tasks") == (not definition_changed)


def test_application_claim_prevents_two_service_instances(scenario):
    s = scenario
    proposal = s.propose()
    assert s.decide(proposal).status == "accepted"
    other = SignoffService(s.app, file_applier=s.file_applier)
    with application_claim(s.db, proposal["id"]):
        assert other.run_one(proposal["id"]) is None
        assert s.count("tasks") == 0
    other.run_one(proposal["id"])
    assert s.count("tasks") == 1 and s.detail(proposal)["state"] == "applied"


def test_changed_raw_file_conflicts_at_approval(scenario):
    s = scenario
    document = s.document()
    staged = s.app.invoke(s.agent_context(), "context_document_update",
        {"document_ids": [document["id"]], "text": "After\n"}, {"idempotency_key": new_id()})
    proposal = dict(staged.signoff)
    (s.root / document["path"]).write_text("Later raw edit\n")
    assert s.decide(proposal).error["code"] == "stale_signoff"
    assert s.detail(proposal)["state"] == "conflicted"
    assert s.count("apply_receipts") == 0


@pytest.mark.parametrize("recover", [False, True])
def test_allowed_file_crash_retry_recovers_without_repreparing(scenario, recover):
    s = scenario
    document = s.document()
    args = {"document_ids": [document["id"]], "text": "Allowed exact after\n"}
    options = {"idempotency_key": new_id()}
    def crash(_resource):
        raise SimulatedCrash()
    s.file_applier.after_replace = crash
    with pytest.raises(SimulatedCrash):
        s.app.invoke(s.owner_context, "context_document_update", args, options)
    assert s.count("signoffs") == 0 and s.count("resource_claims") == 1
    s.file_applier.after_replace = None
    if recover:
        results = s.app.file_executor.recover_pending(s.app)
        assert len(results) == 1 and results[0].status == "ok", [x.to_dict() for x in results]
    else:
        result = s.app.invoke(s.owner_context, "context_document_update", args, options)
        assert result.status == "ok", result.to_dict()
    with s.db.read() as uow:
        row = uow.repository("documents", business_id=s.business).get(document["id"])
        assert row["revision"] == 2
    assert s.count("resource_claims") == 0
    replay = s.app.invoke(s.owner_context, "context_document_update", args, options)
    assert replay.status == "ok" and s.preparations == 1
    changed = s.app.invoke(s.owner_context, "context_document_update", {**args, "text": "Different"}, options)
    assert changed.error["code"] == "idempotency_conflict"


def test_review_immutable_hash_and_database_payload_guards(scenario):
    s = scenario
    proposal = s.propose()
    incorrect = {**proposal, "payload_hash": "f" * 64}
    assert s.decide(incorrect).error["code"] == "stale_signoff"
    assert s.detail(proposal)["state"] == "pending"
    with pytest.raises(DomainError):
        with s.db.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET change_set = ? WHERE id = ?", (b"{}", proposal["id"]))


@pytest.mark.parametrize("retryable,changed_authority", [(True, False), (False, False), (True, True)])
def test_startup_recovers_retryable_failed_files_with_claims(scenario, retryable, changed_authority):
    s = scenario
    s.change_override("context_document_update", "allow")
    document = s.document()
    args = {"document_ids": [document["id"]], "text": "Saved before finalization failed\n"}
    options = {"idempotency_key": new_id()}

    def interrupted(_):
        raise DomainError("busy" if retryable else "policy_denied", "Injected interruption", retryable=retryable)

    s.file_applier.after_replace = interrupted
    result = s.app.invoke(s.agent_context(), "context_document_update", args, options)
    assert result.status == "error"
    with s.db.read() as uow:
        receipt = uow.connection.execute("SELECT state FROM apply_receipts").fetchone()
        assert receipt[0] == "failed"
    assert s.count("resource_claims") == 1
    assert (s.root / document["path"]).read_text() == args["text"]
    s.file_applier.after_replace = None
    if changed_authority:
        s.change_override("context_document_update", "deny")
    results = s.app.file_executor.recover_pending(s.app)
    if not retryable:
        assert results == []
    elif changed_authority:
        assert len(results) == 1 and results[0].status == "error"
    else:
        assert len(results) == 1 and results[0].status == "ok", [r.to_dict() for r in results]
        assert s.count("resource_claims") == 0
        assert s.app.invoke(s.agent_context(), "context_document_update", args, options).status == "ok"
        assert s.app.file_executor.recover_pending(s.app) == []
    with s.db.read() as uow:
        row = uow.repository("documents", business_id=s.business).get(document["id"])
        assert row["revision"] == (2 if retryable and not changed_authority else 1)
    if changed_authority or not retryable:
        assert s.count("resource_claims") == 1
