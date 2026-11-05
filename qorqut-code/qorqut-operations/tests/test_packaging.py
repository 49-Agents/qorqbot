"""Static guarantees that the package is independent of the deployed v1 core and of any host.

The wheel check below builds and installs the package into a disposable virtual environment, so it needs a package index
for the runtime dependencies and is opt-in: ``QORQUT_OPERATIONS_WHEEL_CHECK=1 pytest tests/test_packaging.py``.
"""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "qorqut_operations"
# The two document libraries are real dependencies of the source-document and exact-history packages; the first-party one
# (document history) is a distribution of its own that imports nothing from this package or from any host.
ALLOWED_THIRD_PARTY = {"jsonschema", "markdown_it", "qorqut_business_docs_versioning", "yaml"}


def source_files():
    return sorted(path for path in PACKAGE.rglob("*.py"))


def imported_roots(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.partition(".")[0], 0
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, (node.module or "").partition(".")[0], node.level


def test_package_imports_only_the_standard_library_its_declared_dependencies_and_itself():
    offenders = []
    for path in source_files():
        for line, root, level in imported_roots(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if level or root in ALLOWED_THIRD_PARTY or root == "qorqut_operations" or root in sys.stdlib_module_names:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line} imports {root}")
    assert offenders == []


def test_no_host_or_v1_product_reference_remains_in_the_package_source():
    forbidden = ("bazarlyk", "qorqut_code", "qorqut-code", "qorqut_roles", "qorqut_context", "qorqut-context")
    hits = []
    for path in source_files():
        text = path.read_text(encoding="utf-8").lower()
        if path.name == "safe_io.py":  # the retained v1 on-disk temporary-file prefix is the one allowed literal
            assert text.count("bazarlyk") == text.count('temporary_prefix = ".bazarlyk-"') == 1
            text = text.replace(".bazarlyk-", "")
        hits += [f"{path.relative_to(ROOT)}: {word}" for word in forbidden if word in text]
    assert hits == []


def test_the_four_context_packages_are_gone_and_nothing_names_them():
    """The Context queue, filing, graph-edit and usage packages live in qorqut-context's integration; Operations only calls the
    ``on_revision`` port it is given."""
    old = ("context_queue", "context_filing", "context_graph_edits", "context_usage")
    assert [name for name in old if (PACKAGE / name).exists()] == []
    hits = []
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [alias.name for alias in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            if isinstance(node, ast.ImportFrom):
                names += [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            hits += [f"{path.relative_to(ROOT)}:{node.lineno} {name}" for name in names
                     if any(part in old for part in name.split("."))]
    assert hits == []


def test_no_dynamic_or_lazy_import_can_reach_a_host():
    calls = []
    for path in source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                function = node.func
                name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
                if name in {"import_module", "__import__", "find_spec", "exec", "eval"}:
                    calls.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
            # A function-level import is a lazy host seam; every import here is at module level.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested = [child for child in ast.walk(node) if isinstance(child, (ast.Import, ast.ImportFrom))]
                calls += [f"{path.relative_to(ROOT)}:{child.lineno} function-level import" for child in nested]
    assert calls == []


def test_package_data_is_complete_and_runtime_dependencies_are_the_four_declared_ones():
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "qorqut-operations" and project["version"] == "1.3.0.dev0"
    assert project["dependencies"] == ["jsonschema>=4.18", "markdown-it-py>=3,<5", "PyYAML>=6,<7", "qorqut-business-docs-versioning==0.2.0"]


@pytest.mark.skipif(os.environ.get("QORQUT_OPERATIONS_WHEEL_CHECK") != "1", reason="opt-in: builds and installs a wheel")
def test_built_wheel_installs_and_imports_in_a_clean_environment(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "check_installed_wheel.py"), str(tmp_path)],
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
