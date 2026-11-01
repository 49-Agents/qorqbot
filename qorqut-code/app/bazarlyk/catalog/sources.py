"""Owner-configured source locations; package contents remain independently pinned."""

import json
import os
from pathlib import Path

from qorqut_operations.contracts import DomainError
from .packages import ID, relative_path, validate_schema


SOURCE_SCHEMA = {
    "type": "object", "minProperties": 1, "propertyNames": ID,
    "additionalProperties": {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "path", "manifest"],
        "properties": {
            "kind": {"enum": ["role", "plugin"]},
            "path": {"type": "string", "minLength": 1},
            "manifest": {"type": "string", "minLength": 1},
            "repository": {"type": "string", "minLength": 1},
        },
    },
}


def load_sources(workspace, *, paths=None, config_path=None):
    """Entries resolve against ``workspace``; the default map file is the ``app`` component's, from explicit ``paths``."""
    workspace = Path(workspace).resolve()
    selected = config_path or os.environ.get("QORQUT_PACKAGE_SOURCES")
    if not selected:
        if paths is None:
            raise DomainError("invalid_configuration", "Select a package source map or pass the explicit instance paths")
        from bazarlyk.installation import component
        selected = component(paths, 'app') / 'package-sources.json'
    path = Path(selected)
    try:
        definitions = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DomainError("invalid_configuration", f"Cannot read package source map: {path}") from exc
    validate_schema(definitions, SOURCE_SCHEMA, "package source map")
    return {name: (entry["kind"], (workspace / entry["path"]).resolve(), relative_path(entry["manifest"]))
            for name, entry in definitions.items()}
