"""Focused checks for the browser fixture's synthetic retained learning history.

The seed routine is executed against a real, isolated target store. The checks assert the selected revision and
work sequence, exact before/after audit continuity, and the recorded outcome; a refusing application proves no
procedural-learning operation is attempted.
"""
import ast
import json
import sqlite3
from pathlib import Path

from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
from bazarlyk.composition.schema_ddl import OWNER_SCHEMA
from bazarlyk.service.changes import ChangeFeed
from host_foundation_support import make_paths
from learning_history_seed import seed_learning_history
from qorqut_operations.contracts import new_id

FIXTURE = Path(__file__).with_name("service_browser_fixture.py")
RETIRED = {
    "learnings_engine_import", "learnings_engine_enrollment_set", "learnings_issue_action",
    "learnings_work_list", "learnings_work_get", "learnings_work_start", "learnings_work_defer",
    "learnings_work_done", "learnings_work_drop", "learnings_work_note_set",
}
MUTATIONS = RETIRED | {"learnings_review", "learnings_decide"}


def tree():
    return ast.parse(FIXTURE.read_text())


def invoked_names():
    names = set()
    for node in ast.walk(tree()):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def operation_arguments():
    """Strings passed as an operation name to invoke()/step(), not audit event labels stored in rows."""
    names = set()
    for node in ast.walk(tree()):
        if not isinstance(node, ast.Call):
            continue
        is_invoke = isinstance(node.func, ast.Attribute) and node.func.attr == "invoke"
        is_step = isinstance(node.func, ast.Name) and node.func.id == "step"
        if not (is_invoke or is_step):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                names.add(argument.value)
    return names


def test_the_fixture_never_invokes_procedural_learning_mutations():
    assert MUTATIONS.isdisjoint(invoked_names()), "the browser fixture regained a procedural-learning write"
    assert MUTATIONS.isdisjoint(operation_arguments()), "the browser fixture invokes a procedural-learning operation"


def test_seeded_columns_exist_in_the_real_learning_schema():
    connection = sqlite3.connect(":memory:")
    for statement in OWNER_SCHEMA["learning"]:
        connection.execute(statement)

    def columns(table):
        return {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}

    assert {"candidate_id", "state", "feedback", "changes_json", "decided_by", "revision", "created_at",
            "updated_at"} <= columns("learning_decisions")
    assert {"work_state", "agent_note", "work_outcome_json"} <= columns("learning_candidates")
    assert {"candidate_id", "event", "actor_id", "before_json", "after_json", "created_at"} <= columns("learning_history")


class RefusingApplication:
    """Any learning operation call is a fixture dependency that must not exist."""

    def __init__(self):
        self.calls = []

    def invoke(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("the browser fixture attempted a procedural-learning operation")


def isolated_store(tmp_path):
    database = initialize_empty_target(make_paths(tmp_path), host_assembly(ChangeFeed()))
    principal_id, business_id = new_id(), new_id()
    now = "2026-09-08T00:00:00Z"
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": principal_id, "kind": "agent", "display_name": "Astra",
            "active": 1, "created_at": now, "updated_at": now})
        uow.repository("businesses").insert({"id": business_id, "slug": "demo", "name": "Demo",
            "workspace": str(tmp_path / "demo"), "hq_path": str(tmp_path / "demo-hq"),
            "created_at": now, "updated_at": now})
    return database, business_id, principal_id


def history_rows(database, business_id, identity):
    with database.read() as uow:
        rows = [dict(row) for row in uow.connection.execute(
            "SELECT * FROM learning_history WHERE business_id = ? AND candidate_id = ? ORDER BY created_at, id",
            (business_id, identity))]
    for row in rows:
        row["before_json"] = json.loads(row["before_json"])
        row["after_json"] = json.loads(row["after_json"])
    return rows


def test_seeding_produces_the_selected_state_and_audit_chain(tmp_path):
    database, business_id, principal_id = isolated_store(tmp_path)
    refusing = RefusingApplication()
    ids = seed_learning_history(database, business_id, principal_id, application=refusing)
    assert refusing.calls == [], "seeding must not go through the application"

    def candidate(key):
        with database.read() as uow:
            return uow.repository("learning_candidates", business_id=business_id).get(ids[key])

    def decision(key):
        with database.read() as uow:
            return uow.repository("learning_decisions", business_id=business_id).get(ids[key])

    for key in ("open", "context"):
        assert candidate(key)["revision"] == 1, key
        with database.read() as uow:
            assert uow.repository("learning_decisions", business_id=business_id).get(ids[key]) is None, key
        assert history_rows(database, business_id, ids[key]) == [], key

    expected = {
        "accepted": (2, "accepted", None, ["learnings_review"]),
        "rejected": (2, "rejected", None, ["learnings_review"]),
        "active": (3, "accepted", "in-progress", ["learnings_review", "work_start"]),
        "dropped": (4, "accepted", "rejected-in-work",
                    ["learnings_review", "work_start", "work_drop"]),
        "done": (4, "accepted", "implemented", ["learnings_review", "work_start", "work_done"]),
    }
    for key, (revision, decided, work_state, events) in expected.items():
        row, record = candidate(key), decision(key)
        assert row["revision"] == revision, key
        assert record["state"] == decided, key
        assert row["work_state"] == work_state, key
        rows = history_rows(database, business_id, ids[key])
        assert [item["event"] for item in rows] == events, key
        for previous, following in zip(rows, rows[1:]):
            assert following["before_json"] == previous["after_json"], key + " before/after continuity"
            assert following["before_json"]["revision"] + 1 == following["after_json"]["revision"], key
        assert rows[0]["before_json"]["state"] == "open"
        assert rows[0]["after_json"]["decision"]["state"] == decided
        for item in rows[1:]:
            assert item["before_json"]["decision"]["state"] == decided, key + " decision remains recorded"
        if key == "dropped":
            assert rows[1]["after_json"]["state"] == "in-progress"
            assert rows[2]["before_json"]["state"] == "in-progress"
            assert row["agent_note"] == "Owner chose another approach <literal>"
        if key == "done":
            assert rows[1]["after_json"]["state"] == "in-progress"
            assert row["work_outcome_json"] == {"kind": "already-covered", "reason": "Verified in the fixture"}
            assert rows[-1]["after_json"]["outcome"] == row["work_outcome_json"]
