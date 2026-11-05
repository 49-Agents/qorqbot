"""Persisted browser-turn acceptance; no model or Chrome process is launched."""

from datetime import timedelta

import pytest

from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import DomainError
from browser_turn_support import setup_store


def _assignment(database, store, parent_id, business_id, title):
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
            objective=title, plan={"steps": ["open", "verify"]},
            allowed_operations=["browser_use"], resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
        return store.claim(uow, saved["id"], actor_principal_id=parent_id,
                           dispatcher_ready=True)


def test_one_browser_turn_promotes_next_worker_without_model_polling(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    first = _assignment(database, store, parent_id, business_id, "First browser task")
    second = _assignment(database, store, parent_id, business_id, "Second browser task")
    queue = BrowserTurnQueue(clock=store.clock)
    with database.transaction() as uow:
        owned = queue.acquire(uow, browser_key=business_id, business_id=business_id,
            parent_principal_id=parent_id, assignment_id=first["id"],
            generation=first["execution_generation"])
        waiting = queue.acquire(uow, browser_key=business_id, business_id=business_id,
            parent_principal_id=parent_id, assignment_id=second["id"],
            generation=second["execution_generation"])
        assert owned["state"] == "owning"
        assert waiting["state"] == "queued" and waiting["position"] == 1
        released = queue.release(uow, owned["id"], parent_principal_id=parent_id,
            requester_id=first["id"], generation=first["execution_generation"])
        promoted = queue.status(uow, waiting["id"], parent_principal_id=parent_id,
            requester_id=second["id"])
    assert released["state"] == "released"
    assert promoted["state"] == "owning"


def test_waiting_assignment_releases_browser_and_stale_generation_is_fenced(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    running = _assignment(database, store, parent_id, business_id, "Wait for a browser reply")
    queue = BrowserTurnQueue(clock=store.clock)
    with database.transaction() as uow:
        turn = queue.acquire(uow, browser_key=business_id, business_id=business_id,
            parent_principal_id=parent_id, assignment_id=running["id"],
            generation=running["execution_generation"])
        checkpoint = queue.checkpoint(uow, turn["id"], parent_principal_id=parent_id,
            requester_id=running["id"], lease_id=turn["lease_id"], generation=running["execution_generation"],
            checkpoint={"tab": "settings", "completed": ["open"]},
            pending_outcome={"kind": "uncertain_submission", "form": "profile"},
            next_action="Read the saved profile before retrying")
        current_assignment = store.get(uow, running["id"])
        waiting_assignment = store.checkpoint(uow, running["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=current_assignment["revision"], checkpoint={"step": "open"},
            next_step="Wait for the page to settle", state="waiting",
            wake_condition={"kind": "external_page"})
    assert checkpoint["pending_outcome"]["kind"] == "uncertain_submission"
    with database.read() as uow:
        released = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()[0]
        saved = uow.repository("operational_assignments").get(running["id"])
    assert released == "released"
    assert saved["checkpoint_json"]["browser"]["next_action"] == "Read the saved profile before retrying"

    with database.transaction() as uow:
        resumed = store.control(uow, running["id"], parent_principal_id=parent_id,
            expected_revision=waiting_assignment["revision"], action="resume")
        next_run = store.claim(uow, running["id"], actor_principal_id=parent_id,
            dispatcher_ready=True)
        with pytest.raises(DomainError, match="replaced assignment generation"):
            queue.acquire(uow, browser_key=business_id, business_id=business_id,
                parent_principal_id=parent_id, assignment_id=running["id"],
                generation=running["execution_generation"])
    assert next_run["execution_generation"] > running["execution_generation"]


def test_expired_browser_turn_is_recovered_and_old_lease_cannot_command(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    running = _assignment(database, store, parent_id, business_id, "Recover a crashed browser worker")
    queue = BrowserTurnQueue(clock=store.clock)
    with database.transaction() as uow:
        turn = queue.acquire(uow, browser_key=business_id, business_id=business_id,
            parent_principal_id=parent_id, assignment_id=running["id"],
            generation=running["execution_generation"])
        recovered = queue.recover(uow, now=(now + timedelta(minutes=6)).isoformat().replace("+00:00", "Z"))
        assert recovered == 1
        with pytest.raises(DomainError, match="not owned by this requester|waiting for its exclusive turn"):
            queue._owned(uow, turn["id"], parent_principal_id=parent_id,
                requester_id=running["id"], generation=running["execution_generation"],
                lease_id=turn["lease_id"], now=(now + timedelta(minutes=6)).isoformat().replace("+00:00", "Z"))
