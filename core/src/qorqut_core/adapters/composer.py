"""Conservative offline classification of synthetic and captured composer layouts.

Neither a synthetic layout nor a saved native snapshot authorizes live paste.
"""

import hashlib
import json
import re

from .capabilities import VERSIONS
from .types import ComposerVerdict, Provider

SGR = re.compile(r"\x1b\[[0-9;:]*m")
RULE = re.compile(r"\s*[─━═-]{8,}\s*\Z")
BUSY = ("esc to interrupt", "ctrl+c to interrupt", "tab to queue", "enter to interrupt")
DIALOG = ("enter to confirm", "esc to cancel", "sign in", "log in", "do you trust", "select model",
    "hooks need review")
CLAUDE_FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


def _prompt_tail(provider: Provider, line: str) -> str | None:
    # Consume visible prompt cells, not raw byte/character offsets into SGR.
    glyph = r"›" if provider == "codex" else r"[>❯]"
    separator = " " if provider == "codex" else "[ \xa0]"
    match = re.match(rf"^(?:{SGR.pattern})*{glyph}(?:{SGR.pattern})*{separator}", line)
    return line[match.end():] if match else None


def _styled_text(tail, text, style):
    # SGR around the cells may vary; the entire content must have the observed
    # style and a following reset. A reset inside the text cannot pass.
    return bool(re.fullmatch(
        rf"(?:{SGR.pattern})*\x1b\[{style}m{re.escape(text)}\x1b\[0m(?:{SGR.pattern})*", tail))


def _native_layout(provider, lines, plain, prompts, cursor):
    """Only the observed 120-column, 40-row lower composer geometry."""
    row = 37 if provider == "claude" else 36
    if [i for i in prompts if i >= 34] != [row]:
        return "unknown", "ambiguous_or_misplaced_composer"
    region = "\n".join(plain[34:]).lower()
    if any(word in region for word in DIALOG):
        return "blocked", "dialog_or_setup"
    if any(word in region for word in BUSY):
        return "busy", "turn_running"
    tail = _prompt_tail(provider, lines[row])
    if provider == "claude":
        if plain[36] != "─" * 120 or plain[38] != "─" * 120:
            return "unknown", "no_known_input_box"
        if plain[39] != CLAUDE_FOOTER:
            return "unknown", "unknown_footer_or_multiline_input"
        if plain[row] != "❯\xa0 " or not _styled_text(tail, " ", 7):
            return "blocked", "composer_contains_text_or_unknown_cursor_cell"
        expected_cursor = (2, row, False)
    else:
        model, separator, path = plain[38].removeprefix("  ").partition(" · ")
        if (plain[37] or len(plain[38]) != 120 or not plain[38].startswith("  ")
                or not separator or not model.strip() or not path.strip()
                or not path.startswith(("/", "~/")) or " · " in path
                or plain[39] != "  ? for shortcuts"):
            return "unknown", "unknown_footer_or_multiline_input"
        if not _styled_text(tail, "Ask Codex to do anything", 2):
            return "blocked", "composer_contains_text_or_unknown_placeholder"
        expected_cursor = (2, row, True)
    if cursor != expected_cursor:
        return "unknown", "cursor_not_at_empty_composer"
    return "empty", "native_snapshot_empty_layout_live_unverified"


def classify(provider: Provider, version: str, raw: str,
             cursor: tuple[int, int, bool] | None = None) -> ComposerVerdict:
    fingerprint = hashlib.sha256(json.dumps([provider, version, raw, cursor], ensure_ascii=False).encode()).hexdigest()
    known = VERSIONS.get(provider) == version

    def result(state, reason, evidence=None):
        return ComposerVerdict(state=state, reason=reason, fingerprint=fingerprint,
            evidence=evidence or ("synthetic" if known else "unsupported"))

    if not known:
        return result("unknown", "unsupported_version")
    lines = raw.splitlines()
    plain = [SGR.sub("", line) for line in lines]
    if any("\x1b" in line for line in plain):
        return result("unknown", "unsupported_escape_sequence")
    prompts = [i for i, line in enumerate(lines) if _prompt_tail(provider, line) is not None]
    if not prompts:
        bottom = "\n".join(plain[-6:]).lower()
        if any(word in bottom for word in DIALOG):
            return result("blocked", "dialog_or_setup")
        if any(word in bottom for word in BUSY):
            return result("busy", "turn_running")
        return result("unknown", "no_known_composer")
    if len(lines) == 40:
        state, reason = _native_layout(provider, lines, plain, prompts, cursor)
        return result(state, reason, "native_snapshot" if state == "empty" else None)
    row = prompts[-1]
    if len(prompts) > 1 and row - prompts[-2] <= 1:
        return result("unknown", "ambiguous_composer")
    if provider == "claude" and not (row > 0 and RULE.fullmatch(plain[row - 1])
            and row + 1 < len(lines) and RULE.fullmatch(plain[row + 1])):
        return result("unknown", "no_known_input_box")
    content = plain[row][2:]
    if re.match(r"\d+[.)]\s", content):
        return result("blocked", "selection_dialog")
    below = "\n".join(plain[row + 1:]).lower()
    if any(word in below for word in DIALOG):
        return result("blocked", "dialog_or_setup")
    above_start = row - 1 if provider == "claude" else row
    chrome = "\n".join(plain[max(0, above_start - 2):above_start]).lower() + "\n" + below
    if any(word in chrome for word in BUSY):
        return result("busy", "turn_running")
    # Only this exact dim/reset placeholder convention has fixture evidence.
    raw_tail = _prompt_tail(provider, lines[row])
    placeholder = bool(re.fullmatch(r"\x1b\[2m[^\x1b\r\n]*\x1b\[0m", raw_tail))
    if content.strip() and not placeholder:
        return result("blocked", "composer_contains_text")
    footer = [line.strip() for line in plain[row + 1:] if line.strip() and not RULE.fullmatch(line)]
    if len(footer) != 1 or not re.fullmatch(r"\? for shortcuts(?:\s+\d+% context left)?", footer[0]):
        return result("unknown", "unknown_footer_or_multiline_input")
    if cursor is None or cursor != (2, row, True):
        return result("unknown", "cursor_not_at_empty_composer")
    return result("empty", "synthetic_empty_layout_native_unverified")
