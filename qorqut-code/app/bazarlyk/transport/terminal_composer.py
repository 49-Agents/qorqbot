"""Client-specific recognition of an ordinary, empty input composer from a styled tmux screen capture.

Owner input is typed into an agent terminal only when the screen positively shows an idle, empty composer of the
verified client (or, for the one caller that opts in below, an empty composer under a running turn). Anything else (a
modal or approval dialog, a running turn, unsent text, a login or trust prompt, a layout this module does not know) is not
"ready", and the caller queues instead of typing. Recognition reads
``tmux capture-pane -p -e`` output, so dim placeholder text is told apart from text somebody typed.

The recognizers encode the composer conventions of Codex (a ``›`` prompt line) and Claude Code (a ``>``/``❯`` prompt
line inside horizontal rules). They are exercised against hand-built fixtures in the tests and have not been validated
against every client release: a client that draws its composer differently fails closed (``unrecognized``) and its
input stays queued. ``python -m bazarlyk.telegram composer-check`` classifies a saved capture offline (and, with
``--pasted-file``, a saved capture taken after a paste) so an owner can verify a client version without any input being
typed.

A caller may opt in with ``allow_busy`` (Terminal Chat only): a recognized running turn then does not by itself make the
composer unavailable. The same composer, cursor, menu and dialog validation still runs, and only an empty, editable composer
that is verified under the running chrome reads as ready (with ``Reading.busy`` set). A running turn with no input box, a
dialog or menu, text already typed, or a client whose submit key would interrupt the turn stays not ready.

The composer is located first, from the prompt marker, the input box and the cursor. Only then are the rows that are
client chrome read for busy or dialog indicators: the rows below the composer and the few rows directly above it. The
composer's own content and the conversation above it are never searched for indicator wording, so an owner message or an
earlier answer that mentions "sign in" or "do you want to" cannot look like a dialog. After a paste, the same composer must
show the exact literal that was pasted (or the client's paste placeholder for it); a dialog is never inferred from wording.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

CLIENTS = ("codex", "claude")
REGION_ROWS = 14
SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-ln-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

MARKERS = {
    "codex": re.compile(r"^(?P<lead>\s*)(?P<marker>›)(?P<gap> ?)"),
    # Claude Code draws a no-break space after "❯" (the queued-message placeholder is the case that shows it).
    "claude": re.compile(r"^(?P<lead>\s*(?:[│|]\s*)?)(?P<marker>[>❯])(?P<gap>[ \xa0]?)"),
}
MENU_ITEM = re.compile(r"^\s*\d+[.)]\s")
RULE = re.compile(r"^\s*[╭╰│]?[─━═-]{8,}")
# Wording of dialogs and menus. Searched only in the chrome rows below the composer, never in typed text or the transcript.
MODAL_PHRASES = (
    "do you trust", "trust the contents", "trust this folder", "do you want to", "would you like to", "yes, proceed",
    "yes, and don", "yes, allow", "don't ask again", "(y/n)", "[y/n]", "press enter to", "enter to confirm",
    "enter to select", "esc to cancel", "esc to go back", "select model", "choose a model", "sign in", "log in",
    "please login", "api key", "update available", "update now", "resume a previous", "session expired",
)
# Keystroke hints a client shows only while a turn runs, and the spinner-line shape ("✻ Thinking… (12s)").
BUSY_HINTS = ("esc to interrupt", "ctrl+c to interrupt", "tab to queue", "esc again to")
# A client in a mode where the ordinary submit key stops the running turn (Claude Code's opt-in responsive mode shows
# "enter to interrupt and send"). Typing there would interrupt the agent, so an opted-in busy composer is not ready.
INTERRUPT_HINTS = ("enter to interrupt",)
BUSY_STATUS = re.compile(r"^\W*(?:working|thinking|compacting|running|processing)\b.*(?:\(|…|\.\.\.)", re.IGNORECASE)
PLACEHOLDER = re.compile(r"^\[Pasted[^\]\n]{0,80}\]$", re.IGNORECASE)
PLACEHOLDER_COUNT = re.compile(r"\+\s*(\d+)\s+lines?|(\d+)\s+chars?", re.IGNORECASE)
STATUS_ROWS_ABOVE = 2
FOOTER_PHRASES = (
    "for shortcuts", "context left", "context used", "shift+tab", "ctrl+", "esc to", "tab to", "accept edits",
    "plan mode", "bypass permissions", "auto-accept", "tokens", "/status", "? for",
)
# Codex's configurable status line (``  <model> · <directory>``), drawn in explicit 256-colour/RGB tints below the
# composer and its blank separator row. Its colours are not the gray "dim" range, so it is recognized by shape instead.
STATUS_SEPARATOR = " \u00b7 "


@dataclass(frozen=True)
class Reading:
    """``state`` is empty (ready), text, busy, modal or unrecognized; ``reason`` names the first thing that decided it."""

    state: str
    reason: str
    fingerprint: str
    # A running turn was recognized beside the composer. Only an opted-in read (``allow_busy``) can be ready while busy.
    busy: bool = False
    literal_paste: bool = False

    @property
    def ready(self):
        return self.state == "empty"


def fingerprint(raw, cursor):
    return hashlib.sha256((raw + "\0" + repr(cursor)).encode("utf-8", "replace")).hexdigest()


def is_dim(state):
    if state["dim"]:
        return True
    colour = state["fg"]
    if colour == 90:
        return True
    if isinstance(colour, tuple):
        if colour[0] == "idx":
            return colour[1] == 8 or 238 <= colour[1] <= 250
        if colour[0] == "rgb":
            low, high = min(colour[1:]), max(colour[1:])
            return high - low <= 16 and 70 <= high <= 190
    return False


def _apply_sgr(state, parameters):
    values = [int(item) if item.isdigit() else 0 for item in parameters.replace(":", ";").split(";")] if parameters else [0]
    index = 0
    while index < len(values):
        code = values[index]
        index += 1
        if code == 0:
            state.update(dim=False, inverse=False, fg=None)
        elif code == 2:
            state["dim"] = True
        elif code == 22:
            state["dim"] = False
        elif code == 7:
            state["inverse"] = True
        elif code == 27:
            state["inverse"] = False
        elif 30 <= code <= 37 or 90 <= code <= 97:
            state["fg"] = code
        elif code == 39:
            state["fg"] = None
        elif code in (38, 48):
            kind = values[index] if index < len(values) else None
            if kind == 5 and index + 1 < len(values):
                if code == 38:
                    state["fg"] = ("idx", values[index + 1])
                index += 2
            elif kind == 2 and index + 3 < len(values):
                if code == 38:
                    state["fg"] = ("rgb", *values[index + 1:index + 4])
                index += 4
            else:
                index = len(values)


def parse(raw):
    """Rows of ``(character, style)`` cells with the attributes carried across the capture; style is dim/inverse/plain."""
    state = {"dim": False, "inverse": False, "fg": None}
    rows = []
    for line in raw.split("\n"):
        cells = []
        position = 0
        for match in SGR.finditer(line):
            cells.extend((char, _style(state)) for char in OTHER_ESCAPES.sub("", line[position:match.start()]))
            _apply_sgr(state, match.group(1))
            position = match.end()
        cells.extend((char, _style(state)) for char in OTHER_ESCAPES.sub("", line[position:]))
        while cells and cells[-1][0] == " " and cells[-1][1] != "inverse":
            cells.pop()
        rows.append(cells)
    return rows


def _style(state):
    """``inverse``, ``dim``, ``tinted`` (an explicit, non-gray foreground colour) or ``plain`` (default colour).

    ``tinted`` is treated exactly like ``plain`` (typed text) everywhere except the Codex status line rule below."""
    if state["inverse"]:
        return "inverse"
    if is_dim(state):
        return "dim"
    return "plain" if state["fg"] is None else "tinted"


def _text(cells):
    return "".join(char for char, _ in cells)


def _blank(cells):
    return not _text(cells).strip()


def _contains(text, phrases):
    lowered = text.lower()
    return next((phrase for phrase in phrases if phrase in lowered), None)


def _squash(text):
    return re.sub(r"\s+", "", text)


def _chrome(cells):
    """A row that is client chrome, not typed content: blank, a horizontal rule, or drawn entirely in dim/inverse styling."""
    text = _text(cells)
    return not text.strip() or bool(RULE.match(text)) or all(style in ("dim", "inverse") for char, style in cells if char.strip())


def _footer(cells):
    return _chrome(cells) or bool(_contains(_text(cells), FOOTER_PHRASES))


def _codex_status_line(cells):
    """Codex's status line: indented, ``<label> · <path>``-shaped, with no default-coloured character and some tint.

    Text typed or pasted into the composer is drawn in the default colour, so it never has this shape."""
    text = _text(cells)
    visible = [style for char, style in cells if char.strip()]
    return (text[:1].isspace() and STATUS_SEPARATOR in text and "plain" not in visible and "tinted" in visible)


def _trailing_chrome(client, rows, start):
    """Whether every row from ``start`` to the bottom is client chrome. For Codex this includes its status line, but only
    once a blank row has separated it from the composer (the composer's own rows end before that blank row)."""
    separated = False
    for index in range(start, len(rows)):
        cells = rows[index]
        if _blank(cells):
            separated = True
        elif not (_footer(cells) or (client == "codex" and separated and _codex_status_line(cells))):
            return False
    return True


def _busy_row(text):
    return bool(_contains(text, BUSY_HINTS) or BUSY_STATUS.match(text.strip()))


def _content(text, match, first):
    """One composer row's typed content: after the prompt marker on the first row, without box bars on the others."""
    text = text[match.end():] if first else text
    return text.strip().strip("│|").strip()


def _placeholder_fits(content, text):
    """Whether ``content`` is the client's paste placeholder and any count it states fits the pasted text."""
    if not PLACEHOLDER.match(content):
        return False
    counted = PLACEHOLDER_COUNT.search(content)
    if counted is None:
        return True
    if counted.group(1) is not None:
        return text.count("\n") <= int(counted.group(1)) <= text.count("\n") + 2
    return abs(int(counted.group(2)) - len(text)) <= 2


def _holds_paste(client, rows, first, text, cursor):
    """The last row of the composer holding exactly ``text`` from the prompt row ``first``, else None.

    The composer's rows are consumed against the pasted literal (whitespace and wrapping ignored); afterwards only client
    chrome may remain. A placeholder counts only as the entire composer content. Wording is never interpreted.
    """
    marker = MARKERS[client]
    match = marker.match(_text(rows[first]))
    end = None
    head = _content(_text(rows[first]), match, True)
    if PLACEHOLDER.match(head):
        end = first if _placeholder_fits(head, text) else None
    else:
        want, seen = _squash(text), ""
        for index in range(first, len(rows)):
            if seen == want:
                break
            raw = _text(rows[index])
            if index > first and RULE.match(raw):
                break
            seen += _squash(_content(raw, match, index == first))
            if not want.startswith(seen):
                return None
            end = index
        if seen != want:
            return None
    if end is None or not _trailing_chrome(client, rows, end + 1):
        return None
    if cursor and cursor[2] and not first <= cursor[1] <= end:
        return None
    return end


def _prompt_rows(client, rows, region):
    marker = MARKERS[client]
    return [index for index in region if marker.match(_text(rows[index]))]


def _interrupts(texts):
    return any(_contains(text, INTERRUPT_HINTS) for text in texts)


def read(client, raw, cursor=None, expect=None, allow_busy=False):
    """Classify a styled capture. ``cursor`` is ``(x, y, visible)`` from tmux, or None when unknown.

    ``expect`` is text that was just pasted: the reading is then ``pasted`` only when that exact text (or its placeholder)
    occupies the composer, and otherwise never ``empty``. Without it, ``empty`` means an idle composer with nothing typed.

    ``allow_busy`` lets a running turn coexist with a ready composer: the busy indicators no longer end the reading early,
    so the composer content and cursor are still validated, and an empty editable composer is ``empty`` with ``busy`` set
    (and an exact paste is ``pasted``). Dialogs, menus, typed text, a missing input box and a submit key that would
    interrupt the turn keep their own, not ready, readings.
    """
    digest = fingerprint(raw, cursor)

    def result(state, reason, busy=False, literal_paste=False):
        return Reading(state, reason, digest, busy, literal_paste)

    if client not in CLIENTS:
        return result("unrecognized", "unsupported_client")
    rows = parse(raw)
    filled = [index for index, cells in enumerate(rows) if not _blank(cells)]
    if not filled:
        return result("unrecognized", "blank_screen")
    last = filled[-1]
    region = range(0 if expect is not None else max(0, last - REGION_ROWS + 1), last + 1)
    candidates = _prompt_rows(client, rows, region)
    if expect is not None:
        return _pasted(client, rows, candidates, expect, cursor, result, allow_busy)
    if not candidates:
        # No composer at all: the bottom rows may still show why (a dialog or a running turn) for the reason we report.
        bottom = "\n".join(_text(rows[index]) for index in filled[-6:])
        if _contains(bottom, MODAL_PHRASES):
            return result("modal", "modal_dialog")
        if any(_busy_row(_text(rows[index])) for index in filled[-6:]):
            return result("busy", "turn_running")
        return result("unrecognized", "no_composer")
    marker = MARKERS[client]
    row = candidates[-1]
    match = marker.match(_text(rows[row]))
    content_start = match.end()
    cells = rows[row]
    if MENU_ITEM.match(_text(cells)[match.end("marker"):]) or MENU_ITEM.match(_text(cells)[content_start:]):
        return result("modal", "selection_menu")
    if len(candidates) > 1 and row - candidates[-2] <= 1:
        return result("unrecognized", "ambiguous_composer")
    top = row - 1 if client == "claude" and row > 0 and RULE.match(_text(rows[row - 1])) else None
    if client == "claude" and top is None and not (row + 1 < len(rows) and RULE.match(_text(rows[row + 1]))):
        return result("unrecognized", "no_input_box")
    # Chrome only: the rows below the composer, and the rows directly above it (where a turn's status line is drawn).
    below = [_text(rows[index]) for index in range(row + 1, len(rows))]
    above_from = (top if top is not None else row)
    above = [_text(rows[index]) for index in range(max(0, above_from - STATUS_ROWS_ABOVE), above_from)]
    if _contains("\n".join(below), MODAL_PHRASES):
        return result("modal", "modal_dialog")
    # Rows above the composer sit next to the conversation, so only a keystroke hint (never a sentence shaped like a spinner
    # line) can mark a turn as running there; the footer rows below can also carry the spinner line itself.
    interrupts = _interrupts(above + below)
    running = interrupts or any(_contains(text, BUSY_HINTS) for text in above) or any(_busy_row(text) for text in below)
    if running and not allow_busy:
        return result("busy", "turn_running")
    if interrupts:
        return result("busy", "enter_interrupts", True)
    # The composer line's own content: dim placeholder text and the cursor cell are not typed text.
    tail = cells[content_start:]
    while tail and tail[-1][0] in "│|":
        tail = tail[:-1]
    for offset, (char, style) in enumerate(tail):
        if char.strip() and style != "dim" and not (style == "inverse" and offset == 0):
            return result("text", "composer_not_empty", running)
    visible = bool(cursor and cursor[2])
    if visible:
        x, y, _ = cursor
        if y != row:
            return result("text", "cursor_elsewhere", running)
        if abs(x - content_start) > 1:
            return result("text", "cursor_moved", running)
    else:
        if not _trailing_chrome(client, rows, row + 1):
            return result("text", "continuation_text", running)
    return result("empty", "empty_composer_running" if running else "empty_composer", running)


def _pasted(client, rows, candidates, expect, cursor, result, allow_busy=False):
    """Whether ``expect`` occupies the composer: try each prompt row from the bottom up until one holds exactly the paste.

    A running turn beside the composer makes the reading ``busy`` unless ``allow_busy``: then the exact paste in the
    composer is ``pasted`` (flagged ``busy``), unless the client's submit key would interrupt the turn."""
    for first in reversed(candidates):
        if client == "claude" and not (first > 0 and RULE.match(_text(rows[first - 1]))):
            continue
        end = _holds_paste(client, rows, first, expect, cursor)
        if end is None:
            continue
        trailing = [_text(rows[index]) for index in range(end + 1, len(rows))]
        above = [_text(rows[index]) for index in range(max(0, first - STATUS_ROWS_ABOVE), first)]
        interrupts = _interrupts(above + trailing)
        running = interrupts or any(_busy_row(text) for text in trailing) or any(_contains(text, BUSY_HINTS) for text in above)
        if running and not allow_busy:
            return result("busy", "turn_running")
        if interrupts:
            return result("busy", "enter_interrupts", True)
        head = _content(_text(rows[first]), MARKERS[client].match(_text(rows[first])), True)
        return result("pasted", "paste_in_composer", running, literal_paste=not PLACEHOLDER.match(head))
    return result("text", "paste_not_in_composer")
