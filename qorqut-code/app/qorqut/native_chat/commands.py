"""The slash commands Chat offers for an agent's running CLI.

Qorqbot runs every agent itself, as a headless session, so each command says whether it can run there:

* ``chat``: sent into the session like a message, or carried out through the client's own interface; what it does shows in Chat
  (``/compact``, a custom command, a skill). Claude takes a slash command typed as message text. Codex takes ``$role`` as text but
  not ``/compact`` or ``/model``: those are plain text to its app-server, so the managed adapter turns them into the host's
  ``command`` operation (``codex_command``: ``thread/compact/start``, ``thread/settings/update``).
* ``unavailable``: the CLI opens a menu or panel only a terminal can show (``/model`` without a name, ``/resume``, ``/cost``). Chat
  refuses them (``managed_command``) instead of sending text the client cannot act on.

Built-ins are a curated list per client (checked against Claude Code 2.1 and Codex 0.166). The owner's own Claude Code
commands (``~/.claude/commands``, ``<agent>/.claude/commands``) and skills (``~/.claude/skills``, ``<agent>/.claude/skills``)
are added by name; Qorqbot's own role skills are left out. Nothing here runs a command.
"""
from __future__ import annotations

from pathlib import Path
import re

import yaml

from .contracts import managed_menu_command

CLIENTS = ("claude", "codex")
MAX_CUSTOM = 200
MAX_DESCRIPTION = 160

BUILT_IN = {
    "claude": (
        ("/compact", "Summarize the conversation so far to free up context. Add instructions to say what to keep.", "chat"),
        ("/clear", "Start a new conversation in this session.", "unavailable"),
        ("/init", "Write a CLAUDE.md with notes about this folder.", "chat"),
        ("/review", "Review the changes in this folder.", "chat"),
        ("/security-review", "Check the pending changes for security problems.", "chat"),
        ("/model", "Switch model. Add a name (for example /model sonnet).", "unavailable"),
        ("/cost", "Show what this session has used.", "unavailable"),
        ("/usage", "Show your plan's usage limits.", "unavailable"),
        ("/status", "Show the version, model, account and connections.", "unavailable"),
        ("/context", "Show how full the context is.", "unavailable"),
        ("/resume", "Pick an earlier conversation to continue.", "unavailable"),
        ("/permissions", "Review which tools are allowed.", "unavailable"),
        ("/mcp", "Manage connected servers and connectors.", "unavailable"),
        ("/memory", "Edit Claude's memory files.", "unavailable"),
        ("/agents", "Manage subagents.", "unavailable"),
        ("/config", "Open Claude Code's settings.", "unavailable"),
        ("/doctor", "Check the Claude Code installation.", "unavailable"),
        ("/help", "List every command.", "unavailable"),
        ("/exit", "End this session. Sending a message starts a new one.", "chat"),
    ),
    "codex": (
        ("/compact", "Summarize the conversation so far to free up context.", "chat"),
        ("/new", "Start a new conversation in this session.", "unavailable"),
        ("/init", "Write an AGENTS.md with notes about this folder.", "unavailable"),
        ("/review", "Review changes.", "unavailable"),
        ("/model", "Switch model and reasoning effort: /model name [effort].", "chat"),
        ("/permissions", "Choose what Codex may do without asking.", "unavailable"),
        ("/status", "Show the session's model, folder and limits.", "unavailable"),
        ("/diff", "Show the changes in this folder.", "unavailable"),
        ("/mcp", "List connected servers.", "unavailable"),
        ("/exit", "End this session. Sending a message starts a new one.", "chat"),
    ),
}


def _front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _describe(text: str, meta: dict) -> str:
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        body = text.split("---", 2)[2] if text.startswith("---") and text.count("---") >= 2 else text
        description = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
    description = " ".join(description.split())
    return description[:MAX_DESCRIPTION - 1] + "…" if len(description) > MAX_DESCRIPTION else description


def _read(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _owner_skill(name: str) -> bool:
    return not name.startswith(("qorqut-", "bazarlyk-")) and name != "role"


def _claude_custom(roots) -> list[dict]:
    found = {}
    for root, source in roots:
        commands = root / ".claude" / "commands"
        if commands.is_dir() and not commands.is_symlink():
            for path in sorted(commands.rglob("*.md")):
                text = _read(path)
                if text is None:
                    continue
                relative = path.relative_to(commands).with_suffix("")
                name = "/" + ":".join(relative.parts)
                found.setdefault(name, {"name": name, "description": _describe(text, _front_matter(text)) or "Your command",
                                        "where": "chat", "source": source})
        skills = root / ".claude" / "skills"
        if skills.is_dir() and not skills.is_symlink():
            for path in sorted(skills.glob("*/SKILL.md")):
                text = _read(path)
                if text is None:
                    continue
                meta = _front_matter(text)
                skill = meta.get("name") if isinstance(meta.get("name"), str) else path.parent.name
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skill or "") or not _owner_skill(skill):
                    continue
                name = "/" + skill
                found.setdefault(name, {"name": name, "description": _describe(text, meta) or "Your skill", "where": "chat",
                                        "source": "skill"})
        if len(found) >= MAX_CUSTOM:
            break
    return list(found.values())[:MAX_CUSTOM]


def catalog(client: str, *, home=None, workspace=None) -> dict:
    """The commands for ``client`` ("claude" or "codex"), built-ins first, then the owner's own (Claude only). The commands only a
    terminal can show are ``unavailable``."""
    if client not in CLIENTS:
        return {"client": None, "commands": []}
    commands = [{"name": name, "description": description, "where": where, "source": "built-in"}
                for name, description, where in BUILT_IN[client]]
    if client == "claude":
        roots = [(Path(workspace), "agent")] if workspace else []
        if home:
            roots.append((Path(home), "yours"))
        taken = {item["name"] for item in commands}
        commands += [item for item in _claude_custom(roots) if item["name"] not in taken]
    return {"client": client, "commands": commands}


def codex_builtin(text: str) -> str | None:
    """The Codex built-in command (from the catalog above) that ``text`` runs, or None for any other text.

    Only the first word counts, as the CLI reads it: ``/compact keep decisions`` runs ``/compact``, while ``/tmp/foo`` and
    ``/model/x`` are ordinary text. Nothing is run and no configuration is read."""
    words = text.split(None, 1)
    if not words:
        return None
    word = words[0].lower()
    return next((name for name, _, _ in BUILT_IN["codex"] if name == word), None)


EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def codex_command(text: str):
    """What a Codex session does for ``text`` through its own interface, or None for text it takes as a message.

    ``("compact", {})`` for ``/compact`` (anything after it is ignored: Codex's compaction takes no instruction), and
    ``("model", {"model": name, "effort": level or None})`` for ``/model <name> [effort]`` or ``/model <effort>``."""
    command = codex_builtin(text)
    words = text.split()
    if command == "/compact":
        return "compact", {}
    if command == "/model" and len(words) > 1:
        rest = [word for word in words[1:3]]
        if rest[0].lower() in EFFORTS and len(rest) == 1:
            return "model", {"model": None, "effort": rest[0].lower()}
        effort = rest[1].lower() if len(rest) > 1 and rest[1].lower() in EFFORTS else None
        return "model", {"model": rest[0], "effort": effort}
    return None


def unrunnable(provider: str | None, text: str) -> str | None:
    """The command ``text`` runs that a managed session cannot carry out (a menu or panel only a terminal can show), or None.
    Claude: its menu-only commands (``/model`` without a name is one). Codex: every built-in except ``/compact``, ``/exit`` and
    ``/model`` with a name."""
    if provider == "codex":
        command = codex_builtin(text)
        if command is None or command == "/exit" or codex_command(text) is not None:
            return None
        return command
    return managed_menu_command(text)
