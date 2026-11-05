"""Real catalog and runner with synthetic, executor-declared domain results.

These tests do not execute host/domain operations or assess model judgment.
"""
from pathlib import Path

import pytest

from qorqut_agentic.workflows import Workflows
from tests.conftest import ATTR, make_layer, reopen, sha

PACKAGE = Path(__file__).resolve().parents[1] / "src/qorqut_agentic/resources/defaults/overseer/overseer"
ACTOR = {"actor_id": "synthetic-overseer", "actor_kind": "agent"}
ENTRIES = {"extract", "review", "apply", "extract-instructions", "review-instructions",
           "apply-instructions", "record-direct-instruction"}


def prepared(tmp_path):
    layer = make_layer(tmp_path)
    layer.authoring.import_package(ATTR, "overseer", {"directory": str(PACKAGE)})
    return layer, Workflows(layer._ctx, layer.tools)


def test_catalog_imports_exact_supported_entries(tmp_path):
    layer, _ = prepared(tmp_path)
    report = layer.catalog.validate()
    assert report["valid"], report["issues"]
    entries = layer.catalog.list(package_ids=["overseer"])["entries"]
    assert {entry["ref"].split("/")[1] for entry in entries} == ENTRIES
    assert {entry["ref"].split("/")[1] for entry in entries if entry["kind"] == "workflow"} == {"extract", "review", "apply"}
    for workflow in ("extract", "review", "apply"):
        entry = layer.catalog.read_entry(f"overseer/{workflow}", include_dependency_bodies=True)
        assert any(item["entry"]["ref"] == f"overseer/{workflow}-instructions" for item in entry["dependencies"])


@pytest.mark.parametrize("workflow", ["extract", "review", "apply"])
def test_instruction_and_definition_snapshot_are_frozen(tmp_path, workflow):
    layer, runner = prepared(tmp_path)
    before = runner.start(f"overseer/{workflow}", ACTOR)["run"]
    path = f"plugins/overseer/instructions/{workflow}.md"
    original = (layer.root / path).read_bytes()
    changed = original.decode() + "\nSynthetic later revision.\n"
    layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original), "content": changed}])
    assert runner.get(before["run_id"])["run"] == before
    after = runner.start(f"overseer/{workflow}", ACTOR)["run"]
    assert after["steps"][0]["instruction_revision"] != before["steps"][0]["instruction_revision"]
    assert after["steps"][0]["instructions"] == changed
    assert before["steps"][0]["instructions"] == original.decode()


# Executor-supplied fixtures exercise persistence only. They are not model predictions
# and do not assert that the baseline runner itself enforces domain policy.
SCENARIOS = {
    "extract": {
        "resolve-frontier": {"frontier": "synthetic-frontier", "complete": False},
        "gather-episodes": {"events": ["original", "mirror-same-event"], "gap": "forbidden-edit-history"},
        "inspect-and-record": {"proposals": [{"scope": "project-A", "origin": "inferred_preference"},
                                                {"scope": "research", "origin": "direct_instruction"}],
                               "conflict": {"signoff_ref": "synthetic-input", "pending": True},
                               "unrelated_continued": True},
        "coverage": {"analyzed": ["original"], "remaining": ["forbidden-edit-history"]},
    },
    "review": {
        "load-history": {"prior_rejected_delta": "synthetic-rejection"},
        "group-and-rank": {"attached_to_rejected": "synthetic-rejection", "reopened": False,
                           "new_later_delta": "synthetic-new-delta", "independent_events": 1},
        "freeze-batch": {"items": ["synthetic-new-delta"], "frozen": True},
        "signoffs": {"canonical_ref": "synthetic-input", "choice": "later", "application_started": False},
    },
    "apply": {
        "select-and-inspect": {"accepted_revision": 1, "owner_invocation": "synthetic-invocation"},
        "agree-plan": {"plan_revision": 1, "signoff_ref": "synthetic-plan-input"},
        "worktree": {"worktree": "synthetic-only", "external_task": "synthetic-task"},
        "merge": {"merged_commit": "synthetic-commit"},
        "checks": {"state": "failed", "merge_retained": True, "signoff_ref": "synthetic-failure-input"},
    },
}


@pytest.mark.parametrize("workflow", ["extract", "review", "apply"])
def test_runner_persists_synthetic_domain_results_and_dependencies(tmp_path, workflow):
    layer, runner = prepared(tmp_path)
    run = runner.start(f"overseer/{workflow}", ACTOR)["run"]
    rid = run["run_id"]
    expected = SCENARIOS[workflow]
    for step_id, result in expected.items():
        view = runner.get(rid)["run"]
        assert [step["step_id"] for step in view["ready_steps"]] == [step_id]
        runner.start_step(rid, step_id, ACTOR, f"start-{step_id}")
        completed = runner.complete_step(rid, step_id, ACTOR, f"finish-{step_id}",
                                         "synthetic domain result", result)["run"]
        step = next(item for item in completed["steps"] if item["id"] == step_id)
        assert step["verification"] == "declared" and step["output"] == result
    restored = reopen(layer)
    saved = Workflows(restored._ctx, restored.tools).get(rid)["run"]
    assert saved["state"] == "completed"
    assert {step["id"]: step["output"] for step in saved["steps"]} == expected
    assert all(step["verification"] == "declared" for step in saved["steps"])


def test_optional_arguments_are_snapshotted_and_do_not_prompt(tmp_path):
    _, runner = prepared(tmp_path)
    selection = {"mode": "explicit", "namespaces": [{"kind": "global", "business_id": None}],
                 "agent_ids": [], "task_ids": [], "work_types": [], "workflow_refs": []}
    extract = runner.start("overseer/extract", ACTOR,
                           {"selection": {"value": selection, "source": "synthetic-host"}})["run"]
    assert extract["arguments"]["selection"]["value"] == selection
    assert not extract["argument_requests"]
    apply = runner.start("overseer/apply", ACTOR,
                         {"application_id": {"value": "synthetic-application", "source": "synthetic-host"}})["run"]
    assert apply["arguments"]["application_id"]["value"] == "synthetic-application"
    assert not apply["argument_requests"]
    for workflow, arg in (("extract", "selection"), ("apply", "application_id")):
        run = runner.start(f"overseer/{workflow}", ACTOR)["run"]
        assert run["arguments"][arg]["value"] is None and not run["argument_requests"]
