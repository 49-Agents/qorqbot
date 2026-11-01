#!/usr/bin/env python3
"""Measure ordinary versus opt-in reads through real, isolated MCP sessions.

Reports cold overhead, unchanged repeats and a context/change/error workflow.
No extrapolated traffic weights, silent token fallback, providers or live data.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from scripts.measure_mcp_tool_read_efficiency import packed, tokens, source_identity


def measure(service):
    from starlette.testclient import TestClient
    from qorqut_operations.contracts import new_id
    from bazarlyk.transport.mcp_reads import OPERATIONS
    from mcp_efficiency_support import seed_reads

    records = seed_reads(service)
    with TestClient(service.web) as client:
        normal_headers = service.initialize(client, service.ordu)
        selected_headers = service.initialize(client, service.ordu)
        history, saved, comparisons = {}, {}, []
        scope = {"business_id": service.fixture.business_id}
        reads = [
            ("tasks_list", scope),
            ("tasks_get", {**scope, "task_id": records["tasks"][1]["id"]}),
            ("focus_get", {"scope": "global", "limit": 2, "offset": 0, "response_detail": "summary"}),
            ("focus_get", {"scope": "global", "limit": 2, "offset": 2, "response_detail": "summary"}),
        ]

        def pair(name, args):
            key = packed({"name": name, "arguments": args})
            selected = {**args, **({"if_read_token": history.get(key, "")} if name in OPERATIONS else {})}
            left = service.tool(client, normal_headers, name, args)
            right = service.tool(client, selected_headers, name, selected)
            normal, current = left["decoded"], right["decoded"]
            assert normal["status"] == current["status"]
            state = current.get("read_state", {})
            if normal["status"] == "ok":
                if state.get("unchanged"):
                    assert current["data"] == {"not_modified": True}
                    assert saved[key] == normal["data"], "Suppressed data no longer equals fresh authorized evidence"
                else:
                    assert current["data"] == normal["data"], "Fresh evidence differs between modes"
                    saved[key] = current["data"]
                if state and not state.get("unchanged"):
                    history[key] = state["token"]
                elif not state:
                    history.pop(key, None)
            else:
                assert current["error"] == normal["error"] and not state
                history.pop(key, None)
            row = {"operation": name, "status": normal["status"], "unchanged": bool(state.get("unchanged")),
                "normal_result_tokens": tokens(left["content"][0]["text"]),
                "selected_result_tokens": tokens(right["content"][0]["text"]),
                "normal_request_tokens": tokens(packed({"name": name, "arguments": args})),
                "selected_request_tokens": tokens(packed({"name": name, "arguments": selected})),
                "evidence_parity": True}
            comparisons.append(row)
            return row

        def total(rows):
            result = {"read_calls_per_mode": len(rows), "unchanged_results": sum(row["unchanged"] for row in rows),
                      "evidence_parity": all(row["evidence_parity"] for row in rows)}
            for mode in ("normal", "selected"):
                result[mode + "_result_tokens"] = sum(row[mode + "_result_tokens"] for row in rows)
                result[mode + "_including_request_tokens"] = sum(row[mode + "_result_tokens"] + row[mode + "_request_tokens"] for row in rows)
            for suffix in ("result_tokens", "including_request_tokens"):
                result[suffix + "_reduction_percent"] = round(100 * (1 - result["selected_" + suffix] / result["normal_" + suffix]), 2)
            return result

        cold = [pair(name, args) for name, args in reads]
        repeat_rounds = []
        for _ in range(4):
            repeat_rounds.append([pair(name, args) for name, args in reads])
        repeat_report = {str(n): total(cold + [row for group in repeat_rounds[:n - 1] for row in group]) for n in (1, 2, 3, 5)}

        # A complete scripted read sequence: business entry, full instructions,
        # both board pages, unchanged follow-ups, source mutation, error/retry.
        # Reset client state: a new workflow cannot assume old data survived.
        history.clear()
        saved.clear()
        mixed = [pair("agent_config_get", {**scope, "response_detail": detail}) for detail in ("summary", "context")]
        mixed.extend(pair(name, args) for name, args in reads)
        mixed.extend(pair(name, args) for name, args in reads)
        task = service.tool(client, normal_headers, "tasks_get", reads[1][1])["decoded"]["data"]["task"]
        mutation = service.tool(client, normal_headers, "tasks_comment", {**reads[1][1],
            "expected_revision": task["revision"], "text": "Supplier confirmed a carton contains twelve units. Recheck the pilot quantities.",
            "idempotency_key": new_id()})["decoded"]
        assert mutation["status"] == "ok", mutation
        changed = [pair(name, args) for name, args in reads]
        assert all(not row["unchanged"] for row in changed), "Source edits must invalidate all affected reads"
        mixed.extend(changed)
        mixed.append(pair("tasks_get", {**scope, "task_id": "missing-benchmark-task"}))
        mixed.append(pair(*reads[1]))
        # If the client loses its data (including compaction), discard tokens
        # and recover full evidence. Count this extra read, not just warm hits.
        history.clear()
        saved.clear()
        mixed.append(pair(*reads[1]))

        definitions = client.post("/mcp", headers=normal_headers, json={"jsonrpc": "2.0", "id": 5,
            "method": "tools/list"}).json()["result"]["tools"]
        selected_definitions = [definition for definition in definitions if definition["name"] in OPERATIONS]
        original_definitions = copy.deepcopy(selected_definitions)
        for definition in original_definitions:
            del definition["inputSchema"]["properties"]["if_read_token"]
        return {"repeat_rounds_including_cold_read": repeat_report, "mixed_workflow": total(mixed),
            "comparisons": comparisons, "source": {**source_identity(ROOT),
                "revalidation_sha256": hashlib.sha256((ROOT / "bazarlyk/transport/mcp_reads.py").read_bytes()).hexdigest()},
            "changed_tool_definition_delta_tokens": tokens(packed(selected_definitions)) - tokens(packed(original_definitions)),
            "tokenizer": "tiktoken 0.14.0 / cl100k_base", "production_target_validated": False,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "limitations": ["Small simulated business, not representative production traffic or billed tokens.",
                "Normal requests are Phase 1 behavior on this same server; only the opt-in selector differs.",
                "Every reported hit is checked against an ordinary fresh read for exact data equality.",
                "Mixed workflow includes full instructions, pages, changed data, errors, retry and lost-context recovery.",
                "Instruction and diagnostic reads never use revalidation. Mutation seeds the changed state; metrics count READS only.",
                "Definitions are measured separately; their delivery frequency, provider caches and model reasoning are not modeled.",
                "No polling frequency or workflow weight is assumed. Generated IDs and signing keys cause small count variation."]}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    tokens("verify pinned tokenizer before fixture creation")
    from mcp_efficiency_support import isolated_service
    with TemporaryDirectory(prefix="mcp-read-revalidation-") as directory:
        with isolated_service(Path(directory)) as service:
            result = measure(service)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
