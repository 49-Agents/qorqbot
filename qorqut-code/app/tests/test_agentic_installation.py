"""Real assembled layer/view/composer and strict host binding, with synthetic data."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.agentic.installation import open_installation
from bazarlyk.agentic.support import digest
from host_foundation_support import build_instance, create_registered_global, register_sources


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, _ = build_instance(tmp_path_factory.mktemp("assembled"))
    register_sources(instance, instance.business, "first")
    manager = create_registered_global(instance, "ordubashy")
    applier = create_registered_global(instance, "learning-applier")
    yield instance, manager, applier
    instance.runtime.domains.shared_context.close()
    instance.runtime.release_instance()
    instance.database.close()


def edit(layer, relative, content, actor="fixture-owner"):
    path = layer.root / relative
    # Protected built-ins can change on disk by an offline owner, not through the agent editing API.
    path.write_text(content)
    layer.changes.scan()
    return layer.changes.list()["changes"][-1]


def test_actual_composer_has_one_toolkit_and_uses_current_instructions(host):
    instance, manager, _ = host
    runtime = instance.runtime
    assert runtime.resolver.agentic_view is runtime.agentic_view
    assert runtime.agentic_view.layer is runtime.agentic_layer
    assert runtime.registry.get("agentic_access").effect_kind == "read"
    before = runtime.resolver.resolve_agent_config(Principal(manager, "agent")).to_dict()
    # The text renderer and composer are the actual merged implementations.
    text = before["mandatory_context"]["rendered"]
    assert text.count("## Agentic Toolkit") == 1
    layer = runtime.agentic_layer
    relative = "operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/inbox-session-note.md"
    edit(layer, relative, "Current synthetic inbox note.", manager)
    after = runtime.resolver.resolve_agent_config(Principal(manager, "agent")).to_dict()
    assert after["inbox_session_note"] == "Current synthetic inbox note."
    assert after["effective_config_id"] != before["effective_config_id"]
    assert after["native_guidance_digest"] == before["native_guidance_digest"]
    assert open_installation(instance.paths, ())[0].instance_id == layer.instance_id
    from bazarlyk.bootstrap.email_guidance import home
    guide = home(instance.paths, agentic_view=runtime.agentic_view)
    assert guide == layer.root / "plugins/imported-email-management/guidance/email"
    assert (guide / "SKILL.md").is_file()


@pytest.mark.parametrize("failure", ["record", "state", "identity", "inventory", "digest", "foreign_mode", "symlink", "missing_optional_source", "git"])
def test_strict_open_refuses_without_repair(host, failure):
    instance, _, _ = host
    file = instance.paths.instance_root / "config/agentic-layer.json"
    original = file.read_bytes()
    record = json.loads(original)
    state = instance.paths.agentic_runtime
    hidden = state.with_name("held-agentic")
    git = instance.paths.agentic_root / ".git"
    held_git = git.with_name("held-git")
    try:
        if failure == "git": git.rename(held_git)
        elif failure == "record": file.unlink()
        elif failure == "state": state.rename(hidden)
        elif failure == "identity":
            record["instance_id"] = "inst_" + "0" * 32
            file.write_text(json.dumps(record))
        elif failure == "inventory":
            record["inventory"]["packages"] = []
            record["inventory_sha256"] = digest(record["inventory"])
            file.write_text(json.dumps(record))
        elif failure == "digest":
            record["inventory_sha256"] = "0" * 64
            file.write_text(json.dumps(record))
        elif failure == "foreign_mode": file.chmod(0o644)
        elif failure == "symlink":
            file.unlink(); file.symlink_to(file.parent / "not-a-binding")
        selection = ("paper",) if failure == "missing_optional_source" else ()
        with pytest.raises(DomainError, match="offline"):
            open_installation(instance.paths, selection)
        if failure == "state": assert not state.exists()
        if failure in {"record", "symlink"}: assert not file.is_file()
    finally:
        if hidden.exists(): hidden.rename(state)
        if held_git.exists(): held_git.rename(git)
        if file.is_symlink(): file.unlink()
        file.write_bytes(original)
        file.chmod(0o600)


@pytest.mark.parametrize("damage", ["missing", "empty", "invalid", "unborn", "missing_head_object"])
def test_open_refuses_damaged_git_under_valid_parent_without_repair(host, tmp_path, damage):
    instance, _, _ = host
    root = instance.paths.agentic_root
    parent = root.parent
    held = tmp_path / "original-agentic-git"
    parent_git = parent / ".git"
    assert not parent_git.exists()
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CEILING_DIRECTORIES": str(parent.parent)}
    def git(directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], env=env, capture_output=True, check=True)
    parent_file = parent / "synthetic-parent.txt"
    record = instance.paths.instance_root / "config/agentic-layer.json"
    original_record = record.read_bytes()
    try:
        git(parent, "init", "-b", "synthetic-operations-parent")
        parent_file.write_text("committed synthetic parent\n")
        git(parent, "add", parent_file.name)
        git(parent, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Synthetic parent")
        parent_file.write_text("staged synthetic parent edit\n")
        git(parent, "add", parent_file.name)
        (root / ".git").rename(held)
        if damage in {"empty", "invalid"}:
            (root / ".git").mkdir()
        if damage == "invalid":
            (root / ".git/HEAD").write_text("not a Git HEAD\n")
        if damage in {"unborn", "missing_head_object"}:
            git(root, "init", "-b", "synthetic-agentic-unborn")
        if damage == "missing_head_object":
            (root / ".git/refs/heads/synthetic-agentic-unborn").write_text("1" * 40 + "\n")
        def snapshot():
            files = {str(p.relative_to(parent_git)): p.read_bytes() for p in parent_git.rglob("*") if p.is_file()}
            child_git = root / ".git"
            child_files = ({str(p.relative_to(child_git)): p.read_bytes()
                            for p in child_git.rglob("*") if p.is_file()} if child_git.exists() else None)
            return files, child_files, parent_file.read_bytes(), record.read_bytes()
        before = snapshot()
        with pytest.raises(DomainError, match="offline"):
            open_installation(instance.paths, ())
        assert snapshot() == before
        assert record.read_bytes() == original_record
        if damage == "missing":
            assert not (root / ".git").exists()
    finally:
        if held.exists():
            if (root / ".git").exists(): shutil.rmtree(root / ".git")
            held.rename(root / ".git")
        if parent_git.exists(): shutil.rmtree(parent_git)
        parent_file.unlink(missing_ok=True)


def test_open_accepts_later_commit_branch_and_dirty_authored_files(host):
    instance, _, _ = host
    layer = instance.runtime.agentic_layer
    old_branch = layer.history.status()["branch"]
    layer.history.git.run(["checkout", "-b", "synthetic-later-authoring"])
    try:
        edit(layer, "user-preferences/SKILL.md",
             "A legitimate later authored instruction.")
        commit = layer.history.commit("Synthetic later authored instruction")["commit"]
        (layer.root / "synthetic-dirty.md").write_text("A legitimate uncommitted file.\n")
        reopened, _ = open_installation(instance.paths, ())
        status = reopened.history.status()
        assert status["head"] == commit and status["branch"] == "synthetic-later-authoring"
        assert not status["clean"]
    finally:
        (layer.root / "synthetic-dirty.md").unlink(missing_ok=True)
        layer.history.git.run(["checkout", old_branch])


def test_real_callback_scans_and_preserves_each_event_once(host, monkeypatch):
    instance, manager, applier = host
    runtime, layer = instance.runtime, instance.runtime.agentic_layer
    def notices():
        with instance.database.read() as uow:
            return [dict(r) for r in uow.connection.execute("SELECT * FROM inbox_events WHERE source_type='agentic_changes'")]
    def cursor():
        with instance.database.read() as uow:
            return runtime.agentic_inbox_bridge.status(uow)
    assert cursor()["pending"]  # Initial durable events were not suppressed.
    runtime.inbox_nudges._run_hook()
    baseline = len(notices())
    changed = edit(layer, "operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/inbox-session-note.md", "Known manager edit.", manager)
    runtime.inbox_nudges._run_hook()
    assert len(notices()) == baseline + 1
    path = layer.root / "external.md"
    path.write_text("External synthetic edit")
    runtime.inbox_nudges._run_hook()
    assert len(notices()) == baseline + 2
    event = layer.changes.list(after=changed["sequence"])["changes"][-1]
    assert event["attribution"]["actor_id"] == "external/unknown"
    runtime.inbox_nudges._run_hook()
    assert len(notices()) == baseline + 2
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (applier,))
    path.write_text("Pending while offline")
    runtime.inbox_nudges._run_hook()
    assert cursor()["pending"] and len(notices()) == baseline + 2
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (applier,))
    runtime.inbox_nudges._run_hook()
    assert len(notices()) == baseline + 3
    tasks = []
    monkeypatch.setattr(runtime.domains.tasks, "reconcile_snoozes", lambda uow: tasks.append(True))
    monkeypatch.setattr(layer.changes, "scan", lambda **kwargs: (_ for _ in ()).throw(ValueError("synthetic scan failure")))
    runtime.inbox_nudges._run_hook()
    assert tasks and runtime.agentic_inbox_bridge._last_callback_failure["error_type"] == "ValueError"


def test_old_host_schema_is_refused_without_repair(tmp_path):
    import sqlite3
    from bazarlyk.composition.assembly import HostComposition, host_contributions, initialize_empty_target
    from bazarlyk.extensions import NO_EXTENSIONS
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.runtime import build_runtime
    from host_foundation_support import make_paths, make_workspace
    from agentic_installation_support import provision
    workspace = make_workspace(tmp_path)
    paths = make_paths(tmp_path)
    old = HostComposition()
    for contribution in host_contributions(ChangeFeed(), NO_EXTENSIONS):
        if contribution.owner != "agentic-inbox": old.add(contribution)
    db = initialize_empty_target(paths, old.freeze(), NO_EXTENSIONS)
    db.close()
    provision(paths, roles=workspace / "qorqut-agentic/src/qorqut_agentic/resources/defaults")
    def tables():
        with sqlite3.connect(paths.database) as conn:
            return conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    before = tables()
    with pytest.raises(DomainError, match="target identity"):
        build_runtime(workspace, paths=paths, extensions=NO_EXTENSIONS)
    assert tables() == before
    assert not any(row[0] == "agentic_inbox_cursor" for row in before)


def test_operations_and_nightly_capture_exclude_agentic_tree_and_binding(host):
    from bazarlyk.storage.recovery import ROOT_AUTHORED, PRIVATE_RUNTIME_FILES, SKIP_DIRECTORIES
    from bazarlyk.storage.versioning import public_path
    from bazarlyk.operations_history.composition import registered_areas
    from qorqut_operations.history_exports import TECHNICAL_EXCLUDED
    from bazarlyk.operations_history.catalog import catalog
    instance, _, _ = host
    assert "agentic" not in ROOT_AUTHORED and ".runtime" in SKIP_DIRECTORIES
    assert not any("agentic" in path for path in PRIVATE_RUNTIME_FILES)
    for path in ("agentic/packages/example/instructions.md", ".runtime/agentic/state.sqlite3", "../config/agentic-layer.json"):
        assert not public_path(path, [])
    with instance.database.read() as uow:
        assert all("agentic" not in str(area) for area in registered_areas(uow))
    assert catalog().classifications["agentic_inbox_cursor"][1].kind == TECHNICAL_EXCLUDED


def test_trusted_activation_uses_imported_current_assets_without_writing_the_record(tmp_path):
    import yaml
    instance, _ = build_instance(tmp_path, agentic_sources=("content", "paper", "fastmail"))
    paths, layer = instance.paths, instance.runtime.agentic_layer
    record = paths.instance_root / "config/agentic-layer.json"
    original = record.read_bytes()
    stored = json.loads(original)["inventory"]
    assert all(not s["enabled"] for s in stored["sources"] if s["id"] in {"content", "content-library", "paper", "fastmail"})
    # Authored flags do not activate either an ordinary import or an inactive template.
    for package in ("imported-paper-metadata", "imported-content-templates"):
        path = f"plugins/{package}/agentic.yaml"
        manifest = yaml.safe_load((layer.root / path).read_text())
        manifest["native"]["enabled"] = True
        edit(layer, path, yaml.safe_dump(manifest))
    path = "plugins/imported-paper-metadata/native/qorqut_paper/agentic-assets/guidance.md"
    edit(layer, path, "Current synthetic Paper guidance.")
    def snapshot():
        return {p.relative_to(layer.root).as_posix(): digest(p.read_bytes()) for p in layer.root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(layer.root).parts}
    authored = snapshot()
    inactive = open_installation(paths, ())[1]
    assert "imported-paper-metadata" not in inactive.selection({})["package_ids"]
    enabled = open_installation(paths, ("content", "paper", "fastmail"))[1]
    selected = enabled.selection({})["package_ids"]
    assert {"imported-content", "imported-paper-metadata", "imported-fastmail-metadata"} <= set(selected)
    assert not any(name.endswith("-templates") for name in selected)
    assert enabled.resource("paper", "qorqut_paper/agentic-assets/guidance.md")["content"] == "Current synthetic Paper guidance."
    for package in enabled._inventory["packages"]:
        for entry in package["entries"]:
            if entry["native"]["template"]:
                assert not entry["native"]["enabled"]
    again = open_installation(paths, ())[1]
    assert "imported-fastmail-metadata" not in again.selection({})["package_ids"]
    assert not instance.runtime.registry.known("fastmail_send")  # procedural activation grants no native capability
    assert record.read_bytes() == original and snapshot() == authored
    # Enabling a source whose authored package has been removed needs an offline update.
    package = "imported-fastmail-metadata"
    layer.authoring.apply({"actor_id": "fixture-owner", "actor_kind": "owner", "reason": "Synthetic package removal"},
        [{"op": "delete", "path": f"plugins/{package}/" + p, "expected_sha256": digest((layer.root / f"plugins/{package}" / p).read_bytes())}
         for p in next(item for item in stored["packages"] if item["id"] == package)["files"]] +
        [{"op": "delete", "path": f"plugins/{package}/agentic.yaml", "expected_sha256": digest((layer.root / f"plugins/{package}/agentic.yaml").read_bytes())}])
    with pytest.raises(DomainError, match="offline"):
        open_installation(paths, ("fastmail",))
    assert record.read_bytes() == original
    instance.runtime.release_instance()
    instance.runtime.domains.shared_context.close()
    instance.database.close()


def test_external_scan_remains_durable_when_the_operations_drain_fails(host, monkeypatch):
    from bazarlyk.agentic import inbox as bridge_module
    instance, _, _ = host
    runtime, layer = instance.runtime, instance.runtime.agentic_layer
    runtime.inbox_nudges._run_hook()
    def state():
        with instance.database.read() as uow:
            return (runtime.agentic_inbox_bridge.status(uow)["cursor"],
                    uow.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='agentic_changes'").fetchone()[0])
    before = state()
    head = layer.changes.list()["head"]
    (layer.root / "durable-external.md").write_text("Synthetic external change before a failed drain")
    tasks = []
    with monkeypatch.context() as patch:
        patch.setattr(runtime.domains.tasks, "reconcile_snoozes", lambda uow: tasks.append(True))
        patch.setattr(bridge_module, "emit_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic drain failure")))
        runtime.inbox_nudges._run_hook()
    assert tasks and state() == before
    changes = layer.changes.list(after=head)["changes"]
    assert len(changes) == 1 and changes[0]["attribution"]["actor_id"] == "external/unknown"
    runtime.inbox_nudges._run_hook()
    assert state() == (changes[0]["sequence"], before[1] + 1)
    runtime.inbox_nudges._run_hook()
    assert state() == (changes[0]["sequence"], before[1] + 1)


def test_canonical_email_guide_is_validated_without_seeding(host):
    from bazarlyk.bootstrap.preparation import seed_instance_guidance
    instance, _, _ = host
    resolver = instance.runtime.resolver
    view = resolver.require_agentic_view()
    data = {"plugins": [{"id": "email-management"}], "agentic_selection": view.selection({}),
            "agentic_catalog_revision": view.catalog_revision}
    result = seed_instance_guidance(resolver, data)
    assert result["email-management"]["path"] == str(view.layer.root / "plugins/imported-email-management/guidance/email")
    assert result["email-management"]["seeded"] is False
