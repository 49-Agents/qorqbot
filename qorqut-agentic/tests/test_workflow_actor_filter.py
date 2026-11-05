"""The additive creator filter never paginates or counts foreign histories."""
import pytest

from qorqut_agentic import AgenticError
from qorqut_agentic.cli import dispatch

REF = "example-project-workflow/landing-page"
ACTOR = {"actor_id": "current", "actor_kind": "agent"}
FOREIGN = {"actor_id": "foreign", "actor_kind": "agent"}


def test_optional_creator_filter_precedes_pagination(full_layer):
    workflow = full_layer.workflows
    workflow.start(REF, FOREIGN, operation_id="foreign-first")
    first = workflow.start(REF, ACTOR, operation_id="own-first")["run"]
    workflow.start(REF, FOREIGN, operation_id="foreign-second")
    second = workflow.start(REF, ACTOR, operation_id="own-second")["run"]
    all_runs = workflow.list()
    assert all_runs["total"] == 4
    own = workflow.list(actor=ACTOR, limit=1, offset=1)
    expected = [run for run in all_runs["runs"] if run["actor"] == ACTOR]
    assert own == {"runs": expected[1:2], "total": 2}
    assert {run["run_id"] for run in expected} == {first["run_id"], second["run_id"]}
    assert workflow.list(actor={"actor_kind": "agent", "actor_id": "current"}, offset=2) == {"runs": [], "total": 2}
    assert workflow.list(actor=ACTOR, state="completed") == {"runs": [], "total": 0}
    assert dispatch(full_layer, {"operation": "workflows.list", "arguments": {"actor": ACTOR}})["total"] == 2


@pytest.mark.parametrize("actor", [{}, "current", {"actor_id": "current", "actor_kind": "agent", "extra": True}])
def test_filter_rejects_malformed_actor(full_layer, actor):
    with pytest.raises(AgenticError):
        full_layer.workflows.list(actor=actor)


def test_public_run_selection_is_a_defensive_snapshot(full_layer):
    selection = {"package_ids": ["example-project-workflow"], "project_id": "original", "work_type": "review"}
    result = full_layer.workflows.start(REF, ACTOR, selection=selection, operation_id="saved-context")
    run_id = result["run"]["run_id"]
    result["run"]["selection"]["project_id"] = "mutated-result"
    selection["package_ids"].clear()
    saved = full_layer.workflows.get(run_id)["run"]["selection"]
    assert saved == {"package_ids": ["example-project-workflow"], "project_id": "original", "work_type": "review"}
    full_layer.workflows.list(actor=ACTOR)["runs"][0]["selection"]["package_ids"].clear()
    assert full_layer.workflows.get(run_id)["run"]["selection"] == saved
