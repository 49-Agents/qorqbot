"""Storage-growth regression for the native archive: payload grows once, metadata by a fixed allowance.

The required fixture appends 64 KiB to a synthetic Claude transcript 256 times and captures after each
append. A whole-prefix snapshot design would store ~2 GiB (the sum of every prefix); this archive must
retain exactly 16 MiB of raw payload plus metadata within a declared per-capture / per-entry budget.
Every byte written under the archive root is counted: payloads, receipts, index, pointer, header, lock
and any staging or temporary leftover.
"""
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from test_native_archive_support import Workbench, sha256_file, tree_snapshot, tree_storage

KIB, MIB = 1024, 1024 * 1024
CAPTURES = 256
APPEND = 64 * KIB
LINE = 1024                       # every synthetic record is exactly one KiB, so a 64 KiB append is 64 records

# Declared metadata allowance (apparent bytes). One immutable fixed-size receipt per capture, one 32-byte
# index entry per complete non-object record, and a fixed lineage allowance for the header, the latest
# pointer, the lock and rounding slack. Nothing here depends on how large the transcript is.
RECEIPT_BYTES = 4096
INDEX_ENTRY_BYTES = 32
LINEAGE_ALLOWANCE = 8 * KIB


def metadata_allowance(captures: int, entries: int = 0) -> int:
    return LINEAGE_ALLOWANCE + captures * RECEIPT_BYTES + entries * INDEX_ENTRY_BYTES


def one_kib_record(bench: Workbench, number: int) -> bytes:
    empty = bench.claude("", uuid=f"record-{number:09d}")
    record = bench.claude("x" * (LINE - len(empty)), uuid=f"record-{number:09d}")
    assert len(record) == LINE
    return record


def append_block(bench: Workbench, index: int, size: int) -> int:
    records = size // LINE
    bench.append(b"".join(one_kib_record(bench, index * records + n) for n in range(records)))
    return records


def grow(base: Path, captures: int, size: int, *, durable: bool = True):
    """Append ``size`` bytes and capture, ``captures`` times. The supplementary linearity runs skip the real
    fsync calls (they measure bytes stored, not durability, which the recovery tests cover); the required
    256-capture fixture keeps them."""
    bench = Workbench(base)
    bench.write(b"")
    records, receipt = 0, None
    with nullcontext() if durable else mock.patch("os.fsync", lambda fd: None):
        for index in range(captures):
            records += append_block(bench, index, size)
            receipt = bench.archive.capture(bench.binding())
            assert receipt.sequence == index + 1 and receipt.appended == size and receipt.complete
    return bench, receipt, records


def payload_and_metadata(bench: Workbench):
    usage = tree_storage(bench.root)
    payload = usage["files"][str(Path(bench.provider) / bench.conversation / "payload.raw")]
    return usage, payload, usage["apparent"] - payload


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    return grow(tmp_path_factory.mktemp("base"), CAPTURES, APPEND)


def test_256_captures_of_64_kib_retain_exactly_16_mib_of_payload_and_fixed_metadata(base_run):
    bench, last, records = base_run
    usage, payload, metadata = payload_and_metadata(bench)

    assert payload == CAPTURES * APPEND == 16 * MIB == last.length                 # exactly the raw bytes, once
    assert sha256_file(bench.lineage / "payload.raw") == sha256_file(bench.source) == last.prefix_sha256
    assert len(bench.receipt_files()) == CAPTURES
    receipts = {name for name in usage["files"] if "/receipts/" in name}
    assert len(receipts) == CAPTURES and {usage["files"][name] for name in receipts} == {RECEIPT_BYTES}
    assert set(usage["files"]) - receipts == {
        f"claude/{bench.conversation}/{name}" for name in ("payload.raw", "lineage.json", "latest.json", ".lock")}
    assert (last.records, last.malformed, last.index_entries) == (records, 0, 0)        # valid records need no index

    assert metadata <= metadata_allowance(CAPTURES)                                  # the declared budget
    assert metadata == CAPTURES * RECEIPT_BYTES + sum(
        size for name, size in usage["files"].items() if not name.endswith(".rcpt") and not name.endswith("payload.raw"))
    assert usage["apparent"] <= 16 * MIB + metadata_allowance(CAPTURES)              # not the ~2 GiB of stored prefixes
    sum_of_prefixes = sum(APPEND * k for k in range(1, CAPTURES + 1))
    assert sum_of_prefixes > 2000 * MIB and usage["apparent"] * 100 < sum_of_prefixes
    assert usage["allocated"] <= 16 * MIB + CAPTURES * 2 * RECEIPT_BYTES + 64 * KIB       # on-disk blocks too
    assert not list((bench.lineage / "staging").iterdir()) and not list(bench.lineage.glob(".tmp-*"))


def test_the_chain_records_every_capture_and_the_full_payload_reads_back_record_by_record(base_run):
    bench, last, records = base_run
    assert last.sequence == CAPTURES
    for number in (1, 2, 100, CAPTURES - 1, CAPTURES):
        receipt = bench.archive.load_receipt(f"claude:{bench.conversation}:{number}")
        assert (receipt.length, receipt.appended) == (number * APPEND, APPEND)
        assert receipt.predecessor_id == (f"claude:{bench.conversation}:{number - 1}" if number > 1 else None)
    first = bench.archive.load_receipt(f"claude:{bench.conversation}:1")
    assert bench.archive.read_raw(last, offset=0, limit=2 * LINE).data == bench.source_bytes()[:2 * LINE]
    assert bench.archive.read_raw(first, offset=APPEND - LINE, limit=4 * LINE).data == bench.source_bytes()[APPEND - LINE:APPEND]
    kinds = [record.kind for record in bench.archive.iter_records(last)]
    assert kinds == ["json"] * records and len(kinds) == 16 * KIB


def test_unchanged_polls_add_no_storage(base_run):
    bench, last, _ = base_run
    before = tree_snapshot(bench.root)
    for _ in range(25):
        assert bench.archive.capture(bench.binding()) == last
    assert tree_snapshot(bench.root) == before


def test_doubling_the_captures_or_the_bytes_grows_storage_linearly(base_run, tmp_path_factory):
    bench256, _, _ = base_run
    half, _, _ = grow(tmp_path_factory.mktemp("half"), CAPTURES // 2, APPEND, durable=False)       # half the captures
    wide, _, _ = grow(tmp_path_factory.mktemp("wide"), CAPTURES // 2, 2 * APPEND, durable=False)   # same captures, double the bytes
    _, payload_256, metadata_256 = payload_and_metadata(bench256)
    _, payload_128, metadata_128 = payload_and_metadata(half)
    _, payload_wide, metadata_wide = payload_and_metadata(wide)

    # Doubling the captures doubles payload and receipt storage; the fixed lineage allowance does not scale.
    assert payload_256 == 2 * payload_128
    assert abs(metadata_256 - 2 * metadata_128) <= LINEAGE_ALLOWANCE
    assert metadata_128 <= metadata_allowance(CAPTURES // 2) and metadata_256 <= metadata_allowance(CAPTURES)
    # Doubling the bytes per capture doubles the payload and leaves the metadata untouched.
    assert payload_wide == 2 * payload_128 == 16 * MIB
    assert metadata_wide == metadata_128


def test_index_evidence_grows_by_a_fixed_entry_while_receipts_stay_fixed(tmp_path):
    bench = Workbench(tmp_path)
    bench.write(b"")
    captures = 120
    with mock.patch("os.fsync", lambda fd: None):          # bytes stored are measured here, not durability
        for index in range(captures):
            bench.append(one_kib_record(bench, index) + b"{broken " + str(index).encode() + b"}\n")
            receipt = bench.archive.capture(bench.binding())
            assert (receipt.records, receipt.malformed, receipt.index_entries) == (index + 1, index + 1, index + 1)
    usage = tree_storage(bench.root)
    assert (bench.lineage / "index.bin").stat().st_size == captures * INDEX_ENTRY_BYTES
    assert {size for name, size in usage["files"].items() if name.endswith(".rcpt")} == {RECEIPT_BYTES}
    payload = usage["files"][f"claude/{bench.conversation}/payload.raw"]
    assert payload == len(bench.source_bytes())
    assert usage["apparent"] - payload <= metadata_allowance(captures, entries=captures)
    assert len(bench.archive.index_entries(receipt, limit=500)) == captures
