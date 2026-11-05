"""Guarded owner input through the terminal adapter: scripted panes, plus a real tmux server on an isolated socket."""
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend, runtime_client
from test_terminal_composer import CLAUDE_EMPTY, CODEX_EMPTY, WORDING, claude, codex, talk

ROW = {"agent_id": "a1", "home_business_id": None, "profile_json": json.dumps({"runtime": "codex", "role": {"id": "ordubashy"}})}
CLAUDE_ROW = {**ROW, "profile_json": json.dumps({"runtime": "claude-code", "role": {"id": "founder"}})}
SNAP = {"state": "present", "command": "codex", "client": "codex", "pane_id": "%3", "session_id": "$1", "foreground_pid": 10,
        "foreground_started": "1", "server_pid": "9", "pane_pid": "8"}
TYPED = codex("› [Pasted text #1 +3 lines]")
CURSOR = (2, 4, True)
TEXT = "Qorqut owner message via Telegram (correlation c1).\n\nplease look at the build"


class Pane:
    """A scripted tmux: one ``(screen, cursor)`` per observation (the last repeats), recording every input."""

    def __init__(self, script, *, fail=()):
        self.script, self.fail, self.calls, self.loaded = list(script), set(fail), [], None
        self.index = 0

    def current(self):
        return self.script[min(self.index, len(self.script) - 1)]

    def tmux(self, *args):
        self.calls.append(args)
        name = args[0]
        if name in self.fail:
            return 1, "failed"
        if name == "display-message":
            _, (x, y, visible) = self.current()
            return 0, f"0|0|{int(visible)}|{x}|{y}"
        if name == "capture-pane":
            screen, _ = self.current()
            self.index += 1
            return 0, screen
        if name == "load-buffer":
            path = Path(args[-1])
            self.loaded = (path.read_text(), stat.S_IMODE(path.stat().st_mode))
        return 0, ""

    def inputs(self):
        return [call for call in self.calls if call[0] in ("load-buffer", "paste-buffer", "send-keys")]


def backend_for(pane, snapshots=None, *, row=ROW):
    backend = AgentTerminalBackend(SimpleNamespace(runtime=None), sleep=lambda seconds: None)
    queue = list(snapshots) if snapshots is not None else None
    backend.snapshot = lambda r, client=None: dict(queue.pop(0) if queue and len(queue) > 1 else queue[0]) if queue else dict(SNAP)
    backend.selected = lambda r: (pane, "key", "session", "cwd", None)
    return backend


def private(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def deliver(backend, tmp_path, checkpoint=None, row=ROW, text=TEXT):
    calls = []
    outcome = backend.owner_input(row, text, workdir=private(tmp_path),
                                  checkpoint=checkpoint or (lambda: calls.append("checkpoint")))
    return outcome, calls


def test_an_idle_composer_receives_one_guarded_paste_then_one_submit(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True)), (CODEX_EMPTY, CURSOR)])
    outcome, calls = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed")
    assert calls == ["checkpoint"]
    names = [call[0] for call in pane.inputs()]
    assert names == ["load-buffer", "paste-buffer", "send-keys"]
    # The text travels through a private file, never argv, and the paste is bracketed and addressed to the observed pane.
    assert pane.loaded == (TEXT, 0o600)
    paste = next(call for call in pane.calls if call[0] == "paste-buffer")
    assert paste[1:4] == ("-d", "-r", "-p") and paste[-2:] == ("-t", "%3")
    assert TEXT not in json.dumps(pane.calls)
    assert next(call for call in pane.calls if call[0] == "send-keys") == ("send-keys", "-t", "%3", "Enter")
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.parametrize("screen,reason", [
    (codex(f"› x", above=""), "composer_not_empty"),
    (codex("› ", above="• Working (3s • esc to interrupt)"), "composer_busy"),
    ("  Would you like to run the following command?\n› 1. Yes, proceed (y)\n", "composer_modal"),
    ("user@host:~$ \n", "composer_unrecognized"),
])
def test_a_composer_that_is_not_verifiably_empty_queues_without_any_input(tmp_path, screen, reason):
    pane = Pane([(screen, CURSOR)])
    outcome, calls = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", reason)
    assert calls == [] and pane.inputs() == []


@pytest.mark.parametrize("snapshot,reason", [
    ({"state": "absent", "client": "codex"}, "terminal_absent"),
    ({**SNAP, "command": "bash"}, "terminal_unverified"),
    ({**SNAP, "command": "unrecognized"}, "terminal_unverified"),
    ({**SNAP, "command": "claude"}, "terminal_unverified"),
])
def test_only_the_verified_client_is_ever_typed_into(tmp_path, snapshot, reason):
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    outcome, calls = deliver(backend_for(pane, [snapshot]), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", reason)
    assert pane.calls == [] and calls == []


def test_snapshot_errors_and_unsupported_execution_never_type(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    backend = backend_for(pane)

    def unverified(row, client=None):
        raise DomainError("operation_unavailable", "Cannot identify the foreground terminal process")

    backend.snapshot = unverified
    assert backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: None).reason == "terminal_unverified"

    def unsupported(row):
        raise DomainError("operation_unavailable", "Terminal control currently requires native host execution")

    backend.selected = unsupported
    assert backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: None).reason == "terminal_unsupported"
    backend.selected = lambda row: (_ for _ in ()).throw(DomainError("operation_unavailable", "Host tmux is unavailable"))
    assert backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: None).reason == "terminal_unavailable"


def test_copy_mode_and_dead_panes_are_not_typed_into(tmp_path):
    class InMode(Pane):
        def tmux(self, *args):
            if args[0] == "display-message":
                self.calls.append(args)
                return 0, "1|0|1|2|4"
            return super().tmux(*args)

    pane = InMode([(CODEX_EMPTY, CURSOR)])
    outcome, calls = deliver(backend_for(pane), tmp_path)
    assert outcome.reason == "terminal_in_mode" and pane.inputs() == [] and calls == []


def test_a_process_that_changes_between_the_two_checks_is_not_typed_into(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    swapped = [SNAP, {**SNAP, "foreground_pid": 11}]
    outcome, _ = deliver(backend_for(pane, swapped), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "screen_changing") and pane.inputs() == []


@pytest.mark.parametrize("screen,reason", [
    (codex("› x"), "composer_not_empty"),
    (codex("› ", above="• Working (3s • esc to interrupt)"), "composer_busy"),
    ("  Would you like to run the following command?\n› 1. Yes, proceed (y)\n", "composer_modal"),
    ("user@host:~$ \n", "composer_unrecognized"),
])
def test_a_composer_that_stops_being_ready_between_the_checks_is_not_typed_into(tmp_path, screen, reason):
    pane = Pane([(CODEX_EMPTY, CURSOR), (screen, CURSOR)])
    outcome, calls = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", reason) and pane.inputs() == [] and calls == []


def ticking(client, n):
    """The same idle, empty composer while unrelated output (a transcript timer and a footer counter) keeps changing."""
    if client == "codex":
        return codex(f"› {DIM_}Ask Codex to do anything{RESET_}", above=f"  tool output, {n}s elapsed",
                     footer=f"  {DIM_}? for shortcuts{' ' * 20}{100 - n}% context left{RESET_}")
    return claude(f"> {INVERSE_}T{RESET_}{DIM_}ry something{RESET_}", footer=f"  {DIM_}? for shortcuts · {n}s{RESET_}")


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_unrelated_output_changes_do_not_stop_a_delivery_when_the_composer_stays_empty(tmp_path, client):
    row, snap = (ROW, SNAP) if client == "codex" else (CLAUDE_ROW, {**SNAP, "command": "claude", "client": "claude"})
    typed = codex("› [Pasted text #1 +3 lines]") if client == "codex" else claude("> [Pasted text #1 +3 lines]")
    idle = [ticking(client, n) for n in range(4)]
    assert len(set(idle)) == 4
    pane = Pane([(idle[0], CURSOR), (idle[1], CURSOR), (idle[2], CURSOR), (typed, (20, 4, True)), (idle[3], CURSOR)])
    outcome, calls = deliver(backend_for(pane, [snap], row=row), tmp_path, row=row)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed") and calls == ["checkpoint"]
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]
    # The terminal and client identity is still part of the receipt; the screen text is not.
    assert set(outcome.receipt) == {"client", "composer", "target"} and "tool output" not in json.dumps(outcome.receipt)


@pytest.mark.parametrize("position", [1, 2])
def test_a_client_switch_while_the_screen_animates_is_still_refused(tmp_path, position):
    pane = Pane([(ticking("codex", n), CURSOR) for n in range(4)])
    snapshots = [SNAP, SNAP, SNAP][:position] + [{**SNAP, "foreground_pid": 11}] * 2
    outcome, calls = deliver(backend_for(pane, snapshots), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "screen_changing") and pane.inputs() == []
    assert calls == (["checkpoint"] if position == 2 else [])


def test_the_composer_is_observed_once_more_after_the_checkpoint_and_before_the_first_input(tmp_path):
    # A human starts typing, or the client changes, while the checkpoint waits for the writer: nothing is pasted.
    typing = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (codex("› x"), CURSOR)])
    outcome, calls = deliver(backend_for(typing), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_not_empty") and calls == ["checkpoint"] and typing.inputs() == []
    busy = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (codex("› ", above="• Working (3s • esc to interrupt)"), CURSOR)])
    outcome, calls = deliver(backend_for(busy), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_busy") and calls == ["checkpoint"] and busy.inputs() == []
    swapped = Pane([(CODEX_EMPTY, CURSOR)])
    outcome, calls = deliver(backend_for(swapped, [SNAP, SNAP, {**SNAP, "foreground_pid": 11}]), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "screen_changing") and calls == ["checkpoint"] and swapped.inputs() == []


def test_the_checkpoint_precedes_the_first_input_and_a_failing_one_types_nothing(tmp_path):
    events = []
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True)), (CODEX_EMPTY, CURSOR)])
    original = pane.tmux
    pane.tmux = lambda *args: (events.append(args[0]), original(*args))[1]
    backend_for(pane).owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: events.append("checkpoint"))
    assert events.index("checkpoint") < events.index("load-buffer")

    def refuse():
        raise DomainError("maintenance", "closed")

    pane = Pane([(CODEX_EMPTY, CURSOR)])
    outcome = backend_for(pane).owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=refuse)
    assert (outcome.status, outcome.reason) == ("deferred", "checkpoint_failed") and pane.inputs() == []


def test_an_unverified_paste_is_uncertain_and_never_submitted_or_cleared(tmp_path):
    # The screen never shows the composer holding text: no submit key, no clearing key, no second paste.
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer"]
    assert not any(call[0] == "send-keys" for call in pane.calls)
    # A dialog appearing over the pasted text is not a place to press Enter either.
    modal = "  Would you like to run the following command?\n› 1. Yes, proceed (y)\n"
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (modal, CURSOR)])
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert not any(call[0] == "send-keys" for call in pane.calls)


def test_a_client_swapped_after_the_paste_is_uncertain_and_never_submitted(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True))])
    swapped = [SNAP, SNAP, SNAP, {**SNAP, "foreground_started": "2"}]
    outcome, _ = deliver(backend_for(pane, swapped), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert not any(call[0] == "send-keys" for call in pane.calls)


def test_slow_paste_render_is_reobserved_without_retyping(tmp_path):
    # The first post-paste frame is stale; the next is only partially drawn.
    pane = Pane([(CODEX_EMPTY, CURSOR)] * 4 + [(codex("› Qorqut owner"), (14, 4, True)),
                (TYPED, (20, 4, True)), (CODEX_EMPTY, CURSOR)])
    outcome, calls = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed")
    assert calls == ["checkpoint"]
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]


def test_paste_render_wait_is_bounded_and_diagnosed(tmp_path):
    from bazarlyk.transport.agent_terminal_control import PASTE_VERIFY_ATTEMPTS
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert outcome.receipt["paste_checks"] == PASTE_VERIFY_ATTEMPTS
    assert outcome.receipt["paste_check"] == "paste_not_in_composer"
    assert "target" in outcome.receipt and "composer" in outcome.receipt
    assert pane.index == 3 + PASTE_VERIFY_ATTEMPTS
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer"]


def test_a_client_change_during_paste_render_wait_stops_without_submit(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR)] * 4 + [(TYPED, (20, 4, True))])
    snapshots = [SNAP] * 4 + [{**SNAP, "foreground_started": "2"}]
    outcome, _ = deliver(backend_for(pane, snapshots), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert outcome.receipt["paste_check"] == "session_changed"
    assert outcome.receipt["paste_checks"] == 2
    assert not any(call[0] == "send-keys" for call in pane.calls)


def test_paste_and_submit_failures_are_reported_precisely(tmp_path):
    outcome, calls = deliver(backend_for(Pane([(CODEX_EMPTY, CURSOR)], fail=("load-buffer",))), tmp_path)
    assert (outcome.status, outcome.reason) == ("deferred", "paste_failed") and calls == ["checkpoint"]
    outcome, _ = deliver(backend_for(Pane([(CODEX_EMPTY, CURSOR)], fail=("paste-buffer",))), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_failed")
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True))], fail=("send-keys",))
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "submit_failed")
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 1


def test_a_submit_that_left_the_text_in_the_composer_is_uncertain_and_not_retried(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True)), (TYPED, (20, 4, True))])
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert (outcome.status, outcome.reason) == ("uncertain", "submit_unconfirmed")
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 1


def test_a_turn_that_started_after_submit_counts_as_submitted(tmp_path):
    busy = codex("› ", above="• Working (1s • esc to interrupt)")
    pane = Pane([(CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (CODEX_EMPTY, CURSOR), (TYPED, (20, 4, True)), (busy, CURSOR)])
    outcome, _ = deliver(backend_for(pane), tmp_path)
    assert outcome.status == "submitted"


def test_deliveries_to_one_target_are_serialized(tmp_path):
    entered, release = threading.Event(), threading.Event()
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    backend = backend_for(pane)
    outcomes = []

    def hold():
        entered.set()
        release.wait(5)
        raise RuntimeError("stop")

    thread = threading.Thread(target=lambda: outcomes.append(backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=hold)))
    thread.start()
    assert entered.wait(5)
    assert backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: None)[:2] == ("deferred", "target_busy")
    release.set()
    thread.join(5)
    assert outcomes[0].reason == "checkpoint_failed"


def test_control_characters_and_unsupported_runtimes_are_refused_before_any_terminal_read(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR)])
    backend = backend_for(pane)
    for bad in ("hello\x1b[201~", "nul\x00", "bell\x07", "c1\x9b", "", "   ", "x" * 17001):
        with pytest.raises(DomainError):
            backend.owner_input(ROW, bad, workdir=private(tmp_path), checkpoint=lambda: None)
    row = {**ROW, "profile_json": json.dumps({"runtime": "other"})}
    assert backend.owner_input(row, TEXT, workdir=private(tmp_path), checkpoint=lambda: None)[:2] == ("failed", "unsupported_runtime")
    assert pane.calls == []


def test_runtime_selects_the_client():
    assert runtime_client(ROW) == "codex" and runtime_client(CLAUDE_ROW) == "claude"
    assert runtime_client({"profile_json": {"runtime": "claude-code"}}) == "claude"
    assert runtime_client({}) is None and runtime_client({"profile_json": "{}"}) is None


def test_claude_targets_use_the_claude_recognizer(tmp_path):
    typed = claude("> " + TEXT.replace("\n", " "))
    pane = Pane([(CLAUDE_EMPTY, CURSOR), (CLAUDE_EMPTY, CURSOR), (CLAUDE_EMPTY, CURSOR), (typed, (20, 4, True)), (CLAUDE_EMPTY, CURSOR)])
    snap = {**SNAP, "command": "claude", "client": "claude"}
    outcome, _ = deliver(backend_for(pane, [snap], row=CLAUDE_ROW), tmp_path, row=CLAUDE_ROW)
    assert outcome.status == "submitted"


def test_snapshot_verifies_the_requested_client_for_any_role(tmp_path, monkeypatch):
    import bazarlyk.transport.agent_terminal_control as module
    proc = tmp_path / "proc"
    proc.mkdir()

    def process(pid, group, foreground, command):
        folder = proc / str(pid)
        folder.mkdir()
        fields = ["S", "1", str(group), "1", "44", str(foreground)] + ["0"] * 13 + ["123456"]
        (folder / "stat").write_text(f"{pid} ({command}) " + " ".join(fields))
        (folder / "comm").write_text(command)
        (folder / "cmdline").write_bytes(command.encode() + b"\0")

    process(10, 10, 20, "bash")
    process(20, 20, 20, "claude")
    monkeypatch.setattr(module, "Path", lambda value: proc if value == "/proc" else proc / str(value).removeprefix("/proc/"))
    broker = SimpleNamespace(session_alive=lambda s: True, tmux=lambda *a: (0, "1|$1|%1|10|claude|/dev/pts/44"))
    backend = AgentTerminalBackend(SimpleNamespace(runtime=None))
    backend.selected = lambda row: (broker, None, "session", None, None)
    seen = backend.snapshot(CLAUDE_ROW, client="claude")
    assert (seen["command"], seen["client"]) == ("claude", "claude")
    # The default (no requested client) keeps the historical Codex-only verification for non-Dev roles.
    assert backend.snapshot(CLAUDE_ROW)["command"] == "claude" and "client" not in backend.snapshot(CLAUDE_ROW)
    assert backend.snapshot(ROW, client="codex")["command"] == "unrecognized"


# ----- ordinary owner text: wording in the message or in earlier output never breaks delivery ------------------------


def _full_delivery(tmp_path, client, text, *, history=("  Done.",), typed=None, after=None):
    """One complete delivery through the adapter: idle composer x3, the pasted literal shown, then the composer empty again."""
    row, snap = (ROW, SNAP) if client == "codex" else (CLAUDE_ROW, {**SNAP, "command": "claude", "client": "claude"})
    empty = f"{DIM_}Ask Codex to do anything{RESET_}" if client == "codex" else f"{INVERSE_}T{RESET_}{DIM_}ry something{RESET_}"
    idle = talk(client, list(history), [empty])
    lines = text.split("\n")
    shown = typed if typed is not None else talk(client, list(history), lines)
    top = (2 if client == "codex" else 3) + len(history) - 1  # the prompt row of this layout
    pane = Pane([(idle, (2, top, True))] * 3 + [(shown, (10, top + len(lines) - 1, True)), (after or idle, (2, top, True))])
    outcome, calls = deliver(backend_for(pane, [snap], row=row), tmp_path, row=row, text=text)
    return outcome, calls, pane


DIM_, RESET_, INVERSE_ = "\x1b[2m", "\x1b[0m", "\x1b[7m"


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("phrase", WORDING)
def test_ordinary_owner_text_containing_dialog_or_status_wording_is_submitted_once(tmp_path, client, phrase):
    text = f"Qorqut owner message via Telegram (correlation c1).\n\n{phrase}"
    # The earlier conversation mentions the same wording and even an old status line: the composer is still idle.
    history = ("  I was thinking about it.", f"  {phrase}", "  Working (3s) was only a word in that answer.")
    outcome, calls, pane = _full_delivery(tmp_path, client, text, history=history)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed"), (phrase, outcome)
    assert calls == ["checkpoint"]
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 1 and pane.loaded[0] == text


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_long_multiline_owner_message_is_verified_line_for_line_and_submitted_once(tmp_path, client):
    text = "Qorqut owner message via Telegram (correlation c2).\n\n" + "\n".join(
        f"{n}. Please review the release notes, then sign in and confirm the API key rotation." for n in range(1, 9))
    outcome, calls, pane = _full_delivery(tmp_path, client, text)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed") and calls == ["checkpoint"]
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 1


def test_a_client_that_collapses_the_paste_to_a_placeholder_is_submitted_once(tmp_path):
    outcome, calls, pane = _full_delivery(tmp_path, "codex", TEXT, typed=codex("› [Pasted text #1 +3 lines]"))
    assert outcome.status == "submitted" and sum(1 for call in pane.calls if call[0] == "send-keys") == 1


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_owner_wording_left_in_the_composer_after_submit_is_an_unconfirmed_submit_not_a_dialog(tmp_path, client):
    text = "Qorqut owner message via Telegram (correlation c3).\n\nEnter to confirm or Esc to cancel: do you want to sign in?"
    shown = talk(client, ["  Done."], text.split("\n"))
    outcome, _, pane = _full_delivery(tmp_path, client, text, typed=shown, after=shown)
    assert (outcome.status, outcome.reason) == ("uncertain", "submit_unconfirmed")
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 1  # never pressed again, never cleared


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_real_dialog_after_the_first_observation_is_fail_closed_and_never_submitted(tmp_path, client):
    dialog = {"codex": "  Allow this tool?\n› 1. Yes, proceed (y)\n  2. No, and tell Codex what to do differently (esc)\n",
              "claude": talk("claude", ["  Allow this tool to run?"], ["1. Yes, allow once", "2. No"])}[client]
    outcome, _, pane = _full_delivery(tmp_path, client, TEXT, typed=dialog)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert not any(call[0] == "send-keys" for call in pane.calls)
    # A dialog already showing before the first input is a plain deferral: nothing is typed.
    idle_dialog = Pane([(dialog, CURSOR)])
    snap = SNAP if client == "codex" else {**SNAP, "command": "claude", "client": "claude"}
    row = ROW if client == "codex" else CLAUDE_ROW
    outcome, calls = deliver(backend_for(idle_dialog, [snap], row=row), tmp_path, row=row)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_modal") and idle_dialog.inputs() == [] and calls == []


def test_a_turn_that_starts_after_the_paste_is_never_submitted_into(tmp_path):
    running = talk("codex", ["  Done."], TEXT.split("\n"), footer="  ✻ Working… (1s • esc to interrupt)")
    outcome, _, pane = _full_delivery(tmp_path, "codex", TEXT, typed=running)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert not any(call[0] == "send-keys" for call in pane.calls)


# ----- a real tmux server on an isolated socket ---------------------------------------------------------------

FAKE_CLIENT = r'''
import os, select, sys, termios, tty
receipt = os.environ["RECEIPT"]
running = os.environ.get("MODE") == "running"
animated = os.environ.get("MODE") in ("ticking", "running")
tick, footer_row = 0, 4
fd = sys.stdin.fileno()
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h")
def header():
    if running:
        return "  \u2022 Working (%ds \u2022 esc to interrupt)" % tick
    return "  The assistant explained the change." + (" (%ds)" % tick if animated else "")
def footer():
    if running:
        return "  \x1b[2mtab to queue message   %d%% context left\x1b[0m" % (100 - tick % 100)
    return "  \x1b[2m? for shortcuts" + ("   %d%% context left" % (100 - tick % 100) if animated else "") + "\x1b[0m"
def draw(text=""):
    global footer_row
    footer_row = 4
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write(header() + "\r\n\r\n")
    if text:
        sys.stdout.write("› " + text + "\r\n")
    else:
        sys.stdout.write("› \x1b[2mAsk Codex to do anything\x1b[0m\r\n")
    sys.stdout.write(footer())
    sys.stdout.write("\x1b[3;%dH" % (3 + len(text)))
    sys.stdout.flush()
def draw_lines(lines):
    global footer_row
    footer_row = 3 + len(lines)
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.write(header() + "\r\n\r\n")
    for index, line in enumerate(lines):
        sys.stdout.write(("› " if index == 0 else "  ") + line + "\r\n")
    sys.stdout.write(footer())
    sys.stdout.write("\x1b[%d;%dH" % (2 + len(lines), 3 + len(lines[-1])))
    sys.stdout.flush()
draw()
data = b""
pasted = None
typed = ""
while True:
    if animated and not select.select([fd], [], [], 0.04)[0]:
        # Only unrelated output moves: the transcript timer and the footer counter, never the composer or its cursor.
        tick += 1
        sys.stdout.write("\x1b7\x1b[1;1H\x1b[2K" + header() + "\x1b[%d;1H\x1b[2K" % footer_row + footer() + "\x1b8")
        sys.stdout.flush()
        continue
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    if pasted is None and b"\x1b" not in chunk and b"\r" not in chunk:
        typed += chunk.decode()
        draw(typed)
        continue
    data += chunk
    if b"\x1b[201~" in data and pasted is None:
        start = data.index(b"\x1b[200~") + 6
        end = data.index(b"\x1b[201~")
        pasted = data[start:end].decode()
        data = data[end + 6:]
        if os.environ.get("MODE") == "literal":
            draw_lines(pasted.split("\n"))
        else:
            draw("[Pasted text +%d lines]" % (pasted.count("\n") + 1))
    if pasted is not None and b"\r" in data:
        with open(receipt, "a" if running else "w") as handle:
            handle.write(pasted + ("\n--\n" if running else ""))
        pasted, data = None, b""
        draw()
'''


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required for terminal integration coverage")
@pytest.mark.parametrize("mode,text", [
    ("placeholder", "Qorqut owner message via Telegram (correlation c9).\n\nline two with ünïcode and 'quotes'"),
    ("literal", "Qorqut owner message via Telegram (correlation c9).\n\nDo you want to check the API key? Please sign in.\nEsc to cancel"),
    ("ticking", "Qorqut owner message via Telegram (correlation c9).\n\nline two with ünïcode and 'quotes'"),
])
def test_real_tmux_pane_receives_the_exact_text_once_and_only_when_ready(tmp_path, mode, text):
    socket = str(tmp_path / "tmux.sock")
    receipt = tmp_path / "received.txt"
    script = tmp_path / "client.py"
    script.write_text(FAKE_CLIENT)

    class Broker:
        def tmux(self, *args):
            result = subprocess.run(["tmux", "-S", socket, *args], capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    broker = Broker()
    session = "owner__input"
    env = {**os.environ, "RECEIPT": str(receipt), "TERM": "xterm-256color", "MODE": mode}
    try:
        subprocess.run(["tmux", "-S", socket, "new-session", "-d", "-s", session, "-x", "100", "-y", "20", f"python3 {script}"],
                       env=env, check=True)
        pane = broker.tmux("list-panes", "-t", "=" + session, "-F", "#{pane_id}")[1].strip()
        backend = AgentTerminalBackend(SimpleNamespace(runtime=None), sleep=time.sleep, screen_settle=.2, paste_settle=.4, submit_settle=.5)
        backend.snapshot = lambda row, client=None: {**SNAP, "pane_id": pane}
        backend.selected = lambda row: (broker, "key", session, tmp_path, None)
        until = time.monotonic() + 5
        while "Ask Codex" not in broker.tmux("capture-pane", "-p", "-t", pane)[1] and time.monotonic() < until:
            time.sleep(.05)
        if mode == "ticking":
            # The pane really does change while its composer stays empty: unrelated output must not block the delivery.
            first = broker.tmux("capture-pane", "-p", "-e", "-t", pane)[1]
            time.sleep(.3)
            assert broker.tmux("capture-pane", "-p", "-e", "-t", pane)[1] != first
        work = private(tmp_path)
        checkpoints = []
        outcome = backend.owner_input(ROW, text, workdir=work, checkpoint=lambda: checkpoints.append(1))
        assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed"), outcome
        until = time.monotonic() + 5
        while not receipt.exists() and time.monotonic() < until:
            time.sleep(.05)
        assert receipt.read_text() == text
        assert checkpoints == [1] and list(work.iterdir()) == []
        # Text somebody has begun to type is not overwritten: the second delivery queues and types nothing.
        assert broker.tmux("send-keys", "-t", pane, "-l", "half a thought")[0] == 0
        time.sleep(.3)
        receipt.unlink()
        outcome = backend.owner_input(ROW, "second message", workdir=work, checkpoint=lambda: checkpoints.append(2))
        assert outcome.status == "deferred" and checkpoints == [1]
        assert not receipt.exists()
    finally:
        broker.tmux("kill-server")


def test_a_submitted_or_uncertain_delivery_carries_a_terminal_receipt_without_text_and_a_deferral_does_not(tmp_path):
    typed = (TYPED, (20, 4, True))
    submitted, _ = deliver(backend_for(Pane([(CODEX_EMPTY, CURSOR)] * 3 + [typed, (CODEX_EMPTY, CURSOR)])), tmp_path)
    assert submitted.status == "submitted" and set(submitted.receipt) == {"client", "composer", "target"}
    assert submitted.receipt["client"] == "codex" and len(submitted.receipt["target"]) == 64
    assert TEXT not in json.dumps(submitted.receipt) and "please look" not in json.dumps(submitted.receipt)
    uncertain, _ = deliver(backend_for(Pane([(CODEX_EMPTY, CURSOR)] * 3 + [typed, typed])), tmp_path)
    assert (uncertain.status, uncertain.reason) == ("uncertain", "submit_unconfirmed") and uncertain.receipt == submitted.receipt
    busy = codex("› ", above="• Working (3s • esc to interrupt)")
    deferred, _ = deliver(backend_for(Pane([(busy, CURSOR)])), tmp_path)
    assert deferred.status == "deferred" and deferred.receipt is None


def test_chat_submits_a_successful_long_paste_without_full_visible_echo(tmp_path):
    text = "Long owner message " * 200
    clipped = claude("❯ … message tail")
    pane = Pane([(CLAUDE_EMPTY, CURSOR)] * 3 + [(clipped, (20, 4, True)), (CLAUDE_EMPTY, CURSOR)])
    backend = backend_for(pane, [{**SNAP, "command": "claude", "client": "claude"}])
    outcome = backend.owner_input(CLAUDE_ROW, text, workdir=private(tmp_path), checkpoint=lambda: None, require_paste_echo=False)
    assert outcome.status == "submitted"
    assert pane.loaded == (text, 0o600)
    assert [c for c in pane.calls if c[0] == "send-keys"] == [("send-keys", "-t", "%3", "Enter")]


def test_chat_does_not_submit_if_terminal_changes_after_paste(tmp_path):
    pane = Pane([(CODEX_EMPTY, CURSOR)] * 4)
    backend = backend_for(pane, [SNAP, SNAP, SNAP, {**SNAP, "foreground_started": "2"}])
    outcome = backend.owner_input(ROW, TEXT, workdir=private(tmp_path), checkpoint=lambda: None, require_paste_echo=False)
    assert outcome.status == "uncertain"
    assert not any(c[0] == "send-keys" for c in pane.calls)


# ----- allow_busy: typed while the native turn runs (Terminal Chat only); every other caller stays idle-only ---------------

from test_terminal_composer import capture  # noqa: E402

SETUP = {"codex": (ROW, SNAP), "claude": (CLAUDE_ROW, {**SNAP, "command": "claude", "client": "claude"})}
SAFE_TMUX = {"display-message", "capture-pane", "load-buffer", "paste-buffer", "delete-buffer", "send-keys"}


def running(client, n=0, *, composer=None):
    """A running turn (a status timer that keeps changing) beside an empty editable composer, or beside ``composer``."""
    if client == "codex":
        return codex(composer or f"› {DIM_}Ask Codex to do anything{RESET_}", above=f"  • Working ({n}s • esc to interrupt)",
                     footer=f"  {DIM_}tab to queue message{' ' * 20}{100 - n}% context left{RESET_}")
    return claude(composer or f"> {INVERSE_}T{RESET_}{DIM_}ry something{RESET_}",
                  footer=f"  {DIM_}⏸ manual mode on · esc to interrupt · {n}s{RESET_}")


def pasted(client, n=0, text="[Pasted text #1 +3 lines]"):
    return running(client, n, composer=("› " if client == "codex" else "> ") + text)


def busy_delivery(tmp_path, client, script, *, text=TEXT, checkpoint=None, snapshots=None, **options):
    row, snap = SETUP[client]
    pane = Pane(script)
    calls = []
    outcome = backend_for(pane, snapshots or [snap], row=row).owner_input(
        row, text, workdir=private(tmp_path), checkpoint=checkpoint or (lambda: calls.append("checkpoint")), **options)
    return outcome, calls, pane


def only_enter(pane):
    keys = [call for call in pane.calls if call[0] == "send-keys"]
    assert keys == [("send-keys", "-t", "%3", "Enter")], keys
    # No other tmux command: nothing that starts, stops, interrupts or clears a client.
    assert {call[0] for call in pane.calls} <= SAFE_TMUX
    assert not any(part in ("C-c", "Escape", "Tab", "C-d", "C-u", "kill-pane", "respawn-pane") for call in pane.calls for part in call)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_running_native_turn_receives_one_paste_and_one_normal_enter_and_keeps_running(tmp_path, client):
    busy = [running(client, n) for n in range(4)]
    assert len(set(busy)) == 4                                  # background output moves between every observation
    script = [(busy[0], CURSOR), (busy[1], CURSOR), (busy[2], CURSOR), (pasted(client, 3), (20, 4, True)), (busy[3], CURSOR)]
    outcome, calls, pane = busy_delivery(tmp_path, client, script, require_paste_echo=False, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed") and calls == ["checkpoint"]
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]
    assert pane.loaded == (TEXT, 0o600)
    only_enter(pane)
    # The same exact-echo rule as for an idle composer also holds under running chrome.
    strict, _, strict_pane = busy_delivery(tmp_path, client, script, allow_busy=True)
    assert strict.status == "submitted" and [call[0] for call in strict_pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_the_default_option_still_defers_a_running_turn_for_telegram_and_every_other_caller(tmp_path, client):
    script = [(running(client, n), CURSOR) for n in range(5)]
    outcome, calls, pane = busy_delivery(tmp_path, client, script)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_busy") and calls == [] and pane.inputs() == []
    explicit, calls, pane = busy_delivery(tmp_path, client, script, allow_busy=False)
    assert (explicit.status, explicit.reason) == ("deferred", "composer_busy") and calls == [] and pane.inputs() == []
    # And a turn that is genuinely running is never typed into by the strict path even after an idle first look.
    late, calls, pane = busy_delivery(tmp_path, client, [(ticking(client, 0), CURSOR), (running(client, 1), CURSOR)])
    assert (late.status, late.reason) == ("deferred", "composer_busy") and pane.inputs() == []


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("states", [("busy", "idle", "busy", "idle"), ("idle", "busy", "idle", "idle"), ("busy", "busy", "idle", "busy")])
def test_the_turn_ending_or_starting_between_the_checks_is_fine_while_the_composer_stays_empty(tmp_path, client, states):
    screens = [(running(client, n) if state == "busy" else ticking(client, n), CURSOR) for n, state in enumerate(states)]
    # The paste lands in the composer of whichever state the client is in by then.
    typed = ((pasted(client, 9) if states[3] == "busy" else
              (codex("› [Pasted text #1 +3 lines]") if client == "codex" else claude("> [Pasted text #1 +3 lines]"))), (20, 4, True))
    outcome, calls, pane = busy_delivery(tmp_path, client, [*screens[:3], typed, screens[3]], require_paste_echo=False, allow_busy=True)
    assert (outcome.status, calls) == ("submitted", ["checkpoint"]) and [c[0] for c in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]
    only_enter(pane)


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("position", [0, 1, 2])
def test_text_in_a_running_composer_is_never_typed_over_at_any_observation(tmp_path, client, position):
    typed = running(client, 5, composer=("› " if client == "codex" else "> ") + "half a sentence")
    script = [(running(client, n), CURSOR) for n in range(3)]
    script[position] = (typed, CURSOR)
    outcome, calls, pane = busy_delivery(tmp_path, client, script, require_paste_echo=False, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_not_empty") and pane.inputs() == []
    assert calls == (["checkpoint"] if position == 2 else [])
    for name, real_client in (("claude-2.1.287-typed-while-running", "claude"), ("codex-0.159.3-typed-while-running", "codex")):
        if real_client != client:
            continue
        screen, cursor = capture(name)
        refused, _, real_pane = busy_delivery(tmp_path, client, [(screen, cursor)], allow_busy=True)
        assert refused.status == "deferred" and real_pane.inputs() == [], name


@pytest.mark.parametrize("client,screen,reason", [
    ("codex", "\n".join(["  Working through the plan.", "", "  • Working (12s • esc to interrupt)"]), "composer_busy"),
    ("claude", "\n".join(["  Some output", "", "  ✻ Thinking… (esc to interrupt)"]), "composer_busy"),
    ("codex", "\n".join(["  Would you like to run the following command?", "› 1. Yes, proceed (y)", "  2. No (esc)",
                         "  • Working (3s • esc to interrupt)"]), "composer_modal"),
    ("claude", claude("❯ 1. Yes", footer="  Do you want to proceed? · esc to interrupt"), "composer_modal"),
    ("codex", "user@host:~$ \n", "composer_unrecognized"),
    ("claude", "\n".join(["  ✻ Thinking… (esc to interrupt)", "> a markdown quote", "  more"]), "composer_unrecognized"),
    ("claude", claude(f"❯ {INVERSE_} {RESET_}", footer="  enter to interrupt and send · ctrl+x then enter to queue"), "submit_interrupts"),
    ("codex", codex(f"› {DIM_}Ask Codex{RESET_}", above="• Working (3s • esc to interrupt) · enter to interrupt and send"), "submit_interrupts"),
])
def test_a_running_turn_without_a_valid_composer_a_dialog_or_an_interrupting_submit_key_types_nothing(tmp_path, client, screen, reason):
    outcome, calls, pane = busy_delivery(tmp_path, client, [(screen, CURSOR)], require_paste_echo=False, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("deferred", reason)
    assert calls == [] and pane.inputs() == []
    # Checked again right before the first input too: a dialog or text that appears while the checkpoint waits.
    script = [(running(client, 0), CURSOR), (running(client, 1), CURSOR), (screen, CURSOR)]
    late, calls, pane = busy_delivery(tmp_path, client, script, require_paste_echo=False, allow_busy=True)
    assert (late.status, late.reason, calls, pane.inputs()) == ("deferred", reason, ["checkpoint"], [])


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_the_client_must_stay_the_same_exact_terminal_across_busy_observations(tmp_path, client):
    row, snap = SETUP[client]
    script = [(running(client, n), CURSOR) for n in range(4)]
    other = {**snap, "foreground_pid": 11}
    outcome, calls, pane = busy_delivery(tmp_path, client, script, snapshots=[snap, other], allow_busy=True)
    assert (outcome.status, outcome.reason, calls, pane.inputs()) == ("deferred", "screen_changing", [], [])
    late, calls, pane = busy_delivery(tmp_path, client, script, snapshots=[snap, snap, other], allow_busy=True)
    assert (late.status, late.reason, calls, pane.inputs()) == ("deferred", "screen_changing", ["checkpoint"], [])
    # A swap after the paste is the usual uncertain case: the Enter is never sent.
    after = busy_delivery(tmp_path, client, script, snapshots=[snap, snap, snap, other], allow_busy=True)
    assert (after[0].status, after[0].reason) == ("uncertain", "paste_unverified") and not any(c[0] == "send-keys" for c in after[2].calls)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_busy_chrome_alone_neither_blocks_the_enter_nor_makes_an_unconfirmed_submit_look_sent(tmp_path, client):
    ready = [(running(client, n), CURSOR) for n in range(3)]
    taken = busy_delivery(tmp_path, client, [*ready, (pasted(client, 4), (20, 4, True)), (running(client, 5), CURSOR)], allow_busy=True)
    assert taken[0].status == "submitted" and sum(1 for c in taken[2].calls if c[0] == "send-keys") == 1
    # The paste is still in the (running) composer after the one Enter: uncertain, and never submitted a second time.
    stuck = busy_delivery(tmp_path, client, [*ready, (pasted(client, 4), (20, 4, True)), (pasted(client, 5), (20, 4, True))], allow_busy=True)
    assert (stuck[0].status, stuck[0].reason) == ("uncertain", "submit_unconfirmed")
    assert sum(1 for c in stuck[2].calls if c[0] == "send-keys") == 1
    # A running composer that shows other text than the paste is not the pasted literal: the strict rule sends no Enter ...
    clipped = running(client, 4, composer=("› " if client == "codex" else "> ") + "… message tail")
    strict = busy_delivery(tmp_path, client, [*ready, (clipped, (20, 4, True))], allow_busy=True)
    assert (strict[0].status, strict[0].reason) == ("uncertain", "paste_unverified")
    assert not any(c[0] == "send-keys" for c in strict[2].calls)
    # ... while Chat's long-paste rule (a clipped or collapsed echo is fine, identity and mode are still verified) sends the one Enter.
    chat = busy_delivery(tmp_path, client, [*ready, (clipped, (20, 4, True)), (running(client, 5), CURSOR)], require_paste_echo=False,
                         allow_busy=True)
    assert chat[0].status == "submitted"
    only_enter(chat[2])


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_client_whose_enter_would_interrupt_is_never_pasted_into_or_submitted_after_the_paste(tmp_path, client):
    interrupting = running(client, 4, composer=("› " if client == "codex" else "> ") + "[Pasted text #1 +3 lines]").replace(
        "esc to interrupt", "enter to interrupt and send")
    outcome, _, pane = busy_delivery(tmp_path, client, [(running(client, n), CURSOR) for n in range(3)] + [(interrupting, (20, 4, True))],
                                     require_paste_echo=False, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("uncertain", "paste_unverified")
    assert not any(c[0] == "send-keys" for c in pane.calls)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_nudge_paste_can_finish_rendering_while_the_turn_keeps_running(tmp_path, client):
    ready = [(running(client, n), CURSOR) for n in range(3)]
    script = [*ready, (running(client, 3), CURSOR), (pasted(client, 4), (20, 4, True)),
              (running(client, 5), CURSOR)]
    outcome, calls, pane = busy_delivery(tmp_path, client, script, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed")
    assert calls == ["checkpoint"]
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"]


class RecordingPane(Pane):
    def __init__(self, script, **kwargs):
        super().__init__(script, **kwargs)
        self.texts = []

    def tmux(self, *args):
        outcome = super().tmux(*args)
        if args[0] == "load-buffer":
            self.texts.append(self.loaded[0])
        return outcome


def test_several_messages_are_delivered_one_after_another_while_the_original_claude_turn_keeps_running(tmp_path):
    """Real Claude 2.1.287 captures: after each Enter the composer shows the queued-message placeholder, and is ready again."""
    queued_one, cursor_one = capture("claude-2.1.287-queued-one")
    queued_two, cursor_two = capture("claude-2.1.287-queued-two")
    running_screen, running_cursor = capture("claude-2.1.287-running")
    pasted_screen, pasted_cursor = capture("claude-2.1.287-pasted-while-queued")
    row, snap = SETUP["claude"]
    script = []
    for ready, cursor in ((running_screen, running_cursor), (queued_one, cursor_one), (queued_two, cursor_two)):
        script += [(ready, cursor)] * 3 + [(pasted_screen, pasted_cursor), (queued_two, cursor_two)]
    pane = RecordingPane(script)
    backend = backend_for(pane, [snap], row=row)
    texts = ["folded message two", "a second one", "and a third"]
    outcomes = [backend.owner_input(row, text, workdir=private(tmp_path), checkpoint=lambda: None, require_paste_echo=False, allow_busy=True)
                for text in texts]
    assert [o.status for o in outcomes] == ["submitted"] * 3
    assert pane.texts == texts
    assert [call[0] for call in pane.inputs()] == ["load-buffer", "paste-buffer", "send-keys"] * 3
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 3
    assert {call[0] for call in pane.calls} <= SAFE_TMUX and not any("Escape" in call or "C-c" in call for call in pane.calls)
    # The very same real running screens are refused for any caller that did not opt in.
    strict = backend_for(Pane([(running_screen, running_cursor)]), [snap], row=row).owner_input(
        row, "no", workdir=private(tmp_path), checkpoint=lambda: None)
    assert (strict.status, strict.reason) == ("deferred", "composer_busy")


def test_several_messages_are_delivered_one_after_another_while_the_original_codex_turn_keeps_running(tmp_path):
    """Real codex 0.159.3: after each Enter the composer is empty again (a steer is pending above it) while the turn runs."""
    steer, steer_cursor = capture("codex-0.159.3-pending-steer")
    working = running("codex", 3)
    row, snap = SETUP["codex"]
    script = []
    for ready in ((working, CURSOR), (steer, steer_cursor), (steer, steer_cursor)):
        script += [ready] * 3 + [(pasted("codex", 4), (20, 4, True)), (steer, steer_cursor)]
    pane = RecordingPane(script)
    backend = backend_for(pane, [snap], row=row)
    texts = ["codex steer one", "codex steer two", "codex steer three"]
    outcomes = [backend.owner_input(row, text, workdir=private(tmp_path), checkpoint=lambda: None, require_paste_echo=False, allow_busy=True)
                for text in texts]
    assert [o.status for o in outcomes] == ["submitted"] * 3 and pane.texts == texts
    assert sum(1 for call in pane.calls if call[0] == "send-keys") == 3
    assert {call[0] for call in pane.calls} <= SAFE_TMUX and not any("Escape" in call or "Tab" in call for call in pane.calls)


def test_a_message_waits_until_the_previous_input_has_left_the_composer(tmp_path):
    """While the previous input is still sitting in the composer (not yet taken by the client) nothing more is typed."""
    row, snap = SETUP["claude"]
    sitting = running("claude", 2, composer="> the previous input still here")
    pane = Pane([(sitting, CURSOR)])
    outcome = backend_for(pane, [snap], row=row).owner_input(row, "next", workdir=private(tmp_path), checkpoint=lambda: None,
                                                              require_paste_echo=False, allow_busy=True)
    assert (outcome.status, outcome.reason) == ("deferred", "composer_not_empty") and pane.inputs() == []


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is required for terminal integration coverage")
def test_a_real_tmux_pane_with_a_running_turn_receives_each_text_once_only_with_allow_busy(tmp_path):
    """A real tmux server and pty: a client whose turn never ends (a ticking status row) but whose composer stays editable."""
    socket = str(tmp_path / "tmux.sock")
    receipt = tmp_path / "received.txt"
    script = tmp_path / "client.py"
    script.write_text(FAKE_CLIENT)

    class Broker:
        def tmux(self, *args):
            result = subprocess.run(["tmux", "-S", socket, *args], capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

    broker = Broker()
    session = "owner__busy"
    env = {**os.environ, "RECEIPT": str(receipt), "TERM": "xterm-256color", "MODE": "running"}
    try:
        subprocess.run(["tmux", "-S", socket, "new-session", "-d", "-s", session, "-x", "100", "-y", "20", f"python3 {script}"],
                       env=env, check=True)
        pane = broker.tmux("list-panes", "-t", "=" + session, "-F", "#{pane_id}")[1].strip()
        backend = AgentTerminalBackend(SimpleNamespace(runtime=None), sleep=time.sleep, screen_settle=.2, paste_settle=.4, submit_settle=.5)
        backend.snapshot = lambda row, client=None: {**SNAP, "pane_id": pane}
        backend.selected = lambda row: (broker, "key", session, tmp_path, None)
        until = time.monotonic() + 5
        while "Ask Codex" not in broker.tmux("capture-pane", "-p", "-t", pane)[1] and time.monotonic() < until:
            time.sleep(.05)
        first = broker.tmux("capture-pane", "-p", "-e", "-t", pane)[1]
        time.sleep(.3)
        assert "esc to interrupt" in first and broker.tmux("capture-pane", "-p", "-e", "-t", pane)[1] != first  # running, and moving
        work, checkpoints = private(tmp_path), []
        # Any caller that did not opt in is told the turn is running and nothing is typed.
        refused = backend.owner_input(ROW, "not for telegram", workdir=work, checkpoint=lambda: checkpoints.append(0))
        assert (refused.status, refused.reason) == ("deferred", "composer_busy") and checkpoints == [] and not receipt.exists()
        texts = ["first while running\nsecond line", "another one", "and a third"]
        for index, text in enumerate(texts, 1):
            outcome = backend.owner_input(ROW, text, workdir=work, checkpoint=lambda: checkpoints.append(1), require_paste_echo=False,
                                          allow_busy=True)
            assert (outcome.status, outcome.reason) == ("submitted", "processing_not_confirmed"), (index, outcome)
            until = time.monotonic() + 5
            while receipt.read_text().count("--") < index if receipt.exists() else True:
                assert time.monotonic() < until
                time.sleep(.05)
        assert receipt.read_text() == "".join(text + "\n--\n" for text in texts) and checkpoints == [1, 1, 1]
        assert list(work.iterdir()) == []
        assert "esc to interrupt" in broker.tmux("capture-pane", "-p", "-t", pane)[1]          # the turn is still running
        # Text somebody has begun to type in the running composer is not overwritten: the next delivery waits.
        assert broker.tmux("send-keys", "-t", pane, "-l", "half a thought")[0] == 0
        time.sleep(.3)
        before = receipt.read_text()
        outcome = backend.owner_input(ROW, "after the typing", workdir=work, checkpoint=lambda: checkpoints.append(2),
                                      require_paste_echo=False, allow_busy=True)
        assert (outcome.status, outcome.reason) == ("deferred", "composer_not_empty") and checkpoints == [1, 1, 1]
        assert receipt.read_text() == before
    finally:
        broker.tmux("kill-server")


def test_first_system_input_creates_its_private_paste_directory(tmp_path):
    pane=Pane([(CODEX_EMPTY,CURSOR)]*3+[(TYPED,(20,4,True)),(CODEX_EMPTY,CURSOR)])
    workdir=tmp_path/'new-install'/'inbox-paste'
    result=backend_for(pane).owner_input(ROW,TEXT,workdir=workdir,checkpoint=lambda:None)
    assert result.status=='submitted'
    assert stat.S_IMODE(workdir.stat().st_mode)==0o700
    assert pane.loaded==(TEXT,0o600)
    assert list(workdir.iterdir())==[]
