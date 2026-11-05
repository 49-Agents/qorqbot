"""Focused instruction checks for Ordubashy's Attention board guidance.

Reads the two core guidance texts this slice owns and the workspace role source only. No
database, host instance, provider, publication or adoption is involved; the role-source checks
skip when this workspace carries no ``qorqut-roles/agent-roles`` checkout.
"""

import os
from pathlib import Path

import pytest
import yaml

from bazarlyk.agents import role_workflow
from instance_support import workspace_root

# The frozen operations of the parallel implementation contract, Manager amendment 2 and the
# Tasks contract: board and membership, precise focus with atomic Intent, workflow episodes,
# the client's exact-run coordination pair, and the Worker candidate read.
FROZEN_OPERATIONS = ("attention_get", "attention_add", "attention_remove", "attention_focus_set",
                     "attention_focus_clear", "attention_workflow_update",
                     "attention_workflow_context", "attention_workflow_sync", "workers_list")


def flat(text):
    return " ".join(text.replace("`", "").split())


def shared_procedure():
    return flat((Path(role_workflow.__file__).parent / "skills/role/SKILL.md").read_text())


def imported_guidance():
    return flat(role_workflow.INSTRUCTIONS)


def role_source():
    root = Path(os.environ.get("QORQUT_TEST_ROLE_ROOT", workspace_root() / "qorqut-roles" / "agent-roles"))
    if not (root / "roles" / "ordubashy" / "role.yaml").is_file():
        pytest.skip("the test workspace has no qorqut-roles/agent-roles checkout")
    return root


def test_the_shared_procedure_scopes_the_generic_current_work_list_and_review_to_other_agents():
    text = shared_procedure()
    for phrase in (
        "Agents other than Ordubashy keep a session current-work list with agent_work_update",
        "Agents other than Ordubashy confirm or update this list and reconcile the state of tasks you touched",
        "For agents other than Ordubashy, make this review the last substantive operation of an ordered batch",
        # The generic procedure itself stays available to every other role.
        "A permanent worker keeps one current item, not a list",
        "Prefer patch actions add, replace or remove",
        "Give each worker one atomic assignment at a time and choose its next",
    ):
        assert phrase in text, phrase


def test_the_shared_procedure_moves_ordubashy_to_board_bookkeeping_and_task_state():
    text = shared_procedure()
    for phrase in (
        "Ordubashy keeps his commitments on his own Attention board, not in the session current-work list above",
        "He keeps no second session list: Attention membership, the precise focus with Intent, and the actual Task and thread state are his bookkeeping",
        "including a quick action",
        "add the existing authorized Task with attention_add, or create the missing Global Task first",
        "Ordinary conversation needs no task",
        "add no second subscription",
        "Never discard blocked work",
        "remove Done work only when the owner removed it or instructed you to",
        "owner_requested: true, with the owner's instruction recorded as the rationale",
        "Removal releases the related focus; it never completes or deletes the Task or cancels another worker",
        "never removes Done membership or completes the Task",
        "Current work is the precise focus plus atomic Intent, without a word limit",
        "Focusing a Story, Project, email, customer or workflow still points at the exact work Task already on the board; the broader item never replaces it",
        "Intent-only edits and ordinary tool use do not restart the focus reminder",
        "reaching an Intent completes neither the Task nor its membership",
        "read eligible candidates with workers_list, and including the owner marks User needed",
        "User needed comes only from Workers and is independent of task state: needing the owner forces neither Blocked nor Waiting",
        "Blocked means progress cannot continue; Waiting means an external party owes the next action",
        "Editing Worker never dispatches an assignment or a delegated instruction",
        "unrelated inbox events stay pending rather than being discarded, and direct owner messages always arrive",
        "an explicit owner stop, pause or switch takes precedence",
    ):
        assert phrase in text, phrase


def test_the_shared_procedure_pins_the_workflow_start_receipt_and_the_thirty_minute_grace():
    text = shared_procedure()
    for phrase in (
        "A workflow start needs a stable operation_id: keep that exact id across an uncertain retry",
        "when the result reports attention_sync_pending with local_operation_completed: true and a run_id, reconcile that run instead of starting another and report it as a pending sync",
        "Never forge consumer, registration or binding values that the native helper owns",
        "record the blocked episode with attention_workflow_update",
        "a ready episode clears it without resuming a paused workflow",
        "an explicit owner pause releases the focus at once and keeps the pause",
        "a blocker still unresolved after the 30-minute grace period is detached through that operation, keeping its saved progress and its board Task",
        "A workflow start, resume or pause is synchronized to the board by the native client through attention_workflow_context and attention_workflow_sync",
        "those are coordination reads and association writes only",
        "focus_* and the owner's Pinned board stay separate from these attention_* procedures",
    ):
        assert phrase in text, phrase


def test_imported_role_guidance_scopes_generic_work_and_matches_the_attention_contract():
    text = imported_guidance()
    for phrase in (
        "Agents other than Ordubashy keep a short shared current-work list with agent_work_update for your native session UUID",
        "For agents other than Ordubashy, before finishing a substantive turn, confirm or update that current-work list",
        "For agents other than Ordubashy, make the current-work review the last substantive operation of an ordered batch",
        "For Ordubashy, commitments live on his own Attention board (attention_get, attention_add, attention_remove)",
        "separate from the owner's Pinned board and its focus_* operations",
        "He keeps no second session current-work list: membership, the precise focus with Intent and the actual Task and thread state are his bookkeeping",
        "including a quick action, ensure its Task is on that board",
        "a project Task keeps its project",
        "Current work is the precise focus plus atomic Intent through attention_focus_set",
        "a Story, Project, email, customer or workflow focus still points at the exact work Task already on the board",
        "reaching an Intent completes neither the Task nor its membership",
        "Done stays on the board, most recently completed first, until the owner removes it or instructs you to",
        "with owner_requested and the recorded instruction",
        "clearing session current-work items or tidying the board never removes Done or blocked work",
        "Removing membership releases that commitment and focus without completing or deleting the Task or cancelling another worker",
        "Worker names who contributes (workers_list)",
        "including the owner marks User needed, which is independent of task state and forces neither Blocked nor Waiting",
        "Blocked means progress cannot continue and Waiting means an external party owes the next action",
        "Editing Worker never dispatches",
        "unrelated inbox events stay pending and direct owner messages arrive, and an explicit owner stop, pause or switch takes precedence",
        "A workflow start needs a stable operation_id kept across an uncertain retry",
        "when the result reports attention_sync_pending with local_operation_completed true and a run_id, reconcile that run rather than starting another",
        "report it as a pending sync, and never forge the helper's consumer, registration or binding values",
        "attention_workflow_update records a blocked workflow episode with a stable blocker key and reason",
        "clears it when ready without resuming a paused workflow, keeps an owner pause",
        "detaches a still-blocked episode after the 30-minute grace while keeping saved progress and the board Task",
        "the native client synchronizes a start, resume or pause through attention_workflow_context and attention_workflow_sync",
        "which are coordination only and never replace engine control or approval",
        "A reminder means resume or reassess useful work, not pause it",
    ):
        assert phrase in text, phrase


@pytest.mark.parametrize("source", [shared_procedure, imported_guidance], ids=["shared-procedure", "imported-guidance"])
def test_generic_workers_keep_the_existing_current_work_procedure(source):
    text = source()
    for phrase in ("A permanent worker keeps one current item, not a list",
                   "Prefer patch actions",
                   "Give each worker one atomic assignment at a time and choose its next"):
        assert phrase in text, phrase


def test_the_role_source_grants_only_ordubashy_the_frozen_attention_operations():
    root = role_source()
    ordubashy = yaml.safe_load((root / "roles/ordubashy/role.yaml").read_text())
    policy = ordubashy["default_tool_policy"]
    for name in FROZEN_OPERATIONS:
        assert policy[name] == "allow", name
    # The board references ordinary Tasks and the separate owner Pinned board; those grants stay.
    for name in ("tasks_create", "tasks_update", "tasks_get", "tasks_list", "tasks_overview", "tasks_complete",
                 "assignees_list", "focus_get", "focus_present", "focus_edit"):
        assert policy[name] == "allow", name
    for role in ("global-worker", "learning-applier"):
        other = yaml.safe_load((root / "roles" / role / "role.yaml").read_text())
        assert not [name for name in FROZEN_OPERATIONS if name in other["default_tool_policy"]], role


def test_the_role_brief_keeps_done_visible_and_worker_edits_separate_from_dispatch():
    root = role_source()
    body = (root / "roles/ordubashy/BRIEF.md").read_text()
    assert len(body.strip()) <= 10000
    brief = flat(body)
    for phrase in ("Your commitments live on your own Attention board",
                   "separate from the owner's Pinned board (owner-focus, focus_*)",
                   "Before work beyond ordinary conversation, including a quick action, put its Task on that board",
                   "Current work is the precise focus plus atomic Intent",
                   "Done stays until the owner removes it or instructs you to",
                   "ordinary cleanup never removes Done or blocked work",
                   "editing it never dispatches",
                   "delegate with messages",
                   "synced to the board through attention_workflow_context and attention_workflow_sync",
                   "detached after its 30-minute grace, keeping saved progress and the board Task"):
        assert phrase in brief, phrase
    worker = flat((root / "roles/global-worker/BRIEF.md").read_text())
    assert "update your current work (agent_work_update) with a Task reference" in worker
    assert "Attention board" not in worker