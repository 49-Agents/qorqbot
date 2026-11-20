"""The owner's switch between managed sessions and terminals (``native_chat/runtime_switch.py``).

Everything outside the object is a stand-in: the managed runtime, the terminal backend, the cutover tool's survey and apply, the
clock. No process starts and no tmux runs. The mode file is the real one, in a temporary folder, so "the mode is written last" is
checked against what the stand-ins see when they are called.
"""
import isolated_cli_environment  # before any Qorqbot import

from contextlib import contextmanager
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import mode, options
from qorqut.managed_runtime.manager import HostUnverified
from qorqut.migration import managed_cutover as cutover
from qorqut.native_chat import runtime_switch
from qorqut.native_chat.runtime_switch import RuntimeSwitch

OWNER, STRANGER, AGENT_PRINCIPAL = "owner-1", "someone-2", "agent-3"
TMUX_OK = {"available": True, "reason": None, "detail": None, "path": "/usr/bin/tmux", "version": "3.4"}


def agent(agent_id, name, *, client="claude", on=True, resumable=True):
    return {"agent_id": agent_id, "name": name, "client": client, "on": on, "resumable": resumable}


class Connection:
    def execute(self, sql, *args):
        return SimpleNamespace(fetchone=lambda: ("owner-1",))


class Database:
    PRINCIPALS = {OWNER: {"kind": "human", "active": True}, STRANGER: {"kind": "human", "active": True},
                  AGENT_PRINCIPAL: {"kind": "agent", "active": True}}

    @contextmanager
    def read(self):
        yield SimpleNamespace(connection=Connection(), repository=lambda name: SimpleNamespace(get=self.PRINCIPALS.get))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Managed:
    """The managed runtime: hosts that close when asked, unless told to be stubborn."""

    def __init__(self, world):
        self.world, self.hosts, self.busy, self.unverified, self.stubborn = world, set(), set(), set(), {}

    def running_hosts(self):
        return [{"agent_id": item} for item in sorted(self.hosts)]

    def handle(self, agent_id):
        if agent_id in self.unverified:
            raise HostUnverified(agent_id)
        return object() if agent_id in self.hosts else None

    def snapshot(self, agent_id):
        return {"can_stop": agent_id in self.busy, "pending": []}

    @contextmanager
    def hold(self, agent_id):
        self.world.events.append(("hold", agent_id))
        yield

    def close(self, agent_id, how):
        self.world.events.append(("close", agent_id, how, self.world.mode()))
        if self.stubborn.get(agent_id) == "never" or (self.stubborn.get(agent_id) == "now" and how == "graceful"):
            return True
        self.hosts.discard(agent_id)
        return True


class Backend:
    def __init__(self, world):
        self.world, self.failing = world, {}

    def start_agent(self, agent_id):
        self.world.events.append(("start", agent_id, self.world.mode()))
        if agent_id in self.failing:
            raise DomainError("operation_failed", self.failing[agent_id])
        return {"agent_id": agent_id, "resumed": True}


class FakeTmux:
    def __init__(self, world):
        self.world = world

    def sessions(self):
        return [{"id": "$9", "name": "global__workspace"}] if self.world.shells else []

    def kill_session(self, session_id):
        if self.world.shell_failure:
            raise cutover.CutoverError("tmux_kill_failed", "no")
        self.world.events.append(("kill_shell", session_id))
        self.world.shells = False


class World:
    """One installation: its mode file, agents, hosts, terminals and the object under test."""

    def __init__(self, test, *, terminals=False, agents=None, tmux=TMUX_OK, spawn=None):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.paths = SimpleNamespace(runtime=root / "runtime", instance_root=root / "instance")
        self.events, self.agents = [], agents if agents is not None else [agent("a", "Atlas"), agent("m", "Mira")]
        self.tmux, self.clock, self.shells, self.shell_failure = dict(tmux), Clock(), False, False
        self.terminal_up = {"a": True, "m": True} if terminals else {}
        self.turns, self.problems, self.apply_fails, self.stopped = set(), {}, {}, []
        self.managed, self.backend = Managed(self), Backend(self)
        self.native = SimpleNamespace(locks=self.lock_of, backend=SimpleNamespace(wake=lambda: self.events.append(("wake",))),
                                      opener=mock.Mock())
        self.writer = SimpleNamespace(writer=self.writer_of)
        self.runtime = SimpleNamespace(paths=self.paths, database=Database(), native_chat=self.native, managed_runtime=self.managed,
                                       application=SimpleNamespace(ensure_writes_open=self.writes, writer_admission=self.writer))
        self.writes_error = None
        self.deferred = []
        self.bumped = []
        self.force_stopped = []
        self.trusted = True                    # the owner allowed Qorqbot to mark agent folders as trusted in Claude Code
        if terminals:
            mode.commit(self.paths, mode.TERMINAL)
        test.addCleanup(mode._cache.clear)
        test.addCleanup(mode._switching.clear)
        self.switch = self.make(spawn)

    def make(self, spawn=None):
        return RuntimeSwitch(self.runtime, managed=self.managed, backend=self.backend, agents=lambda: self.agents,
                             availability=lambda refresh=False: dict(self.tmux), survey=self.survey, apply=self.apply,
                             tmux_factory=lambda socket: FakeTmux(self), env={}, force_stop=self.force_stop, clock=self.clock,
                             sleep=self.clock.sleep, bump=self.bumped.append, spawn=spawn or (lambda work: work()),
                             trusted=lambda: self.trusted, grant_trust=self.grant_trust)

    def grant_trust(self):
        self.events.append(("trust", self.mode()))
        self.trusted = True

    def defer(self):
        """A switch whose worker runs only when ``run_deferred`` is called."""
        self.switch = self.make(self.deferred.append)

    def run_deferred(self):
        for work in self.deferred:
            work()

    @contextmanager
    def lock_of(self, agent_id):
        self.events.append(("lock", agent_id))
        yield

    @contextmanager
    def writer_of(self):
        self.events.append(("writer",))
        yield

    def writes(self, uow):
        if self.writes_error:
            raise DomainError("maintenance", "maintenance")

    def mode(self):
        return mode.current(self.paths)

    def set_hosts(self, *ids):
        self.managed.hosts = set(ids)

    # -- the cutover tool ------------------------------------------------------------------------------------------------
    def view(self, item):
        present = self.terminal_up.get(item["agent_id"], False)
        return {"agent_id": item["agent_id"], "name": item["name"], "session_present": present, "leftovers": [],
                "client": "claude" if present else "none", "turn": "running" if item["agent_id"] in self.turns else "idle",
                "problems": list(self.problems.get(item["agent_id"], [])), "other_processes": []}

    def survey(self):
        return {"agents": [self.view(item) for item in self.agents],
                "owner_shell_sessions": [{"socket": "qorqut", "session": "global__workspace"}] if self.shells else []}

    def apply(self, agent_id, force):
        self.events.append(("apply", agent_id, force, self.mode()))
        if agent_id in self.apply_fails:
            return {"stopped_at": {"agent_id": agent_id, "code": self.apply_fails[agent_id], "message": "x"}, "results": []}
        self.terminal_up[agent_id] = False
        return {"stopped_at": None, "results": [{"agent_id": agent_id, "status": "done"}]}

    def force_stop(self, agent_id):
        self.force_stopped.append(agent_id)
        self.events.append(("force_stop", agent_id))
        if agent_id not in self.stopped:
            self.managed.hosts.discard(agent_id)

    def kinds(self):
        return [event[0] for event in self.events]

    def refusal(self, test, *args, **kwargs):
        with test.assertRaises(DomainError) as caught:
            self.switch.start(*args, **kwargs)
        return caught.exception


class Refusals(unittest.TestCase):
    def test_only_the_owner_reads_or_switches(self):
        world = World(self)
        for who in (STRANGER, AGENT_PRINCIPAL, "nobody", None):
            with self.assertRaises(DomainError) as caught:
                world.switch.view(who)
            self.assertEqual((caught.exception.code, caught.exception.message), ("forbidden", "Only the owner can change how agents run."))
            self.assertEqual(world.refusal(self, who, "terminal").code, "forbidden")
        self.assertEqual((world.mode(), world.events), ("managed", []))

    def test_a_mode_that_is_neither_is_refused(self):
        world = World(self)
        self.assertEqual(world.refusal(self, OWNER, "both").code, "validation_error")
        self.assertEqual(world.refusal(self, OWNER, "terminal", force="yes").code, "validation_error")

    def test_no_tmux_is_said_in_the_owners_words(self):
        for reason in ("tmux_missing", "tmux_broken"):
            from qorqut import terminal_support
            world = World(self, tmux={"available": False, "reason": reason, "detail": terminal_support.REASONS[reason]})
            error = world.refusal(self, OWNER, "terminal")
            self.assertEqual((error.code, error.message), ("terminal_unavailable", terminal_support.REASONS[reason]))
            self.assertEqual((world.mode(), world.events), ("managed", []))
        self.assertEqual(terminal_support.REASONS["tmux_missing"], "Terminals need tmux, which is not installed on this computer.")

    def test_codex_agents_name_themselves(self):
        world = World(self, agents=[agent("a", "Atlas"), agent("m", "Mira", client="codex"), agent("d", "Dev", client="codex")])
        error = world.refusal(self, OWNER, "terminal")
        self.assertEqual(error.code, "codex_agents")
        self.assertEqual(error.message, "Mira and Dev use Codex. Only Claude Code agents can run in terminals. "
                                        "Set them to Claude Code under Client, model and effort first.")
        self.assertEqual([item["name"] for item in error.details["agents"]], ["Mira", "Dev"])
        one = World(self, agents=[agent("m", "Mira", client="codex")])
        self.assertTrue(one.refusal(self, OWNER, "terminal").message.startswith("Mira uses Codex. Only Claude Code agents"))
        self.assertEqual(world.events, [])

    def test_turns_running_refuse_unless_the_owner_cuts_them_off(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.managed.busy = {"a"}
        error = world.refusal(self, OWNER, "terminal")
        self.assertEqual(error.code, "turns_running")
        self.assertEqual(error.message, "Atlas is in the middle of a turn. Wait for it to finish, or switch now and cut the turn off.")
        self.assertEqual([item["agent_id"] for item in error.details["agents"]], ["a"])
        self.assertEqual((world.mode(), world.managed.hosts, mode.switching(world.paths)), ("managed", {"a", "m"}, False))
        world.managed.busy = {"a", "m"}
        self.assertTrue(world.refusal(self, OWNER, "terminal").message.startswith("Atlas and Mira are in the middle of a turn."))

    def test_force_goes_ahead_over_a_running_turn(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.busy = {"a"}
        result = world.switch.start(OWNER, "terminal", force=True)
        self.assertTrue(result["accepted"])
        self.assertEqual(world.mode(), "terminal")
        self.assertTrue(mode.document(world.paths)["last"]["ok"])

    def test_a_terminal_that_is_not_understood_refuses_before_anything_is_ended(self):
        world = World(self, terminals=True)
        world.problems["m"] = ["unrecognised_process_in_pane"]
        error = world.refusal(self, OWNER, "managed")
        self.assertEqual(error.code, "terminal_unclear")
        self.assertEqual(error.message, "Mira's terminal holds something Qorqbot does not recognise (for example a second program). "
                                        "Close it yourself, then try again.")
        self.assertEqual([item["name"] for item in error.details["agents"]], ["Mira"])
        self.assertEqual((world.mode(), world.events, world.terminal_up), ("terminal", [], {"a": True, "m": True}))

    def test_turns_in_terminals_refuse_unless_forced(self):
        world = World(self, terminals=True)
        world.turns = {"m"}
        self.assertEqual(world.refusal(self, OWNER, "managed").code, "turns_running")
        self.assertEqual(world.events, [])
        world.switch.start(OWNER, "managed", force=True)
        self.assertIn(("apply", "m", True, "terminal"), world.events)
        self.assertEqual(world.mode(), "managed")

    def test_a_session_that_cannot_be_checked_refuses_to_terminals(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.unverified = {"m"}
        error = world.refusal(self, OWNER, "terminal")
        self.assertEqual(error.code, "session_open")
        self.assertEqual(error.message, "Mira has a session that cannot be checked right now. Try again in a minute.")
        self.assertEqual(("close" in world.kinds(), world.mode()), (False, "managed"))

    def test_writes_closed_for_maintenance_refuse(self):
        world = World(self)
        world.writes_error = True
        error = world.refusal(self, OWNER, "terminal")
        self.assertEqual((error.code, error.retryable), ("operation_unavailable", True))
        self.assertEqual(error.message, "Qorqbot is busy with a backup or maintenance. Try again in a few minutes.")
        self.assertEqual((world.mode(), mode.switching(world.paths)), ("managed", False))

    def test_a_refusal_leaves_the_switch_free_for_the_next_request(self):
        world = World(self, tmux={"available": False, "reason": "tmux_missing", "detail": "x"})
        world.refusal(self, OWNER, "terminal")
        world.tmux = dict(TMUX_OK)
        self.assertTrue(world.switch.start(OWNER, "terminal")["accepted"])

    def test_already_that_mode_changes_nothing(self):
        world = World(self)
        result = world.switch.start(OWNER, "managed")
        self.assertIs(result["changed"], False)
        self.assertEqual((result["mode"], result["switching"]), ("managed", None))
        self.assertEqual((world.events, mode.document(world.paths)), ([], {}))
        terminals = World(self, terminals=True)
        self.assertIs(terminals.switch.start(OWNER, "terminal")["changed"], False)


class ToTerminals(unittest.TestCase):
    def test_sessions_end_first_the_mode_is_written_last_then_the_terminals_start(self):
        world = World(self, agents=[agent("a", "Atlas"), agent("m", "Mira"), agent("o", "Off", on=False), agent("n", "Nothing", resumable=False)])
        world.set_hosts("a", "m")
        result = world.switch.start(OWNER, "terminal")
        self.assertTrue(result["accepted"])
        self.assertEqual([event for event in world.events if event[0] in ("close", "start")], [
            ("close", "a", "graceful", "managed"), ("close", "m", "graceful", "managed"),
            ("start", "a", "terminal"), ("start", "m", "terminal")])
        self.assertEqual(world.managed.hosts, set())
        self.assertEqual((world.mode(), mode.switching(world.paths)), ("terminal", False))
        self.assertEqual(result["last"], {"at": result["last"]["at"], "to": "terminal", "ok": True, "message": "Agents now run in terminals."})
        self.assertEqual(world.bumped, [["a", "m", "n", "o"]])
        self.assertLess(world.kinds().index("close"), world.kinds().index("wake"))

    def test_each_agent_is_closed_under_its_own_locks_in_the_products_order(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.switch.start(OWNER, "terminal")
        closing = [event for event in world.events if event[0] in ("writer", "hold", "lock", "close")]
        self.assertEqual([event[0] for event in closing[:4]], ["writer", "hold", "lock", "close"])
        self.assertEqual([event[0] for event in closing[4:]], ["writer", "hold", "lock", "close"])

    def test_nothing_is_started_for_an_agent_that_is_off_has_nothing_to_resume_or_runs_elsewhere(self):
        world = World(self, agents=[agent("o", "Off", on=False), agent("n", "Nothing", resumable=False)])
        world.switch.start(OWNER, "terminal")
        self.assertNotIn("start", world.kinds())
        self.assertEqual(world.mode(), "terminal")

    def test_without_the_owners_trust_consent_no_terminal_is_started_and_the_owner_is_told(self):
        world = World(self)
        world.trusted = False
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual(world.mode(), "terminal")
        self.assertNotIn("start", world.kinds())
        self.assertNotIn("trust", world.kinds())                          # nothing is recorded that the owner did not allow
        self.assertEqual(result["last"]["message"], "Agents now run in terminals. Claude Code asks once whether to trust each "
                         "agent's folder, so no agent was started. Open each agent's Terminal and run claude there.")
        self.assertTrue(result["last"]["ok"])
        self.assertFalse(result["terminals"]["trusted"])

    def test_consent_given_with_the_switch_is_recorded_after_the_commit_and_before_any_start(self):
        world = World(self)
        world.trusted = False
        result = world.switch.start(OWNER, "terminal", trust=True)
        kinds = [event[0] for event in world.events if event[0] in ("trust", "start")]
        self.assertEqual(kinds, ["trust", "start", "start"])
        self.assertEqual([event for event in world.events if event[0] == "trust"], [("trust", "terminal")])
        self.assertEqual(result["last"]["message"], "Agents now run in terminals.")

    def test_consent_is_not_recorded_when_the_switch_is_refused_or_goes_to_managed(self):
        world = World(self)
        world.trusted = False
        world.set_hosts("a")
        world.managed.busy = {"a"}
        with self.assertRaises(DomainError):
            world.switch.start(OWNER, "terminal", trust=True)
        self.assertNotIn("trust", world.kinds())
        back = World(self, terminals=True)
        back.trusted = False
        back.switch.start(OWNER, "managed", trust=True)
        self.assertEqual(back.mode(), "managed")
        self.assertNotIn("trust", back.kinds())

    def test_a_consent_that_cannot_be_recorded_leaves_the_switch_done_and_starts_nothing(self):
        world = World(self)
        world.trusted = False
        world.grant_trust = mock.Mock(side_effect=OSError("read-only"))
        world.switch = world.make()
        result = world.switch.start(OWNER, "terminal", trust=True)
        self.assertEqual(world.mode(), "terminal")
        self.assertNotIn("start", world.kinds())
        self.assertIn("no agent was started", result["last"]["message"])

    def test_trust_must_be_true_or_false(self):
        world = World(self)
        with self.assertRaises(DomainError) as raised:
            world.switch.start(OWNER, "terminal", trust="yes")
        self.assertEqual(raised.exception.code, "validation_error")
        self.assertEqual(world.mode(), "managed")

    def test_a_stubborn_host_gets_the_second_and_third_step(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.stubborn = {"a": "now"}
        world.switch.start(OWNER, "terminal")
        self.assertEqual([event[1:3] for event in world.events if event[0] == "close"], [("a", "graceful"), ("a", "now")])
        self.assertEqual(world.mode(), "terminal")
        stuck = World(self)
        stuck.set_hosts("a")
        stuck.managed.stubborn = {"a": "never"}
        stuck.switch.start(OWNER, "terminal")
        self.assertEqual(stuck.force_stopped, ["a"])
        self.assertEqual(stuck.mode(), "terminal")

    def test_a_host_that_survives_everything_keeps_the_old_mode(self):
        world = World(self, agents=[agent("a", "Atlas"), agent("m", "Mira")])
        world.set_hosts("a", "m")
        world.managed.stubborn = {"m": "never"}
        world.stopped = ["m"]
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual(world.mode(), "managed")
        self.assertEqual(world.managed.hosts, {"m"})                       # Atlas stays closed
        self.assertNotIn("start", world.kinds())
        self.assertEqual(result["last"]["ok"], False)
        self.assertEqual(result["last"]["message"], "Nothing was switched. Mira has a session that did not close. "
                                                    "Some agents were closed; they start again on their next message.")
        self.assertEqual((mode.switching(world.paths), result["switching"], mode.document(world.paths).get("pending")), (False, None, None))
        self.assertEqual(world.bumped, [])
        world.managed.stubborn = {}
        again = world.switch.start(OWNER, "terminal")                      # pressing it again finishes it
        self.assertEqual((again["accepted"], world.mode()), (True, "terminal"))

    def test_nothing_closed_yet_does_not_say_so(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.stubborn = {"a": "never"}
        world.stopped = ["a"]
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual(result["last"]["message"], "Nothing was switched. Atlas has a session that did not close.")

    def test_a_failure_while_closing_is_a_plain_sentence_and_the_mode_stays(self):
        world = World(self)
        world.set_hosts("a")

        def refuse(agent_id, how):
            raise DomainError("session_opening", "A session is being opened for this agent; try again in a moment")
        world.managed.close = refuse
        world.managed.stubborn = {"a": "never"}
        world.stopped = ["a"]
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual(world.mode(), "managed")
        self.assertIn("Nothing was switched.", result["last"]["message"])
        self.assertFalse(result["last"]["ok"])

    def test_an_unexpected_error_before_the_commit_keeps_the_mode_and_opens_the_gate(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.close = mock.Mock(side_effect=RuntimeError("boom"))
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual((world.mode(), mode.switching(world.paths)), ("managed", False))
        self.assertEqual(result["last"]["message"], "Nothing was switched. Qorqbot ran into an unexpected problem.")
        self.assertTrue(world.switch._lock.acquire(blocking=False))      # the lock is free again
        world.switch._lock.release()

    def test_a_terminal_that_does_not_start_is_said_and_the_switch_stands(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.backend.failing = {"a": "Terminal is not an idle shell"}
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual(world.mode(), "terminal")
        self.assertEqual(world.kinds().count("start"), 2)                  # Mira was still tried
        self.assertEqual(result["last"], {"at": result["last"]["at"], "to": "terminal", "ok": True,
                                          "message": "Agents now run in terminals. Atlas could not be started; start it from its Terminal."})
        world2 = World(self)
        world2.set_hosts("a", "m")
        world2.backend.failing = {"a": "x", "m": "y"}
        message = world2.switch.start(OWNER, "terminal")["last"]["message"]
        self.assertEqual(message, "Agents now run in terminals. Atlas and Mira could not be started; start them from their Terminals.")

    def test_an_error_in_the_starting_never_undoes_the_switch(self):
        world = World(self)
        world.set_hosts("a")
        world.backend.start_agent = mock.Mock(side_effect=RuntimeError("boom"))
        result = world.switch.start(OWNER, "terminal")
        self.assertEqual((world.mode(), result["last"]["ok"]), ("terminal", True))
        self.assertIn("could not be started", result["last"]["message"])

    def test_stored_model_and_effort_are_not_touched(self):
        world = World(self)
        options.write(world.paths, "a", client="claude", model="m1", effort="high")
        world.set_hosts("a")
        with mock.patch.object(options, "write") as written:
            world.switch.start(OWNER, "terminal")
        written.assert_not_called()
        self.assertEqual(options.read(world.paths, "a").public(), {"client": "claude", "model": "m1", "effort": "high"})

    def test_it_never_turns_an_agent_off(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.switch.start(OWNER, "terminal")
        world.native.opener.turn_off.assert_not_called()
        world.native.opener.set_desired.assert_not_called()


class ToManaged(unittest.TestCase):
    def test_terminals_end_one_agent_at_a_time_the_mode_is_written_last_and_nothing_starts(self):
        world = World(self, terminals=True)
        world.shells = True
        result = world.switch.start(OWNER, "managed")
        self.assertTrue(result["accepted"])
        self.assertEqual([event for event in world.events if event[0] in ("apply", "kill_shell", "start")], [
            ("apply", "a", False, "terminal"), ("apply", "m", False, "terminal"), ("kill_shell", "$9")])
        self.assertEqual(world.terminal_up, {"a": False, "m": False})
        self.assertEqual((world.mode(), mode.switching(world.paths)), ("managed", False))
        self.assertEqual(result["last"]["message"], "Agents now run as managed sessions.")
        self.assertNotIn("start", world.kinds())
        self.assertEqual(world.bumped, [["a", "m"]])
        world.native.opener.turn_off.assert_not_called()

    def test_each_agent_is_ended_under_its_own_locks(self):
        world = World(self, terminals=True)
        world.switch.start(OWNER, "managed")
        order = [event[0] for event in world.events if event[0] in ("writer", "hold", "lock", "apply")]
        self.assertEqual(order, ["writer", "hold", "lock", "apply"] * 2)

    def test_a_failure_at_the_second_agent_keeps_terminals_and_leaves_the_first_ended(self):
        world = World(self, terminals=True)
        world.apply_fails = {"m": "client_did_not_end"}
        result = world.switch.start(OWNER, "managed")
        self.assertEqual(world.mode(), "terminal")
        self.assertEqual(world.terminal_up, {"a": False, "m": True})
        self.assertEqual(result["last"]["message"], "Nothing was switched. Mira could not be ended: it did not stop when asked. "
                                                    "Terminals that were already ended stay ended; switch again to finish.")
        self.assertFalse(result["last"]["ok"])
        self.assertEqual((mode.switching(world.paths), result["switching"]), (False, None))
        world.apply_fails = {}
        again = world.switch.start(OWNER, "managed")
        self.assertEqual((again["accepted"], world.mode()), (True, "managed"))
        self.assertEqual([event[1] for event in world.events if event[0] == "apply"], ["a", "m", "m"])   # Atlas has nothing left

    def test_a_turn_that_began_since_the_check_stops_the_switch_at_that_agent(self):
        world = World(self, terminals=True)
        world.apply_fails = {"a": "turn_in_progress"}
        result = world.switch.start(OWNER, "managed")
        self.assertEqual(result["last"]["message"], "Nothing was switched. Atlas could not be ended: it is in the middle of a turn.")
        self.assertEqual(world.mode(), "terminal")

    def test_a_workspace_shell_that_cannot_be_ended_keeps_terminals(self):
        world = World(self, terminals=True)
        world.shells, world.shell_failure = True, True
        result = world.switch.start(OWNER, "managed")
        self.assertEqual(world.mode(), "terminal")
        self.assertIn("Your Workspace terminal could not be ended.", result["last"]["message"])

    def test_a_terminal_that_is_still_there_after_the_tool_keeps_terminals(self):
        world = World(self, terminals=True)
        world.switch._apply_seam = lambda agent_id, force: {"stopped_at": None, "results": []}
        result = world.switch.start(OWNER, "managed")
        self.assertEqual(world.mode(), "terminal")
        self.assertIn("still have terminals", result["last"]["message"])

    def test_without_tmux_there_is_nothing_to_end_and_the_mode_is_written_at_once(self):
        world = World(self, terminals=True, tmux={"available": False, "reason": "tmux_missing", "detail": "x"})
        result = world.switch.start(OWNER, "managed")
        self.assertEqual(world.mode(), "managed")
        self.assertEqual([event for event in world.events if event[0] == "apply"], [])
        self.assertTrue(result["last"]["ok"])

    def test_the_real_cutover_tool_is_called_for_each_agent_without_the_instance_lock(self):
        world = World(self, terminals=True)
        world.apply = None
        world.switch = RuntimeSwitch(world.runtime, managed=world.managed, backend=world.backend, agents=lambda: world.agents,
                                     availability=lambda refresh=False: dict(TMUX_OK), survey=world.survey, env={},
                                     tmux_factory=lambda socket: FakeTmux(world), clock=world.clock, sleep=world.clock.sleep,
                                     bump=world.bumped.append, spawn=lambda work: work())
        calls = []

        def fake(paths, env, **kwargs):
            calls.append(kwargs)
            world.terminal_up[kwargs["only"]] = False
            return {"stopped_at": None, "results": []}
        with mock.patch.object(cutover, "run_apply", fake):
            world.switch.start(OWNER, "managed", force=True)
        self.assertEqual([(item["only"], item["lock"], item["switching"], item["force"], item["record"]) for item in calls],
                         [("a", False, True, True, False), ("m", False, True, True, False)])
        self.assertEqual(world.mode(), "managed")


class OneAtATime(unittest.TestCase):
    def test_a_second_request_while_one_runs_gets_the_progress(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.defer()
        first = world.switch.start(OWNER, "terminal")
        self.assertEqual(first["switching"], {"to": "terminal", "step": "ending", "done": 0, "total": 2, "current": None})
        for target in ("terminal", "managed"):
            error = world.refusal(self, OWNER, target)
            self.assertEqual((error.code, error.retryable, error.message), ("switch_running", True, runtime_switch.SWITCH_RUNNING))
            self.assertEqual(error.details["switching"]["to"], "terminal")
        self.assertEqual(world.events, [])
        self.assertTrue(mode.switching(world.paths))
        self.assertEqual(mode.document(world.paths)["pending"]["to"], "terminal")
        world.run_deferred()
        self.assertEqual(world.mode(), "terminal")
        self.assertIs(world.switch.start(OWNER, "terminal")["changed"], False)

    def test_another_process_holding_the_file_lock_is_a_running_switch_too(self):
        world = World(self)
        world.defer()
        world.switch.start(OWNER, "terminal")
        other = World.__new__(World)
        other.__dict__.update(world.__dict__)
        other.switch = other.make(lambda work: None)                      # a second object over the same folder, as a second process
        with self.assertRaises(DomainError) as caught:
            other.switch.start(OWNER, "terminal")
        self.assertEqual(caught.exception.code, "switch_running")
        world.run_deferred()

    def test_the_file_lock_is_released_when_the_switch_ends(self):
        world = World(self)
        world.switch.start(OWNER, "terminal")
        other = world.make()
        self.assertTrue(other.start(OWNER, "managed")["accepted"])

    def test_a_worker_that_cannot_start_frees_everything(self):
        world = World(self)

        def refuse(work):
            raise RuntimeError("no threads")
        world.switch = world.make(refuse)
        with self.assertRaises(RuntimeError):
            world.switch.start(OWNER, "terminal")
        self.assertEqual((world.mode(), mode.switching(world.paths)), ("managed", False))
        world.switch = world.make()
        self.assertTrue(world.switch.start(OWNER, "terminal")["accepted"])

    def test_the_real_thread_runs_the_switch(self):
        world = World(self)
        done = threading.Event()
        world.switch = world.make(lambda work: threading.Thread(target=work).start())
        real = world.switch._finish

        def finish(*args, **kwargs):
            real(*args, **kwargs)
            done.set()
        with mock.patch.object(world.switch, "_finish", finish):
            self.assertTrue(world.switch.start(OWNER, "terminal")["accepted"])
            self.assertTrue(done.wait(5))
        self.assertEqual(world.mode(), "terminal")


class Progress(unittest.TestCase):
    def test_done_and_total_count_the_agents_handled(self):
        world = World(self)
        world.set_hosts("a", "m")
        seen = []
        original = world.managed.close

        def close(agent_id, how):
            seen.append(world.switch.view(OWNER)["switching"])
            return original(agent_id, how)
        world.managed.close = close
        world.backend.start_agent = lambda agent_id: seen.append(world.switch.view(OWNER)["switching"])
        world.switch.start(OWNER, "terminal")
        self.assertEqual([(item["step"], item["done"], item["total"], item["current"]) for item in seen], [
            ("ending", 0, 2, "Atlas"), ("ending", 1, 2, "Mira"), ("starting", 0, 2, "Atlas"), ("starting", 1, 2, "Mira")])
        self.assertTrue(all(item["to"] == "terminal" for item in seen))
        self.assertIsNone(world.switch.view(OWNER)["switching"])

    def test_the_terminals_direction_counts_too(self):
        world = World(self, terminals=True)
        seen = []
        original = world.apply

        def apply(agent_id, force):
            seen.append(world.switch.view(OWNER)["switching"])
            return original(agent_id, force)
        world.switch = world.make()
        world.switch._apply_seam = apply
        world.switch.start(OWNER, "managed")
        self.assertEqual([(item["to"], item["step"], item["done"], item["total"]) for item in seen],
                         [("managed", "ending", 0, 2), ("managed", "ending", 1, 2)])

    def test_the_view_while_switching_does_not_walk_the_agents_again(self):
        world = World(self)
        world.defer()
        world.switch.start(OWNER, "terminal")
        calls = []
        world.switch._agents_seam = lambda: calls.append(1) or []
        for _ in range(3):
            world.switch.view(OWNER)
        self.assertEqual(calls, [])


class Views(unittest.TestCase):
    def test_a_managed_installation_at_rest(self):
        world = World(self)
        view = world.switch.view(OWNER)
        self.assertEqual(view, {"mode": "managed", "recommended": "managed", "busy": [], "switching": None, "last": None,
                                "terminals": {"available": True, "reason": None, "detail": None, "agents": [], "trusted": True}})
        world.trusted = False
        self.assertFalse(world.switch.view(OWNER)["terminals"]["trusted"])

    def test_a_terminals_installation_at_rest(self):
        world = World(self, terminals=True)
        view = world.switch.view(OWNER)
        self.assertEqual((view["mode"], view["recommended"], view["busy"], view["switching"]), ("terminal", "managed", [], None))
        self.assertEqual(view["last"]["to"], "terminal")

    def test_busy_names_the_agents_in_the_middle_of_a_turn_in_either_mode(self):
        world = World(self)
        world.set_hosts("a", "m")
        world.managed.busy = {"m"}
        self.assertEqual(world.switch.view(OWNER)["busy"], [{"agent_id": "m", "name": "Mira"}])
        terminals = World(self, terminals=True)
        terminals.turns = {"a"}
        self.assertEqual(terminals.switch.view(OWNER)["busy"], [{"agent_id": "a", "name": "Atlas"}])

    def test_busy_is_read_at_most_every_two_seconds(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.snapshot = mock.Mock(return_value={"can_stop": False, "pending": []})
        for _ in range(3):
            world.switch.view(OWNER)
        self.assertEqual(world.managed.snapshot.call_count, 1)
        world.clock.now += 3
        world.switch.view(OWNER)
        self.assertEqual(world.managed.snapshot.call_count, 2)

    def test_a_question_waiting_counts_as_busy(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.snapshot = lambda agent_id: {"can_stop": False, "pending": [{"request_id": "r"}]}
        self.assertEqual(world.switch.view(OWNER)["busy"], [{"agent_id": "a", "name": "Atlas"}])

    def test_no_tmux_and_codex_are_reasons_the_owner_can_read(self):
        from qorqut import terminal_support
        world = World(self, tmux={"available": False, "reason": "tmux_broken", "detail": terminal_support.REASONS["tmux_broken"]})
        self.assertEqual(world.switch.view(OWNER)["terminals"], {"available": False, "reason": "tmux_broken",
                                                                  "detail": terminal_support.REASONS["tmux_broken"], "agents": [],
                                                                  "trusted": True})
        codex = World(self, agents=[agent("a", "Atlas"), agent("m", "Mira", client="codex")])
        shown = codex.switch.view(OWNER)["terminals"]
        self.assertEqual((shown["available"], shown["reason"], shown["agents"]), (False, "codex_agents", [{"agent_id": "m", "name": "Mira"}]))
        self.assertTrue(shown["detail"].startswith("Mira uses Codex."))

    def test_the_last_switch_is_kept_after_a_failed_one(self):
        world = World(self)
        world.set_hosts("a")
        world.managed.stubborn = {"a": "never"}
        world.stopped = ["a"]
        world.switch.start(OWNER, "terminal")
        last = world.switch.view(OWNER)["last"]
        self.assertEqual((last["to"], last["ok"]), ("terminal", False))
        self.assertTrue(last["message"].startswith("Nothing was switched."))
        self.assertTrue(mode.document(world.paths)["last"]["ok"] is False)

    def test_the_view_says_what_was_asked_in_the_start_answer(self):
        world = World(self)
        world.defer()
        answer = world.switch.start(OWNER, "terminal")
        self.assertEqual((answer["accepted"], answer["mode"], answer["switching"]["to"]), (True, "managed", "terminal"))
        world.run_deferred()


class DefaultAgents(unittest.TestCase):
    """The default reading of the agents (no ``agents`` seam): profiles, effective client, desired state and selection."""

    def test_it_reads_the_client_the_state_and_the_conversation(self):
        world = World(self)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE native_session_owners(agent_id TEXT PRIMARY KEY, consumer_id TEXT, binding_json TEXT, generation INTEGER, desired_state TEXT);
            INSERT INTO native_session_owners VALUES('a','conv-a','{"runtime":"claude-code"}',1,'running');
            INSERT INTO native_session_owners VALUES('m','conv-m','{"runtime":"codex"}',1,'stopped');""")
        profiles = [{"agent_id": item, "name": item, "profile_json": json.dumps({"display_name": label})}
                    for item, label in (("a", "Atlas"), ("m", "Mira"), ("n", "New"))]
        for item, client in (("a", "claude"), ("m", "codex"), ("n", "claude")):
            options.write(world.paths, item, client=client)

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        world.runtime.database = SimpleNamespace(read=read)
        switch = RuntimeSwitch(world.runtime)
        with mock.patch("qorqut.native_chat.store.observed_agents", return_value=profiles):
            found = switch._agents()
        self.assertEqual([(item["name"], item["client"], item["on"], item["resumable"]) for item in found],
                         [("Atlas", "claude", True, True), ("Mira", "codex", False, False), ("New", "claude", True, False)])


class Recovery(unittest.TestCase):
    def test_a_journal_a_dead_process_left_is_dropped_and_the_mode_stands(self):
        world = World(self, terminals=True)
        mode.begin(world.paths, "managed")
        self.assertTrue(mode.switching(world.paths))
        dropped = world.switch.recover()
        self.assertEqual(dropped["to"], "managed")
        self.assertEqual((world.mode(), mode.switching(world.paths), mode.document(world.paths).get("pending")), ("terminal", False, None))

    def test_nothing_to_recover_is_none(self):
        world = World(self)
        self.assertIsNone(world.switch.recover())
        self.assertEqual(mode.document(world.paths), {})


class NoAgentCanSwitch(unittest.TestCase):
    def test_the_switch_is_not_an_operation_or_a_tool(self):
        import inspect
        source = inspect.getsource(runtime_switch)
        for forbidden in ("register(", "@operation", "mcp"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
