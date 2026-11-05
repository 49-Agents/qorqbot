"""Packaged Overseer guidance names only executable interfaces and real transitions.

These checks validate the public interface vocabulary and the pre-merge-only
re-plan boundary, not exact prose. No model, host or provider is invoked.
"""
from pathlib import Path
import re

import yaml

from qorqut_agentic.cli import OPERATIONS

PACKAGE = Path(__file__).resolve().parents[1] / "src/qorqut_agentic/resources/defaults/overseer/overseer"

# The host MCP surface implemented for Overseer by the accepted host operations
# contract (A013/A018). The portable module cannot import host code, so the
# guidance is checked against this explicit public vocabulary.
HOST_OPERATIONS = frozenset({
    "overseer_evidence_list", "overseer_episode_get", "overseer_proposals_list",
    "overseer_outside_delta_record", "overseer_outside_delta_get", "overseer_outside_deltas_list",
    "overseer_delta_get", "overseer_deltas_list", "overseer_application_get",
    "overseer_application_recovery_status", "overseer_owner_request_get",
    "overseer_owner_requests_list", "overseer_due_list", "overseer_due_get",
    "overseer_extract_request", "overseer_review_start", "overseer_application_start",
    "overseer_input_request", "overseer_invocation_request", "overseer_input_reconcile",
    "overseer_development_task_create", "overseer_development_task_link",
    "overseer_development_tasks_reconcile", "overseer_application_prepare",
    "overseer_application_prepare_merge", "overseer_application_publish",
    "overseer_application_recover", "overseer_application_record_checks",
    "overseer_application_replan", "overseer_application_inspect_satisfaction",
})


def resources():
    return {str(path.relative_to(PACKAGE)): path.read_text()
            for path in sorted(PACKAGE.rglob("*")) if path.is_file()}


def referenced_host_operations():
    used = set()
    for text in resources().values():
        used |= set(re.findall(r"\boverseer_[a-z_]+\b", text))
    return used


def referenced_module_operations():
    used = set()
    for text in resources().values():
        used |= set(re.findall(r"\boverseer\.[a-z_]+\.[a-z_]+\b", text))
    return used


def application_steps():
    data = yaml.safe_load(resources()["workflows/apply.yaml"])
    return data["steps"], {step["id"]: step.get("instructions", "") for step in data["steps"]}


def test_guidance_names_only_supported_portable_operations():
    used = referenced_module_operations()
    assert used, "packaged guidance should name its portable operations"
    assert used <= set(OPERATIONS), sorted(used - set(OPERATIONS))
    # The plan and queue reads that the module really exposes stay in guidance.
    assert {"overseer.applications.list", "overseer.applications.save_plan"} <= used


def test_guidance_names_only_registered_host_operations():
    used = referenced_host_operations()
    assert used, "packaged guidance should name its host operations"
    assert used <= HOST_OPERATIONS, sorted(used - HOST_OPERATIONS)


def test_extraction_guidance_uses_focused_episode_pages_and_exact_coverage():
    text = resources()["instructions/extract.md"]
    for required in ("related_episodes", "cursor_reset: true", "evidence_busy",
                     "same cursor", "same run and", "fragments have been read and analyzed",
                     "Unopened related episodes"):
        assert required in text
    assert "read whole related authorized episodes" not in text


def test_application_uses_the_actual_executor_sequence():
    steps, instructions = application_steps()
    assert [step["id"] for step in steps] == ["select-and-inspect", "agree-plan", "worktree", "merge", "checks"]
    assert "overseer_application_prepare" in instructions["worktree"]
    merge = instructions["merge"]
    assert merge.index("overseer_application_prepare_merge") < merge.index("overseer_application_publish")
    assert "overseer_application_record_checks" in instructions["checks"]
    for text in resources().values():
        for invented in ("overseer.applications.prepare", "overseer.applications.publish"):
            assert invented not in text


def test_replan_guidance_stays_a_pre_merge_transition():
    qualifiers = ("pre-merge", "before merge", "working draft")
    for paragraph in re.split(r"\n\s*\n", resources()["instructions/apply.md"]):
        if "replan" in paragraph.lower():
            assert any(qualifier in paragraph.lower() for qualifier in qualifiers), paragraph
    steps, _ = application_steps()
    for step in steps:
        text = step.get("instructions", "").lower()
        if "replan" in text:
            assert any(qualifier in text for qualifier in qualifiers), step["id"]
    # Post-merge outcomes keep their merge facts and use owner disposition;
    # there is no post-merge re-plan and no automatic rollback.
    checks = next(step["instructions"] for step in steps if step["id"] == "checks").lower()
    assert "signoff" in checks and "rollback" in checks
    assert "retain the merge" in checks
