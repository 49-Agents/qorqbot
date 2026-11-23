"""Which client a model name belongs to, so a model chosen for one client is never handed to the other.

``options.py`` only checks that a model is a plausible name. A model is free text (the owner may type any name the client accepts), but
the names of the two families are told apart here: Claude Code takes ``opus``, ``sonnet``, ``haiku`` (optionally ``[1m]``) and ``claude-*``
ids; Codex takes ``gpt-*``, ``codex-*`` and the ``o<digit>`` series. A name of neither family is left alone (it may be a new
model); a name of the *other* family is the mismatch this module exists to catch. Nothing here starts anything or reads anything.
"""

from __future__ import annotations

import re

CLAUDE, CODEX = "claude", "codex"
NAMES = {CLAUDE: "Claude Code", CODEX: "Codex"}
# What the dashboard offers as completions for each client (any other valid name may still be typed).
SUGGESTIONS = {CLAUDE: ("opus", "sonnet", "haiku"), CODEX: ("gpt-6-luna",)}
_CLAUDE_ALIAS = re.compile(r"^(opus|sonnet|haiku|opusplan|best)(\[[0-9a-z]+\])?$")
_CODEX_FAMILY = re.compile(r"^(gpt|codex|chatgpt|o[0-9])")


def family(model) -> str | None:
    """``claude`` or ``codex`` when the name belongs to one family, else None (unknown: not judged)."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip().lower()
    if name.startswith("claude") or _CLAUDE_ALIAS.fullmatch(name):
        return CLAUDE
    if _CODEX_FAMILY.match(name):
        return CODEX
    return None


def fits(client, model) -> bool:
    """Whether ``model`` can be passed to ``client``: no model, a model of its family, or one of no known family."""
    belongs = family(model)
    return belongs is None or belongs == client


def mismatch_text(client, model, who=None) -> str:
    """The plain refusal for a model of the other client, for the owner."""
    other = CODEX if client == CLAUDE else CLAUDE
    return (f"{str(model)[:60]} is a {NAMES[other]} model, and {who + ' uses' if who else 'this agent uses'} {NAMES[client]}. "
            f"Choose a {NAMES[client]} model, or leave it on default.")
