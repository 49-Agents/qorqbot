"""The shared Chat window cache: a receipt id repeats across agents and lineages, so the cache key must name the whole receipt.

Synthetic archives only: each workbench is a fresh temporary transcripts root with a generated provider source (see
``test_native_archive_support``). No database, registered agent, provider home, process or network is touched.
"""
from __future__ import annotations

import dataclasses

import pytest

from bazarlyk.native_archive import CaptureError
from bazarlyk.native_chat.reader import ConversationReader, Window

from test_native_archive_support import AGENT, CONVERSATION, Workbench

OTHER_AGENT = "9c41e7d2-6a0b-4f3e-8d15-2b7a4c9e0f61"


def bench(base, name: str, agent: str, *texts: str) -> tuple[Workbench, object]:
    """One agent's archive of ``CONVERSATION`` holding ``texts`` as user records, captured once (receipt sequence 1)."""
    folder = base / name
    folder.mkdir()
    workbench = Workbench(folder, agent=agent)
    workbench.write(b"".join(workbench.claude(text, uuid=f"m{index}") for index, text in enumerate(texts)))
    return workbench, workbench.archive.capture(workbench.binding())


def texts(window: Window) -> list[str]:
    return [entry.text for entry in window.entries]


def counted(reader: ConversationReader) -> list:
    """Record every archive pass the reader makes (a cache hit makes none)."""
    calls, read = [], reader._read

    def spy(archive, receipt, *rest):
        calls.append(receipt)
        return read(archive, receipt, *rest)

    reader._read = spy
    return calls


def test_two_agents_with_the_same_receipt_id_and_byte_lengths_each_read_their_own_text(tmp_path):
    first, first_receipt = bench(tmp_path, "a", AGENT, "first agent private words", "first again")
    second, second_receipt = bench(tmp_path, "b", OTHER_AGENT, "other agent private notes", "other again")
    # The collision the key must survive: one receipt id, one archive id, the same boundary and length, two agents.
    assert first_receipt.receipt_id == second_receipt.receipt_id
    assert first_receipt.archive_id == second_receipt.archive_id
    assert (first_receipt.record_boundary, first_receipt.length) == (second_receipt.record_boundary, second_receipt.length)
    assert first_receipt.agent_id != second_receipt.agent_id

    reader = ConversationReader()
    calls = counted(reader)
    assert texts(reader.window(first.archive, first_receipt, "claude", CONVERSATION)) == ["first agent private words", "first again"]
    assert texts(reader.window(second.archive, second_receipt, "claude", CONVERSATION)) == ["other agent private notes", "other again"]
    # And in the other order, through the same warm reader: each agent still gets its own page from the cache.
    assert texts(reader.window(second.archive, second_receipt, "claude", CONVERSATION)) == ["other agent private notes", "other again"]
    assert texts(reader.window(first.archive, first_receipt, "claude", CONVERSATION)) == ["first agent private words", "first again"]
    assert len(calls) == 2


def test_another_lineage_of_the_same_agent_with_other_content_does_not_reuse_the_cache(tmp_path):
    first, first_receipt = bench(tmp_path, "a", AGENT, "words of the first home")
    moved, moved_receipt = bench(tmp_path, "b", AGENT, "words of the other home")
    assert (first_receipt.agent_id, first_receipt.archive_id, first_receipt.receipt_id) == \
           (moved_receipt.agent_id, moved_receipt.archive_id, moved_receipt.receipt_id)
    assert first_receipt.record_sha256 != moved_receipt.record_sha256

    reader = ConversationReader()
    assert texts(reader.window(first.archive, first_receipt, "claude", CONVERSATION)) == ["words of the first home"]
    assert texts(reader.window(moved.archive, moved_receipt, "claude", CONVERSATION)) == ["words of the other home"]


def test_a_repeated_exact_request_is_a_cache_hit_with_no_growth(tmp_path):
    first, receipt = bench(tmp_path, "a", AGENT, "hello", "again")
    reader = ConversationReader()
    calls = counted(reader)
    page = reader.window(first.archive, receipt, "claude", CONVERSATION, limit=1)
    assert reader.window(first.archive, receipt, "claude", CONVERSATION, limit=1) is page
    assert reader.window(first.archive, first.archive.load_receipt(receipt.receipt_id), "claude", CONVERSATION, limit=1) is page
    assert len(calls) == 1 and len(reader._cache) == 1
    # Another window of the same receipt is another entry.
    assert texts(reader.window(first.archive, receipt, "claude", CONVERSATION, before=page.older, limit=1)) == ["hello"]
    assert len(calls) == 2 and len(reader._cache) == 2


@pytest.mark.parametrize("field, value", [("agent_id", OTHER_AGENT), ("archive_id", "claude:other"),
                                          ("receipt_id", "claude:other:1"), ("record_sha256", "0" * 64)])
def test_each_part_of_the_receipt_identity_and_content_separates_cache_entries(tmp_path, field, value):
    first, receipt = bench(tmp_path, "a", AGENT, "hello")
    reader = ConversationReader()
    pages = []
    reader._read = lambda archive, used, *rest: pages.append(used) or Window([], None, used.record_boundary)
    page = reader.window(first.archive, receipt, "claude", CONVERSATION)
    other = dataclasses.replace(receipt, **{field: value})
    assert reader.window(first.archive, other, "claude", CONVERSATION) is not page
    assert reader.window(first.archive, receipt, "claude", CONVERSATION) is page
    assert pages == [receipt, other]


def test_a_projection_that_is_not_the_receipts_own_is_refused_before_the_cache(tmp_path):
    first, receipt = bench(tmp_path, "a", AGENT, "hello")
    reader = ConversationReader()
    calls = counted(reader)
    reader.window(first.archive, receipt, "claude", CONVERSATION)
    for provider, conversation in [("codex", CONVERSATION), ("claude", "22222222-2222-4222-8222-222222222222"),
                                   ("codex", "22222222-2222-4222-8222-222222222222")]:
        with pytest.raises(CaptureError) as refused:
            reader.window(first.archive, receipt, provider, conversation)
        assert refused.value.code == "receipt_mismatch"
    assert len(calls) == 1 and len(reader._cache) == 1
