"""Core-owned instructions for user-invoked operational cleanup."""

from importlib.resources import files

import yaml

from bazarlyk.catalog.packages import files_digest


SKILL_NAME = "bazarlyk-cleanup"
SKILL_DIRECTORIES = (".claude/skills/bazarlyk-cleanup", ".agents/skills/bazarlyk-cleanup")


def skill_files():
    return {"SKILL.md": files(__package__).joinpath("skills/cleanup/SKILL.md").read_bytes()}


def skill_metadata():
    content = skill_files()
    metadata = yaml.safe_load(content["SKILL.md"].decode().split("---", 2)[1])
    digest = files_digest(content)
    return {"id": SKILL_NAME, "name": SKILL_NAME, "description": metadata["description"],
            "source_path": "qorqut-core:cleanup", "materialization_hash": digest,
            "owner": {"kind": "core", "id": "cleanup-workflow"}, "version": "1",
            "digest": digest, "generated_path": SKILL_DIRECTORIES[0] + "/SKILL.md",
            "expected_discoverability": "project_skill_after_preparation"}


def fingerprint():
    return {"skill": files_digest(skill_files())}
