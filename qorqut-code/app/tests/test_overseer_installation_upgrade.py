"""Focused synthetic proof of the offline Overseer installation and upgrade (P07, A018 section 4).

No native product session, provider call, live instance or startup mutation is involved. Fresh installs are the real
``provision`` fixture. Old installations are a genuinely version 1 binding plus, where migration is the subject, an actual
schema-1 module store rebuilt with the module's own DDL and legacy fixture rows; its real public ``migrate_state`` runs
through the host entry point. No migration is faked.
"""
import json
import os
import sqlite3
import stat

import yaml
import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.agentic import provisioning, upgrade
from bazarlyk.agentic.installation import OVERSEER_REFS, digest, open_installation, seed_files
from bazarlyk.agentic.support import canonical
from native_chat_support import Native
from host_foundation_support import build_instance, make_paths, make_workspace

ATTRIBUTION = {"actor_id": "owner", "actor_kind": "owner", "reason": "Synthetic offline upgrade"}
CUSTOM_TOOL_REF = "custom-tools/synthetic-echo"


def _export_root(instance):
    root = instance.paths.instance_root
    return root.parent / (root.name + "-source-fixture")


def _binding(instance):
    from bazarlyk.agentic.installation import RECORD
    path = instance.paths.instance_root / "config" / RECORD
    return path, json.loads(path.read_text())


def _write_binding(path, record):
    path.write_bytes(canonical(record) + b"\n")
    os.chmod(path, 0o600)


def _make_old(instance):
    """Turn the fresh synthetic install into the state an installed v1 host presents: no Overseer seed, v1 binding."""
    layer = instance.runtime.agentic_layer
    from qorqut_agentic.authoring import package_revision
    from qorqut_agentic.files import DiskSource
    revision = package_revision(DiskSource(layer._ctx.tree).files, "overseer")
    layer.authoring.remove_package(attribution={**ATTRIBUTION, "reason": "Synthetic old-state fixture"},
                                   package_id="overseer", expected_package_revision=revision)
    if not layer.history.status()["clean"]:
        layer.history.commit("Synthetic old state: the packaged Overseer package did not exist")
    path, record = _binding(instance)
    old = {key: value for key, value in record.items() if key != "overseer_seed"}
    old["version"] = 1
    _write_binding(path, old)
    return path, record


def _change_upstream(export, relative, addition):
    """Append to one exported source asset and recompute every component digest, as an upstream change would."""
    from bazarlyk.source_snapshot import component_digest
    path = export / relative
    path.write_text(path.read_text() + addition)
    manifest_path = export / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    for component in manifest["components"]:
        component["snapshot_sha256"] = component_digest(export, component, manifest["components"])
    manifest_path.write_text(json.dumps(manifest))


def _downgrade_state_to_v1(state_dir, *, run_id="run_legacy"):
    """Rebuild the module store as a real schema-1 fixture, preserving every base table and row.

    This follows the module's own ``old_state`` fixture: the schema-1 DDL plus explicit meta/inventory/run rows, with a
    legacy run inserted exactly as that fixture does. The schema-2 Overseer tables and migration receipt do not exist.
    """
    from qorqut_agentic.store import DDL
    database = state_dir / "state.sqlite3"
    source = sqlite3.connect(database)
    source.row_factory = sqlite3.Row
    try:
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tables = [row["name"] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        rows = {table: [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')] for table in tables}
    finally:
        source.close()
    temporary = state_dir / "state.schema1.sqlite3"
    if temporary.exists():
        temporary.unlink()
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(DDL)
        base_tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                       if row[0] != "sqlite_sequence"]
        for table in base_tables:
            for row in rows.get(table, []):
                columns = list(row)
                connection.execute(f'INSERT INTO "{table}" ({", ".join(columns)}) VALUES '
                                   f'({", ".join("?" for _ in columns)})', [row[column] for column in columns])
        connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        connection.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (run_id, "overseer/workflow", "revision-1", "2026-01-01T00:00:00Z",
                            "2026-01-01T00:00:00Z", "{}", "{}", None, None, "{}", "{}"))
        connection.commit()
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        journal = state_dir / ("state.sqlite3" + suffix)
        if journal.exists():
            journal.unlink()
    os.replace(temporary, database)


def _schema_version(state_dir):
    with sqlite3.connect(state_dir / "state.sqlite3") as connection:
        return connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]


def _author_custom_tool(layer):
    """Author a real custom tool package through the public authoring API (fixture content only)."""
    manifest = {
        "schema_version": 1, "id": "custom-tools", "name": "Synthetic custom tools",
        "description": "Synthetic fixture package; not an imported source.",
        "entries": [{"id": "synthetic-echo", "kind": "tool", "description": "Synthetic echo tool",
                     "when_to_use": "Synthetic fixture only", "path": "synthetic-echo.json"}],
    }
    definition = {"schema_version": 1, "runtime": "python", "code": "synthetic-echo.py",
                  "input_schema": {"type": "object"}, "output_schema": True}
    layer.authoring.apply(attribution={**ATTRIBUTION, "reason": "Synthetic custom tool"},
                          changes=[{"op": "create", "path": "packages/custom-tools/agentic.yaml",
                                    "content": yaml.safe_dump(manifest, sort_keys=True)},
                                   {"op": "create", "path": "packages/custom-tools/synthetic-echo.json",
                                    "content": json.dumps(definition)},
                                   {"op": "create", "path": "packages/custom-tools/synthetic-echo.py",
                                    "content": "print('synthetic custom tool')\n"}])
    return CUSTOM_TOOL_REF


def _build_with_selection(tmp_path, selected, optional):
    """A genuinely prepared and initialized binding that records the requested optional-extension selection."""
    from unittest import mock
    import agentic_installation_support as support
    from bazarlyk.agentic.installation import initialize_installation, prepare_installation

    installed = tuple(selected)

    def provision(paths, *, selected=(), roles=None, optional=()):
        root = support.snapshot(paths.instance_root.parent / (paths.instance_root.name + "-source-fixture"),
                                roles=roles, optional=set(optional))
        return initialize_installation(paths, prepare_installation(root, installed))

    with mock.patch.object(support, "provision", provision):
        return build_instance(tmp_path, agentic_sources=tuple(optional))


def test_fresh_install_seeds_the_packaged_overseer_and_private_roots(tmp_path):
    native = Native(tmp_path, "claude", role="learning-applier")
    layer = native.runtime.agentic_layer
    for ref in OVERSEER_REFS:
        assert layer.catalog.read_entry(ref)
    path, record = _binding(native.instance)
    assert record["version"] == 2
    assert record["overseer_seed"]["resource"] == "qorqut_agentic/resources/overseer"
    packed = seed_files("overseer")
    assert {rel: digest(body) for rel, body in packed.items()} == record["overseer_seed"]["files"]
    installed = sorted(path.relative_to(layer.root).as_posix() for path in (layer.root / "packages/overseer").rglob("*")
                       if path.is_file())
    assert set(record["overseer_seed"]["files"]) == {rel.removeprefix("packages/overseer/") for rel in installed}
    assert record["instance_id"] == layer.instance_id
    # The shipped fresh-install profile mapping grants the experience-read capability that ordinary global reads do
    # not imply, and does not broaden any other role.
    from bazarlyk.bootstrap.initialize import AGENT_CAPABILITIES
    assert "read_agent_experience" in AGENT_CAPABILITIES["learning-applier"]
    assert "read_agent_experience" not in AGENT_CAPABILITIES["ordubashy"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_private_roots_are_created_once_with_mode_0700_and_never_repaired(tmp_path):
    workspace = make_workspace(tmp_path)
    paths = make_paths(tmp_path / "workspace-parent")
    paths.operations.mkdir(parents=True)
    (paths.operations / ".runtime").mkdir(mode=0o700)
    planned = provisioning.plan(paths)
    assert planned["complete"] and {item["state"] for item in planned["items"]} == {"missing"}
    applied = provisioning.apply(paths)
    assert set(applied["created"]) == {"overseer", "overseer_archive", "application_worktrees", "application_states"}
    for name, locate in provisioning.ROOTS:
        assert stat.S_IMODE(os.stat(locate(paths)).st_mode) == 0o700, name
    again = provisioning.apply(paths)
    assert again["created"] == [] and set(again["preserved"]) == {"overseer", "overseer_archive",
                                                                   "application_worktrees", "application_states"}
    # A wider existing mode is a refusal, never a silent chmod; the directory keeps its mode.
    os.chmod(paths.overseer_archive, 0o750)
    assert provisioning.plan(paths)["complete"] is False
    with pytest.raises(Exception):
        provisioning.apply(paths)
    assert stat.S_IMODE(os.stat(paths.overseer_archive).st_mode) == 0o750
    os.chmod(paths.overseer_archive, 0o700)
    # A symlinked root is refused and left alone.
    (paths.runtime / "agentic-worktrees").rmdir() if (paths.runtime / "agentic-worktrees").exists() else None
    (paths.runtime / "agentic-worktrees").symlink_to(tmp_path, target_is_directory=True)
    assert provisioning.plan(paths)["items"][3]["state"] == "unsafe_symlink"
    with pytest.raises(Exception):
        provisioning.apply(paths)
    assert (paths.runtime / "agentic-worktrees").is_symlink()


def test_opening_an_installed_layer_creates_no_private_path(tmp_path):
    native = Native(tmp_path, "claude", role="learning-applier")
    paths = native.instance.paths
    import shutil
    provisioning.apply(paths)
    shutil.rmtree(paths.overseer_archive)
    shutil.rmtree(paths.operations / ".agentic-worktrees")
    shutil.rmtree(paths.runtime / "agentic-worktrees")
    open_installation(paths, ())
    assert not paths.overseer_archive.exists()
    assert not (paths.operations / ".agentic-worktrees").exists()
    assert not (paths.runtime / "agentic-worktrees").exists()


def test_explicit_old_state_upgrade_preserves_identity_edits_legacy_rows_and_reports_grants(tmp_path):
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    layer = instance.runtime.agentic_layer
    before_instance = layer.instance_id
    _, fresh_record = _binding(instance)
    _make_old(instance)
    # A local edit and a locally owned file in the managed tree survive the upgrade untouched.
    local = "notes/local.md"
    layer.authoring.apply(attribution={**ATTRIBUTION, "reason": "Local owner note"},
                          changes=[{"op": "create", "path": local, "content_base64": "bG9jYWw="}])
    original_extract = (layer.root / "packages/overseer/instructions/extract.md")
    assert not original_extract.exists()  # the synthetic old state has no packaged Overseer package
    # An existing Learning Applier profile that predates experience-read: preparation reports it, apply never writes it.
    agent = native.agent
    with instance.runtime.database.transaction() as uow:
        row = uow.connection.execute("SELECT profile_json,revision FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()
        profile = json.loads(row[0])
        profile["scope"]["global_capabilities"] = [c for c in profile["scope"]["global_capabilities"]
                                                   if c != "read_agent_experience"]
        uow.connection.execute("UPDATE agent_profiles SET profile_json=?,revision=? WHERE agent_id=?",
                               (json.dumps(profile), row[1] + 1, agent))
    # Retained legacy history: one procedural delta row and its decision read back identically.
    with instance.runtime.database.transaction() as uow:
        uow.repository("learning_candidates", business_id=None).insert({
            "id": "5b1d0e0e-0000-4000-8000-000000000001",
            "candidate_json": {"bucket": "delta", "tag": "rule", "scope": "biz", "text": "legacy retained rule",
                               "created": "2026-01-01T00:00:00Z"},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
    with instance.runtime.database.read() as uow:
        legacy = uow.connection.execute("SELECT candidate_json FROM learning_candidates WHERE id=?",
                                        ("5b1d0e0e-0000-4000-8000-000000000001",)).fetchone()[0]

    export = _export_root(instance)
    assert (export / "SOURCE-MANIFEST.json").exists()
    _downgrade_state_to_v1(paths.agentic_runtime)
    assert _schema_version(paths.agentic_runtime) == "1"
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan["binding"]["current_version"] == 1 and plan["binding"]["target_version"] == 2
    assert plan["state"]["migration_required"] is True
    assert set(plan["overseer_seed"]["desired"]["files"]) == set(seed_files("overseer"))
    assert {change["path"] for change in plan["overseer_seed"]["changes"]} == {
        "packages/overseer/" + rel for rel in seed_files("overseer")}
    assert plan["management_seed"]["conflicts"] == [] and plan["import"]["conflicts"] == []
    assert set(plan["grants"]["missing"]) == {"read_agent_experience", "manage_agentic_layer"}
    assert plan["provisioning"]["complete"] is True
    assert plan["complete"] is True
    public = upgrade.public_plan(plan)
    assert "_prepared" not in public and json.dumps(public)

    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert result["state"]["migrated"] is True
    assert result["recovered_partial_attempt"] is False
    _, record = _binding(instance)
    assert record["version"] == 2 and record["instance_id"] == before_instance
    assert record["initial_commit"] == fresh_record["initial_commit"]
    assert record["selected_extensions"] == fresh_record["selected_extensions"]
    assert record["source_manifest_sha256"] == fresh_record["source_manifest_sha256"]
    # The module's real public migration committed schema 2 with exactly one receipt and left a read-only reopen working.
    assert _schema_version(paths.agentic_runtime) == "2"
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT from_version,to_version FROM overseer_migrations").fetchall() == [(1, 2)]
        assert connection.execute("SELECT run_id FROM runs WHERE run_id='run_legacy'").fetchone()[0] == "run_legacy"
    reopened, _ = open_installation(paths, ())
    for ref in OVERSEER_REFS:
        assert reopened.catalog.read_entry(ref)
    assert (reopened.root / local).read_text() == "local"
    with instance.runtime.database.read() as uow:
        profile_after = json.loads(uow.connection.execute(
            "SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()[0])
        legacy_after = uow.connection.execute("SELECT candidate_json FROM learning_candidates WHERE id=?",
                                              ("5b1d0e0e-0000-4000-8000-000000000001",)).fetchone()[0]
    assert "read_agent_experience" not in profile_after["scope"]["global_capabilities"]  # report only, never granted
    assert "manage_agentic_layer" not in profile_after["scope"]["global_capabilities"]
    assert legacy_after == legacy
    assert stat.S_IMODE(os.stat(paths.overseer_archive).st_mode) == 0o700
    # A repeat (no remaining changes) is refused as a changed plan, never applied twice.
    with pytest.raises(Exception, match="changed since preparation|digest"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM overseer_migrations").fetchone()[0] == 1


def test_changed_upstream_recovery_seals_a_receipt_for_the_desired_inventory(tmp_path, monkeypatch):
    """Manager case 5: a crash after the imported files but before the binding used to seal the stale receipt."""
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    _make_old(instance)
    export = _export_root(instance)
    _change_upstream(export, "qorqut-code/app/bazarlyk/agentic/assets/inbox.md",
                     "\nSynthetic changed upstream inbox instruction.\n")
    paths = instance.paths
    plan = upgrade.prepare_upgrade(paths, export)
    changed = [change for change in plan["import"]["changes"] if change["path"].endswith("assets/inbox.md")]
    assert plan["complete"] is True and changed
    old_manifest = _binding(instance)[1]["import_manifest"]
    real_replace = os.replace

    def crash(source, destination):
        raise OSError("synthetic crash before the binding update")

    monkeypatch.setattr(upgrade, "_replace", crash)
    with pytest.raises(OSError, match="synthetic crash"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    monkeypatch.setattr(upgrade, "_replace", real_replace)
    assert _binding(instance)[1]["version"] == 1
    layer = instance.runtime.agentic_layer
    head_after_crash = layer.history.status()["head"]
    changes_after_crash = layer.changes.list(limit=500)["changes"]
    # The imported files are already at the verified desired bytes, so no file change remains; the newly desired inventory
    # nevertheless differs from the installed receipt.
    remaining = upgrade.prepare_upgrade(paths, export)
    assert remaining["import"]["changes"] == [] and remaining["complete"] is True
    assert remaining["import"]["desired_files"] != old_manifest["files"]
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert result["recovered_partial_attempt"] is True and result["packages_updated"] == []
    assert result["upgrade_commit"] == head_after_crash
    reopened, _ = open_installation(paths, ())
    _, record = _binding(instance)
    assert record["import_manifest"]["files"] == remaining["import"]["desired_files"]
    assert record["import_manifest"]["previous_import_digest"] == old_manifest["manifest_digest"]
    assert reopened.instance_id == instance.runtime.agentic_layer.instance_id
    assert reopened.changes.list(limit=500)["changes"] == changes_after_crash  # no duplicate authoring effects
    asset = reopened.root / "packages/imported-host-builtins/native/app/bazarlyk/agentic/assets/inbox.md"
    assert "Synthetic changed upstream inbox instruction." in asset.read_text()


def test_upgrade_preserves_a_recorded_optional_extension_selection(tmp_path):
    """A nonempty installed selection survives an omitted prepare selection; a different explicit one is refused."""
    instance, _ = _build_with_selection(tmp_path, ("content",), ("content",))
    paths = instance.paths
    _, installed = _binding(instance)
    assert installed["selected_extensions"] == ["content"]
    content = next(package for package in installed["inventory"]["packages"] if package["id"] == "imported-content")
    assert content["native"]["enabled"] is True
    _make_old(instance)
    export = _export_root(instance)
    _change_upstream(export, "qorqut-code/app/bazarlyk/agentic/assets/inbox.md",
                     "\nSynthetic changed upstream inbox instruction.\n")
    plan = upgrade.prepare_upgrade(paths, export)  # selection omitted: keep the installed one
    assert plan["selected_extensions"] == ["content"] and plan["complete"] is True
    assert not any(change["op"] == "delete" for change in plan["import"]["changes"])
    with pytest.raises(DomainError, match="separate explicit installation/update"):
        upgrade.prepare_upgrade(paths, export, ["paper"])
    assert _binding(instance)[1]["selected_extensions"] == ["content"]
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert result["recovered_partial_attempt"] is False
    reopened, view = open_installation(paths, ("content",))
    _, record = _binding(instance)
    assert record["selected_extensions"] == ["content"]
    stored = next(package for package in record["inventory"]["packages"] if package["id"] == "imported-content")
    assert stored["native"]["enabled"] is True
    assert record["import_manifest"]["files"] == plan["import"]["desired_files"]
    assert "imported-content" in view.selection({})["package_ids"]
    for path in stored["files"]:
        assert (reopened.root / "packages/imported-content" / path).is_file()
    # A repeat keeps the recorded selection and is refused as a changed plan, never applied twice.
    with pytest.raises(DomainError, match="changed since preparation"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert _binding(instance)[1]["selected_extensions"] == ["content"]


def test_real_v1_state_upgrade_preserves_history_custom_edits_and_a_custom_tool(tmp_path):
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    layer = instance.runtime.agentic_layer
    instance_id = layer.instance_id
    _, fresh_record = _binding(instance)
    _make_old(instance)
    assert layer.catalog.read_entry(_author_custom_tool(layer))["entry"]["ref"] == CUSTOM_TOOL_REF
    local = "notes/owner-note.md"
    layer.authoring.apply(attribution={**ATTRIBUTION, "reason": "Synthetic local owner note"},
                          changes=[{"op": "create", "path": local, "content": "owner note\n"}])
    authored_history = {entry["commit"] for entry in layer.history.log(limit=100)["commits"]}
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        original_changes = {row[0] for row in connection.execute("SELECT change_id FROM changes")}
    _downgrade_state_to_v1(paths.agentic_runtime)
    assert _schema_version(paths.agentic_runtime) == "1"
    export = _export_root(instance)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan["state"]["migration_required"] is True and plan["complete"] is True
    # A locally edited file the packaged seed would create is retained and refused before any migration.
    (layer.root / "packages/overseer/instructions").mkdir(parents=True)
    (layer.root / "packages/overseer/instructions/extract.md").write_text("my local extraction\n")
    diverged = upgrade.prepare_upgrade(paths, export)
    assert diverged["complete"] is False and diverged["overseer_seed"]["conflicts"]
    retired_bytes = _binding(instance)[0].read_bytes()
    with pytest.raises(DomainError, match="conflict"):
        upgrade.apply_upgrade(paths, diverged, attribution=ATTRIBUTION)
    assert _schema_version(paths.agentic_runtime) == "1"
    assert _binding(instance)[1]["version"] == 1
    assert (layer.root / "packages/overseer/instructions/extract.md").read_text() == "my local extraction\n"
    # Neither preparation nor a refused plan creates a history file or touches the retired binding.
    assert not _history(instance).exists() and _binding(instance)[0].read_bytes() == retired_bytes
    (layer.root / "packages/overseer/instructions/extract.md").unlink()
    (layer.root / "packages/overseer/instructions").rmdir()
    (layer.root / "packages/overseer").rmdir()
    clean = upgrade.prepare_upgrade(paths, export)
    assert not _history(instance).exists() and _binding(instance)[0].read_bytes() == retired_bytes
    result = upgrade.apply_upgrade(paths, clean, attribution=ATTRIBUTION)
    assert result["state"]["migrated"] is True
    # The retired version 1 binding is retained once, byte for byte, under its own digest.
    snapshot = _history(instance) / (result["prior_binding"]["sha256"] + ".json")
    assert result["prior_binding"] == {"snapshot": str(snapshot), "sha256": result["prior_binding"]["sha256"],
                                       "created": True}
    assert snapshot.read_bytes() == retired_bytes
    assert sorted(p.name for p in _history(instance).iterdir()) == [snapshot.name]
    reopened, _ = open_installation(paths, ())
    assert reopened.instance_id == instance_id
    _, record = _binding(instance)
    assert record["version"] == 2 and record["instance_id"] == instance_id
    assert record["initial_commit"] == fresh_record["initial_commit"]
    assert record["import_manifest"]["previous_import_digest"] == fresh_record["import_manifest"]["manifest_digest"]
    # The actual public migration preserved every schema-1 row and recorded one receipt.
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
        assert connection.execute("SELECT from_version,to_version FROM overseer_migrations").fetchall() == [(1, 2)]
        assert {row[0] for row in connection.execute("SELECT change_id FROM changes")} >= original_changes
        assert connection.execute("SELECT run_id FROM runs WHERE run_id='run_legacy'").fetchone()[0] == "run_legacy"
    # Authored history, the custom edit and the custom tool remain; the read-only reopen writes nothing.
    before_open = _binding(instance)[0].read_bytes()
    assert {entry["commit"] for entry in reopened.history.log(limit=100)["commits"]} >= authored_history
    assert (reopened.root / local).read_text() == "owner note\n"
    assert reopened.catalog.read_entry(CUSTOM_TOOL_REF)["entry"]["ref"] == CUSTOM_TOOL_REF
    assert (reopened.root / "packages/custom-tools/synthetic-echo.py").read_text() == "print('synthetic custom tool')\n"
    assert reopened.recovery_required is False
    assert _binding(instance)[0].read_bytes() == before_open
    # A repeat of the same plan is refused and cannot add a second receipt, authoring effect or history copy.
    with pytest.raises(DomainError, match="changed since preparation"):
        upgrade.apply_upgrade(paths, clean, attribution=ATTRIBUTION)
    assert _binding(instance)[0].read_bytes() == before_open
    assert sorted(p.name for p in _history(instance).iterdir()) == [snapshot.name]
    assert snapshot.read_bytes() == retired_bytes


def test_real_v1_partial_upgrade_recovers_one_migration_receipt_and_one_commit(tmp_path, monkeypatch):
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    instance_id = instance.runtime.agentic_layer.instance_id
    _make_old(instance)
    _downgrade_state_to_v1(paths.agentic_runtime)
    export = _export_root(instance)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan["state"]["migration_required"] is True
    real_replace = os.replace

    def crash(source, destination):
        raise OSError("synthetic crash before the binding update")

    monkeypatch.setattr(upgrade, "_replace", crash)
    with pytest.raises(OSError, match="synthetic crash"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    monkeypatch.setattr(upgrade, "_replace", real_replace)
    # The real migration and its receipt committed; only the binding update is missing.
    assert _schema_version(paths.agentic_runtime) == "2"
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT from_version,to_version FROM overseer_migrations").fetchall() == [(1, 2)]
    assert _binding(instance)[1]["version"] == 1
    remaining = upgrade.prepare_upgrade(paths, export)
    assert remaining["state"]["migration_required"] is False and remaining["state"]["openable"] is True
    assert remaining["import"]["changes"] == [] and remaining["overseer_seed"]["changes"] == []
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert result["recovered_partial_attempt"] is True and result["state"]["migrated"] is False
    reopened, _ = open_installation(paths, ())
    assert reopened.instance_id == instance_id
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM overseer_migrations").fetchone()[0] == 1
    assert sum("Offline upgrade" in entry["subject"] for entry in reopened.history.log(limit=20)["commits"]) == 1
    # The public migration replay is explicit and idempotent, leaving the one receipt.
    replay = upgrade.AgenticLayer.migrate_state(paths.agentic_root, paths.agentic_runtime)
    assert replay["replayed"] is True
    with sqlite3.connect(paths.agentic_runtime / "state.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM overseer_migrations").fetchone()[0] == 1


def test_upgrade_cli_needs_only_the_instance_root(tmp_path):
    from bazarlyk.agentic.__main__ import dispatch
    native = Native(tmp_path, "claude", role="learning-applier")
    export = _export_root(native.instance)
    plan = dispatch({"operation": "upgrade_prepare", "arguments": {"export_root": str(export)}},
                    instance_root=str(native.instance.paths.instance_root))
    assert plan["binding"]["target_version"] == 2 and "_prepared" not in plan
    assert plan["selected_extensions"] == []


def _history(instance):
    from pathlib import Path
    return Path(instance.paths.instance_root) / "config" / "agentic-layer-history"


def test_a_real_0_1_0_binding_upgrades_and_retains_its_prior_provenance(tmp_path):
    """Preparation is read-only; the retired 0.1.0 binding survives byte for byte in private history."""
    import hashlib
    from importlib import metadata
    from pathlib import Path
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    _, fresh = _binding(instance)
    instance_id = instance.runtime.agentic_layer.instance_id
    _make_old(instance)
    path, record = _binding(instance)
    # The shipped 0.1.0 installation recorded 0.1.0 as the management seed supplier; its file hashes are unchanged.
    record["management_seed"] = {**fresh["management_seed"], "version": "0.1.0"}
    _write_binding(path, record)
    prior_bytes = path.read_bytes()
    prior_digest = hashlib.sha256(prior_bytes).hexdigest()
    # Ordinary opening refuses a version 1 binding read-only and leaves its provenance untouched.
    with pytest.raises(DomainError, match="offline upgrade"):
        open_installation(paths, ())
    assert path.read_bytes() == prior_bytes
    export = _export_root(instance)
    plan = upgrade.prepare_upgrade(paths, export)
    public = upgrade.public_plan(plan)
    installed_version = metadata.version("qorqut-agentic")
    assert installed_version == "0.2.0"
    # The plan reports the complete original supplier record, not only the hash map, and names the private destination.
    assert public["management_seed"]["previous_provenance"] == record["management_seed"]
    assert public["management_seed"]["previous"] == record["management_seed"]["files"]
    assert public["management_seed"]["desired"]["version"] == installed_version
    assert public["overseer_seed"]["previous_provenance"] is None      # the seed was genuinely absent before
    assert public["history"]["sha256"] == prior_digest
    assert public["history"]["snapshot"] == str(
        Path(paths.instance_root) / "config" / "agentic-layer-history" / (prior_digest + ".json"))
    assert path.read_bytes() == prior_bytes and not _history(instance).exists()
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    reopened, _ = open_installation(paths, ())
    assert reopened.instance_id == instance_id
    _, upgraded = _binding(instance)
    assert upgraded["version"] == 2
    assert upgraded["management_seed"]["version"] == installed_version
    assert upgraded["overseer_seed"]["version"] == installed_version
    assert upgraded["management_seed"]["files"] == plan["management_seed"]["desired"]["files"]
    assert upgraded["initial_commit"] == fresh["initial_commit"]
    # The exact retired record is retained in private history, keeping its own truthfully old supplier provenance.
    snapshot = Path(result["prior_binding"]["snapshot"])
    assert result["prior_binding"] == {"snapshot": str(_history(instance) / (prior_digest + ".json")),
                                       "sha256": prior_digest, "created": True}
    assert snapshot.read_bytes() == prior_bytes
    assert json.loads(snapshot.read_text())["management_seed"]["version"] == "0.1.0"
    assert stat.S_IMODE(os.stat(_history(instance)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(snapshot).st_mode) == 0o600
    assert sorted(p.name for p in _history(instance).iterdir()) == [prior_digest + ".json"]
    # A stale plan after success is refused and neither publishes nor replaces a snapshot.
    with pytest.raises(DomainError, match="changed since preparation"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert sorted(p.name for p in _history(instance).iterdir()) == [prior_digest + ".json"]
    assert snapshot.read_bytes() == prior_bytes
    # A current record that still claims the old distribution supplied its seeds is refused, so the obsolete fixed
    # check is replaced rather than bypassed.
    tampered = {**upgraded, "management_seed": {**upgraded["management_seed"], "version": "0.1.0"}}
    _write_binding(path, tampered)
    with pytest.raises(DomainError, match="Malformed agentic-management seed provenance"):
        open_installation(paths, ())


def test_interrupted_upgrade_reuses_one_retained_prior_binding(tmp_path, monkeypatch):
    """A crash between migration and the binding write leaves exactly one reusable history snapshot."""
    import hashlib
    from pathlib import Path
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    _make_old(instance)
    _downgrade_state_to_v1(paths.agentic_runtime)
    path, record = _binding(instance)
    assert record["version"] == 1
    prior_bytes = path.read_bytes()
    prior_digest = hashlib.sha256(prior_bytes).hexdigest()
    export = _export_root(instance)
    plan = upgrade.prepare_upgrade(paths, export)
    assert plan["state"]["migration_required"] is True
    real_replace = upgrade._replace
    monkeypatch.setattr(upgrade, "_replace", lambda source, target: (_ for _ in ()).throw(
        OSError("synthetic crash before the binding update")))
    with pytest.raises(OSError, match="synthetic crash"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    monkeypatch.setattr(upgrade, "_replace", real_replace)
    assert _binding(instance)[1]["version"] == 1                       # the binding was never replaced
    snapshot = Path(_history(instance)) / (prior_digest + ".json")
    assert snapshot.read_bytes() == prior_bytes
    assert sorted(p.name for p in _history(instance).iterdir()) == [snapshot.name]
    remaining = upgrade.prepare_upgrade(paths, export)
    assert remaining["state"]["migration_required"] is False
    result = upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert result["recovered_partial_attempt"] is True
    assert result["prior_binding"] == {"snapshot": str(snapshot), "sha256": prior_digest, "created": False}
    assert sorted(p.name for p in _history(instance).iterdir()) == [snapshot.name]
    assert snapshot.read_bytes() == prior_bytes
    _, upgraded = _binding(instance)
    assert upgraded["version"] == 2 and upgraded["management_seed"]["version"] == "0.2.0"


def test_a_differing_or_symlinked_history_snapshot_is_refused_without_overwrite(tmp_path):
    """An existing, unsafe or different history copy is refused instead of being replaced; nothing is applied."""
    import hashlib
    from pathlib import Path
    native = Native(tmp_path, "claude", role="learning-applier")
    instance = native.instance
    paths = instance.paths
    _make_old(instance)
    path, record = _binding(instance)
    prior_bytes = path.read_bytes()
    prior_digest = hashlib.sha256(prior_bytes).hexdigest()
    export = _export_root(instance)
    plan = upgrade.prepare_upgrade(paths, export)
    history = Path(_history(instance))
    history.mkdir(mode=0o700)
    os.chmod(history, 0o700)
    target = history / (prior_digest + ".json")
    target.write_bytes(b'{"synthetic": "other record"}\n')
    os.chmod(target, 0o600)
    with pytest.raises(DomainError, match="different bytes"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert target.read_bytes() == b'{"synthetic": "other record"}\n'
    assert _binding(instance)[1]["version"] == 1
    target.unlink()
    os.symlink(paths.instance_root / "config" / "agentic-layer.json", target)
    with pytest.raises(DomainError, match="private file"):
        upgrade.apply_upgrade(paths, plan, attribution=ATTRIBUTION)
    assert _binding(instance)[1]["version"] == 1
    assert sorted(p.name for p in history.iterdir()) == [target.name]
