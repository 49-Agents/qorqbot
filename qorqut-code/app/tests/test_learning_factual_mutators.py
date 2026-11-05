"""The retained factual owner mutators still execute through the shared factual saved-plan guard.

Edit, Keep both and the Learning Applier's instructed decision are legitimate factual writes on Context candidates. Each
one must pass the same semantic guard that refuses procedural effects, produce its retained history record and leave the
standalone-era vocabulary untouched. The Learning Applier is the real 1.16.0 artifact from the workspace role sources,
published and registered through the real catalog and `agents_create`; production role validation is not weakened.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, utc_now
from host_foundation_support import (build_instance, create_registered_global, owner_global_context, publish_role,
                                     register_sources)

BUSINESS = "first"
EDIT_ID = "7a0c1e00-0000-4000-8000-000000000001"
CURRENT_ID = "7a0c1e00-0000-4000-8000-000000000002"
EARLIER_ID = "7a0c1e00-0000-4000-8000-000000000003"
DECIDE_ID = "7a0c1e00-0000-4000-8000-000000000004"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=(BUSINESS,))
    runtime = instance.runtime
    (tmp_path / "instance/operations/projects" / BUSINESS / "hq").mkdir(parents=True, exist_ok=True)
    from bazarlyk.background.integration import attach_background
    attach_background(runtime, runtime.programs, model_client_factory=lambda _business: None,
                      transcript_source=lambda business_id, agent_id=None: iter(()))
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [instance.business])
    runtime.freeze_registry()
    register_sources(instance, ids[BUSINESS], BUSINESS)   # packaged global instructions, as the host seeds them
    now = utc_now()

    def insert(identity, value, **columns):
        with instance.database.transaction() as uow:
            uow.repository("learning_candidates", business_id=ids[BUSINESS]).insert(
                {"id": identity, "candidate_json": value, "created_at": now, "updated_at": now, **columns})

    insert(EDIT_ID, {"text": "Original fact", "scope": "biz", "bucket": "context", "tag": "fact", "run": "run-1",
                     "evidence": {"session": "s-1"}})
    insert(CURRENT_ID, {"text": "Surviving fact", "scope": "biz", "bucket": "context", "tag": "fact", "run": "run-1",
                        "evidence": {"session": "s-1"},
                        "reconciliations": [{"kind": "duplicate", "reason": "same fact", "automatic": True,
                                             "earlier": {"id": EARLIER_ID}}]})
    insert(EARLIER_ID, {"text": "Earlier fact", "scope": "biz", "bucket": "context", "tag": "fact", "run": "run-1",
                        "evidence": {"session": "s-1"}, "reconciled_into": CURRENT_ID})
    insert(DECIDE_ID, {"text": "Instructed fact", "scope": "biz", "bucket": "context", "tag": "fact", "run": "run-1",
                       "evidence": {"session": "s-1"}})
    with instance.database.transaction() as uow:
        uow.repository("learning_runs", business_id=ids[BUSINESS]).insert(
            {"id": "run-1", "result_json": {"cores": {"s-1": {"markdown": "core"}}}, "created_at": now})
    create_registered_global(instance, "ordubashy")     # the role package that carries the mandatory foundation context
    yield SimpleNamespace(instance=instance, ids=ids, runtime=runtime, owner=instance.human(ids[BUSINESS]),
                          role=learning_applier(instance))
    runtime.release_instance()


# The reconciled Learning Applier 1.16.0 artifact (predecessor 1.15.2 plus the retired-grant removal), as recorded by the
# integration: its digest is over the manifest-selected files of the combined role source.
RECONCILED_VERSION = "1.16.0"
RECONCILED_DIGEST = "sha256:3bd75674ddc5b51fe633eddec7990c476312573e5b1ab6b1fb28d4afc9a3b2fd"
RETIRED_GRANTS = ("learnings_issue_action", "learnings_work_list", "learnings_work_get", "learnings_work_start",
                  "learnings_work_defer", "learnings_work_done", "learnings_work_drop", "learnings_work_note_set",
                  "learnings_engine_enrollment_set", "learnings_engine_import")
RETAINED_GRANTS = ("learnings_review_list", "learnings_review_get", "learnings_legacy_pending", "learnings_decide",
                   "agents_get", "tasks_get", "tasks_thread_get", "actions_thread_get")


def learning_applier(instance):
    """The real reconciled Learning Applier artifact, published and registered through the real catalog and roster.

    The role source copied into the fixture workspace is the combined checkout's own package. The published pin must equal
    an independent load of that source and the recorded reconciled digest; the created agent's stored profile must name
    that exact artifact, and its policy keeps every retained grant and none of the retired ones.
    """
    from bazarlyk.catalog.packages import load_package

    role = "learning-applier"
    source = Path(instance.workspace) / "qorqut-roles/agent-roles"
    package = load_package(source, f"roles/{role}/role.yaml", "role", instance.runtime.registry)
    assert (package.version, package.digest) == (RECONCILED_VERSION, RECONCILED_DIGEST)
    policy = package.manifest["default_tool_policy"]
    assert not [name for name in RETIRED_GRANTS if name in policy]
    assert all(policy.get(name) == "allow" for name in RETAINED_GRANTS), [n for n in RETAINED_GRANTS if n not in policy]
    pin = publish_role(instance, role)
    assert (pin["id"], pin["version"], pin["digest"]) == (role, package.version, package.digest)
    identity = create_registered_global(instance, role)
    with instance.database.read() as uow:
        profile = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
    stored = json.loads(profile[0])["role"]
    assert (stored["id"], stored["version"], stored["digest"]) == (role, RECONCILED_VERSION, RECONCILED_DIGEST), stored
    return identity


def invoke(host, ctx, name, arguments, *, revision=1, business=None):
    options = InvocationOptions(idempotency_key=new_id(), expected_revision=revision,
                                business_id=business)
    return host.runtime.application.invoke(ctx, name, arguments, options)


def rows(host, statement, parameters=()):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(statement, parameters)]


def test_owner_edit_passes_the_guard_and_appends_its_retained_history(host):
    result = invoke(host, host.owner, "learnings_edit", {"learning_id": EDIT_ID, "changes": {"text": "Owner corrected fact"}},
                    business=host.ids[BUSINESS])
    assert result.status == "ok", result.to_dict()
    assert result.data["item"]["text"] == "Owner corrected fact"
    assert result.data["item"]["decision"]["state"] == "open"
    candidate = rows(host, "SELECT candidate_json FROM learning_candidates WHERE id=?", (EDIT_ID,))[0]["candidate_json"]
    assert json.loads(candidate)["edits"][0]["set"] == {"text": "Owner corrected fact"}
    decision = rows(host, "SELECT changes_json FROM learning_decisions WHERE candidate_id=?", (EDIT_ID,))[0]
    assert json.loads(decision["changes_json"]) == {"text": "Owner corrected fact"}
    assert [row["event"] for row in rows(host, "SELECT event FROM learning_history WHERE candidate_id=?", (EDIT_ID,))] == [
        "learnings_edit"]


def test_keep_both_restores_a_factual_earlier_candidate_through_the_guard(host):
    result = invoke(host, host.owner, "learnings_keep_both", {"learning_id": CURRENT_ID}, business=host.ids[BUSINESS])
    assert result.status == "ok", result.to_dict()
    current = json.loads(rows(host, "SELECT candidate_json FROM learning_candidates WHERE id=?", (CURRENT_ID,))[0]["candidate_json"])
    assert current["reconciliations"][0]["kept_both"] is True
    earlier = json.loads(rows(host, "SELECT candidate_json FROM learning_candidates WHERE id=?", (EARLIER_ID,))[0]["candidate_json"])
    assert "reconciled_into" not in earlier and earlier["reconciliation_reviewed"] is True
    assert len(rows(host, "SELECT id FROM learning_history WHERE candidate_id IN (?,?)", (CURRENT_ID, EARLIER_ID))) == 2


def test_the_instructed_decision_passes_the_guard_and_keeps_its_attribution(host):
    agent = host.runtime.resolver.invocation_context(Principal(host.role, "agent"))
    assert agent.role_artifact_ref is not None and agent.role_artifact_ref.id == "learning-applier", agent.role_artifact_ref
    result = invoke(host, agent, "learnings_decide", {"learning_id": DECIDE_ID, "state": "accepted",
                                                     "owner_instruction": "The owner said yes."},
                    business=host.ids[BUSINESS])
    assert result.status == "ok", result.to_dict()
    assert result.data["item"]["state"] == "accepted"
    assert result.data["item"]["decision"]["state"] == "accepted"
    decision = rows(host, "SELECT state, decided_by FROM learning_decisions WHERE candidate_id=?", (DECIDE_ID,))[0]
    assert (decision["state"], decision["decided_by"]) == ("accepted", host.role)
    history = rows(host, "SELECT event, actor_id, after_json FROM learning_history WHERE candidate_id=?", (DECIDE_ID,))
    assert [(row["event"], row["actor_id"]) for row in history] == [("learnings_decide", host.role)]
    # The instruction is the owner's, reported by the applier: it is attributed in the retained audit, not relabelled.
    after = json.loads(history[0]["after_json"])
    assert after["decision"]["owner_instruction"] == "The owner said yes."