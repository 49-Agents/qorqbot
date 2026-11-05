"""Phase B: the executable offline preparation and migration coordinator, proved on disposable synthetic instances.

Every instance is built by the real owner paths (real role publication and registration, the real native preparation, a real
agentic layer) and converted to the exact frozen predecessor; the coordinator then runs the genuine Application, managed
upgrade and native refresh and migrates the schema. No live instance, provider, model, service or network is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
from unittest import mock

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut_operations.storage.database import Database

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.bootstrap import learning_removal_preparation as coordinator
from bazarlyk.bootstrap.learning_removal import TRANSITION_ID
from bazarlyk.extensions import NO_EXTENSIONS, select
from bazarlyk.service.ownership import InstanceOwnership
from bazarlyk.telegram.private_files import write_private_file
from learning_removal_preparation_support import (CUSTOM_OVERRIDES, ENDPOINT, RETIRED_OVERRIDES, VERSIONS,
                                                  build_predecessor, combined_release, forge_receipt,
                                                  open_target_runtime, saved_plan, seed_interrupted_review)
from test_learning_removal_migration import (OWNER, _remove_temporary_trees, private_directory, raw, release_for)

__all__ = ["_remove_temporary_trees"]

SHARED_LEARNING_TABLES = ("learning_candidates", "learning_decisions", "learning_history", "learning_runs",
                          "learning_scan_state")
# The only tables the four genuine stages write: the Application receipt and its idempotency receipt, the published role
# artifact, the adopted profile and the refreshed native registration (the change feed and audit tables stay untouched).
STAGE_TABLES = {"apply_receipts", "idempotency_receipts", "artifacts", "agent_profiles", "machine_registrations",
                "inbox_changes"}


# -- fixtures and helpers -------------------------------------------------------------------------------------------

def selected_entries(tmp_path) -> list:
    """The three real optional extensions (Content, Fastmail, Paper) with valid explicit configuration."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pytest.importorskip("qorqut_content.extension")
    pytest.importorskip("qorqut_paper.extension")
    pytest.importorskip("qorqut_fastmail.extension")
    key = tmp_path / "paper-key.pem"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return [{"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": False}},
            {"factory": "qorqut_fastmail.extension:create",
             "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": str(tmp_path / "token")}},
            {"factory": "qorqut_paper.extension:create",
             "config": {"host": {"base_url": "https://outreach.example.test", "audience": "outreach",
                                 "subject": "owner@example.test", "private_key_file": str(key),
                                 "dashboard_origin": "https://dashboard.example.test"},
                        "token_root": str(tmp_path / "paper-tokens")}}]


def profile_revision(paths, agent) -> int:
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        return connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()[0]
    finally:
        connection.close()


def standard_request(p, *, managed=False, native=False, context=None, adopt=True) -> dict:
    agent = p.agents["learning-applier"]
    return coordinator.normalise_request({
        "format": 1, "workspace": str(p.workspace), "roles": [{"source_id": "learning-applier"}] if adopt else [],
        "adoptions": [{"agent_id": agent, "role": "learning-applier", "expected_revision": profile_revision(p.paths, agent),
                       "remove_overrides": sorted(RETIRED_OVERRIDES)}] if adopt else [],
        "managed": managed,
        "native": [{"agent_id": agent, "endpoint": ENDPOINT, "versions": VERSIONS, "interpreter": None}] if native else [],
        "context": None if context is None else str(context)})


def registration_identity(paths) -> dict:
    """The native registration identities and every credential row: what a refresh must never replace or rotate."""
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        return {"registrations": [tuple(row) for row in connection.execute(
                    "SELECT id, principal_id, workspace, credential_id FROM machine_registrations ORDER BY id")],
                "credentials": [tuple(row) for row in connection.execute("SELECT * FROM credentials ORDER BY id")]}
    finally:
        connection.close()


def instance_fingerprint(root: Path) -> dict:
    """Bytes, modes and types of every path below an instance root (and its sidecars): a read-only claim's evidence."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        status = path.lstat()
        key = str(path.relative_to(root))
        if stat.S_ISREG(status.st_mode):
            result[key] = (stat.S_IMODE(status.st_mode), hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[key] = (stat.S_IMODE(status.st_mode), "link" if stat.S_ISLNK(status.st_mode) else "dir")
    return result


def baseline(paths, transition) -> dict:
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        return removal.baseline_manifest(connection, assembly=transition.predecessor)
    finally:
        connection.close()


def changed_tables(before: dict, after: dict) -> set:
    return set(removal.manifest_difference({"tables": before["tables"]}, {"tables": after["tables"]}))


@pytest.mark.parametrize("fault", ("profile_column", "native_endpoint", "git_config", "module_cursor"))
def test_unrelated_effect_inside_a_real_preparation_step_blocks_ddl(tmp_path, monkeypatch, fault):
    """A real successful operation is not permission to exempt its row or its container files from preservation."""
    managed = fault in ("git_config", "module_cursor")
    native = fault == "native_endpoint"
    p = build_predecessor(tmp_path / "w", native=native)
    release = combined_release(tmp_path, p)
    request = standard_request(p, managed=managed, native=native)
    plan = coordinator.plan_preparation(p.paths, request, release, extensions=p.extensions)
    agent = p.agents["learning-applier"]
    name = "_managed" if managed else "_native" if native else "_invoke"
    original = getattr(coordinator, name)

    def introduce_unrelated_change(*args):
        result = original(*args)
        if fault == "profile_column" and args[-1]["kind"] != "profile_adoption":
            return result
        if fault == "git_config":
            config = p.paths.agentic_root / ".git/config"
            config.write_text(config.read_text() + "\n[unexpected]\n\tsetting = changed\n")
        elif fault == "module_cursor":
            with sqlite3.connect(p.paths.agentic_runtime / "state.sqlite3") as db:
                db.execute("INSERT INTO consumers VALUES ('unrelated-consumer', 1, '2026-01-01T00:00:00Z')")
        else:
            db = removal._connect(p.paths.database, "rw")
            try:
                if fault == "profile_column":
                    db.execute("UPDATE agent_profiles SET name='unreviewed-name' WHERE agent_id=?", (agent,))
                else:
                    db.execute("UPDATE machine_registrations SET service_url='https://unreviewed.example' "
                               "WHERE principal_id=?", (agent,))
            finally:
                db.close()
        return result

    monkeypatch.setattr(coordinator, name, introduce_unrelated_change)
    dropped = []
    monkeypatch.setattr(removal, "_drop_table", lambda *args: dropped.append(args))
    private = private_directory(tmp_path, "private")
    with pytest.raises(DomainError) as caught:
        coordinator.run(p.paths, plan, backup=private / "before.sqlite3", extensions=p.extensions)
    assert caught.value.code in ("operation_conflict", "release_blocked"), str(caught.value)
    assert not dropped
    connection = removal._connect(p.paths.database, "ro")
    try:
        assert removal._recognise(connection, removal._transition_for(p.extensions))[0] == "predecessor"
    finally:
        connection.close()


def cli_arguments(command, paths, *extra):
    arguments = [command, "--instance-root", str(paths.instance_root)]
    for name, location in paths.components.items():
        arguments += ["--component", f"{name}={location}"]
    return [*arguments, *extra]


def write_json(directory: Path, name: str, value) -> Path:
    return write_private_file(directory, name, (json.dumps(value, sort_keys=True) + "\n").encode())


def run_cli(capsys, arguments):
    status = removal.main(arguments)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return status, json.loads(out)


class Boundary:
    """Every point at which a second owner would be a violation: records that a concurrent acquisition always fails."""

    def __init__(self, monkeypatch, paths):
        self.paths, self.seen = paths, []
        for module, name in ((removal, "_publish_backup"), (coordinator, "_invoke"), (coordinator, "_managed"),
                             (coordinator, "_native"), (removal, "_baseline_with_files"), (removal, "_drop_table"),
                             (removal, "_update_identity"), (removal, "_verify_target")):
            monkeypatch.setattr(module, name, self._wrap(name, getattr(module, name)))
        monkeypatch.setattr(removal.PlanAttempt, "_publish_completion",
                            self._wrap("_publish_completion", removal.PlanAttempt._publish_completion))

    def _wrap(self, name, original):
        def probe(*args, **kwargs):
            with pytest.raises(DomainError):
                InstanceOwnership(self.paths).acquire()
            if name not in self.seen:
                self.seen.append(name)
            return original(*args, **kwargs)
        return probe


# -- the flagship: a complete selected-extension predecessor-to-target transition -----------------------------------

def test_complete_selected_extension_transition_through_the_cli(tmp_path, monkeypatch, capsys):
    entries = selected_entries(tmp_path)
    selection = select(entries)
    p = build_predecessor(tmp_path / "w", extensions=selection, agentic_sources=("content", "paper", "fastmail"))
    paths, agent = p.paths, p.agents["learning-applier"]
    transition = removal._transition_for(selection)
    assert transition.predecessor.target.digest != transition.target.target.digest
    # Normal startup rejects the exact predecessor: it is never served by the current composition.
    with pytest.raises(DomainError):
        Database(paths.database, assembly=transition.target).open_existing()

    # An owner's own grant, a custom note outside the managed content, a foreign native MCP server and a note in the
    # native workspace must survive.
    custom_package = paths.agentic_root / "OWNER-NOTES.md"
    custom_package.write_text("Owner-authored guidance outside the managed content.\n")
    preference = paths.agentic_root / "user-preferences/SKILL.md"
    preference.write_text("Synthetic customized owner preference.\n")
    custom = paths.agentic_root / "plugins/custom-owner"
    custom.mkdir()
    (custom / "agentic.yaml").write_text("schema_version: 1\nid: custom-owner\nname: Custom\ndescription: Custom\nentries: []\n")
    (custom / "notes.md").write_text("Synthetic owner plugin content.\n")
    from test_agentic_managed_plan import scan_and_commit
    scan_and_commit(paths)
    workspace = paths.operations / "agents" / agent
    settings_path, servers_path = workspace / ".claude" / "settings.json", workspace / ".mcp.json"
    settings = json.loads(settings_path.read_text())
    settings["enabledMcpjsonServers"] = [*settings.get("enabledMcpjsonServers", []), "owner-tool"]
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    servers = json.loads(servers_path.read_text())
    servers["mcpServers"]["owner-tool"] = {"command": "owner-tool", "args": ["--owner-configured"]}
    servers_path.write_text(json.dumps(servers, indent=2) + "\n")
    notes_path = workspace / "OWNER-NOTES.md"
    notes_path.write_text("Owner notes in the native workspace.\n")
    session_identity = registration_identity(paths)
    assert len(session_identity["registrations"]) == 1 and session_identity["credentials"]

    private = private_directory(tmp_path, "private")
    release = combined_release(tmp_path, p, entries=entries)
    assert sorted(release["extension_components"]) == ["qorqut_content", "qorqut_fastmail", "qorqut_paper"]
    request = standard_request(p, managed=True, native=True)
    request_file = write_json(private, "request.json", request)
    release_file = write_json(private, "release.json", release)
    selection_file = Path(release["selection"])
    before_plan = instance_fingerprint(paths.instance_root)
    plan_file = private / "plan.json"
    arguments = cli_arguments("prepare", paths, "--extensions-config", str(selection_file),
                              "--coordinator-request", str(request_file), "--release-reference", str(release_file),
                              "--save", str(plan_file))
    status, response = run_cli(capsys, arguments)
    assert status == 0 and response["ok"] is True, response
    plan = response["result"]
    # Planning is read-only and deterministic: nothing in the instance changed and a second plan is byte-identical.
    assert instance_fingerprint(paths.instance_root) == before_plan
    again = private / "plan-again.json"
    status, second = run_cli(capsys, cli_arguments("prepare", paths, "--extensions-config", str(selection_file),
                                                   "--coordinator-request", str(request_file),
                                                   "--release-reference", str(release_file), "--save", str(again)))
    assert status == 0 and second["result"]["plan_digest"] == plan["plan_digest"]
    assert instance_fingerprint(paths.instance_root) == before_plan
    kinds = [(step["kind"], step["identity"].split(":")[0]) for step in plan["preparation"]["steps"]]
    assert kinds == [("managed_upgrade", "managed"), ("native_preparation", "native"), ("profile_adoption", "profile"),
                     ("role_adoption", "role")]
    assert plan["release_evidence"]["extension_sources"]["installation"] is None
    assert sorted(plan["release_evidence"]["extension_sources"]["packages"]) == [
        "qorqut_content", "qorqut_fastmail", "qorqut_paper"]

    before = baseline(paths, transition)
    boundary = Boundary(monkeypatch, paths)
    backup = private / "before.sqlite3"
    status, applied = run_cli(capsys, cli_arguments("apply", paths, "--extensions-config", str(selection_file),
                                                    "--plan", str(plan_file), "--backup", str(backup), "--coordinate"))
    assert status == 0 and applied["ok"] is True, applied
    result = applied["result"]
    assert set(boundary.seen) == {"_publish_backup", "_invoke", "_managed", "_native", "_baseline_with_files",
                                  "_drop_table", "_update_identity", "_verify_target", "_publish_completion"}
    Path(os.environ.get("PHASE_B_DUMP", os.devnull)).write_text(json.dumps(
        {"steps": result["preparation"]["steps"], "accounting": result.get("accounting")}, indent=1, default=str))
    assert result["applied"] is True and result["verification"]["target"] == "exact"
    assert result["verification"]["retained_rows"] == "exact"
    assert result["verification"]["identity"]["digest"] == transition.target.target.digest
    operations = {step["identity"].split(":")[0]: step["operation"] for step in result["preparation"]["steps"]}
    assert operations == {"role": "roles_publish", "profile": "agents_update", "managed": "agentic_upgrade_apply",
                          "native": "native_prepare_agent"}
    InstanceOwnership(paths).acquire().release()

    # The shared learning tables and every unrelated table are exactly what they were; only the stages' own tables moved.
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        after = removal.baseline_manifest(connection, assembly=transition.target)
        profile = json.loads(connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?",
                                                (agent,)).fetchone()[0])
        kept = {name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY 1, 2')]
                for name in ("principals",)}
    finally:
        connection.close()
    moved = changed_tables({"tables": {k: v for k, v in before["tables"].items() if k in after["tables"]}}, after)
    assert moved == STAGE_TABLES | {"target_identity"}, moved
    assert not moved & set(SHARED_LEARNING_TABLES)
    for table in SHARED_LEARNING_TABLES:
        assert after["tables"][table] == before["tables"][table]
    assert registration_identity(paths) == session_identity
    assert profile["tool_policy"]["overrides"] == CUSTOM_OVERRIDES
    assert profile["role"]["version"] != p.old_pin["version"]
    assert custom_package.read_text() == "Owner-authored guidance outside the managed content.\n"
    assert preference.read_text() == "Synthetic customized owner preference.\n"
    assert (custom / "notes.md").read_text() == "Synthetic owner plugin content.\n"
    assert json.loads(servers_path.read_text())["mcpServers"]["owner-tool"] == {
        "command": "owner-tool", "args": ["--owner-configured"]}
    assert "owner-tool" in json.loads(settings_path.read_text())["enabledMcpjsonServers"]
    assert notes_path.read_text() == "Owner notes in the native workspace.\n"
    assert kept["principals"]

    # An exact-target retry is the verified no-op: no preparation, no second backup, no changed evidence.
    evidence_before = instance_fingerprint(private)
    retry_backup = private / "second.sqlite3"
    status, retry = run_cli(capsys, cli_arguments("apply", paths, "--extensions-config", str(selection_file),
                                                  "--plan", str(plan_file), "--backup", str(retry_backup), "--coordinate"))
    assert status == 0 and retry["result"]["applied"] is False and retry["result"]["already_migrated"] is True
    assert not retry_backup.exists() and instance_fingerprint(private) == evidence_before


# -- the no-preparation (core-only) path and the exact schema transaction -------------------------------------------

def core_instance(tmp_path):
    from test_learning_removal_migration import add_candidate, make_predecessor, seed_principal
    paths, transition = make_predecessor(tmp_path)
    connection = raw(paths)
    try:
        seed_principal(connection)
        add_candidate(connection)
        connection.commit()
    finally:
        connection.close()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = coordinator.normalise_request({"format": 1, "workspace": str(workspace)})
    plan = coordinator.plan_preparation(paths, request, release_for(paths))
    return paths, transition, plan


def test_core_only_transition_needs_no_preparation_and_keeps_one_owner(tmp_path, monkeypatch):
    paths, transition, plan = core_instance(tmp_path)
    assert plan["preparation"]["steps"] == [] and plan["preparation"]["files"] == []
    boundary = Boundary(monkeypatch, paths)
    backup = private_directory(tmp_path) / "before.sqlite3"
    before = baseline(paths, transition)
    result = coordinator.run(paths, plan, backup=backup)
    assert result["applied"] is True and result["verification"]["retained_rows"] == "exact"
    assert result["preparation"]["steps"] == [] and backup.is_file()
    assert {"_publish_backup", "_baseline_with_files", "_drop_table", "_update_identity", "_verify_target",
            "_publish_completion"} <= set(boundary.seen)
    assert not {"_invoke", "_managed", "_native"} & set(boundary.seen)
    after = baseline_target(paths, transition)
    for table in SHARED_LEARNING_TABLES:
        assert after["tables"][table] == before["tables"][table]
    # The owner is free again, and a second coordination is the verified no-op: no new backup, no second preparation.
    InstanceOwnership(paths).acquire().release()
    second = private_directory(tmp_path) / "second.sqlite3"
    retry = coordinator.run(paths, plan, backup=second)
    assert retry["applied"] is False and retry["already_migrated"] is True and not second.exists()


def baseline_target(paths, transition) -> dict:
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        return removal.baseline_manifest(connection, assembly=transition.target)
    finally:
        connection.close()


def attempt_directory(paths, plan) -> Path:
    base = removal._receipt_directory(paths) / plan["plan_digest"]
    (directory,) = [entry for entry in base.iterdir() if entry.is_dir()]
    return directory


@pytest.mark.parametrize("fail_after", (0, 3, 6, "identity", "preserved"))
def test_a_fault_at_every_stage_of_the_schema_transaction_rolls_back_to_the_predecessor_and_b(tmp_path, monkeypatch,
                                                                                              fail_after):
    paths, transition, plan = core_instance(tmp_path)
    backup = private_directory(tmp_path) / "before.sqlite3"
    original_drop, dropped = removal._drop_table, []

    def failing_drop(connection, table):
        if isinstance(fail_after, int) and len(dropped) >= fail_after:
            raise DomainError("storage_error", "synthetic DDL failure")
        dropped.append(table)
        return original_drop(connection, table)

    monkeypatch.setattr(removal, "_drop_table", failing_drop)
    if fail_after == "identity":
        monkeypatch.setattr(removal, "_update_identity",
                            lambda *_: (_ for _ in ()).throw(DomainError("storage_error", "synthetic identity failure")))
    if fail_after == "preserved":
        monkeypatch.setattr(removal, "_require_preserved",
                            lambda *_: (_ for _ in ()).throw(DomainError("unsupported_schema", "synthetic late failure")))
    before = baseline(paths, transition)
    with pytest.raises(DomainError):
        coordinator.run(paths, plan, backup=backup)
    assert removal.inspect_migration(paths)["state"] == "predecessor"
    assert baseline(paths, transition)["tables"] == before["tables"]
    assert backup.is_file()
    # Baseline B and the precommit marker stay with the attempt for a separately authorized, matched restore; nothing was
    # restored, retried or released by the coordinator.
    names = {entry.name for entry in attempt_directory(paths, plan).iterdir()}
    assert "precommit.json" in names and any(name.startswith("baseline-b") for name in names)
    InstanceOwnership(paths).acquire().release()


def test_a_change_after_b_fails_the_preservation_check_and_rolls_back(tmp_path, monkeypatch):
    paths, transition, plan = core_instance(tmp_path)
    original_drop = removal._drop_table

    def tampering_drop(connection, table):
        connection.execute("UPDATE principals SET display_name='changed after B' WHERE 1")
        return original_drop(connection, table)

    monkeypatch.setattr(removal, "_drop_table", tampering_drop)
    with pytest.raises(DomainError):
        coordinator.run(paths, plan, backup=private_directory(tmp_path) / "before.sqlite3")
    assert removal.inspect_migration(paths)["state"] == "predecessor"
    connection = raw(paths)
    try:
        assert connection.execute("SELECT display_name FROM principals WHERE id=?", (OWNER,)).fetchone()[0] == "Owner"
    finally:
        connection.close()


# -- pending work is proved with the real target contracts ------------------------------------------------------------

def context_file(directory: Path, name: str, source_bytes: int) -> Path:
    return write_private_file(private_directory(directory, name), "context.json",
                              json.dumps({"source_bytes": source_bytes}).encode())


def context_settings(path: Path):
    from bazarlyk.shared_context.service import runner_config_from_file
    from qorqut_context.integration.service import ContextSettings
    return ContextSettings(config=runner_config_from_file(path))


def pending_counts(paths, transition, plan) -> dict:
    target = removal._target_for(paths, transition, transition.extensions, plan["preparation"])
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        return removal._pending_receipts(connection, transition.extensions, target)
    finally:
        connection.close()


def test_a_compatible_interrupted_intake_migrates_then_resumes_exactly_once(tmp_path):
    configured = context_file(tmp_path, "context-configured", 64 * 1024)
    tightened = context_file(tmp_path, "context-tightened", 4096)
    state = {}

    def interrupted(instance, ids):
        state["intake"] = seed_interrupted_review(instance, markdown_bytes=20000)

    p = build_predecessor(tmp_path / "w", native=False, old_role=False, context=context_settings(configured),
                          prepare=interrupted)
    paths, transition = p.paths, removal._transition_for(p.extensions)
    release = release_for(paths, p.extensions)

    # The configured non-default limit admits the saved plan; a tighter one is a real incompatibility, never a default.
    blocked = coordinator.plan_preparation(paths, standard_request(p, adopt=False, context=tightened), release)
    assert blocked["gates"]["ready"] is False and blocked["gates"]["incompatible_receipts"] == 1
    with pytest.raises(DomainError) as refused:
        removal.begin_attempt(paths, blocked, backup=private_directory(tmp_path) / "never.sqlite3")
    assert refused.value.code == "release_blocked" and not (private_directory(tmp_path) / "never.sqlite3").exists()
    plan = coordinator.plan_preparation(paths, standard_request(p, adopt=False, context=configured), release)
    assert plan["gates"]["ready"] is True and plan["gates"]["pending_factual_receipts"] == 1
    assert pending_counts(paths, transition, plan)["factual"] == 1

    result = coordinator.run(paths, plan, backup=private_directory(tmp_path) / "before.sqlite3")
    assert result["applied"] is True and result["verification"]["retained_rows"] == "exact"
    connection = removal._connect(removal._database_path(paths), "ro")
    try:
        receipt = connection.execute("SELECT state FROM apply_receipts WHERE operation_name='learnings_review'").fetchone()
        decisions = connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0]
    finally:
        connection.close()
    # Migration neither executed nor recovered the pending work.
    assert receipt[0] == "failed" and decisions == 0

    runtime, owner = open_target_runtime(p, context_settings(configured))
    try:
        options = InvocationOptions(idempotency_key=state["intake"]["key"], expected_revision=1)
        resumed = runtime.application.invoke(owner, "learnings_review", state["intake"]["arguments"], options)
        assert resumed.status == "ok", resumed.to_dict()
        replay = runtime.application.invoke(owner, "learnings_review", state["intake"]["arguments"], options)
        assert replay.status == "ok"
        with p_database(runtime) as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 1
            assert uow.connection.execute("SELECT state FROM apply_receipts WHERE operation_name='learnings_review'"
                                          ).fetchone()[0] == "applied"
    finally:
        runtime.release_instance()


def p_database(runtime):
    return runtime.database.read()


def test_procedural_mixed_removed_table_unknown_and_stale_pending_work_blocks(tmp_path):
    from dataclasses import replace
    from qorqut_operations.contracts import ExpectedTarget, PreparedChange, ResourceRef, utc_now
    state = {}

    def seeded(instance, ids):
        genuine = seed_interrupted_review(instance, key="genuine")
        plan, arguments = saved_plan(instance, "genuine"), genuine["arguments"]
        business = instance.business
        delta = "5b1d0e0e-0000-4000-8000-0000000000d1"
        now = utc_now()
        with instance.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                (business, delta, json.dumps({"text": "Old procedural rule", "scope": "biz", "bucket": "delta",
                                              "tag": "rule", "run": "run-1", "evidence": {"session": "s-2"}}), now, now))

        def candidate(identity, values, *, action="insert", exists=False):
            resource = ResourceRef("learning_candidates", identity, business_id=business)
            return (PreparedChange("record", resource, action, values),
                    ExpectedTarget(resource, exists=exists, revision=1 if exists else None))

        def extended(change, target):
            return replace(plan, changes=(*plan.changes, change), expected_targets=(*plan.expected_targets, target))

        forged = {}
        change, target = candidate("forged-delta", {"business_id": business, "created_at": now, "updated_at": now,
                                                    "candidate_json": {"text": "Forged procedural delta", "scope": "biz",
                                                                       "bucket": "delta", "tag": "rule"}})
        forged["procedural"] = forge_receipt(instance, extended(change, target), "forged-procedural", arguments)
        change, target = candidate(delta, {"candidate_json": {"text": "rewritten"}, "updated_at": now},
                                   action="update", exists=True)
        forged["mixed"] = forge_receipt(instance, extended(change, target), "forged-mixed", arguments)
        resource = ResourceRef("learning_issues", "removed-issue", business_id=business)
        forged["removed_table"] = forge_receipt(
            instance, extended(PreparedChange("record", resource, "insert", {"business_id": business, "id": "removed-issue",
                                                                              "title": "Retired", "state": "open",
                                                                              "created_at": now, "updated_at": now}),
                               ExpectedTarget(resource, exists=False, revision=None)), "forged-removed", arguments)
        forged["unknown"] = forge_receipt(instance, replace(plan, operation_name="learnings_engine_mystery"),
                                          "forged-unknown", arguments, as_operation="learnings_review")
        seed_interrupted_review(instance, key="stale", candidate_id="5b1d0e0e-0000-4000-8000-0000000000c2")
        with instance.database.transaction() as uow:
            uow.connection.execute("UPDATE learning_candidates SET revision=2 WHERE id=?",
                                   ("5b1d0e0e-0000-4000-8000-0000000000c2",))
        state["forged"] = forged

    p = build_predecessor(tmp_path / "w", native=False, old_role=False, prepare=seeded)
    paths, transition = p.paths, removal._transition_for(p.extensions)
    plan = coordinator.plan_preparation(paths, standard_request(p, adopt=False), release_for(paths))
    counts = pending_counts(paths, transition, plan)
    forged = state["forged"]
    # Exactly the one genuine, unchanged intake is compatible; every forged or newly stale plan is a named blocker.
    assert counts["factual"] == 1, json.dumps({**counts, "forged": forged}, default=str)
    assert counts["engine"] == 1 and counts["unclassified"] + counts["unrelated"] == 1
    details = counts["details"]
    assert details[forged["procedural"]]["status"] == "incompatible"
    assert details[forged["mixed"]]["status"] == "incompatible"
    assert [info["reason"] for receipt, info in details.items() if receipt not in forged.values()] == ["revision_conflict"]
    assert counts["incompatible"] == 3 and counts["unverified"] == 0
    assert plan["gates"]["ready"] is False
    backup = private_directory(tmp_path) / "never.sqlite3"
    with pytest.raises(DomainError) as refused:
        coordinator.run(paths, plan, backup=backup)
    assert refused.value.code == "release_blocked" and not backup.exists()
    assert removal.inspect_migration(paths)["state"] == "predecessor"
    InstanceOwnership(paths).acquire().release()


def test_pending_input_and_task_capture_survive_real_preparation(tmp_path, monkeypatch):
    import copy
    from bazarlyk.bootstrap import learning_removal_evidence as evidence
    from qorqut_operations.contracts import utc_now
    state = {}

    def seed(instance, ids):
        form = {'title': 'Synthetic question', 'explanation': 'Choose a direction',
                'kind': 'question', 'selection_mode': 'none'}
        result = instance.runtime.application.invoke(instance.human(instance.business), 'signoffs_input_create',
            {'request_key': 'synthetic-question', 'form': form}, {'idempotency_key': 'synthetic-question'})
        assert result.status == 'ok', result.to_dict()
        state['question'] = result.data['signoff']

    p = build_predecessor(tmp_path / 'w', prepare=seed)
    connection = raw(p.paths)
    try:
        stamp = utc_now()
        connection.execute("INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at) VALUES (?,?,?,'open',?,?,?)",
                           (p.business, 'synthetic-pending-task', 'Synthetic pending task', p.owner, stamp, stamp))
        connection.commit()
        capture = connection.execute('SELECT * FROM inbox_changes ORDER BY id').fetchall()
        events = connection.execute('SELECT COUNT(*) FROM inbox_events').fetchone()[0]
        assert capture
    finally:
        connection.close()
    plan = coordinator.plan_preparation(p.paths, standard_request(p, native=True), combined_release(tmp_path, p))
    assert plan['gates']['ready'], plan['gates']
    original_verifier = evidence._verify_profile
    checked = []

    def verify_with_negative_cases(intent, result, connection, paths, attempt):
        allowed = original_verifier(intent, result, connection, paths, attempt)
        for variant in ('duplicate', 'old', 'changed', 'wrong_actor', 'wrong_image'):
            forged = copy.deepcopy(result)
            claim = next(item for item in forged['rows'] if item['table'] == 'inbox_changes')
            if variant == 'duplicate':
                forged['rows'].append(copy.deepcopy(claim))
            elif variant == 'old':
                claim['before'] = claim['after']
            elif variant == 'changed':
                claim['after'] = '0' * 64
            if variant in ('wrong_actor', 'wrong_image'):
                old_row_image = evidence.row_image
                def altered(conn, assembly, table, keys):
                    image = old_row_image(conn, assembly, table, keys)
                    if table == 'inbox_changes':
                        image = copy.deepcopy(image)
                        for pair in image:
                            if pair[0] == ('actor_id' if variant == 'wrong_actor' else 'after_json'):
                                pair[1] = 'not-the-actor' if variant == 'wrong_actor' else '{}'
                        claim['after'] = evidence.image_fingerprint(image)
                    return image
                with monkeypatch.context() as patch:
                    patch.setattr(evidence, 'row_image', altered)
                    with pytest.raises(DomainError):
                        original_verifier(intent, forged, connection, paths, attempt)
            else:
                with pytest.raises(DomainError):
                    original_verifier(intent, forged, connection, paths, attempt)
        checked.append(True)
        return allowed

    monkeypatch.setitem(evidence._VERIFIERS, 'profile_adoption', verify_with_negative_cases)
    result = coordinator.run(p.paths, plan, backup=private_directory(tmp_path) / 'before.sqlite3')
    assert result['applied'] and checked
    connection = raw(p.paths)
    try:
        after = connection.execute('SELECT * FROM inbox_changes ORDER BY id').fetchall()
        assert after[:len(capture)] == capture and len(after) == len(capture) + 1
        assert after[-1][0] > capture[-1][0]
        assert connection.execute('SELECT COUNT(*) FROM inbox_events').fetchone()[0] == events
    finally:
        connection.close()
    runtime, owner = open_target_runtime(p)
    try:
        q = state['question']
        answer = runtime.application.signoffs.respond(owner, q['id'], notes='Synthetic answer', selected_choices=[],
            proposal_revision=q['proposal_revision'], payload_hash=q['payload_hash'],
            expected_status_revision=q['status_revision'], idempotency_key='synthetic-answer')
        assert answer.status == 'ok', answer.to_dict()
        assert answer.data['signoff']['state'] == 'answered'
        with runtime.database.read() as uow:
            assert uow.connection.execute('SELECT COUNT(*) FROM inbox_changes').fetchone()[0] == 0
    finally:
        runtime.release_instance()


def test_offline_assembly_excludes_only_identifiable_hook():
    from dataclasses import replace
    from functools import partial
    from bazarlyk.bootstrap.learning_removal_runtime import _defer_inbox_finalizer
    from bazarlyk.inbox.events import finalize_events
    assembly = removal._transition_for(NO_EXTENSIONS).predecessor
    other = lambda uow: None
    for hook in (finalize_events, partial(finalize_events, sources=object())):
        original = replace(assembly, finalizers=(other, hook))
        result = _defer_inbox_finalizer(original)
        assert result.finalizers == (other,) and original.finalizers == (other, hook)
        assert result.target is original.target and result.schema is original.schema
        assert result.change_publisher is original.change_publisher
    with pytest.raises(DomainError):
        _defer_inbox_finalizer(replace(assembly, finalizers=(other,)))
    with pytest.raises(DomainError):
        _defer_inbox_finalizer(replace(assembly, finalizers=(finalize_events, partial(finalize_events))))


def test_stored_input_compatibility_and_answer_without_creation_replay(tmp_path):
    from dataclasses import replace
    from qorqut_operations.contracts import ChangeSet, PreparedChange, ResourceRef
    from bazarlyk.bootstrap.learning_removal_pending import prove_input_request
    state = {}

    def seed(instance, ids):
        form = {'title': 'Synthetic question', 'explanation': 'Choose a direction',
                'kind': 'question', 'selection_mode': 'none'}
        result = instance.runtime.application.invoke(instance.human(instance.business), 'signoffs_input_create',
            {'request_key': 'synthetic-question', 'form': form}, {'idempotency_key': 'synthetic-question'})
        assert result.status == 'ok', result.to_dict()
        state['question'] = result.data['signoff']

    p = build_predecessor(tmp_path / 'w', native=False, old_role=False, prepare=seed)
    transition = removal._transition_for(p.extensions)
    plan = coordinator.plan_preparation(p.paths, standard_request(p, adopt=False), release_for(p.paths))
    assert plan['gates']['ready'], plan['gates']
    target = removal._target_for(p.paths, transition, p.extensions, plan['preparation'])
    source = raw(p.paths)
    memory = sqlite3.connect(':memory:')
    try:
        source.backup(memory)
        memory.row_factory = sqlite3.Row
        row = memory.execute('SELECT * FROM signoffs WHERE id=?', (state['question']['id'],)).fetchone()
        saved = ChangeSet.from_bytes(row['change_set'], expected_hash=row['payload_hash'])
        prove_input_request(target, memory, row['id'])
        # The disposable in-memory copy permits construction of tampered stored plans.
        for trigger in memory.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='signoffs'").fetchall():
            memory.execute('DROP TRIGGER "' + trigger[0].replace('"', '""') + '"')
        extra = PreparedChange('record', ResourceRef('learning_issues', 'forged'), 'insert', {})
        cases = [replace(saved, changes=(*saved.changes, extra)),
                 replace(saved, operation_version=99),
                 replace(saved, changes=(replace(saved.changes[0], action='input_cancel'),)),
                 replace(saved, changes=(replace(saved.changes[0], resource=ResourceRef('signoffs', 'bad-id', p.business)),))]
        for forged in cases:
            memory.execute('UPDATE signoffs SET change_set=?, payload_hash=? WHERE id=?',
                           (forged.canonical, forged.payload_hash, row['id']))
            with pytest.raises(DomainError):
                prove_input_request(target, memory, row['id'])
        memory.execute('UPDATE signoffs SET change_set=?, payload_hash=?, preview_json=? WHERE id=?',
                       (saved.canonical, saved.payload_hash, '{}', row['id']))
        with pytest.raises(DomainError):
            prove_input_request(target, memory, row['id'])
        memory.execute('UPDATE signoffs SET preview_json=?, payload_hash=? WHERE id=?',
                       (row['preview_json'], '0' * 64, row['id']))
        with pytest.raises(DomainError):
            prove_input_request(target, memory, row['id'])
        memory.execute('UPDATE signoffs SET payload_hash=?, proposer_principal_id=? WHERE id=?',
                       (saved.payload_hash, p.agents['ordubashy'], row['id']))
        with pytest.raises(DomainError):
            prove_input_request(target, memory, row['id'])
    finally:
        source.close()
        memory.close()
    result = coordinator.run(p.paths, plan, backup=private_directory(tmp_path) / 'before.sqlite3')
    assert result['applied']
    runtime, owner = open_target_runtime(p)
    try:
        q = state['question']
        answer = runtime.application.signoffs.respond(owner, q['id'], notes='Synthetic answer', selected_choices=[],
            proposal_revision=q['proposal_revision'], payload_hash=q['payload_hash'],
            expected_status_revision=q['status_revision'], idempotency_key='synthetic-answer')
        assert answer.status == 'ok', answer.to_dict()
        assert answer.data['signoff']['state'] == 'answered'
    finally:
        runtime.release_instance()
