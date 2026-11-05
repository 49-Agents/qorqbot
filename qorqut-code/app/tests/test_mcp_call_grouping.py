from scripts.audit_mcp_context import call_grouping


def test_claude_groups_streaming_fragments_by_message_not_log_line():
    def record(message, identity, name):
        return {"message": {"role": "assistant", "id": message, "content": [
            {"type": "tool_use", "id": identity, "name": "mcp__bazarlyk__" + name,
             "input": {"secret": "PRIVATE-ARGUMENT"}}]}}
    first = record("response1", "call1", "tasks_comment")
    records = [first, record("response1", "call2", "agents_message_send"), first,
               record("response2", "call3", "tasks_get"), record(None, "call4", "tasks_get")]
    result = call_grouping(records)
    assert result["claude"] == {"assistant_messages": 2, "tool_calls": 3,
        "multi_call_messages": 1, "calls_per_message": {1: 1, 2: 1}, "ungrouped_calls": 1,
        "multi_call_shapes": [{"operations": ["tasks_comment", "agents_message_send"], "count": 1}]}
    assert "PRIVATE-ARGUMENT" not in str(result)


def test_codex_counts_program_shapes_not_loop_iterations():
    def record(identity, source):
        return {"type": "response_item", "payload": {"type": "custom_tool_call",
            "name": "exec", "call_id": identity, "input": source}}
    first = record("1", "await tools.mcp__bazarlyk__tasks_complete({}); await tools.mcp__bazarlyk__agents_message_send({});")
    result = call_grouping([first, first,
        record("2", "await Promise.all(items.map(x => tools.mcp__bazarlyk__tasks_get(x)))"),
        record("3", "for (const x of items) await tools.mcp__bazarlyk__tasks_create(x)"),
        record("4", "await tools.exec_command({})")])
    assert result["codex"] == {"outer_calls_with_static_mcp_references": 3,
        "multiple_static_references": 1, "promise_all_markers": 1, "loop_markers": 2,
        "grouping_or_loop_markers": 3}
