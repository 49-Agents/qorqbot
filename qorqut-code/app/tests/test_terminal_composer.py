"""Composer recognition against styled screen fixtures. The fixtures are hand-built stand-ins for the documented
composer conventions of each client, not captures of a live session."""
import json
from pathlib import Path

import pytest

from bazarlyk.telegram.__main__ import main
from bazarlyk.transport import terminal_composer as composer

E = "\x1b"
DIM, RESET, INVERSE = f"{E}[2m", f"{E}[0m", f"{E}[7m"
HISTORY = "\n".join(["  The assistant explained the change.", "  It touched three files.", ""])


def codex(composer_row, *, above="", footer=None):
    footer = footer if footer is not None else f"  {DIM}? for shortcuts{' ' * 30}100% context left{RESET}"
    return "\n".join([HISTORY.rstrip("\n"), "", above, composer_row, footer]) + "\n"


def claude(inner, *, footer=f"  {DIM}? for shortcuts{RESET}"):
    rule = f"{DIM}{'─' * 60}{RESET}"
    return "\n".join([HISTORY.rstrip("\n"), "", rule, inner, rule, footer]) + "\n"


CODEX_EMPTY = codex(f"› {DIM}Ask Codex to do anything{RESET}")
CODEX_EMPTY_BARE = codex("› ")
CLAUDE_EMPTY = claude(f"> {INVERSE}T{RESET}{DIM}ry \"fix lint errors\"{RESET}")
CLAUDE_EMPTY_ARROW = claude(f"❯ {INVERSE} {RESET}")
CLAUDE_EMPTY_BOXED = "\n".join([HISTORY, f"╭{'─' * 40}╮", f"│ > {INVERSE}T{RESET}{DIM}ry something{RESET}{' ' * 20}│", f"╰{'─' * 40}╯",
                                f"  {DIM}? for shortcuts{RESET}"])


@pytest.mark.parametrize("client,screen", [("codex", CODEX_EMPTY), ("codex", CODEX_EMPTY_BARE), ("claude", CLAUDE_EMPTY),
                                           ("claude", CLAUDE_EMPTY_ARROW), ("claude", CLAUDE_EMPTY_BOXED)])
def test_an_idle_empty_composer_is_ready(client, screen):
    reading = composer.read(client, screen)
    assert (reading.state, reading.reason, reading.ready) == ("empty", "empty_composer", True)


def test_dim_placeholder_is_not_typed_text_but_plain_text_is():
    assert composer.read("codex", codex(f"› {DIM}Ask Codex to do anything{RESET}")).ready
    reading = composer.read("codex", codex("› fix the failing test"))
    assert (reading.state, reading.reason) == ("text", "composer_not_empty")
    assert composer.read("claude", claude("> half a sentence")).state == "text"
    # A single typed character is text even with the cursor cell after it.
    assert composer.read("claude", claude(f"> a{INVERSE} {RESET}")).state == "text"
    # Gray 256-colour placeholders count as dim; a plain white or coloured one does not.
    assert composer.read("codex", codex(f"› {E}[38;5;244mAsk{RESET}")).ready
    assert composer.read("codex", codex(f"› {E}[38;5;15mAsk{RESET}")).state == "text"
    assert composer.read("codex", codex(f"› {E}[90mAsk{RESET}")).ready


@pytest.mark.parametrize("client,screen", [
    ("codex", codex(f"› {DIM}Ask Codex to do anything{RESET}", above="• Working (12s • esc to interrupt)")),
    ("claude", claude(f"> {DIM}Try something{RESET}", footer="  ✻ Thinking… (esc to interrupt)")),
])
def test_a_running_turn_is_busy(client, screen):
    assert (composer.read(client, screen).state, composer.read(client, screen).reason) == ("busy", "turn_running")


@pytest.mark.parametrize("client,screen", [
    ("codex", "\n".join(["  Would you like to run the following command?", "  $ rm -rf build", "",
                         "› 1. Yes, proceed (y)", "  2. No, and tell Codex what to do differently (esc)"])),
    ("codex", "\n".join(["  Do you trust the contents of this directory?", "› 1. Yes, continue", "  2. No, quit"])),
    ("claude", claude("❯ 1. Yes")),
    ("claude", "\n".join(["  Do you want to proceed?", "  ❯ 1. Yes", "    2. No"])),
    ("codex", "\n".join(["  Select model", "› gpt-5 (current)", "  gpt-5-mini", "  Enter to confirm · Esc to cancel"])),
])
def test_dialogs_and_menus_are_never_the_composer(client, screen):
    assert composer.read(client, screen).state == "modal"


def test_unknown_layouts_fail_closed():
    assert composer.read("codex", "user@host:~$ \n").state == "unrecognized"
    assert composer.read("codex", "\n\n").reason == "blank_screen"
    assert composer.read("claude", "> a markdown quote\n").reason == "no_input_box"
    assert composer.read("gemini", CODEX_EMPTY).reason == "unsupported_client"
    # Two adjacent prompt-looking rows are ambiguous, not a composer.
    assert composer.read("codex", "› a\n› b\n").reason == "ambiguous_composer"


def test_continuation_lines_and_cursor_position_detect_multiline_text():
    typed = codex("› ", footer="  text below an otherwise empty prompt row")
    assert (composer.read("codex", typed).state, composer.read("codex", typed).reason) == ("text", "continuation_text")
    lines = CODEX_EMPTY.split("\n")
    row = next(index for index, line in enumerate(lines) if line.startswith("›"))
    assert composer.read("codex", CODEX_EMPTY, (2, row, True)).ready
    assert composer.read("codex", CODEX_EMPTY, (2, row + 1, True)).reason == "cursor_elsewhere"
    assert composer.read("codex", CODEX_EMPTY, (30, row, True)).reason == "cursor_moved"
    # A hidden cursor is not evidence either way.
    assert composer.read("codex", CODEX_EMPTY, (30, row + 3, False)).ready


def test_fingerprint_changes_with_screen_and_cursor():
    first = composer.read("codex", CODEX_EMPTY, (2, 4, True)).fingerprint
    assert composer.read("codex", CODEX_EMPTY, (2, 4, True)).fingerprint == first
    assert composer.read("codex", CODEX_EMPTY, (3, 4, True)).fingerprint != first
    assert composer.read("codex", CODEX_EMPTY_BARE, (2, 4, True)).fingerprint != first


def test_other_escapes_and_wide_text_do_not_break_parsing():
    screen = codex(f"{E}]0;title\x07› {DIM}Ask Codex 日本語{RESET}")
    assert composer.read("codex", screen).ready


def test_composer_check_command_classifies_a_saved_capture_without_touching_a_terminal(tmp_path, capsys):
    ready = tmp_path / "ready.txt"
    ready.write_text(CODEX_EMPTY)
    busy = tmp_path / "busy.txt"
    busy.write_text(codex("› ", above="• Working (3s • esc to interrupt)"))
    assert main(["composer-check", "--client", "codex", "--screen", str(ready)]) == 0
    assert json.loads(capsys.readouterr().out) == {"client": "codex", "ready": True, "reason": "empty_composer", "state": "empty"}
    assert main(["composer-check", "--client", "codex", "--screen", str(busy)]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "busy"
    assert main(["composer-check", "--client", "codex", "--screen", str(tmp_path / "missing")]) == 2


# ----- the composer is located first; wording never makes typed text or the transcript look like a dialog ---------

WORDING = [
    "Do you want to check the API key?", "Please sign in again before you continue", "I was thinking about it",
    "Would you like to trust this folder? (y/n)", "Press Enter to confirm or Esc to cancel", "Update available: update now",
]


def talk(client, history, rows, footer=None):
    """A screen with ``history`` lines of conversation above the composer's ``rows`` (a list of prompt-row texts)."""
    if client == "codex":
        composer = ["› " + rows[0]] + ["  " + row for row in rows[1:]]
        footer = footer if footer is not None else f"  {DIM}? for shortcuts{' ' * 30}100% context left{RESET}"
        return "\n".join([*history, "", *composer, footer]) + "\n"
    rule = f"{DIM}{'─' * 60}{RESET}"
    composer = ["> " + rows[0]] + ["  " + row for row in rows[1:]]
    footer = footer if footer is not None else f"  {DIM}? for shortcuts{RESET}"
    return "\n".join([*history, "", rule, *composer, rule, footer]) + "\n"


@pytest.mark.parametrize("client", ["codex", "claude"])
@pytest.mark.parametrize("phrase", WORDING)
def test_owner_wording_typed_into_the_composer_is_text_never_a_dialog_or_a_running_turn(client, phrase):
    reading = composer.read(client, talk(client, ["  Done."], [phrase]))
    assert (reading.state, reading.reason) == ("text", "composer_not_empty")
    # A multi-line draft with the wording on a later line is not ready either (whichever way it is labelled).
    assert not composer.read(client, talk(client, ["  Done."], ["first line", phrase])).ready


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_earlier_transcript_wording_never_blocks_an_idle_composer(client):
    history = ["  I was thinking about it while you were away.", "  Do you want to check the API key? Please sign in first.",
               "  Press Esc to cancel; the update is available. Working (3s) is only a phrase here.", "  > quoted", "› an earlier owner message"]
    empty = f"{DIM}Ask Codex to do anything{RESET}" if client == "codex" else f"{INVERSE}T{RESET}{DIM}ry something{RESET}"
    screen = talk(client, history, [empty])
    assert composer.read(client, screen).ready, composer.read(client, screen)
    row = len(history) + 1 + (0 if client == "codex" else 1)
    assert composer.read(client, screen, (2, row, True)).ready


def test_a_status_line_and_dialog_wording_in_the_chrome_rows_still_decide_and_a_real_menu_stays_a_modal():
    running = talk("codex", ["  Done."], [f"{DIM}Ask Codex{RESET}"], footer="  ✻ Working… (12s • esc to interrupt)")
    assert composer.read("codex", running).state == "busy"
    status_above = "\n".join(["  Done.", "", "  • Working (3s • esc to interrupt)", f"› {DIM}Ask Codex{RESET}", f"  {DIM}? for shortcuts{RESET}"])
    assert composer.read("codex", status_above).state == "busy"
    dialog_below = "\n".join(["  Sign in", f"› {DIM}Ask Codex{RESET}", "  Enter to confirm · Esc to cancel"])
    assert composer.read("codex", dialog_below).state == "modal"
    api_key = "\n".join(["  Paste your API key", "  Sign in with ChatGPT", "  Please login to continue"])
    assert composer.read("codex", api_key).state == "modal"  # no composer at all: not ready, labelled by the bottom rows
    menus = {"codex": "  Allow this tool?\n› 1. Yes, proceed (y)\n  2. No", "claude": talk("claude", ["  Allow?"], ["1. Yes, allow once"])}
    assert {client: composer.read(client, screen).state for client, screen in menus.items()} == {"codex": "modal", "claude": "modal"}


MULTILINE = "Qorqut owner message via Telegram (correlation c1).\n\nDo you want to check the API key?\nplease sign in, then reply"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_the_exact_pasted_literal_in_the_same_composer_is_verified_even_when_it_contains_dialog_wording(client):
    rows = MULTILINE.split("\n")
    screen = talk(client, ["  Done."], rows)
    first = 2 if client == "codex" else 3
    cursor = (10, first + len(rows) - 1, True)
    reading = composer.read(client, screen, cursor, expect=MULTILINE)
    assert (reading.state, reading.reason) == ("pasted", "paste_in_composer")
    assert composer.read(client, screen, None, expect=MULTILINE).state == "pasted"
    # Wrapping by the terminal changes nothing: only the characters must agree.
    wrapped = talk(client, ["  Done."], ["Qorqut owner message via", "Telegram (correlation c1).", "", "Do you want to check the API", "key?",
                                         "please sign in, then", "reply"])
    assert composer.read(client, wrapped, None, expect=MULTILINE).state == "pasted"


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_a_paste_that_is_not_the_intended_literal_in_the_composer_is_never_verified(client):
    rows = MULTILINE.split("\n")
    for screen, cursor in (
            (talk(client, ["  Done."], rows[:-1]), None),                      # part of the text
            (talk(client, ["  Done."], [*rows, "one more typed line"]), None),   # extra typed text after it
            (talk(client, ["  Done."], ["something else entirely"]), None),
            (talk(client, ["  Done."], rows), (5, 0, True)),                     # the cursor is not in the composer
            (talk(client, ["  Done."], [f"{DIM}Ask Codex{RESET}"]), None)):     # nothing pasted
        reading = composer.read(client, screen, cursor, expect=MULTILINE)
        assert reading.state == "text" and reading.reason == "paste_not_in_composer", (screen, reading)
    # A dialog on top of the composer is a menu, never the pasted literal.
    dialog = "\n".join(["  Allow this tool?", "› 1. Yes, proceed (y)", "  2. No"])
    assert composer.read(client if client == "codex" else "codex", dialog, None, expect=MULTILINE).state == "text"
    # A turn that began after the paste is a running turn, not a verified paste.
    running = talk(client, ["  Done."], rows, footer="  ✻ Working… (1s • esc to interrupt)")
    assert composer.read(client, running, None, expect=MULTILINE).state == "busy"


def test_a_paste_placeholder_counts_only_as_the_entire_composer_with_a_fitting_count():
    text = "line one\nline two\nline three"
    for row in ("[Pasted text #1 +3 lines]", "[Pasted Content 28 chars]", "[Pasted text #1]"):
        assert composer.read("codex", talk("codex", ["  Done."], [row]), None, expect=text).state == "pasted", row
    for row in ("[Pasted text #1 +40 lines]", "[Pasted Content 900 chars]", "[Pasted text #1] and more typed words",
                "look [Pasted text #1 +3 lines]"):
        assert composer.read("codex", talk("codex", ["  Done."], [row]), None, expect=text).state == "text", row


def test_composer_check_can_verify_a_saved_capture_taken_after_a_paste(tmp_path, capsys):
    capture = tmp_path / "after.txt"
    capture.write_text(talk("codex", ["  Done."], MULTILINE.split("\n")))
    pasted = tmp_path / "pasted.txt"
    pasted.write_text(MULTILINE)
    assert main(["composer-check", "--client", "codex", "--screen", str(capture), "--pasted-file", str(pasted)]) == 0
    assert json.loads(capsys.readouterr().out) == {"client": "codex", "ready": True, "reason": "paste_in_composer", "state": "pasted"}
    pasted.write_text("some other text")
    assert main(["composer-check", "--client", "codex", "--screen", str(capture), "--pasted-file", str(pasted)]) == 1
    assert main(["composer-check", "--client", "codex", "--screen", str(capture), "--pasted-file", str(tmp_path / "none")]) == 2


# ----- allow_busy: an empty, editable composer under a running turn (Terminal Chat only) -----------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "terminal_screens"


def capture(name):
    """A real capture taken from the installed client (see the fixtures README): ``(screen, (x, y, cursor_visible))``."""
    flags = (FIXTURES / f"{name}.cursor").read_text().strip().split("|")
    return (FIXTURES / f"{name}.screen").read_text(), (int(flags[3]), int(flags[4]), flags[2] == "1")


RUNNING_EMPTY = [
    ("codex", codex(f"› {DIM}Ask Codex to do anything{RESET}", above="• Working (12s • esc to interrupt)")),
    ("codex", codex("› ", above="• Working (3s • esc to interrupt)")),
    ("codex", codex(f"› {DIM}Ask Codex{RESET}", footer=f"  {DIM}tab to queue message   100% context left{RESET}")),
    ("claude", claude(f"> {DIM}Try something{RESET}", footer="  ✻ Thinking… (esc to interrupt)")),
    ("claude", claude(f"❯ {INVERSE} {RESET}", footer=f"  {DIM}⏸ manual mode on · esc to interrupt · ← for agents{RESET}")),
]


@pytest.mark.parametrize("client,screen", RUNNING_EMPTY)
def test_a_running_turn_beside_a_verified_empty_composer_is_ready_only_with_allow_busy(client, screen):
    default = composer.read(client, screen)
    assert (default.state, default.reason, default.ready) == ("busy", "turn_running", False)
    opted_in = composer.read(client, screen, allow_busy=True)
    assert (opted_in.state, opted_in.reason, opted_in.ready, opted_in.busy) == ("empty", "empty_composer_running", True, True)


@pytest.mark.parametrize("client,screen", [("codex", CODEX_EMPTY), ("claude", CLAUDE_EMPTY), ("claude", CLAUDE_EMPTY_BOXED)])
def test_an_idle_composer_reads_the_same_with_or_without_allow_busy_and_is_not_marked_busy(client, screen):
    assert composer.read(client, screen) == composer.read(client, screen, allow_busy=True)
    assert composer.read(client, screen, allow_busy=True).busy is False


@pytest.mark.parametrize("name,client,default,busy", [
    ("claude-2.1.287-idle", "claude", "empty", False),
    ("codex-0.159.3-idle", "codex", "empty", False),
    ("claude-2.1.287-running", "claude", "busy", True),
    ("claude-2.1.287-queued-one", "claude", "busy", True),
    ("claude-2.1.287-queued-two", "claude", "busy", True),
    # Codex shows no wording of its own while it streams text (a spinner glyph only), and its pending-steer preview sits
    # beyond the status rows read above the composer: both are an empty composer for either mode, and Enter steers.
    ("codex-0.159.3-running", "codex", "empty", False),
    ("codex-0.159.3-pending-steer", "codex", "empty", False)])
def test_real_client_captures_taking_input_while_running_are_ready_with_allow_busy(name, client, default, busy):
    screen, cursor = capture(name)
    reading = composer.read(client, screen, cursor, allow_busy=True)
    assert (reading.state, reading.ready, reading.busy) == ("empty", True, busy), reading
    assert composer.read(client, screen, cursor).state == default


def test_claude_draws_a_no_break_space_after_the_prompt_and_the_queued_placeholder_is_still_an_empty_composer():
    screen, cursor = capture("claude-2.1.287-queued-one")
    assert "❯\xa0" in screen
    assert composer.read("claude", screen, cursor, allow_busy=True).ready
    # Typed text after the same prompt is text, with or without the opt-in and whatever the running chrome says.
    placeholder = next(line for line in screen.split("\n") if "ress up to edit queued messages" in line)
    typed = screen.replace(placeholder, "\x1b[38;5;246m❯\xa0\x1b[39mhalf a thought\x1b[7m \x1b[0m")
    assert composer.read("claude", typed, cursor, allow_busy=True).state == "text"
    assert composer.read("claude", claude("❯\xa0half a thought")).state == "text"


@pytest.mark.parametrize("client,screen", [
    ("codex", codex("› fix the failing test", above="• Working (3s • esc to interrupt)")),
    ("claude", claude("> half a sentence", footer="  ✻ Thinking… (esc to interrupt)")),
    ("codex", codex("› ", above="• Working (3s • esc to interrupt)", footer="  typed on a later line")),
])
def test_running_with_text_already_in_the_composer_is_never_ready_even_with_allow_busy(client, screen):
    reading = composer.read(client, screen, allow_busy=True)
    assert reading.state == "text" and not reading.ready and reading.reason in ("composer_not_empty", "continuation_text")
    for name, cursor_client in (("claude-2.1.287-typed-while-running", "claude"), ("codex-0.159.3-typed-while-running", "codex")):
        real, cursor = capture(name)
        assert not composer.read(cursor_client, real, cursor, allow_busy=True).ready, name


def test_running_with_the_cursor_or_the_composer_in_the_wrong_place_is_not_ready_even_with_allow_busy():
    running = codex(f"› {DIM}Ask Codex{RESET}", above="• Working (3s • esc to interrupt)")
    row = next(index for index, line in enumerate(running.split("\n")) if line.startswith("›"))
    assert composer.read("codex", running, (2, row, True), allow_busy=True).ready
    assert composer.read("codex", running, (2, row + 2, True), allow_busy=True).reason == "cursor_elsewhere"
    assert composer.read("codex", running, (30, row, True), allow_busy=True).reason == "cursor_moved"


@pytest.mark.parametrize("client,screen,state", [
    # Running with no input box at all: nothing to type into.
    ("codex", "\n".join(["  Working through the plan.", "", "  • Working (12s • esc to interrupt)"]), "busy"),
    ("claude", "\n".join(["  Some output", "", "  ✻ Thinking… (esc to interrupt)"]), "busy"),
    ("codex", "user@host:~$ \n", "unrecognized"),
    # Menus and approval prompts win over any running chrome.
    ("codex", "\n".join(["  Would you like to run the following command?", "  $ rm -rf build", "",
                         "› 1. Yes, proceed (y)", "  2. No (esc)", "  • Working (3s • esc to interrupt)"]), "modal"),
    ("codex", codex(f"› {DIM}Ask Codex{RESET}", above="• Working (3s • esc to interrupt)", footer="  Enter to confirm · Esc to cancel"), "modal"),
    ("claude", claude("❯ 1. Yes", footer="  Do you want to proceed? · esc to interrupt"), "modal"),
    ("claude", claude(f"> {DIM}Try{RESET}", footer="  Do you want to proceed? (y/n) · esc to interrupt"), "modal"),
])
def test_running_without_a_usable_composer_a_dialog_or_a_menu_is_not_ready_even_with_allow_busy(client, screen, state):
    reading = composer.read(client, screen, allow_busy=True)
    assert (reading.state, reading.ready) == (state, False), reading


def test_an_unrecognized_claude_layout_stays_unrecognized_while_running():
    # A prompt marker that is not inside Claude's input box is a quote, not a composer.
    screen = "\n".join(["  ✻ Thinking… (esc to interrupt)", "> a markdown quote", "  more"])
    assert composer.read("claude", screen, allow_busy=True).reason == "no_input_box"


@pytest.mark.parametrize("client,screen", [
    ("claude", claude(f"❯ {INVERSE} {RESET}", footer="  enter to interrupt and send · ctrl+x then enter to queue")),
    ("claude", "\n".join(["  ✻ Thinking… · enter to interrupt and send", *claude(f"❯ {INVERSE} {RESET}").split("\n")[3:]])),
    ("codex", codex(f"› {DIM}Ask Codex{RESET}", above="• Working (3s • esc to interrupt) — enter to interrupt and send")),
])
def test_a_client_whose_submit_key_would_interrupt_the_turn_is_never_ready(client, screen):
    reading = composer.read(client, screen, allow_busy=True)
    assert (reading.state, reading.reason, reading.ready) == ("busy", "enter_interrupts", False)
    # Without the opt-in the same screen is simply a running turn: the interrupt hint is never taken for an idle composer.
    assert (composer.read(client, screen).state, composer.read(client, screen).ready) == ("busy", False)


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_the_exact_paste_in_a_running_composer_is_verified_with_allow_busy_and_otherwise_still_busy(client):
    rows = MULTILINE.split("\n")
    running = talk(client, ["  Done."], rows, footer="  ✻ Working… (1s • esc to interrupt)")
    assert composer.read(client, running, None, expect=MULTILINE).state == "busy"
    reading = composer.read(client, running, None, expect=MULTILINE, allow_busy=True)
    assert (reading.state, reading.reason, reading.busy) == ("pasted", "paste_in_composer", True)
    # Only the exact literal counts: busy chrome never turns other composer content into a verified paste.
    other = talk(client, ["  Done."], ["something else entirely"], footer="  ✻ Working… (1s • esc to interrupt)")
    assert composer.read(client, other, None, expect=MULTILINE, allow_busy=True).reason == "paste_not_in_composer"
    # Nor does it mask a client whose submit key interrupts.
    interrupting = talk(client, ["  Done."], rows, footer="  enter to interrupt and send · ctrl+x then enter to queue")
    assert composer.read(client, interrupting, None, expect=MULTILINE, allow_busy=True).reason == "enter_interrupts"


def test_a_real_claude_paste_into_the_running_composer_is_the_pasted_literal():
    screen, cursor = capture("claude-2.1.287-pasted-while-queued")
    reading = composer.read("claude", screen, cursor, expect="folded message two", allow_busy=True)
    assert (reading.state, reading.reason) == ("pasted", "paste_in_composer")
    assert composer.read("claude", screen, cursor, expect="a different message", allow_busy=True).state == "text"
