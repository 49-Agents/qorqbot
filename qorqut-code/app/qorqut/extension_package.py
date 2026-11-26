"""The agentic-layer package made from an extension's plugin part (docs/EXTENSION-CONTRACT.md, section 7).

``plugins/<id>/`` in the layer holds the files of the folder's ``plugin/`` at the same relative paths, plus a generated
``agentic.yaml``. Each ``skills/<name>/SKILL.md`` becomes a skill entry and each ``workflows/<name>.yaml`` a workflow
entry, so the layer serves them. ``hooks.yaml``, ``records.yaml`` and ``instructions/*.md`` are plain files no entry
names: the layer holds them with Git history and lets them be edited, but does not list them in its catalog.

``snapshot`` reads files only; copying it into the layer is ``ExtensionRegistry.sync``.
"""

from __future__ import annotations

import base64
from pathlib import Path

from qorqut_agentic.catalog import MANIFEST_NAME, bundle_digest
from qorqut_agentic.layout import import_dir
from qorqut_agentic.util import dump_yaml, load_yaml, sha256_hex, split_frontmatter

from qorqut.extension_contract import ContractError, Manifest

OWNER = "extension"


def package_id(ext_id: str) -> str:
    """The layer package of the extension ``ext_id``: its own id. Every place that names the package asks here."""
    return ext_id


def native(manifest: Manifest) -> dict:
    """The mark that tells a package made from an extension apart from every other package of the layer."""
    return {"owner": OWNER, "extension": manifest.id}


def _skill(plugin: Path, folder: Path) -> dict:
    path = folder / "SKILL.md"
    meta, problem = split_frontmatter(path.read_text(encoding="utf-8"))
    if problem or not isinstance(meta, dict):
        raise ContractError(f"The skill {folder.name!r} has no usable front matter in SKILL.md.")
    return {"id": folder.name, "kind": "skill", "description": meta.get("description"),
            "when_to_use": meta.get("read_for"), "path": path.relative_to(plugin).as_posix()}


def _workflow(plugin: Path, path: Path) -> dict:
    data = load_yaml(path.read_text(encoding="utf-8"))
    description = data.get("description") if isinstance(data, dict) else None
    return {"id": path.stem, "kind": "workflow", "description": description, "when_to_use": description,
            "path": path.relative_to(plugin).as_posix()}


def snapshot(folder: Path, manifest: Manifest) -> dict | None:
    """The package as an ``import_package`` snapshot (``{"files": [...]}``, ``agentic.yaml`` included), or None when the
    extension has no plugin part. Raises ``ContractError`` when the plugin part cannot be made into a package."""
    plugin = Path(folder) / "plugin"
    if plugin.is_symlink():
        raise ContractError("`plugin/` is a symbolic link.")
    if not plugin.is_dir():
        return None
    paths = sorted(plugin.rglob("*"))
    links = [path.relative_to(plugin).as_posix() for path in paths if path.is_symlink()]
    if links:
        raise ContractError(f"The plugin part holds a symbolic link: plugin/{links[0]}.")
    files = {path.relative_to(plugin).as_posix(): path.read_bytes() for path in paths if path.is_file()}
    if MANIFEST_NAME in files:
        raise ContractError(f"The plugin part holds its own {MANIFEST_NAME}, which Qorqbot writes itself.")
    skills = plugin / "skills"
    workflows = plugin / "workflows"
    entries = [_skill(plugin, item) for item in sorted(skills.iterdir()) if item.is_dir()] if skills.is_dir() else []
    entries += [_workflow(plugin, item) for item in sorted(workflows.rglob("*.yaml")) if item.is_file()] \
        if workflows.is_dir() else []
    files[MANIFEST_NAME] = dump_yaml({
        "schema_version": 1, "id": package_id(manifest.id), "name": manifest.name, "description": manifest.summary,
        "source": {"kind": OWNER, "extension": manifest.id, "version": manifest.version, "delivery": manifest.delivery},
        "native": native(manifest), "entries": entries}).encode("utf-8")
    return {"files": [{"path": path, "content_base64": base64.b64encode(data).decode("ascii")}
                      for path, data in sorted(files.items())]}


def _contents(snap: dict):
    for item in snap["files"]:
        yield item["path"], sha256_hex(base64.b64decode(item["content_base64"]))


def digest(snap: dict) -> str:
    """The digest of a snapshot, the same the layer reports as its ``snapshot_digest``."""
    return bundle_digest(_contents(snap))


def revision(package: str, snap: dict) -> str:
    """The layer's package revision once the package equals ``snap`` exactly."""
    directory = import_dir(package)
    return bundle_digest((f"{directory}/{path}", sha) for path, sha in _contents(snap))
