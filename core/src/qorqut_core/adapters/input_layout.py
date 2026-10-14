"""Pure observations of the pinned native composers; never input permission.

Short printable ASCII can be compared visibly. This does not establish exact
preservation of arbitrary message bodies or the identity/ownership of a pane.
"""

import hashlib
import json
import re
import unicodedata

from ..runner.models import TerminalSnapshot
from .composer import BUSY, DIALOG
from .types import ComposerVerdict, Provider

# Deliberately pinned independently of future preparation profiles.
VERSIONS = {"claude": "2.1.287", "codex": "0.160.0"}
SGR = re.compile(r"\x1b\[[0-9;:]*m")
# Observed prompt decoration: normal/bold and foreground color only. In
# particular, a dim or concealed prompt prefix cannot prove ordinary input.
PROMPT_SGR = r"\x1b\[(?:0|1|0;1|22|39|38;5;[0-9]{1,3})m"
OSC8 = re.compile(r"\x1b\]8;[^;\x00-\x1f\x7f-\x9f]*;[^\x00-\x1f\x7f-\x9f]*(?:\x07|\x1b\\)")
PROMPT = re.compile(r"^[❯›>][ \xa0]")
CLAUDE_FOOTER = re.compile(
    r"  ⏵⏵ (?:auto mode on|bypass permissions on) \(shift\+tab to cycle\)(?: · ← for agents)?")
# Observed after a turn: one promotional line under the mode footer, right-aligned
# with a two-column margin. It is not remote control being active.
CLAUDE_REMOTE_HINT = "control this session from your phone · /remote-control"
# Observed first-turn suggestion: inverted T, dim "ry", then dim words split by
# plain spaces. The words may vary; the SGR shape and the visible text may not.
CLAUDE_HINT = re.compile(
    r"\x1b\[7mT\x1b\[0;2mry\x1b\[0m \x1b\[2m[!-~]+\x1b\[0m(?: \x1b\[2m[!-~]+\x1b\[0m)*")
CLAUDE_HINT_TEXT = re.compile(r'Try "[^" ](?:[^"]*[^" ])?"')
# Observed post-turn suggestion: the first character inverted, the rest dim to the end
# of the row. The words are the model's own; only the SGR shape and ASCII row are fixed.
CLAUDE_SUGGESTION = re.compile(r"\x1b\[7m[!-~]\x1b\[0;2m[ -~]*[!-~]\x1b\[0m")
CODEX_WARNING = r"⚠ +[1-9][0-9]* +warnings? +· +f2 +to +view"
CODEX_EMPTY_FOOTER = re.compile(rf"  \? for shortcuts(?: +{CODEX_WARNING} *)?")
CODEX_INPUT_FOOTER = re.compile(rf"(?: +{CODEX_WARNING} *)?")


def _lines(screen: str):
    """Drop only OSC-8 delimiters; keep SGR for the exact cursor/placeholder."""
    kept = []
    position = 0
    while position < len(screen):
        char = screen[position]
        if char == "\x1b":
            sgr = SGR.match(screen, position)
            link = None if sgr else OSC8.match(screen, position)
            if sgr:
                kept.append(sgr.group())
                position = sgr.end()
                continue
            if link:
                # No URL interpretation, decoding or access. Unexpected Unicode
                # controls are rejected inside delimiters as well as outside.
                if any(unicodedata.category(c) in {"Cf", "Cs", "Zl", "Zp"} for c in link.group()):
                    return None
                position = link.end()
                continue
            return None
        if char != "\n" and unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            return None
        kept.append(char)
        position += 1
    # capture-pane terminates each captured row with LF. Remove at most that
    # final terminator, retaining actual blank rows (unlike strip/splitlines).
    raw = "".join(kept).removesuffix("\n").split("\n")
    return raw, [SGR.sub("", line) for line in raw]


def _tail(provider, line):
    glyph, separator = ("❯", "\xa0") if provider == "claude" else ("›", " ")
    match = re.match(rf"^(?:{PROMPT_SGR})*{glyph}(?:{PROMPT_SGR})*{separator}", line)
    return line[match.end():] if match else None


def _claude_hint(tail, columns):
    if tail is None:
        return False
    visible = SGR.sub("", tail)
    first_turn = CLAUDE_HINT.fullmatch(tail) and CLAUDE_HINT_TEXT.fullmatch(visible)
    return bool(first_turn or CLAUDE_SUGGESTION.fullmatch(tail)) and 2 + len(visible) <= columns


def _claude_rows_after_footer_clear(plain, start, columns):
    """Rows below the mode footer are blank, except the one exact known hint right after it."""
    rest = plain[start:]
    if rest and rest[0] == CLAUDE_REMOTE_HINT.rjust(columns - 2):
        rest = rest[1:]
    return not any(rest)


def _observe(provider, version, snapshot, expected):
    if VERSIONS.get(provider) != version or provider not in VERSIONS:
        return "unknown", "unsupported_version"
    minimum_columns = 54 if provider == "codex" else 80
    if (type(snapshot.columns) is not int or type(snapshot.rows) is not int
            or not minimum_columns <= snapshot.columns <= 500 or not 24 <= snapshot.rows <= 250):
        return "unknown", "unsupported_geometry"
    if (type(snapshot.cursor_x) is not int or type(snapshot.cursor_y) is not int
            or not 0 <= snapshot.cursor_x < snapshot.columns or not 0 <= snapshot.cursor_y < snapshot.rows
            or any(type(getattr(snapshot, flag)) is not bool for flag in
                   ("cursor_visible", "in_mode", "input_off", "synchronized"))):
        return "unknown", "malformed_cursor_or_flags"
    if snapshot.in_mode or snapshot.input_off or snapshot.synchronized:
        return "unknown", "pane_not_in_normal_input_mode"
    if not isinstance(snapshot.screen, str):
        return "unknown", "malformed_screen"
    decoded = _lines(snapshot.screen)
    if decoded is None:
        return "unknown", "unsupported_escape_or_control"
    raw, plain = decoded
    if len(raw) != snapshot.rows:
        return "unknown", "captured_row_count_mismatch"
    # Claude draws its box where its cursor is (it floats under short output);
    # Codex stays at the bottom. Both use the same local exclusion window.
    row = snapshot.cursor_y if provider == "claude" else snapshot.rows - 4
    start = max(0, row - 5) if provider == "claude" else snapshot.rows - 8
    region = "\n".join(plain[start:]).lower()
    if any(word in region for word in DIALOG):
        return "blocked", "dialog_or_setup"
    if any(word in region for word in BUSY):
        return "busy", "turn_running"
    # Claude's own output above the upper rule is history, even prompt-looking rows (a resumed
    # session replays its earlier `/exit`). Only the current box and what follows it compete;
    # busy/dialog words above are still scanned from `start`. Codex keeps its whole window.
    first = row if provider == "claude" else start
    if [i for i in range(first, snapshot.rows) if PROMPT.match(plain[i])] != [row]:
        return "unknown", "ambiguous_or_misplaced_composer"
    tail = _tail(provider, raw[row])
    if provider == "claude":
        footer = row + 2
        if (row < 1 or footer >= snapshot.rows or plain[row - 1] != "─" * snapshot.columns
                or plain[row + 1] != "─" * snapshot.columns):
            return "unknown", "no_known_input_box"
        if (not CLAUDE_FOOTER.fullmatch(plain[footer])
                or not _claude_rows_after_footer_clear(plain, footer + 1, snapshot.columns)):
            return "unknown", "unknown_footer_or_multiline_input"
        text = expected if expected is not None else ""
        # Each presentation is bound to its own cursor. Hidden: the text, then an inverted
        # cell (or, for an empty check only, a suggestion that proves an empty box, never a
        # pasted pointer). Visible, as in a resumed session: exactly the unstyled text, with
        # the terminal cursor itself directly after it.
        if tail == text + "\x1b[7m \x1b[0m" or (expected is None and _claude_hint(tail, snapshot.columns)):
            visible = False
        elif tail == text:
            visible = True
        else:
            return "blocked", "composer_contains_text_or_unknown_cursor_cell"
    else:
        model, separator, path = plain[-2][2:].partition(" · ")
        footer = CODEX_EMPTY_FOOTER if expected is None else CODEX_INPUT_FOOTER
        if (plain[row + 1] != "" or not plain[-2].startswith("  ")
                or not model.strip() or not separator or not path.startswith(("/", "~/"))
                or " · " in path or len(plain[-2]) > snapshot.columns
                or len(plain[-1]) > snapshot.columns or not footer.fullmatch(plain[-1])):
            return "unknown", "unknown_footer_or_multiline_input"
        if expected is None:
            # tmux can carry the exact dim placeholder through the blank row
            # and emit its full reset at the start of the model/footer row.
            reset_on_footer = (tail == "\x1b[2mAsk Codex to do anything"
                and raw[row + 1] == "" and raw[-2].startswith("\x1b[0m  "))
            if tail != "\x1b[2mAsk Codex to do anything\x1b[0m" and not reset_on_footer:
                return "blocked", "composer_contains_text_or_unknown_placeholder"
        elif tail != expected:
            return "blocked", "composer_text_mismatch_or_styled_input"
        text, visible = expected or "", True
    if (snapshot.cursor_x, snapshot.cursor_y, snapshot.cursor_visible) != (2 + len(text), row, visible):
        return "unknown", "cursor_not_at_expected_composer"
    return "empty", "native_snapshot_empty_layout_live_unverified" if expected is None else "exact_visible_ascii_input"


def classify_input(provider: Provider, version: str, snapshot: TerminalSnapshot) -> ComposerVerdict:
    """Observe an empty composer, without authorizing paste or live readiness."""
    fingerprint = hashlib.sha256(json.dumps(
        [provider, version, snapshot.model_dump(warnings=False)], sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()
    state, reason = _observe(provider, version, snapshot, None)
    return ComposerVerdict(state=state, reason=reason, fingerprint=fingerprint,
        evidence="native_snapshot" if provider in VERSIONS and VERSIONS[provider] == version else "unsupported")


def matches_input(provider: Provider, version: str, snapshot: TerminalSnapshot, expected_text: str) -> bool:
    """Prove exact visible short ASCII input, not arbitrary stored body bytes."""
    if (not isinstance(expected_text, str) or not expected_text
            or any(not 0x20 <= ord(c) <= 0x7e for c in expected_text)
            or type(snapshot.columns) is not int or 2 + len(expected_text) >= snapshot.columns):
        return False
    return _observe(provider, version, snapshot, expected_text)[1] == "exact_visible_ascii_input"
