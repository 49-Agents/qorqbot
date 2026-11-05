"""The workstation build-context helper and recipe, on synthetic wheels and a synthetic preparation receipt only.

Nothing builds an image or contacts an index: the Dockerfile's two pip steps are replayed locally with ``--no-index``.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from synthetic_wheels import make_environment, make_wheel

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "business_sandbox"
HOST_REQUIRES = ("qorqut-operations==1.3.0.dev0", "qorqut-context>=1.3.0.dev0", "qorqut-business-docs-versioning==1.3.0.dev0",
                 "synthetic-third>=1", 'bazarlyk-engine-email; extra == "service"')
LOCKS = ("service-constraints.txt", "tools/browser/package.json", "tools/browser/package-lock.json",
         "tools/business_sandbox/package.json", "tools/business_sandbox/package-lock.json")


@pytest.fixture(scope="module")
def helper():
    spec = importlib.util.spec_from_file_location("stage_workstation_context_under_test", TOOLS / "stage_workstation_context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(root, host_requires=HOST_REQUIRES, omit=()):
    """A synthetic prepare-local.py output: wheels, a receipt naming them, and the prepared source copy's lock files."""
    (root / "wheels").mkdir(parents=True)
    app = root / "src/qorqut-code/app"
    for relative in LOCKS:
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text(f"{relative}\n")
    wheels = {"qorqut-operations": ("qorqut-operations", ()), "qorqut-context": ("qorqut-context", ()),
              "document-history": ("qorqut-business-docs-versioning", ()),
              "email-engine": ("bazarlyk-engine-email", ()), "host": ("bazarlyk-core", host_requires)}
    entries = []
    for key, (name, requires) in wheels.items():
        if key in omit:
            continue
        path = make_wheel(root / "wheels", name, "1.3.0.dev0", requires=requires)
        entries.append({"key": key, "name": name, "version": "1.3.0.dev0", "file": f"wheels/{path.name}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (root / "PREPARATION-RECEIPT.json").write_text(json.dumps({"format": 1, "state": "prepared-not-configured", "wheels": entries}))
    return root


def snapshot(root):
    return {str(Path(directory, name).relative_to(root)): Path(directory, name).read_bytes()
            for directory, _, names in os.walk(root) for name in names}


def test_the_hosts_base_first_party_closure_is_staged_with_exact_files_digests_and_locks(helper, tmp_path, capsys):
    prepared = prepare(tmp_path / "prepared")
    before, context = snapshot(prepared), tmp_path / "context"
    assert helper.main(["--prepared-root", str(prepared), "--output", str(context)]) == 0
    assert snapshot(prepared) == before
    # Only the base closure: not the email engine, which only a selected provider or another environment names.
    assert sorted(path.name for path in (context / "wheelhouse").glob("*.whl")) == [
        "bazarlyk_core-1.3.0.dev0-py3-none-any.whl", "qorqut_business_docs_versioning-1.3.0.dev0-py3-none-any.whl",
        "qorqut_context-1.3.0.dev0-py3-none-any.whl", "qorqut_operations-1.3.0.dev0-py3-none-any.whl"]
    lines = (context / "wheelhouse/first-party.txt").read_text().splitlines()
    assert len(lines) == 4
    for line in lines:
        name, _, rest = line.partition(" @ ")
        url, _, digest = rest.partition("#sha256=")
        wheel = context / "wheelhouse" / url.removeprefix("file://" + helper.IMAGE_WHEELS + "/")
        assert digest == hashlib.sha256(wheel.read_bytes()).hexdigest() and re.fullmatch(r"(qorqut|bazarlyk)-[a-z-]+", name)
    assert (context / "wheelhouse/third-party.txt").read_text() == "synthetic-third>=1\n"
    assert all((context / relative).read_text() == f"{relative}\n" for relative in LOCKS)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["context", "prepared"]


def test_the_staged_files_install_as_the_dockerfile_does_and_a_changed_wheel_is_refused_without_an_index(helper, tmp_path):
    context = tmp_path / "context"
    assert helper.main(["--prepared-root", str(prepare(tmp_path / "prepared")), "--output", str(context)]) == 0
    index = tmp_path / "index"
    index.mkdir()
    make_wheel(index, "synthetic-third", "1.0")
    wheelhouse = context / "wheelhouse"
    local = tmp_path / "first-party.txt"
    local.write_text((wheelhouse / "first-party.txt").read_text().replace("file://" + helper.IMAGE_WHEELS, wheelhouse.as_uri()))

    def pip(python, *arguments):
        return subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", *arguments], capture_output=True, text=True)

    python = make_environment(tmp_path / "environment")
    assert pip(python, "install", "--no-index", "--no-deps", "--requirement", str(local)).returncode == 0
    assert pip(python, "install", "--no-index", "--find-links", str(index), "--requirement", str(wheelhouse / "third-party.txt")).returncode == 0
    assert pip(python, "check").returncode == 0
    installed = {item["name"] for item in json.loads(pip(python, "list", "--format=json").stdout)}
    assert {"bazarlyk-core", "qorqut-operations", "qorqut-context", "qorqut-business-docs-versioning", "synthetic-third"} <= installed
    assert not {"qorqut-learning", "bazarlyk-engine-email"} & installed

    altered = next(wheelhouse.glob("qorqut_context-*.whl"))
    altered.write_bytes(altered.read_bytes() + b"\0")
    refusal = pip(make_environment(tmp_path / "second"), "install", "--no-index", "--no-deps", "--requirement", str(local))
    assert refusal.returncode != 0 and "HASH" in refusal.stderr.upper()


REFUSALS = {
    "a wheel file missing from wheels/": (lambda root: next((root / "wheels").glob("qorqut_context-*.whl")).unlink(), {}, "is not under wheels/"),
    "a wheel whose digest differs from the receipt": (lambda root: next((root / "wheels").glob("qorqut_operations-*.whl")).write_bytes(b"x"), {},
                                                      "does not have the SHA-256 the receipt records"),
    "a first-party requirement without a prepared wheel": (lambda root: None, {"omit": ("document-history",)},
                                                           "requires qorqut-business-docs-versioning, but the receipt has no prepared wheel"),
    "a first-party requirement by URL": (lambda root: None, {"host_requires": ("qorqut-context @ git+https://example.invalid/context.git@" + "a" * 40,)},
                                         "by URL"),
    "a local version that does not satisfy the requirement": (lambda root: None, {"host_requires": ("qorqut-operations==2.0",)},
                                                              "but the prepared wheel is version 1.3.0.dev0"),
    "a third-party requirement by URL": (lambda root: None, {"host_requires": ("synthetic-third @ https://example.invalid/t.whl",)}, "by URL"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_a_missing_mismatched_or_unbound_first_party_wheel_is_refused_before_anything_is_written(helper, tmp_path, capsys, case):
    alter, options, message = REFUSALS[case]
    prepared = prepare(tmp_path / "prepared", **options)
    alter(prepared)
    before = snapshot(prepared)
    assert helper.main(["--prepared-root", str(prepared), "--output", str(tmp_path / "context")]) == 1
    assert message in capsys.readouterr().err
    assert snapshot(prepared) == before and sorted(path.name for path in tmp_path.iterdir()) == ["prepared"]


def test_the_output_must_be_new_and_outside_the_prepared_root(helper, tmp_path):
    prepared = prepare(tmp_path / "prepared")
    (tmp_path / "taken").mkdir()
    assert helper.main(["--prepared-root", str(prepared), "--output", str(tmp_path / "taken")]) == 1
    assert helper.main(["--prepared-root", str(prepared), "--output", str(prepared / "context")]) == 1
    assert not (prepared / "context").exists()


def test_the_dockerfile_installs_first_party_packages_only_from_the_staged_local_wheels():
    text = (TOOLS / "workstation.Dockerfile").read_text()
    instructions = re.sub(r"\\\n\s*", " ", re.sub(r"(?m)^#.*$", "", text))
    spec = importlib.util.spec_from_file_location("stage_workstation_context_static", TOOLS / "stage_workstation_context.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert f"COPY wheelhouse {helper.IMAGE_WHEELS}" in instructions and "COPY service-constraints.txt /opt/qorqut/" in instructions
    commands = [command.strip() for command in instructions.split("&&")]
    first = next(i for i, command in enumerate(commands) if "first-party.txt" in command)
    third = next(i for i, command in enumerate(commands) if "third-party.txt" in command)
    assert first < third and "--no-index" in commands[first] and "--no-deps" in commands[first]
    assert f"--requirement {helper.IMAGE_WHEELS}/first-party.txt" in commands[first]
    assert "--no-index" not in commands[third] and "--constraint /opt/qorqut/service-constraints.txt" in commands[third]
    assert commands[third + 1].splitlines()[0] == "/opt/qorqut/venv/bin/pip check"
    # No source tree or project directory is installed any more, and no other index or VCS source exists.
    assert not re.search(r"COPY (pyproject\.toml|bazarlyk)\b", instructions) and not re.search(r"pip install[^&]*\s/opt/qorqut(\s|$)", instructions)
    assert "git+" not in instructions and "--extra-index-url" not in instructions and "--index-url" not in instructions
    for line in ("FROM node:22.22.0-bookworm-slim@sha256:", "FROM python:3.12.12-slim-bookworm@sha256:", "npm ci --prefix /opt/qorqut-tools",
                 "npm ci --prefix /opt/qorqut-browser --omit=dev --ignore-scripts", "USER agent"):
        assert line in instructions
