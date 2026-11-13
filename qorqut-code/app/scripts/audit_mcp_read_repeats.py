"""Audit explicit transcript copies for repeated read payloads, never emit bodies.

This is candidate evidence, NOT a cache-hit forecast: execution grouping, exact
arguments, permission changes and client context retention may be unobservable.
No global discovery, provider calls, runtime changes or transcript writes.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_mcp_context import envelopes, json_objects, text_content
from scripts.measure_mcp_tool_read_efficiency import tokens

READS = frozenset({"agents_activity_list", "agent_config_get", "tasks_get",
                  "tasks_list", "tasks_overview", "tasks_notes_list", "inbox_list",
                  "agents_messages_list", "signoffs_list", "signoffs_get"})
PREFIX = "mcp__qorqut__"
MAX_BYTES = 8 * 1024 * 1024


def audit(path):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file() or path.suffix != ".jsonl":
        raise ValueError("Select a regular, non-symlink transcript copy explicitly")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Transcript exceeds the 8 MiB audit bound")
    calls, seen, previous = {}, set(), {}
    rows = defaultdict(Counter)
    skipped = Counter()

    def result(identity, body):
        if not identity:
            skipped["results_without_call_identity"] += 1
            return
        if identity in seen:
            return
        seen.add(identity)
        names = calls.get(identity, [])
        if len(names) != 1 or names[0] not in READS:
            skipped["unattributed_or_non_read_results"] += 1
            return
        name = names[0]
        found = [entry for value in json_objects(text_content(body)) for entry, _, _ in envelopes(value)]
        if len(found) != 1:
            skipped["ambiguous_or_unparseable_results"] += 1
            return
        value = found[0]
        if value.get("status") != "ok" or value.get("warnings") or value.get("context_refresh_required"):
            rows[name]["non_success_or_attention_results"] += 1
            previous.pop(name, None)
            return
        # Only correlation/false-refresh scaffolding is ignored. Keep all data,
        # timestamps, pagination, instructions, scope and configuration revision.
        compared = {k: v for k, v in value.items() if k != "request_id"}
        encoded = json.dumps(compared, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).digest()
        count = tokens(json.dumps(value, separators=(",", ":")))
        rows[name]["read_results"] += 1
        rows[name]["result_tokens"] += count
        if previous.get(name) == digest:
            rows[name]["repeated_payload_candidates"] += 1
            rows[name]["candidate_tokens"] += count
        previous[name] = digest

    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            skipped["invalid_lines"] += 1
            continue
        if not isinstance(record, dict):
            skipped["invalid_lines"] += 1
            continue
        # Compaction invalidates reuse, even if a later read happens to match.
        if record.get("subtype") == "compact_boundary" or record.get("type") == "compacted":
            previous.clear()
        payload = record.get("payload", {})
        if record.get("type") == "response_item" and isinstance(payload, dict):
            kind, identity = payload.get("type"), payload.get("call_id")
            if kind in {"function_call", "custom_tool_call"}:
                name = payload.get("name", "")
                if name.startswith(PREFIX):
                    names = [name[len(PREFIX):]]
                else:
                    source = payload.get("input", payload.get("arguments", ""))
                    names = re.findall(r"tools\.mcp__qorqut__([a-zA-Z0-9_]+)\s*\(", source) if isinstance(source, str) else []
                    # Loops/dynamic dispatch cannot establish one executed call.
                    if isinstance(source, str) and re.search(r"\b(?:for|while)\s*\(|\.map\s*\(", source):
                        names = []
                calls[identity] = names
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                result(identity, payload.get("output"))
        message = record.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                calls[block.get("id")] = [name[len(PREFIX):]] if name.startswith(PREFIX) else []
            elif block.get("type") == "tool_result":
                result(block.get("tool_use_id"), block.get("content"))
    return {"file": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "operations": {key: dict(value) for key, value in sorted(rows.items())}, "skipped": dict(skipped)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Explicit copied .jsonl files; no directory discovery")
    args = parser.parse_args()
    reports = [audit(path) for path in args.paths]
    print(json.dumps({"tokenizer": "tiktoken 0.14.0 / cl100k_base", "sessions": reports,
        "limitations": "Consecutive matching payloads per operation within each transcript are candidates only. "
            "Not proof of equal arguments, stable permissions, context retention, current runtime or billed savings. "
            "Ambiguous/mixed/truncated outputs are excluded; no source text or arguments are emitted."}, indent=2))


if __name__ == "__main__":
    main()
