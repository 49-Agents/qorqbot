"""Contract for the Ideas removal in the role sources: source-only, no host needed.

The packages are Ordubashy, the global worker and Learning Applier. None of them may instruct the removed ``ideas_*``
operations, the retired ``manage_ideas`` capability or the idea-only arguments, and no historical exception remains: the
retired Founder source is not part of this repository.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROLES = ("ordubashy", "global-worker", "learning-applier")
REMOVED = (
    re.compile(r"\bideas_[a-z_]+"),
    re.compile(r"\bmanage_ideas\b"),
    re.compile(r"\b(?:source_idea_id|founding_idea_id|parent_idea_id|idea_id|founder_agent_id)\b"),
    re.compile(r"\bIdeas (?:tools?|records?|store)\b"),
    re.compile(r"(?i)\bidea focus\b"),
)
RETIRED_IDEAS = ("ideas_list", "ideas_get", "ideas_focus_get", "ideas_create", "ideas_update", "ideas_focus",
                 "ideas_start_business", "ideas_handoff", "ideas_businesses_list", "ideas_link_business", "ideas_tags_get")


def role(name):
    return yaml.safe_load((ROOT / "roles" / name / "role.yaml").read_text())


def flat(path):
    return " ".join((ROOT / path).read_text().split())


def removed_tokens(text):
    return sorted({match.group(0) for pattern in REMOVED for match in pattern.finditer(text)})


def role_files(name):
    """Every file a role package carries from this repository: manifest, brief and each selected skill's directory."""
    manifest = role(name)
    files = {f"roles/{name}/role.yaml", manifest["brief"]}
    for skill in manifest.get("skills", []):
        files.update(str(path.relative_to(ROOT)) for path in (ROOT / skill["path"]).parent.rglob("*") if path.is_file())
    return files


class ActivePackagesTest(unittest.TestCase):
    def test_no_file_in_the_repository_instructs_a_removed_operation_capability_or_argument(self):
        files = set()
        for name in ACTIVE_ROLES:
            files |= role_files(name)
        self.assertGreater(len(files), 30)
        for path in sorted(files):
            if path.endswith((".md", ".yaml", ".json")):
                with self.subTest(path=path):
                    self.assertEqual(removed_tokens((ROOT / path).read_text()), [])
        # Nothing else in the packaged folders mentions them either: there is no historical exception.
        for folder in ("roles", "skills"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file():
                    with self.subTest(path=str(path.relative_to(ROOT))):
                        self.assertEqual(removed_tokens(path.read_text()), [])

    def test_active_policies_hold_no_retired_operation(self):
        for name in ACTIVE_ROLES:
            policy = role(name)["default_tool_policy"]
            with self.subTest(role=name):
                self.assertFalse(set(RETIRED_IDEAS) & set(policy))
                self.assertFalse([key for key in policy if key.startswith("ideas_")])
        self.assertEqual(role("ordubashy")["default_tool_policy"]["business_create"], "allow")
        # No worker receives creation, setup submission or any Ideas operation by default.
        worker = role("global-worker")["default_tool_policy"]
        self.assertFalse([key for key in worker if key.startswith(("ideas_", "business_create", "business_setup"))])

    def test_the_founder_source_and_its_only_procedure_are_not_carried(self):
        self.assertFalse((ROOT / "roles/founder").exists())
        self.assertFalse((ROOT / "skills/business-founding").exists())
        self.assertFalse((ROOT / "plugins").exists())
        readme = flat("README.md")
        self.assertNotIn("Bounded historical exception", readme)
        self.assertNotIn("byte-identical", readme)


class ReplacementGuidanceTest(unittest.TestCase):
    """The creation procedure is Ordubashy's project-creation; test_project_transition covers it in full."""

    def test_creation_is_ordubashys_project_creation_and_the_setup_procedure_is_gone(self):
        self.assertFalse((ROOT / "skills/business-setup").exists())
        body = flat("skills/project-creation/SKILL.md")
        for phrase in ("Call `business_create` in Global (select no project)", "a stable `idempotency_key`",
                       "never create, request or revive one", "Nothing else is accepted"):
            self.assertIn(phrase, body)
        self.assertEqual({skill["id"]: skill["path"] for skill in role("ordubashy")["skills"]}["project-creation"],
                         "skills/project-creation/SKILL.md")


class ReferencesResolveTest(unittest.TestCase):
    def test_every_selected_skill_exists_and_no_skill_directory_is_orphaned(self):
        selected = set()
        for name in ACTIVE_ROLES:
            for skill in role(name).get("skills", []):
                with self.subTest(role=name, skill=skill["id"]):
                    self.assertTrue((ROOT / skill["path"]).is_file())
                selected.add((ROOT / skill["path"]).parent)
        self.assertEqual({path.parent for path in (ROOT / "skills").glob("*/SKILL.md")}, selected)


if __name__ == "__main__":
    unittest.main()
