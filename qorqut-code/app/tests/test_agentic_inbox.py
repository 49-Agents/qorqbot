"""Real module changes and ordinary host inbox delivery, with no running services.

Only synthetic stores under pytest's explicit scratch root are used. The normal
host assembly, profile registration, emit_event, inbox visibility and nudge hook
run unchanged. No provider, native process or background service is started.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError, Principal, new_id
from qorqut_operations.history_exports import TECHNICAL_EXCLUDED
from qorqut_operations.storage.database import Database

from bazarlyk.agentic import inbox as bridge_module
from bazarlyk.agentic.inbox import AgenticInboxBridge, SOURCE, attach_agentic_inbox
from bazarlyk.agentic.inbox_schema import REQUIRED_TABLES, storage_contribution
from bazarlyk.composition.assembly import require_additive_registrations
from bazarlyk.extensions import Extension, ExtensionSet, SourceHandler
from bazarlyk.instance_paths import InstancePathError, InstancePaths
from bazarlyk.native_chat.nudges import HostVisibility
from bazarlyk.operations_history.catalog import catalog
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources


@pytest.fixture
def host(tmp_path):
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    recipient = create_registered_global(instance, "learning-applier")
    other = create_worker(instance, "other")
    instance.runtime.freeze_registry()
    # Separate real module feed keeps these cursor-unit cases independent of
    # the runtime's installed inventory events; callback integration is tested separately.
    layer = AgenticLayer.initialize(tmp_path / "feed-root" / "authored", tmp_path / "feed-state" / "runtime")
    instance.runtime.inbox_nudges.reconcile = lambda uow: instance.runtime.domains.tasks.reconcile_snoozes(uow)
    instance.runtime.agentic_inbox_bridge = None
    instance.runtime.inbox.host.agentic_inbox_bridge = None
    bridge = attach_agentic_inbox(instance.runtime, layer, instance.runtime.inbox)
    return SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database,
                           layer=layer, bridge=bridge, recipient=recipient, other=other)


def change(host, name="one", *, actor="external/unknown"):
    return host.layer.authoring.apply(
        {"actor_id": actor, "actor_kind": "agent", "reason": "Synthetic private reason must not be copied"},
        [{"op": "create", "path": f"notes/{name}.md", "content": "Synthetic private body"},
         {"op": "create", "path": f"notes/{name}-two.md", "content": "Other private body"}])["change"]


def drain(host, limit=100):
    with host.runtime.application.writer_admission.writer():
        with host.database.transaction() as uow:
            host.runtime.application.ensure_writes_open(uow)
            return host.bridge.reconcile(uow, limit=limit)


def context(host, identity=None):
    return host.runtime.resolver.invocation_context(Principal(identity or host.recipient, "agent"))


def pending(host, identity=None):
    with host.database.read() as uow:
        return host.runtime.inbox.pending(context(host, identity), uow)["items"]


def events(host):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute("SELECT * FROM inbox_events WHERE source_type=? ORDER BY id", (SOURCE,))]


def test_multifile_apply_delivers_once_and_reopen_preserves_host_cursor(host):
    changed = change(host)
    assert changed["sequence"] == 1 and len(changed["files"]) == 2 and changed["commits"] == []
    with host.database.read() as uow:
        assert host.bridge.status(uow)["pending"]
        assert uow.connection.execute("SELECT count(*) FROM agentic_inbox_cursor").fetchone()[0] == 0
    result = drain(host)
    assert result == {"instance_id": host.layer.instance_id, "cursor": 1, "recipient_id": host.recipient,
                      "delivered": 1, "self_skipped": 0, "pending": False, "state": "idle"}
    item, = pending(host)
    assert item["agentic_change"] == {"instance_id": host.layer.instance_id, "change_id": changed["change_id"], "sequence": 1}
    assert item["actor_id"] is None
    assert "2 changed file(s)" in item["summary"]
    assert "private" not in json.dumps(item)
    assert pending(host, host.other) == []
    read = host.instance.invoke(context(host), "inbox_list", {})
    assert read.status == "ok" and read.data["items"][0]["agentic_change"]["change_id"] == changed["change_id"]
    refused = host.instance.invoke(context(host, host.other), "inbox_acknowledge", {"event_ids": [item["id"]]})
    assert refused.status == "error" and refused.error["code"] == "not_found"
    assert drain(host)["delivered"] == 0
    reopened = Database(host.database.path, assembly=host.database.assembly).open_existing()
    bridge = AgenticInboxBridge(AgenticLayer.open(host.layer.root, host.layer.state))
    with reopened.transaction() as uow:
        assert bridge.reconcile(uow)["delivered"] == 0
    reopened.close()
    assert len(events(host)) == 1
    with host.database.read() as uow:
        delivery = uow.connection.execute("SELECT * FROM inbox_deliveries WHERE event_id=?", (item["id"],)).fetchone()
        assert delivery["agent_id"] == host.recipient and delivery["required"] == 1
    assert host.layer.changes.cursor("overseer")["exists"] is False


class Crash(BaseException):
    pass


def test_crash_before_commit_rolls_back_both_and_retries(host):
    change(host)
    with pytest.raises(Crash):
        with host.runtime.application.writer_admission.writer(), host.database.transaction() as uow:
            assert host.bridge.reconcile(uow)["delivered"] == 1
            raise Crash()
    assert events(host) == []
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM agentic_inbox_cursor").fetchone()[0] == 0
    assert drain(host)["delivered"] == 1


@pytest.mark.parametrize("failure", ["none", "raise"])
def test_failed_second_append_rolls_back_first_and_does_not_leapfrog(host, monkeypatch, failure):
    change(host)
    change(host, "two")
    change(host, "three")
    original = bridge_module.emit_event
    calls = []

    def append(uow, **kwargs):
        calls.append(kwargs["source_revision"])
        if len(calls) == 2:
            if failure == "raise":
                raise RuntimeError("append failed")
            return None
        return original(uow, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(bridge_module, "emit_event", append)
        with pytest.raises((DomainError, RuntimeError)):
            drain(host)
    assert calls == [1, 2] and events(host) == []
    assert drain(host)["delivered"] == 3
    assert [row["source_revision"] for row in events(host)] == [1, 2, 3]


def test_concurrent_transactions_reread_the_cursor(host):
    change(host)
    change(host, "two")
    barrier = threading.Barrier(2)

    def worker():
        # Separate bridge objects model two callers without sharing a cached cursor.
        bridge = AgenticInboxBridge(AgenticLayer.open(host.layer.root, host.layer.state))
        barrier.wait(timeout=10)
        with host.runtime.application.writer_admission.writer(), host.database.transaction() as uow:
            return bridge.reconcile(uow)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(worker), pool.submit(worker)
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert sorted(result["delivered"] for result in results) == [0, 2]
    assert len(events(host)) == 2


@pytest.mark.parametrize("missing", ["principal", "profile", "stored_status", "capability", "role"])
def test_unavailable_recipient_keeps_pending_work_until_restored(host, missing):
    changed = change(host)
    with host.database.transaction() as uow:
        profile = uow.repository("agent_profiles", business_id=None).get(host.recipient)
        if missing == "principal":
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.recipient,))
        elif missing == "profile":
            uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (host.recipient,))
        else:
            data = dict(profile["profile_json"])
            if missing == "stored_status":
                data["status"] = "inactive"
            elif missing == "capability":
                data["scope"] = {**data["scope"], "global_capabilities": []}
            else:
                data["role"] = {**data["role"], "id": "retired"}
            uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(data), host.recipient))
    assert drain(host) == {"instance_id": host.layer.instance_id, "cursor": 0, "recipient_id": None,
                           "delivered": 0, "self_skipped": 0, "pending": True, "state": "recipient_unavailable"}
    assert events(host) == []
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1,display_name='Overseer' WHERE id=?", (host.recipient,))
        uow.connection.execute("UPDATE agent_profiles SET status='active',profile_json=? WHERE agent_id=?",
                               (json.dumps(profile["profile_json"]), host.recipient))
    assert drain(host)["cursor"] == changed["sequence"]
    assert pending(host)[0]["agentic_change"]["change_id"] == changed["change_id"]


def test_ambiguous_roster_fails_closed(host):
    change(host)
    with host.database.transaction() as uow:
        profile = uow.repository("agent_profiles", business_id=None).get(host.other)["profile_json"]
        profile["role"]["id"] = "learning-applier"
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(profile), host.other))
    with pytest.raises(DomainError, match="More than one"):
        drain(host)
    assert events(host) == []


def test_self_change_is_retained_without_notice_and_registered_actor_is_kept(host):
    first = change(host, actor=host.recipient)
    second = change(host, "two", actor=host.other)
    result = drain(host)
    assert result["delivered"] == result["self_skipped"] == 1 and result["cursor"] == 2
    assert [event["change_id"] for event in host.layer.changes.list()["changes"]] == [first["change_id"], second["change_id"]]
    assert events(host)[0]["actor_id"] == host.other
    assert events(host)[0]["source_id"] == second["change_id"]


@pytest.mark.parametrize("native_state", [None, "paused", "stopped"])
def test_offline_disabled_native_still_queues_and_existing_nudges_can_see_it(host, native_state):
    change(host)
    if native_state:
        with host.database.transaction() as uow:
            uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) "
                                   "VALUES(?,?,?,1,?)", (host.recipient, new_id(), '{"fixture":true}', native_state))
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM native_inputs").fetchone()[0] == 0
    host.runtime.inbox_nudges._run_hook()
    row, = events(host)
    with host.database.read() as uow:
        assert HostVisibility(host.runtime.inbox).authorized(uow, host.recipient, [row]) == {row["id"]}
    assert len(pending(host)) == 1


def test_visibility_refuses_malformed_foreign_delegated_and_stale_metadata(host):
    changed = change(host)
    drain(host)
    row, = events(host)
    ctx = context(host)
    with host.database.read() as uow:
        assert host.runtime.inbox.event_visible(ctx, row, uow)
        delegated = replace(ctx, delegation_assignment_id=new_id(), delegation_binding_id=new_id(), delegation_generation=1)
        assert not host.runtime.inbox.event_visible(delegated, row, uow)
        assert not host.runtime.inbox.event_visible(context(host, host.other), row, uow)
        base = json.loads(row["visibility_json"])
        values = [None, [], {}, {**base, "extra": "forbidden"}, {**base, "sequence": True},
                  {**base, "sequence": 2}, {**base, "sequence": 0},
                  {**base, "instance_id": "inst_" + "0" * 32}, {**base, "change_id": "bad"},
                  {**base, "recipient_id": host.other}, {**base, "change_id": "chg_" + "0" * 32}]
        for metadata in values:
            assert not host.runtime.inbox.event_visible(ctx, {**row, "visibility_json": json.dumps(metadata)}, uow)
        assert not host.runtime.inbox.event_visible(ctx, {**row, "visibility_json": "{"}, uow)
        assert not host.runtime.inbox.event_visible(ctx, {**row, "event_type": "other"}, uow)
        assert not host.runtime.inbox.event_visible(ctx, {**row, "source_revision": 2}, uow)
    # Visibility uses the durable reference; authored files need not still exist.
    for path in changed["paths"]:
        (host.layer.root / path).unlink()
    assert len(pending(host)) == 1
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.recipient,))
        assert not host.runtime.inbox.event_visible(ctx, row, uow)
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (host.recipient,))
    alternate = AgenticLayer.initialize(host.layer.root.parent / "alternate", host.layer.state.parent / "alternate")
    host.runtime.inbox.host.agentic_inbox_bridge = AgenticInboxBridge(alternate)
    assert pending(host) == []
    with host.database.read() as uow, pytest.raises(DomainError) as error:
        AgenticInboxBridge(alternate).status(uow)
    assert error.value.code == "agentic_instance_mismatch"
    with host.database.transaction() as uow, pytest.raises(DomainError) as error:
        AgenticInboxBridge(alternate).reconcile(uow)
    assert error.value.code == "agentic_instance_mismatch"


def test_bounds_read_only_and_bounded_drain(host):
    change(host)
    change(host, "two")
    assert drain(host, 1)["pending"]
    assert drain(host, 1000)["delivered"] == 1
    for limit in (0, 1001, True, 1.5, "1"):
        with pytest.raises(DomainError):
            drain(host, limit)
    with host.database.read() as uow, pytest.raises(DomainError) as error:
        host.bridge.reconcile(uow)
    assert error.value.code == "read_only"


def test_attachment_keeps_original_task_hook_once_and_refuses_conflicts(host):
    backend = host.runtime.inbox_nudges
    assert attach_agentic_inbox(host.runtime, host.layer, host.runtime.inbox) is host.bridge
    hook = backend.reconcile
    assert attach_agentic_inbox(host.runtime, host.layer, host.runtime.inbox.host) is host.bridge
    assert backend.reconcile is hook
    with pytest.raises(DomainError):
        attach_agentic_inbox(host.runtime, AgenticLayer.open(host.layer.root, host.layer.state), host.runtime.inbox)
    # Fresh synthetic composition with a recording Task callback in its existing slot.
    calls = []
    runtime = SimpleNamespace(inbox_nudges=SimpleNamespace(reconcile=lambda uow: calls.append(uow)))
    inbox = SimpleNamespace()
    bridge = attach_agentic_inbox(runtime, host.layer, inbox)
    assert attach_agentic_inbox(runtime, host.layer, inbox) is bridge
    change(host)
    with host.database.transaction() as uow:
        runtime.inbox_nudges.reconcile(uow)
        assert calls == [uow]
    assert len(events(host)) == 1


def test_busy_authoring_does_not_hold_operations_writer(host):
    import threading
    import time
    locked, release = threading.Event(), threading.Event()
    task_id = due_task(host)
    change(host)

    def authoring():
        with host.layer._ctx.store.lock(exclusive=True):
            locked.set()
            assert release.wait(5)

    worker = threading.Thread(target=authoring)
    worker.start()
    try:
        assert locked.wait(2)
        started = time.monotonic()
        host.runtime.inbox_nudges._run_hook()
        assert time.monotonic() - started < 1
        assert host.bridge.last_callback_failure['code'] == 'lock_timeout'
        # Unrelated Task reconciliation still commits; agentic delivery retries.
        with host.database.read() as uow:
            assert uow.connection.execute('SELECT event_id FROM task_snooze_wakes WHERE task_id=?', (task_id,)).fetchone()[0]
        assert events(host) == []
    finally:
        release.set()
        worker.join(2)
    host.runtime.inbox_nudges._run_hook()
    assert host.bridge.last_callback_failure is None
    assert len(events(host)) == 1


def due_task(host):
    created = host.instance.invoke(host.instance.human(), "tasks_create",
                                   {"title": "Due synthetic task", "assignee_ids": [host.other]})
    assert created.status == "ok", created.error
    task_id = created.data["task"]["id"]
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET snoozed_until='2000-01-01T00:00:00Z',snooze_generation=1 WHERE id=?", (task_id,))
    return task_id


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_existing_task_snooze_commits_while_failed_agentic_batch_rolls_back(host, monkeypatch, failure, caplog):
    task_id = due_task(host)
    change(host)
    change(host, "two")
    original = host.runtime.domains.tasks.reconcile_snoozes
    calls = []

    def reconcile(uow):
        calls.append(uow)
        return original(uow)

    monkeypatch.setattr(host.runtime.domains.tasks, "reconcile_snoozes", reconcile)
    append = bridge_module.emit_event
    appends = []

    def fail_second(uow, **kwargs):
        appends.append(kwargs["source_revision"])
        if len(appends) == 2:
            if failure == "exception":
                raise RuntimeError("private-content-do-not-log " * 1000)
            return None
        return append(uow, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(bridge_module, "emit_event", fail_second)
        host.runtime.inbox_nudges._run_hook()
    assert len(calls) == 1 and appends == [1, 2]
    with host.database.read() as uow:
        wake, = uow.connection.execute("SELECT * FROM task_snooze_wakes").fetchall()
        assert wake["task_id"] == task_id and wake["event_id"] is not None
        assert uow.connection.execute("SELECT count(*) FROM agentic_inbox_cursor").fetchone()[0] == 0
    assert events(host) == []
    diagnostic = host.bridge.last_callback_failure
    assert diagnostic["code"] == ("agentic_delivery_failed" if failure == "none" else "agentic_callback_failed")
    assert diagnostic["error_type"] == ("DomainError" if failure == "none" else "RuntimeError")
    assert len(json.dumps(diagnostic)) < 250 and "private-content" not in caplog.text
    diagnostic["code"] = "caller mutation"
    assert host.bridge.last_callback_failure["code"] != diagnostic["code"]
    # Read-only status is not a callback success and does not clear its observation.
    with host.database.read() as uow:
        assert host.bridge.status(uow)["pending"]
    assert host.bridge.last_callback_failure is not None
    host.runtime.inbox_nudges._run_hook()
    assert len(calls) == 2 and host.bridge.last_callback_failure is None
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM task_snooze_wakes").fetchone()[0] == 1
    assert [row["source_revision"] for row in events(host)] == [1, 2]
    host.runtime.inbox_nudges._run_hook()
    assert len(calls) == 3 and len(events(host)) == 2


@pytest.mark.parametrize("failure_at", ["prior", "bridge"])
def test_prior_callback_exception_and_process_crash_escape_the_failure_handler(host, monkeypatch, failure_at):
    due_task(host)
    change(host)

    def fail(*args, **kwargs):
        if failure_at == "prior":
            raise RuntimeError("prior callback failed")
        raise Crash()

    if failure_at == "prior":
        monkeypatch.setattr(host.runtime.domains.tasks, "reconcile_snoozes", fail)
    else:
        monkeypatch.setattr(bridge_module, "emit_event", fail)
    with pytest.raises(RuntimeError if failure_at == "prior" else Crash):
        with host.runtime.application.writer_admission.writer(), host.database.transaction() as uow:
            host.runtime.inbox_nudges.reconcile(uow)
    assert host.bridge.last_callback_failure is None and events(host) == []
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM task_snooze_wakes").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM agentic_inbox_cursor").fetchone()[0] == 0


def test_writer_admission_failure_is_not_hidden_by_the_callback(host):
    change(host)
    host.runtime.application.writer_admission.refuse_new()
    with pytest.raises(DomainError) as error:
        host.runtime.inbox_nudges._run_hook()
    assert error.value.code == "maintenance"
    assert host.bridge.last_callback_failure is None and events(host) == []


@pytest.mark.parametrize("recipient_missing", [False, True])
def test_older_same_identity_store_refuses_cursor_beyond_head_without_writes(host, recipient_missing, tmp_path):
    change(host)
    snapshot = tmp_path / "older-module.sqlite3"
    with sqlite3.connect(host.layer.state / "state.sqlite3") as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
    change(host, "two")
    assert drain(host)["cursor"] == 2
    identity = host.layer.instance_id
    with sqlite3.connect(snapshot) as source, sqlite3.connect(host.layer.state / "state.sqlite3") as target:
        source.backup(target)
    assert host.layer.instance_id == identity and host.layer.changes.list()["head"] == 1
    task_id = due_task(host)
    if recipient_missing:
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.recipient,))

    with host.database.read() as uow:
        before = dict(uow.connection.execute("SELECT * FROM agentic_inbox_cursor").fetchone())
        with pytest.raises(DomainError) as error:
            host.bridge.status(uow)
        assert error.value.code == "agentic_feed_rewound"
    with host.database.transaction() as uow:
        writes_before = uow.connection.total_changes
        with pytest.raises(DomainError) as error:
            host.bridge.reconcile(uow)
        assert error.value.code == "agentic_feed_rewound"
        assert uow.connection.total_changes == writes_before
        assert dict(uow.connection.execute("SELECT * FROM agentic_inbox_cursor").fetchone()) == before
    assert len(events(host)) == 2
    if not recipient_missing:
        # Existing references are still visible, including the later stored event.
        assert len(pending(host)) == 2

    host.runtime.inbox_nudges._run_hook()
    assert host.bridge.last_callback_failure["code"] == "agentic_feed_rewound"
    assert host.layer.changes.list()["head"] == 1
    with host.database.read() as uow:
        wake, = uow.connection.execute("SELECT * FROM task_snooze_wakes").fetchall()
        assert wake["task_id"] == task_id and wake["event_id"] is not None
        assert dict(uow.connection.execute("SELECT * FROM agentic_inbox_cursor").fetchone()) == before
    assert len(events(host)) == 2


def test_construction_is_inert_and_conflicting_binding_is_an_error():
    class Unopened:
        @property
        def instance_id(self):
            raise AssertionError("Construction must not open a store")

    layer = Unopened()
    bridge = AgenticInboxBridge(layer)
    runtime = SimpleNamespace(inbox_nudges=SimpleNamespace(reconcile=None), agentic_inbox_bridge=bridge)
    inbox = SimpleNamespace(agentic_inbox_bridge=bridge)
    with pytest.raises(DomainError, match="Conflicting"):
        attach_agentic_inbox(runtime, layer, inbox)
    runtime.agentic_inbox_bridge = None
    inbox.agentic_inbox_bridge = None
    assert attach_agentic_inbox(runtime, layer, inbox).layer is layer


def test_binding_is_immutable_and_explicitly_excluded_from_history(host):
    drain(host)
    contribution = storage_contribution()
    assert contribution.required_tables == REQUIRED_TABLES
    assert host.database.assembly.tables["agentic_inbox_cursor"].revision_column is None
    assert catalog().classifications["agentic_inbox_cursor"][1].kind == TECHNICAL_EXCLUDED
    for statement in ("DELETE FROM agentic_inbox_cursor", "UPDATE agentic_inbox_cursor SET instance_id='inst_" + "0" * 32 + "'"):
        with pytest.raises(DomainError):
            with host.database.transaction() as uow:
                uow.connection.execute(statement)
    class Shadow(Extension):
        id = "shadow"

        def sources(self):
            return (SourceHandler(SOURCE, event_visible=lambda *args: True),)

    extensions = ExtensionSet([Shadow()])
    with pytest.raises(DomainError, match="cannot replace a core source"):
        require_additive_registrations(host.database.assembly, extensions, {})


def test_explicit_paths_are_separate_inert_and_reject_symlinks(tmp_path):
    root = tmp_path / "fresh"
    paths = InstancePaths(root, {})
    assert paths.agentic_root == root / "operations/agentic"
    assert paths.agentic_runtime == root / "operations/.runtime/agentic"
    assert not root.exists()
    assert len({paths.agentic_root, paths.agentic_runtime, paths.context, paths.context_runtime}) == 4
    paths.operations.mkdir(parents=True)
    (tmp_path / "foreign").mkdir()
    paths.agentic_root.symlink_to(tmp_path / "foreign", target_is_directory=True)
    with pytest.raises(InstancePathError):
        _ = paths.agentic_root
    paths.runtime.mkdir()
    (paths.runtime / "agentic").symlink_to(tmp_path / "foreign", target_is_directory=True)
    with pytest.raises(InstancePathError):
        _ = paths.agentic_runtime
