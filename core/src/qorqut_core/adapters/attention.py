"""Pure, pinned native form observations. Never composer/input permission."""

from dataclasses import dataclass
import re
from typing import Literal

from ..runner.models import TerminalSnapshot
from .input_layout import SGR, _lines


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


def classify_attention(provider: str, version: str, snapshot: TerminalSnapshot) -> AttentionVerdict:
    unknown = AttentionVerdict("unknown")
    if (provider, version) != ("codex", "0.160.0"):
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
