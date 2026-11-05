"""Core-owned role work procedure shared by both prepared agent clients."""

from bazarlyk.agentic.text import bundled

from importlib.resources import files

import yaml

from bazarlyk.agentic.text import read_resource, render
from bazarlyk.catalog.packages import content_hash, files_digest


SKILL_NAME = "role"
SKILL_DIRECTORIES = (".claude/skills/role", ".agents/skills/role")
INSTRUCTIONS = bundled("host", "app/bazarlyk/agentic/assets/role-workflow.md")


def skill_files(*, agentic_view=None):
    if agentic_view is None:
        return {"SKILL.md": files(__package__).joinpath("skills/role/SKILL.md").read_bytes()}
    body = read_resource(agentic_view, "host", "app/bazarlyk/agents/skills/role/SKILL.md")["content"]
    return {"SKILL.md": body.encode("utf-8")}


def skill_metadata(*, agentic_view=None):
    content = skill_files(agentic_view=agentic_view)
    metadata = yaml.safe_load(content["SKILL.md"].decode().split("---", 2)[1])
    digest = files_digest(content)
    return {"id": SKILL_NAME, "name": SKILL_NAME, "description": metadata["description"],
            "source_path": "qorqut-core:role", "materialization_hash": digest,
            "owner": {"kind": "core", "id": "role-workflow"}, "version": "1",
            "digest": digest, "generated_path": SKILL_DIRECTORIES[0] + "/SKILL.md",
            "expected_discoverability": "project_skill_after_preparation"}


def fingerprint(*, agentic_view=None):
    return {"instructions": content_hash((INSTRUCTIONS if agentic_view is None else render(agentic_view, "host", "app/bazarlyk/agentic/assets/role-workflow.md")).encode()), "skill": files_digest(skill_files(agentic_view=agentic_view))}
