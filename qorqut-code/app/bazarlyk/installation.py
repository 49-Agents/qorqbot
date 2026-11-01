"""Process-pinned installation inputs; authored-workspace symlink policy is unchanged."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path

from qorqut_operations.contracts import DomainError
from bazarlyk.instance_paths import InstancePaths


@lru_cache(maxsize=1)
def app_manifest():
    path = os.environ.get("QORQUT_APP_MANIFEST")
    if not path:
        return None
    try:
        value = json.loads(Path(path).resolve(strict=True).read_text())
        if value["format"] != 1:
            raise ValueError
        for name in ("core", "dashboard", "template"):
            root = Path(value["components"][name]["path"])
            if not root.is_absolute() or not root.is_dir():
                raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DomainError("invalid_installation", "Selected application manifest is unavailable") from exc


def component(paths: InstancePaths, name: str) -> Path:
    """The explicitly selected source or installed-artifact location of one component (``app``, ``dashboard``, ...)."""
    if not isinstance(paths, InstancePaths):
        raise DomainError("invalid_configuration", "Component locations require the explicit instance paths")
    return paths.component(name)


def installation_config():
    path = os.environ.get("QORQUT_INSTALLATION_CONFIG")
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text())
        if value["format"] != 1 or not Path(value["root"]).is_absolute():
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DomainError("invalid_installation", "Installation configuration is unavailable") from exc


def helper_launcher():
    config = installation_config()
    return str(Path(config["root"]) / "bin/agent-helper") if config else None
