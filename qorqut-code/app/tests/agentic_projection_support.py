"""Complete synthetic inventories and explicit stand-ins for Worker C's APIs.

No real-source import is claimed: C's importer and text changes are integrated
in a later worktree. Fixtures only create files beneath pytest's scratch root.
"""
import base64
from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys
from types import ModuleType, SimpleNamespace

import yaml

from bazarlyk.agentic.inventory import Inventory
from bazarlyk.agentic.plans import build_import_plan
from bazarlyk.agentic.support import digest
from bazarlyk.agentic.view import HostAgenticView
from bazarlyk.catalog.packages import ArtifactPackage, content_hash, files_digest
from qorqut_agentic import AgenticLayer


ATTRIBUTION = {"actor_id": "fixture-owner", "actor_kind": "human", "reason": "Synthetic projection fixture"}


def skill_body(name, text="Current managed instructions."):
    return ("---\nname: " + name + "\ndescription: Synthetic managed procedure.\n---\n\n" + text + "\n").encode()


def native_package(kind="plugin", package_id="content-creation", skill_id="content-foundations", *, root=False):
    path = "SKILL.md" if root else "skills/" + skill_id + "/SKILL.md"
    body = skill_body("bazarlyk-" + kind + "-" + skill_id, "Stale pinned instructions.")
    manifest = {"id": package_id, "version": "1.0.0", "skills": [{"id": skill_id, "path": path}],
                "scope_kinds": ["ecosystem"], "default_tool_policy": {}, "brief": "BRIEF.md"}
    files = {path: body, "BRIEF.md": b"Pinned brief.", "tools.yaml": b"tools: {}\n"}
    return ArtifactPackage(kind, manifest, "manifest.yaml", files, files_digest(files),
        ({"id": skill_id, "name": "bazarlyk-" + kind + "-" + skill_id, "source_path": path,
          "subtree": str(Path(path).parent), "description": "Pinned description.",
          "materialization_hash": files_digest({path: body})},))


def inventory_fixture(packages=(), *, inactive=False, preferences=False):
    inv = Inventory()
    for source_id, owner, enabled in (("host", "host", True), ("roles", "roles", True),
                                       ("content", "content", True), ("content-library", "content", True),
                                       ("email", "email-management", True), ("custom-off", "off", False)):
        source = SimpleNamespace(id=source_id, owner=owner, enabled=enabled, used={}, commit="0" * 40)
        source.record = lambda source=source: {"id": source.id, "owner": source.owner, "enabled": source.enabled,
            "commit": source.commit, "repository_prefix": "", "clean": True, "files": source.used,
            "snapshot_digest": digest(source.used)}
        inv.sources[source_id] = source
    host = inv.package("imported-host-builtins", "host", True)
    for path, body in {
        "app/bazarlyk/agents/skills/role/SKILL.md": skill_body("role"),
        "app/bazarlyk/agents/skills/cleanup/SKILL.md": skill_body("bazarlyk-cleanup"),
        "app/bazarlyk/agentic/assets/toolkit.md": b"Run {{TOOLKIT_COMMAND}}. Read the catalog before work.\n",
        "app/bazarlyk/agentic/assets/inbox-session-note.md": b"Managed inbox session note.\n",
        "app/bazarlyk/agentic/assets/software-ownership.md": b"Managed software ownership boundary.\n",
        "app/bazarlyk/agentic/assets/connection-orientation.md": b"Managed connection orientation.\n",
        "app/bazarlyk/agentic/assets/connection-guidance.md": b"Managed connection guidance.\n",
    }.items():
        inv.entry(inv.sources["host"], host, path, "skill" if path.endswith("SKILL.md") else "instruction", data=body)
    inv.binding(inv.sources["host"], "host", host, [host["id"] + "/" + e["id"] for e in host["entries"]], kind="builtin")
    for package in packages:
        source = inv.sources["roles" if package.kind == "role" else "content"]
        target = inv.package("imported-roles" if package.kind == "role" else "imported-content", source.owner, True)
        choices, refs = [], []
        for skill in package.skills:
            path = skill["source_path"]
            ref = inv.entry(source, target, path, data=skill_body(skill["name"]))
            refs.append(ref)
            choices.append({"native_skill_id": skill["id"], "canonical_entry": ref})
            root = str(Path(path).parent)
            asset_path = (root + "/" if root != "." else "") + "assets/guide.txt"
            inv.put(source, target, asset_path, data=b"Managed resource.\n")
        if package.kind == "role":
            refs.append(inv.entry(source, target, package.manifest["brief"], "instruction", data=b"Managed brief.\n"))
        inv.binding(source, package.id, target, refs, kind=package.kind, skills=choices)
    if inactive:
        target = inv.package("imported-custom-off", "off", False)
        ref = inv.entry(inv.sources["custom-off"], target, "SKILL.md", data=skill_body("off"))
        inv.binding(inv.sources["custom-off"], "off", target, [ref], kind="skill")
        template = inv.package("imported-host-builtins-templates", "host", False)
        ref = inv.entry(inv.sources["host"], template, "templates/SKILL.md", template=True,
                        data=skill_body("bazarlyk-plugin-basic"))
        inv.binding(inv.sources["host"], "basic", template, [ref], kind="plugin", template=True,
                    skills=[{"native_skill_id": "basic", "canonical_entry": ref}])
    if preferences:
        package = inv.package("user-preferences", "host", True)
        inv.entry(inv.sources["host"], package, "SKILL.md", entry_id="preferences",
                  destination="SKILL.md", data=skill_body("preferences", "Synthetic preferences."))
    return inv.finish()


def opened_view(tmp_path, packages=(), *, inactive=False, preferences=False):
    inv = inventory_fixture(packages, inactive=inactive, preferences=preferences)
    layer = AgenticLayer.initialize(tmp_path / "agentic", tmp_path / "state")
    # Fixture owner provisions protected built-ins on disk, then the real engine observes them.
    for change in build_import_plan(inv)["changes"]:
        target = layer.root / change["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(change["content_base64"]))
    layer.changes.scan()
    return HostAgenticView(layer, inv), inv


def fake_c_interfaces(monkeypatch):
    """Use only C's exact keyword contracts, without copying its implementation."""
    from bazarlyk.agents import cleanup_workflow, role_workflow, resolver

    def workflow_files(path):
        def read(*, agentic_view):
            resource = agentic_view.resource("host", path)
            return {"SKILL.md": resource["content"].encode()}
        return read

    for workflow, path in ((role_workflow, "app/bazarlyk/agents/skills/role/SKILL.md"),
                           (cleanup_workflow, "app/bazarlyk/agents/skills/cleanup/SKILL.md")):
        monkeypatch.setattr(workflow, "skill_files", workflow_files(path))
        def metadata(*, agentic_view, workflow=workflow):
            bodies = workflow.skill_files(agentic_view=agentic_view)
            return {"name": workflow.SKILL_NAME, "id": workflow.SKILL_NAME,
                    "materialization_hash": files_digest(bodies)}
        monkeypatch.setattr(workflow, "skill_metadata", metadata)

    def compose(profile, role, sources, *, workspace, agentic_view, **kwargs):
        # C owns role-path resolution and the completed toolkit section. Keep
        # its toolkit ContextSource contract explicit in this limited fake.
        body = agentic_view.resource("roles", role.manifest["brief"])["content"]
        body += "\n\n" + sources["user-preferences/preferences"].selected_text()
        toolkit = render(agentic_view, "host", "app/bazarlyk/agentic/assets/toolkit.md",
                         TOOLKIT_COMMAND=shlex.quote(str(Path(workspace) / ".bazarlyk/agentic")))
        toolkit_hash = content_hash(toolkit.encode())
        body += "\n\n## Agentic Toolkit\n[source=core:agentic-toolkit; revision=1; hash=" + toolkit_hash + "]\n" + toolkit
        entry = {"document_id": "core:agentic-toolkit", "source_id": "core:agentic-toolkit", "source_kind": "core",
                 "source_path": "qorqut-core:agentic-toolkit", "section_selector": None, "revision": 1,
                 "hash": toolkit_hash, "required": True, "character_count": len(toolkit),
                 "inclusion_reason": "Current native authoring, catalog and progress guidance"}
        return {"rendered": body, "entries": [entry], "character_count": len(body), "bundle_hash": content_hash(body.encode())}
    monkeypatch.setattr(resolver, "compose_context", compose)
    text = ModuleType("bazarlyk.agentic.text")
    def render(view, source_id, source_path, **values):
        body = view.resource(source_id, source_path)["content"]
        for key, value in values.items():
            body = body.replace("{{" + key + "}}", value)
        return body
    text.render = render
    monkeypatch.setitem(sys.modules, "bazarlyk.agentic.text", text)


def profile_for(role, plugins=()):
    return {"schema_version": 2, "agent_id": "00000000-0000-4000-8000-000000000001", "profile_revision": 1,
            "name": "fixture", "display_name": "Fixture", "status": "active", "runtime": "codex",
            "role": role.ref, "plugins": [{**p.ref, "config": {}} for p in plugins],
            "scope": {"kind": "ecosystem", "global_capabilities": ["read_global_context"], "allowed_business_ids": []},
            "assignment_brief": "Fixture.", "tool_policy": {"overrides": {}}, "presentation": {},
            "created_at": "2026-10-26T00:00:00Z"}
