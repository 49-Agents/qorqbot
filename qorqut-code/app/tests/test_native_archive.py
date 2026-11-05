"""Focused synthetic checks of the lossless native archive library (no provider home, process or database)."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from bazarlyk.native_archive import (
    CaptureBinding, CaptureError, CaptureReceipt, FinalCaptureEvidence, TranscriptArchive,
)
from bazarlyk.native_archive import _files, _providers
from bazarlyk.native_archive import _format as fmt
from test_native_archive_support import (
    AGENT, CONVERSATION, EPOCH, Workbench, flip_byte, jsonl, temporaries,
)


@pytest.fixture
def bench(tmp_path):
    return Workbench(tmp_path)


def error_of(callable_, *args, **kwargs) -> CaptureError:
    with pytest.raises(CaptureError) as caught:
        callable_(*args, **kwargs)
    return caught.value


def raw_all(archive, receipt) -> bytes:
    out, offset = b"", 0
    while offset < receipt.length:
        part = archive.read_raw(receipt, offset=offset, limit=4096)
        out += part.data
        offset = part.next_offset if part.next_offset is not None else receipt.length
    return out


# ------------------------------------------------------------------ construction and layout

def test_construction_and_lookup_create_nothing(bench, tmp_path):
    root = tmp_path / "missing-parent" / "transcripts"
    archive = TranscriptArchive(root)
    assert not root.exists() and not root.parent.exists()
    assert archive.latest(bench.binding()) is None
    assert not root.parent.exists()
    with pytest.raises(CaptureError) as caught:
        archive.load_receipt(f"claude:{CONVERSATION}:1")
    assert caught.value.code == "not_found"
    for bad in ("relative/path", "/abs/../parent", "/abs//double", "/", ""):
        with pytest.raises(ValueError):
            TranscriptArchive(bad)


def test_capture_creates_only_the_root_leaf_so_a_mistaken_root_cannot_fabricate_a_tree(bench, tmp_path):
    bench.write(bench.claude())
    archive = TranscriptArchive(tmp_path / "no" / "such" / "transcripts")
    assert error_of(archive.capture, bench.binding()).code == "archive_root_missing"
    assert not (tmp_path / "no").exists()


def test_first_capture_stores_exact_bytes_one_payload_and_a_fixed_size_receipt(bench):
    raw = bench.claude("one", uuid="m1") + bench.claude("two", uuid="m2", role="assistant")
    bench.write(raw)
    receipt = bench.archive.capture(bench.binding())
    info = bench.source.stat()

    assert bench.payload() == raw
    assert receipt.receipt_id == f"claude:{CONVERSATION}:1" and receipt.sequence == 1
    assert receipt.archive_id == f"claude:{CONVERSATION}"
    assert (receipt.predecessor_id, receipt.predecessor_sha256) == (None, None)
    assert (receipt.agent_id, receipt.provider, receipt.conversation_id) == (AGENT, "claude", CONVERSATION)
    assert (receipt.generation, receipt.workspace, receipt.source_path) == (1, str(bench.workspace), str(bench.source))
    assert (receipt.source_device, receipt.source_inode) == (info.st_dev, info.st_ino)
    assert receipt.length == len(raw) == receipt.appended == receipt.source_size == receipt.source_size_end
    assert receipt.prefix_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.complete and receipt.incomplete_reasons == () and receipt.final is None
    assert (receipt.records, receipt.malformed, receipt.index_entries) == (2, 0, 0)
    assert (receipt.record_boundary, receipt.partial_tail_bytes) == (len(raw), 0)
    assert receipt.observed_at == "2026-10-11T12:00:00.000000Z"
    assert bench.archive.latest(bench.binding()) == receipt
    assert bench.archive.load_receipt(receipt.receipt_id) == receipt
    assert [path.stat().st_size for path in bench.receipt_files()] == [4096]
    assert temporaries(bench) == []


def test_files_and_directories_are_private(bench):
    bench.write(bench.claude())
    bench.archive.capture(bench.binding())
    for path in bench.root.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        assert mode & 0o077 == 0, (path, oct(mode))


def test_codex_capture_uses_session_metadata_and_record_order(tmp_path):
    bench = Workbench(tmp_path, provider="codex", conversation="22222222-2222-4222-8222-222222222222")
    bench.write(bench.codex_meta() + bench.codex_message("hi"))
    receipt = bench.archive.capture(bench.binding())
    assert receipt.provider == "codex" and receipt.records == 2 and receipt.complete
    assert bench.payload() == bench.source_bytes()
    assert (bench.root / "codex" / bench.conversation / "payload.raw").is_file()


def test_opaque_conversation_ids_are_encoded_in_names_and_kept_whole_in_receipts(tmp_path):
    odd = "a/b..c d%é"
    bench = Workbench(tmp_path, conversation=odd)
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    assert receipt.conversation_id == odd
    assert receipt.archive_id == "claude:a%2Fb%2E%2Ec%20d%25%C3%A9"
    assert {path.name for path in (bench.root / "claude").iterdir()} == {receipt.archive_id.split(":", 1)[1]}
    assert not (bench.root.parent / "b").exists()
    assert bench.archive.load_receipt(receipt.receipt_id) == receipt
    assert bench.archive.latest(bench.binding()) == receipt
    with pytest.raises(ValueError):
        bench.binding(conversation_id="x" * 129)
    with pytest.raises(ValueError):
        bench.binding(conversation_id="line\nbreak")
    with pytest.raises(ValueError):
        bench.binding(workspace="relative/path")
    with pytest.raises(ValueError):
        bench.binding(source_path=str(bench.source) + "/../x")
    with pytest.raises(ValueError):
        bench.binding(generation=0)


# ------------------------------------------------------------------ appends and bounded reads

def test_append_adds_only_the_new_suffix_and_old_prefix_reads_stay_bounded(bench):
    first = bench.claude("one", uuid="m1")
    second = bench.claude("two", uuid="m2")
    bench.write(first)
    r1 = bench.archive.capture(bench.binding())
    bench.append(second)
    r2 = bench.archive.capture(bench.binding())

    assert bench.payload() == first + second == bench.source_bytes()
    assert (r2.sequence, r2.predecessor_id, r2.predecessor_sha256) == (2, r1.receipt_id, r1.record_sha256)
    assert (r2.length, r2.appended) == (len(first) + len(second), len(second))
    assert r2.prefix_sha256 == hashlib.sha256(first + second).hexdigest()
    old = bench.archive.read_raw(r1, offset=0, limit=1 << 20)
    assert old.data == first and old.boundary == len(first) and old.next_offset is None
    assert bench.archive.read_raw(r1, offset=len(first), limit=10).data == b""
    assert raw_all(bench.archive, r2) == first + second
    page = bench.archive.read_raw(r2, offset=5, limit=7)
    assert (page.data, page.offset, page.next_offset, page.boundary) == ((first + second)[5:12], 5, 12, r2.length)
    for offset, limit in ((r1.length + 1, 1), (-1, 1), (0, 0), (0, 1 << 30)):
        with pytest.raises(ValueError):
            bench.archive.read_raw(r1, offset=offset, limit=limit)
    # An older receipt is still loadable and still names only its own prefix.
    assert bench.archive.load_receipt(r1.receipt_id) == r1
    assert len(bench.receipt_files()) == 2


def test_unchanged_capture_returns_the_same_receipt_and_changes_no_file(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    before = bench.snapshot()
    for _ in range(5):
        assert bench.archive.capture(bench.binding()) == receipt
    assert bench.snapshot() == before
    assert temporaries(bench) == []


def test_resume_with_a_new_generation_reuses_the_lineage_and_stores_metadata_only(bench):
    bench.write(bench.claude("one", uuid="m1"))
    r1 = bench.archive.capture(bench.binding(1))
    payload_before = (bench.lineage / "payload.raw").stat()
    r2 = bench.archive.capture(bench.binding(2))
    payload_after = (bench.lineage / "payload.raw").stat()

    assert (r2.sequence, r2.generation, r2.appended, r2.length) == (2, 2, 0, r1.length)
    assert r2.prefix_sha256 == r1.prefix_sha256
    assert (payload_after.st_size, payload_after.st_mtime_ns) == (payload_before.st_size, payload_before.st_mtime_ns)
    assert bench.archive.capture(bench.binding(2)) == r2
    assert len(bench.receipt_files()) == 2

    bench.append(bench.claude("two", uuid="m2"))
    r3 = bench.archive.capture(bench.binding(3))
    assert (r3.generation, r3.appended) == (3, len(bench.claude("two", uuid="m2")))
    assert bench.payload() == bench.source_bytes()
    assert {path.name for path in bench.root.rglob("payload.raw")} == {"payload.raw"}
    assert len(list(bench.root.rglob("payload.raw"))) == 1


# ------------------------------------------------------------------ binding evidence

def test_wrong_agent_conversation_workspace_and_source_are_rejected_without_changing_storage(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    before = bench.snapshot()

    assert error_of(bench.archive.capture, bench.binding(agent_id="other-agent")).code == "agent_mismatch"
    assert error_of(bench.archive.capture, bench.binding(workspace=str(bench.base / "elsewhere"))).code == "workspace_mismatch"
    other_source = bench.sources / "other.jsonl"
    other_source.write_bytes(bench.source_bytes())
    assert error_of(bench.archive.capture, bench.binding(source_path=str(other_source))).code == "source_path_changed"
    assert bench.snapshot() == before
    assert error_of(bench.archive.capture, bench.binding(agent_id="other-agent")).latest is None   # nothing of another agent's
    assert bench.archive.latest(bench.binding()) == receipt
    assert error_of(bench.archive.latest, bench.binding(agent_id="other-agent")).code == "agent_mismatch"


def test_a_source_recording_another_session_or_workspace_is_never_archived(bench):
    other = jsonl({"type": "user", "sessionId": "99999999-9999-4999-8999-999999999999", "cwd": str(bench.workspace),
                   "message": {"role": "user", "content": "x"}})
    bench.write(bench.claude() + other)
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "session_mismatch" and not failure.retryable and failure.latest is None

    bench.write(jsonl({"type": "user", "uuid": "m", "message": {"role": "user", "content": "x"}}))
    assert error_of(bench.archive.capture, bench.binding()).code == "session_mismatch"

    # Right session, wrong workspace: no complete matching evidence (the source may still gain some).
    elsewhere = jsonl({"type": "user", "sessionId": CONVERSATION, "cwd": str(bench.base / "elsewhere"),
                       "message": {"role": "user", "content": "x"}})
    bench.write(elsewhere)
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "no_identity_evidence" and failure.retryable

    # A sidechain record never establishes the binding.
    bench.write(bench.claude(isSidechain=True))
    assert error_of(bench.archive.capture, bench.binding()).code == "no_identity_evidence"
    bench.write(b"")
    assert error_of(bench.archive.capture, bench.binding()).code == "no_identity_evidence"

    # Nothing was archived for any rejected binding: no payload, no receipt, no header.
    assert bench.archive.latest(bench.binding()) is None
    assert not (bench.lineage / "payload.raw").exists() and not (bench.lineage / "receipts").exists()
    assert not (bench.lineage / "lineage.json").exists()
    assert temporaries(bench) == []
    bench.write(bench.claude())
    assert bench.archive.capture(bench.binding()).sequence == 1


def test_codex_records_before_matching_metadata_are_rejected(tmp_path):
    bench = Workbench(tmp_path, provider="codex", conversation="22222222-2222-4222-8222-222222222222")
    bench.write(bench.codex_message("early") + bench.codex_meta())
    assert error_of(bench.archive.capture, bench.binding()).code == "session_mismatch"
    bench.write(bench.codex_meta(cwd=str(bench.base / "elsewhere")))
    assert error_of(bench.archive.capture, bench.binding()).code == "session_mismatch"
    bench.write(bench.codex_meta(id="99999999-9999-4999-8999-999999999999"))
    assert error_of(bench.archive.capture, bench.binding()).code == "session_mismatch"
    bench.write(bench.codex_meta())
    assert bench.archive.capture(bench.binding()).records == 1


def test_an_incremental_check_rejects_a_later_wrong_session_record_and_keeps_the_trusted_prefix(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    bench.append(jsonl({"type": "assistant", "sessionId": "99999999-9999-4999-8999-999999999999"}))
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "session_mismatch" and failure.latest == receipt
    assert bench.payload() == bench.claude() and temporaries(bench) == []


# ------------------------------------------------------------------ symlinks and special files

def test_symlink_special_and_missing_sources_are_refused(bench, tmp_path):
    target = bench.sources / "real.jsonl"
    target.write_bytes(bench.claude())
    bench.source.symlink_to(target)
    assert error_of(bench.archive.capture, bench.binding()).code == "unsafe_path"
    bench.source.unlink()

    os.mkfifo(bench.source)
    assert error_of(bench.archive.capture, bench.binding()).code == "unsafe_path"
    bench.source.unlink()

    linked = tmp_path / "linked-home"
    linked.symlink_to(bench.sources, target_is_directory=True)
    assert error_of(bench.archive.capture, bench.binding(source_path=str(linked / "real.jsonl"))).code == "unsafe_path"

    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "source_unavailable" and failure.retryable
    bench.source.mkdir()
    assert error_of(bench.archive.capture, bench.binding()).code == "unsafe_path"
    assert bench.archive.latest(bench.binding()) is None


def test_symlinked_roots_and_archive_members_are_refused(bench, tmp_path):
    bench.write(bench.claude())
    real = tmp_path / "real-transcripts"
    real.mkdir()
    bench.root.symlink_to(real, target_is_directory=True)
    assert error_of(bench.archive.capture, bench.binding()).code == "unsafe_path"
    assert error_of(bench.archive.latest, bench.binding()).code == "unsafe_path"
    assert list(real.iterdir()) == []
    bench.root.unlink()

    ancestor = tmp_path / "linked-agent"
    ancestor.symlink_to(bench.parent, target_is_directory=True)
    through = TranscriptArchive(ancestor / "transcripts")
    assert error_of(through.capture, bench.binding()).code == "unsafe_path"
    assert not bench.root.exists()

    receipt = bench.archive.capture(bench.binding())
    payload = bench.lineage / "payload.raw"
    stolen = tmp_path / "stolen"
    payload.rename(stolen)
    payload.symlink_to(stolen)
    assert error_of(bench.archive.capture, bench.binding()).code == "unsafe_path"
    assert error_of(bench.archive.read_raw, receipt, offset=0, limit=10).code == "unsafe_path"
    assert stolen.read_bytes() == bench.claude()


# ------------------------------------------------------------------ malformed and partial records

def test_malformed_blank_and_partial_records_are_retained_and_indexed_incrementally(bench):
    good = bench.claude("one", uuid="m1")
    bad = b"{not json}\n"
    blank = b"   \n"
    scalar = b"[1, 2]\n"
    untyped = b'{"no": "type"}\n'
    next_line = bench.claude("two", uuid="m2")
    raw = good + bad + blank + scalar + untyped + next_line[:30]
    bench.write(raw)
    receipt = bench.archive.capture(bench.binding())

    assert bench.payload() == raw
    assert (receipt.records, receipt.malformed, receipt.index_entries) == (1, 3, 3)
    assert receipt.record_boundary == len(raw) - 30 and receipt.partial_tail_bytes == 30
    assert not receipt.complete and receipt.incomplete_reasons == ("partial_tail",)
    entries = bench.archive.index_entries(receipt)
    assert [(entry.offset, entry.length, entry.kind) for entry in entries] == [
        (len(good), len(bad), "malformed"), (len(good + bad + blank), len(scalar), "malformed"),
        (len(good + bad + blank + scalar), len(untyped), "malformed")]
    records = list(bench.archive.iter_records(receipt))
    assert [record.kind for record in records] == ["json", "malformed", "blank", "malformed", "malformed", "partial"]
    assert records[0].value["uuid"] == "m1" and records[1].value is None
    cursor = 0
    for record in records:
        assert record.offset == cursor
        cursor = record.end
    assert cursor == len(raw)
    assert (bench.lineage / "index.bin").stat().st_size == 3 * 32

    bench.append(next_line[30:])
    completed = bench.archive.capture(bench.binding())
    assert completed.complete and (completed.records, completed.malformed) == (2, 3)
    assert completed.record_boundary == completed.length and completed.partial_tail_bytes == 0
    assert bench.payload() == raw + next_line[30:]
    assert (bench.lineage / "index.bin").stat().st_size == 3 * 32      # the tail is not re-indexed
    assert bench.archive.index_entries(completed) == entries
    assert bench.archive.index_entries(receipt, start=1, limit=1) == entries[1:2]
    assert bench.archive.index_entries(receipt, start=3) == ()
    assert [r.kind for r in bench.archive.iter_records(completed)] == ["json", "malformed", "blank", "malformed", "malformed", "json"]
    # The earlier receipt still names its old prefix, partial line included.
    assert list(bench.archive.iter_records(receipt))[-1].kind == "partial"


def test_oversized_records_are_framed_without_parsing_and_indexed(bench, monkeypatch):
    monkeypatch.setattr(_providers, "MAX_RECORD_BYTES", 300)
    big = bench.claude("y" * 1000, uuid="big")
    raw = bench.claude("a", uuid="m1") + big + bench.claude("b", uuid="m2")
    bench.write(raw)
    receipt = bench.archive.capture(bench.binding())
    assert (receipt.records, receipt.malformed) == (2, 1)
    assert [record.kind for record in bench.archive.iter_records(receipt)] == ["json", "oversized", "json"]
    entry = bench.archive.index_entries(receipt)[0]
    assert (entry.kind, entry.length) == ("oversized", len(big)) and raw[entry.offset:entry.offset + entry.length] == big
    assert bench.payload() == raw


def test_records_can_be_iterated_from_a_boundary_and_closed_early(bench):
    lines = [bench.claude(str(i), uuid=f"m{i}") for i in range(4)]
    bench.write(b"".join(lines))
    receipt = bench.archive.capture(bench.binding())
    start = len(lines[0]) + len(lines[1])
    assert [r.value["uuid"] for r in bench.archive.iter_records(receipt, offset=start)] == ["m2", "m3"]
    assert list(bench.archive.iter_records(receipt, offset=receipt.length)) == []
    with pytest.raises(ValueError):
        bench.archive.iter_records(receipt, offset=start + 3)
    with pytest.raises(ValueError):
        bench.archive.iter_records(receipt, offset=receipt.length + 1)
    with bench.archive.iter_records(receipt) as records:
        assert next(records).value["uuid"] == "m0"
    with pytest.raises(StopIteration):
        next(records)


# ------------------------------------------------------------------ active versus final evidence

def test_active_and_final_tail_evidence_are_separate_claims(bench):
    head = bench.claude("one", uuid="m1")
    tail = bench.claude("two", uuid="m2")[:25]
    bench.write(head + tail)
    active = bench.archive.capture(bench.binding())
    assert not active.complete and active.final is None and active.partial_tail_bytes == 25

    evidence = bench.final()
    final = bench.archive.capture(bench.binding(), final_evidence=evidence)
    assert final.sequence == 2 and final.appended == 0 and final.length == active.length
    assert final.final == evidence and final.complete and final.incomplete_reasons == ()
    assert final.partial_tail_bytes == 25                # the genuine truncated last line stays explicit
    assert bench.payload() == head + tail
    assert bench.archive.capture(bench.binding(), final_evidence=evidence) == final
    assert bench.archive.capture(bench.binding()) == final      # a plain poll never downgrades a final tail
    assert len(bench.receipt_files()) == 2
    assert bench.archive.latest(bench.binding()).final == evidence

    # A different exit token for the same generation is a conflict, not a replacement.
    assert error_of(bench.archive.capture, bench.binding(),
                    final_evidence=bench.final(token="another-process")).code == "final_evidence_conflict"
    # Evidence for another generation, agent or conversation is not accepted for this binding.
    for wrong in (bench.final(generation=2), bench.final(agent_id="other-agent")):
        assert error_of(bench.archive.capture, bench.binding(), final_evidence=wrong).code == "final_evidence_mismatch"
    # A resumed generation that appends bytes starts without any final claim.
    bench.append(bench.claude("two", uuid="m2")[25:])
    resumed = bench.archive.capture(bench.binding(2))
    assert resumed.final is None and resumed.complete and resumed.generation == 2 and resumed.partial_tail_bytes == 0


def test_a_final_claim_needs_a_stable_observation(bench):
    bench.write(bench.claude("one", uuid="m1"))
    grown = bench.claude("two", uuid="m2")

    def append_during_read(stage):
        if stage == "suffix_read":
            bench.append(grown)

    unstable = bench.new_archive(append_during_read).capture(bench.binding(), final_evidence=bench.final())
    assert unstable.final is None and not unstable.complete
    assert unstable.incomplete_reasons == ("source_changed_during_observation",)
    assert unstable.length == len(bench.claude("one", uuid="m1")) and unstable.source_size_end == unstable.length + len(grown)
    assert bench.payload() == bench.claude("one", uuid="m1")

    stable = bench.archive.capture(bench.binding(), final_evidence=bench.final())
    assert stable.final == bench.final() and stable.complete and stable.length == len(bench.source_bytes())
    assert bench.payload() == bench.source_bytes()
    assert stable.appended == len(grown)


def test_an_active_append_during_observation_is_an_explicit_incomplete_receipt(bench):
    bench.write(bench.claude("one", uuid="m1"))
    more = bench.claude("two", uuid="m2")
    state = {"done": False}

    def append_once(stage):
        if stage == "suffix_read" and not state["done"]:
            state["done"] = True
            bench.append(more)

    receipt = bench.new_archive(append_once).capture(bench.binding())
    assert not receipt.complete and receipt.incomplete_reasons == ("source_changed_during_observation",)
    assert receipt.length == len(bench.claude("one", uuid="m1"))
    follow = bench.archive.capture(bench.binding())
    assert follow.complete and follow.length == receipt.length + len(more) and follow.appended == len(more)


def test_a_stable_unchanged_source_upgrades_an_incomplete_receipt_without_new_payload(bench):
    bench.write(bench.claude("one", uuid="m1"))

    def touch_during_read(stage):
        if stage == "suffix_read":
            os.utime(bench.source, ns=(1, 12345))

    unstable = bench.new_archive(touch_during_read).capture(bench.binding())
    assert not unstable.complete
    upgraded = bench.archive.capture(bench.binding())
    assert upgraded.complete and upgraded.sequence == 2 and upgraded.appended == 0
    assert bench.archive.capture(bench.binding()) == upgraded


# ------------------------------------------------------------------ corruption and replacement

def test_a_corrupt_archive_payload_is_detected_by_capture_and_reads(bench):
    bench.write(bench.claude("one", uuid="m1"))
    receipt = bench.archive.capture(bench.binding())
    flip_byte(bench.lineage / "payload.raw", 10)
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "archive_integrity" and failure.latest == receipt
    assert error_of(bench.archive.read_raw, receipt, offset=0, limit=10).code == "archive_integrity"
    assert error_of(lambda: list(bench.archive.iter_records(receipt))).code == "archive_integrity"
    assert bench.archive.latest(bench.binding()) == receipt          # the receipt itself is still trusted metadata


def test_source_prefix_mutation_shrink_replacement_and_removal_preserve_the_trusted_receipt(bench):
    raw = bench.claude("one", uuid="m1") + bench.claude("two", uuid="m2")
    bench.write(raw)
    receipt = bench.archive.capture(bench.binding())
    before = bench.snapshot()

    mutated = bytearray(raw)
    mutated[20] ^= 0x01
    bench.write(bytes(mutated) + b"more\n")
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "source_prefix_changed" and not failure.retryable and failure.latest == receipt

    bench.write(raw[:40])
    assert error_of(bench.archive.capture, bench.binding()).code == "source_shrunk"

    bench.write(raw)
    replacement = bench.sources / "replacement"
    replacement.write_bytes(raw)
    os.replace(replacement, bench.source)             # identical bytes, different inode
    assert error_of(bench.archive.capture, bench.binding()).code == "source_replaced"

    bench.source.unlink()
    assert error_of(bench.archive.capture, bench.binding()).code == "source_unavailable"

    assert bench.snapshot() == before
    assert bench.archive.latest(bench.binding()) == receipt
    assert raw_all(bench.archive, receipt) == raw
    assert temporaries(bench) == []


def test_receipt_and_pointer_tampering_is_detected(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    bench.append(bench.claude("two", uuid="m2"))
    bench.archive.capture(bench.binding())
    first, second = bench.receipt_files()

    flip_byte(first, 100)
    assert error_of(bench.archive.load_receipt, receipt.receipt_id).code == "archive_integrity"
    flip_byte(first, 100)
    assert bench.archive.load_receipt(receipt.receipt_id) == receipt

    pointer = bench.lineage / "latest.json"
    good = pointer.read_bytes()
    pointer.write_bytes(b"not json")
    assert error_of(bench.archive.latest, bench.binding()).code == "archive_integrity"
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    pointer.write_bytes(good)
    flip_byte(second, 200)
    assert error_of(bench.archive.latest, bench.binding()).code == "archive_integrity"


def forge_newest(bench, **changes) -> None:
    """Replace the newest receipt with a checksum-valid record carrying ``changes`` and re-point to it."""
    newest = bench.receipt_files()[-1]
    fields = fmt._open_record(newest.read_bytes())
    fields.update(changes)
    record = fmt.encode_receipt(fields)
    newest.write_bytes(record)
    (bench.lineage / "latest.json").write_bytes(
        fmt.encode_pointer(fields["archive_id"], fields["sequence"], fmt.record_digest(record)))


def test_a_newest_receipt_whose_tail_cursor_disagrees_with_the_stored_bytes_is_not_trusted(bench):
    whole, tail = bench.claude("one"), b'{"type": "user", "sessionId": "' + CONVERSATION.encode() + b'", "mess'
    bench.write(whole + tail)
    receipt = bench.archive.capture(bench.binding())
    assert (receipt.record_boundary, receipt.partial_tail_bytes) == (len(whole), len(tail))

    forge_newest(bench, record_boundary=len(whole) + 5, partial_tail_bytes=len(tail) - 5)      # not after a newline
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    forge_newest(bench, record_boundary=0, partial_tail_bytes=len(whole) + len(tail))          # a complete line inside the tail
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    assert bench.payload() == whole + tail                                                       # nothing was appended or repaired


def test_a_missing_lineage_header_is_corruption_not_an_empty_archive(bench):
    bench.write(bench.claude())
    bench.archive.capture(bench.binding())
    (bench.lineage / "lineage.json").unlink()
    assert error_of(bench.archive.latest, bench.binding()).code == "archive_integrity"
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    assert not (bench.lineage / "lineage.json").exists()


def test_forged_unknown_and_malformed_receipt_values_are_refused(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    assert error_of(bench.archive.read_raw, replace(receipt, length=receipt.length - 5), offset=0, limit=5).code == "receipt_mismatch"
    assert error_of(bench.archive.read_raw, replace(receipt, prefix_sha256="0" * 64), offset=0, limit=5).code == "receipt_mismatch"
    assert error_of(bench.archive.load_receipt, f"claude:{CONVERSATION}:2").code == "not_found"
    assert error_of(bench.archive.load_receipt, f"codex:{CONVERSATION}:1").code == "not_found"
    for bad in ("", "nonsense", f"claude:{CONVERSATION}:0", f"claude:{CONVERSATION}:01", "claude:../x:1", "claude:a%zz:1", 7):
        with pytest.raises(ValueError):
            bench.archive.load_receipt(bad)
    assert bench.archive.read_raw(receipt.receipt_id, offset=0, limit=3).data == bench.claude()[:3]


def test_the_largest_accepted_identifiers_fit_one_receipt_and_escape_heavy_ones_fail_before_any_write(bench):
    long_path = "/" + "/".join(["d" * 255] * 4)                  # 1021 bytes
    maximal = CaptureBinding("a" * 128, "claude", "c" * 128, 1, long_path, "/" + "/".join(["s" * 255] * 4))
    evidence = bench.final(process_token="t" * 128, agent_id="a" * 128, conversation_id="c" * 128)
    TranscriptArchive(bench.root)._check_fits(maximal, evidence)       # does not raise

    escaped = CaptureBinding(AGENT, "claude", CONVERSATION, 1, "/" + '"' * 1023, "/" + '"' * 1023)
    failure = error_of(TranscriptArchive(bench.root).capture, escaped)
    assert failure.code == "receipt_too_large"
    assert not bench.root.exists()


# ------------------------------------------------------------------ serialization

def test_a_held_archive_lock_makes_capture_busy_and_never_corrupts(bench):
    bench.write(bench.claude())
    receipt = bench.archive.capture(bench.binding())
    bench.append(bench.claude("two", uuid="m2"))
    script = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(float(sys.argv[2]))\n")
    holder = subprocess.Popen([sys.executable, "-c", script, str(bench.lineage / ".lock"), "1.5"],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        busy = error_of(bench.new_archive(lock_timeout=0.1).capture, bench.binding())
        assert busy.code == "archive_busy" and busy.retryable and busy.latest is None
        assert bench.archive.latest(bench.binding()) == receipt         # readers never take the lock
        waited = bench.new_archive(lock_timeout=10).capture(bench.binding())   # waits out the other process
    finally:
        holder.wait(timeout=10)
    assert waited.sequence == 2 and bench.payload() == bench.source_bytes()


def test_concurrent_captures_publish_one_receipt_and_never_duplicate_bytes(bench):
    bench.write(bench.claude("one", uuid="m1") + bench.claude("two", uuid="m2"))
    results, errors = [], []

    def run():
        try:
            results.append(bench.new_archive().capture(bench.binding()))
        except BaseException as error:        # pragma: no cover - reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(set(results)) == 1
    assert bench.payload() == bench.source_bytes() and len(bench.receipt_files()) == 1
    assert temporaries(bench) == []
