"""Fresh-instance initializer: refusals before any write, one seeded core instance, and a startup command that accepts it.

The prepared output is a synthetic copy of the layout ``tools/prepare-local.py`` writes (receipt, source manifest and the
component directories, with the real role sources); nothing leaves ``tmp_path`` and no service or native session starts."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bazarlyk.bootstrap import initialize as init
from bazarlyk.service import main as service_main
from host_foundation_support import APP

COMPONENTS = {"qorqut-code": "qorqut-code", "qorqut-operations": "qorqut-code/qorqut-operations",
              "qorqut-context": "qorqut-code/qorqut-context", "biz-dashboard": "qorqut-code/biz-dashboard",
              "qorqut-agentic": "qorqut-agentic"}


def prepare(tmp_path, name="prepared", *, optional=()):
    root = tmp_path / name
    src = root / "src"
    from agentic_installation_support import snapshot
    snapshot(src, optional=optional)
    for destination in COMPONENTS.values():
        (src / destination).mkdir(parents=True, exist_ok=True)
    (src / "qorqut-code" / "app").mkdir(parents=True, exist_ok=True)
    shutil.copy(APP / "package-sources.json", src / "qorqut-code" / "app" / "package-sources.json")
    from bazarlyk.source_snapshot import component_digest
    components = json.loads((src / "SOURCE-MANIFEST.json").read_text())["components"]
    names = {c["name"] for c in components}
    components += [{"name": key, "destination": value, "commit": "0" * 40, "tree": "1" * 40}
                   for key, value in COMPONENTS.items() if key not in names]
    for c in components:
        c["snapshot_sha256"] = component_digest(src, c, components)
    (src / "SOURCE-MANIFEST.json").write_text(json.dumps({"format": 1, "components": components}))
    (root / "PREPARATION-RECEIPT.json").write_text(json.dumps({
        "source_manifest_sha256": hashlib.sha256((src / "SOURCE-MANIFEST.json").read_bytes()).hexdigest(),
        "format": 1, "state": "prepared-not-configured", "prepared_at": "2026-10-24T00:00:00+00:00", "source_components": components,
        "environments": {"venv": {"packages": {"bazarlyk-core": "1.3.0.dev0"}}}}))
    return root


REAL_REQUIRE_INSTALLED = init.require_installed


def no_environment(prepared, receipt, factories):
    """The synthetic prepared output has no installed environment; ``require_installed`` has its own case below."""


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(init, "require_installed", no_environment)


@pytest.fixture(scope="module")
def initialized(tmp_path_factory, request):
    tmp_path = tmp_path_factory.mktemp("initialized")
    prepared = prepare(tmp_path)
    instance = tmp_path / "instance"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(init, "require_installed", no_environment)
        record = init.initialize(prepared, instance, owner_name="Ada")
    return prepared, instance, record


def tree(path):
    return sorted((str(item.relative_to(path)), hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else "")
                  for item in Path(path).rglob("*"))


def rows(instance, sql, *arguments):
    import sqlite3
    connection = sqlite3.connect(instance / "operations" / ".runtime" / "operations.sqlite3")
    try:
        return connection.execute(sql, arguments).fetchall()
    finally:
        connection.close()


def test_one_core_instance_with_the_owner_the_core_roles_and_the_coordinator(initialized):
    from bazarlyk.bootstrap.initialize import AGENT_CAPABILITIES
    prepared, instance, record = initialized
    assert record["extensions"] == []
    assert rows(instance, "SELECT id, kind, display_name FROM principals WHERE kind='human'") == [(record["owner_principal_id"], "human", "Ada")]
    assert rows(instance, "SELECT installation_id, owner_principal_id, management_coordinator_id FROM installation") == [
        (record["installation_id"], record["owner_principal_id"], record["agents"]["ordubashy"])]
    profiles = {name: json.loads(profile) for name, profile in rows(instance, "SELECT name, profile_json FROM agent_profiles")}
    assert set(profiles) == {"ordubashy", "learning-applier"}                      # no worker, no other registration
    for role, profile in profiles.items():
        assert profile["agent_id"] == record["agents"][role] and profile["status"] == "active" and profile["plugins"] == []
        assert profile["role"] == record["role_pins"][role]
        assert profile["scope"] == {"kind": "ecosystem", "allowed_business_ids": [],
                                    "global_capabilities": sorted(AGENT_CAPABILITIES[role])}
    assert rows(instance, "SELECT home_business_id FROM agent_profiles") == [(None,), (None,)]
    # The explicit installation prerequisite for background programs, including the scheduled due program.
    system_workers = {name for (name,) in rows(instance, "SELECT display_name FROM principals WHERE kind='system'")}
    assert "overseer.extraction_due worker" in system_workers and "learnings.extract worker" in system_workers
    assert set(record["role_pins"]) == {"ordubashy", "global-worker", "learning-applier"}      # the worker role is published, not registered
    assert sorted(path for (path,) in rows(instance, "SELECT path FROM documents WHERE kind='shared_instruction'")) == [
        "files/instructions/agent-foundation.md", "files/instructions/ecosystem.md", "files/instructions/operating/ecosystem-agent.md"]
    for table in ("businesses", "tasks", "routines", "jobs", "inbox_events", "workspace_presentations"):
        assert rows(instance, f"SELECT COUNT(*) FROM {table}") == [(0,)], table
    names = {name for (name,) in rows(instance, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {name for name in names if name.startswith(("outreach_", "content_session", "integration_", "campaign", "milestone",
                                                           "personal_context", "daily_report", "ledger", "products"))}


def git(operations, *arguments, check=True):
    """The test's own Git over the operations root: no ambient or global configuration."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(operations), *arguments], env=environment, capture_output=True, text=True)
    assert not check or result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_fresh_setup_provisions_the_operations_repository_with_one_empty_bootstrap_commit(initialized):
    prepared, instance, record = initialized
    operations = instance / "operations"
    commit = git(operations, "rev-parse", "--verify", "HEAD^{commit}")                 # HEAD is never unborn
    assert record["history_repository"] == {"path": str(operations), "created": True, "branch": "qorqut-instance", "bootstrap_commit": commit}
    assert json.loads((instance / "config" / "initialization-receipt.json").read_text())["history_repository"] == record["history_repository"]
    # The neutral branch, one parentless commit, Git's empty tree: no instance content (database, instructions, role pins) in it.
    assert git(operations, "symbolic-ref", "HEAD") == "refs/heads/qorqut-instance"
    assert git(operations, "for-each-ref", "--format=%(refname)") == "refs/heads/qorqut-instance"
    assert git(operations, "rev-list", "--count", "--all") == "1" and git(operations, "rev-list", "--parents", "HEAD") == commit
    assert git(operations, "rev-parse", "HEAD^{tree}") == "4b825dc642cb6eb9a060e54bf8d69288fbee4904" and git(operations, "ls-tree", "-r", "-t", "HEAD") == ""
    # Nothing was staged or scanned; the repository is its own top level, with no remote; neither publisher's ref exists yet.
    assert git(operations, "ls-files") == "" and not (operations / ".git" / "index").exists()
    assert git(operations, "rev-parse", "--show-toplevel") == str(operations) and git(operations, "remote") == ""
    assert (operations / ".runtime" / "operations.sqlite3").is_file() and not (instance / ".git").exists()
    for ref in ("refs/heads/qorqut-tables", "refs/heads/bazarlyk-history"):
        assert git(operations, "rev-parse", "--verify", "--quiet", ref, check=False) == ""
    # The operational snapshot repository stays a separate store with its own history branch.
    assert (operations / ".runtime" / "history.git" / "HEAD").read_text() == "ref: refs/heads/history\n"
    assert not (operations / ".runtime" / "history.git" / "refs" / "heads" / "qorqut-instance").exists()
    assert [str(path) for path in (operations / ".git").rglob("*") if stat.S_IMODE(path.lstat().st_mode) & 0o077] == []


def test_the_command_reports_the_repository_it_created(tmp_path, monkeypatch, capsys):
    record = {"instance_root": "/i", "owner_principal_id": "o", "installation_id": "n", "extensions": [], "receipt_file": "/i/r",
              "service_command": ["python"], "history_repository": {"path": "/i/operations", "created": True, "branch": "qorqut-instance",
                                                                       "bootstrap_commit": "a" * 40}}
    monkeypatch.setattr(init, "initialize", lambda *args, **kwargs: record)
    assert init.main(["--prepared-root", str(tmp_path), "--instance-root", str(tmp_path / "i")]) == 0
    assert f"operations repository: /i/operations (branch qorqut-instance, empty bootstrap commit {'a' * 40})" in capsys.readouterr().out


def test_a_failed_repository_setup_leaves_the_incomplete_instance_without_a_receipt(tmp_path, monkeypatch, capsys):
    prepared, instance = prepare(tmp_path), tmp_path / "instance"
    seeded = {"installation_id": "i", "owner_principal_id": "o", "agents": {}, "coordinator_id": "c", "role_pins": {}}
    calls = []
    monkeypatch.setattr(init, "seed_instance", lambda *args: seeded)

    def refuse(operations):
        calls.append(operations)
        raise init.DomainError("history_repository_failed", "Git failed the history repository setup")

    monkeypatch.setattr(init, "provision_history_repository", refuse)
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    error = capsys.readouterr().err
    assert "history repository setup" in error and "left at" in error and calls == [instance / "operations"]
    assert (instance / "config" / "owner-token").is_file() and not (instance / "config" / "initialization-receipt.json").exists()
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1       # no resume, no repair
    assert "not an empty directory" in capsys.readouterr().err and len(calls) == 1


def test_an_existing_operations_repository_makes_the_destination_non_empty_and_is_left_alone(tmp_path, capsys):
    prepared, instance = prepare(tmp_path), tmp_path / "instance"
    (instance / "operations").mkdir(parents=True)
    git(instance / "operations", "init", "--quiet", "--initial-branch=owner-branch")                  # an unborn repository: not repaired
    before = tree(tmp_path)
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    assert "not an empty directory" in capsys.readouterr().err and tree(tmp_path) == before
    assert git(instance / "operations", "symbolic-ref", "HEAD") == "refs/heads/owner-branch"
    assert git(instance / "operations", "rev-parse", "--verify", "--quiet", "HEAD", check=False) == ""


def test_private_permissions_and_the_receipt_never_hold_the_token(initialized):
    prepared, instance, record = initialized
    config = instance / "config"
    assert stat.S_IMODE(instance.stat().st_mode) == 0o700 and stat.S_IMODE(config.stat().st_mode) == 0o700
    for name in ("extensions.json", "owner-token", "initialization-receipt.json"):
        assert stat.S_IMODE((config / name).stat().st_mode) == 0o600, name
    # Everything the production primitives created is owner-only too, not only config/.
    assert [str(path) for path in instance.rglob("*") if stat.S_IMODE(path.lstat().st_mode) & 0o077] == []
    token = (config / "owner-token").read_text()
    assert len(token) >= 32 and all(token not in path.read_text() for path in (config / "initialization-receipt.json", config / "extensions.json"))
    assert token not in " ".join(record["service_command"]) and token not in json.dumps(record)
    receipt = json.loads((config / "initialization-receipt.json").read_text())
    assert receipt["state"] == "initialized" and receipt["owner_principal_id"] == record["owner_principal_id"]
    assert receipt["workspace"] == str(prepared / "src") and receipt["extensions"] == []
    assert receipt["components"]["roles"] == str(prepared / "src/qorqut-agentic/src/qorqut_agentic/resources/defaults")
    assert receipt["preparation"]["receipt_sha256"] == hashlib.sha256((prepared / "PREPARATION-RECEIPT.json").read_bytes()).hexdigest()
    assert json.loads((config / "extensions.json").read_text()) == {"extensions": []}


def test_a_second_initialization_is_refused_and_changes_nothing(initialized, capsys):
    prepared, instance, _ = initialized
    before = tree(instance)
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    assert "not an empty directory" in capsys.readouterr().err and tree(instance) == before


def test_the_printed_command_starts_the_service_assembly_for_this_instance(initialized, monkeypatch):
    prepared, instance, record = initialized
    command = record["service_command"]
    assert command[:3] == [str(prepared / "venv" / "bin" / "python"), "-m", "bazarlyk.service"]
    seen = {}
    monkeypatch.setattr(service_main, "assemble", lambda *args, **kwargs: seen.update(kwargs) or SimpleNamespace(state=None))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: seen.update(served=True)))
    assert service_main.main(command[3:]) == 0
    assert seen["bootstrap_token"] == (instance / "config" / "owner-token").read_text() and seen["served"]
    assert seen["extensions"].ids == () and seen["paths"].instance_root == instance
    tokenless = [part for part in command[3:] if part not in ("--token-file", str(instance / "config" / "owner-token"))]
    assert service_main.main([*tokenless, "--token", "x" * 40, "--token-file", str(instance / "config" / "owner-token")]) == 1
    (instance / "config" / "owner-token").chmod(0o640)
    try:
        assert service_main.main(command[3:]) == 1                                  # a group-readable token file is refused
    finally:
        (instance / "config" / "owner-token").chmod(0o600)


@pytest.mark.parametrize("case", ["root", "relative", "nonempty", "file", "symlink", "ancestor", "no-parent", "inside-prepared",
                                  "contains-prepared"])
def test_refusals_happen_before_anything_is_written(tmp_path, capsys, case):
    prepared = prepare(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "keep").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "other")
    instance = {"root": Path("/"), "relative": Path("instance"), "nonempty": tmp_path / "other", "file": tmp_path / "other" / "keep",
                "symlink": tmp_path / "link", "ancestor": tmp_path / "link" / "new", "no-parent": tmp_path / "absent" / "new",
                "inside-prepared": prepared / "src" / "instance", "contains-prepared": tmp_path}[case]
    before = tree(tmp_path)
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    assert "refused, nothing was written" in capsys.readouterr().err
    assert tree(tmp_path) == before and not (tmp_path / "absent").exists()


def test_an_empty_existing_directory_is_accepted_and_made_private(tmp_path):
    prepared, instance = prepare(tmp_path), tmp_path / "instance"
    instance.mkdir(mode=0o755)
    init.initialize(prepared, instance)
    assert stat.S_IMODE(instance.stat().st_mode) == 0o700


@pytest.mark.parametrize("breakage", ["receipt-state", "manifest-mismatch", "missing-component", "bad-owner-name"])
def test_an_unprepared_or_inconsistent_output_is_refused(tmp_path, capsys, breakage):
    prepared, name = prepare(tmp_path), "Owner"
    receipt = json.loads((prepared / "PREPARATION-RECEIPT.json").read_text())
    if breakage == "receipt-state":
        receipt["state"] = "started"
        (prepared / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt))
    elif breakage == "manifest-mismatch":
        receipt["source_components"][0]["commit"] = "f" * 40
        (prepared / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt))
    elif breakage == "missing-component":
        shutil.rmtree(prepared / "src" / "qorqut-code" / "biz-dashboard")
    else:
        name = " "
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(tmp_path / "instance"), "--owner-name", name]) == 1
    assert "nothing was written" in capsys.readouterr().err and not (tmp_path / "instance").exists()


def test_a_failure_after_the_first_write_leaves_the_incomplete_instance_and_retries_nothing(tmp_path, monkeypatch, capsys):
    prepared, instance = prepare(tmp_path), tmp_path / "instance"
    calls = []
    monkeypatch.setattr(init, "seed_instance", lambda *args: calls.append(args) or (_ for _ in ()).throw(RuntimeError("boom")))
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    error = capsys.readouterr().err
    assert "boom" in error and "left at" in error and len(calls) == 1
    assert (instance / "config" / "owner-token").is_file() and not (instance / "config" / "initialization-receipt.json").exists()
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1       # no resume
    assert "not an empty directory" in capsys.readouterr().err


def test_a_selected_extension_that_is_not_installed_is_refused_before_any_write(tmp_path, capsys):
    prepared, instance = prepare(tmp_path), tmp_path / "instance"
    configuration = tmp_path / "extensions.json"
    configuration.write_text(json.dumps({"extensions": [{"factory": "no_such_extension_package.extension:create", "config": {}}]}))
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance), "--extensions-config", str(configuration)]) == 1
    assert "not installed" in capsys.readouterr().err and not instance.exists()


def test_a_selected_extension_is_stored_privately_and_composes_its_own_storage(tmp_path):
    pytest.importorskip("qorqut_content.extension")
    prepared, instance = prepare(tmp_path, optional=("content",)), tmp_path / "instance"
    configuration = tmp_path / "extensions.json"
    configuration.write_text(json.dumps({"extensions": [{"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": False}}]}))
    record = init.initialize(prepared, instance, extensions_config=configuration)
    assert record["extensions"] == ["content"]
    assert json.loads((instance / "config" / "extensions.json").read_text()) == json.loads(configuration.read_text())
    assert stat.S_IMODE((instance / "config" / "extensions.json").stat().st_mode) == 0o600
    assert rows(instance, "SELECT name FROM sqlite_master WHERE name='content_session_invocations'")
    assert "--extensions-config" in record["service_command"]


class FakeDistribution:
    def __init__(self, version, origin):
        self.version, self.origin = version, origin

    def read_text(self, name):
        return None if name != "direct_url.json" or self.origin is None else json.dumps(self.origin)


def installed_host(tmp_path, monkeypatch):
    """A prepared output whose host environment holds exactly the receipt wheel of ``bazarlyk-core`` (version, wheel file, origin)."""
    prepared = prepare(tmp_path)
    wheel = prepared / "wheels" / "bazarlyk_core-1.3.0.dev0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"the receipt wheel")
    entry = {"name": "bazarlyk-core", "version": "1.3.0.dev0", "file": f"wheels/{wheel.name}", "sha256": hashlib.sha256(b"the receipt wheel").hexdigest()}
    receipt = {"wheels": [entry], "environments": {"venv": {"first_party": ["bazarlyk-core"]}}}
    origin = {"url": wheel.as_uri(), "archive_info": {"hashes": {"sha256": entry["sha256"]}}}
    environment = prepared / "venv"
    monkeypatch.setattr(init.sys, "prefix", str(environment))
    monkeypatch.setattr(init.metadata, "packages_distributions", lambda: {"bazarlyk": ["bazarlyk-core"], "qorqut_agentic": ["bazarlyk-core"]})
    monkeypatch.setattr(init.metadata, "distribution", lambda name: FakeDistribution("1.3.0.dev0", origin))
    monkeypatch.setattr(init.importlib.util, "find_spec", lambda module: SimpleNamespace(origin=str(environment / "lib/site-packages/bazarlyk/__init__.py")))
    return prepared, receipt, wheel, origin


def test_initialization_requires_the_prepared_environment_and_the_receipt_wheels(tmp_path, monkeypatch):
    prepared, receipt, wheel, origin = installed_host(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "bazarlyk", SimpleNamespace(__file__=str(prepared / "venv/lib/site-packages/bazarlyk/__init__.py")))
    monkeypatch.setitem(sys.modules, "qorqut_agentic", SimpleNamespace(__file__=str(prepared / "venv/lib/site-packages/qorqut_agentic/__init__.py")))
    REAL_REQUIRE_INSTALLED(prepared, receipt, [])                                    # the healthy case
    with pytest.raises(init.InitializationError, match="no host environment"):
        REAL_REQUIRE_INSTALLED(prepared, {"environments": {"venv": {"packages": {}}}}, [])
    with pytest.raises(init.InitializationError, match="bazarlyk is not installed from the prepared environment"):
        REAL_REQUIRE_INSTALLED(prepared, {**receipt, "environments": {"venv": {"first_party": []}}}, [])
    monkeypatch.setattr(init.sys, "prefix", str(tmp_path / "another-venv"))
    with pytest.raises(init.InitializationError, match="prepared host environment"):
        REAL_REQUIRE_INSTALLED(prepared, receipt, [])
    monkeypatch.setattr(init.sys, "prefix", str(prepared / "venv"))
    monkeypatch.setattr(init.importlib.util, "find_spec", lambda module: SimpleNamespace(origin=str(prepared / "src" / module / "__init__.py")))
    with pytest.raises(init.InitializationError, match="imported from the source copy"):
        REAL_REQUIRE_INSTALLED(prepared, receipt, [])
    monkeypatch.setattr(init.importlib.util, "find_spec", lambda module: SimpleNamespace(origin=str(tmp_path / "elsewhere" / module / "__init__.py")))
    with pytest.raises(init.InitializationError, match="outside the prepared environment"):
        REAL_REQUIRE_INSTALLED(prepared, receipt, [])


def test_a_same_version_installation_from_another_origin_is_refused_before_any_instance_is_written(tmp_path, monkeypatch, capsys):
    prepared, receipt, wheel, origin = installed_host(tmp_path, monkeypatch)
    other = wheel.with_name("bazarlyk_core-1.3.0.dev0-other-build.whl")
    other.write_bytes(b"another build with the same version")
    monkeypatch.setattr(init.metadata, "distribution",
                        lambda name: FakeDistribution("1.3.0.dev0", {"url": other.as_uri(), "archive_info": origin["archive_info"]}))
    with pytest.raises(init.InitializationError, match="not from the receipt wheel"):
        REAL_REQUIRE_INSTALLED(prepared, receipt, [])
    # The same refusal through the command, which has written nothing: the check precedes the destination.
    monkeypatch.setattr(init, "require_installed", REAL_REQUIRE_INSTALLED)
    monkeypatch.setattr(init, "load_prepared", lambda root: (receipt, {}, {}))
    instance = tmp_path / "instance"
    assert init.main(["--prepared-root", str(prepared), "--instance-root", str(instance)]) == 1
    assert "not from the receipt wheel" in capsys.readouterr().err and not instance.exists()
    for variant in ({"url": wheel.as_uri(), "dir_info": {"editable": True}}, {"url": "https://example.test/x.whl"}, None):
        monkeypatch.setattr(init.metadata, "distribution", lambda name, variant=variant: FakeDistribution("1.3.0.dev0", variant))
        with pytest.raises(init.InitializationError, match="editable, VCS or other origin|no direct-URL record"):
            REAL_REQUIRE_INSTALLED(prepared, receipt, [])
    wheel.write_bytes(b"a rebuilt wheel with the same name")
    monkeypatch.setattr(init.metadata, "distribution", lambda name: FakeDistribution("1.3.0.dev0", origin))
    with pytest.raises(init.InitializationError, match="no longer has its recorded SHA-256"):
        REAL_REQUIRE_INSTALLED(prepared, receipt, [])


@pytest.mark.parametrize("tamper", ["source", "legacy_manifest", "duplicate_component", "prepared_digest"])
def test_export_integrity_refusal_precedes_first_target_write(tmp_path, tamper):
    prepared = prepare(tmp_path)
    manifest_file = prepared / "src/SOURCE-MANIFEST.json"
    receipt_file = prepared / "PREPARATION-RECEIPT.json"
    manifest = json.loads(manifest_file.read_text())
    receipt = json.loads(receipt_file.read_text())
    if tamper == "source":
        target = prepared / "src/qorqut-code/app/bazarlyk/agentic/assets/toolkit.md"
        target.write_text("Changed after preparation")
    elif tamper == "legacy_manifest":
        manifest["components"][0].pop("snapshot_sha256")
        manifest_file.write_text(json.dumps(manifest))
    elif tamper == "duplicate_component":
        manifest["components"].append(dict(manifest["components"][0]))
        manifest_file.write_text(json.dumps(manifest))
    else:
        receipt["source_manifest_sha256"] = "0" * 64
        receipt_file.write_text(json.dumps(receipt))
    instance = tmp_path / "refused"
    with pytest.raises(init.InitializationError):
        init.initialize(prepared, instance)
    assert not instance.exists()
