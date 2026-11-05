"""Project archive and restore: the owner's Signoff, the exact target fingerprint, terminal reset before the records, restore.

Real: ``Application``, the Signoff service with its durable approval and application, the policy evaluator carrying the
feature's own ``POLICY_RULES`` and the archive operations. Faked, and explicit: the terminal-reset callable (here a recorder
that can fail or inspect), the agent registry and the synthetic principals. Terminals, sessions and providers do not exist
here; a reset is only a call with the project row and its registered agent rows."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from qorqut_operations import signoffs
from qorqut_operations.application import Application
from qorqut_operations.application.registry import CatalogEntry, OperationRegistry
from qorqut_operations.contracts import (ArtifactRef, DomainError, InvocationContext, Principal, ResourceRef, canonical_hash,
                                         new_id, plain)
from qorqut_operations.policy import PolicyResolver
from qorqut_operations.policy.resolver import AgentPolicyBasis
from qorqut_operations.policy.rules import resolve_operation_policy
from qorqut_operations.projects import archive as module
from qorqut_operations.projects.archive import POLICY_RULES, BusinessArchive, archive_approval_rule
from qorqut_operations.signoffs import ABSENT, SignoffPorts, SignoffService
from qorqut_operations.storage import LockPaths, StorageContribution
from qorqut_operations.storage.admission import RecoveryAdmission
from qorqut_operations.storage.repositories import decode_row
from signoff_support import (COORDINATOR, HOST_SCHEMA, HOST_TABLES, FakeAgents, FakeExecution, FakePeers, FakeReferences,
                             FakeTasks)
from support import AGENT, BUSINESS, HUMAN, OTHER, WORKER, Demo, database_at

STRANGER = "00000000-0000-4000-8000-0000000000c3"
HOME_ONE = "00000000-0000-4000-8000-0000000000a4"
HOME_TWO = "00000000-0000-4000-8000-0000000000a5"
INSTALLATION = "11111111-1111-4111-8111-111111111111"
RATIONALE = "The project is finished"
SIGNOFF_READS = ("signoffs_get", "signoffs_list", "signoffs_notes_list", "signoffs_comment", "signoffs_input_create",
                 "signoffs_input_cancel", "signoffs_attention_set", "signoffs_reject", "signoffs_input_answer_delegated")
ORDUBASHY = ArtifactRef("ordubashy", "1", "0" * 64)
ENTRIES = (CatalogEntry("business_archive", True, "records"),
           CatalogEntry("business_restore", False, "records", frozenset({"dashboard"}), True),
           CatalogEntry("business_archived_list", False, "read", frozenset({"dashboard"}), True))


class Authority(FakeAgents):
    """Role ids by principal: the coordinator is Ordubashy, everyone else a plain global worker. As the host's agent registry
    does, it also stamps the caller's role artifact onto the refreshed invocation context."""

    def refresh_invocation_context(self, ctx, uow):
        refreshed = super().refresh_invocation_context(ctx, uow)
        role = "ordubashy" if ctx.principal_id == COORDINATOR else "global-worker"
        return replace(refreshed, role_artifact_ref=ArtifactRef(role, "1", "0" * 64))

    def policy_basis(self, ctx, uow):
        role = "ordubashy" if ctx.principal_id == COORDINATOR else "global-worker"
        return AgentPolicyBasis(self.defaults, self.overrides, {"id": role, "version": "1"}, self.revision)


class Terminals:
    """The host's terminal reset, replaced by a recorder; it can inspect the stored state or fail."""

    def __init__(self, host):
        self.host, self.calls, self.seen, self.fail = host, [], [], None

    def __call__(self, business, agents):
        self.calls.append((dict(business), [dict(agent) for agent in agents]))
        self.seen.append(self.host.row(business["id"])["archived_at"])           # what a reader sees while the reset runs
        if self.fail:
            raise self.fail


class ArchiveHost:
    def __init__(self, tmp_path):
        self.tmp_path = Path(tmp_path)
        self.now = "2026-10-12T11:59:00.000000Z"
        self.agents, self.terminals = Authority(), Terminals(self)
        self.database = database_at(self.tmp_path, demo=Demo(), extra=(
            StorageContribution("host", HOST_TABLES, schema=HOST_SCHEMA), signoffs.storage_contribution()))
        self.locks = LockPaths.under(self.tmp_path / "locks")
        self.seed()
        ports = SignoffPorts(execution=FakeExecution(), peers=FakePeers(), tasks=FakeTasks(), references=FakeReferences(),
                             platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
        self.service = SignoffService(self.database, ports, locks=self.locks, clock=lambda: self.now)
        self.registry = OperationRegistry()
        self.policy = PolicyResolver(self.registry, self.agents, rules=POLICY_RULES)
        self.app = Application(self.database, self.registry, policy=self.policy, signoffs=self.service,
                               context_resolver=self.policy.refresh_context, writer_admission=RecoveryAdmission())
        self.archive = BusinessArchive(self.app, reset_terminals=self.terminals)
        contribution = self.service.contribution()
        for entry in (*ENTRIES, *contribution.entries):
            self.registry.declare(entry)
        for descriptor in contribution.descriptors:
            self.registry.register(descriptor)
        self.archive.register(self.registry)
        self.registry.freeze()
        self.service.bind(self.app)
        self.agents.defaults = {name: "allow" for name in (*SIGNOFF_READS, "business_archive")}

    def seed(self):
        with self.database.transaction() as uow:
            principals = uow.repository("principals")
            for identity, kind, name in ((COORDINATOR, "agent", "Ordubashy"), (AGENT, "agent", "Worker"), (STRANGER, "human", "Member"),
                                         (HOME_ONE, "agent", "Home one"), (HOME_TWO, "agent", "Home two"), (WORKER, "system", "Program")):
                principals.insert({"id": identity, "kind": kind, "display_name": name})
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": HUMAN})
            plain_scope = {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []}
            profiles = {COORDINATOR: (None, {"status": "active", "role": {"id": "ordubashy"}, "runtime": "codex", "scope": {
                            "kind": "ecosystem", "allowed_business_ids": [BUSINESS, OTHER], "global_capabilities": ["read_global_context"]}}),
                        AGENT: (None, {"status": "active", "role": {"id": "global-worker"}, "scope": plain_scope}),
                        HOME_TWO: (BUSINESS, {"role": {"id": "business-writer"}, "scope": {"kind": "business", "business_id": BUSINESS}}),
                        HOME_ONE: (BUSINESS, {"role": {"id": "business-agent"}, "scope": {"kind": "business", "business_id": BUSINESS}})}
            for identity, (home, profile) in profiles.items():
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                    (identity, identity, home, home or "ecosystem", "agent-" + identity[-2:], "/nonexistent/" + identity, json.dumps(profile)))
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": STRANGER})
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": HOME_ONE})

    # -- callers ---------------------------------------------------------------------------------------------------

    @staticmethod
    def coordinator(business=None, **kwargs):
        return InvocationContext(Principal(COORDINATOR, "agent"), business_id=business, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}), profile_revision=1, source="mcp", **kwargs)

    @staticmethod
    def owner(business=None):
        return InvocationContext(Principal(HUMAN, "human"), business_id=business, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                                 global_capabilities=frozenset({"read_global_context"}), source="dashboard")

    # -- state -----------------------------------------------------------------------------------------------------

    def query(self, sql, *parameters):
        with self.database.read() as uow:
            return [decode_row(row) for row in uow.connection.execute(sql, parameters)]

    def row(self, business=BUSINESS):
        return self.query("SELECT * FROM businesses WHERE id=?", business)[0]

    def propose(self, business=BUSINESS, ctx=None, revision=None, rationale=RATIONALE):
        revision = self.row(business)["revision"] if revision is None else revision
        return self.app.invoke(ctx or self.coordinator(), "business_archive", {"business_id": business, "rationale": rationale},
                               {"expected_revision": revision, "idempotency_key": new_id()})

    def staged(self, **kwargs):
        result = self.propose(**kwargs)
        assert result.status == "signoff_required", (result.status, result.error)
        return result.signoff["id"]

    def review(self, signoff_id):
        row = self.query("SELECT * FROM signoffs WHERE id=?", signoff_id)[0]
        return self.service.review(self.owner(), signoff_id, decision="approve", proposal_revision=row["proposal_revision"],
                                   payload_hash=row["payload_hash"], expected_status_revision=row["status_revision"],
                                   idempotency_key=new_id(), feedback=None)

    def approve(self, signoff_id):
        accepted = self.review(signoff_id)
        assert accepted.status == "accepted", accepted
        return signoff_id

    def receipt(self):
        return self.query("SELECT state, error_json FROM apply_receipts ORDER BY created_at DESC")[0]

    def apply_approved(self, signoff_id):
        self.approve(signoff_id)
        return self.service.run_one(signoff_id)

    def restore(self, business=BUSINESS, ctx=None, revision=None):
        revision = self.row(business)["revision"] if revision is None else revision
        return self.app.invoke(ctx or self.owner(), "business_restore", {"business_id": business},
                               {"expected_revision": revision, "idempotency_key": new_id()})


@pytest.fixture
def host(tmp_path):
    return ArchiveHost(tmp_path)


def agents_of(host, business=BUSINESS):
    return [{"agent_id": agent, "name": "agent-" + agent[-2:], "workspace": "/nonexistent/" + agent, "revision": 1,
             "home_business_id": business} for agent in sorted((HOME_ONE, HOME_TWO))]


# -- the Signoff floor ----------------------------------------------------------------------------------------------------------------

def test_the_proposal_is_always_a_signoff_whatever_the_grant_and_stages_nothing_else(host):
    for override in ({}, {"business_archive": "allow"}, {"business_archive": "signoff"}):
        host.agents.overrides = override
        result = host.propose()
        assert result.status == "signoff_required", override
        assert host.row()["archived_at"] is None and host.terminals.calls == []        # nothing happens before the approval
    host.agents.overrides = {"business_archive": "deny"}
    assert host.propose().error["code"] == "policy_denied"
    host.agents.overrides = {}
    host.agents.defaults.pop("business_archive")                                      # no grant at all
    assert host.propose().error["code"] == "policy_denied"


@pytest.mark.parametrize("role,defaults,overrides,expected", [
    ("ordubashy", {}, {}, ("deny", "business_archive_owner_approval")),
    ("ordubashy", {"business_archive": "allow"}, {}, ("signoff", "business_archive_owner_approval")),
    ("ordubashy", {"business_archive": "signoff"}, {}, ("signoff", "business_archive_owner_approval")),
    ("ordubashy", {}, {"business_archive": "allow"}, ("signoff", "business_archive_owner_approval")),
    ("ordubashy", {"business_archive": "allow"}, {"business_archive": "deny"}, ("deny", "business_archive_owner_approval")),
    ("ordubashy", {"business_archive": "deny"}, {"business_archive": "allow"}, ("signoff", "business_archive_owner_approval")),
    ("global-worker", {"business_archive": "allow"}, {"business_archive": "allow"}, ("deny", "business_archive_owner_approval")),
    (None, {"business_archive": "allow"}, {"business_archive": "allow"}, ("deny", "business_archive_owner_approval")),
])
def test_the_policy_rule_decides_before_any_override_and_only_for_this_operation(host, role, defaults, overrides, expected):
    decision = resolve_operation_policy("business_archive", defaults, overrides, host.registry, rules=POLICY_RULES, role_id=role)
    assert (decision.mode, decision.provenance) == expected
    assert POLICY_RULES.before_override == (archive_approval_rule,) and not POLICY_RULES.after_override and not POLICY_RULES.after_default
    assert archive_approval_rule("business_restore", {}, {}, host.registry, role_id=role, scope="global", resolve=None) is None
    assert archive_approval_rule("tables_archive", {"tables_archive": "allow"}, {}, host.registry, role_id=role, scope="global",
                                 resolve=None) is None


# -- approval, the terminal reset and the records ----------------------------------------------------------------------------------------

def test_an_approved_archive_resets_terminals_once_with_the_same_rows_before_the_records_change(host):
    before = host.row()
    signoff = host.staged()
    assert host.terminals.calls == []
    applied = host.apply_approved(signoff)
    assert applied["state"] == "applied", applied
    [(business, agents)] = host.terminals.calls                                       # once
    assert business["id"] == BUSINESS and business["slug"] == before["slug"] and business["archived_at"] is None
    assert agents == agents_of(host)                                                  # the registered agent rows, in agent_id order
    assert host.terminals.seen == [None]                                              # the reset ran before the archive was visible
    after = host.row()
    assert after["archived_at"] is not None and after["archived_by"] == COORDINATOR and after["revision"] == before["revision"] + 1
    assert host.receipt()["state"] == "applied"
    assert len(host.terminals.calls) == 1


def test_a_terminal_reset_failure_leaves_the_project_active_and_changes_no_record(host):
    before = host.row()
    signoff = host.staged()
    host.terminals.fail = RuntimeError("terminal service unavailable")
    host.approve(signoff)
    assert host.service.run_one(signoff) is None
    [(business, _)] = host.terminals.calls                                            # the reset ran once, with the pre-archive row
    assert business["archived_at"] is None and host.terminals.seen == [None]
    assert host.row() == before                                                       # no record changed: the reset precedes the records
    assert host.query("SELECT state FROM signoffs WHERE id=?", signoff) == [{"state": "failed"}]
    assert host.receipt()["state"] == "failed" and host.receipt()["error_json"]["code"] == "operation_failed"
    host.terminals.fail = None
    assert host.service.run_one(signoff) is None and len(host.terminals.calls) == 1   # a failed application is not silently retried
    assert host.row() == before


def test_without_the_late_bound_terminal_service_an_approved_archive_is_unavailable(host):
    assert BusinessArchive(host.app).reset_terminals is None                          # optional at construction
    before = host.row()
    host.archive.reset_terminals = None
    signoff = host.staged()
    host.approve(signoff)
    assert host.service.run_one(signoff) is None
    receipt = host.receipt()
    assert receipt["state"] == "failed" and receipt["error_json"]["code"] == "operation_unavailable"
    assert host.row() == before and host.terminals.calls == []                        # nothing reset, nothing archived
    calls = []                                                                        # the host's late assignment, after proposal
    host.archive.reset_terminals = lambda business, agents: calls.append((business["id"], len(agents)))
    second = host.staged()
    assert host.apply_approved(second)["state"] == "applied"
    assert calls == [(BUSINESS, 2)] and host.row()["archived_at"] is not None


def test_the_unused_resolver_field_is_gone(host):
    assert not hasattr(host.archive, "resolver")
    with pytest.raises(TypeError):
        BusinessArchive(host.app, resolver=object())


# -- the exact target fingerprint --------------------------------------------------------------------------------------------------------

def test_the_agent_fingerprint_is_the_canonical_hash_of_the_registered_rows(host):
    signoff = host.staged()
    [stored] = host.query("SELECT change_set FROM signoffs WHERE id=?", signoff)
    plan = json.loads(stored["change_set"])
    targets = {item["resource"]["resource_type"]: item for item in plan["expected_targets"]}
    expected = canonical_hash(agents_of(host))
    assert targets["business_archive_agents"]["content_hash"] == expected
    with host.database.read() as uow:
        assert host.archive.snapshot(None, ResourceRef("business_archive_agents", BUSINESS, None, "global"), uow) == {"content_hash": expected}
    assert any(change["action"] == "policy_effect" and "terminal sessions" in json.dumps(change) for change in plan["changes"])


def test_a_registered_agent_that_changed_after_preparation_refuses_the_approval_without_a_reset(host):
    before = host.row()
    signoff = host.staged()
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET name='renamed' WHERE agent_id=?", (HOME_ONE,))
    refused = host.review(signoff)
    assert refused.status == "error" and refused.error["code"] == "stale_signoff"
    assert host.terminals.calls == [] and host.row() == before


def test_a_project_revision_that_changed_after_preparation_is_not_archived(host):
    before = host.row()
    signoff = host.staged()
    with host.database.transaction() as uow:
        uow.repository("businesses").update(BUSINESS, {"name": "Renamed"}, expected_revision=before["revision"])
    refused = host.review(signoff)
    assert refused.status == "error" and refused.error["code"] == "stale_signoff"
    assert host.terminals.calls == [] and host.row()["archived_at"] is None


# -- who may propose and for which project -----------------------------------------------------------------------------------------------

def test_only_the_owner_or_scoped_ordubashy_in_global_may_propose_and_the_project_must_be_owned(host):
    from types import SimpleNamespace
    with host.database.read() as uow:
        def refuses(ctx, plan=None):
            with pytest.raises(DomainError) as error:
                host.archive.guard(ctx, plan, uow)
            return error.value
        assert "reviewed in Global" in refuses(host.coordinator(BUSINESS)).message            # a selected project
        assert "reviewed in Global" in refuses(host.owner(BUSINESS)).message
        assert "reviewed in Global" in refuses(InvocationContext(Principal(COORDINATOR, "agent"), home_business_id=BUSINESS,
                                                                allowed_business_ids=frozenset({BUSINESS}), source="mcp")).message
        stranger = InvocationContext(Principal(STRANGER, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
        assert "installation owner or Ordubashy" in refuses(stranger).message
        worker = InvocationContext(Principal(AGENT, "agent"), allowed_business_ids=frozenset({BUSINESS}), profile_revision=1, source="mcp")
        assert "installation owner or Ordubashy" in refuses(worker).message
        ordubashy = InvocationContext(Principal(COORDINATOR, "agent"), source="mcp", role_artifact_ref=ORDUBASHY,
                                      allowed_business_ids=frozenset({BUSINESS}))
        host.archive.guard(host.owner(), None, uow)                                               # the owner and Ordubashy pass
        host.archive.guard(ordubashy, None, uow)
        host.archive.guard(ordubashy, SimpleNamespace(arguments={"business_id": BUSINESS}), uow)
        host.archive.guard(host.owner(), SimpleNamespace(arguments={"business_id": OTHER}), uow)  # the owner is not limited to a scope
        assert "outside Ordubashy's current scope" in refuses(ordubashy, SimpleNamespace(arguments={"business_id": OTHER})).message
    with host.database.transaction() as uow:                                                      # the owner membership must be current
        uow.connection.execute("UPDATE business_memberships SET is_owner=0 WHERE business_id=? AND principal_id=?", (OTHER, HUMAN))
    with host.database.read() as uow:
        for ctx in (host.owner(), ordubashy):
            with pytest.raises(DomainError) as error:
                host.archive.guard(ctx, SimpleNamespace(arguments={"business_id": OTHER}), uow)
            assert error.value.code == "forbidden" and "currently owned by the installation owner" in error.value.message
    assert host.propose(business=OTHER).error["code"] == "forbidden"
    assert host.propose(business=new_id(), revision=1).error["code"] == "not_found"
    assert host.propose(rationale="   ").error["code"] == "validation_error"
    assert host.row()["archived_at"] is None


def test_an_archived_project_cannot_be_proposed_again_and_a_stale_revision_conflicts(host):
    assert host.propose(revision=99).error["code"] == "revision_conflict"
    host.apply_approved(host.staged())
    assert host.propose().error["code"] == "business_archived"


# -- restore ----------------------------------------------------------------------------------------------------------------------------

def test_restore_returns_only_the_project_and_touches_no_agent_or_membership(host):
    host.apply_approved(host.staged())
    archived = host.row()
    profiles = host.query("SELECT * FROM agent_profiles ORDER BY agent_id")
    members = host.query("SELECT * FROM business_memberships ORDER BY business_id, principal_id")
    principals = host.query("SELECT * FROM principals ORDER BY id")
    listed = host.app.invoke(host.owner(), "business_archived_list", {}, {})
    assert [item["business_id"] for item in plain(listed.data)["items"]] == [BUSINESS]
    assert plain(listed.data)["items"][0]["archived_at"] == archived["archived_at"]
    result = host.restore()
    assert result.status == "ok", result.error
    assert plain(result.data) == {"business_id": BUSINESS, "restored": True}
    restored = host.row()
    assert restored["archived_at"] is None and restored["archived_by"] is None and restored["revision"] == archived["revision"] + 1
    assert host.query("SELECT * FROM agent_profiles ORDER BY agent_id") == profiles          # historical agents stay as they were
    assert host.query("SELECT * FROM business_memberships ORDER BY business_id, principal_id") == members
    assert host.query("SELECT * FROM principals ORDER BY id") == principals
    assert plain(host.app.invoke(host.owner(), "business_archived_list", {}, {}).data)["items"] == []
    assert len(host.terminals.calls) == 1                                                    # restore resets and starts nothing


def test_restore_is_owner_only_needs_the_current_revision_and_an_archived_owned_project(host):
    assert host.restore().error["code"] == "business_not_archived"
    host.apply_approved(host.staged())
    assert host.restore(ctx=host.coordinator()).status == "error"                            # an agent never restores
    stranger = InvocationContext(Principal(STRANGER, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
    assert host.restore(ctx=stranger).error["code"] in {"forbidden", "policy_denied"}
    assert host.restore(revision=1).error["code"] == "revision_conflict"
    assert host.restore(ctx=host.owner(BUSINESS)).status == "error"                          # restore is an owner action in Global
    assert host.row()["archived_at"] is not None
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?", (BUSINESS, HUMAN))
    assert host.restore().error["code"] == "forbidden"
    assert plain(host.app.invoke(host.owner(), "business_archived_list", {}, {}).data)["items"] == []   # only owner-held projects list


def test_the_archive_method_the_transport_calls_is_the_restore_invocation(host):
    host.apply_approved(host.staged())
    result = host.archive.restore(host.owner(), {"business_id": BUSINESS},
                                  {"expected_revision": host.row()["revision"], "idempotency_key": new_id()})
    assert result.status == "ok" and host.row()["archived_at"] is None


def test_a_saved_restore_plan_that_carries_a_retired_roster_change_is_refused():
    from types import SimpleNamespace
    plan = SimpleNamespace(changes=[SimpleNamespace(action="restore_roster")])
    with pytest.raises(DomainError) as error:
        BusinessArchive.validate_restore(None, plan, None)
    assert error.value.code == "invalid_plan"
    BusinessArchive.validate_restore(None, SimpleNamespace(changes=[SimpleNamespace(action="update")]), None)


def test_the_operation_names_and_exposure_are_unchanged(host):
    assert (module.OPERATION, module.RESTORE, module.ARCHIVED) == ("business_archive", "business_restore", "business_archived_list")
    archive, restore, listing = (host.registry.get(name) for name in (module.OPERATION, module.RESTORE, module.ARCHIVED))
    assert archive.scope_kinds == restore.scope_kinds == listing.scope_kinds == frozenset({"global"})
    assert archive.global_capability == restore.global_capability == listing.global_capability == "read_global_context"
    assert restore.owner_only and listing.owner_only and not archive.owner_only
    assert restore.exposure == listing.exposure == frozenset({"dashboard"}) and listing.effect_kind == "read"
    assert archive.description.startswith("Ordubashy: propose archiving an owner project in Global. Always requires an urgent human Signoff")
