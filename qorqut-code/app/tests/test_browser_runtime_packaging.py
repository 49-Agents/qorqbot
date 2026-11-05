"""The browser Node runtime is a locked, reproducible build; its pins must not drift silently."""
import importlib.util
import json
from pathlib import Path

from bazarlyk.bootstrap import agent_browser

TOOLS = Path(__file__).resolve().parents[1] / "tools/browser"
spec = importlib.util.spec_from_file_location("build_runtime", TOOLS / "build_runtime.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
PINS = {"agent-browser": "0.38.1", "proxy-chain": "3.0.1"}


def test_the_runtime_pins_are_exact_and_locked_with_integrity():
    package = json.loads((TOOLS / "package.json").read_text())
    assert package["dependencies"] == PINS  # exact versions, no ranges
    lock = json.loads((TOOLS / "package-lock.json").read_text())
    assert lock["lockfileVersion"] == 3 and lock["packages"][""]["dependencies"] == PINS
    for name, version in PINS.items():
        entry = lock["packages"]["node_modules/" + name]
        assert entry["version"] == version and entry["integrity"].startswith("sha512-")
        assert entry["resolved"].startswith("https://registry.npmjs.org/")
    assert agent_browser.VERSION == PINS["agent-browser"]
    # Every locked package is integrity-checked; nothing is fetched from an unpinned location.
    for path, entry in lock["packages"].items():
        if path:
            assert entry.get("integrity") and entry.get("resolved", "").startswith("https://registry.npmjs.org/"), path


def test_installed_node_modules_and_the_bundled_node_are_build_outputs_not_source():
    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().splitlines()
    assert "tools/browser/node_modules/" in ignored and "tools/browser/node/" in ignored
    assert "tools/browser/.runtime-build-*/" in ignored
    assert (TOOLS / "build_runtime.py").is_file() and not (TOOLS / "node/bin/node").is_symlink()


def test_the_declared_engines_are_satisfied_by_the_pinned_bundled_node_and_npm():
    engines = json.loads((TOOLS / "package.json").read_text())["engines"]
    assert engines == {"node": ">=24.0.0", "npm": ">=11.0.0"}
    assert int(builder.NODE_VERSION.split(".")[0]) >= 24 and int(builder.NPM_VERSION.split(".")[0]) >= 11
