"""Structural role/Context boundaries; semantic quality needs the pilot."""
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/learning-applier"
SKILL = ROOT / "skills/learning-application"


def test_reused_role_and_context_authority():
    role = yaml.safe_load((ROLE / "role.yaml").read_text())
    assert role["id"] == "learning-applier"
    assert role["version"] == "1.16.0"
    assert role["scope_kinds"] == ["ecosystem"]
    assert any(s["id"] == "learning-application" for s in role["skills"])
    policy = role["default_tool_policy"]
    for name in ("context_list", "context_search", "context_get", "context_upload", "context_instruct", "context_edit"):
        assert policy[name] == "allow"
    assert policy.get("context_people_merge") != "allow"
    assert policy.get("context_people_affiliation_set") != "allow"
    for name in ("agents_get", "tasks_get", "tasks_thread_get", "actions_get", "actions_thread_get",
                 "signoffs_get", "signoffs_notes_list"):
        assert policy[name] == "allow"
    for name in ("agents_update", "tasks_create", "tasks_update", "tasks_comment", "permissions_set"):
        assert policy.get(name) != "allow"


def test_skill_references_exist_and_no_superseded_procedural_commands():
    skill = (SKILL / "SKILL.md").read_text()
    for ref in re.findall(r"\]\((references/[^)]+)\)", skill):
        assert (SKILL / ref).is_file()
    files = [ROLE / "BRIEF.md", *SKILL.rglob("*.md")]
    guidance = "\n".join(path.read_text().lower() for path in files)
    for old in ("learnings_decide", "learnings_work_start", "learnings_work_done", "learnings_issue_action"):
        assert old not in guidance
