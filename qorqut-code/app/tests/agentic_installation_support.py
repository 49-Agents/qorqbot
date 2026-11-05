"""Provision real disposable module state and host binding for runtime tests.

Synthetic export manifests describe these copied fixtures, never live sources.
All composition and import components run normally; nothing bypasses startup.
"""
from pathlib import Path
import json
import shutil

from bazarlyk.agentic.installation import prepare_installation, initialize_installation
from bazarlyk.source_snapshot import component_digest
from instance_support import CODE, workspace_root


def snapshot(root, *, roles=None, optional=()):
    group = workspace_root()
    sources = {"qorqut-code": (CODE, "qorqut-code"),
               "email-management": (group / "dependencies/email-management", "dependencies/email-management"),
               "qorqut-agentic": (group / "qorqut-agentic", "qorqut-agentic")}
    for name, relative in (("qorqut-content", "extensions/qorqut-content"), ("qorqut-paper", "extensions/qorqut-paper"),
                           ("qorqut-fastmail", "extensions/qorqut-fastmail"), ("content-library", "extensions/content-library")):
        src = group / ("dependencies/content-library" if name == "content-library" else name)
        selected = {"content": {"qorqut-content", "content-library"}, "paper": {"qorqut-paper"}, "fastmail": {"qorqut-fastmail"}}
        if src.is_dir() and any(name in selected.get(key, set()) for key in optional):
            sources[name] = src, relative
    components = []
    for name, (src, dest) in sources.items():
        if name == "qorqut-code":
            # Only procedural code/resources are fixture input, not nested repositories/build products.
            shutil.copytree(src / "app", root / dest / "app", ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", "tests", "tools", "build"))
        elif name == "qorqut-agentic":
            (root / dest).mkdir(parents=True)
            (root / dest / "pyproject.toml").write_text('[project]\nname="qorqut-agentic"\nversion="0.2.0"\n')
            shutil.copytree(src / "src/qorqut_agentic/resources/defaults", root / dest / "src/qorqut_agentic/resources/defaults")
        else:
            shutil.copytree(src, root / dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.egg-info", ".pytest_cache", "build"))
        components.append(dict(name=name, destination=dest, commit="a" * 40, tree="b" * 40))
    for c in components:
        c["snapshot_sha256"] = component_digest(root, c, components)
    (root / "SOURCE-MANIFEST.json").write_text(json.dumps(dict(format=1, components=components)))
    return root


def provision(paths, *, selected=(), roles=None, optional=()):
    root = snapshot(paths.instance_root.parent / (paths.instance_root.name + "-source-fixture"), roles=roles, optional=set(selected) | set(optional))
    return initialize_installation(paths, prepare_installation(root, selected))
