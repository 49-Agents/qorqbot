import json

import pytest

from scripts.audit_mcp_read_repeats import audit
from scripts.measure_mcp_tool_read_efficiency import tokens


def exchange(identity, operation="tasks_get", *, data=None, extra=None):
    envelope = {"request_id": identity, "status": "ok", "data": data or {"task": {"body": "PRIVATE BODY", "revision": 1}}, **(extra or {})}
    return [
        {"type": "response_item", "payload": {"type": "function_call", "call_id": identity,
            "name": "mcp__bazarlyk__" + operation, "arguments": '{"task_id":"PRIVATE ID"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": identity,
            "output": json.dumps({"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": False})}},
    ]


def run(tmp_path, records):
    path = tmp_path / "explicit-copy.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records))
    return audit(path)


def test_candidate_counts_are_private_scoped_and_deduplicated(tmp_path):
    first, second = exchange("one"), exchange("two")
    result = run(tmp_path, first + first + second + exchange("changed", data={"task": {"revision": 2}}))
    row = result["operations"]["tasks_get"]
    assert row["read_results"] == 3 and row["repeated_payload_candidates"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert result["sha256"] and row["candidate_tokens"] < row["result_tokens"]


@pytest.mark.parametrize("boundary", [
    [{"type": "system", "subtype": "compact_boundary"}],
    [{"type": "compacted"}],
    exchange("warning", extra={"warnings": ["Read this"]}),
    exchange("error", extra={"status": "error", "error": {"code": "forbidden"}}),
    exchange("refresh", extra={"context_refresh_required": True}),
])
def test_context_and_attention_boundaries_prevent_reuse_claims(tmp_path, boundary):
    result = run(tmp_path, exchange("one") + boundary + exchange("two"))
    assert result["operations"]["tasks_get"].get("repeated_payload_candidates", 0) == 0


def test_exec_grouping_and_transformed_results_are_not_guessed(tmp_path):
    records = exchange("one")
    records[0]["payload"] = {"type": "custom_tool_call", "name": "functions.exec", "call_id": "one",
        "input": "text(await tools.mcp__bazarlyk__tasks_get({})); text(await tools.mcp__bazarlyk__focus_get({}));"}
    result = run(tmp_path, records)
    assert not result["operations"] and result["skipped"]["unattributed_or_non_read_results"] == 1
    records = exchange("one")
    records[1]["payload"]["output"] = "truncated {"
    assert run(tmp_path, records)["skipped"]["ambiguous_or_unparseable_results"] == 1


def test_authored_special_token_text_is_measured_as_literal():
    assert tokens('<|endoftext|> is a literal in a tool result') > 0


def test_directory_and_symlink_discovery_are_refused(tmp_path):
    with pytest.raises(ValueError):
        audit(tmp_path)
    target = tmp_path / "copy.jsonl"
    target.write_text("")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        audit(link)


def test_workflow_benchmark_preserves_evidence_and_counts_cold_overhead(tmp_path):
    from mcp_efficiency_support import isolated_service
    from scripts.measure_mcp_read_revalidation import measure
    with isolated_service(tmp_path) as service:
        result = measure(service)
    rounds = result["repeat_rounds_including_cold_read"]
    assert rounds["1"]["unchanged_results"] == 0
    assert rounds["1"]["including_request_tokens_reduction_percent"] < 0
    assert rounds["5"]["unchanged_results"] == 16
    assert rounds["5"]["evidence_parity"]
    assert rounds["5"]["including_request_tokens_reduction_percent"] > 40
    assert result["mixed_workflow"]["read_calls_per_mode"] == 17
    assert result["mixed_workflow"]["evidence_parity"]
    assert result["changed_tool_definition_delta_tokens"] > 0
    assert result["production_target_validated"] is False
