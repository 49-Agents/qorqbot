"""The native session owner storage and the continuity facts that depend on it, over disposable synthetic state.

``native_session_owners`` keeps only the exact registration/session/generation ownership of an agent's native session: the
registered session, the verified native binding, the generation and the desired state. It has no poll, check or lease column,
no generation default and refuses an empty binding or a zero generation, so no row can be a fabricated or generation-zero
owner; the retired notice-receipt table and the delivery ``notified_*`` columns do not exist in the composed schema. The
continuity snapshot reports the owner and its generation and never waits for a poll. Nothing starts a native session, a terminal, a
provider or the release worker; the process and helper probes are replaced where a case needs a live-looking owner."""

import json
from dataclasses import replace

import pytest
from qorqut_operations.contracts import DomainError, Principal, new_id

from bazarlyk.release_worker import continuity
from host_foundation_support import build_instance, create_registered_global, register_sources

OWNER_COLUMNS = ["agent_id", "consumer_id", "binding_json", "generation", "desired_state"]


class Owned:
    """One registered global agent (Ordubashy) with a machine registration, and the calls a native startup makes."""

    def __init__(self, tmp_path):
        self.instance, ids = build_instance(tmp_path)
        self.runtime, self.database = self.instance.runtime, self.instance.database
        register_sources(self.instance, self.instance.business, "first")
        self.agent = create_registered_global(self.instance, "ordubashy")
        self.workspace = str(tmp_path / "agent-workspace")
        self.registration = new_id()
        with self.database.transaction() as uow:
            uow.repository("machine_registrations").insert({
                "id": self.registration, "principal_id": self.agent, "workspace": self.workspace,
                "service_url": "http://127.0.0.1:9", "profile_revision": 1})
        self.context = self.runtime.resolver.invocation_context(Principal(self.agent, "agent"))

    def binding(self, session, *, owner="123:456", event="startup"):
        return {"agent_id": self.agent, "registration_id": self.registration, "workspace": self.workspace, "session_id": session,
                "runtime": "codex", "pid": 123, "started": "456", "owner": owner, "execution_target": "local", "event": event}

    def start(self, session, *, binding=True, event="startup", old_state="unknown", expected_generation=None, owner="123:456"):
        return self.runtime.inbox.start_session(
            self.context, self.registration, session, event,
            binding=self.binding(session, owner=owner, event=event) if binding else None, old_state=old_state,
            expected_generation=expected_generation)

    def row(self):
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM native_session_owners WHERE agent_id=?", (self.agent,)).fetchone()
        return dict(row) if row else None

    def owner_check(self, session, generation):
        bound = replace(self.context, inbox_session_id=session, inbox_generation=generation)
        with self.database.read() as uow:
            return self.runtime.inbox.session_owner(self.runtime.inbox.context(bound, uow), session, uow)


@pytest.fixture
def owned(tmp_path):
    return Owned(tmp_path)


def columns(database, table):
    with database.read() as uow:
        return [row["name"] for row in uow.connection.execute(f"PRAGMA table_info({table})")]


def tables(database):
    with database.read() as uow:
        return {row["name"] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}


def test_the_composed_schema_has_only_the_ownership_and_none_of_the_retired_storage(owned):
    assert columns(owned.database, "native_session_owners") == OWNER_COLUMNS
    present = tables(owned.database)
    assert not present & {"inbox_watchers", "inbox_notification_receipts", "inbox_notice_retention"}
    assert not {"notified_consumer_id", "notified_at"} & set(columns(owned.database, "inbox_deliveries"))
    with owned.database.read() as uow:
        sql = uow.connection.execute("SELECT sql FROM sqlite_master WHERE name='native_session_owners'").fetchone()["sql"]
    assert "DEFAULT" not in sql.split("generation")[1].split(",")[0]            # no generation default
    assert "last_check_at" not in sql and "lease_until" not in sql


def test_the_table_refuses_a_fabricated_owner(owned):
    insert = "INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation) VALUES(?,?,?,?)"
    for binding, generation in (("{}", 1), ("not json", 1), ("[]", 1), (json.dumps({"owner": "1:1"}), 0)):
        with pytest.raises(DomainError) as refused:
            with owned.database.transaction() as uow:
                uow.connection.execute(insert, (owned.agent, new_id(), binding, generation))
        assert "constraint" in refused.value.message
    with pytest.raises(DomainError):                                               # the generation has no default
        with owned.database.transaction() as uow:
            uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json) VALUES(?,?,?)",
                                   (owned.agent, new_id(), json.dumps({"owner": "1:1"})))
    assert owned.row() is None


def test_the_first_verified_binding_is_generation_one_and_repeating_it_keeps_the_owner(owned):
    session = new_id()
    with pytest.raises(DomainError) as unbound:                                     # no binding, no owner: nothing is fabricated
        owned.start(session, binding=False)
    assert unbound.value.code == "inbox_binding_required" and owned.row() is None

    first = owned.start(session)
    assert first["inbox_generation"] == 1 and first["inbox_consumer_id"] == session
    row = owned.row()
    assert (row["consumer_id"], row["generation"], row["desired_state"]) == (session, 1, "running")
    assert json.loads(row["binding_json"])["owner"] == "123:456"
    assert owned.owner_check(session, 1)["generation"] == 1

    # The same session asking again, even without a verifiable binding, keeps the owner and its binding unchanged.
    assert owned.start(session, expected_generation=1)["inbox_generation"] == 1
    assert owned.start(session, binding=False, expected_generation=1)["inbox_generation"] == 1
    assert owned.row() == row


def test_a_stale_missing_or_foreign_generation_never_passes_the_ownership_check(owned):
    session = new_id()
    with pytest.raises(DomainError) as none_yet:
        owned.owner_check(session, 1)
    assert none_yet.value.code == "inbox_generation_stale"                           # a caller claiming a binding that does not exist
    owned.start(session)
    for generation in (None, 0, 2, True):
        with pytest.raises(DomainError) as stale:
            owned.owner_check(session, generation)
        assert stale.value.code == "inbox_generation_stale"
    with pytest.raises(DomainError) as other:
        owned.owner_check(new_id(), 1)
    assert other.value.code == "inbox_session_superseded"
    with pytest.raises(DomainError) as moved:                                        # a delayed start that saw an older generation
        owned.start(new_id(), expected_generation=0)
    assert moved.value.code == "inbox_generation_stale"


def test_only_a_confirmed_dead_owner_is_replaced_and_the_generation_fences_the_old_one(owned):
    old, candidate = new_id(), new_id()
    owned.start(old)
    with pytest.raises(DomainError) as unknown:                                      # an unproven replacement displaces nobody
        owned.start(candidate, event="prompt", old_state="unknown")       # no native selection and no proof the owner is gone
    assert unknown.value.code == "inbox_binding_ambiguous"
    with pytest.raises(DomainError) as unbound:
        owned.start(candidate, binding=False)
    assert unbound.value.code == "inbox_binding_required"
    with pytest.raises(DomainError) as reused:                                       # the same session with another process identity
        owned.start(old, owner="123:999", old_state="unknown")
    assert reused.value.code == "inbox_binding_ambiguous"
    assert owned.row()["consumer_id"] == old and owned.row()["generation"] == 1

    replaced = owned.start(candidate, event="resume", old_state="dead", expected_generation=1)
    assert replaced["inbox_generation"] == 2 and owned.row()["consumer_id"] == candidate
    with pytest.raises(DomainError) as superseded:
        owned.start(old)
    assert superseded.value.code == "inbox_session_superseded"
    with pytest.raises(DomainError) as fenced:
        owned.owner_check(old, 1)
    assert fenced.value.code == "inbox_session_superseded"

    renewed = owned.start(candidate, event="resume", owner="123:777", old_state="dead", expected_generation=2)
    assert renewed["inbox_generation"] == 3                                          # same conversation, a new native process
    with pytest.raises(DomainError) as old_generation:
        owned.owner_check(candidate, 2)
    assert old_generation.value.code == "inbox_generation_stale"


def test_the_paused_or_stopped_desired_state_survives_a_replacement(owned):
    old, candidate = new_id(), new_id()
    owned.start(old)
    with owned.database.transaction() as uow:
        uow.connection.execute("UPDATE native_session_owners SET desired_state='paused' WHERE agent_id=?", (owned.agent,))
    result = owned.start(candidate, event="resume", old_state="dead", expected_generation=1)
    assert result["inbox_desired_state"] == "paused" and owned.row()["desired_state"] == "paused"


# ----- continuity snapshot: the owner and its generation, never a poll ------------------------------------------------------

def live_looking(monkeypatch, *, alive=True, helpers=True):
    monkeypatch.setattr("bazarlyk.bootstrap.codex_session.owner_alive", lambda owner: alive)
    monkeypatch.setattr("bazarlyk.bootstrap.diagnostics.prepared_helpers", lambda projection, workspace=None: {"ready": helpers})


def test_continuity_is_the_owner_and_generation_with_no_poll_or_lease_fact(owned, monkeypatch):
    live_looking(monkeypatch)
    session = new_id()
    owned.start(session)
    before = continuity.snapshot(owned.runtime)
    [(agent, facts)] = before["integrations"].items()
    assert agent == owned.agent and facts["healthy"] is True
    assert (facts["session_id"], facts["generation"], facts["native_owner"], facts["target"]) == (session, 1, "123:456", "local")
    assert facts["delivery"] == "available" and facts["native_process"] == "verified"
    assert not {"last_check_at", "lease_until", "watcher_fresh", "functional_legacy", "legacy_eligible", "protocol"} & set(facts)


def test_a_queued_native_input_is_reported_as_waiting_without_a_receipt_table(owned, monkeypatch):
    live_looking(monkeypatch)
    session = new_id()
    owned.start(session)
    with owned.database.read() as uow:
        assert uow.connection.execute("SELECT 1 FROM sqlite_master WHERE name='native_inputs'").fetchone()
    assert continuity.snapshot(owned.runtime)["integrations"][owned.agent]["delivery"] == "available"
    session_row = new_id()
    with owned.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,owner,"
            "observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (session_row, owned.agent, "codex", session, 1, "terminal", 123, "456",
                                                                       "123:456", "2026-10-12T12:00:00Z", "2026-10-12T12:00:00Z"))
        uow.connection.execute(
            "INSERT INTO native_inputs(id,agent_id,session_id,origin,input_type,source_key,request_hash,state,sequence,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (new_id(), owned.agent, session_row, "owner", "chat", "k", "0" * 64, "waiting", 1,
                                                          "2026-10-12T12:00:00Z", "2026-10-12T12:00:00Z"))
    assert continuity.snapshot(owned.runtime)["integrations"][owned.agent]["delivery"] == "waiting"

