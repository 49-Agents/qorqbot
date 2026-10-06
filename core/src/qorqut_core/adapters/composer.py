"""Conservative classification of P06 synthetic composer layouts.

The pinned source's own tests are hand-built, not native captures. Classification
is useful offline; no profile in this increment authorizes automated paste.
"""

import hashlib
import json
import re

from .capabilities import VERSIONS
from .types import ComposerVerdict, Provider

SGR = re.compile(r"\x1b\[[0-9;:]*m")
RULE = re.compile(r"\s*[─━═-]{8,}\s*\Z")
BUSY = ("esc to interrupt", "ctrl+c to interrupt", "tab to queue", "enter to interrupt")
DIALOG = ("enter to confirm", "esc to cancel", "sign in", "log in", "do you trust", "select model")


def classify(provider: Provider, version: str, raw: str,
             cursor: tuple[int, int, bool] | None = None) -> ComposerVerdict:
    fingerprint = hashlib.sha256(json.dumps([provider, version, raw, cursor], ensure_ascii=False).encode()).hexdigest()
    known = VERSIONS.get(provider) == version

    def result(state, reason):
        return ComposerVerdict(state=state, reason=reason, fingerprint=fingerprint,
            evidence="synthetic" if known else "unsupported")

    if not known:
        return result("unknown", "unsupported_version")
    lines = raw.splitlines()
    plain = [SGR.sub("", line) for line in lines]
    if any("\x1b" in line for line in plain):
        return result("unknown", "unsupported_escape_sequence")
    marker = re.compile(r"^(?:›) ") if provider == "codex" else re.compile(r"^[>❯][ \xa0]")
    prompts = [i for i, line in enumerate(plain) if marker.match(line)]
    if not prompts:
        bottom = "\n".join(plain[-6:]).lower()
        if any(word in bottom for word in DIALOG):
            return result("blocked", "dialog_or_setup")
        if any(word in bottom for word in BUSY):
            return result("busy", "turn_running")
        return result("unknown", "no_known_composer")
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
    raw_tail = lines[row][2:]
    placeholder = bool(re.fullmatch(r"\x1b\[2m[^\x1b\r\n]*\x1b\[0m", raw_tail))
    if content.strip() and not placeholder:
        return result("blocked", "composer_contains_text")
    footer = [line.strip() for line in plain[row + 1:] if line.strip() and not RULE.fullmatch(line)]
    if len(footer) != 1 or not re.fullmatch(r"\? for shortcuts(?:\s+\d+% context left)?", footer[0]):
        return result("unknown", "unknown_footer_or_multiline_input")
    if cursor is None or cursor != (2, row, True):
        return result("unknown", "cursor_not_at_empty_composer")
    return result("empty", "synthetic_empty_layout_native_unverified")
