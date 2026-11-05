"""Synthetic current-layout public coordinator paths."""
import json
import sqlite3

import pytest
from bazarlyk.agentic import upgrade
from bazarlyk.bootstrap import learning_removal as removal
from qorqut_operations.contracts import canonical_bytes
from learning_removal_preparation_support import build_predecessor, combined_release
from test_learning_removal_preparation import (cli_arguments, private_directory, registration_identity,
                                               run_cli, standard_request, write_json)


@pytest.mark.parametrize("managed", [True, False])
def test_current_layout_coordinator_adoption(tmp_path, capsys, managed):
    p = build_predecessor(tmp_path / "w")
    release = combined_release(tmp_path, p, upstream=True)
    plan = upgrade.prepare_upgrade(p.paths, release["export"])
    assert plan["kind"] == "agentic_managed_update_v2" and plan["complete"] and plan["changes"]
    private = private_directory(tmp_path, "private")
    release_file = write_json(private, "release.json", release)
    selection_file = release["selection"]
    common = ("--extensions-config", selection_file, "--release-reference", str(release_file))
    identity = registration_identity(p.paths)
    request_file = write_json(private, "request.json", standard_request(p, managed=managed, native=True))
    plan_file = private / "plan.json"
    status, prepared = run_cli(capsys, cli_arguments("prepare", p.paths, *common,
        "--coordinator-request", str(request_file), "--save", str(plan_file)))
    assert status == 0, prepared
    raw = plan_file.read_bytes()
    assert len(raw) <= removal.MAX_PLAN_BYTES
    assert len(canonical_bytes(prepared["result"]["preparation"]["inputs"])) <= removal.MAX_PREPARATION_BYTES
    assert b'content_base64' not in raw and b'Upstream guidance added by this release.' not in raw
    if managed:
        intent = next(item for item in prepared["result"]["preparation"]["inputs"]["derived"]["intents"]
                      if item["kind"] == "managed_upgrade")
        assert intent["arguments"]["plan_ref"]["plan_digest"] == plan["plan_digest"]
        assert intent["changes"] and "plan" not in intent
    kinds = {item["kind"] for item in prepared["result"]["preparation"]["steps"]}
    assert kinds == {"role_adoption", "profile_adoption", "native_preparation"} | ({"managed_upgrade"} if managed else set())
    status, result = run_cli(capsys, cli_arguments("apply", p.paths, "--extensions-config", selection_file,
        "--plan", str(plan_file), "--backup", str(private / "before.sqlite3"), "--coordinate"))
    assert status == 0, result
    assert registration_identity(p.paths) == identity
    with sqlite3.connect(p.paths.database) as db:
        profile = json.loads(db.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (p.agents["learning-applier"],)).fetchone()[0])
    assert profile["role"]["version"] == "1.17.0"
    assert profile["tool_policy"]["overrides"] == {"notes_list": "allow"}
