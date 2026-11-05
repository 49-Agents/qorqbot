"""The installed-host checker against a synthetic prepared root: hand-made wheels, real local venvs, no index, no service.

One consistent root is built once; each refusal alters one thing in it and restores it. The root also proves the checker writes
nothing into it (no bytecode, no report, no repair).
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from synthetic_wheels import make_environment, make_wheel

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_installed_host.py"
# receipt key -> (distribution, source directory, owning component, environment)
WHEELS = {"qorqut-agentic": ("qorqut-agentic", "qorqut-agentic", "qorqut-agentic", "venv"), "qorqut-operations": ("qorqut-operations", "qorqut-code/qorqut-operations", "qorqut-operations", "venv"),
          "qorqut-context": ("qorqut-context", "qorqut-code/qorqut-context", "qorqut-context", "venv"),
          "document-history": ("qorqut-business-docs-versioning", "dependencies/document-history", "document-history", "venv"),
          "host": ("bazarlyk-core", "qorqut-code/app", "qorqut-code", "venv")}
# Added only by a preparation that selected the optional Fastmail provider: its host bridge and the separate email engine.
FASTMAIL_WHEELS = {"qorqut-fastmail": ("qorqut-fastmail", "extensions/qorqut-fastmail", "qorqut-fastmail", "venv"),
                   "email-engine": ("bazarlyk-engine-email", "services/email-engine", "email-engine", "email-engine-venv")}
DESTINATIONS = {"qorqut-agentic": "qorqut-agentic", "qorqut-code": "qorqut-code", "qorqut-operations": "qorqut-code/qorqut-operations", "qorqut-context": "qorqut-code/qorqut-context",
                "document-history": "dependencies/document-history"}
FASTMAIL_DESTINATIONS = {"qorqut-fastmail": "extensions/qorqut-fastmail", "email-engine": "services/email-engine"}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("check_installed_host_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_root(root, wheels, destinations, selection):
    """A synthetic prepared root holding exactly ``wheels`` (receipt key -> description) and the environments they name."""
    (root / "wheels").mkdir()
    components = [{"name": name, "destination": destination, "commit": hashlib.sha1(name.encode()).hexdigest(),
                   "tree": hashlib.sha1(b"tree-" + name.encode()).hexdigest()} for name, destination in destinations.items()]
    (root / "src").mkdir()
    from bazarlyk.source_snapshot import component_digest
    for c in components:
        (root / "src" / c["destination"]).mkdir(parents=True, exist_ok=True)
    for c in components:
        c["snapshot_sha256"] = component_digest(root / "src", c, components)
    (root / "src" / "SOURCE-MANIFEST.json").write_text(json.dumps({"format": 1, "components": components}))
    receipt_wheels, files, environments = [], {}, {"venv": []}
    environments.update({environment: [] for *_, environment in wheels.values()})
    for key, (name, source, component, environment) in wheels.items():
        stem = name.replace("-", "_")
        path = make_wheel(root / "wheels", name, "1.3.0.dev0", {f"{stem}/__init__.py": b"VALUE = 1\n", f"{stem}/resource.md": f"# {name}\n".encode()})
        owner = next(c for c in components if c["name"] == component)
        receipt_wheels.append({"key": key, "name": name, "version": "1.3.0.dev0", "file": f"wheels/{path.name}",
                               "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source": source, "component": component,
                               "commit": owner["commit"], "tree": owner["tree"]})
        files.setdefault(environment, []).append(path)
        environments[environment].append(name)
    python = {}
    for environment, wheels in files.items():
        python[environment] = make_environment(root / environment, *wheels)
    receipt = {"format": 1, "state": "prepared-not-configured", "prepared_at": "2026-10-24T00:00:00+00:00", "python": sys.version.split()[0],
               "source_manifest_sha256": hashlib.sha256((root / "src/SOURCE-MANIFEST.json").read_bytes()).hexdigest(),
               "selection": selection, "source_components": components, "wheels": receipt_wheels,
               "environments": {name: {"python": sys.version.split()[0], "first_party": sorted(names), "packages": {}}
                                for name, names in environments.items()}, "not_included": []}
    (root / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt, indent=2))
    return root


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    """The standard preparation: the host environment only, with no email engine and no Fastmail package."""
    return build_root(tmp_path_factory.mktemp("prepared"), WHEELS, DESTINATIONS, {"content": False, "paper": False, "fastmail": False})


@pytest.fixture(scope="module")
def prepared_fastmail(tmp_path_factory):
    """A preparation that selected the optional Fastmail provider (``--with-fastmail``)."""
    return build_root(tmp_path_factory.mktemp("prepared-fastmail"), {**WHEELS, **FASTMAIL_WHEELS}, {**DESTINATIONS, **FASTMAIL_DESTINATIONS},
                      {"content": False, "paper": False, "fastmail": True})


def snapshot(root):
    return {str(Path(directory, name).relative_to(root)): (os.lstat(Path(directory, name)).st_size, os.lstat(Path(directory, name)).st_mtime_ns)
            for directory, _, names in os.walk(root) for name in names}


def run(tool, prepared, tmp_path, capsys):
    output = tmp_path / "report.json"
    status = tool.main(["--prepared-root", str(prepared), "--output", str(output)])
    return status, json.loads(output.read_text()), capsys.readouterr().err


def test_a_consistent_prepared_root_passes_and_the_report_names_what_was_checked(tool, prepared, tmp_path, capsys):
    before = snapshot(prepared)
    status, report, _ = run(tool, prepared, tmp_path, capsys)
    assert status == 0 and report["passed"] is True and report["failures"] == []
    assert sorted(report["environments"]) == ["venv"]                    # a standard preparation has no email-engine environment
    assert report["environments"]["venv"]["first_party_checked"] == sorted(name for name, *_ in
        (WHEELS[key] for key in WHEELS if WHEELS[key][3] == "venv"))
    assert report["environments"]["venv"]["pip_check"]["ok"] is True
    host = report["environments"]["venv"]["distributions"]["bazarlyk-core"]
    assert host["python_files_verified"] == 1 and host["resource_files_verified"] == 1 and host["imported"] == ["bazarlyk_core"]
    assert {wheel["key"] for wheel in report["wheels"]} == set(WHEELS) and len(report["source_components"]) == len(DESTINATIONS)
    assert snapshot(prepared) == before


@pytest.fixture
def altered():
    """Alter, add or remove a prepared file for one test and put it back exactly afterwards."""
    originals = []

    def change(path, content):
        originals.append((path, path.read_bytes() if path.exists() else None))
        if content is None:
            path.unlink()
        else:
            path.write_bytes(content.encode())

    yield change
    for path, content in reversed(originals):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(content)


def site_packages(prepared, environment, name):
    return next((prepared / environment).glob(f"lib/python*/site-packages/{name}"))


def other_digest(prepared, change):
    path = prepared / "PREPARATION-RECEIPT.json"
    receipt = json.loads(path.read_text())
    receipt["wheels"][0]["sha256"] = "0" * 64
    change(path, json.dumps(receipt))


CASES = {
    "an installed resource that differs from its wheel": (
        lambda prepared, change: change(site_packages(prepared, "venv", "bazarlyk_core/resource.md"), "# edited after installation\n"),
        "bazarlyk-core: 1 wheel file(s) different from the wheel: bazarlyk_core/resource.md"),
    "a wheel whose digest differs from the receipt": (other_digest, "has SHA-256"),
    "a first-party distribution installed from a VCS origin": (
        lambda prepared, change: change(site_packages(prepared, "venv", "qorqut_context-1.3.0.dev0.dist-info/direct_url.json"),
            json.dumps({"url": "https://example.invalid/qorqut-context.git", "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})),
        "qorqut-context was installed from a direct-URL origin that is not a local wheel file"),
    "a path entry that reaches outside the environment": (
        lambda prepared, change: change(site_packages(prepared, "venv", "") / "outside.pth", str(prepared.parent)),
        "sys.path entry outside the environment"),
    "a receipt wheel that is missing": (
        lambda prepared, change: change(next((prepared / "wheels").glob("qorqut_context-*.whl")), None),
        "is missing from wheels/ or is not a plain wheel file"),
}


@pytest.mark.parametrize("case", CASES)
def test_a_mismatch_between_the_receipt_and_the_installation_is_refused_and_reported(tool, prepared, tmp_path, capsys, altered, case):
    alter, expected = CASES[case]
    alter(prepared, altered)
    status, report, error = run(tool, prepared, tmp_path, capsys)
    assert status == 1 and report["passed"] is False
    assert any(expected in failure for failure in report["failures"]), report["failures"]
    assert "FAILED" in error


def test_the_report_must_be_a_new_file_outside_the_prepared_root(tool, prepared, tmp_path, capsys):
    existing = tmp_path / "existing.json"
    existing.write_text("{}")
    assert tool.main(["--prepared-root", str(prepared), "--output", str(existing)]) == 2
    assert tool.main(["--prepared-root", str(prepared), "--output", str(prepared / "report.json")]) == 2
    assert existing.read_text() == "{}" and not (prepared / "report.json").exists()


def test_a_fastmail_preparation_is_checked_with_its_bridge_and_its_email_engine_environment(tool, prepared_fastmail, tmp_path, capsys):
    status, report, _ = run(tool, prepared_fastmail, tmp_path, capsys)
    assert status == 0 and report["passed"] is True and report["failures"] == []
    assert sorted(report["environments"]) == ["email-engine-venv", "venv"]
    assert "qorqut-fastmail" in report["environments"]["venv"]["first_party_checked"]
    assert report["environments"]["email-engine-venv"]["first_party_checked"] == ["bazarlyk-engine-email"]
    assert report["receipt"]["selection"]["fastmail"] is True
    assert {wheel["key"] for wheel in report["wheels"]} == set(WHEELS) | set(FASTMAIL_WHEELS)


def retag(path, change):
    receipt = json.loads(path.read_text())
    change(receipt)
    return json.dumps(receipt)


def test_the_selection_and_the_receipt_must_agree_about_the_email_engine(tool, prepared, prepared_fastmail, tmp_path, capsys, altered):
    # Selected but missing: the receipt claims Fastmail yet records neither its wheels nor its environment.
    path = prepared / "PREPARATION-RECEIPT.json"
    altered(path, retag(path, lambda receipt: receipt["selection"].update(fastmail=True)))
    status, report, _ = run(tool, prepared, tmp_path, capsys)
    assert status == 1
    assert any("no wheel for: email-engine, qorqut-fastmail" in failure for failure in report["failures"]), report["failures"]
    assert any("no environment for: email-engine-venv" in failure for failure in report["failures"]), report["failures"]
    # Present but not selected: a standard preparation must not carry the email engine or the bridge.
    path = prepared_fastmail / "PREPARATION-RECEIPT.json"
    altered(path, retag(path, lambda receipt: receipt["selection"].update(fastmail=False)))
    (tmp_path / "again").mkdir()
    status, report, _ = run(tool, prepared_fastmail, tmp_path / "again", capsys)
    assert status == 1
    assert any("wheels of extensions it did not select: email-engine, qorqut-fastmail" in failure for failure in report["failures"])
    assert any("records email-engine-venv without selecting Fastmail" in failure for failure in report["failures"])
