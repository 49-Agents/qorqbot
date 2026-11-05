"""The Task/Action counting readers and the pure settings merge behind the host's project views.

``projects.summary`` holds the four counting loops of the combined progress reads as small readers: the same SQL, one query
each, every row counted once. ``projects.settings.merge_settings`` is the recursive partial-update merge of a project's
configuration. Neither module needs a host."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from operational_support import AGENT, BUSINESS, OTHER, OperationalHost
from qorqut_operations.projects.settings import merge_settings
from qorqut_operations.projects.summary import (global_action_counts, global_task_completions, project_action_counts,
                                                project_task_completions)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


class Traced:
    """A unit of work over any connection that records each statement it runs."""

    def __init__(self, connection):
        self.statements = []
        connection.set_trace_callback(lambda sql: self.statements.append(" ".join(sql.split())))
        self.connection = connection

    def selects(self):
        return [sql for sql in self.statements if sql.upper().startswith("SELECT")]


def counter():
    counted = []
    return counted, lambda key, value: counted.append((key, value))


# -- settings ---------------------------------------------------------------------------------------------------------

def test_merge_settings_merges_nested_mappings_and_deletes_on_null():
    previous = {"a": 1, "nested": {"keep": True, "drop": 1, "deeper": {"x": 1, "y": 2}}, "list": [1, 2], "gone": "yes"}
    merged = merge_settings(previous, {"b": 2, "nested": {"drop": None, "deeper": {"y": 3, "z": 4}}, "list": [3], "gone": None})
    assert merged == {"a": 1, "b": 2, "nested": {"keep": True, "deeper": {"x": 1, "y": 3, "z": 4}}, "list": [3]}
    assert list(merged) == ["a", "nested", "list", "b"]                    # existing keys keep their place, new keys follow


def test_merge_settings_never_shares_or_mutates_its_inputs():
    previous = {"nested": {"values": [1, 2]}, "kept": {"deep": [0]}}
    changes = {"nested": {"added": {"items": [9]}}, "list": [{"k": 1}]}
    merged = merge_settings(previous, changes)
    assert previous == {"nested": {"values": [1, 2]}, "kept": {"deep": [0]}}
    assert changes == {"nested": {"added": {"items": [9]}}, "list": [{"k": 1}]}
    merged["nested"]["values"].append(3)
    merged["kept"]["deep"].append(1)
    merged["list"][0]["k"] = 2
    merged["nested"]["added"]["items"].append(10)
    assert previous["nested"]["values"] == [1, 2] and previous["kept"]["deep"] == [0]
    assert changes["list"] == [{"k": 1}] and changes["nested"]["added"]["items"] == [9]


def test_merge_settings_treats_a_missing_or_non_mapping_previous_value_as_empty():
    for previous in (None, [], "text", 3):
        assert merge_settings(previous, {"a": {"b": 1}, "c": None}) == {"a": {"b": 1}}
    assert merge_settings({"a": 1}, {}) == {"a": 1}
    assert merge_settings({"a": "scalar"}, {"a": {"b": 1}}) == {"a": {"b": 1}}       # a mapping replaces a scalar
    assert merge_settings({"a": {"b": 1}}, {"a": 5}) == {"a": 5}                    # a scalar replaces a mapping
    assert merge_settings({}, {"a": {"b": None}}) == {"a": {}}                       # a null inside a new mapping deletes nothing


# -- the project readers, over the real Task and Action schema -----------------------------------------------------

def test_project_readers_count_each_row_of_that_project_once_in_stored_order(host):
    done = [host.create_task(f"Done {index}") for index in range(3)]
    other = host.create_task("Elsewhere", ctx=host.agent(business=OTHER))
    with host.database.transaction() as uow:
        for task in done:
            uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?", (BUSINESS, task["id"]))
        uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?", (OTHER, other["id"]))
        uow.connection.execute("UPDATE tasks SET status='open' WHERE business_id=? AND id=?", (BUSINESS, done[0]["id"]))
        uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?", (BUSINESS, done[0]["id"]))   # a recompletion
        uow.connection.execute("UPDATE task_completions SET completed_at=? WHERE task_id=?", ("2026-09-13T12:00:00Z", done[1]["id"]))
        uow.connection.execute("UPDATE task_completions SET completed_at=NULL WHERE task_id=?", (done[2]["id"],))
    with host.database.read() as uow:
        traced = Traced(uow.connection)
        counted, count = counter()
        assert project_task_completions(uow, BUSINESS, count) is None
        assert traced.selects() == [f"SELECT completed_at FROM task_completions WHERE business_id='{BUSINESS}'"]
        stored = {row[0]: row[1] for row in uow.connection.execute("SELECT task_id, completed_at FROM task_completions WHERE business_id=?", (BUSINESS,))}
        assert len(counted) == 3 and all(key == "tasks_done" for key, _ in counted)          # once per first completion, not per status change
        assert sorted(value or "" for _, value in counted) == sorted(stored[task["id"]] or "" for task in done)
        assert ("tasks_done", "2026-09-13T12:00:00Z") in counted and ("tasks_done", None) in counted
        other_counted, other_count = counter()
        project_task_completions(uow, OTHER, other_count)
        assert len(other_counted) == 1
        empty, nothing = counter()
        project_task_completions(uow, "00000000-0000-4000-8000-0000000000ee", nothing)
        assert empty == []


def test_project_action_counts_include_shared_actions_once_and_split_dated_from_undated(host):
    own = host.record_action("Own", real_time="2026-09-10")
    backfill = host.record_action("Backfilled", real_time="2026-08-01")
    undated = host.record_action("Undated")
    shared = host.data("actions_record", {"text": "Shared", "category": "business-operational", "business_ids": [BUSINESS, OTHER],
                                          "real_time": "2026-09-10"}, host.coordinator())["action"]
    host.record_action("Elsewhere only", ctx=host.agent(business=OTHER), real_time="2026-09-10")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE action_logs SET occurred_date=NULL WHERE action_key=?", (undated["action_id"],))
    series = {"2026-09-09": {"actions": 0}, "2026-09-10": {"actions": 0}, "2026-09-11": {"actions": 0}}
    totals, no_date = {"actions": 0}, {"actions": 0}
    with host.database.read() as uow:
        traced = Traced(uow.connection)
        assert project_action_counts(uow, BUSINESS, totals, no_date, series) is None
        selects = traced.selects()
    assert selects == ["SELECT a.occurred_date FROM action_logs a JOIN action_businesses m ON m.action_key=a.action_key "
                       f"WHERE m.business_id='{BUSINESS}'"]
    assert totals == {"actions": 4} and no_date == {"actions": 1}                       # own, backfill, undated, shared: each once
    assert series == {"2026-09-09": {"actions": 0}, "2026-09-10": {"actions": 2}, "2026-09-11": {"actions": 0}}   # the backfill is out of range
    assert backfill["action_id"] and shared["action_id"].startswith("global/") and own["action_id"]
    other_series, other_totals, other_undated = {"2026-09-10": {"actions": 0}}, {"actions": 0}, {"actions": 0}
    with host.database.read() as uow:
        project_action_counts(uow, OTHER, other_totals, other_undated, other_series)
    assert other_totals == {"actions": 2} and other_undated == {"actions": 0} and other_series["2026-09-10"] == {"actions": 2}


def test_readers_add_to_what_the_caller_already_counted(host):
    host.record_action("One", real_time="2026-09-10")
    series = {"2026-09-10": {"actions": 5}}
    totals, no_date = {"actions": 7}, {"actions": 2}
    with host.database.read() as uow:
        project_action_counts(uow, BUSINESS, totals, no_date, series)
    assert totals == {"actions": 8} and no_date == {"actions": 2} and series == {"2026-09-10": {"actions": 6}}


# -- the Global readers, with the same SQL over bare tables -------------------------------------------------------

@pytest.fixture
def bare():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE task_completions (business_id TEXT, task_id TEXT NOT NULL, completed_at TEXT);
        CREATE TABLE action_logs (action_key TEXT NOT NULL, occurred_date TEXT);""")
    yield SimpleNamespace(connection=connection)
    connection.close()


def test_global_task_completions_counts_only_unowned_completions_once_each(bare):
    bare.connection.executemany("INSERT INTO task_completions VALUES(?,?,?)", [
        (None, "g1", "2026-09-10T12:00:00Z"), (None, "g2", None), (None, "g3", "2026-09-11T12:00:00Z"), (BUSINESS, "b1", "2026-09-10T12:00:00Z")])
    traced = Traced(bare.connection)
    counted, count = counter()
    assert global_task_completions(bare, count) is None
    assert traced.selects() == ["SELECT completed_at FROM task_completions WHERE business_id IS NULL"]
    assert counted == [("tasks_done", "2026-09-10T12:00:00Z"), ("tasks_done", None), ("tasks_done", "2026-09-11T12:00:00Z")]


def test_global_action_counts_bound_the_query_by_the_first_day_and_count_each_action_once(bare):
    bare.connection.executemany("INSERT INTO action_logs VALUES(?,?)", [
        ("a", "2026-09-08"), ("b", "2026-09-09"), ("c", "2026-09-10"), ("d", "2026-09-10"), ("e", "2026-09-12"), ("f", None), ("g", "2026-09-11")])
    series = {"2026-09-11": {"actions": 0, "emails_sent": 4}, "2026-09-09": {"actions": 0, "emails_sent": 0}, "2026-09-10": {"actions": 1, "emails_sent": 0}}
    traced = Traced(bare.connection)
    assert global_action_counts(bare, series) is None
    assert traced.selects() == ["SELECT occurred_date FROM action_logs WHERE occurred_date>='2026-09-09'"]     # bounded by min(series)
    assert series == {"2026-09-11": {"actions": 1, "emails_sent": 4}, "2026-09-09": {"actions": 1, "emails_sent": 0},
                      "2026-09-10": {"actions": 3, "emails_sent": 0}}           # the 12th is after the series, the 8th before it, no date is skipped


# -- the module boundary ------------------------------------------------------------------------------------------------------------------

def test_both_modules_load_without_a_host_or_context():
    script = (
        "import sys\n"
        "import qorqut_operations.projects.settings, qorqut_operations.projects.summary\n"
        "assert not [name for name in sys.modules if name.split('.')[0] in {'bazarlyk', 'qorqut_context'}]\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PATH": ""})
    assert done.returncode == 0, done.stderr
