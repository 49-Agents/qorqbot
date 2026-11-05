from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"ordubashy": "1.72.0", "learning-applier": "1.16.0"}
GRANTS = ("actions_record", "actions_list", "actions_get", "actions_config_get")


class ActionRecordingTest(unittest.TestCase):
    def test_active_roles_select_the_shared_skill_and_ordinary_grants(self):
        for role, version in ROLES.items():
            with self.subTest(role=role):
                manifest = yaml.safe_load((ROOT / f"roles/{role}/role.yaml").read_text())
                self.assertEqual(manifest["version"], version)
                self.assertIn({"id": "action-recording", "path": "skills/action-recording/SKILL.md"}, manifest["skills"])
                policy = manifest["default_tool_policy"]
                self.assertEqual({name: policy.get(name) for name in GRANTS}, dict.fromkeys(GRANTS, "allow"))
                # Owner edits of manual captures are never an agent grant.
                self.assertNotIn("actions_update", policy)

    def test_only_the_general_roles_remain(self):
        self.assertEqual(sorted(path.parent.name for path in (ROOT / "roles").glob("*/role.yaml")), sorted([*ROLES, "global-worker"]))

    def test_skill_states_the_rule_scope_and_time_convention(self):
        text = (ROOT / "skills/action-recording/SKILL.md").read_text()
        for phrase in ("at most 20 words", "A task is\noptional", "business-operational", "global-operational",
                       "`global-systems` and Unclassified are owner-only", "America/Puerto_Rico",
                       "`YYYY-MM-DDTHH`", "Never record only to\n  satisfy a completion reminder", "Record only your own work",
                       "Always choose `category`"):
            self.assertIn(phrase, text)
        for phrase in ("Business Agent", "Business roles", "home business", "Founder", "Global Marketing"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
