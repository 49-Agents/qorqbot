"""Recorded Claude Code agent transcripts as compaction test material. Reads only.

A session file is cut at its native compaction boundaries into segments: segment 0 runs from the session start to the
first boundary, segment i from boundary i to boundary i+1. ``summary_after`` is the summary the native compactor wrote
when the segment ended and ``kept`` the last messages it carried over word for word next to that summary (the summary is
written a little before the cut and does not cover them); together they are the baseline. Owner messages get stable ids (o1, o2, ...) across the whole session.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

MIN_BYTES = 200_000
NOT_OWNER = ("<qorqut-system-input", "Another Claude session", "[Your previous response", "This session is being continued",
             "<command-", "<local-command", "Caveat:", "<task-notification", "<system-reminder", "[Request interrupted")
RESULT_CAPS = (2000, 800, 300, 120)
_PASTED = re.compile(r"</?pasted_content[^>]*>")
_NOTICE = re.compile(r'<qorqut-system-input[^>]*type="([^"]+)"')
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|passcode|cvv|cvc)\b(\s*(?:is|:|=|-)?\s*)\S+")


def scrub(text):
    """Long digit runs (card numbers) and values that follow a password word, removed before any model sees them."""
    return _SECRET.sub(r"\1\2[removed]", _CARD.sub("[number removed]", text))


@dataclass
class Segment:
    index: int
    started: str = ""
    ended: str = ""
    events: list = field(default_factory=list)      # (kind, text, record id): owner, notice, agent, call, result
    kept: list = field(default_factory=list)        # the events the native compactor carried over verbatim
    owner: list = field(default_factory=list)       # {"id", "at", "text", "reply"}
    summary_after: str | None = None
    pre_tokens: int | None = None


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _owner_text(text):
    text = scrub(_PASTED.sub("", text or "").strip())
    return text if text and not any(mark in text[:400] for mark in NOT_OWNER) else None


def agent_names(agents_root):
    names = {}
    for profile in Path(agents_root).glob("*/agent.yaml"):
        for line in profile.read_text().splitlines():
            if line.startswith("name:"):
                names[profile.parent.name] = line.split(":", 1)[1].strip()
    return names


def session_files(projects_root):
    return sorted(path for path in Path(projects_root).glob("-home-alp-qorqut-v1-2-operations-agents-*/*.jsonl")
                  if path.stat().st_size >= MIN_BYTES)


def segments(path):
    """Every segment of one session file, in order."""
    current, result, seen, waiting = Segment(0), [], set(), None
    result.append(current)
    for line in open(path, errors="replace"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        stamp, kind = record.get("timestamp") or "", record.get("type")
        if stamp:
            current.started = current.started or stamp
            current.ended = stamp
        if kind == "system" and record.get("subtype") == "compact_boundary":
            details = record.get("compactMetadata") or {}
            carried = set((details.get("preservedMessages") or {}).get("uuids") or [])
            current.kept = [event for event in current.events if event[2] in carried]
            current = Segment(current.index + 1, pre_tokens=details.get("preTokens"))
            result.append(current)
            continue
        if record.get("isCompactSummary"):
            if len(result) > 1:
                result[-2].summary_after = scrub(text_of(record["message"].get("content")))
            continue
        said, key = None, record.get("uuid")
        if kind == "attachment":
            attachment = record.get("attachment") or {}
            if attachment.get("type") != "queued_command":
                continue
            prompt = attachment.get("prompt")
            prompt = prompt if isinstance(prompt, str) else text_of(prompt)
            if (attachment.get("origin") or {}).get("kind") == "human":
                said = _owner_text(prompt)
            elif _NOTICE.search(prompt or ""):
                current.events.append(("notice", _NOTICE.search(prompt).group(1), key))
        elif kind == "user" and not record.get("isMeta"):
            content = record["message"].get("content")
            if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "tool_result" for part in content):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        body = part.get("content")
                        body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                        if '"type": "image"' in body or '"type":"image"' in body:
                            body = "[image]"
                        current.events.append(("result", ("ERROR " if part.get("is_error") else "") + scrub(body), key))
                continue
            raw = text_of(content)
            notice = _NOTICE.search(raw[:600])
            if notice:
                current.events.append(("notice", notice.group(1), key))
            else:
                said = _owner_text(raw)
        elif kind == "assistant":
            for part in record["message"].get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part["text"].strip():
                    current.events.append(("agent", scrub(part["text"].strip()), key))
                    if waiting is not None and not waiting["reply"]:
                        waiting["reply"] = scrub(part["text"].strip())[:300]
                elif part.get("type") == "tool_use":
                    call = scrub(json.dumps(part.get("input"), ensure_ascii=False))[:300]
                    current.events.append(("call", f"{part['name']} {call}", key))
        if said and said[:200] not in seen:
            seen.add(said[:200])
            waiting = {"id": f"o{len(seen)}", "at": stamp[:16], "text": said, "reply": ""}
            current.owner.append(waiting)
            current.events.append(("owner", f"[{waiting['id']} {waiting['at']}] {said}", key))
    return result


def render(segment, budget=300_000, kept=False):
    """The segment as plain text for a model, tool results cut so that the whole stays within ``budget`` characters.

    With ``kept`` only the messages the native compactor carried over are rendered.
    """
    labels = {"owner": "OWNER", "notice": "SYSTEM NOTICE", "agent": "YOU", "call": "TOOL CALL", "result": "TOOL RESULT"}
    for cap in RESULT_CAPS:
        lines, notices = [], 0
        for kind, text, _ in (segment.kept if kept else segment.events):
            if kind == "notice":
                notices += 1
                continue
            if notices:
                lines.append(f"[{notices} system notices]")
                notices = 0
            if kind == "result" and len(text) > cap:
                text = text[:cap] + f" …[{len(text) - cap} more characters]"
            if kind == "agent" and len(text) > 4 * cap:
                text = text[:4 * cap] + " …"
            lines.append(f"{labels[kind]}: {text}")
        rendered = "\n".join(lines)
        if len(rendered) <= budget:
            return rendered
    return rendered[:budget // 2] + "\n…[middle of the conversation left out]…\n" + rendered[-budget // 2:]
