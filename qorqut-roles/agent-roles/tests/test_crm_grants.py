"""Guard the CRM operation grants: Ordubashy holds all nine, the worker only the two Task-based ones."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TABLE_OPERATIONS = ("crm_entries_list", "crm_entries_add", "crm_entries_update", "crm_entries_remove",
                    "tables_thread_get", "tables_follow", "tables_comment")
TASK_OPERATIONS = ("crm_activity_list", "crm_activity_record")
# The existing operation each one is bounded by in the host policy; the role must hold it too, or the new grant is inert.
BOUNDED_BY = {"crm_entries_list": "tables_rows_list", "crm_entries_add": "tables_rows_write", "crm_entries_update": "tables_rows_write",
              "crm_entries_remove": "tables_rows_write", "tables_thread_get": "tables_get", "tables_follow": "tables_get",
              "tables_comment": "tables_rows_write", "crm_activity_list": "tasks_get", "crm_activity_record": "tasks_comment"}


def manifest(role):
    return yaml.safe_load((ROOT / "roles" / role / "role.yaml").read_text())


def flat(path):
    return " ".join((ROOT / path).read_text().split())


class CrmGrantsTest(unittest.TestCase):
    def test_ordubashy_holds_every_crm_operation_as_an_ordinary_allow_on_top_of_the_base_grant_it_is_bounded_by(self):
        role = manifest("ordubashy")
        policy = role["default_tool_policy"]
        for name in (*TABLE_OPERATIONS, *TASK_OPERATIONS):
            with self.subTest(operation=name):
                self.assertEqual(policy[name], "allow")
                self.assertEqual(policy[BOUNDED_BY[name]], "allow")
        self.assertEqual(role["version"], "1.72.0")

    def test_the_worker_holds_only_the_two_task_based_operations_and_never_a_table_one(self):
        role = manifest("global-worker")
        policy = role["default_tool_policy"]
        self.assertEqual({name: policy[name] for name in TASK_OPERATIONS}, dict.fromkeys(TASK_OPERATIONS, "allow"))
        for name in TASK_OPERATIONS:
            self.assertEqual(policy[BOUNDED_BY[name]], "allow")
        self.assertFalse([name for name in TABLE_OPERATIONS if name in policy])
        self.assertFalse([name for name in policy if name.startswith(("tables_", "crm_entries_"))])
        self.assertEqual(role["version"], "1.6.0")

    def test_no_other_role_receives_a_crm_operation(self):
        policy = manifest("learning-applier")["default_tool_policy"]
        self.assertFalse([name for name in (*TABLE_OPERATIONS, *TASK_OPERATIONS) if name in policy])

    def test_the_guidance_names_the_tools_and_the_boundaries_it_cannot_widen(self):
        skill = flat("skills/business-tables/SKILL.md")
        for text in ("CRM Tables", "`crm_entries_add`", "`crm_entries_list`", "`crm_activity_list`", "`crm_activity_record`",
                     "`tables_thread_get`", "`tables_comment`", "`tables_follow`", "never copied or converted",
                     "it neither sends anything nor imports a mailbox", "stays in `owner_conversation_*`"):
            self.assertIn(text, skill)
        brief = flat("roles/ordubashy/BRIEF.md")
        self.assertLessEqual(len((ROOT / "roles/ordubashy/BRIEF.md").read_text()), 10000)
        for text in ("CRM Tables", "`crm_activity_list`", "`crm_activity_record`", "Task comment authority", "never reposted"):
            self.assertIn(text, brief)
        worker = flat("roles/global-worker/BRIEF.md")
        self.assertNotIn("crm_entries", worker)

    def test_the_readme_and_source_document_record_the_new_versions_without_claiming_adoption(self):
        readme, document = flat("README.md"), flat("docs/V1.3-ROLES.md")
        self.assertIn("| `roles/ordubashy/` | 1.72.0 |", readme)
        self.assertIn("| `roles/global-worker/` | 1.6.0 |", readme)
        self.assertIn("does not publish or adopt either role", readme)
        self.assertIn("nothing is published, adopted or granted live by this source", document)


if __name__ == "__main__":
    unittest.main()
