"""Synthetic copied roots only; all fixtures live in the explicit pytest scratch root."""

from pathlib import Path
import subprocess

import pytest
import yaml


def write(root, path, value):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(value if isinstance(value, bytes) else value.encode())


def commit(root):
    for args in (("init", "-q", "-b", "agentic"), ("config", "user.email", "fixture@example.invalid"),
                 ("config", "user.name", "Fixture"), ("add", "."), ("commit", "-qm", "Synthetic source")):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def plugin(root, prefix, native_id, skill="compose", paths_prefix=None):
    base = prefix if paths_prefix is None else paths_prefix
    manifest = dict(schema_version=1, id=native_id, version="1.0.0", description="Synthetic native package",
                    scope_kinds=["ecosystem"], config_schema=base + "config.schema.json", tools=base + "tools.yaml",
                    rules=base + "rules.yaml", skills=[dict(id=skill, path=base + f"skills/{skill}/SKILL.md")])
    write(root, prefix + "plugin.yaml", yaml.safe_dump(manifest))
    write(root, prefix + "config.schema.json", '{}\n')
    write(root, prefix + "tools.yaml", 'tools: []\n')
    write(root, prefix + "rules.yaml", 'approval: owner_input\n')
    write(root, prefix + f"skills/{skill}/SKILL.md", f'---\nname: {skill}\ndescription: Synthetic guidance\n---\nRead [rules](references/rules.md).\n')
    write(root, prefix + f"skills/{skill}/references/rules.md", b'Synthetic rule bytes\x00\xff')


@pytest.fixture
def sources(tmp_path):
    roots = {name: tmp_path / name for name in ("host", "roles", "email", "content", "content-library", "paper", "fastmail", "custom")}
    for root in roots.values():
        root.mkdir()
    host = roots["host"]
    write(host, "app/bazarlyk/agents/skills/role/SKILL.md", "---\nname: role\n---\nSynthetic role procedure.")
    write(host, "app/bazarlyk/bootstrap/instructions/agent-foundation.md", "Synthetic foundation")
    for path in ("agents/role_workflow.py", "agents/cleanup_workflow.py", "agents/engineering.py", "agents/context.py", "inbox/defaults.py", "bootstrap/email_guidance.py"):
        write(host, "app/bazarlyk/" + path, 'INSTRUCTIONS = "' + 'Synthetic exact instruction. ' * 4 + '"\nraise RuntimeError("must never execute")\n')
    plugin(host, "app/plugins/agent-terminal-control/", "agent-terminal-control", paths_prefix="plugins/agent-terminal-control/")
    plugin(host, "app/templates/plugins/basic/", "example-procedure", paths_prefix="")
    roles = roots["roles"]
    write(roles, "skills/shared/SKILL.md", "---\nname: shared\n---\nCommon role instructions")
    write(roles, "skills/shared/references/procedure.md", "shared resource")
    for role in ("ordubashy", "global-worker", "learning-applier"):
        write(roles, f"roles/{role}/role.yaml", yaml.safe_dump(dict(schema_version=1, id=role, version="1.0.0",
            brief=f"roles/{role}/BRIEF.md", skills=[dict(id="shared", path="skills/shared/SKILL.md")], default_tool_policy={})))
        write(roles, f"roles/{role}/BRIEF.md", f"Synthetic {role} brief")
    plugin(roots["email"], "", "email-management", skill="email-management")
    write(roots["email"], "skills/email-management/instance-template/SKILL.md", "Template instructions")
    write(roots["email"], "skills/email-management/instance-template/instructions.md", "{{EMAIL_MANAGEMENT_HOME}}")
    for path in ("inboxes/README.md", "providers/gmail.md", "providers/fastmail.md"):
        write(roots["email"], "skills/email-management/instance-template/" + path, "Synthetic email guidance")
    content = roots["content"]
    for name in ("content-creation", "content-foundations", "content-gallery", "content-demand-thinking", "pull-framework", "pull-framework-content"):
        write(content, f"skills/{name}/SKILL.md", f"---\nname: {name}\n---\nAdapted {name} instructions")
    write(content, "skills/content-gallery/references/procedure.md", "Adapted resource")
    write(content, "PROVENANCE.md", "Synthetic documented adaptations")
    write(content, "experiences/case-studies/README.md", "No cases supplied")
    plugin(content, "templates/creative-channel/", "example-channel", paths_prefix="")
    write(content, "templates/creative-channel/critic/SKILL.md", "Retained critic scaffold")
    plugin(roots["content-library"], "", "content-creation", skill="content-foundations")
    write(roots["content-library"], "skills/content-foundations/SKILL.md", "Original upstream foundation")
    write(roots["paper"], "qorqut_paper/native.py", 'CONNECTION = "outreach"\nclass PaperNative:\n    def guidance():\n        return "Synthetic Paper guidance"\n')
    write(roots["paper"], "qorqut_paper/extension.py", 'ID = "paper"\nraise RuntimeError("not imported")\n')
    write(roots["paper"], "qorqut_paper/outreach.py", 'ID = "paper"\nraise RuntimeError("not imported")\n')
    write(roots["fastmail"], "qorqut_fastmail/operations.py", 'READS = {"read": (schema(), "Read metadata")}\n')
    write(roots["fastmail"], "qorqut_fastmail/extension.py", 'ID = "fastmail"\nraise RuntimeError("not imported")\n')
    plugin(roots["custom"], "", "custom-procedure")
    for root in roots.values():
        commit(root)
    return roots


@pytest.fixture
def roots(sources):
    return {name: str(path) for name, path in sources.items() if name != "custom"}


@pytest.fixture
def custom(sources):
    return [dict(id="supplied", root=str(sources["custom"]), owner="fixture-owner", kind="plugin", enabled=False)]
