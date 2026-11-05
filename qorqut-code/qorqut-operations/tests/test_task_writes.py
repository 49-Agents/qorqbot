"""Tasks written inside another domain's plan are announced once, in the same unit of work.

``TaskWrites.apply`` reads each touched Task before and after the plan's records are applied and hands the difference to
``TaskOperations.announce``: follows, the thread entry and the one inbox event, in the writing transaction and never
after it."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from operational_support import AGENT, BUSINESS, OperationalHost
from qorqut_operations.contracts import InvocationOptions, PreparedChange, ResourceRef, new_id
from qorqut_operations.tasks.writes import TaskWrites, touched

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def change(resource_type, identity, action, **values):
    return PreparedChange("record", ResourceRef(resource_type, identity, BUSINESS, BUSINESS), action, values)


def test_touched_lists_written_tasks_and_new_task_edges_once_in_plan_order():
    plan = SimpleNamespace(changes=[
        change("tasks", "task-a", "insert", title="A"),
        change("relationships", "edge-1", "insert", from_type="task", from_id="task-b", to_type="task", to_id="task-a"),
        change("thread_messages", "message-1", "insert", item_type="task", item_id="task-a"),   # a comment is not a Task write
        change("tasks", "task-a", "update", title="A again"),                                    # a repeat keeps its first place
        change("tasks", "task-c", "policy_effect"),                                              # a policy effect is not a record
        change("relationships", "edge-2", "delete", from_type="task", from_id="task-d"),         # only a new edge announces
        change("relationships", "edge-3", "insert", from_type="topic", from_id="topic-e"),       # only a Task's own edge
        change("relationships", "edge-4", "insert", from_type="task", from_id="task-a"),
        change("tasks", "task-f", "update", status="done")])
    assert touched(plan) == ["task-a", "task-b", "task-f"]
    assert touched(SimpleNamespace(changes=[])) == []


class Recorder:
    """The real announcement, observed: which unit of work, what the Task was before and after, and the comments."""

    def __init__(self, tasks):
        self.tasks, self.calls = tasks, []

    def announce(self, ctx, uow, plan, before, after, comments):
        self.calls.append((uow, before, after, list(comments)))
        self.tasks.announce(ctx, uow, plan, before, after, comments)


def test_a_created_task_is_announced_once_with_its_follows_and_required_delivery(host):
    ctx = host.owner()
    recorder = Recorder(host.tasks)
    with host.database.transaction() as uow:
        plan = host.tasks.prepare("tasks_create", ctx, {"title": "Routed by a domain", "assignee_ids": [AGENT]},
                                  InvocationOptions(idempotency_key=new_id()), uow)
        result = TaskWrites(recorder).apply(ctx, plan, uow)
        [(announced_in, before, after, comments)] = recorder.calls
        assert announced_in is uow and before is None and comments == []
        assert after["row"]["title"] == "Routed by a domain" and after["assignees"] == [AGENT]
    assert result is not None
    [made] = host.query("SELECT id FROM tasks WHERE title='Routed by a domain'")
    events = host.deliveries(AGENT, source_type="tasks")
    assert [(row["source_id"], row["event_type"], row["required"]) for row in events] == [(made["id"], "created", 1)]
    assert host.count("inbox_events", "source_type='tasks' AND source_id=?", made["id"]) == 1
    assert host.count("thread_follows", "item_id=? AND principal_id=?", made["id"], AGENT) == 1


def test_an_existing_task_is_read_before_the_records_and_after_them_with_its_comments(host):
    task = host.create_task("Existing")
    ctx = host.owner()
    recorder = Recorder(host.tasks)
    with host.database.transaction() as uow:
        plan = host.tasks.prepare("tasks_update", ctx, {"task_id": task["id"], "changes": {"title": "Renamed by a domain"}},
                                  InvocationOptions(expected_revision=task["revision"], idempotency_key=new_id()), uow)
        TaskWrites(recorder).apply(ctx, plan, uow)
    [(_, before, after, comments)] = recorder.calls
    assert before["row"]["title"] == "Existing" and after["row"]["title"] == "Renamed by a domain"
    assert after["row"]["revision"] == task["revision"] + 1 and comments == []
    revision = after["row"]["revision"]
    recorder.calls.clear()
    with host.database.transaction() as uow:
        plan = host.tasks.prepare("tasks_comment", ctx, {"task_id": task["id"], "text": "Noted by a domain"},
                                  InvocationOptions(expected_revision=revision, idempotency_key=new_id()), uow)
        TaskWrites(recorder).apply(ctx, plan, uow)
    [(_, before, after, comments)] = recorder.calls
    assert after["row"]["revision"] == before["row"]["revision"] + 1
    [comment] = comments
    assert comment["item_type"] == "task" and comment["item_id"] == task["id"] and comment["body"] == "Noted by a domain"
    assert host.count("thread_messages", "item_id=? AND body=?", task["id"], "Noted by a domain") == 1


def test_a_failed_announcement_leaves_neither_the_task_nor_an_event(host):
    ctx = host.owner()

    class Failing:
        def announce(self, *arguments):
            raise RuntimeError("announcement failed")

    with pytest.raises(RuntimeError):
        with host.database.transaction() as uow:
            plan = host.tasks.prepare("tasks_create", ctx, {"title": "Never lands", "assignee_ids": [AGENT]},
                                      InvocationOptions(idempotency_key=new_id()), uow)
            TaskWrites(Failing()).apply(ctx, plan, uow)
    assert host.count("tasks", "title='Never lands'") == 0 and host.count("inbox_events") == 0


def test_the_owning_module_loads_without_a_host_or_context_and_the_package_initializer_does_not_load_it():
    script = (
        "import sys\n"
        "import qorqut_operations.tasks\n"
        "assert 'qorqut_operations.tasks.writes' not in sys.modules\n"
        "import qorqut_operations.tasks.writes\n"
        "assert not [name for name in sys.modules if name.split('.')[0] in {'bazarlyk', 'qorqut_context'}]\n")
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "PATH": ""})
    assert done.returncode == 0, done.stderr
