"""The stock history catalog and real host commit notifications, on empty synthetic instances."""
import subprocess
import time

from qorqut_operations.contracts import Principal, InvocationContext, InvocationOptions, utc_now
from qorqut_operations.history_exports import HistoryObserver
from bazarlyk.operations_history import composition
from bazarlyk.operations_history.catalog import catalog
from bazarlyk.operations_history.composition import start
from host_foundation_support import build_instance, create_registered_global, register_sources


def test_stock_exports_current_items_and_filters_private_notices(tmp_path):
    host, _ = build_instance(tmp_path)
    try:
        for ctx, scope in ((host.human(), "business"), (InvocationContext(Principal(host.owner, "human"), source="dashboard"), "global")):
            result = host.invoke(ctx, "tasks_create", {"id": "same-id", "title": "Plan", "scope": scope})
            assert result.status == "ok", result.error
        action = host.invoke(host.human(), "actions_record", {"text": "Confirmed the plan", "tags": ["Launch"]})
        assert action.status == "ok", action.error
        register_sources(host, host.business, "first")
        agent = create_registered_global(host, "ordubashy")
        ctx = host.runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=host.business)
        comment = host.runtime.application.invoke(ctx, "tasks_comment", {"task_id": "same-id", "text": "The work is recorded"}, InvocationOptions(idempotency_key="comment", expected_revision=1))
        assert comment.status == "ok", comment.error
        with host.database.transaction() as uow:
            uow.connection.execute("INSERT INTO inbox_events(source_type,source_id,event_type,summary,created_at) VALUES(?,?,?,?,?)",
                                   ("private_dm", "private", "message", "PRIVATE-DM-MARKER", utc_now()))
        with host.database.read() as uow:
            exported = catalog().export(uow)
        assert exported.coverage_complete, exported.missing
        rows = exported.tables["tasks"].rows
        assert [row["business_id"] for row in rows] == [None, host.business]
        assert len(exported.tables["action_revisions"].rows) == 1
        assert exported.tables["thread_messages"].rows
        assert all(row["summary"] != "PRIVATE-DM-MARKER" for row in exported.tables["inbox_events"].rows)
        summary = host.runtime.history.snapshot()
        assert summary["complete"], summary
        assert not host.runtime.history.snapshot()["changed"]
        tree = subprocess.check_output(["git", "--git-dir", str(host.paths.history), "ls-tree", "-r", "--name-only", summary["commit"]], text=True)
        assert "database/tasks.jsonl" in tree
        assert "private_dm" not in tree and "native_transcript" not in tree
        with host.database.transaction() as uow:
            uow.connection.execute("CREATE TABLE unclassified_private (secret TEXT)")
            uow.connection.execute("INSERT INTO unclassified_private VALUES ('DO-NOT-EXPORT')")
        with host.database.read() as uow:
            incomplete = catalog().export(uow)
        assert not incomplete.coverage_complete and incomplete.missing == ("unclassified_private",)
        assert "unclassified_private" not in incomplete.tables
    finally:
        host.runtime.release_instance()
        host.database.close()


def test_post_commit_history_runs_and_restarts_on_the_same_host(tmp_path, monkeypatch):
    # The composed observer keeps a minimum gap of one interval (60 s) after each pass; this check needs the second, hinted
    # pass within seconds, so only the gap is shortened. The binding, commit listener and lifecycle are the real ones.
    monkeypatch.setattr(composition, "HistoryObserver", lambda snapshot: HistoryObserver(snapshot, min_gap=0.2))
    host, _ = build_instance(tmp_path)
    observer = host.runtime.history_observer
    try:
        start(host.runtime)
        result = host.invoke(host.human(), "tasks_create", {"title": "Recorded after commit"})
        assert result.status == "ok", result.error
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = host.runtime.history.status()
            if status.get("state") == "ok":
                commit = status["commit"]
                read = subprocess.run(["git", "--git-dir", str(host.paths.history), "show", f"{commit}:database/tasks.jsonl"], capture_output=True)
                if read.returncode == 0 and b"Recorded after commit" in read.stdout:
                    break
            time.sleep(.05)
        else:
            raise AssertionError(host.runtime.history.status())
        assert observer.stop(timeout=5).stopped
        start(host.runtime)
        assert host.runtime.history_observer is observer
        assert observer.stop(timeout=5).stopped
    finally:
        observer.stop(timeout=5)
        host.runtime.release_instance()
        host.database.close()


def test_routine_history_exports_only_failed_runs_without_deleting_bookkeeping(tmp_path):
    host, _ = build_instance(tmp_path)
    try:
        with host.database.transaction() as uow:
            uow.connection.execute("INSERT INTO routines(namespace,id,name,scope,target,schedule,timezone,status,job_kind,arguments_json,timeout_seconds,created_at,updated_at) VALUES('global','sync','Sync','global','root','* * * * *','UTC','active','email.sync','{}',60,'2026-10-16','2026-10-16')")
            for state in ('succeeded','failed','queued','running','cancelled','waiting_signoff'):
                uow.connection.execute("INSERT INTO routine_runs(id,namespace,routine_id,trigger,state,started_at,created_at,updated_at) VALUES(?,'global','sync','schedule',?,'2026-10-16','2026-10-16','2026-10-16')",(state,state))
        with host.database.read() as uow:
            export = catalog().export(uow)
            assert [row['id'] for row in export.tables['routine_runs'].rows] == ['failed']
            assert len(export.tables['routines'].rows) == 1
            assert uow.connection.execute('SELECT count(*) FROM routine_runs').fetchone()[0] == 6
        before = host.runtime.history.snapshot()
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE routine_runs SET revision=revision+1 WHERE id='succeeded'")
        assert host.runtime.history.snapshot()['changed'] is False
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE routine_runs SET state='failed',revision=revision+1 WHERE id='running'")
        assert host.runtime.history.snapshot()['changed'] is True
    finally:
        host.runtime.release_instance()
        host.database.close()
