"""The real combined dashboard against the real composed owner HTTP application.

The synthetic host is composed by ``story_associations_http_support`` from this version's own host support (assembled
schema and catalog, owned mutation admission, ``create_app`` with the dashboard compatibility routes, owner origin
checks and ``manage_lifecycle=False``) and serves the combined dashboard's static files. This is the acceptance check
for the Story contract: the actual dashboard must read ``tasks_associated_records`` over the ordinary business HTTP
transport and render the Story's recursive descendant-associated Actions and Signoffs exactly once.

The focused browser driver in the dashboard repository talks only to that server. It does not intercept, fabricate or
stub the new operation, its response or the Story record; the child record is opened through the real record workspace.
No live database, model, native agent, provider delivery, routine/job execution or service command runs here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from story_associations_http_support import BROWSER_TEST, build_story_associations, write_fixture

PLAYWRIGHT = os.environ.get("PLAYWRIGHT_MODULE", "/tmp/node_modules/playwright")


def browser_available():
    return shutil.which("node") is not None and Path(PLAYWRIGHT).is_dir() and BROWSER_TEST.is_file()


def _rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def _fixture_preconditions(host):
    """The synthetic seed has the exact ordinary stored shapes this acceptance check depends on."""
    fixture = host.fixture["story_associations"]
    tasks = {row["id"]: row for row in _rows(host, "SELECT id,parent_id FROM tasks WHERE id IN (?,?,?)",
                                             fixture["story"]["id"], fixture["child"]["id"], fixture["grandchild"]["id"])}
    assert tasks[fixture["story"]["id"]]["parent_id"] is None
    assert tasks[fixture["child"]["id"]]["parent_id"] == fixture["story"]["id"]
    assert tasks[fixture["grandchild"]["id"]]["parent_id"] == fixture["child"]["id"]
    associated = _rows(host, "SELECT refs_json,business_id FROM action_logs WHERE action_key=?",
                       fixture["action"]["id"])[0]
    unrelated = _rows(host, "SELECT refs_json,business_id FROM action_logs WHERE action_key=?",
                      fixture["unrelated_action"]["id"])[0]
    assert json.loads(associated["refs_json"]).get("task") == fixture["grandchild"]["id"], \
        "the Action is attached through canonical task_id"
    assert json.loads(unrelated["refs_json"]).get("task") == fixture["unrelated"]["id"], \
        "the unrelated Action belongs to another Task"
    assert associated["business_id"] == fixture["project_id"], "the Action is stored in the Story's Project scope"
    assert unrelated["business_id"] == fixture["project_id"], "the unrelated Action is in the same Project"
    assert _rows(host, "SELECT 1 FROM tasks WHERE id=? AND parent_id IS NULL",
                 fixture["unrelated"]["id"]), "the unrelated Task is not a Story descendant"
    duplicate = _rows(host, "SELECT count(*) count FROM relationships WHERE from_type='task' AND from_id=? "
                            "AND to_type='action' AND to_id=? AND role='concerns'",
                      fixture["child"]["id"], fixture["action"]["id"])[0]["count"]
    signoff = _rows(host, "SELECT count(*) count FROM relationships WHERE from_type='task' AND from_id=? "
                          "AND to_type='signoff' AND to_id=? AND role='concerns'",
                    fixture["child"]["id"], fixture["signoff"]["id"])[0]["count"]
    assert duplicate == 2, "the duplicate concerns association stayed two stored rows"
    assert signoff == 1, "the Signoff has one documented concerns association"


def _stored_shape(host):
    """Stored facts this read-only acceptance check must leave exactly as the real contract left them."""
    with host.database.read() as uow:
        tasks = sorted(tuple(row) for row in uow.connection.execute(
            "SELECT id,business_id,title,parent_id FROM tasks"))
        relationships = sorted(tuple(row) for row in uow.connection.execute(
            "SELECT id,business_id,from_type,from_id,to_type,to_id,role FROM relationships"))
        signoffs = sorted(tuple(row) for row in uow.connection.execute(
            "SELECT id,state,status_revision FROM signoffs"))
        messages = sorted(tuple(row) for row in uow.connection.execute(
            "SELECT c.source_key, count(m.sequence) FROM owner_conversations c "
            "LEFT JOIN owner_conversation_messages m ON m.conversation_id=c.id GROUP BY c.source_key"))
    return tasks, relationships, signoffs, messages


@pytest.mark.skipif(not browser_available(), reason="node/playwright or the dashboard's Story driver is unavailable")
def test_the_real_dashboard_reads_story_associated_records_over_http(tmp_path):
    host = build_story_associations(tmp_path)
    try:
        _fixture_preconditions(host)
        before = _stored_shape(host)
        fixture = tmp_path / "fixture.json"
        write_fixture(host, fixture)
        environment = {
            **os.environ,
            "STORY_ASSOCIATIONS_BASE": host.origin,
            "STORY_ASSOCIATIONS_FIXTURE": str(fixture),
            "PLAYWRIGHT_MODULE": PLAYWRIGHT,
        }
        completed = subprocess.run(["node", str(BROWSER_TEST)], cwd=str(BROWSER_TEST.parent), env=environment,
                                   capture_output=True, text=True, timeout=900)
        print(completed.stdout)
        print(completed.stderr)
        assert completed.returncode == 0, "the composed Story acceptance check failed"
        assert _stored_shape(host) == before, \
            "reading the Story changed a Task, relationship, Signoff or private message"
    finally:
        host.close()