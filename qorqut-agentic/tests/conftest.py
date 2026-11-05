"""Shared fixtures. Every test uses synthetic data under pytest's tmp_path.

Run with an explicit scratch location, e.g. ``TMPDIR=<group>/scratch/tmp pytest``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qorqut_agentic import AgenticLayer

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "packages"
ATTR = {"actor_id": "test-owner", "actor_kind": "owner", "reason": "unit test"}
GIT_USER = {"name": "Test Author", "email": "author@example.invalid"}
PY = {"python": sys.executable}


class SimulatedCrash(BaseException):
    """Raised from a fault hook; deliberately not an ``Exception``."""


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def make_layer(base: Path, **kwargs) -> AgenticLayer:
    kwargs.setdefault("git_user", GIT_USER)
    layer = AgenticLayer.initialize(base / "root", base / "state", **kwargs)
    (layer.root / "user-preferences").mkdir(exist_ok=True)
    return layer


def reopen(layer: AgenticLayer, **kwargs) -> AgenticLayer:
    return AgenticLayer.open(layer.root, layer.state, **kwargs)


def import_example(layer: AgenticLayer, name: str, **kwargs):
    return layer.authoring.import_package(ATTR, name, {"directory": str(EXAMPLES / name)}, **kwargs)


def file_sha(layer: AgenticLayer, rel: str) -> str:
    return sha((layer.root / rel).read_bytes())


def crash_at(layer: AgenticLayer, hook: str, *, after: int = 1) -> None:
    """Make the ``after``-th firing of ``hook`` raise :class:`SimulatedCrash`."""
    counter = {"n": 0}

    def fire(**_context):
        counter["n"] += 1
        if counter["n"] >= after:
            raise SimulatedCrash(hook)

    layer._ctx.hooks[hook] = fire


def git(layer_or_root, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    root = layer_or_root.root if isinstance(layer_or_root, AgenticLayer) else layer_or_root
    env = {"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root)}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=check)


@pytest.fixture
def layer(tmp_path: Path) -> AgenticLayer:
    return make_layer(tmp_path)


@pytest.fixture
def email_layer(layer: AgenticLayer) -> AgenticLayer:
    import_example(layer, "email-composition")
    return layer


@pytest.fixture
def full_layer(layer: AgenticLayer) -> AgenticLayer:
    for name in ("email-composition", "influencer-outreach", "example-project-workflow"):
        import_example(layer, name)
    return layer
