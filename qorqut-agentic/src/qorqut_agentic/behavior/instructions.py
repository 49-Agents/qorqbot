"""Canonical runtime prose and pure hook policy; also usable as an isolated hook helper.

The host supplies a catalog reader or a prepared snapshot. There is deliberately
no runtime fallback to packaged defaults and no service, process or credential IO.
This module uses only the standard library so trusted preparation can copy it
beside an isolated native-client hook without importing the application.
"""

import json
import os
from pathlib import Path
import re
import stat

PACKAGE = "runtime-instructions"
SNAPSHOT = ".bazarlyk/runtime-instructions.json"
HELPER = ".bazarlyk/agentic-instructions.py"
MAX_BYTES = 512 * 1024
TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

ALWAYS_SKILLS = (
    "bazarlyk-role-owner-focus", "bazarlyk-role-chat-references",
    "bazarlyk-role-owner-input", "bazarlyk-role-action-recording",
    "bazarlyk-role-owner-reminders", "bazarlyk-role-calendar",
    "bazarlyk-plugin-email-management", "bazarlyk-role-business-tables",
    "bazarlyk-role-browser-control",
    "bazarlyk-qorqbot-guide",
)
SKILL_ROOTS = {"codex": ".agents/skills", "claude-code": ".claude/skills"}
SKILL_BUDGET = 120000
ACTIVITY_INTERVAL = 900


class Instructions:
    def __init__(self, document):
        if (not isinstance(document, dict) or document.get("format") != 1
                or document.get("package") != PACKAGE or not isinstance(document.get("messages"), dict)):
            raise ValueError("Invalid runtime instruction snapshot")
        self.document = document
        for key, item in document["messages"].items():
            if (not re.fullmatch(r"[a-z][a-z0-9-]*", key) or not isinstance(item, dict)
                    or not isinstance(item.get("body"), str) or not item["body"].strip()
                    or not isinstance(item.get("parameters"), list)
                    or sorted(set(TOKEN.findall(item["body"]))) != item["parameters"]):
                raise ValueError("Invalid runtime instruction: " + key)

    def render(self, key, **values):
        item = self.document["messages"][key]
        if set(values) != set(item["parameters"]) or any(not isinstance(v, str) for v in values.values()):
            raise ValueError("Runtime instruction arguments do not match: " + key)
        # Substitute once: values are data, never a second template or executable code.
        return TOKEN.sub(lambda match: values[match.group(1)], item["body"])

    def bytes(self):
        return (json.dumps(self.document, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def from_catalog(read):
    """Read one consistent catalog snapshot; the host owns the read transaction."""
    index = json.loads(read("index.json"))
    if index.get("format") != 1 or not isinstance(index.get("entries"), list):
        raise ValueError("Invalid runtime instruction index")
    messages = {}
    for entry in index["entries"]:
        key, path = entry["id"], entry["path"]
        if key in messages or path != "messages/" + key + ".md":
            raise ValueError("Invalid runtime instruction path or duplicate ID")
        messages[key] = {"body": read(path), "parameters": entry["parameters"]}
    return Instructions({"format": 1, "package": PACKAGE, "messages": messages})


def from_prepared(registration_path):
    """Read the prepared, bounded local copy even while the service is unavailable."""
    path = Path(registration_path).parent / Path(SNAPSHOT).name
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Runtime instruction directory cannot be a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Invalid runtime instruction snapshot file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Runtime instruction snapshot exceeds its budget")
    return Instructions(json.loads(data))


def bootstrap_failure(instructions, *, code, message, pending, denial, temporary):
    """Choose recovery behavior without altering identity or permission decisions."""
    if pending:
        return instructions.render("startup-pending")
    key = "startup-denied" if denial else "startup-unavailable" if temporary else "startup-repair"
    values = {"CODE": code}
    if key != "startup-unavailable":
        values["MESSAGE"] = message
    return instructions.render(key, **values) + instructions.render("startup-fault")


def activity_reminder_due(*, checkpoint_enabled, event, state, now, reminder_at, work_at):
    return (not checkpoint_enabled and event == "PostToolUse" and state == "running"
            and now - max(reminder_at, work_at or 0) >= ACTIVITY_INTERVAL)


def completion_wrapper(instructions, correction):
    return instructions.render("completion-wrapper") + correction


def render_skills(instructions, workspace, runtime, limit=SKILL_BUDGET):
    root = Path(workspace) / SKILL_ROOTS[runtime]
    header = instructions.render("skills-header")
    parts, notes, size = [header], [], len(header)
    for name in ALWAYS_SKILLS:
        path = root / name / "SKILL.md"
        try:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise OSError("symlink")
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            notes.append(f"- {name}: not available in this workspace; skipped.")
            continue
        block = f'<skill name="{name}" path="{path}">\n{text}\n</skill>'
        if size + len(block) + 2 > limit - 250 * len(ALWAYS_SKILLS):
            notes.append(instructions.render("skills-omitted", NAME=name, PATH=str(path)))
            continue
        parts.append(block)
        size += len(block) + 2
    if notes:
        parts.append("Skill notes:\n" + "\n".join(notes))
    return "\n\n".join(parts)
