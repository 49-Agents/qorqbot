"""Tool-call facts from agent conversation transcripts.

Reads already-decoded transcript records (Claude Code or Codex format) and
reports two things: the tool calls that went wrong, and call sequences that
repeat. It only organizes and counts; it never judges or writes prose.

Pure standard library: no database and no imports from the rest of the app.
Malformed or unknown records are skipped, never raised on.
"""

import json
import re
import shlex

ARG_LIMIT = 160
ERROR_LIMIT = 300
SIGNATURE_LIMIT = 120

_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(?:sk-|fmu\d-|ghp_|gho_|xox[baprs]-)[A-Za-z0-9_-]{12,}"), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.I), "[REDACTED]"),
    (
        re.compile(
            r"""(?i)((?:password|passwd|secret|token|api[_-]?key)["']?\s*[=:]\s*)"""
            r"""(?:"[^"]*"|'[^']*'|\S+)"""
        ),
        r"\1[REDACTED]",
    ),
)

_EXACT_OPERATIONS = {
    "Read": "read", "NotebookRead": "read",
    "Grep": "search", "WebSearch": "search", "ToolSearch": "search",
    "Glob": "list", "LS": "list",
    "Write": "write",
    "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit", "apply_patch": "edit",
    "Bash": "execute", "shell": "execute", "local_shell": "execute", "exec_command": "execute",
    "WebFetch": "web_fetch",
}
_WORD_OPERATIONS = (
    ("list", "list"),
    ("read", ("get", "read", "view", "open")),
    ("search", ("search", "find", "query", "grep")),
    ("write", ("create", "write", "save", "put", "submit", "send", "add")),
    ("edit", ("update", "edit", "patch", "replace", "modify", "set", "delete", "remove")),
    ("execute", ("exec", "execute", "run", "shell", "bash")),
    ("web_fetch", ("fetch", "http", "browse")),
)
_WORD_LOOKUP = {}
for _op, _words in _WORD_OPERATIONS:
    for _w in (_words,) if isinstance(_words, str) else _words:
        _WORD_LOOKUP[_w] = _op

_SUBCOMMAND_TOOLS = {"git", "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "docker", "kubectl", "go", "uv", "gh"}
_METACHARACTERS = (";", "&&", "|", "`", "$(")
_FIRST_EXECUTABLE = re.compile(r"\s*(?:[A-Za-z_]\w*=\S*\s+)*([^\s;&|`$()<>]+)")

_PATH_KEYS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}


def _text(value):
    return value if isinstance(value, str) else ""


def _clean(text, limit):
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def _stamp(record):
    value = record.get("timestamp")
    return value if isinstance(value, str) else None


def _words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[\s_\-]+", spaced) if w]


def _operation(tool):
    if tool in _EXACT_OPERATIONS:
        return _EXACT_OPERATIONS[tool]
    last = re.split(r"__|\.", tool)[-1]
    for word in _words(last):
        if word in _WORD_LOOKUP:
            return _WORD_LOOKUP[word]
    return "other"


def _command(inp):
    if isinstance(inp, str):
        return inp
    if isinstance(inp, dict):
        for key in ("command", "cmd"):
            value = inp.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return shlex.join(value)
    return None


def _shell_shape(command):
    if not command or not command.strip():
        return "execute:?"
    if any(m in command for m in _METACHARACTERS):
        found = _FIRST_EXECUTABLE.match(command)
        return "execute:%s+" % found.group(1) if found else "execute:?"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "execute:?"
    while tokens and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return "execute:?"
    shape = "execute:" + tokens[0]
    if tokens[0] in _SUBCOMMAND_TOOLS:
        sub = next((t for t in tokens[1:] if not t.startswith("-")), None)
        if sub:
            shape += " " + sub
    return shape


def _compact(inp):
    try:
        return json.dumps(inp, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(inp)


def _shape(operation, inp, command):
    if operation == "execute" and command is not None:
        return _shell_shape(command)
    if operation == "execute" and isinstance(inp, dict) and not inp:
        return "execute:?"
    keys = sorted(str(k) for k in inp)[:12] if isinstance(inp, dict) else []
    return "%s:%s" % (operation, ",".join(keys))


def _digest(tool, operation, inp, command):
    value = None
    if operation == "execute" and command is not None:
        value = command
    elif isinstance(inp, dict):
        if tool in _PATH_KEYS:
            value = _text(inp.get(_PATH_KEYS[tool])) or _text(inp.get("path"))
        elif tool in ("Grep", "Glob"):
            value = " ".join(p for p in (_text(inp.get("pattern")), _text(inp.get("path"))) if p)
        elif tool in ("Agent", "Task"):
            value = _text(inp.get("description"))
    if not value:
        value = inp if isinstance(inp, str) else _compact(inp)
    return _clean(value, ARG_LIMIT)


def _new_call(tool, inp, at, segment, sidechain):
    operation = _operation(tool)
    command = _command(inp) if operation == "execute" else None
    return {
        "index": 0, "at": at, "tool": tool, "operation": operation,
        "shape": _shape(operation, inp, command),
        "arg": _digest(tool, operation, inp, command),
        "ok": None, "error": None, "interrupted": False,
        "segment": segment, "sidechain": sidechain,
    }


def _flatten(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _finish(call, failed, error, interrupted=False):
    if call["ok"] is not None:
        return
    call["ok"] = not failed
    call["interrupted"] = bool(interrupted)
    call["error"] = (_clean(error or "", ERROR_LIMIT) or "failed") if failed else None


def _claude(records):
    found, by_id, segment = [], {}, 0
    for record in records:
        if not isinstance(record, dict):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        kind = record.get("type")
        side = record.get("isSidechain") is True
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    call = _new_call(block["name"], block.get("input"), _stamp(record), segment, side)
                    call["index"] = len(found)
                    found.append(call)
                    if isinstance(block.get("id"), str):
                        by_id[block["id"]] = call
        elif kind == "user":
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            if not results:
                if (isinstance(content, (str, list)) and record.get("isMeta") is not True and not side):
                    segment += 1
                continue
            extra = record.get("toolUseResult")
            for block in results:
                call = by_id.get(block.get("tool_use_id"))
                if call is None:
                    continue
                info = extra if isinstance(extra, dict) and len(results) == 1 else {}
                interrupted = info.get("interrupted") is True
                failed = block.get("is_error") is True or interrupted
                error = _flatten(block.get("content"))
                if failed and not error.strip():
                    error = _text(info.get("stderr")) or (extra if isinstance(extra, str) else "")
                    if not error.strip() and interrupted:
                        error = "interrupted"
                _finish(call, failed, error, interrupted)
    return found


def _codex_output(payload):
    output = payload.get("output")
    failed, text = False, ""
    if isinstance(output, dict):
        text = _flatten(output.get("content")) or _text(output.get("output"))
        failed = output.get("success") is False
        meta = output.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int) and meta["exit_code"] != 0:
            failed = True
    elif isinstance(output, str):
        text = output
        try:
            data = json.loads(output)
        except ValueError:
            data = None
        if isinstance(data, dict):
            meta = data.get("metadata")
            if isinstance(meta, dict) and isinstance(meta.get("exit_code"), int) and not isinstance(meta["exit_code"], bool):
                failed = meta["exit_code"] != 0
            if isinstance(data.get("output"), str):
                text = data["output"]
            if data.get("success") is False:
                failed = True
    return failed, text


def _codex(records):
    found, by_id, segment = [], {}, 0
    for record in records:
        payload = record.get("payload") if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        rtype, ptype = record.get("type"), payload.get("type")
        if rtype == "event_msg":
            item = payload.get("item")
            if ptype == "user_message" or (
                ptype == "item_completed" and isinstance(item, dict) and item.get("type") == "UserMessage"
            ):
                segment += 1
        elif rtype == "response_item" and ptype in ("function_call", "custom_tool_call", "local_shell_call"):
            if ptype == "local_shell_call":
                action = payload.get("action")
                name, inp = "local_shell", {"command": action.get("command")} if isinstance(action, dict) else {}
            else:
                name = payload.get("name")
                inp = payload.get("arguments" if ptype == "function_call" else "input")
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except ValueError:
                        pass
            if not isinstance(name, str):
                continue
            call = _new_call(name, inp, _stamp(record), segment, False)
            call["index"] = len(found)
            found.append(call)
            ident = payload.get("call_id", payload.get("id"))
            if isinstance(ident, str):
                by_id[ident] = call
        elif rtype == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
            call = by_id.get(payload.get("call_id"))
            if call is not None:
                failed, text = _codex_output(payload)
                _finish(call, failed, text)
    return found


def calls(records, *, provider="claude"):
    """The tool calls of one transcript, in order, each with its result. ``records`` may be any iterable of decoded
    records, read once: a whole transcript is never held in memory."""
    if records is None or isinstance(records, (str, bytes, dict)):
        return []
    try:
        records = iter(records)
    except TypeError:
        return []
    return (_codex if provider == "codex" else _claude)(records)


def problems(found):
    """Every failed call, oldest first, with retry, recovery and streak facts."""
    runs = {}
    for i, call in enumerate(found):
        if call["ok"] is not False:
            continue
        if i > 0 and _same_run(found[i - 1], call):
            runs[i] = runs[i - 1]
        else:
            runs[i] = [0]
        runs[i][0] += 1
    out = []
    for i, call in enumerate(found):
        if call["ok"] is not False:
            continue
        nxt = found[i + 1] if i + 1 < len(found) else None
        out.append({
            "index": call["index"], "at": call["at"], "tool": call["tool"], "arg": call["arg"],
            "error": call["error"], "interrupted": call["interrupted"],
            "retried": bool(nxt and nxt["segment"] == call["segment"] and nxt["tool"] == call["tool"]),
            "recovered": any(
                later["segment"] == call["segment"] and later["tool"] == call["tool"] and later["ok"] is True
                for later in found[i + 1:]
            ),
            "streak": runs[i][0],
        })
    return out


def _same_run(prev, call):
    return prev["ok"] is False and prev["tool"] == call["tool"] and prev["segment"] == call["segment"]


_VOLATILE = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\"[^\"]*\"|`[^`]*`|(?<!\w)'[^']*'"), "<str>"),
    (re.compile(r"(?<![\w:/.])/[\w.\-~@+]+(?:/[\w.\-~@+]*)*"), "<path>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<id>"),
    (re.compile(r"\d+"), "<n>"),
)


def error_signature(tool, error):
    """A stable key for an error: tool name plus the error with volatile parts replaced."""
    text = re.sub(r"\s+", " ", error or "").strip()
    for pattern, placeholder in _VOLATILE:
        text = pattern.sub(placeholder, text)
    return ("%s: %s" % (tool, text)).lower()[:SIGNATURE_LIMIT]


def group_problems(sessions):
    """Group problems across sessions by error signature, most frequent first."""
    groups = {}
    for session in sessions:
        for item in session.get("problems") or []:
            signature = error_signature(item["tool"], item["error"])
            group = groups.setdefault(signature, {
                "signature": signature, "tool": item["tool"], "count": 0, "agents": set(),
                "keys": set(), "stamps": [], "recovered": 0, "examples": [],
            })
            group["count"] += 1
            group["agents"].add(session.get("agent"))
            group["keys"].add((session.get("agent"), session.get("key")))
            if item.get("at"):
                group["stamps"].append(item["at"])
            group["recovered"] += 1 if item.get("recovered") else 0
            if len(group["examples"]) < 3:
                group["examples"].append({
                    "agent": session.get("agent"), "session": session.get("key"),
                    "at": item.get("at"), "arg": item.get("arg"), "error": item.get("error"),
                })
    out = [{
        "signature": g["signature"], "tool": g["tool"], "count": g["count"],
        "agents": sorted(a for a in g["agents"] if isinstance(a, str)), "sessions": len(g["keys"]),
        "first_at": min(g["stamps"]) if g["stamps"] else None,
        "last_at": max(g["stamps"]) if g["stamps"] else None,
        "recovered": g["recovered"], "examples": g["examples"],
    } for g in groups.values()]
    out.sort(key=lambda g: (-g["count"], g["signature"]))
    return out


def repeats(found, *, minimum=3):
    """Runs of the same tool with the same argument, back to back within one segment."""
    out, i = [], 0
    while i < len(found):
        j = i + 1
        while (j < len(found) and found[j]["tool"] == found[i]["tool"] and found[j]["arg"] == found[i]["arg"]
               and found[j]["segment"] == found[i]["segment"]):
            j += 1
        if j - i >= minimum:
            out.append({
                "tool": found[i]["tool"], "arg": found[i]["arg"], "count": j - i,
                "first_index": found[i]["index"], "at": found[i]["at"],
                "failed": sum(1 for c in found[i:j] if c["ok"] is False),
            })
        i = j
    return out


def _blocks(session_calls):
    block, key = [], None
    for call in session_calls:
        mark = (call["segment"], call["sidechain"])
        if mark != key and block:
            yield block
            block = []
        key = mark
        block.append(call)
    if block:
        yield block


def _count_projection(sessions, projection, min_length, max_length):
    stats = {}
    for number, session in enumerate(sessions):
        agent, name = session.get("agent"), session.get("key")
        for block in _blocks(session.get("calls") or []):
            tokens = [c[projection] for c in block]
            last_end = {}
            for start in range(len(tokens)):
                for length in range(min_length, min(max_length, len(tokens) - start) + 1):
                    sequence = tuple(tokens[start:start + length])
                    if last_end.get(sequence, 0) > start:
                        continue
                    last_end[sequence] = start + length
                    stat = stats.get(sequence)
                    if stat is None:
                        stat = stats[sequence] = {"count": 0, "sessions": set(), "agents": set(), "examples": []}
                    stat["count"] += 1
                    stat["sessions"].add(number)
                    stat["agents"].add(agent)
                    if len(stat["examples"]) < 3:
                        stat["examples"].append({
                            "agent": agent, "session": name, "at": block[start]["at"],
                            "args": [c["arg"] for c in block[start:start + length]],
                        })
    return stats


def patterns(sessions, *, min_length=2, max_length=5, min_occurrences=3, limit=50):
    """Repeated contiguous call sequences, counted across sessions, by tool name and by shape."""
    out = []
    for projection, field in (("tool", "tool"), ("shape", "shape")):
        stats = _count_projection(sessions, field, max(min_length, 2), max_length)
        kept = {s: v for s, v in stats.items() if v["count"] >= min_occurrences and len(set(s)) > 1}
        dominated = set()
        for sequence, stat in kept.items():
            for length in range(min_length, len(sequence)):
                for start in range(len(sequence) - length + 1):
                    inner = sequence[start:start + length]
                    if inner in kept and kept[inner]["count"] == stat["count"]:
                        dominated.add(inner)
        for sequence, stat in kept.items():
            if sequence in dominated:
                continue
            out.append({
                "projection": projection, "sequence": list(sequence), "length": len(sequence),
                "occurrences": stat["count"], "sessions": len(stat["sessions"]),
                "agents": sorted(a for a in stat["agents"] if isinstance(a, str)),
                "examples": stat["examples"],
            })
    out.sort(key=lambda p: (-p["occurrences"] * p["length"], -p["length"], p["sequence"], p["projection"]))
    return out[:limit]
