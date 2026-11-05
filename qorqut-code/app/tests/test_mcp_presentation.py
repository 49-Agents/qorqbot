import copy
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.transport.mcp import tool_schema
from bazarlyk.transport.mcp_presentation import COMPACT_OPERATIONS, present, split_detail, split_tool_names
from scripts.audit_mcp_context import analyze, compact, envelopes, json_objects
from test_service_transports import ORIGIN
from mcp_efficiency_support import isolated_service


@pytest.fixture
def service(tmp_path):
    with isolated_service(tmp_path) as value:
        yield value


def envelope(data, **extra):
    return {"request_id": "correlation", "status": "ok", "data": data,
            "warnings": ["Preserve this warning"], "configuration_revision": "current",
            "context_refresh_required": True, **extra}


@pytest.mark.parametrize("name,data,key", [
    ("tasks_comment", {"task": {"id": "t", "revision": 8, "status": "blocked", "assignee_ids": ["a"],
        "blocked_by": ["b"], "unresolved_blockers": ["b"], "notes": [{"body": "x" * 20000}], "fields": {"instructions": "y" * 10000}}}, "task"),
    ("stories_note_add", {"story": {"id": "s", "revision": 4, "sequence": 7, "state": "working", "details": "x" * 30000},
        "event": {"id": "e", "story_id": "s", "sequence": 7, "kind": "note", "body": "x" * 20000}}, "story"),
    ("tables_rows_write", {"table": {"id": "t", "revision": 9, "columns": [{"description": "x" * 30000}]},
        "rows": [{"id": "r", "revision": 2, "table_id": "t", "cells": {"body": "x" * 20000}}],
        "deleted_ids": ["old"], "job_id": "history-job"}, "table"),
])
def test_compact_receipts_are_lossless_for_action_metadata_not_echoes(name, data, key):
    full = envelope(data, job={"id": "j", "state": "queued"})
    before = copy.deepcopy(full)
    result = present(name, full)
    assert result["response_detail"] == "compact"
    assert result["data"][key]["id"] == data[key]["id"]
    assert result["data"][key]["revision"] == data[key]["revision"]
    for field in ("request_id", "status", "warnings", "configuration_revision", "context_refresh_required", "job"):
        assert result[field] == full[field]
    assert len(compact(result)) < len(compact(full)) * .1
    assert present(name, full, "full") == full == before
    if name == "tasks_comment":
        assert result["data"]["task"]["unresolved_blockers"] == ["b"]
        assert result["data"]["task"]["assignee_ids"] == ["a"]
        assert result["data"]["task"]["note_count"] == 1
    elif name == "stories_note_add":
        assert result["data"]["event"] == {"id": "e", "story_id": "s", "sequence": 7, "kind": "note"}
    else:
        assert result["data"]["rows"] == [{"id": "r", "revision": 2, "table_id": "t"}]
        assert result["data"]["deleted_ids"] == ["old"] and result["data"]["job_id"] == "history-job"


def test_describe_returns_schema_only_for_a_single_named_tool():
    schema = {"type": "object", "properties": {"x": {"type": "string", "description": "d" * 2000}}}
    tools = [{"name": "tasks_get", "available": True, "mode": "allow", "description": "Read", "input_schema": schema},
             {"name": "tasks_update", "available": True, "mode": "allow", "description": "Write", "input_schema": schema}]
    many = envelope({"tools": tools, "plugins": []})
    result = present("operations_describe", many)
    assert all("input_schema" not in tool for tool in result["data"]["tools"])
    assert result["data"]["tools"][0]["mode"] == "allow"
    one = envelope({"tools": tools[:1], "plugins": []})
    assert present("operations_describe", one)["data"]["tools"][0]["input_schema"] == schema
    assert present("operations_describe", many, "full") is many


def test_text_detail_renders_yaml_once_without_json_escaping():
    from bazarlyk.transport.mcp_presentation import finalize, serialize
    value = present("tasks_list", envelope({"tasks": [
        {"id": "a1", "revision": 3, "title": 'Say "hello" to Acme', "status": "waiting", "notes": ["x"]}],
        "next_cursor": None}), "text")
    assert value["response_detail"] == "text"
    rendered = serialize(finalize(value))
    assert rendered.startswith("response_detail: text\n")
    assert "status: ok" in rendered and "Say \\\"hello\\\"" not in rendered
    assert "request_id" not in rendered and "note_count: 1" in rendered
    # Multi-line bodies render as literal blocks, not escaped scalars.
    body = present("tasks_get", envelope({"task": {"id": "t", "revision": 1, "notes": [
        {"id": "n", "body": "line one\nline two", "created_at": "now"}]}}), "text")
    block = serialize(finalize(body))
    assert "|-\n" in block and "\\n" not in block
    # JSON remains the wire for compact/full and for errors.
    assert serialize(finalize(envelope({"tasks": []}))).startswith("{")
    error = {"request_id": "c", "status": "error", "error": {"code": "boom"}, "response_detail": "text"}
    assert serialize(error).startswith("{")


def test_batches_reject_text_detail_entries():
    from bazarlyk.transport.batches import validate_entries
    with pytest.raises(DomainError):
        validate_entries({"operations": [
            {"id": "1", "operation": "tasks_list", "arguments": {"response_detail": "text"}}]})
    validate_entries({"operations": [
        {"id": "1", "operation": "tasks_list", "arguments": {"response_detail": "compact"}}]})


def test_finalize_strips_dead_scaffolding_only_on_success():
    from bazarlyk.transport.mcp_presentation import finalize
    ok = {"request_id": "c", "status": "ok", "data": {"task": {"id": "t"}}, "warnings": [],
          "configuration_revision": "sha256:x", "context_refresh_required": False, "response_detail": "compact"}
    slim = finalize(ok)
    assert slim == {"status": "ok", "data": {"task": {"id": "t"}}, "response_detail": "compact"}
    assert ok["request_id"] == "c"  # original untouched
    warned = finalize({**ok, "warnings": ["Keep me"]})
    assert warned["warnings"] == ["Keep me"]
    refresh = finalize({**ok, "context_refresh_required": True})
    assert refresh["context_refresh_required"] is True
    assert refresh["configuration_revision"] == "sha256:x"
    error = {"request_id": "c", "status": "error", "error": {"code": "boom"}}
    assert finalize(error) is error
    assert finalize("text") == "text"


def test_finalize_slims_batch_sub_envelopes_without_touching_receipts():
    from bazarlyk.transport.mcp_presentation import finalize
    inner_ok = {"request_id": "i", "status": "ok", "data": {"task": {"id": "t"}}, "warnings": [],
                "configuration_revision": "sha256:x", "context_refresh_required": False}
    inner_error = {"request_id": "e", "status": "error", "error": {"code": "stale"}}
    batch = {"request_id": "outer", "status": "ok", "warnings": [],
             "data": {"results": [
                 {"id": "1", "operation": "tasks_update", "state": "returned", "result": inner_ok},
                 {"id": "2", "operation": "tasks_update", "state": "returned", "result": inner_error},
                 {"id": "3", "operation": "tasks_update", "state": "not_executed", "reason": "stop"}],
                 "attempted": 2, "total": 3, "all_ok": False, "stopped_after": "2", "stop_reason": "operation_error"}}
    slim = finalize(batch)
    assert "request_id" not in slim
    rows = slim["data"]["results"]
    assert rows[0]["result"] == {"status": "ok", "data": {"task": {"id": "t"}}}
    assert rows[1]["result"] is inner_error
    assert rows[2] == batch["data"]["results"][2]
    assert slim["data"]["attempted"] == 2 and slim["data"]["all_ok"] is False
    # A domain payload that merely resembles a batch keeps its rows untouched.
    lookalike = {"status": "ok", "data": {"results": [{"result": {"score": 1}}]}, "request_id": "c"}
    assert finalize(lookalike)["data"]["results"] == [{"result": {"score": 1}}]


def test_task_index_preserves_cursors_and_empty_lists():
    full = envelope({"tasks": [{"id": "a", "revision": 3, "title": "Needs review", "notes": ["long"]}],
                     "next_cursor": "opaque-page", "has_more": True})
    result = present("tasks_list", full)
    assert result["data"] == {"tasks": [{"id": "a", "revision": 3, "title": "Needs review", "note_count": 1}],
                               "next_cursor": "opaque-page", "has_more": True}
    # Empty and default fields are elided from compact projections: an empty
    # page reads as an empty data object, and full detail keeps the shape.
    empty = envelope({"tasks": [], "next_cursor": None})
    assert present("tasks_list", empty)["data"] == {}
    assert present("tasks_list", empty, "full")["data"] == {"tasks": [], "next_cursor": None}


def test_compact_elision_drops_defaults_but_keeps_meaningful_values():
    from bazarlyk.transport.mcp_presentation import elide
    assert elide({"origin": None, "tags": [], "fields": {}, "title": "", "closed": False,
                  "paused": False, "archived": False, "has_more": False, "position": 0,
                  "available": False, "actionable": False, "ready": False,
                  "cells": {"note": None, "count": 0, "opt_out": False},
                  "attention": {"notify": False, "urgent": False, "blocking": False},
                  "due": "2026-09-20", "blocked_by": ["b"]}) == {
        "position": 0, "available": False, "actionable": False, "ready": False,
        "cells": {"note": None, "count": 0, "opt_out": False},
        # Attention is read-before-write state: all-false stays explicit.
        "attention": {"notify": False, "urgent": False, "blocking": False},
        "due": "2026-09-20", "blocked_by": ["b"]}
    assert elide([{"a": None}, {"b": 1}]) == [{}, {"b": 1}]


def test_terminal_message_delivery_survives_compact_receipt_and_history():
    message = {"id": "m", "body": "Private text", "terminal_delivery": {
        "action_id": "m", "state": "uncertain", "detail": "Inspect before retrying"}}
    receipt = present("agents_message_send", envelope({"message": message}))["data"]["message"]
    assert receipt["terminal_delivery"] == message["terminal_delivery"]
    assert "body" not in receipt
    history = present("agents_messages_list", envelope({"items": [message]}))["data"]["items"][0]
    assert history["terminal_delivery"] == message["terminal_delivery"]


def test_authored_values_and_worker_provenance_survive_compact_projection():
    task = {"id": "t", "revision": 2, "title": "Work", "fields": {
        "blocked": False, "stale": False, "zero": 0, "empty": "", "none": None, "list": []}}
    task_view = present("tasks_get", envelope({"task": task}))
    assert task_view["data"]["task"]["fields"] == task["fields"]

    message = {"id": "m", "event_id": 1, "thread_id": "m", "reply_to": None,
               "sender_id": "worker", "sender_name": "Worker", "recipient_id": "owner",
               "recipient_name": "Owner", "body": "", "summary": "", "created_at": "now",
               "acknowledged_at": None, "outcome": None,
               "delegation_attribution": {"assignment_id": "a", "worker_id": "w", "trusted": False}}
    messages = present("agents_messages_list", envelope({"items": [message]}))
    assert messages["data"]["items"][0]["delegation_attribution"] == message["delegation_attribution"]
    inbox = present("inbox_list", envelope({"items": [{"id": "event", "message": message}]}))
    assert inbox["data"]["items"][0]["message"]["delegation_attribution"] == message["delegation_attribution"]


def test_config_index_preserves_scope_denials_and_readiness():
    full = envelope({"effective_config": {"agent_id": "a", "role": {"id": "business-agent"}, "scope": {"kind": "business", "business_id": "b"},
        "assignment_brief": "Own operations", "plugins": [{"id": "p", "config": {"allowed": ["exact"]}}],
        "tools": [{"canonical_operation": "denied", "available": False, "effective_mode": "deny", "denial_reason": "No grant", "input_schema": "x" * 20000}],
        "preparation": {"ready": False, "projection_drift": ["file"], "runtimes": {"codex": {"ready": False, "projection_drift": ["file"], "compatibility": "x" * 10000}}},
        "mandatory_context": {"rendered": "x" * 20000}}, "work_briefing": {"rendered": "x" * 20000}})
    result = present("agent_config_get", full)
    config = result["data"]["effective_config"]
    assert config["scope"] == full["data"]["effective_config"]["scope"]
    assert config["assignment_brief"] == "Own operations"
    assert "tools" not in config and config["tool_inventory_omitted"]
    assert config["tool_counts"] == {"deny": 1}
    targeted = present("agent_config_get", full, tool_names=["denied", "unknown"])["data"]["effective_config"]
    assert targeted["tools"][0] == {"canonical_operation": "denied", "available": False, "effective_mode": "deny", "denial_reason": "No grant"}
    assert targeted["unknown_tools"] == ["unknown"]
    assert config["preparation"]["runtimes"]["codex"] == {"ready": False, "projection_drift": ["file"]}
    assert "work_briefing" not in result["data"] and "mandatory_context" not in config
    assert len(compact(result)) < len(compact(full)) * .1


def test_config_context_preserves_exact_mandatory_rules_and_bounds_inventory():
    tools = [{"canonical_operation": f"operation_{i}", "exposed_aliases": [f"alias_{i}"],
        "effective_mode": "allow", "available": True, "denial_reason": None} for i in range(300)]
    mandatory = {"rendered": "Exact mandatory rules\n" * 100, "bundle_hash": "exact-hash",
                 "character_count": 2200, "entries": [{"diagnostic": "x" * 10000}]}
    value = envelope({"effective_config": {"agent_id": "a", "tools": tools, "mandatory_context": mandatory},
                      "work_briefing": {"rendered": "large" * 10000}})
    original = copy.deepcopy(value)
    index = present("agent_config_get", value)
    assert len(compact(index)) < 1000
    context = present("agent_config_get", value, "context", tool_names=["alias_5", "operation_5", "missing"])
    selected = context["data"]["effective_config"]
    assert context["response_detail"] == "context"
    assert selected["mandatory_context"] == {k: mandatory[k] for k in ("rendered", "bundle_hash", "character_count")}
    assert len(selected["tools"]) == 1 and selected["tools"][0]["canonical_operation"] == "operation_5"
    assert selected["unknown_tools"] == ["missing"]
    assert "work_briefing" not in context["data"]
    assert present("agent_config_get", value, "full") == original == value


def test_config_sections_return_named_rules_and_hash_the_rest():
    from bazarlyk.transport.mcp_presentation import split_sections
    rendered = ("# Qorqut Configuration sha256:head\npreamble\n"
                "## Core Product\n[source=profile:x; revision=3]\nproduct rules\n"
                "## Team\nteam rules\n"
                "## Agent inbox\ninbox rules\n")
    mandatory = {"rendered": rendered, "bundle_hash": "whole", "character_count": len(rendered),
                 "entries": [{"diagnostic": "x" * 5000}]}
    value = envelope({"effective_config": {"agent_id": "a", "tools": [], "mandatory_context": mandatory}})
    args, sections = split_sections("agent_config_get", {"sections": ["Core Product", "Missing"]}, "context")
    assert args == {} and sections == ["Core Product", "Missing"]
    result = present("agent_config_get", value, "context", sections=sections)
    context = result["data"]["effective_config"]["mandatory_context"]
    assert context["rendered"] == ("# Qorqut Configuration sha256:head\npreamble\n"
                                   "## Core Product\n[source=profile:x; revision=3]\nproduct rules\n")
    assert context["bundle_hash"] == "whole"
    assert [entry["section"] for entry in context["omitted_sections"]] == ["Team", "Agent inbox"]
    assert all(len(entry["sha256"]) == 64 and entry["characters"] > 0 for entry in context["omitted_sections"])
    assert context["unknown_sections"] == ["Missing"]
    # No selector keeps the exact full rendered bundle.
    whole = present("agent_config_get", value, "context")
    assert whole["data"]["effective_config"]["mandatory_context"]["rendered"] == rendered
    for name, detail, selection in (("agent_config_get", "compact", ["Team"]),
                                    ("agent_config_get", "context", []),
                                    ("agent_config_get", "context", ["x"] * 21),
                                    ("tasks_list", "compact", ["Team"])):
        with pytest.raises(DomainError):
            split_sections(name, {"sections": selection}, detail)


@pytest.mark.parametrize("names", [[], ["x"] * 2, ["x"] * 21, [None], [{}], "x", ["BAD"], ["x" * 101]])
def test_named_config_lookup_rejects_invalid_selection(names):
    with pytest.raises(DomainError):
        split_tool_names("agent_config_get", {"tool_names": names}, "compact")


def test_config_selectors_are_transport_only():
    original = {"business_id": "b", "response_detail": "context", "tool_names": ["tasks_create"]}
    args, detail = split_detail("agent_config_get", original)
    args, names = split_tool_names("agent_config_get", args, detail)
    assert args == {"business_id": "b"} and names == ["tasks_create"]
    assert original["response_detail"] == "context" and "tool_names" in original
    for name, mode in (("tasks_list", "compact"), ("agent_config_get", "full")):
        with pytest.raises(DomainError):
            split_tool_names(name, {"tool_names": ["tasks_create"]}, mode)
    with pytest.raises(DomainError):
        split_detail("signoffs_list", {"response_detail": "context"})


def test_signoff_index_keeps_decision_bindings_attention_and_pagination():
    row = {"id": "s", "proposal_revision": 2, "status_revision": 3, "payload_hash": "exact",
        "review_scope": {"kind": "global"}, "category": "input_request", "state": "answered",
        "decision_state": "answered", "execution_state": "not_applicable", "actionable": False,
        "attention": {"notify": True, "urgent": True, "blocking": True, "snoozed_until": "later"},
        "superseded_by_id": "new", "decision": {"notes": "answer" * 1000},
        "input": {"choices": ["x" * 10000]}, "platform": {"body": "evidence" * 5000}}
    value = envelope({"items": [row], "counts": {"answered": 1}, "next_cursor": "page",
                      "updated_cursor": "delta", "has_more": True, "review_business_id": None})
    result = present("signoffs_list", value)
    compact_row = result["data"]["items"][0]
    # The index keeps status bindings and attention; decision evidence, the
    # payload hash and duplicate identity spellings come from a fresh
    # signoffs_get, which the review procedure requires before deciding.
    assert compact_row == {k: v for k, v in row.items()
                           if k not in {"decision", "input", "platform", "payload_hash"}}
    # Null defaults are elided; decision bindings, counts and cursors survive.
    assert {k: v for k, v in result["data"].items() if k != "items"} == {
        k: v for k, v in value["data"].items() if k != "items" and v is not None}
    assert result["warnings"] == value["warnings"]
    assert len(compact(result)) < len(compact(value)) * .1
    assert present("signoffs_list", value, "full") is value
    assert present("signoffs_get", value, "full") is value


def test_signoff_get_keeps_bindings_and_evidence_but_compacts_previews():
    matrix = [{"canonical_operation": f"op_{i}", "effective_mode": "allow"} for i in range(95)]
    signoff = {"id": "s", "proposal_revision": 2, "status_revision": 3, "payload_hash": "bound",
               "state": "pending", "actionable": True, "category": "platform_action",
               "input": {"question": "Q", "choices": ["a", "b"]},
               "platform": {"body": "full email evidence", "recipients": ["x@y.z"]},
               "decision": None,
               "preview": {"title": "Enable plugin", "rationale": "why",
                   "changes": [
                       {"kind": "text", "resource": {"resource_id": "doc"},
                        "before": "same", "after": "same", "diff": ""},
                       {"kind": "text", "resource": {"resource_id": "brief"},
                        "before": "x" * 4000, "after": "y" * 4000, "diff": "@@ -1 +1 @@"},
                       {"kind": "structured", "resource": {"resource_id": "config"},
                        "fields": [{"path": "tools", "before": [], "after": matrix},
                                   {"path": "brief", "before": "", "after": "z" * 3000},
                                   {"path": "noop", "before": 1, "after": 1}]}]}}
    full = envelope({"signoff": signoff})
    result = present("signoffs_get", full)
    kept = result["data"]["signoff"]
    for key in ("payload_hash", "proposal_revision", "status_revision"):
        assert kept[key] == signoff[key]
    assert kept["input"] == {"question": "Q", "choices": ["a", "b"]}
    assert kept["platform"] == signoff["platform"]
    changes = kept["preview"]["changes"]
    assert changes[0] == {"kind": "text", "resource": {"resource_id": "doc"}, "unchanged": True}
    assert changes[1] == {"kind": "text", "resource": {"resource_id": "brief"}, "diff": "@@ -1 +1 @@"}
    assert changes[2]["fields"] == [
        {"path": "tools", "after": {"omitted_entries": 95, "read": "response_detail=full"}},
        {"path": "brief", "after": {"omitted_chars": 3000, "head": "z" * 200, "read": "response_detail=full"}}]
    assert len(compact(result)) < len(compact(full)) * .3
    assert present("signoffs_get", full, "full") is full


def test_inbox_index_keeps_event_identity_and_message_text_without_echoed_wrappers():
    message = {"id": "m", "event_id": 9, "business_id": "b", "sender_id": "s", "sender_name": "Aylin",
               "sender_kind": "agent", "recipient_id": "me", "recipient_name": "Ordubashy",
               "recipient_kind": "agent", "reply_to": None, "thread_id": "m", "body": "Real question",
               "summary": "question", "created_at": "now", "acknowledged_at": None, "outcome": None}
    event = {"id": 9, "business_id": "b", "source_type": "agent_messages", "source_id": "m",
             "actor_id": "s", "actor_name": "Aylin", "event_type": "direct_message",
             "summary": "question", "created_at": "now", "source_revision": 3,
             "business_slug": "acme", "message": message}
    full = envelope({"items": [event], "next_cursor": None})
    result = present("inbox_list", full)
    item = result["data"]["items"][0]
    assert item["id"] == 9 and item["source_id"] == "m" and item["business_id"] == "b"
    assert "actor_id" not in item and "source_revision" not in item
    assert item["message"]["body"] == "Real question" and item["message"]["thread_id"] == "m"
    assert "business_id" not in item["message"] and "sender_kind" not in item["message"]
    assert present("inbox_list", full, "full") is full


def test_inbox_index_keeps_the_bound_source_pointer_for_private_messages():
    global_event = {"id": 20, "business_id": None, "source_type": "owner_conversations", "source_id": "conv-g",
        "actor_id": "owner", "actor_name": "Owner", "event_type": "private_message",
        "summary": "Owner sent a private message", "created_at": "now", "source_revision": 1,
        "business_slug": None,
        "visibility_json": '{"source": {"type": "task", "id": "5442e1ff", "business_id": null}}',
        "source": {"type": "task", "id": "5442e1ff", "business_id": None}}
    business_event = {"id": 21, "business_id": "b", "source_type": "owner_conversations", "source_id": "conv-b",
        "actor_id": "owner", "actor_name": "Owner", "event_type": "private_message",
        "summary": "Owner sent a private message", "created_at": "now", "source_revision": 1,
        "business_slug": "acme",
        "visibility_json": '{"source": {"type": "task", "id": "task-b", "business_id": "b"}}',
        "source": {"type": "task", "id": "task-b", "business_id": "b"}}
    agent_event = {"id": 9, "business_id": "b", "source_type": "agent_messages", "source_id": "m",
        "actor_id": "s", "actor_name": "Aylin", "event_type": "direct_message",
        "summary": "question", "created_at": "now", "source_revision": 3, "business_slug": "acme"}
    full = envelope({"items": [global_event, business_event, agent_event], "next_cursor": None})
    result = present("inbox_list", full)
    global_item, business_item, agent_item = result["data"]["items"]
    # business_id: None on a Global-bound object is meaningful (not a default),
    # so it must survive projection instead of being silently elided.
    assert global_item["source"] == {"type": "task", "id": "5442e1ff", "business_id": None}
    assert business_item["source"] == {"type": "task", "id": "task-b", "business_id": "b"}
    assert business_item["business_id"] == "b"
    for item in (global_item, business_item):
        assert "actor_id" not in item and "source_revision" not in item and "visibility_json" not in item
    # agent_messages events never had a source key; projection must not invent one.
    assert "source" not in agent_item
    assert present("inbox_list", full, "full") is full


def test_activity_index_trims_agents_and_sessions_but_keeps_work_revision():
    session = {"session_id": "sess", "runtime": "codex", "state": "idle", "last_seen_at": "now",
               "work_updated_at": "earlier", "work_reviewed_at": "earlier", "work_revision": 6,
               "items": [{"summary": "task", "task_id": "t"}], "checkpoint": None,
               "inbox_owner": False, "work_stale": False}
    agent = {"agent_id": "a", "name": "business-agent", "display_name": "Aylin", "role_id": "business-agent",
             "business_id": "b", "business_slug": "acme", "business_name": "Acme",
             "state": "idle", "last_seen_at": "now", "sessions": [session],
             "inbox": {"state": "ready", "last_check_at": "now", "lease_until": "later"}}
    full = envelope({"agents": [agent], "next_cursor": None, "as_of": "now"})
    result = present("agents_activity_list", full)
    row = result["data"]["agents"][0]
    assert row["agent_id"] == "a" and row["role_id"] == "business-agent" and row["business_id"] == "b"
    assert "display_name" not in row and "business_name" not in row
    kept = row["sessions"][0]
    assert kept["work_revision"] == 6 and kept["items"] == [{"summary": "task", "task_id": "t"}]
    assert "work_updated_at" not in kept and "inbox_owner" not in kept and "work_stale" not in kept
    assert present("agents_activity_list", full, "full") is full


def test_focus_activity_and_config_summary_contracts_are_explicit_and_bounded():
    source = {"type": "task", "id": "task-1", "business_id": "business-1"}
    reference = {"key": "ref-1", "title": "Preview", "target": {"type": "url", "url": "https://example.test"}}
    context = {"type": "story", "id": "story-1", "business_id": "business-1"}
    focus = envelope({"items": [
        {"id": "card-1", "revision": 8, "position": 0, "display_name": "Task review", "label": "Task",
         "source": source, "state": "blocked", "source_revision": 3, "requested_response": "Do the full action.",
         "reason": "The exact reason.", "blocker": False, "urgent": True, "deferred_until": None,
         "private_unread": False, "private_message_count": 0, "resolved_source": {"large": "omit"},
         "references": [{"large": "omit"}]},
        {"id": "card-2", "revision": 4, "position": 1, "reference": reference, "context": context,
         "context_business_slug": "demo", "source_revision": 6, "requested_response": "Open the target.",
         "reason": "Reference reason.", "blocker": True, "urgent": False, "private_unread": True,
         "private_message_count": 2, "opening_target": {"type": "url", "url": "https://example.test"}}],
        "revision": 9, "next_offset": 30, "status": "ready", "browser": {"url": "omit"},
        "desktop_control_url": "https://desktop.invalid"})
    summary = present("focus_get", focus, "summary", arguments={})
    assert summary["response_detail"] == "summary"
    first, second = summary["data"]["items"]
    assert first["source"] == source and first["source_revision"] == 3 and first["revision"] == 8
    assert first["requested_response"] == "Do the full action." and first["reason"] == "The exact reason."
    assert first["scope"] == {"kind": "business", "business_id": "business-1"}
    assert second["card_kind"] == "reference" and second["reference"] == reference
    assert second["context"] == context and second["context_revision"] == 6
    assert "resolved_source" not in first and "references" not in first
    assert "browser" not in summary["data"] and "desktop_control_url" not in summary["data"]
    assert summary["data"]["next_offset"] == 30 and summary["data"]["revision"] == 9
    global_summary = present("focus_get", envelope({"items": [{"id": "global", "revision": 1,
        "source": {"type": "task", "id": "global-task", "business_id": None}, "state": "open",
        "source_revision": 2, "requested_response": "", "reason": "", "blocker": False,
        "urgent": False}], "next_offset": None}), "summary")
    assert global_summary["data"]["items"][0]["scope"] == {"kind": "global", "business_id": None}
    assert "next_offset" in global_summary["data"] and global_summary["data"]["next_offset"] is None

    activity = envelope({"agents": [{"agent_id": "a", "name": "worker", "display_name": "Worker",
        "role_id": "role", "business_id": "b", "business_slug": "demo", "state": "running",
        "last_seen_at": "now", "inbox": {"state": "ready"}, "sessions": [
            {"session_id": "s-old", "runtime": "codex", "state": "idle", "last_seen_at": "old",
             "work_revision": 2, "items": [{"summary": "old work", "blocked": False}],
             "checkpoint": {"state": "checked", "last_checked_at": "old", "outstanding_count": 2,
                 "issues": [{"finding_id": "f-1", "code": "x", "message": "same", "correction_attempted": False}]}},
            {"session_id": "s-new", "runtime": "codex", "state": "running", "last_seen_at": "now",
             "work_revision": 5, "items": [{"summary": "new work", "blocked": True}],
             "checkpoint": {"state": "checked", "last_checked_at": "now", "outstanding_count": 2,
                 "issues": [{"finding_id": "f-1", "code": "x", "message": "same", "correction_attempted": False}]}}]}],
        "next_cursor": "more", "as_of": "now"})
    activity_summary = present("agents_activity_list", activity, "summary")
    agent = activity_summary["data"]["agents"][0]
    assert len(agent["sessions"]) == 2
    assert [s["work_revision"] for s in agent["sessions"]] == [2, 5]
    assert agent["checkpoint_findings"]["items"] == [activity["data"]["agents"][0]["sessions"][0]["checkpoint"]["issues"][0]]
    assert [s["checkpoint"]["outstanding_count"] for s in agent["sessions"]] == [2, 2]
    assert agent["work_items_replacement_safe"] is False
    assert activity_summary["data"]["next_cursor"] == "more"

    config = envelope({"effective_config": {"schema_version": 1, "agent_id": "a", "profile_revision": 4,
        "name": "worker", "display_name": "Worker", "status": "active", "runtime": "codex",
        "role": {"id": "role", "version": "1.0.0", "digest": "private"},
        "scope": {"kind": "business", "business_id": "b"}, "business_id": "b", "business_revision": 7,
        "configuration_revision": "cfg-7", "context_refresh_required": True,
        "plugins": [{"id": "private", "version": "9"}], "skills": [{"name": "private", "description": "private", "generated_path": "/private"}],
        "tools": [{"canonical_operation": "private", "effective_mode": "allow"}],
        "preparation": {"ready": False, "status": "refresh_required", "projection_drift": ["PLAN.md"],
                         "missing_artifacts": ["role"], "runtimes": {"codex": {"ready": False}}}},
        "work_briefing": {"rendered": "private"}})
    config_summary = present("agent_config_get", config, "summary")["data"]["effective_config"]
    assert config_summary["role"] == {"id": "role", "version": "1.0.0"}
    assert {k: config_summary["readiness"][k] for k in ("ready", "status", "refresh_required", "problem")} == {
        "ready": False, "status": "refresh_required", "refresh_required": True, "problem": True}
    assert "diagnostics" in config_summary["readiness"]["next_read"]
    assert "mandatory instructions" in config_summary["not_included"]
    assert "plugins" not in config_summary and "skills" not in config_summary and "tools" not in config_summary
    assert "work_briefing" not in present("agent_config_get", config, "summary")["data"]


def test_summary_rejects_incompatible_selectors_and_tool_checks():
    with pytest.raises(DomainError, match="does not include conversations"):
        split_detail("focus_get", {"response_detail": "summary", "item_id": "card", "conversation": True})
    with pytest.raises(DomainError, match="compact/context"):
        split_tool_names("agent_config_get", {"tool_names": ["tasks_get"]}, "summary")


def test_tool_schemas_advertise_summary_only_for_bounded_reads():
    descriptors = {
        name: SimpleNamespace(name=name, input_schema={"type": "object", "properties": {}},
            transport_only=False, effect_kind="read", alias_bindings={})
        for name in ("focus_get", "agents_activity_list", "agent_config_get", "tasks_get")}
    registry = SimpleNamespace(get=lambda name: descriptors[name])
    assert "summary" in tool_schema(registry, "focus_get")["properties"]["response_detail"]["enum"]
    assert "summary" in tool_schema(registry, "agents_activity_list")["properties"]["response_detail"]["enum"]
    assert "summary" in tool_schema(registry, "agent_config_get")["properties"]["response_detail"]["enum"]
    assert "summary" not in tool_schema(registry, "tasks_get")["properties"]["response_detail"]["enum"]


def test_recipient_and_row_indexes_keep_send_and_write_identities():
    recipient = {"agent_id": "a", "name": "business-agent", "display_name": "Aylin", "role_id": "business-agent",
                 "business_id": "b", "business_slug": "acme", "business_name": "Acme",
                 "presentation": {"emoji": "x"}, "role": {"id": "business-agent"}, "group": "Businesses"}
    full = envelope({"items": [recipient], "businesses": [{"business_id": "b", "business_slug": "acme"}]})
    for operation in ("agents_message_recipients", "broadcast_recipients"):
        result = present(operation, full)
        row = result["data"]["items"][0]
        assert row["agent_id"] == "a" and row["business_id"] == "b"
        assert "display_name" not in row and "presentation" not in row and "role" not in row
        assert result["data"]["businesses"] == full["data"]["businesses"]
    table_row = {"business_id": "b", "id": "r", "table_id": "t", "position": 2, "revision": 3,
                 "cells": {"company": "Acme", "opt_out": False, "notes": None},
                 "source_upload_id": None, "created_by": "u", "updated_by": "u",
                 "created_at": "now", "updated_at": "now"}
    listed = present("tables_rows_list", envelope({"items": [table_row], "next_cursor": "page"}))
    kept = listed["data"]["items"][0]
    assert kept == {"id": "r", "position": 2, "revision": 3,
                    "cells": {"company": "Acme", "opt_out": False, "notes": None}}
    assert listed["data"]["next_cursor"] == "page"


def test_task_notes_keep_text_and_refs_without_parent_echoes():
    note = {"id": "n", "business_id": "b", "task_id": "t", "body": "Finding", "refs": [{"type": "signoff", "id": "s"}],
            "created_by": "u", "created_by_kind": "agent", "created_by_name": "Ordubashy", "created_at": "now"}
    task = {"id": "t", "revision": 2, "title": "Work", "status": "active", "notes": [note], "origin": None}
    full = envelope({"task": task})
    result = present("tasks_get", full)
    kept = result["data"]["task"]["notes"][0]
    assert kept == {"id": "n", "body": "Finding", "created_by_name": "Ordubashy",
                    "created_at": "now", "refs": [{"type": "signoff", "id": "s"}]}
    assert result["data"]["task"]["revision"] == 2 and "origin" not in result["data"]["task"]
    assert present("tasks_get", full, "full") is full
    paged = present("tasks_notes_list", envelope({"notes": [note], "next_cursor": "older"}))
    assert paged["data"]["notes"][0] == kept and paged["data"]["next_cursor"] == "older"


def test_message_send_receipt_drops_the_echoed_body():
    message = {"id": "m", "event_id": 7, "business_id": "b", "sender_id": "s", "recipient_id": "r",
               "reply_to": None, "thread_id": "m", "body": "x" * 16000, "created_at": "now",
               "summary": "short", "sender_name": "Ordubashy", "sender_kind": "agent",
               "recipient_name": "Aylin", "recipient_kind": "agent", "acknowledged_at": None, "outcome": None}
    full = envelope({"message": message})
    result = present("agents_message_send", full)
    assert result["data"]["message"] == {"id": "m", "event_id": 7, "thread_id": "m",
                                         "created_at": "now", "recipient_id": "r", "recipient_name": "Aylin"}
    assert len(compact(result)) < len(compact(full)) * .1
    assert present("agents_message_send", full, "full") is full


def test_input_create_receipt_keeps_decision_bindings_not_the_form():
    signoff = {"id": "s", "proposal_revision": 1, "status_revision": 1, "payload_hash": "bound",
               "review_scope": {"kind": "global"}, "state": "pending", "decision_state": "pending",
               "actionable": True, "execution_state": "awaiting_approval", "category": "input_request",
               "title": "Pick one", "attention": {"notify": True}, "created_at": "now",
               "input": {"question": "x" * 5000, "choices": ["y" * 5000], "response_policy": "owner_only"},
               "preview_json": {"input": {"question": "x" * 5000}}}
    full = envelope({"signoff": signoff})
    result = present("signoffs_input_create", full)
    receipt = result["data"]["signoff"]
    for key in ("id", "proposal_revision", "status_revision", "payload_hash", "state", "actionable"):
        assert receipt[key] == signoff[key]
    assert "input" not in receipt and "preview_json" not in receipt
    assert len(compact(result)) < len(compact(full)) * .1
    assert present("signoffs_input_create", full, "full") is full


def test_focus_writes_return_revision_receipts_not_the_whole_pane():
    card = {"id": "c", "revision": 4, "archived": False, "display_name": "Landing page review",
            "source": {"type": "tasks", "id": "t", "business_id": "b"}, "resolved_source": {"label": "x" * 3000},
            "requested_response": "y" * 2000}
    full = envelope({"item": card, "outcome": "presented"})
    result = present("focus_present", full)
    assert result["data"]["item"] == {"id": "c", "revision": 4}
    assert result["data"]["outcome"] == "presented"
    assert present("focus_present", full, "full") is full
    # The retired focus_show/focus_clear/focus_browser_input trio always answers
    # upgrade_required, so it is deliberately outside the compact set.
    assert present("focus_show", envelope({"revision": 9, "items": []})) == envelope({"revision": 9, "items": []})


@pytest.mark.parametrize("status", ["error", "signoff_required", "accepted"])
def test_non_success_results_never_project_away_decisions(status):
    value = {"request_id": "c", "status": status, "error": {"code": "stale", "current_revision": 4},
             "signoff": {"id": "s", "payload_hash": "bound"}, "job": {"id": "j"}}
    for operation in COMPACT_OPERATIONS:
        assert present(operation, value) is value
    assert present("unknown", envelope({"anything": "long"})) == envelope({"anything": "long"})


def test_detail_validation_and_non_mutating_argument_split():
    args = {"task_id": "t", "response_detail": "full", "idempotency_key": "same"}
    clean, mode = split_detail("tasks_update", args)
    assert clean == {"task_id": "t", "idempotency_key": "same"} and mode == "full"
    assert args["response_detail"] == "full"
    for name, arguments in (("tasks_update", {"response_detail": "invalid"}),
                            ("operations_batch", {"response_detail": "full"}), ("tasks_update", [])):
        with pytest.raises(DomainError):
            split_detail(name, arguments)


def test_transport_single_copy_full_replay_and_read_schema(service):
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        listing = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).json()["result"]["tools"]
        assert all("outputSchema" not in tool for tool in listing)
        schemas = {tool["name"]: tool["inputSchema"] for tool in listing}
        assert "expected_revision" not in schemas["tasks_list"]["properties"]
        assert "idempotency_key" not in schemas["tasks_get"]["properties"]
        assert "business_id" in schemas["tasks_get"]["properties"]
        assert "idempotency_key" in schemas["tasks_create"]["required"]
        assert schemas["tasks_list"]["properties"]["response_detail"]["default"] == "compact"
        key = new_id()
        arguments = {"title": "Write once", "idempotency_key": key}
        receipt = service.tool(client, headers, "tasks_create", arguments)
        assert not receipt["isError"] and receipt["decoded"]["response_detail"] == "compact"
        full = service.tool(client, headers, "tasks_create", {**arguments, "response_detail": "full"})["decoded"]
        assert "response_detail" not in full and "notes" in full["data"]["task"]
        assert full["data"]["task"]["id"] == receipt["decoded"]["data"]["task"]["id"]
        assert full["data"]["task"]["revision"] == 1
        ctx = service.runtime.resolver.invocation_context(service.manager)
        canonical = service.runtime.application.invoke(ctx, "tasks_create", {"title": "Write once"}, InvocationOptions(idempotency_key=key))
        assert canonical.data["task"]["id"] == full["data"]["task"]["id"]
        assert canonical.to_dict()["data"] == full["data"]
        with service.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks WHERE title='Write once'").fetchone()[0] == 1
        error = service.tool(client, headers, "tasks_create", {**arguments, "response_detail": "bad"})
        assert error["isError"] and error["decoded"]["error"]["code"] == "validation_error"
        summary = service.tool(client, headers, "tasks_list", {})["decoded"]
        details = service.tool(client, headers, "tasks_list", {"response_detail": "full"})["decoded"]
        assert "notes" not in summary["data"]["tasks"][0] and "notes" in details["data"]["tasks"][0]

        gated = client.post("/api/biz/widget/operations/permissions_set", headers=ORIGIN, json={
            "agent_id": service.manager.id, "overrides": {"tasks_create": "signoff"},
            "expected_revision": 1, "idempotency_key": new_id()})
        assert gated.status_code == 200, gated.text
        proposal_args = {"title": "Needs approval", "idempotency_key": new_id()}
        proposal = service.tool(client, headers, "tasks_create", proposal_args)["decoded"]
        replay = service.tool(client, headers, "tasks_create", {**proposal_args, "response_detail": "full"})["decoded"]
        assert proposal["status"] == replay["status"] == "signoff_required"
        assert proposal["signoff"] == replay["signoff"]
        assert "response_detail" not in proposal
        with service.fixture.db.read() as uow:
            assert uow.connection.execute("SELECT COUNT(*) FROM tasks WHERE title='Needs approval'").fetchone()[0] == 0


def test_audit_counts_single_copy_and_duplicate_without_emitting_bodies(tmp_path):
    value = envelope({"secret_field": "DO-NOT-PRINT-THIS"})
    wire = {"content": [{"type": "text", "text": compact(value)}], "structuredContent": value}
    records = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "c", "name": "mcp__bazarlyk__tasks_get", "input": {"private": "DO-NOT-PRINT-THIS"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "c", "content": compact(value)}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "x", "name": "exec", "input": "text(await tools.mcp__bazarlyk__tasks_get({}))"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "x", "output": [{"type": "input_text", "text": "Script completed\n" + compact(wire)}]}},
    ]
    path = tmp_path / "sample.jsonl"
    path.write_text("\n".join(compact(record) for record in [*records, records[1]]) + "\n{broken")
    result = analyze(path)
    assert result["counts"]["envelopes"] == 2 and result["counts"]["duplicated_envelopes"] == 1
    assert result["results"]["tasks_get"]["count"] == 2
    assert "DO-NOT-PRINT-THIS" not in compact(result)
    assert result["counts"]["invalid_json_lines"] == 1
    assert list(envelopes(wire))[0][2]
    assert list(json_objects('{"incomplete":')) == []


def test_audit_marks_mixed_batches_without_guessing_operation_mapping(tmp_path):
    first, second = envelope({"task": {"id": "t"}}), envelope({"items": []})
    output = compact(first) + "\n" + compact(second) + '\n{"incomplete":'
    records = [
        {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "x", "name": "exec",
            "input": "tools.mcp__bazarlyk__tasks_get({}); tools.mcp__bazarlyk__inbox_list({}); tools.exec_command({})"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "x", "output": output}},
    ]
    path = tmp_path / "batch.jsonl"
    path.write_text("\n".join(compact(record) for record in records))
    result = analyze(path)
    assert set(result["results"]) == {"<exec-batch>"}
    assert result["counts"]["envelopes"] == 2
    assert result["counts"]["duplicated_envelopes"] == 0


def test_audit_recognizes_new_text_only_wire_without_counting_it_twice():
    value = envelope({"task": {"id": "t", "revision": 1}})
    wire = {"content": [{"type": "text", "text": compact(value)}], "isError": False}
    assert list(envelopes(wire)) == [(value, len(compact(wire)), False)]
    nested = {"result": wire}
    assert list(envelopes(nested)) == [(value, len(compact(wire)), False)]


def test_real_domain_receipts_keep_full_evidence_and_action_ids(service):
    long_note = "Synthetic evidence. " * 600
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.agent)

        def call(name, args):
            result = service.tool(client, headers, name, args)["decoded"]
            assert result["status"] == "ok", result
            return result

        task = call("tasks_create", {"title": "Compact receipt test", "idempotency_key": new_id()})["data"]["task"]
        note_args = {"task_id": task["id"], "expected_revision": task["revision"], "text": long_note, "idempotency_key": new_id()}
        receipt = call("tasks_comment", note_args)
        full = call("tasks_comment", {**note_args, "response_detail": "full"})
        assert len(compact(receipt)) < len(compact(full)) * .2
        assert long_note in compact(call("tasks_get", {"task_id": task["id"]}))
        assert receipt["data"]["task"]["revision"] == full["data"]["task"]["revision"]

        story = call("stories_create", {"title": "Story receipt", "purpose": "Test projections", "details": long_note, "idempotency_key": new_id()})["data"]["story"]
        link_args = {"story_id": story["id"], "resource_type": "tasks", "resource_id": task["id"], "idempotency_key": new_id()}
        linked = call("stories_link_add", link_args)
        full_link = call("stories_link_add", {**link_args, "response_detail": "full"})
        link_id = linked["data"]["event"]["data_json"]["link_id"]
        assert link_id == full_link["data"]["event"]["data_json"]["link_id"]
        assert len(compact(linked)) < len(compact(full_link)) * .3
        assert long_note in compact(call("stories_get", {"story_id": story["id"]}))
        event_id = linked["data"]["event"]["id"]
        assert call("stories_event_get", {"story_id": story["id"], "event_id": event_id})["data"]["event"]["data_json"]["link_id"] == link_id

        table = call("tables_create", {"title": "Projection table", "columns": [{"id": "body", "name": "Body", "type": "text"}], "idempotency_key": new_id()})["data"]["table"]
        row_args = {"table_id": table["id"], "expected_revision": table["revision"], "rows": [{"cells": {"body": long_note}}], "idempotency_key": new_id()}
        written = call("tables_rows_write", row_args)
        full_rows = call("tables_rows_write", {**row_args, "response_detail": "full"})
        assert len(compact(written)) < len(compact(full_rows)) * .2
        assert written["data"]["rows"][0]["id"] == full_rows["data"]["rows"][0]["id"]
        assert written["data"]["rows"][0]["revision"] == full_rows["data"]["rows"][0]["revision"]
        assert call("tables_rows_list", {"table_id": table["id"]})["data"]["items"][0]["cells"]["body"] == long_note


def test_real_config_selection_and_signoff_index_preserve_full_authority(service):
    from test_signoff_inputs import FORM

    with TestClient(service.web) as client:
        headers = service.initialize(client, service.agent)

        def call(name, args):
            result = service.tool(client, headers, name, args)["decoded"]
            assert result["status"] == "ok", result
            return result

        schemas = {t["name"]: t["inputSchema"] for t in client.post("/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 17, "method": "tools/list"}).json()["result"]["tools"]}
        assert schemas["agent_config_get"]["properties"]["response_detail"]["enum"] == ["compact", "summary", "context", "full"]
        assert "tool_names" not in schemas["tasks_list"]["properties"]
        assert schemas["signoffs_list"]["properties"]["response_detail"]["default"] == "compact"
        minimal = call("agent_config_get", {})
        full = call("agent_config_get", {"response_detail": "full"})
        context = call("agent_config_get", {"response_detail": "context", "tool_names": ["tasks_create", "does_not_exist"]})
        config = context["data"]["effective_config"]
        assert config["mandatory_context"]["rendered"] == full["data"]["effective_config"]["mandatory_context"]["rendered"]
        assert config["mandatory_context"]["bundle_hash"] == full["data"]["effective_config"]["mandatory_context"]["bundle_hash"]
        assert config["tools"][0]["canonical_operation"] == "tasks_create"
        assert config["unknown_tools"] == ["does_not_exist"]
        assert "tools" not in minimal["data"]["effective_config"]
        assert len(compact(minimal)) < len(compact(full)) * .2
        for mode in ("compact", "context", "full"):
            foreign = service.tool(client, headers, "agent_config_get", {"business_id": new_id(), "response_detail": mode})
            assert foreign["isError"] and foreign["decoded"]["status"] == "error"
        batch = call("operations_batch", {"operations": [{"id": "scope", "operation": "agent_config_get",
            "arguments": {"response_detail": "context", "tool_names": ["tasks_create"]}}]})
        assert batch["data"]["results"][0]["result"]["response_detail"] == "context"

        row = call("signoffs_input_create", {"request_key": "compact-index", "form": FORM,
            "attention": {"notify": True, "urgent": True, "blocking": True, "reason": "Needs owner input"},
            "idempotency_key": new_id()})["data"]["signoff"]
        index = call("signoffs_list", {})
        index_row = next(r for r in index["data"]["items"] if r["id"] == row["id"])
        detail = call("signoffs_get", {"signoff_id": row["id"]})["data"]["signoff"]
        full_list = call("signoffs_list", {"response_detail": "full"})
        assert all(detail["input"][key] == value for key, value in FORM.items()) and "input" not in index_row
        assert index_row["attention"] == detail["attention"]
        # Decision bindings come from a fresh signoffs_get; index rows keep the
        # status revision but no longer repeat the payload hash.
        assert "payload_hash" not in index_row and detail["payload_hash"]
        assert index_row["status_revision"] == detail["status_revision"]
        assert any(r["input"] == detail["input"] for r in full_list["data"]["items"])
        assert detail["state"] == "pending"
        canonical = service.runtime.application.invoke(service.runtime.resolver.invocation_context(service.agent),
            "signoffs_list", {}).to_dict()
        assert any(r["input"] == detail["input"] for r in canonical["data"]["items"])
        assert "response_detail" not in canonical


def test_mcp_config_context_skips_live_work_briefing(service, monkeypatch):

    class UnexpectedBriefing:
        def build(self, principal, config):
            raise AssertionError("context projection must not build a work briefing")

    monkeypatch.setattr(service.runtime.resolver, "work_briefing", UnexpectedBriefing())
    with TestClient(service.web) as client:
        headers = service.initialize(client, service.manager)
        for arguments in ({}, {"response_detail": "context"}, {"response_detail": "summary"}):
            result = service.tool(client, headers, "agent_config_get", arguments)["decoded"]
            assert result["status"] == "ok", result
            assert "work_briefing" not in result["data"]
