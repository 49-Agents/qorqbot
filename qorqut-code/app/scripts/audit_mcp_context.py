"""Read explicitly selected transcript copies; emit sizes/shapes, never source text.

Counts characters, not provider tokens or billing. Codex exec batches are not
attributed to individual operations when the transcript cannot prove the mapping.
No API requests, transcript-store discovery or file writes are performed.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(block.get("text", "") for block in value if isinstance(block, dict))
    return ""


def json_objects(text):
    decoder = json.JSONDecoder()
    offset = 0
    while (start := text.find("{", offset)) >= 0:
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            offset = start + 1
            continue
        yield value
        offset = end


RESULT_STATUSES = {"ok", "signoff_required", "accepted", "error"}


def is_envelope(value):
    # Historical envelopes always carried request_id; slimmed wire envelopes
    # drop it on success but keep status plus at least one result member.
    if not isinstance(value, dict) or value.get("status") not in RESULT_STATUSES:
        return False
    return any(key in value for key in ("request_id", "data", "error", "signoff", "job"))


def envelopes(value):
    if not isinstance(value, dict):
        return
    if isinstance(value.get("structuredContent"), dict):
        structured = value["structuredContent"]
        serialized = text_content(value.get("content"))
        try:
            duplicate = json.loads(serialized) == structured
        except ValueError:
            duplicate = False
        if is_envelope(structured):
            yield structured, len(compact(value)), duplicate
        return
    if is_envelope(value):
        yield value, len(compact(value)), False
        return
    # Text-only MCP results still wrap serialized JSON. Count their wire wrapper
    # once, just as for the historical text + structured result above.
    if isinstance(value.get("content"), list):
        serialized = text_content(value["content"])
        try:
            decoded = json.loads(serialized)
        except ValueError:
            decoded = None
        if is_envelope(decoded):
            yield decoded, len(compact(value)), False
            return
    for child in value.values():
        if isinstance(child, dict):
            yield from envelopes(child)
        elif isinstance(child, list):
            for item in child:
                yield from envelopes(item)


def summarize(values):
    ordered = sorted(values)
    return {"count": len(values), "total_chars": sum(values),
            "median_chars": int(statistics.median(values)) if values else 0,
            "p95_chars": ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if values else 0,
            "max_chars": max(values, default=0)}


def call_grouping(records):
    """Count explicit grouping evidence, not inferred provider round trips.

    Claude streaming fragments share message.id; Codex exec source is inspected,
    never executed. Static references cannot count loop iterations or dynamic
    dispatch. An exec may also yield and require separate wait calls.
    """
    claude, codex = defaultdict(dict), {}
    ungrouped = set()
    for record in records:
        message = record.get("message", {})
        if isinstance(message, dict) and message.get("role") == "assistant":
            content = message.get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, identity = block.get("name", ""), block.get("id")
                if not isinstance(name, str) or not identity:
                    continue
                if message.get("id"):
                    claude[message["id"]][identity] = name
                elif name.startswith("mcp__bazarlyk__"):
                    ungrouped.add(identity)
        payload = record.get("payload", {})
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        if payload.get("type") not in {"function_call", "custom_tool_call"} or not payload.get("call_id"):
            continue
        name = payload.get("name", "")
        source = payload.get("input", payload.get("arguments", ""))
        if name.startswith("mcp__bazarlyk__"):
            codex[payload["call_id"]] = ([name], False, False)
        elif name in {"exec", "functions.exec"} and isinstance(source, str):
            names = re.findall(r"tools\.(mcp__bazarlyk__[a-zA-Z0-9_]+)\s*\(", source)
            if names:
                codex[payload["call_id"]] = (names, "Promise.all" in source,
                    bool(re.search(r"\bfor\s*\(|\.map\s*\(", source)))
    groups = [[name.removeprefix("mcp__bazarlyk__") for name in group.values()
               if name.startswith("mcp__bazarlyk__")] for group in claude.values()]
    groups = [group for group in groups if group]
    widths = Counter(len(group) for group in groups)
    examples = Counter(tuple(group) for group in groups if len(group) > 1)
    return {
        "claude": {"assistant_messages": len(groups), "tool_calls": sum(map(len, groups)),
            "multi_call_messages": sum(count for width, count in widths.items() if width > 1),
            "calls_per_message": dict(sorted(widths.items())), "ungrouped_calls": len(ungrouped),
            "multi_call_shapes": [{"operations": list(shape), "count": count}
                                  for shape, count in examples.most_common(10)]},
        "codex": {"outer_calls_with_static_mcp_references": len(codex),
            "multiple_static_references": sum(len(names) > 1 for names, _, _ in codex.values()),
            "promise_all_markers": sum(parallel for _, parallel, _ in codex.values()),
            "loop_markers": sum(loop for _, _, loop in codex.values()),
            "grouping_or_loop_markers": sum(len(names) > 1 or loop for names, _, loop in codex.values())},
    }


def analyze(path):
    with path.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Transcript copy exceeds the 8 MiB audit bound")
    calls, seen = {}, set()
    sizes, canonical, fields = defaultdict(list), defaultdict(list), Counter()
    counts = Counter()
    first, last = None, None

    def result(identity, names, body, runtime):
        if identity in seen or not any("mcp__bazarlyk__" in name for name in names):
            return
        seen.add(identity)
        text = text_content(body)
        operation = names[0].split("mcp__bazarlyk__")[-1] if len(names) == 1 else "<exec-batch>"
        sizes[operation].append(len(text))
        counts[runtime + "_result_messages"] += 1
        for value in json_objects(text):
            for envelope, wire_chars, duplicate in envelopes(value):
                counts["envelopes"] += 1
                counts["duplicated_envelopes"] += duplicate
                counts["envelope_wire_chars"] += wire_chars
                counts["single_copy_envelope_chars"] += len(compact(envelope))
                canonical[operation].append(len(compact(envelope)))
                for key, child in envelope.items():
                    if key == "data" and isinstance(child, dict):
                        for subkey, subvalue in child.items():
                            fields[operation + ".data." + subkey] += len(compact(subvalue))
                    else:
                        fields["envelope." + key] += len(compact(child))

    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            counts["invalid_json_lines"] += 1
            continue
        if not isinstance(record, dict):
            counts["invalid_json_lines"] += 1
            continue
        records.append(record)
        timestamp = record.get("timestamp")
        if timestamp:
            first = first or timestamp
            last = timestamp
        payload = record.get("payload", {})
        if record.get("type") == "response_item":
            kind = payload.get("type")
            identity = payload.get("call_id")
            if kind in {"function_call", "custom_tool_call"}:
                name = payload.get("name", "")
                calls[identity] = ([name] if "mcp__bazarlyk__" in name else
                    re.findall(r"tools\.([a-zA-Z0-9_]+)", payload.get("input", payload.get("arguments", ""))))
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                result(identity, calls.get(identity, []), payload.get("output"), "codex")
        message = record.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    calls[block["id"]] = [block["name"]]
                elif block.get("type") == "tool_result":
                    identity = block.get("tool_use_id")
                    result(identity, calls.get(identity, []), block.get("content"), "claude")
        if record.get("type") == "system" and record.get("hookAdditionalContext"):
            # Hook logs may repeat delivered content: report separately, not as MCP.
            counts["logged_hook_context_chars"] += len(text_content(record["hookAdditionalContext"]))
    return {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "first_timestamp": first, "last_timestamp": last, "counts": dict(counts),
            "results": {key: summarize(value) for key, value in sorted(sizes.items())},
            "canonical_envelopes": {key: summarize(value) for key, value in sorted(canonical.items())},
            "field_chars": dict(fields.most_common(25)), "call_grouping": call_grouping(records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Explicit files or business-owned sessions directories")
    parser.add_argument("--latest-per-directory", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.latest_per_directory <= 20:
        parser.error("latest-per-directory must be between 1 and 20")
    files = set()
    for path in args.paths:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            parser.error("Transcript input cannot follow symlinks")
        if path.is_dir():
            files.update(sorted(path.glob("*.jsonl"), key=lambda item: (item.stat().st_mtime, item.name),
                                reverse=True)[:args.latest_per_directory])
        else:
            files.add(path)
    reports = []
    for path in sorted(files):
        if path.is_symlink() or not path.is_file() or path.suffix != ".jsonl" or path.stat().st_size > 8 * 1024 * 1024:
            parser.error("Expected a regular transcript copy no larger than 8 MiB")
        reports.append(analyze(path))
    print(json.dumps({"measurement": "characters, not provider tokens; logged copies are not proof of model-visible context",
                      "sessions": reports}, indent=2))


if __name__ == "__main__":
    main()
