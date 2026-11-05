"""Small harness contract tests; no providers, billing APIs or live snapshots."""
from pathlib import Path
import sys

import pytest

from scripts import measure_mcp_tool_read_efficiency as benchmark


def test_tokenizer_failure_never_falls_back_to_characters(monkeypatch):
    benchmark.tokenizer.cache_clear()
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    with pytest.raises(RuntimeError, match="No character estimate"):
        benchmark.tokens("a real token count is required")
    benchmark.tokenizer.cache_clear()


def test_measurement_uses_the_final_text_and_real_schema_definitions(tmp_path):
    from mcp_efficiency_support import isolated_service
    from bazarlyk.transport.mcp import tool_schema
    from bazarlyk.transport.mcp_presentation import description, finalize, present, serialize
    with isolated_service(tmp_path) as service:
        descriptor = service.runtime.registry.get("agent_config_get")
    value = {"status": "ok", "request_id": "discard-this", "warnings": [],
             "configuration_revision": "discard-that", "context_refresh_required": False,
             "data": {"effective_config": {"name": "worker", "mandatory_context": {"rendered": "Complete instructions"}}}}
    spec = {"name": descriptor.name, "input_schema": dict(descriptor.input_schema), "effect_kind": "read",
            "transport_only": False, "alias_bindings": {}, "description": descriptor.description}
    corpus = {"cases": {"context": {"operation": "agent_config_get", "value": value,
              "arguments": {}, "after_detail": "context"}}, "descriptors": {descriptor.name: spec}}
    result = benchmark.measure(Path(__file__).resolve().parents[1], "phase1", corpus)
    expected = serialize(finalize(present("agent_config_get", value, "context")))
    assert "discard-this" not in expected and "Complete instructions" in expected
    assert result["measurements"]["context"]["tokens"] == benchmark.tokens(expected)
    from types import SimpleNamespace
    registry = SimpleNamespace(get=lambda _: SimpleNamespace(**spec))
    definition = [{"name": descriptor.name, "description": descriptor.description + description(descriptor.name),
                   "inputSchema": tool_schema(registry, descriptor.name)}]
    assert result["three_tool_definition_tokens"] == benchmark.tokens(benchmark.packed(definition))


def test_complete_sequences_count_context_extra_reads_and_retries():
    metrics = {key: {"tokens": amount, "request_tokens": 2} for key, amount in
               {"config": 100, "context": 1000, "detail": 200, "error": 50}.items()}
    before = {"measurements": metrics, "three_tool_definition_tokens": 200}
    after = {"measurements": {**metrics, "config": {"tokens": 20, "request_tokens": 5}}, "three_tool_definition_tokens": 210}
    corpus = {"scenarios": {name: {"before": ["config", "context", "error", "detail"],
                                   "after": ["config", "context", "error", "detail", "detail"]}
                            for name in benchmark.WEIGHTS}}
    result = benchmark.compare(before, after, corpus)
    assert result["weighted_workflow"]["before_tokens"] == 1350
    assert result["weighted_workflow"]["after_tokens"] == 1470
    assert result["weighted_workflow"]["reduction_percent"] < 0
    assert result["offline_target_met"] is False and result["production_target_validated"] is False
    assert result["three_tool_definition_delta_tokens"] == 10
