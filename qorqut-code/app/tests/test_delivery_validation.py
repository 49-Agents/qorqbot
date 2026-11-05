"""Focused guards for background validation and derived result reporting."""

from types import SimpleNamespace

import pytest

from bazarlyk.background.context import ContextPrograms
from bazarlyk.domains.learnings import analysis
from qorqut_operations.contracts import DomainError


def test_filing_normalizes_letter_first_topic_keys_and_references():
    answer = {"topics": [{"key": "example-ide", "title": "Example IDE", "slug": "example-ide"}],
              "notes": [{"title": "Launch", "topic_key": "example-ide"}]}
    normalized = ContextPrograms._normalize_topic_identifiers(answer)
    topic = normalized["topics"][0]
    assert topic["key"][0].isalpha() and topic["slug"][0].isalpha()
    assert normalized["notes"][0]["topic_key"] == topic["key"]
    ambiguous = {'topics': [answer['topics'][0], answer['topics'][0]]}
    with pytest.raises(DomainError, match='unique'):
        ContextPrograms._normalize_topic_identifiers(ambiguous)


def test_summary_rebases_to_current_source_revision():
    source = {"id": "note", "kind": "context_note", "revision": 4, "content_hash": "hash",
              "body": "Current fact.", "metadata": {}}
    snapshots = iter([DomainError("revision_conflict", "stale"),
                      {"source": source, "related": []}])
    class Model:
        def ask(self, *args, **kwargs):
            return "Current fact.", {}

    programs = ContextPrograms(Model(), None)
    def snapshot(control, kind, args):
        value = next(snapshots)
        if isinstance(value, Exception):
            raise value
        return value
    programs.snapshot = snapshot
    result = programs.summarize(object(), {"document_id": "note", "expected_revision": 3})
    assert result.data["rebased"] is True
    assert result.outputs[0]["options"]["expected_revision"] == 4


def test_extraction_diagnostics_keep_rejected_attribution_details():
    core = analysis.build_core({"source_id": "registered/session.jsonl", "content":
        '{"type":"user","timestamp":"2026-09-08T12:00:00Z","message":{"content":"Owner fact."}}'})
    biz = SimpleNamespace(id="business", header=lambda: {}, agents=lambda: [])
    diagnostics = []
    raw = {"bucket": "context", "tag": "fact", "scope": "biz", "scope_reason": "business fact",
           "text": "Invented", "said_by": "owner", "from_correction": False, "confidence": .9,
           "evidence": {"turns": ["T1"], "owner_quote": "Missing quote", "agent_quote": None}}
    items, _, errors = analysis.extract(core, biz, "run", passes=1,
        chat=lambda *args: ({"items": [raw]}, {}), diagnostics=diagnostics)
    assert not items and not errors
    assert diagnostics[0]["stage"] == "evidence"
    assert "owner_quote is not in an OWNER block" in diagnostics[0]["reason"]
