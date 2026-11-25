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
SNAPSHOT = ".qorqut/runtime-instructions.json"
HELPER = ".qorqut/agentic-instructions.py"
MAX_BYTES = 512 * 1024
TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

ALWAYS_SKILLS = (
    "qorqut-role-chat-references",
    "qorqut-role-owner-input", "qorqut-role-action-recording",
    "qorqut-plugin-email-management", "qorqut-role-project-tables",
    "qorqut-qorqbot-guide",
)
SKILL_ROOTS = {"codex": ".agents/skills", "claude-code": ".claude/skills"}
SKILL_BUDGET = 120000
# The ordered startup message combines the two previously separate deliveries.
# The mandatory part follows qorqut.agents.context.MAX_CONTEXT_CHARACTERS.
BOOTSTRAP_BUDGET = 160000 + SKILL_BUDGET
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


DISCOVERY_SKIPPED = ("role",)  # /role is the first message of every conversation: it is loaded, not discovered.
DISCOVERY_LINE_LIMIT = 220
DISCOVERY_SECTION_LIMIT = 6000
DISCOVERY_PHRASE_LIMIT = 160  # A skill's own phrase is under 120; a description's first sentence is cut to this.


def skill_front_matter(text):
    """The `name`, `description` and `read_for` values of a SKILL.md, read without a YAML library.

    Plain, quoted and folded (``>``) or literal (``|``) one-key values are understood; anything else is ignored.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    block = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    else:
        return {}
    found, index = {}, 0
    while index < len(block):
        match = re.match(r"([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", block[index])
        index += 1
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value[:1] in ">|":
            parts = []
            while index < len(block) and (not block[index].strip() or block[index][:1] in " \t"):
                parts.append(block[index].strip())
                index += 1
            value = " ".join(part for part in parts if part)
        elif value[:1] in "\"'" and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        found[key] = value
    return {key: found[key] for key in ("name", "description", "read_for") if found.get(key)}


def _shorten(text, limit):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:max(limit - 1, 1)].rsplit(" ", 1)[0] or text[:max(limit - 1, 1)]
    return cut.rstrip(" ,;:.-") + "…"


def _first_sentence(text):
    text = " ".join(text.split())
    match = re.search(r"[.!?](?:\s|$)", text)
    return text[:match.start()] if match else text


def installed_skills(workspace, runtime):
    """``[(name, front matter)]`` for every skill folder installed for this agent, by name; unreadable ones are kept
    with an empty front matter so that no installed skill goes unlisted."""
    root = Path(workspace) / SKILL_ROOTS[runtime]
    try:
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents) or not root.is_dir():
            return []
        children = sorted(child for child in root.iterdir() if child.is_dir() and not child.is_symlink())
    except OSError:
        return []
    found = []
    for child in children:
        path = child / "SKILL.md"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            front = skill_front_matter(path.read_text(encoding="utf-8")[:20000])
        except (OSError, UnicodeError):
            front = {}
        found.append((child.name, front))
    return found


def render_skill_discovery(instructions, workspace, runtime, loaded=()):
    """The "for this kind of work, read this skill first" section: one line for each installed skill that is not loaded in full.

    ``loaded`` names the skills this start already carries in full. The ``role`` skill is never listed. A skill
    brings its own short phrase as `read_for` in its front matter; without one its line is built from the first
    sentence of its description. Returns "" when nothing is left to list.
    """
    skipped = set(DISCOVERY_SKIPPED) | set(loaded)
    entries = [(name, front) for name, front in installed_skills(workspace, runtime) if name not in skipped]
    if not entries:
        return ""
    suffix = runtime  # Claude Code reads a skill by name; Codex opens the file, so its lines carry the path.
    header = instructions.render("skill-discovery-header-" + suffix)

    def build(phrase_limit):
        lines = []
        for name, front in entries:
            path = f"{SKILL_ROOTS[runtime]}/{name}/SKILL.md"
            work = front.get("read_for") or _first_sentence(front.get("description", "")) or "Work this skill covers"
            room = phrase_limit
            for _ in range(200):
                where = {"PATH": path} if runtime == "codex" else {}
                line = instructions.render("skill-discovery-line-" + suffix, WORK=_shorten(work, room),
                                           NAME=name, **where)
                over = len(line) - DISCOVERY_LINE_LIMIT
                if over <= 0 or room <= 8:
                    break
                room = max(room - over, 8)  # Only this line gets shorter.
            lines.append(line)
        return header + "\n" + "\n".join(lines)

    phrase_limit = DISCOVERY_PHRASE_LIMIT
    section = build(phrase_limit)
    while len(section) > DISCOVERY_SECTION_LIMIT and phrase_limit > 8:
        phrase_limit = int(phrase_limit * 0.75)
        section = build(phrase_limit)
    return section


def render_skills(instructions, workspace, runtime, limit=SKILL_BUDGET, extra=()):
    root = Path(workspace) / SKILL_ROOTS[runtime]
    header = instructions.render("skills-header")
    parts, notes, size = [header], [], len(header)
    names = (*ALWAYS_SKILLS, *extra)
    for name in names:
        path = root / name / "SKILL.md"
        try:
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise OSError("symlink")
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            notes.append(f"- {name}: not available in this workspace; skipped.")
            continue
        block = f'<skill name="{name}" path="{path}">\n{text}\n</skill>'
        if size + len(block) + 2 > limit - 250 * len(names):
            notes.append(instructions.render("skills-omitted", NAME=name, PATH=str(path)))
            continue
        parts.append(block)
        size += len(block) + 2
    if notes:
        parts.append("Skill notes:\n" + "\n".join(notes))
    return "\n\n".join(parts)
