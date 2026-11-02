"""Core-owned instructions for user-invoked operational cleanup."""

from importlib.resources import files

import yaml

from bazarlyk.agentic.text import read_resource
from bazarlyk.catalog.packages import files_digest


SKILL_NAME = "bazarlyk-cleanup"
SKILL_DIRECTORIES = (".claude/skills/bazarlyk-cleanup", ".agents/skills/bazarlyk-cleanup")


def skill_files(*, agentic_view=None):
    if agentic_view is None:
        return {"SKILL.md": files(__package__).joinpath("skills/cleanup/SKILL.md").read_bytes()}
    body = read_resource(agentic_view, "host", "app/bazarlyk/agents/skills/cleanup/SKILL.md")["content"]
    return {"SKILL.md": body.encode("utf-8")}


def skill_metadata(*, agentic_view=None):
    content = skill_files(agentic_view=agentic_view)
    metadata = yaml.safe_load(content["SKILL.md"].decode().split("---", 2)[1])
    digest = files_digest(content)
    return {"id": SKILL_NAME, "name": SKILL_NAME, "description": metadata["description"],
            "source_path": "qorqut-core:cleanup", "materialization_hash": digest,
            "owner": {"kind": "core", "id": "cleanup-workflow"}, "version": "1",
            "digest": digest, "generated_path": SKILL_DIRECTORIES[0] + "/SKILL.md",
            "expected_discoverability": "project_skill_after_preparation"}


def fingerprint(*, agentic_view=None):
    return {"skill": files_digest(skill_files(agentic_view=agentic_view))}
