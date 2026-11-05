import json
from pathlib import Path
import subprocess
import sys

import pytest

from bazarlyk.bootstrap import diagnostics as d
from tools.prepare_agentic_helper_manifest import MODULE, prepare


@pytest.fixture
def stable(tmp_path, monkeypatch):
    executable = tmp_path / "bin/agent-helper"
    executable.parent.mkdir()
    executable.write_text("fixture")
    executable.chmod(0o700)
    selected = tmp_path / "runtime/agent-tools/current"
    selected.mkdir(parents=True)
    manifest = selected / "manifest.json"
    data = {"python": sys.executable, "code": str(tmp_path), "modules": list(d.HELPER_MODULES)}
    manifest.write_text(json.dumps(data))
    calls = []
    def imports(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0, json.dumps([str(executable)]), "")
    monkeypatch.setattr(d.subprocess, "run", imports)
    d._verified_imports.cache_clear()
    yield {"helper_runtime": {"executable": str(executable), "stable": True}}, manifest, data, calls
    d._verified_imports.cache_clear()


def test_stable_allowlist_checked_even_with_cached_import_success(stable):
    projection, manifest, data, calls = stable
    assert d.prepared_helpers(projection)["ready"]
    data["modules"].remove(MODULE)
    manifest.write_text(json.dumps(data))
    assert d.prepared_helpers(projection) == {
        "ready": False, "reason": "helper_module_unsupported", "missing_modules": [MODULE]}
    data["modules"].append(MODULE)
    manifest.write_text(json.dumps(data))
    assert d.prepared_helpers(projection)["ready"]
    assert len(calls) == 1


@pytest.mark.parametrize("modules", [None, MODULE, [42], {MODULE: True}])
def test_stable_malformed_allowlist_fails_before_probe(stable, modules):
    projection, manifest, data, calls = stable
    data["modules"] = modules
    manifest.write_text(json.dumps(data))
    assert d.prepared_helpers(projection) == {"ready": False, "reason": "helper_manifest_invalid"}
    assert not calls


def test_stable_complete_allowlist_still_requires_successful_import(stable, monkeypatch):
    projection, _, _, _ = stable
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "missing"))
    assert d.prepared_helpers(projection)["reason"] == "helper_import_failed"


@pytest.fixture
def source(tmp_path):
    code = tmp_path / "code"
    module = code / "bazarlyk/agentic"
    module.mkdir(parents=True)
    # A complete ordinary fixture package: the initializers make the staged tree the imported regular package even
    # when the interpreter also has an installed ``bazarlyk``, so both the success and the failure probe read it.
    (code / "bazarlyk/__init__.py").write_text("")
    (code / "bazarlyk/agentic/__init__.py").write_text("")
    (module / "client.py").write_text("VALUE = 1\n")
    data = {"python": sys.executable, "code": str(code), "modules": ["existing.helper"],
            "components": {"core": {"path": str(code)}}, "binding_protocol": 1}
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps(data))
    return manifest, data


def test_staging_preserves_source_and_adds_only_agentic(source, tmp_path):
    manifest, data = source
    # The child interpreter must import the selected fixture's own client module, never an installed ``bazarlyk``.
    client = Path(data["code"]) / "bazarlyk/agentic/client.py"
    witness = tmp_path / "imported-from"
    client.write_text(f"from pathlib import Path\nPath({str(witness)!r}).write_text(__file__)\nVALUE = 1\n")
    original = manifest.read_bytes()
    output = prepare(manifest, tmp_path / "new")
    assert witness.read_text() == str(client)
    expected = {**data, "modules": [*data["modules"], MODULE]}
    assert json.loads(output.read_text()) == expected
    assert manifest.read_bytes() == original
    assert "arbitrary.module" not in expected["modules"]
    second = prepare(output, tmp_path / "second")
    assert json.loads(second.read_text()) == expected


def test_staging_import_failure_creates_no_output(source, tmp_path):
    manifest, data = source
    (Path(data["code"]) / "bazarlyk/agentic/client.py").write_text("raise ImportError('missing dependency')\n")
    destination = tmp_path / "new"
    with pytest.raises(subprocess.CalledProcessError) as failure:
        prepare(manifest, destination)
    # The failure is the fixture module's own ImportError raised in a real child interpreter, not a mocked result.
    assert b"ImportError: missing dependency" in failure.value.stderr
    assert str(Path(data["code"]) / "bazarlyk/agentic/client.py") in failure.value.stderr.decode()
    assert not destination.exists()


def test_staging_refuses_existing_destination_and_source_overwrite(source, tmp_path):
    manifest, _ = source
    original = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(manifest, manifest.parent)
    assert manifest.read_bytes() == original
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        prepare(manifest, destination)
