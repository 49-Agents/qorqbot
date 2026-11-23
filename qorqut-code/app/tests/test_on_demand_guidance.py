"""Settings variables, Fastmail, WhatsApp and first-conversation guidance load as role skills, not at every start."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest import mock

import yaml

from qorqut_agentic.behavior import composition
from qorqut_agentic.behavior import instructions as policy

from qorqut.bootstrap import always_skills

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = ROOT / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
SKILLS = DEFAULTS / "operations/imported-role-skills/native/skills"
ROLES = DEFAULTS / "roles/imported-roles/native/roles"
GET_SET_UP = "qorqut-role-get-set-up"
WHERE = {"environment": {"ordubashy", "global-worker", "learning-applier"},
         "fastmail": {"ordubashy", "global-worker"}, "whatsapp": {"ordubashy"}, "get-set-up": {"ordubashy"}}


class OnDemandGuidanceTests(unittest.TestCase):
    def test_environment_texts_are_no_longer_composed(self):
        self.assertEqual(composition.capability_guidance({"environment_set", "environment_get", "environment_list"}), ())

    def test_skills_exist_and_belong_to_exactly_their_roles(self):
        roles = {path.parent.name: yaml.safe_load(path.read_text()) for path in ROLES.glob("*/role.yaml")}
        for folder, expected in WHERE.items():
            with self.subTest(skill=folder):
                head = (SKILLS / folder / "SKILL.md").read_text().split("---")[1]
                meta = yaml.safe_load(head)
                self.assertEqual(meta["name"], "qorqut-role-" + folder)
                self.assertTrue(1 <= len(meta["description"]) <= 600)
                path = f"operations/imported-role-skills/native/skills/{folder}/SKILL.md"
                listed = {name for name, role in roles.items() if any(s["path"] == path for s in role["skills"])}
                self.assertEqual(listed, expected)

    def test_first_conversation_skill_is_preloaded_only_on_request(self):
        self.assertNotIn(GET_SET_UP, policy.ALWAYS_SKILLS)
        instructions = NS(render=lambda key, **_values: "header")
        with tempfile.TemporaryDirectory() as directory:
            for name in (*policy.ALWAYS_SKILLS[:1], GET_SET_UP):
                skill = Path(directory) / ".claude/skills" / name / "SKILL.md"
                skill.parent.mkdir(parents=True)
                skill.write_text("Body of " + name)
            plain = policy.render_skills(instructions, directory, "claude-code")
            extra = policy.render_skills(instructions, directory, "claude-code", extra=(GET_SET_UP,))
        self.assertNotIn("Body of " + GET_SET_UP, plain)
        self.assertIn("Body of " + GET_SET_UP, extra)

    def test_extra_skill_is_not_sent_again_when_the_digest_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / ".qorqut/registration.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"workspace": directory, "registration_id": "r"}))
            def render_skills(_instructions, _workspace, _runtime, limit=None, extra=()):
                return "base" + "".join(" +" + name for name in extra)
            fake = NS(SKILL_BUDGET=1, render_skills=render_skills,
                      from_prepared=lambda _path: NS(render=lambda _key: "intake"))
            prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s"}
            text = lambda **values: always_skills.handle(prompt, path, "claude-code", **values)[
                "hookSpecificOutput"]["additionalContext"]
            with mock.patch.object(always_skills, "agentic_policy", return_value=fake):
                self.assertEqual(text(extra=("x",)), "intake\n\nbase +x")
                self.assertEqual(text(extra=("x",)), "intake")


if __name__ == "__main__":
    unittest.main()
