"""prepare-local.py's selection of the optional Fastmail provider, with no index, build backend, network or service.

The tool's own orchestration runs for real (source checks, the first-party reconciliation, the install plan and the receipt).
Only the three steps that would reach outside the process are replaced: running a subprocess records the command (a wheel build
writes a tiny synthetic wheel instead), creating a virtual environment makes an empty directory, and listing an environment
reports the wheels the recorded install commands named. A standard preparation must need and build neither the email engine nor
the Fastmail package; ``--with-fastmail`` adds the bridge to the host environment and a separate email-engine environment.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from synthetic_wheels import make_wheel

TOOL = Path(__file__).resolve().parents[2] / "tools" / "prepare-local.py"
VERSION = "1.3.0.dev0"
# source directory under the export -> (distribution, Requires-Dist)
DISTRIBUTIONS = {
    "qorqut-agentic": ("qorqut-agentic", ()),
    "qorqut-code/qorqut-operations": ("qorqut-operations", ()),
    "qorqut-code/qorqut-context": ("qorqut-context", ("qorqut-operations==1.3.0.dev0",)),
    "dependencies/document-history": ("qorqut-business-docs-versioning", ()),
    "qorqut-code/app": ("bazarlyk-core", ("qorqut-operations==1.3.0.dev0", "qorqut-context==1.3.0.dev0")),
    "services/email-engine": ("bazarlyk-engine-email", ()),
    "extensions/qorqut-fastmail": ("qorqut-fastmail", ("bazarlyk-core==1.3.0.dev0",)),
}
BASE_COMPONENTS = {"qorqut-agentic": "qorqut-agentic", "qorqut-code": "qorqut-code", "qorqut-operations": "qorqut-code/qorqut-operations",
                   "qorqut-context": "qorqut-code/qorqut-context", "document-history": "dependencies/document-history"}
FASTMAIL_COMPONENTS = {"email-engine": "services/email-engine", "qorqut-fastmail": "extensions/qorqut-fastmail"}
BASE_KEYS = {"qorqut-agentic", "qorqut-operations", "qorqut-context", "document-history", "host"}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("prepare_local_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def touch(root, relative, text=""):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def export(root, components, *, fastmail_files):
    """A synthetic source export: the files the tool requires, and a manifest naming ``components``."""
    for relative in ("qorqut-agentic/pyproject.toml", "qorqut-code/app/pyproject.toml", "qorqut-code/app/service-constraints.txt", "qorqut-code/app/package-sources.json",
                     "qorqut-code/qorqut-operations/pyproject.toml", "qorqut-code/qorqut-context/pyproject.toml",
                     "qorqut-code/biz-dashboard/index.html", "dependencies/document-history/pyproject.toml",
                     "dependencies/email-management/plugin.yaml"):
        touch(root, relative)
    (root / "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles").mkdir(parents=True)
    if fastmail_files:
        for relative in ("extensions/qorqut-fastmail/pyproject.toml", "extensions/qorqut-fastmail/constraints.txt",
                         "services/email-engine/pyproject.toml"):
            touch(root, relative)
    manifest = [{"name": name, "destination": destination, "commit": hashlib.sha1(name.encode()).hexdigest(),
                 "tree": hashlib.sha1(b"tree-" + name.encode()).hexdigest()} for name, destination in components.items()]
    from bazarlyk.source_snapshot import component_digest
    for c in manifest:
        c["snapshot_sha256"] = component_digest(root, c, manifest)
    touch(root, "SOURCE-MANIFEST.json", json.dumps({"format": 1, "components": manifest}))
    return root


class Recorder:
    """Stands in for the subprocess, virtual-environment and listing steps of one Preparer."""

    def __init__(self, preparer, tool):
        self.commands, self.environments, self.installs = [], [], {}
        preparer.run = self.run
        preparer.make_environment = self.make_environment
        preparer.inventory = self.inventory
        self.preparer, self.tool = preparer, tool

    def run(self, name, command):
        command = [str(part) for part in command]
        self.commands.append((name, command))
        if name.startswith("build wheel"):
            source = dict(self.preparer.wheel_sources())[Path(command[-1]).name]
            distribution, requires = DISTRIBUTIONS[source]
            make_wheel(command[command.index("--wheel-dir") + 1], distribution, VERSION, requires=requires)
        elif name.startswith("install"):
            self.installs[Path(command[0]).parent.parent.name] = [part for part in command[1:] if ".whl" in part]

    def make_environment(self, directory):
        self.commands.append((f"create {directory.name}", []))
        self.environments.append(directory.name)
        (directory / "bin").mkdir(parents=True)
        (directory / "bin" / "python").write_text("")
        return directory / "bin" / "python"

    def inventory(self, python):
        names = sorted(Path(part.split("[")[0]).name.split("-")[0].replace("_", "-") for part in self.installs[python.parent.parent.name])
        return {"python": "3.12.0", "first_party": names, "packages": {}}


def prepare(tool, tmp_path, *, fastmail, present):
    components = {**BASE_COMPONENTS, **(FASTMAIL_COMPONENTS if present else {})}
    source = export(tmp_path / "export", components, fastmail_files=present)
    output = tmp_path / "prepared"
    preparer = tool.Preparer(source, output, False, False, fastmail)
    recorder = Recorder(preparer, tool)
    preparer.check_source()
    preparer.prepare()
    return recorder, json.loads((output / "PREPARATION-RECEIPT.json").read_text())


def test_a_standard_preparation_builds_and_installs_neither_the_email_engine_nor_the_fastmail_package(tool, tmp_path):
    recorder, receipt = prepare(tool, tmp_path, fastmail=False, present=False)
    assert recorder.environments == ["venv"]
    assert {wheel["key"] for wheel in receipt["wheels"]} == BASE_KEYS and sorted(receipt["environments"]) == ["venv"]
    assert receipt["selection"] == {"content": False, "paper": False, "fastmail": False}
    text = json.dumps(recorder.commands) + json.dumps(receipt)
    assert "email-engine" not in text and "fastmail" not in text.replace('"fastmail": false', "")
    assert sum(name == "install host environment" for name, _ in recorder.commands) == 1


def test_an_export_that_happens_to_carry_the_provider_still_prepares_a_standard_host_without_it(tool, tmp_path):
    recorder, receipt = prepare(tool, tmp_path, fastmail=False, present=True)
    assert recorder.environments == ["venv"] and {wheel["key"] for wheel in receipt["wheels"]} == BASE_KEYS
    assert not any("email" in name for name, _ in recorder.commands)


def test_with_fastmail_adds_the_bridge_to_the_host_environment_and_a_separate_email_engine_environment(tool, tmp_path):
    recorder, receipt = prepare(tool, tmp_path, fastmail=True, present=True)
    assert recorder.environments == ["venv", "email-engine-venv"]
    assert {wheel["key"] for wheel in receipt["wheels"]} == BASE_KEYS | {"qorqut-fastmail", "email-engine"}
    assert receipt["selection"]["fastmail"] is True and sorted(receipt["environments"]) == ["email-engine-venv", "venv"]
    assert receipt["environments"]["email-engine-venv"]["first_party"] == ["bazarlyk-engine-email"]
    assert "qorqut-fastmail" in receipt["environments"]["venv"]["first_party"]
    assert "bazarlyk-engine-email" not in receipt["environments"]["venv"]["first_party"]          # the engine is never imported by the host
    host = next(command for name, command in recorder.commands if name == "install host environment")
    pins = [host[index + 1] for index, part in enumerate(host) if part == "-c"]
    assert [Path(pin).name for pin in pins] == ["service-constraints.txt", "constraints.txt"]
    assert pins[1].endswith("extensions/qorqut-fastmail/constraints.txt")
    assert any("qorqut_fastmail" in part for part in host)
    engine = next(command for name, command in recorder.commands if name == "install email engine")
    assert engine[-1].endswith(".whl") and "bazarlyk_engine_email" in engine[-1]
    assert not any(token in name for name, _ in recorder.commands for token in ("start", "service", "configure", "enable"))


def test_selecting_fastmail_from_an_export_without_it_is_refused_before_anything_is_built(tool, tmp_path, capsys):
    source = export(tmp_path / "export", BASE_COMPONENTS, fastmail_files=False)
    output = tmp_path / "prepared"
    assert tool.main(["--source-root", str(source), "--output", str(output), "--with-fastmail"]) == 1
    error = capsys.readouterr().err
    for missing in ("extensions/qorqut-fastmail/pyproject.toml", "extensions/qorqut-fastmail/constraints.txt", "services/email-engine/pyproject.toml"):
        assert missing in error
    assert not (output / "wheels").exists() or not any((output / "wheels").iterdir())
    # and the same export is a complete standard source: nothing mail-related is required
    tool.Preparer(source, tmp_path / "other", False, False, False).check_source()
