"""The slash commands Chat offers for an agent's running CLI.

Each command says where its result appears:

* ``chat``: typed into the session like a message; what it does shows in Chat (``/compact``, a custom command, a skill).
* ``terminal``: the CLI opens a menu or panel that only its terminal can show (``/model`` without a name, ``/resume``,
  ``/cost``); Chat sends it and the owner finishes in the agent's Terminal. Chat never sends keys the owner did not type.

Built-ins are a curated list per client (checked against Claude Code 2.1 and Codex 0.166). The owner's own Claude Code
commands (``~/.claude/commands``, ``<agent>/.claude/commands``) and skills (``~/.claude/skills``, ``<agent>/.claude/skills``)
are added by name; Qorqbot's own role skills are left out. Nothing here runs a command.
"""
from __future__ import annotations

from pathlib import Path
import re

import yaml

CLIENTS = ("claude", "codex")
MAX_CUSTOM = 200
MAX_DESCRIPTION = 160

BUILT_IN = {
    "claude": (
        ("/compact", "Summarize the conversation so far to free up context. Add instructions to say what to keep.", "chat"),
        ("/clear", "Start a new conversation in this session.", "chat"),
        ("/init", "Write a CLAUDE.md with notes about this folder.", "chat"),
        ("/review", "Review the changes in this folder.", "chat"),
        ("/security-review", "Check the pending changes for security problems.", "chat"),
        ("/model", "Switch model. Add a name (for example /model sonnet), or pick one in Terminal.", "terminal"),
        ("/cost", "Show what this session has used.", "terminal"),
        ("/usage", "Show your plan's usage limits.", "terminal"),
        ("/status", "Show the version, model, account and connections.", "terminal"),
        ("/context", "Show how full the context is.", "terminal"),
        ("/resume", "Pick an earlier conversation to continue.", "terminal"),
        ("/permissions", "Review which tools are allowed.", "terminal"),
        ("/mcp", "Manage connected servers and connectors.", "terminal"),
        ("/memory", "Edit Claude's memory files.", "terminal"),
        ("/agents", "Manage subagents.", "terminal"),
        ("/config", "Open Claude Code's settings.", "terminal"),
        ("/doctor", "Check the Claude Code installation.", "terminal"),
        ("/help", "List every command.", "terminal"),
        ("/exit", "End this session. Start the agent again to continue.", "chat"),
    ),
    "codex": (
        ("/compact", "Summarize the conversation so far to free up context.", "chat"),
        ("/new", "Start a new conversation in this session.", "chat"),
        ("/init", "Write an AGENTS.md with notes about this folder.", "chat"),
        ("/review", "Review changes; choose what to review in Terminal.", "terminal"),
        ("/model", "Pick the model and reasoning effort.", "terminal"),
        ("/permissions", "Choose what Codex may do without asking.", "terminal"),
        ("/status", "Show the session's model, folder and limits.", "terminal"),
        ("/diff", "Show the changes in this folder.", "terminal"),
        ("/mcp", "List connected servers.", "terminal"),
        ("/exit", "End this session. Start the agent again to continue.", "chat"),
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
    return not name.startswith("bazarlyk-") and name != "role"


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
    """The commands for ``client`` ("claude" or "codex"), built-ins first, then the owner's own (Claude only)."""
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


def delivery_text(text: str) -> str:
    """What Chat types for a slash command: a bare command gets one trailing space, which closes the CLI's completion
    menu so the paste can be confirmed and Enter runs exactly that command. Anything else is typed unchanged."""
    if re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9:_-]{0,80}", text.strip()) and text == text.strip():
        return text + " "
    return text
