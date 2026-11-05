"""Keeps a test away from the real user's Claude Code and Codex configuration and from any real Qorqbot instance.

Trust and launcher tests read and write ``~/.claude.json`` and ``~/.codex/config.toml`` and resolve them from HOME,
CLAUDE_CONFIG_DIR, CODEX_HOME and QORQUT_NATIVE_USER_ENV. ``isolate`` points all of them at a disposable directory for the
length of one test; the fake executables a test runs get the same environment from :func:`environment`.
"""
import os
from pathlib import Path
import shutil
import tempfile
from unittest import mock

VARIABLES = ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME")


def environment(root: Path) -> dict:
    """The CLI configuration variables for a disposable ``root``, created as directories."""
    values = {"HOME": root / "home", "CLAUDE_CONFIG_DIR": root / "claude", "CODEX_HOME": root / "codex", "XDG_CONFIG_HOME": root / "xdg"}
    for path in values.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in values.items()}


def isolate(case) -> dict:
    """Run ``case`` (a TestCase) with the CLI configuration variables in a fresh disposable directory and without
    QORQUT_NATIVE_USER_ENV. Returns the variables so a subprocess can be given the same ones."""
    root = Path(tempfile.mkdtemp(prefix="cli-isolation "))
    case.addCleanup(shutil.rmtree, root, True)
    values = environment(root)
    patcher = mock.patch.dict(os.environ, values)
    patcher.start()
    case.addCleanup(patcher.stop)
    os.environ.pop("QORQUT_NATIVE_USER_ENV", None)
    for name in VARIABLES:
        assert os.environ[name].startswith(str(root)), name
    return values
