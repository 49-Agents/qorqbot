"""Backend inbox nudges to native sessions: real inbox producers, the real arbiter and engine, a stateful fake terminal and a fake
Codex queue. No agent, terminal client, model, server or network is started and no operational data is read.

Each case runs for Claude Code in a terminal, Codex in a terminal and Codex through its thread queue where the provider adapter
matters."""

from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id
from qorqut_operations.inbox_nudges import InboxNudges

from bazarlyk.native_chat import store
from bazarlyk.native_chat.contracts import InputRequest, NativeChatError
from bazarlyk.native_chat.nudges import HostVisibility, NudgeBackend
from bazarlyk.native_chat.projection import marker_text
from host_foundation_support import create_registered_global
from native_chat_support import Native, count, rows

START = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
VARIANTS = ["claude", "codex", "codex_queue"]


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Deny(HostVisibility):
    """The inbox's real authorization with an explicit revocation switch, for the moment between reservation and input."""

    def __init__(self, inbox):
        super().__init__(inbox)
        self.denied = set()

    def authorized(self, uow, agent_id, events):
        return {identity for identity in super().authorized(uow, agent_id, events) if identity not in self.denied}


class Host:
    def __init__(self, tmp_path, variant="claude"):
        provider = "claude" if variant == "claude" else "codex"
        self.native = Native(tmp_path, provider)            # the registered Global Marketing agent; businesses have no resident agents
        if variant == "codex_queue":
            self.native.transport, self.native.thread_owner = "codex_queue", "owner-1"
        self.variant, self.clock = variant, Clock()
        self.build()

    def build(self):
        native = self.native
        self.visibility = Deny(native.runtime.inbox)
        self.engine = InboxNudges(native.runtime.database, self.visibility, clock=self.clock)
        self.nudges = NudgeBackend(native.runtime, self.engine, native.probe, native.registry, native.arbiter,
                                   native.arbiter.locks, interval=0.05)

    def restart(self):
        """A new object graph over the same database and files, as after a service restart."""
        self.native.build()
        self.build()
        self.native.pass_()

    # ----- producers (the real ones) ---------------------------------------------------------------------------------
    def task(self, title="Review the brief", agent=None):
        instance = self.native.instance
        result = instance.invoke(instance.human(), "tasks_create", {"title": title, "assignee_ids": [agent or self.native.agent]})
        assert result.status == "ok", result.error
        return result.data["task"]["id"]

    def private_message(self, to_agent, text="a private word"):
        runtime = self.native.runtime
        sender = create_registered_global(self.native.instance, "ordubashy")       # an independent peer (one registration)
        result = runtime.application.invoke(runtime.resolver.invocation_context(Principal(sender, "agent")), "agents_message_send",
                                            {"scope": "global", "to_agent_id": to_agent, "message": text, "summary": "greeting"},
                                            InvocationOptions(idempotency_key=new_id()))
        assert result.status == "ok", result.error

    def acknowledge(self, agent, *event_ids):
        with self.native.runtime.database.transaction() as uow:
            for event_id in event_ids:
                uow.connection.execute("UPDATE inbox_deliveries SET acknowledged_at='2026-10-12T12:30:00Z',outcome='handled' "
                                       "WHERE agent_id=? AND event_id=?", (agent, event_id))

    def events(self, agent=None):
        return [row["id"] for row in rows(self.native.runtime, "SELECT event_id AS id FROM inbox_deliveries WHERE agent_id=? "
                                          "AND acknowledged_at IS NULL ORDER BY event_id", (agent or self.native.agent,))]

    # ----- the native side -------------------------------------------------------------------------------------------
    def work(self, running=True):
        """The hook's small activity observation of a running turn (the probe's only evidence of it)."""
        native = self.native
        path = native.workspace / ".bazarlyk/activity" / (native.session_id + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        owner = native.thread_owner or f"{native.pid}:{native.started}"
        path.write_text(json.dumps({"agent_id": native.agent, "session_id": native.session_id, "runtime": native.runtime_name,
                                    "owner": owner, "state": "running" if running else "idle"}))

    def begin_turn(self, user_text):
        """The owner's turn starts in the transcript: a plain user entry (the provider records every input this way)."""
        native = self.native
        if native.provider == "claude":
            native.write(native.claude_user(user_text))
        else:
            native.write(native.codex_event("task_started"), native.codex_event("user_message", message=user_text))
        native.pass_(2)

    def end_turn(self, reply):
        """The provider's own end of the open turn, with its reply."""
        native = self.native
        if native.provider == "claude":
            native.write(native.claude_assistant(reply))
        else:
            native.write(native.codex_assistant(reply, final=True, native_id="item-" + new_id()), native.codex_event("task_complete"))
        native.pass_(2)

    def record_system_input(self, input_type="inbox_nudge"):
        """The latest typed system input reaches the transcript as the provider's own user entry: a terminal recorded it when Enter
        was sent; a thread queue records it only when the thread takes it, which is here."""
        native = self.native
        if self.variant == "codex_queue":
            row = rows(native.runtime, "SELECT * FROM native_inputs WHERE input_type=? ORDER BY sequence DESC LIMIT 1", (input_type,))[0]
            native.write(native.codex_event("user_message", message=marker_text(row["marker"], input_type, "notice")))
            native.pass_(2)

    def system_turn(self, reply="Checked.", input_type="inbox_nudge"):
        """A turn started by the last typed system input (the terminal already recorded its marker entry; a queue is recorded here)."""
        native = self.native
        if self.variant == "codex_queue":
            native.write(native.codex_event("task_started"))
            self.record_system_input(input_type)
        elif native.provider == "codex":
            native.write(native.codex_event("task_started"))
        self.end_turn(reply)

    def nudge_turn(self, reply="Checked."):
        """A turn started by the last typed nudge."""
        self.system_turn(reply)

    def replies(self):
        """What the indexer recorded for each completed reply: who started its turn and whether real user work was in it."""
        return [(row["origin"], row["user_work"]) for row in
                rows(self.native.runtime, "SELECT origin,user_work FROM native_replies ORDER BY completion_sequence")]

    def counters(self):
        with self.native.runtime.database.read() as uow:
            return store.counters(uow.connection, self.native.agent)

    def typed(self):
        """What reached the session: the pane's submitted inputs, or the text of each Codex call (``codex queue`` and the
        steering ``turn/steer``/``turn/start`` path alike carry the text last)."""
        native = self.native
        return list(native.pane.typed()) if self.variant != "codex_queue" else [argv[-1] for argv, _ in native.queued]

    def attempts(self):
        return rows(self.native.runtime, "SELECT * FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence", (self.native.agent,))

    def run(self, times=1):
        for _ in range(times):
            self.nudges.run_once()


@pytest.fixture(params=VARIANTS)
def host(request, tmp_path):
    host = Host(tmp_path, request.param)
    host.native.publish()
    if host.native.provider == "codex":
        host.native.write(host.native.codex_session_meta())
    host.native.pass_()                                                           # the native backend binds the exact session
    return host


# ----- delivery ---------------------------------------------------------------------------------------------------------

def test_pending_events_coalesce_into_one_system_notice_through_the_arbiter(host):
    ids = [host.task(f"Task {index}") for index in range(3)]
    host.run()
    [text] = host.typed()
    [attempt] = host.attempts()
    assert attempt["state"] == "submitted" and attempt["kind"] == "initial" and len(json.loads(attempt["event_ids_json"])) == 3
    [row] = rows(host.native.runtime, "SELECT * FROM native_inputs")
    assert (row["origin"], row["input_type"], row["state"]) == ("system", "inbox_nudge", "submitted")
    assert row["source_key"] == "inbox-nudge:" + attempt["id"] and row["session_id"] == json.loads(attempt["binding_json"])["binding_id"]
    assert f'id="{row["marker"]}"' in text and 'type="inbox_nudge"' in text
    assert "Task 0" not in text and "Review the brief" not in text                       # the notice names no item
    assert "priority" in text and "MCP" in text and "acknowledges nothing" in text
    # The announced backlog is not announced again, however often passes run.
    host.clock.advance(3600)
    host.run(3)
    assert len(host.typed()) == 1 and len(host.attempts()) == 1
    assert len(ids) == 3 and sorted(json.loads(attempt["event_ids_json"])) == host.events()


def test_a_busy_session_gets_the_notice_at_once_without_inferring_processing(host):
    host.task()
    host.native.pane.busy = True
    host.work()
    host.run()
    [attempt] = host.attempts()
    assert len(host.typed()) == 1 and attempt["state"] == "submitted"
    assert json.loads(attempt["progress_json"])["working"] is True
    outcome = json.loads(attempt["outcome_json"])
    assert outcome["state"] == "submitted" and outcome["receipt"]["effect"] == "submitted"
    assert outcome["receipt"]["binding"]["generation"] == 1
    # Submission is not processing: no reply was indexed and nothing was acknowledged.
    assert count(host.native.runtime, "native_replies") == 0 and len(host.events()) == 1


@pytest.mark.parametrize("variant", ["claude", "codex"])
def test_an_occupied_composer_defers_without_touching_it_and_the_need_stays_pending(tmp_path, variant):
    host = Host(tmp_path, variant)
    host.native.publish()
    host.native.pass_()
    host.task()
    host.native.pane.pasted = "half-written owner text"
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] == "deferred" and host.native.pane.pasted == "half-written owner text"
    assert host.typed() == [] and not [call for call in host.native.pane.calls if call[0] == "send-keys"]
    view = host.nudges.status(host.native.agent)
    assert view["state"] == "deferred" and view["active"] and view["pending"] == 1
    # The same pass again does nothing until the retry backoff elapses, and then types once into the freed composer.
    host.run()
    assert len(host.attempts()) == 1
    host.native.pane.pasted = None
    host.clock.advance(6)
    host.run()
    assert [row["state"] for row in host.attempts()] == ["deferred", "submitted"] and len(host.typed()) == 1


def test_a_pending_chat_input_goes_before_any_advisory(host):
    host.task()
    host.native.pane.pasted = "owner draft"
    host.native.chat.send(host.native.owner, host.native.agent, "Please change course", "chat-key-0001", host.native.pin())
    host.run()
    assert host.attempts() == []                                              # user work keeps its priority
    host.native.pane.pasted = None
    host.native.pass_()
    host.run()
    assert len(host.attempts()) == 1


# ----- stale reservations ------------------------------------------------------------------------------------------------

def hook_before_input(host, action):
    """Run ``action`` after the reservation exists and immediately before the adapter reaches its checkpoint."""
    arbiter = host.native.arbiter
    real = arbiter.adapters[host.native.transport]

    class Wrapped:
        name = real.name

        def submit(self, profile, session, text, **options):
            action()
            return real.submit(profile, session, text, **options)

    arbiter.adapters[host.native.transport] = Wrapped()


def test_an_acknowledgement_before_input_cancels_the_reservation_and_types_nothing(host):
    host.task()
    [event] = host.events()
    hook_before_input(host, lambda: host.acknowledge(host.native.agent, event))
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] == "stale" and attempt["reason"] == "coverage_handled"
    assert host.typed() == [] and count(host.native.runtime, "native_inputs") == 0
    assert host.nudges.status(host.native.agent)["state"] == "idle"


def test_revoked_access_before_input_cancels_the_reservation(host):
    host.task()
    [event] = host.events()
    hook_before_input(host, lambda: host.visibility.denied.add(event))
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] == "stale" and host.typed() == [] and count(host.native.runtime, "native_inputs") == 0
    assert host.events() == [event]                                           # revocation is not an acknowledgement


def test_a_partial_acknowledgement_before_input_still_types_once_and_delivery_acknowledges_nothing(host):
    host.task("First")
    host.task("Second")
    first, second = host.events()
    hook_before_input(host, lambda: host.acknowledge(host.native.agent, first))
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] == "submitted" and len(host.typed()) == 1         # an event the notice covers is still pending
    assert host.events() == [second]                                          # only the explicit acknowledgement changed anything
    [delivery] = rows(host.native.runtime, "SELECT acknowledged_at FROM inbox_deliveries WHERE agent_id=? AND event_id=?",
                      (host.native.agent, second))
    assert delivery["acknowledged_at"] is None


def test_a_session_that_changes_before_input_aborts_the_old_attempt(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    old_pane = host.native.pane
    hook_before_input(host, lambda: host.native.new_session())
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] in {"stale", "deferred"} and old_pane.typed() == [] and host.native.pane.typed() == []
    assert count(host.native.runtime, "native_inputs") == 0
    # Un-wrap and let the next pass address the exact new generation.
    host.native.build()
    host.build()
    host.clock.advance(61)
    host.run()
    assert len(host.native.pane.typed()) == 1
    assert json.loads(host.attempts()[-1]["binding_json"])["generation"] == 2


# ----- an unchanged backlog is never announced again ----------------------------------------------------------------------
# Host seam: the backend passes the engine no work or user counter, so nothing done in Chat, no reply and no elapsed time can
# make an announced, unacknowledged backlog due again. These hold with any engine behind the public interface.

def test_work_running_at_the_first_notice_finishing_never_announces_the_unchanged_backlog_again(host):
    host.begin_turn("Fix the lint errors")                                       # the owner's turn is running when the notice comes
    host.task()
    host.native.pane.busy = True
    host.work()
    host.run()
    assert len(host.attempts()) == 1 and len(host.typed()) == 1                  # delivered at once, even while busy
    host.clock.advance(120)
    host.run()
    host.native.pane.busy = False
    host.work(running=False)
    host.end_turn("Lint errors fixed.")                                          # the running work finishes: a real, owner-started reply
    assert host.counters()["work_sequence"] == 1
    host.nudge_turn("Looked at the inbox; I will handle it later.")             # the agent defers: the event stays unacknowledged
    for _ in range(3):
        host.clock.advance(24 * 3600)
        host.run()
    assert len(host.attempts()) == 1 and len(host.typed()) == 1 and len(host.events()) == 1


def test_a_direct_chat_input_worked_through_never_announces_the_unchanged_backlog_again(host):
    host.task()
    host.run()                                                                   # idle agent: one notice
    host.nudge_turn("Reviewed the inbox.")
    host.clock.advance(3600)
    sent = host.native.chat.send(host.native.owner, host.native.agent, "Please check the supplier list", "chat-key-0002", host.native.pin())
    assert sent["input"]["state"] in {"waiting", "submitted"}
    host.native.pass_()                                                          # the backend types the waiting input
    if host.variant == "codex_queue":                                            # a queue does not record the turn it starts here
        host.begin_turn("Please check the supplier list")
    host.end_turn("Supplier list checked.")
    assert host.counters()["user_sequence"] == 1 and host.counters()["work_sequence"] == 1
    for _ in range(3):
        host.clock.advance(3600)
        host.run()
    attempts = host.attempts()
    assert len(attempts) == 1 and len([text for text in host.typed() if 'type="inbox_nudge"' in text]) == 1


def test_the_engine_gets_no_chat_counter_and_counters_do_not_change_what_can_make_a_notice_due(host):
    observations = []
    real = host.engine.prepare

    def prepare(agent_id, binding, observation=None):
        observations.append(observation)
        return real(agent_id, binding, observation)

    host.engine.prepare = prepare
    host.task()
    host.native.pane.busy = True
    host.work()
    host.run()
    with host.native.runtime.database.read() as uow:
        before = host.nudges._fingerprint(uow.connection, host.native.agent)[0]
    host.native.pane.busy = False
    host.work(running=False)
    host.end_turn("Done.")
    chat_into_the_open_turn(host, "Please check the supplier list", "chat-key-counters")
    host.end_turn("Checked.")
    with host.native.runtime.database.read() as uow:
        after = host.nudges._fingerprint(uow.connection, host.native.agent)[0]
    assert host.counters()["work_sequence"] >= 1 and host.counters()["user_sequence"] == 1
    assert before == after                                                       # only the binding and the pending set count
    host.clock.advance(3600)
    host.nudges._memo.clear()                                                    # ask the engine again, whatever it said before
    host.run()
    assert observations and all((seen.work, seen.user) == (0, 0) for seen in observations)
    assert observations[0].working is True                                       # busy evidence stays a diagnostic
    assert len(host.attempts()) == 1


def chat_into_the_open_turn(host, text, key):
    """The owner's Chat input is typed while the turn is open (a queue does not record the entry it starts here)."""
    host.native.chat.send(host.native.owner, host.native.agent, text, key, host.native.pin())
    host.native.pass_()
    if host.variant == "codex_queue":
        host.native.write(host.native.codex_event("user_message", message=text))
        host.native.pass_(2)


def test_owner_input_steered_into_a_nudge_turn_is_work_but_never_announces_the_backlog_again(host):
    host.task()
    host.run()                                                                   # the idle agent's one notice starts a turn
    host.clock.advance(120)
    if host.variant == "codex_queue":
        host.native.write(host.native.codex_event("task_started"))
    host.record_system_input()
    chat_into_the_open_turn(host, "Please check the supplier list", "chat-key-steer-1")        # nudge -> owner input -> end of turn
    assert host.counters()["user_sequence"] == 1
    host.end_turn("Inbox reviewed and the supplier list checked.")
    assert host.replies() == [("nudge", 1)] and host.counters()["work_sequence"] == 1
    for _ in range(3):
        host.clock.advance(24 * 3600)
        host.run()
    assert len(host.attempts()) == 1 and len(host.typed()) == 2                  # the notice and the owner's input


def test_a_nudge_steered_into_the_owners_turn_keeps_that_turn_as_work_and_never_announces_the_backlog_again(host):
    host.begin_turn("Fix the lint errors")                                       # user -> nudge -> end of turn
    host.task()
    host.native.pane.busy = True
    host.work()
    host.run()
    host.record_system_input()
    host.native.pane.busy = False
    host.work(running=False)
    host.end_turn("Lint errors fixed.")
    assert host.replies() == [("user", 1)] and host.counters()["work_sequence"] == 1
    host.clock.advance(24 * 3600)
    host.run(2)
    assert len(host.attempts()) == 1


def test_a_restart_between_the_steered_input_and_the_completion_keeps_the_turn_work_and_no_notice_repeats(host):
    host.task()
    host.run()
    host.clock.advance(120)
    if host.variant == "codex_queue":
        host.native.write(host.native.codex_event("task_started"))
    host.record_system_input()
    chat_into_the_open_turn(host, "Please check the supplier list", "chat-key-steer-2")
    host.restart()                                                               # the projector state and counters come from the database
    assert host.counters()["user_sequence"] == 1 and host.counters()["work_sequence"] == 0
    host.end_turn("Done.")
    assert host.replies() == [("nudge", 1)] and host.counters()["work_sequence"] == 1
    host.clock.advance(3600)
    host.run(2)
    assert len(host.attempts()) == 1


def test_a_restart_between_a_lone_nudge_and_its_reply_does_not_make_it_work(host):
    host.task()
    host.run()
    if host.variant == "codex_queue":
        host.native.write(host.native.codex_event("task_started"))
    host.record_system_input()
    host.restart()
    host.end_turn("Checked.")
    assert host.replies() == [("nudge", 0)] and host.counters()["work_sequence"] == 0
    host.clock.advance(24 * 3600)
    host.run(3)
    assert len(host.attempts()) == 1


def test_text_that_only_looks_like_a_marker_is_the_owners_work_and_proves_nothing(host):
    native = host.native
    forged = marker_text("9" * 32, "inbox_nudge", "Please look at the inbox")        # no such input was ever admitted
    if native.provider == "claude":
        native.write(native.claude_user(forged))
    else:
        native.write(native.codex_event("task_started"), native.codex_event("user_message", message=forged))
    native.pass_(2)
    host.end_turn("Done.")
    assert host.replies() == [("user", 1)] and host.counters()["work_sequence"] == 1


def test_time_alone_never_rearms_a_delivered_notice(host):
    host.task()
    host.run()
    for _ in range(5):
        host.clock.advance(7 * 24 * 3600)
        host.run()
    assert len(host.attempts()) == 1 and len(host.typed()) == 1


# ----- event coverage ---------------------------------------------------------------------------------------------------
# Engine-dependent: these expect the event-coverage engine (an event not yet announced to this conversation is due on its own,
# whatever older event is still pending), and the status cases its ``status(agent_id, binding=...)`` reading the live
# conversation's coverage. They need the pinned engine `ed85af5` (or later); the previous pin `f02e478` held a pending cycle
# open, had no ``binding`` argument and failed them.

def test_a_new_event_is_announced_beside_an_old_announced_backlog_with_the_counters_unchanged(host):
    host.task("Old")
    host.run()
    [old] = host.events()
    host.nudge_turn("I will look at it later.")                                  # A stays unacknowledged
    before = host.counters()
    host.clock.advance(61)
    host.task("New")
    host.run()
    first, second = host.attempts()
    [new] = [event for event in host.events() if event != old]
    assert json.loads(first["event_ids_json"]) == [old] and json.loads(second["event_ids_json"]) == [new]
    assert second["state"] == "submitted" and len([text for text in host.typed() if 'type="inbox_nudge"' in text]) == 2
    assert (host.counters()["work_sequence"], host.counters()["user_sequence"]) == (before["work_sequence"], before["user_sequence"])
    assert host.events() == sorted([old, new])                                   # nothing was acknowledged by the notices
    host.clock.advance(3600)
    host.run(3)
    assert len(host.attempts()) == 2


def test_events_arriving_during_the_spacing_wait_and_then_go_out_in_one_batch(host):
    host.task("First")
    host.run()
    [first] = host.events()
    host.clock.advance(10)
    host.task("During spacing 1")
    host.run()
    host.clock.advance(20)
    host.task("During spacing 2")
    host.run(2)
    assert len(host.attempts()) == 1                                             # the minimum spacing holds them back
    later = [event for event in host.events() if event != first]
    host.clock.advance(31)
    host.run()
    attempts = host.attempts()
    assert len(attempts) == 2 and sorted(json.loads(attempts[1]["event_ids_json"])) == later
    host.clock.advance(3600)
    host.run(3)
    assert len(host.attempts()) == 2 and len(host.typed()) == 2


def test_status_does_not_call_an_inbox_announced_while_a_newer_event_is_not(host):
    host.task("Old")
    host.run()
    assert host.nudges.status(host.native.agent)["state"] == "submitted"
    host.clock.advance(10)
    host.task("New")
    view = host.nudges.status(host.native.agent)
    assert view["state"] == "pending" and view["pending"] == 2                   # the pending count is not the coverage
    delivery = host.native.runtime.inbox.owner_snapshot(host.native.owner_context(), host.native.agent)["delivery"]
    assert delivery["state"] == "pending"


def test_a_fresh_conversation_is_pending_before_its_first_notice(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    host.run()
    assert host.nudges.status(host.native.agent)["state"] == "submitted"
    host.native.new_session()                                                   # a new conversation, told nothing yet
    host.native.pass_()
    host.run()
    assert len(host.attempts()) == 1                                            # the minimum spacing still holds it back
    view = host.nudges.status(host.native.agent)
    assert view["state"] == "pending" and view["pending"] == 1                   # not "submitted" because the old one was told
    delivery = host.native.runtime.inbox.owner_snapshot(host.native.owner_context(), host.native.agent)["delivery"]
    assert delivery["state"] == "pending"
    host.clock.advance(61)
    host.run()
    assert len(host.attempts()) == 2 and host.nudges.status(host.native.agent)["state"] == "submitted"


def test_an_agent_offline_after_an_old_notice_is_pending_offline_not_submitted(host):
    host.task()
    host.run()
    assert host.nudges.status(host.native.agent)["state"] == "submitted"
    host.native.end()                                                            # the session ends; nobody starts another
    host.native.pass_()
    view = host.nudges.status(host.native.agent)
    assert (view["state"], view["reason"]) == ("pending", "offline") and view["active"]
    delivery = host.native.runtime.inbox.owner_snapshot(host.native.owner_context(), host.native.agent)["delivery"]
    assert (delivery["state"], delivery["reason"]) == ("pending", "offline")
    host.clock.advance(3600)
    host.run(2)
    assert len(host.attempts()) == 1                                            # offline: nothing typed, nothing started
    host.acknowledge(host.native.agent, *host.events())
    assert host.nudges.status(host.native.agent)["state"] == "idle"              # nothing pending: idle, offline or not


def test_an_uncertain_notice_suppresses_only_its_own_events_and_a_new_event_is_still_announced(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task("Old")
    [old] = host.events()
    host.native.pane.enter_works = False                                        # the paste landed; the submit key did nothing observable
    host.run()
    [uncertain] = host.attempts()
    assert uncertain["state"] == "uncertain"
    host.native.pane.enter_works, host.native.pane.pasted = True, None
    host.restart()
    host.clock.advance(61)
    host.task("New")
    host.run()
    first, second = host.attempts()
    [new] = [event for event in host.events() if event != old]
    assert first["state"] == "uncertain" and second["state"] == "submitted"
    assert json.loads(second["event_ids_json"]) == [new]                         # never a blind replay of the uncertain one
    assert len(host.native.pane.typed()) == 1
    host.clock.advance(3600)
    host.run(3)
    assert len(host.attempts()) == 2


def test_the_same_conversation_in_a_new_process_or_after_a_backend_restart_is_not_told_again(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task("Old")
    host.run()
    [first] = host.attempts()
    conversation = json.loads(first["binding_json"])["conversation_id"]
    host.native.new_session(same_conversation=True)                             # the same conversation, reattached by a new process
    host.native.pass_()
    host.restart()                                                              # and the backend restarts
    host.clock.advance(3600)
    host.run(3)
    assert len(host.attempts()) == 1 and len(host.native.pane.typed()) == 1
    host.task("New")
    host.run()
    first, second = host.attempts()
    binding = json.loads(second["binding_json"])
    assert binding["conversation_id"] == conversation and binding["generation"] > json.loads(first["binding_json"])["generation"]
    assert len(json.loads(second["event_ids_json"])) == 1 and len(host.native.pane.typed()) == 2


# ----- offline, restart, generations ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["claude", "codex"])
def test_an_offline_event_waits_for_a_manual_session_start_and_nothing_launches_one(tmp_path, variant):
    host = Host(tmp_path, variant)                                              # no session was ever published
    host.task()
    host.run(2)
    assert host.attempts() == [] and count(host.native.runtime, "native_inputs") == 0
    assert host.nudges.status(host.native.agent)["reason"] == "offline"
    assert host.nudges.status(host.native.agent)["state"] == "pending"
    host.clock.advance(3600)
    host.run()
    assert host.attempts() == [] and host.native.pane.calls == []               # no probe-driven action, no terminal activity
    host.native.publish()                                                       # a person starts the session by hand
    host.native.pass_()
    host.run()
    assert len(host.attempts()) == 1 and len(host.native.pane.typed()) == 1


@pytest.mark.parametrize("desired", ["paused", "stopped"])
def test_an_explicit_stop_or_pause_is_not_undone_by_an_inbox_event(host, desired):
    host.task()
    agent = host.native.agent
    with host.native.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) "
                               "VALUES(?,?,?,?,?)", (agent, host.native.session_id, json.dumps({"owner": "synthetic"}), 1, desired))
    host.run(2)
    with host.native.runtime.database.read() as uow:
        [profile] = [item for item in store.observed_agents(uow.connection, host.native.runtime.paths) if item["agent_id"] == agent]
    assert host.nudges.step(profile) == "desired_" + desired
    host.clock.advance(3600)
    host.run()
    # Nothing was reserved, typed or recorded, and the pending need is untouched.
    assert host.attempts() == [] and host.typed() == [] and count(host.native.runtime, "native_inputs") == 0
    assert len(host.events()) == 1
    with host.native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE native_session_owners SET desired_state='running' WHERE agent_id=?", (agent,))
    host.run()
    assert len(host.attempts()) == 1 and len(host.typed()) == 1


def test_an_uncertain_submission_survives_a_restart_and_is_never_resent(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    host.native.pane.enter_works = False                                        # the paste landed; the submit key did nothing observable
    host.run()
    [attempt] = host.attempts()
    assert attempt["state"] == "uncertain"
    host.native.pane.enter_works, host.native.pane.pasted = True, None
    host.restart()
    host.clock.advance(3600)
    host.run(2)
    [attempt] = host.attempts()
    assert attempt["state"] == "uncertain" and host.native.pane.typed() == []
    assert host.nudges.status(host.native.agent)["state"] == "uncertain"
    assert "does not mean the agent read" in host.nudges.status(host.native.agent)["note"]


def test_a_reservation_left_by_a_crash_is_cancelled_or_settled_from_the_native_record(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    with host.native.runtime.database.read() as uow:
        session = store.live_session(uow.connection, host.native.agent)
    from bazarlyk.native_chat.nudges import binding_of
    reservation = host.engine.prepare(host.native.agent, binding_of(session))
    host.restart()
    host.nudges.reconcile_reservations()                                        # nothing was claimed: never typed later
    [attempt] = host.attempts()
    assert attempt["state"] == "stale" and attempt["reason"] == "unclaimed" and host.native.pane.typed() == []
    host.clock.advance(1)
    host.run()
    assert len(host.attempts()) == 2 and len(host.native.pane.typed()) == 1 and reservation.agent_id == host.native.agent


def test_two_backend_passes_at_once_type_one_notice(host):
    host.task()
    other = NudgeBackend(host.native.runtime, host.engine, host.native.probe, host.native.registry, host.native.arbiter,
                         host.native.arbiter.locks, interval=0.05)
    barrier, errors = threading.Barrier(2), []

    def run(backend):
        try:
            barrier.wait(10)
            backend.run_once()
        except Exception as error:                                              # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=run, args=(backend,)) for backend in (host.nudges, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and len(host.attempts()) == 1 and len(host.typed()) == 1
    assert count(host.native.runtime, "native_inputs", "input_type='inbox_nudge'") == 1


def test_a_reservation_typed_before_a_crash_is_settled_from_its_native_input_and_never_typed_again(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    with host.native.runtime.database.read() as uow:
        session = store.live_session(uow.connection, host.native.agent)
    from bazarlyk.native_chat.nudges import binding_of
    reservation = host.engine.prepare(host.native.agent, binding_of(session))
    typed = host.native.arbiter.admit_nudge(reservation.agent_id, reservation.source_key, reservation.text,
                                            expected_binding=session["id"])     # the input went out; the settle did not
    assert typed.state == "submitted" and host.attempts()[0]["state"] == "prepared"
    host.restart()
    host.nudges.reconcile_reservations()
    host.nudges.reconcile_reservations()                                        # reconciling twice changes nothing
    [attempt] = host.attempts()
    assert attempt["state"] == "submitted" and json.loads(attempt["outcome_json"])["receipt"]["input_id"] == typed.input_id
    assert count(host.native.runtime, "inbox_nudge_evidence") == 1
    host.clock.advance(3600)
    host.run(2)
    assert len(host.attempts()) == 1 and len(host.native.pane.typed()) == 1


def test_a_new_conversation_gets_the_outstanding_batch_and_the_old_receipt_stays_exact(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.task()
    host.run()
    old_pane = host.native.pane
    host.native.new_session()
    host.native.pass_()
    host.run()
    assert len(host.attempts()) == 1                                            # minimum spacing: a lower bound, then...
    host.clock.advance(61)
    host.run()
    first, second = host.attempts()
    old, new = json.loads(first["binding_json"]), json.loads(second["binding_json"])
    assert first["kind"] == "initial" and old["conversation_id"] != new["conversation_id"]
    assert json.loads(second["event_ids_json"]) == json.loads(first["event_ids_json"]) == host.events()
    assert old["generation"] == 1 and new["generation"] == 2
    assert first["state"] == second["state"] == "submitted" and len(old_pane.typed()) == 1 and len(host.native.pane.typed()) == 1
    assert first["outcome_json"] != second["outcome_json"]


# ----- unchanged workflows, several recipients --------------------------------------------------------------------------

def test_a_nudge_changes_no_task_signoff_message_or_acknowledgement_state(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    host.private_message(host.native.agent, "TOP-SECRET-WORD")
    tables = ("tasks", "signoffs", "agent_messages", "inbox_events", "inbox_deliveries", "relationships")
    before = {table: rows(host.native.runtime, f"SELECT * FROM {table} ORDER BY 1,2") for table in tables}
    host.run()
    after = {table: rows(host.native.runtime, f"SELECT * FROM {table} ORDER BY 1,2") for table in tables}
    assert after == before and len(host.attempts()) == 1
    [text] = host.native.pane.typed()
    assert "TOP-SECRET-WORD" not in text and "greeting" not in text            # a private message discloses nothing in the notice
    assert all(row["acknowledged_at"] is None for row in after["inbox_deliveries"])


def test_several_recipients_in_business_and_global_scope_are_independent(tmp_path):
    host = Host(tmp_path, "claude")
    host.native.publish()
    host.native.pass_()
    # The other recipient is Ordubashy, offline: the private message's sender, deliberately reused, assigned a business Task.
    other = create_registered_global(host.native.instance, "ordubashy")
    host.private_message(host.native.agent)
    host.task("Business task", agent=other)
    host.run(2)
    assert len(host.native.pane.typed()) == 1                                    # the live global recipient
    assert rows(host.native.runtime, "SELECT COUNT(*) AS n FROM inbox_nudge_attempts WHERE agent_id=?", (other,))[0]["n"] == 0
    other_view = host.nudges.status(other)
    assert other_view["state"] == "pending" and other_view["reason"] == "offline"
    host.acknowledge(other, *host.events(other))
    assert host.nudges.status(other)["state"] == "idle"
    assert host.nudges.status(host.native.agent)["state"] == "submitted"


def test_a_bootstrap_recovery_notice_is_its_own_system_input_type_and_no_inbox_event(host):
    native = host.native
    result = host.nudges.recovery_notice(native.agent, new_id(), consumer_id=native.session_id)
    assert result["state"] == "submitted" and len(host.typed()) == 1
    [row] = rows(native.runtime, "SELECT * FROM native_inputs")
    assert (row["origin"], row["input_type"]) == ("system", "bootstrap_recovery")
    assert set(json.loads(row["metadata_json"])) == {"episode"} and row["marker"] in host.typed()[0]
    assert 'type="bootstrap_recovery"' in host.typed()[0] and 'type="inbox_nudge"' not in host.typed()[0]
    assert count(native.runtime, "inbox_events") == 0 and count(native.runtime, "inbox_nudge_attempts") == 0
    again = host.nudges.recovery_notice(native.agent, json.loads(row["metadata_json"])["episode"], consumer_id=native.session_id)
    assert again["state"] == "submitted" and again["input_id"] == result["input_id"] and len(host.typed()) == 1   # one input per episode


def test_a_recovery_notice_names_its_exact_session_at_the_producer_and_the_arbiter(host):
    native = host.native
    with pytest.raises(DomainError) as missing:
        host.nudges.recovery_notice(native.agent, new_id(), consumer_id="")
    assert missing.value.code == "validation_error"
    with pytest.raises(TypeError):
        host.nudges.recovery_notice(native.agent, new_id())                                    # the conversation is not optional
    with pytest.raises(TypeError):
        native.arbiter.admit_recovery(native.agent, "bootstrap_recovery:x", "text")            # nor is the exact binding
    with pytest.raises(NativeChatError) as unpinned:
        native.arbiter.submit(InputRequest(native.agent, "bootstrap_recovery", "bootstrap_recovery:y", "text", steer=True))
    assert unpinned.value.code == "validation_error"
    with pytest.raises(NativeChatError) as owned:
        native.arbiter.submit(InputRequest(native.agent, "bootstrap_recovery", "bootstrap_recovery:z", "text", principal_id=native.owner,
                                           steer=True, expected_binding="whatever"))
    assert owned.value.code == "validation_error"
    assert host.typed() == [] and count(native.runtime, "native_inputs") == 0


def test_a_recovery_notice_is_pinned_to_the_conversation_of_the_hook_that_asked(host):
    native = host.native
    elsewhere = host.nudges.recovery_notice(native.agent, new_id(), consumer_id=new_id())      # another conversation's hook
    assert (elsewhere["state"], elsewhere["reason"], elsewhere["input_id"]) == ("deferred", "session_not_current", None)
    assert host.typed() == [] and count(native.runtime, "native_inputs") == 0                  # nothing typed, nothing recorded
    episode = new_id()
    asked = host.nudges.recovery_notice(native.agent, episode, consumer_id=native.session_id)
    assert asked["state"] == "submitted" and asked["binding"]["conversation_id"] == native.session_id and len(host.typed()) == 1
    again = host.nudges.recovery_notice(native.agent, episode, consumer_id=native.session_id)
    assert again["state"] == "submitted" and again["input_id"] == asked["input_id"] and len(host.typed()) == 1


def test_a_recovery_wake_neither_spends_nor_rearms_an_inbox_notice_and_its_own_reply_is_no_work(host):
    host.task()
    host.run()                                                                   # the idle agent gets its one inbox notice
    host.nudge_turn("Reviewed the inbox.")
    assert len(host.attempts()) == 1 and host.counters()["work_sequence"] == 0
    host.clock.advance(3600)
    woken = host.nudges.recovery_notice(host.native.agent, new_id(), consumer_id=host.native.session_id)
    assert woken["state"] == "submitted"
    host.system_turn("Context recovered.", "bootstrap_recovery")
    host.clock.advance(24 * 3600)
    host.run(3)
    assert host.replies() == [("nudge", 0), ("system", 0)] and host.counters()["work_sequence"] == 0
    assert len(host.attempts()) == 1 and len([text for text in host.typed() if 'type="inbox_nudge"' in text]) == 1
    assert host.nudges.status(host.native.agent)["attempts"][0]["state"] == "submitted"
    assert [row["kind"] for row in host.attempts()] == ["initial"]


def test_the_chat_labels_a_recovery_wake_as_a_session_recovery_notice_never_an_inbox_nudge_or_the_owner(host):
    native = host.native
    host.nudges.recovery_notice(native.agent, new_id(), consumer_id=native.session_id)
    host.record_system_input("bootstrap_recovery")
    native.pass_(2)
    notices = [entry for entry in native.chat.state(native.owner, native.agent)["entries"] if entry["speaker"] == "notice"]
    assert len(notices) == 1 and notices[0]["system"]["input_type"] == "bootstrap_recovery" and notices[0]["system"]["verified"]
    assert notices[0]["system"]["input_id"] and "owner" not in json.dumps(notices[0]["system"])


# ----- the owner's inbox read ---------------------------------------------------------------------------------------------

DELIVERY_KEYS = {"state", "reason", "last_attempt_at"}
POLLER_KEYS = {"watcher", "watcher_active", "lease", "lease_until", "last_check_at", "last_poll_at", "poll", "notified_at"}


def test_the_owner_inbox_read_adds_the_delivery_state_with_the_meaning_of_the_activity_list(host):
    native = host.native
    runtime, owner = native.runtime, native.owner_context()
    quiet = runtime.inbox.owner_snapshot(owner, native.agent)
    assert quiet["delivery"] == {"state": "idle", "reason": None, "last_attempt_at": None} and quiet["items"] == []
    host.task()
    waiting = runtime.inbox.owner_snapshot(owner, native.agent)
    assert waiting["delivery"]["state"] == "pending" and waiting["delivery"]["last_attempt_at"] is None
    host.run()
    typed = runtime.inbox.owner_snapshot(owner, native.agent)
    assert typed["delivery"]["state"] == "submitted" and typed["delivery"]["last_attempt_at"]
    assert set(typed["delivery"]) == DELIVERY_KEYS and typed["delivery"] == runtime.activity.nudge(native.agent)
    assert typed["delivery"]["state"] == host.nudges.status(native.agent)["state"]
    # Items, subscriptions and pagination are what they were; nothing of the retired watcher or poll lease is back.
    assert set(typed) == {"items", "next_cursor", "subscriptions", "delivery"} and len(typed["items"]) == 1
    assert typed["next_cursor"] is None and not POLLER_KEYS & set(typed) and not POLLER_KEYS & set(typed["delivery"])
    assert runtime.inbox.owner_snapshot(owner, native.agent, after=typed["items"][0]["id"])["items"] == []
    host.acknowledge(native.agent, *host.events())
    assert runtime.inbox.owner_snapshot(owner, native.agent)["delivery"]["state"] == "idle"          # nothing left to notify about


def test_the_owner_inbox_read_checks_the_owner_and_agent_before_it_reads_any_delivery_state(host):
    native = host.native
    runtime = native.runtime
    asked = []
    runtime.inbox.delivery = lambda agent_id: asked.append(agent_id) or {"state": "idle", "reason": None, "last_attempt_at": None}
    outsider = create_registered_global(native.instance, "ordubashy")
    stranger = runtime.resolver.invocation_context(Principal(outsider, "agent"))
    with pytest.raises(DomainError) as agent_caller:
        runtime.inbox.owner_snapshot(stranger, native.agent)                      # an agent is not the owner of another agent
    assert agent_caller.value.code == "forbidden"
    with pytest.raises(DomainError) as unknown_agent:
        runtime.inbox.owner_snapshot(native.owner_context(), new_id())
    assert unknown_agent.value.code == "not_found"
    assert asked == []
    runtime.inbox.owner_snapshot(native.owner_context(), native.agent)
    assert asked == [native.agent]


def test_the_owner_inbox_read_says_unknown_with_a_reason_when_diagnostics_are_unavailable(host):
    native = host.native
    runtime, owner = native.runtime, native.owner_context()
    host.task()
    working = runtime.inbox_nudges
    try:
        runtime.inbox_nudges = None
        gone = runtime.inbox.owner_snapshot(owner, native.agent)
        assert gone["delivery"] == {"state": "unknown", "reason": "nudge_backend_not_running", "last_attempt_at": None}
        assert gone["delivery"] == runtime.activity.nudge(native.agent) and len(gone["items"]) == 1       # the items still read

        class Broken:
            def status(self, agent_id):
                raise DomainError("internal_error", "diagnostics are down")

        runtime.inbox_nudges = Broken()
        broken = runtime.inbox.owner_snapshot(owner, native.agent)
        assert broken["delivery"] == {"state": "unknown", "reason": "nudge_status_unavailable", "last_attempt_at": None}
        assert broken["delivery"] == runtime.activity.nudge(native.agent) and len(broken["items"]) == 1
    finally:
        runtime.inbox_nudges = working


def test_the_lifetime_thread_drains_within_its_bound_and_restarts(host):
    host.task()
    host.nudges.start()
    host.nudges.wake()
    deadline = datetime.now().timestamp() + 10
    while datetime.now().timestamp() < deadline and not host.attempts():
        host.nudges.wake()
        time.sleep(0.05)
    host.nudges.stop(5)
    assert len(host.attempts()) == 1 and host.nudges._thread is None
    host.nudges.stop(5)                                                          # stopping twice is harmless
    host.nudges.start()
    host.nudges.stop(5)


def test_a_task_snooze_creates_one_eligible_inbox_event_and_follows_the_exact_native_delivery_path(host):
    """The production hook (the Task service's snooze reconciliation over its routing ports and this database, wired by the
    host composition) runs in the nudge pass; the injected native graph and provider then deliver the one resulting event
    exactly as any other inbox event."""
    native, agent = host.native, host.native.agent
    runtime = native.runtime
    assert runtime.inbox_nudges.reconcile is not None                                 # the composed hook, not a test stand-in
    task = host.task("Follow up on the supplier")
    host.acknowledge(agent, *host.events())                                           # the creation events are handled
    assert host.events() == []
    until = datetime.now(timezone.utc) + timedelta(seconds=1)
    [created] = rows(runtime, "SELECT revision FROM tasks WHERE id=?", (task,))
    snoozed = runtime.application.invoke(native.instance.human(), "tasks_update",
                                         {"task_id": task, "changes": {"snoozed_until": until.isoformat()}},
                                         InvocationOptions(idempotency_key=new_id(), expected_revision=created["revision"]))
    assert snoozed.status == "ok", snoozed.error
    host.acknowledge(agent, *host.events())                                           # so is the update that set the snooze
    host.nudges.reconcile = runtime.inbox_nudges.reconcile
    host.run()
    assert host.events() == [] and host.typed() == [] and host.attempts() == []       # not due yet: nothing woke, nothing sent
    while datetime.now(timezone.utc) <= until:
        time.sleep(0.05)

    host.run()
    [event] = host.events()
    [wake] = rows(runtime, "SELECT * FROM task_snooze_wakes")
    assert wake["task_id"] == task and wake["event_id"] == event and wake["snooze_generation"] == 1
    [stored] = rows(runtime, "SELECT event_type,source_type,source_id FROM inbox_events WHERE id=?", (event,))
    assert (stored["event_type"], stored["source_type"], stored["source_id"]) == ("snooze_expired", "tasks", task)
    [attempt] = host.attempts()
    assert attempt["state"] == "submitted" and attempt["kind"] == "initial" and json.loads(attempt["event_ids_json"]) == [event]
    [text] = host.typed()
    [native_input] = rows(runtime, "SELECT origin,input_type,state,source_key FROM native_inputs")
    assert (native_input["origin"], native_input["input_type"], native_input["state"]) == ("system", "inbox_nudge", "submitted")
    assert native_input["source_key"] == "inbox-nudge:" + attempt["id"] and 'type="inbox_nudge"' in text

    # A woken generation is never woken again, and waking neither changed the Task nor acknowledged anything.
    host.clock.advance(3600)
    host.run(3)
    assert count(runtime, "task_snooze_wakes") == 1 and count(runtime, "inbox_events", "event_type='snooze_expired'") == 1
    assert host.events() == [event] and len(host.attempts()) == 1 and len(host.typed()) == 1
    [row] = rows(runtime, "SELECT status FROM tasks WHERE id=?", (task,))
    assert row["status"] == "open"


def test_the_snooze_reconcile_pass_is_an_admitted_writer_a_pass_begun_before_refusal_settles_and_none_after(tmp_path):
    """The hook takes the actual write admission before its SQLite transaction: a pass admitted before shutdown began
    finishes its durable wake, and a pass begun afterwards runs no reconciliation and opens no transaction."""
    host = Host(tmp_path)
    native, runtime = host.native, host.native.runtime
    admission = runtime.application.writer_admission
    task = host.task("Follow up on the supplier")
    host.acknowledge(native.agent, *host.events())
    until = datetime.now(timezone.utc) + timedelta(seconds=1)
    [created] = rows(runtime, "SELECT revision FROM tasks WHERE id=?", (task,))
    snoozed = runtime.application.invoke(native.instance.human(), "tasks_update",
                                         {"task_id": task, "changes": {"snoozed_until": until.isoformat()}},
                                         InvocationOptions(idempotency_key=new_id(), expected_revision=created["revision"]))
    assert snoozed.status == "ok", snoozed.error
    host.acknowledge(native.agent, *host.events())
    while datetime.now(timezone.utc) <= until:
        time.sleep(0.05)

    real, calls = runtime.inbox_nudges.reconcile, []
    inside, proceed, outcome = threading.Event(), threading.Event(), {}

    def hook(uow):
        calls.append("reconcile")
        inside.set()
        assert proceed.wait(10)
        return real(uow)

    host.nudges.reconcile = hook
    pass_started = threading.Thread(target=lambda: outcome.setdefault("first", host.nudges._run_hook()))
    pass_started.start()
    assert inside.wait(10) and admission.admitted == 1                                # admitted, mid-transaction
    admission.begin_shutdown()                                                        # refusal begins; nothing is waited for
    transactions = []
    real_transaction = host.nudges.runtime.database.transaction
    host.nudges.runtime.database.transaction = lambda *a, **k: (transactions.append(k), real_transaction(*a, **k))[1]
    with pytest.raises(DomainError) as refused:                                       # a pass begun after refusal writes nothing
        host.nudges._run_hook()
    assert refused.value.code == "maintenance" and calls == ["reconcile"] and transactions == []

    proceed.set()                                                                     # the admitted pass settles its wake
    pass_started.join(10)
    assert not pass_started.is_alive() and admission.admitted == 0
    assert count(runtime, "task_snooze_wakes") == 1 and count(runtime, "inbox_events", "event_type='snooze_expired'") == 1
    host.nudges.runtime.database.transaction = real_transaction


@pytest.mark.parametrize('source', ['business_task', 'private_message'])
def test_status_uses_delivery_identity_and_rechecks_revoked_policy(tmp_path, monkeypatch, source):
    from host_foundation_support import set_agent_overrides
    host = Host(tmp_path)
    native = host.native
    if source == "business_task":
        host.task()
    else:
        host.private_message(native.agent)
    def no_bootstrap(*args, **kwargs):
        pytest.fail('Inbox status must not rebuild native bootstrap configuration')
    monkeypatch.setattr(native.runtime.resolver, 'invocation_context', no_bootstrap)
    assert host.nudges.status(native.agent)['state'] == 'pending'
    pending = host.events()
    set_agent_overrides(native.instance, native.agent, {'inbox_list': 'deny'}, [native.instance.business])
    assert host.nudges.status(native.agent)['state'] == 'idle'
    assert host.events() == pending  # Losing visibility never acknowledges work.
    set_agent_overrides(native.instance, native.agent, {}, [native.instance.business])
    assert host.nudges.status(native.agent)['state'] == 'pending'


def test_status_checks_repeated_sources_once_and_rechecks_access_on_the_next_read(tmp_path, monkeypatch):
    from host_foundation_support import set_agent_overrides
    host = Host(tmp_path)
    native = host.native
    host.task()
    native.runtime.freeze_registry()
    with native.runtime.database.transaction() as uow:
        event = uow.connection.execute(
            "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
            "WHERE d.agent_id=? AND e.source_type='tasks' LIMIT 1", (native.agent,)).fetchone()
        fields = ('business_id', 'source_type', 'source_id', 'source_revision', 'actor_id',
                  'event_type', 'summary', 'visibility_json', 'created_at')
        for _ in range(299):
            identity = uow.connection.execute(
                'INSERT INTO inbox_events(' + ','.join(fields) + ') VALUES(?,?,?,?,?,?,?,?,?)',
                tuple(event[field] for field in fields)).lastrowid
            uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (native.agent, identity))
    checks = []
    real_visible = native.runtime.inbox.event_visible

    def counted(*args, **kwargs):
        checks.append(args[1]['id'])
        return real_visible(*args, **kwargs)

    monkeypatch.setattr(native.runtime.inbox, 'event_visible', counted)
    result = host.nudges.status(native.agent)
    assert result['state'] == 'pending' and result['pending'] == 100
    assert len(checks) == 1  # One source, even though the first scan page holds 200 deliveries.
    pending = host.events()
    set_agent_overrides(native.instance, native.agent, {'tasks_get': 'deny'}, [native.instance.business])
    assert host.nudges.status(native.agent)['state'] == 'idle'
    assert host.events() == pending
    set_agent_overrides(native.instance, native.agent, {}, [native.instance.business])
    assert host.nudges.status(native.agent)['state'] == 'pending'
    assert len(checks) > 1


def test_status_keeps_deleted_source_kind_event_type_and_business_checks_separate(tmp_path):
    from host_foundation_support import set_agent_overrides
    host = Host(tmp_path)
    native = host.native
    set_agent_overrides(native.instance, native.agent,
                        {'assets_get': 'allow', 'notes_get': 'deny'}, [native.instance.business])
    event = {'id': 1, 'business_id': native.instance.business, 'source_type': 'documents',
             'source_id': new_id(), 'event_type': 'deleted', 'visibility_json': '{"kind":"asset"}'}
    events = [event,
              {**event, 'id': 2, 'visibility_json': '{"kind":"journal"}'},
              {**event, 'id': 3, 'event_type': 'updated'},
              {**event, 'id': 4, 'business_id': new_id()}]
    with native.runtime.database.read() as uow:
        assert host.visibility.authorized(uow, native.agent, events) == {1}
