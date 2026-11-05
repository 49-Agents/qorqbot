"""Every producer of native input goes through the one arbiter; the remaining low-level transports are named and justified.

Synthetic tmux/runtime fakes only. Nothing launches an agent, a terminal client or a service."""

from pathlib import Path
import importlib.util
import re
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError

import bazarlyk
from bazarlyk.agents.terminal_control import execution_outcome
from bazarlyk.native_chat.contracts import RESULT_DEFERRED, STATE_FAILED, STATE_SUBMITTED, STATE_UNCERTAIN
from bazarlyk.telegram.owner_input import OwnerInputs
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend
from bazarlyk.transport.terminals import TerminalService
from native_chat_support import Native, rows

SOURCE = Path(bazarlyk.__file__).parent

# The complete inventory of code that writes into a native client's input. Anything new here must go through the arbiter.
# (``sandbox/tmux.py`` defines the broker transport behind ``broker.submit_input`` and ``queue_notice``; neither has a caller of
# its own. Inbox notices and the recovery wake are system inputs of the arbiter: ``native_chat/nudges.py``.)
RETAINED = {
    "transport/agent_terminal_control.py": "the verified composer transport the arbiter's terminal adapter calls, and the manual "
                                           "lifecycle launcher typed into an idle shell",
    "transport/terminals.py": "broadcast delivery to a sandbox or remote broker target (host-native targets use the arbiter)",
}
LOW_LEVEL = re.compile(r"\.owner_input\(|\.service_notice\(|\.submit_input\(|\.queue_notice\(|\[\s*['\"]codex['\"]\s*,\s*['\"]queue['\"]")


def test_the_input_producer_inventory_is_exactly_the_retained_set():
    found = {}
    for path in SOURCE.rglob("*.py"):
        relative = path.relative_to(SOURCE).as_posix()
        if relative.startswith("native_chat/"):
            continue
        if LOW_LEVEL.search(path.read_text()):
            found[relative] = True
    assert set(found) == set(RETAINED), sorted(set(found) ^ set(RETAINED))


def test_the_retired_independent_chat_runner_is_gone():
    assert importlib.util.find_spec("bazarlyk.global_chat") is None
    offenders = [p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob("*.py")
                 if re.search(r"QORQUT_GLOBAL_CHAT|global_chat|runtime\.chat\(|\.handles\(", p.read_text())
                 and p.name != "schema_ddl.py"]
    assert offenders == []
    assert not hasattr(AgentTerminalBackend, "chat")


# ----- broadcasts -------------------------------------------------------------------------------------------------------

class FakeTerminals:
    """The part of ``Terminals`` the broadcast port uses, over a real runtime."""

    def __init__(self, native, *, sandbox=False):
        self.runtime, self.broker = native.runtime, object()
        self.sandbox, self.calls = sandbox, []
        self.execution = lambda business: SimpleNamespace(target_identity="sandbox" if sandbox else None, broker=self.broker)
        self.binding = lambda *args: ("key", "session", "cwd")
        self.broker = SimpleNamespace(tmux_available=lambda: True, get=lambda key: SimpleNamespace(done=False),
                                      session_alive=lambda session: True, submit_input=lambda s, d: self.calls.append((s, d)))
        self.execution = lambda business: SimpleNamespace(target_identity="sandbox" if sandbox else None, broker=self.broker)

    _native_input = TerminalService._native_input
    input_existing = TerminalService.input_existing


def test_a_broadcast_to_a_host_native_agent_goes_through_the_arbiter_once(tmp_path):
    native = Native(tmp_path)
    native.publish()
    terminals = FakeTerminals(native)
    first = terminals.input_existing(None, native.agent, "Weekly note", key="broadcast:b1:1:r1")
    assert first["state"] == "delivered" and native.pane.typed() == ["Weekly note"] and terminals.calls == []
    again = terminals.input_existing(None, native.agent, "Weekly note", key="broadcast:b1:1:r1")
    assert again["state"] == "delivered" and native.pane.typed() == ["Weekly note"]
    assert rows(native.runtime, "SELECT input_type,origin,source_key,state FROM native_inputs") == [
        {"input_type": "broadcast", "origin": "owner", "source_key": "broadcast:b1:1:r1", "state": "submitted"}]


def test_a_broadcast_to_an_offline_or_busy_native_agent_is_unavailable_and_types_nothing(tmp_path):
    native = Native(tmp_path)
    native.publish()
    terminals = FakeTerminals(native)
    native.pane.busy = True
    assert terminals.input_existing(None, native.agent, "Later", key="broadcast:b2:1:r1")["state"] == "unavailable"
    native.pane.busy = False
    native.end()
    assert terminals.input_existing(None, native.agent, "Later", key="broadcast:b3:1:r1")["state"] == "unavailable"
    assert native.pane.typed() == [] and terminals.calls == []


def test_an_uncertain_native_write_is_reported_uncertain_to_the_broadcast_dispatcher(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.pane.enter_works = False
    result = FakeTerminals(native).input_existing(None, native.agent, "Maybe", key="broadcast:b4:1:r1")
    assert result["state"] == "uncertain"


def test_a_sandbox_target_keeps_its_broker_transport(tmp_path):
    native = Native(tmp_path)
    native.publish()
    terminals = FakeTerminals(native, sandbox=True)
    assert terminals.input_existing(None, native.agent, "Sandbox note", key="broadcast:b5:1:r1") == {"state": "delivered"}
    assert terminals.calls == [("session", "Sandbox note")] and native.pane.typed() == []
    assert rows(native.runtime, "SELECT COUNT(*) AS n FROM native_inputs") == [{"n": 0}]


# ----- terminal-control direct messages -----------------------------------------------------------------------------

def profile_row(native):
    with native.runtime.database.read() as uow:
        return dict(uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (native.agent,)).fetchone())


def test_a_direct_message_ping_is_a_direct_message_input_and_a_refusal_is_not_delivered(tmp_path):
    native = Native(tmp_path)
    native.publish()
    row, backend = profile_row(native), native.terminal_backend
    expected = backend.snapshot(row)
    note = backend.execute(row, "ping", "A colleague asks", expected, input_key="terminal-action:a1")
    assert note.startswith("Direct message submitted") and native.pane.typed() == ["A colleague asks"]
    assert rows(native.runtime, "SELECT input_type,source_key,state FROM native_inputs") == [
        {"input_type": "direct_message", "source_key": "terminal-action:a1", "state": "submitted"}]
    native.pane.busy = True
    with pytest.raises(DomainError) as refused:
        backend.execute(row, "ping", "Not now", expected, input_key="terminal-action:a2")
    assert refused.value.code == "not_delivered" and native.pane.typed() == ["A colleague asks"]
    assert execution_outcome(refused.value)[0] == "failed"
    native.pane.busy, native.pane.enter_works = False, False
    with pytest.raises(DomainError) as unknown:
        backend.execute(row, "ping", "Unclear", expected, input_key="terminal-action:a3")
    assert unknown.value.code != "not_delivered" and execution_outcome(unknown.value)[0] == "uncertain"
    assert execution_outcome(RuntimeError("boom"))[0] == "uncertain"


def test_the_terminal_backend_has_no_chat_managed_branch(tmp_path):
    native = Native(tmp_path)
    native.publish()
    snapshot = native.terminal_backend.snapshot(profile_row(native))
    assert "chat" not in snapshot and not hasattr(native.terminal_backend, "chat")


# ----- Telegram owner input -----------------------------------------------------------------------------------------

def result(state, reason, **evidence):
    return SimpleNamespace(state=state, reason=reason, evidence=evidence)


def test_telegram_maps_the_arbiter_result_to_its_own_outcomes():
    convert = OwnerInputs._outcome
    assert convert(result(STATE_SUBMITTED, "processing_not_confirmed", adapter="terminal")).status == "submitted"
    assert convert(result(STATE_UNCERTAIN, "submit_unconfirmed")).status == "uncertain"
    assert convert(result(STATE_FAILED, "paste_failed")).status == "failed"
    deferred = convert(result(RESULT_DEFERRED, "composer_busy"))
    assert deferred.status == "deferred" and deferred.reason == "composer_busy"
    assert convert(result("rejected", "unsupported_target")).status == "failed"
