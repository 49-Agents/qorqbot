"""Guard Ordubashy's Attention-board guidance and the separate owner Pinned board.

Source-only: reads the manifests, briefs and documentation of this repository. No host,
database, service or provider is used, and no publication or adoption is performed.
"""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The frozen public operations from the parallel implementation contract: his board, its membership,
# the precise focus with atomic Intent, workflow episodes, and the Worker candidate read.
ATTENTION = ("attention_get", "attention_add", "attention_remove", "attention_focus_set",
             "attention_focus_clear", "attention_workflow_update",
             "attention_workflow_context", "attention_workflow_sync", "workers_list")


def manifest(role):
    return yaml.safe_load((ROOT / "roles" / role / "role.yaml").read_text())


def flat(path):
    return " ".join((ROOT / path).read_text().split())


class AttentionGuidanceTest(unittest.TestCase):
    def test_ordubashy_holds_the_frozen_attention_operations_as_ordinary_allows(self):
        role = manifest("ordubashy")
        policy = role["default_tool_policy"]
        for name in ATTENTION:
            with self.subTest(operation=name):
                self.assertEqual(policy[name], "allow")
        # The board references the existing task system: those grants and the owner Pinned board stay.
        for name in ("tasks_create", "tasks_get", "tasks_list", "tasks_overview", "tasks_update", "tasks_complete",
                     "assignees_list", "focus_get", "focus_present", "focus_edit", "focus_move"):
            with self.subTest(operation=name):
                self.assertEqual(policy[name], "allow")
        self.assertEqual(role["version"], "1.72.0")

    def test_no_other_role_receives_an_attention_operation(self):
        for path in sorted((ROOT / "roles").glob("*/role.yaml")):
            role = yaml.safe_load(path.read_text())
            if role["id"] == "ordubashy":
                continue
            with self.subTest(role=role["id"]):
                self.assertFalse([name for name in ATTENTION if name in role["default_tool_policy"]])

    def test_brief_requires_board_membership_before_work_and_keeps_done_and_worker_separate(self):
        brief_path = ROOT / "roles/ordubashy/BRIEF.md"
        self.assertLessEqual(len(brief_path.read_text().strip()), 10000)
        brief = flat("roles/ordubashy/BRIEF.md")
        for text in ("Your commitments live on your own Attention board",
                     "separate from the owner's Pinned board (owner-focus, focus_*)",
                     "Before work beyond ordinary conversation, including a quick action, put its Task on that board",
                     "create the missing Global Task first",
                     "Current work is the precise focus plus atomic Intent",
                     "kept current with attention_focus_set",
                     "Done stays until the owner removes it or instructs you to",
                     "ordinary cleanup never removes Done or blocked work",
                     "Worker (workers_list) names who contributes",
                     "editing it never dispatches",
                     "delegate with messages",
                     "While Focused, unrelated inbox events wait and direct owner messages arrive",
                     "a blocked workflow is recorded with attention_workflow_update and detached after its 30-minute grace",
                     "synced to the board through attention_workflow_context and attention_workflow_sync",
                     "detached after its 30-minute grace, keeping saved progress and the board Task",
                     "Reminders prompt resume or reassessment",
                     "never wait on them while useful work is available"):
            self.assertIn(text, brief)
        # The retired five-word status and an activity-reporting header are not reinstated as current work.
        for text in ("five-word", "activity-reporting Status"):
            self.assertNotIn(text, brief)

    def test_generic_workers_keep_their_session_current_work_procedure(self):
        worker = flat("roles/global-worker/BRIEF.md")
        for text in ("update your current work (`agent_work_update`) with a Task reference",
                     "a description of at most 100 words, or both"):
            self.assertIn(text, worker)
        for role in ("global-worker", "learning-applier"):
            with self.subTest(role=role):
                self.assertFalse([name for name in ATTENTION if name in manifest(role)["default_tool_policy"]])
                self.assertNotIn("Attention board", flat(f"roles/{role}/BRIEF.md"))

    def test_documentation_records_the_new_source_version_without_claiming_adoption(self):
        readme, document = flat("README.md"), flat("docs/V1.3-ROLES.md")
        self.assertIn("| `roles/ordubashy/` | 1.72.0 |", readme)
        self.assertIn("does not publish or adopt the role", readme)
        self.assertIn("## Attention board (Ordubashy 1.72.0)", document)
        self.assertIn("nothing is published, adopted or granted live by this source", document.lower())
        self.assertIn("the host must register those operations", document.lower())


if __name__ == "__main__":
    unittest.main()