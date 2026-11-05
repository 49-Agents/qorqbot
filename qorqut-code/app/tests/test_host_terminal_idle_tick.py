"""The terminal controller reads first: an empty queue takes no write unit, a queued action takes the original path.

Over a synthetic host instance, a registered Ordubashy holding the terminal-control plugin and a registered global worker as its
target: businesses have no resident agents, and the singleton globals (Founder, Learning Applier, Global Marketing) are never its
terminal target. The terminal backend is a recording stand-in; no tmux, native session, provider or network is used."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import shutil
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id

from host_foundation_support import (APP, build_instance, create_worker, register_sources, select_coordinator,
                                     set_agent_overrides)

PLUGIN = "agent-terminal-control"


class Backend:
    """Records each effect with the durable state it found and whether the controller held a write unit at that moment."""

    def __init__(self, database):
        self.database, self.current, self.calls, self.seen, self.fail = database, {"state": "present", "command": "claude", "session_id": "$1"}, [], [], None

    def snapshot(self, row):
        return dict(self.current)

    def validate(self, action, snapshot):
        pass

    def execute(self, row, action, message, expected, *, input_key=None):
        identity = input_key.removeprefix("terminal-action:")
        self.calls.append((row["agent_id"], action, identity))
        with self.database.read() as uow:
            state = uow.connection.execute("SELECT state FROM terminal_actions WHERE id=?", (identity,)).fetchone()[0]
        with type(self.database).transaction(self.database):    # would block, and fail, if the controller still held its write unit
            pass
        self.seen.append(state)
        if self.fail:
            raise self.fail
        return "confirmed"


def publish(instance, owner, source, kind):
    from bazarlyk.bootstrap.seed import apply_initial_plan

    runtime = instance.runtime
    with instance.database.read() as uow:
        plan = runtime.publisher.prepare(owner, {"source_id": source}, InvocationOptions(idempotency_key="seed-" + source), uow, kind)
    return apply_initial_plan(instance.database, owner, plan, runtime.publisher, runtime.publisher.resolve_file_target, runtime.locks)["artifact"]


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    from bazarlyk.agents.models import GLOBAL_CAPABILITIES

    instance, ids = build_instance(tmp_path_factory.mktemp("terminal-idle"))
    first = ids["first"]
    register_sources(instance, first, "first")
    target = create_worker(instance, "worker-one")
    shutil.copytree(APP / "plugins" / PLUGIN, instance.workspace / "qorqut-code" / "app" / "plugins" / PLUGIN)
    owner = InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset({first}),
                              global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    role, plugin = publish(instance, owner, "ordubashy", "role"), publish(instance, owner, PLUGIN, "plugin")
    created = instance.invoke(owner, "agents_create", {
        "role": role, "plugins": [{**plugin, "config": {}}], "name": "ordubashy", "display_name": "Ordubashy",
        "assignment_brief": "Synthetic coordinator.",
        "scope": {"kind": "ecosystem", "allowed_business_ids": [first], "global_capabilities": ["read_global_context"]}})
    assert created.status == "ok", created.error
    select_coordinator(instance, created.data["agent_id"])           # the configured Ordubashy is the one that manages workers
    instance.runtime.freeze_registry()
    control = instance.runtime.terminal_control
    control.backend = Backend(instance.database)
    yield SimpleNamespace(instance=instance, db=instance.database, control=control, backend=control.backend, first=first,
                          target=target, coordinator=created.data["agent_id"], owner=instance.owner)
    instance.runtime.stop()


@pytest.fixture(autouse=True)
def clean(host):
    host.backend.calls.clear(), host.backend.seen.clear()
    host.backend.current, host.backend.fail = {"state": "present", "command": "claude", "session_id": "$1"}, None
    set_overrides(host, {})
    yield
    while host.control.tick():                  # leave no queued action behind for the next case
        pass


def set_overrides(host, overrides):
    return set_agent_overrides(host.instance, host.coordinator, overrides, (host.first,))


def queue(host, action="restart", *, batch=None, target=None):
    identity, target = new_id(), target or host.target
    with host.db.transaction() as uow:
        row = dict(uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (target,)).fetchone()) \
            if target == host.target else None
        binding = host.control.binding(row) if row else {"profile_revision": 1, "terminal": host.backend.snapshot(None)}
        uow.repository("terminal_actions").insert({"id": identity, "batch_id": batch or new_id(), "proposer_id": host.coordinator,
            "target_id": target, "action": action, "message": None, "binding_json": binding, "state": "queued", "detail": None})
    return identity


def action(host, identity):
    with host.db.read() as uow:
        return dict(uow.connection.execute("SELECT state, detail, revision FROM terminal_actions WHERE id=?", (identity,)).fetchone())


@pytest.fixture
def units(host, monkeypatch):
    """Every write unit the controller opens from now on."""
    calls, original = [], host.db.transaction

    def counted(*args, **kwargs):
        calls.append(kwargs.get("operation"))
        return original(*args, **kwargs)

    monkeypatch.setattr(host.db, "transaction", counted)
    return calls


def test_an_empty_queue_takes_no_write_unit_and_reports_no_work(host, units):
    for _ in range(25):
        assert host.control.tick() is False
    assert units == [] and host.backend.calls == []


def test_a_queued_action_is_processed_on_the_next_tick_after_an_idle_one(host, units):
    assert host.control.tick() is False and units == []
    identity = queue(host)
    del units[:]
    assert host.control.tick() is True
    assert len(units) == 2                       # the original claim unit and the final receipt unit
    assert host.backend.calls == [(host.target, "restart", identity)]
    assert host.backend.seen == ["uncertain"]    # durably uncertain before the effect, and no unit held while it ran
    assert action(host, identity)["state"] == "completed" and action(host, identity)["detail"] == "confirmed"
    del units[:]
    assert host.control.tick() is False and units == [] and len(host.backend.calls) == 1


def test_a_revoked_permission_blocks_the_effect(host):
    identity = queue(host)
    set_overrides(host, {"agent_terminal_restart": "deny"})
    assert host.control.tick() is True
    stored = action(host, identity)
    assert stored["state"] == "failed" and "denied" in stored["detail"] and host.backend.calls == []
    assert host.control.tick() is False


def test_a_changed_terminal_binding_blocks_the_effect(host):
    identity = queue(host)
    host.backend.current = {"state": "present", "command": "claude", "session_id": "$2"}
    assert host.control.tick() is True
    assert action(host, identity) == {"state": "failed", "detail": "Target session changed before execution", "revision": 2}
    assert host.backend.calls == [] and host.control.tick() is False


def test_one_unauthorized_target_fails_the_whole_batch_before_any_effect(host):
    batch = new_id()
    valid, invalid = queue(host, batch=batch), queue(host, batch=batch, target=host.owner)    # the owner is no registered agent
    assert host.control.tick() is True
    assert action(host, invalid)["state"] == "failed" and action(host, valid)["state"] == "failed"
    assert "Another target" in action(host, valid)["detail"] and host.backend.calls == []
    assert host.control.tick() is False


def test_an_unconfirmed_execution_stays_uncertain_and_is_never_replayed(host):
    host.backend.fail = RuntimeError("terminal vanished")
    identity = queue(host)
    assert host.control.tick() is True
    assert action(host, identity)["state"] == "uncertain" and len(host.backend.calls) == 1
    host.backend.fail = None
    assert host.control.tick() is False and len(host.backend.calls) == 1


def test_a_definitely_undelivered_execution_is_recorded_as_failed(host):
    host.backend.fail = DomainError("not_delivered", "The composer refused the input")
    identity = queue(host)
    assert host.control.tick() is True
    assert action(host, identity)["state"] == "failed" and action(host, identity)["detail"] == "The composer refused the input"


def test_concurrent_ticks_run_a_queued_action_at_most_once(host):
    identity = queue(host)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: host.control.tick(), range(4)))
    assert results.count(True) == 1 and [call[2] for call in host.backend.calls] == [identity]
    assert action(host, identity)["state"] == "completed"
