"""Pure, pinned native form observations. Never composer/input permission."""

from dataclasses import dataclass
import re
from typing import Literal

from ..runner.models import TerminalSnapshot
from .input_layout import SGR, _lines, classify_input


# Claude Code 2.1.287's AskUserQuestion form: observed 256-colour (foreground, background, bold).
GRAY, TAB, ASK, ARROW = (246, None, False), (16, 153, False), (231, None, True), (153, None, False)
CLAUDE_ACTIONS = "Enter to select · ↑/↓ to navigate · Esc to cancel"
CLAUDE_OPTION = re.compile(r"(?:❯| ) ([1-9])\. (\S.*)")


@dataclass(frozen=True)
class AttentionVerdict:
    state: Literal["waiting", "none", "unknown"]
    kind: Literal["approval", "question"] | None = None
    prompt: str | None = None
    truncated: bool = False


def _reverse(row):
    return any("7" in match.group()[2:-1].split(";") for match in SGR.finditer(row))


def _prompt(lines):
    # Selection changes only decoration and the leading arrow, not the prompt.
    text = "\n".join(re.sub(r"^› (?=[0-9]+\. )", "", line.strip()) for line in lines).strip()
    return text[:6000], len(text) > 6000


def _options(raw, plain, start, end):
    choices = [(i, re.fullmatch(r"  (› |  )([1-4])\. (.+?) *", plain[i]))
               for i in range(start, end)]
    choices = [(i, match) for i, match in choices if match]
    if (len(choices) < 2 or [int(match[2]) for _, match in choices] != list(range(1, len(choices) + 1))
            or sum(match[1] == "› " for _, match in choices) != 1
            or not all(_reverse(raw[i]) for i, match in choices if match[1] == "› ")):
        return []
    return choices


def _question(raw, plain, end):
    # The pinned renderer wraps whole footer actions onto subsequent rows.
    footer_start = end
    while footer_start > 0 and plain[footer_start - 1].strip():
        footer_start -= 1
    footer = " | ".join(line.strip() for line in plain[footer_start:end + 1])
    ordinary = re.fullmatch(r"tab to add notes \| enter to submit (answer|all)"
        r"(?: \| ←/→ to navigate questions)? \| esc to interrupt", footer)
    notes = re.fullmatch(r"tab or esc to clear notes \| enter to submit (answer|all)", footer)
    if not (ordinary or notes) or not raw[footer_start].startswith("  \x1b[1mtab\x1b[0m"):
        # Notes uses a dim reset after the first bold key.
        if not notes or not raw[footer_start].startswith("  \x1b[1mtab\x1b[0;2m"):
            return None
    headers = [(i, re.fullmatch(r"  Question ([1-3])/([1-3]) \(([0-3]) unanswered\)", plain[i]))
               for i in range(footer_start) if "\x1b[2mQuestion " in raw[i]]
    headers = [(i, match) for i, match in headers if match]
    if not headers:
        return None
    start, header = headers[-1]
    if (not 1 <= int(header[1]) <= int(header[2]) or int(header[3]) > int(header[2])
            or not raw[start + 1].startswith("  \x1b[38;5;75m")):
        return None
    stop = footer_start
    if notes:
        inputs = [i for i in range(start + 1, stop) if raw[i].startswith("  \x1b[1m›\x1b[0m ")]
        if len(inputs) != 1:
            return None
        stop = inputs[0]  # Typed notes are an answer, never part of the prompt.
    choices = _options(raw, plain, start + 1, stop)
    if not choices or not choices[-1][1][3].startswith("None of the above  "):
        return None
    return _prompt(plain[start + 1:stop])


def _sgr(style, sequence):
    fg, bg, bold = style
    params = sequence[2:-1].split(";")
    while params:
        param = params.pop(0)
        if param in ("", "0"):
            fg, bg, bold = None, None, False
        elif param in ("1", "22"):
            bold = param == "1"
        elif param == "39":
            fg = None
        elif param == "49":
            bg = None
        elif (param in ("38", "48") and len(params) > 1 and params[0] == "5"
                and params[1].isdecimal() and int(params[1]) < 256):
            fg, bg = (int(params[1]), bg) if param == "38" else (fg, int(params[1]))
            del params[:2]
        else:
            return None  # Only the sequences this form is observed to use are interpreted.
    return fg, bg, bold


def _paint(rows):
    """(character, style) cells of rows starting from the default style; tmux carries SGR across rows."""
    style, painted = (None, None, False), []
    for row in rows:
        cells = []
        codes = SGR.findall(row)
        for i, text in enumerate(SGR.split(row)):
            if i:
                style = _sgr(style, codes[i - 1])
                if style is None:
                    return None
            cells += [(char, style) for char in text]
        painted.append(cells)
    return painted


def _styled(cells, style, spaces=False):
    shown = [cell for char, cell in cells if spaces or char != " "]
    return bool(shown) and all(cell == style for cell in shown)


def _claude_question(raw, plain, end, snapshot):
    """The single-question form at the bottom of the pane; the hidden cursor sits inside it."""
    rule, lower = "─" * snapshot.columns, end - 3
    top = next((i for i in range(lower - 1, -1, -1) if plain[i] == rule), -1)
    if (lower < 7 or top < 0 or plain[lower] != rule or plain[end] != CLAUDE_ACTIONS or plain[end - 1]
            or snapshot.cursor_visible or not top <= snapshot.cursor_y <= end):
        return None
    cells = _paint(raw[top:end + 1])
    if cells is None:
        return None
    painted = dict(zip(range(top, end + 1), cells))
    if not (_styled(painted[top], GRAY) and _styled(painted[lower], GRAY) and _styled(painted[end], GRAY)
            and re.fullmatch(r" ☐ \S(?:.*\S)? ", plain[top + 1]) and _styled(painted[top + 1], TAB, True)
            and not plain[top + 2]):
        return None
    start = stop = top + 3
    while _styled(painted[stop], ASK):
        stop += 1
    if stop == start or plain[stop]:
        return None
    labels, numbers, selected = {}, [], 0
    for i in [*range(stop + 1, lower), end - 2]:
        option = CLAUDE_OPTION.fullmatch(plain[i])
        if option:
            labels[i] = option[2]
            numbers.append(int(option[1]))
            if plain[i][0] == "❯":
                if painted[i][0][1] != ARROW:
                    return None
                selected += 1
        elif not (labels and plain[i].startswith("     ") and _styled(painted[i], GRAY)):
            return None  # Only dim description rows may follow an option.
    if (selected != 1 or len(numbers) < 4 or numbers != list(range(1, len(numbers) + 1))
            or (labels.get(lower - 1), labels.get(end - 2)) != ("Type something.", "Chat about this")):
        return None
    # Selection changes only the arrow; history, tab, rule and action footer are not the prompt.
    return _prompt([re.sub(r"^❯", " ", line) for line in plain[start:lower] + [plain[end - 2]]])


def classify_attention(provider: str, version: str, snapshot: TerminalSnapshot) -> AttentionVerdict:
    unknown = AttentionVerdict("unknown")
    if (provider, version) not in (("codex", "0.160.0"), ("claude", "2.1.287")):
        return unknown
    if (not 54 <= snapshot.columns <= 500 or not 24 <= snapshot.rows <= 250
            or not 0 <= snapshot.cursor_x < snapshot.columns or not 0 <= snapshot.cursor_y < snapshot.rows
            or snapshot.in_mode or snapshot.input_off or snapshot.synchronized):
        return unknown
    decoded = _lines(snapshot.screen)
    if decoded is None:
        return unknown
    raw, plain = decoded
    if len(plain) != snapshot.rows:
        return unknown
    end = next((i for i in range(len(plain) - 1, -1, -1) if plain[i].strip()), -1)
    if end < 0:
        return unknown
    if provider == "claude":
        question = _claude_question(raw, plain, end, snapshot)
        if question is not None:
            return AttentionVerdict("waiting", "question", *question)
        # Only the known empty composer shows that the form is gone; other layouts stay unknown.
        return AttentionVerdict("none") if classify_input(provider, version, snapshot).state == "empty" else unknown
    footer = plain[end]
    question = _question(raw, plain, end)
    if question is not None:
        return AttentionVerdict("waiting", "question", *question)
    if (footer == "  Press enter to confirm or esc to go back"
            and raw[end].startswith("  Press \x1b[1menter\x1b[0m")):
        starts = [i for i in range(end) if raw[i] == "  \x1b[1mSubmit with unanswered questions?\x1b[0m"]
        choices = _options(raw, plain, starts[-1] + 1, end) if starts else []
        if (len(choices) == 2 and choices[0][1][3].startswith("Proceed  ")
                and choices[1][1][3].startswith("Go back  ")):
            return AttentionVerdict("waiting", "question", *_prompt(plain[starts[-1]:end]))
        return unknown
    if footer != "  enter to submit | esc to cancel":
        if any(_reverse(raw[i]) and "› " in plain[i] for i in range(max(0, end - 24), end)) \
                and ("enter" in footer or "esc" in footer):
            return unknown
        return AttentionVerdict("none")
    # Native forms have decorated action keys. A prose quote of their footer
    # cannot establish an active form, even at the end of the pane.
    if not raw[end].startswith("  \x1b[1menter\x1b[0m to submit"):
        return unknown
    starts = [i for i in range(end) if plain[i] == "  Field 1/1"
              and "\x1b[2mField 1/1" in raw[i]]
    if not starts:
        return unknown
    start = starts[-1]
    body = plain[start + 1:end]
    choices = _options(raw, plain, start + 1, end)
    if ([match[2] for _, match in choices] != ["1", "2", "3", "4"]
            or not " ".join(body).lstrip().startswith("Allow the ")
            or "MCP server to run tool" not in " ".join(" ".join(body).split())
            or not all(match[3].startswith(label) for (_, match), label in zip(choices,
                ("Allow ", "Allow for this session ", "Always allow ", "Cancel ")))):
        return unknown
    prompt, truncated = _prompt(body)
    return AttentionVerdict("waiting", "approval", prompt, truncated)
