"""Crash-gap and recovery checks for the native archive: truthful adoption or retained uncertainty, never duplicates."""
from contextlib import contextmanager
import errno
import hashlib
import os
import shutil
from unittest import mock

import pytest

from bazarlyk.native_archive import CaptureError
from bazarlyk.native_archive import _format as fmt
from test_native_archive_support import (
    SimulatedCrash, Workbench, crash_at, flip_byte, temporaries,
)

STAGES = ["staged", "payload_chunk", "payload_appended", "index_appended", "receipt_temp", "receipt_published",
          "pointer_temp", "pointer_published"]


@pytest.fixture
def bench(tmp_path):
    return Workbench(tmp_path)


def error_of(callable_, *args, **kwargs) -> CaptureError:
    with pytest.raises(CaptureError) as caught:
        callable_(*args, **kwargs)
    return caught.value


def first_batch(bench) -> bytes:
    return (bench.claude("one", uuid="m1") + b"{broken one\n" + bench.claude("two", uuid="m2")
            + b"second broken\n")


def second_batch(bench) -> bytes:
    return (bench.claude("three", uuid="m3", role="assistant") + b"{broken three\n"
            + bench.claude("four", uuid="m4") + b"x" * 40)       # ends in an unterminated final line


def crash_during(bench, stage, *, occurrence=1, chunk=100):
    archive = bench.new_archive(crash_at(stage, occurrence=occurrence))
    archive._COPY_CHUNK = chunk
    with pytest.raises(SimulatedCrash):
        archive.capture(bench.binding())


def read_all(bench, receipt) -> bytes:
    data = bench.archive.read_raw(receipt, offset=0, limit=1 << 20)
    assert data.next_offset is None and data.boundary == receipt.length
    return data.data


def assert_healthy(bench, raw, receipts):
    """The lineage is internally consistent, holds each source byte once and has nothing left over."""
    assert bench.payload() == raw
    files = bench.receipt_files()
    assert [path.name for path in files] == [f"{n:012d}.rcpt" for n in range(1, receipts + 1)]
    previous_digest, appended = None, 0
    for number, path in enumerate(files, 1):
        data = path.read_bytes()
        assert len(data) == 4096
        loaded = bench.archive.load_receipt(f"claude:{bench.conversation}:{number}")
        assert loaded.predecessor_sha256 == previous_digest
        previous_digest = hashlib.sha256(data).hexdigest()
        appended += loaded.appended
    latest = bench.archive.latest(bench.binding())
    assert latest.sequence == receipts and latest.length == len(raw) == appended
    assert latest.prefix_sha256 == hashlib.sha256(raw).hexdigest()
    assert read_all(bench, latest) == raw
    assert (bench.lineage / "index.bin").stat().st_size == 32 * latest.malformed
    assert [e.kind for e in bench.archive.index_entries(latest)] == ["malformed"] * latest.malformed
    assert temporaries(bench) == []
    before = bench.snapshot()
    assert bench.archive.capture(bench.binding()) == latest        # an unchanged retry stores nothing
    assert bench.snapshot() == before


# ------------------------------------------------------------------ every crash gap, fresh lineage

@pytest.mark.parametrize("stage", STAGES)
def test_a_crash_in_the_first_capture_is_recovered_without_duplicate_or_lost_bytes(bench, stage):
    raw = first_batch(bench)
    bench.write(raw)
    crash_during(bench, stage, occurrence=2 if stage == "payload_chunk" else 1)

    latest = bench.archive.latest(bench.binding())
    if stage == "pointer_published":
        assert latest is not None and latest.length == len(raw)         # fully committed before the crash
    else:
        assert latest is None                                          # nothing is committed or readable yet
    stored = len(bench.payload()) if (bench.lineage / "payload.raw").exists() else 0
    assert stored == {"staged": 0, "payload_chunk": 200}.get(stage, len(raw))     # the crash left exactly this tail
    if stage in {"receipt_published", "pointer_temp"}:
        assert error_of(bench.archive.load_receipt, f"claude:{bench.conversation}:1").code == "receipt_uncommitted"
        published = (bench.lineage / "receipts" / "000000000001.rcpt").read_bytes()

    receipt = bench.archive.capture(bench.binding())
    assert receipt.sequence == 1 and receipt.length == len(raw) and receipt.complete
    if stage in {"receipt_published", "pointer_temp"}:
        # The receipt the interrupted writer published was adopted, not replaced by a new one.
        assert (bench.lineage / "receipts" / "000000000001.rcpt").read_bytes() == published
    assert_healthy(bench, raw, 1)


@pytest.mark.parametrize("stage", STAGES)
def test_a_crash_in_a_later_capture_keeps_the_prior_boundary_and_is_recovered(bench, stage):
    first, second = first_batch(bench), second_batch(bench)
    bench.write(first)
    r1 = bench.archive.capture(bench.binding())
    bench.append(second)
    crash_during(bench, stage, occurrence=2 if stage == "payload_chunk" else 1)

    latest = bench.archive.latest(bench.binding())
    if stage == "pointer_published":
        assert latest.sequence == 2
    else:
        assert latest == r1                                             # the read boundary did not advance
        assert read_all(bench, latest) == first                         # ... even when the payload file is longer
    if stage in {"payload_chunk", "payload_appended", "index_appended", "receipt_temp", "receipt_published",
                 "pointer_temp"}:
        assert len(bench.payload()) > len(first)                        # uncommitted bytes really are on disk

    receipt = bench.archive.capture(bench.binding())
    assert receipt.sequence == 2 and receipt.appended == len(second) and receipt.predecessor_id == r1.receipt_id
    assert not receipt.complete and receipt.incomplete_reasons == ("partial_tail",)
    assert_healthy(bench, first + second, 2)
    assert bench.archive.load_receipt(r1.receipt_id) == r1


def test_a_partially_written_payload_suffix_is_continued_not_repeated(bench):
    first, second = first_batch(bench), second_batch(bench)
    bench.write(first)
    bench.archive.capture(bench.binding())
    bench.append(second)
    crash_during(bench, "payload_chunk", occurrence=2)
    assert len(bench.payload()) == len(first) + 200                     # two 100-byte chunks of the new suffix
    bench.archive.capture(bench.binding())
    assert bench.payload() == first + second and len(bench.payload()) == bench.source.stat().st_size


@pytest.mark.parametrize("stage", ["payload_appended", "receipt_published"])
def test_a_source_that_grew_after_the_crash_is_extended_after_adoption(bench, stage):
    first, second, third = first_batch(bench), second_batch(bench), bench.claude("five", uuid="m5")
    bench.write(first)
    bench.archive.capture(bench.binding())
    bench.append(second)
    crash_during(bench, stage)
    bench.append(third)
    receipt = bench.archive.capture(bench.binding())
    assert bench.payload() == first + second + third == bench.source_bytes()
    assert receipt.length == len(first + second + third)
    assert_healthy(bench, first + second + third, 3 if stage == "receipt_published" else 2)


def test_repeated_crashes_during_recovery_still_converge(bench):
    raw = first_batch(bench)
    bench.write(raw)
    crash_during(bench, "receipt_published")
    crash_during(bench, "pointer_temp")            # the recovery run itself dies while moving the pointer
    assert bench.archive.latest(bench.binding()) is None
    receipt = bench.archive.capture(bench.binding())
    assert receipt.sequence == 1
    assert_healthy(bench, raw, 1)


def test_a_lost_pointer_adopts_the_validated_chain_and_only_then(bench):
    first, second = first_batch(bench), second_batch(bench)
    bench.write(first)
    bench.archive.capture(bench.binding())
    bench.append(second)
    r2 = bench.archive.capture(bench.binding())
    (bench.lineage / "latest.json").unlink()
    assert bench.archive.latest(bench.binding()) is None
    assert error_of(bench.archive.load_receipt, r2.receipt_id).code == "receipt_uncommitted"
    assert bench.archive.capture(bench.binding()) == r2
    assert_healthy(bench, first + second, 2)


# ------------------------------------------------------------------ retained uncertainty

def crashed_tail(bench):
    """A committed first batch plus a second batch fully appended to the payload but never committed."""
    first, second = first_batch(bench), second_batch(bench)
    bench.write(first)
    r1 = bench.archive.capture(bench.binding())
    bench.append(second)
    crash_during(bench, "payload_appended")
    return first, second, r1


def test_an_unverifiable_tail_is_kept_unresolved_and_does_not_advance_the_read_boundary(bench):
    first, second, r1 = crashed_tail(bench)
    on_disk = bench.payload()
    flip_byte(bench.source, len(first) + 10)                            # the source no longer confirms the tail
    before = bench.snapshot()
    staged = sorted(path.name for path in (bench.lineage / "staging").iterdir())

    for _ in range(2):
        failure = error_of(bench.archive.capture, bench.binding())
        assert failure.code == "unresolved_tail" and not failure.retryable and failure.latest == r1
    assert bench.payload() == on_disk and len(on_disk) == len(first + second)     # uncertain bytes untouched
    assert bench.snapshot() == before                                  # nothing appended, published or discarded
    assert sorted(path.name for path in (bench.lineage / "staging").iterdir()) == staged
    assert len(staged) == 1                                            # the interrupted writer's staged copy is retained too
    assert bench.archive.latest(bench.binding()) == r1
    assert read_all(bench, r1) == first
    assert len(bench.receipt_files()) == 1


def test_a_source_shorter_than_the_tail_is_unresolved_but_below_the_committed_prefix_is_a_source_error(bench):
    first, second, r1 = crashed_tail(bench)
    on_disk = bench.payload()
    bench.write((first + second)[:len(first) + 7])
    assert error_of(bench.archive.capture, bench.binding()).code == "unresolved_tail"
    bench.write(first[:50])
    assert error_of(bench.archive.capture, bench.binding()).code == "source_shrunk"
    assert bench.payload() == on_disk and bench.archive.latest(bench.binding()) == r1
    bench.write(first + second)                                        # the source returns: recovery completes
    assert bench.archive.capture(bench.binding()).length == len(first + second)
    assert_healthy(bench, first + second, 2)


def test_a_published_receipt_is_not_adopted_when_the_source_or_archive_no_longer_supports_it(bench):
    raw = first_batch(bench)
    bench.write(raw)
    crash_during(bench, "receipt_published")
    published = bench.receipt_files()[0].read_bytes()

    replacement = bench.sources / "replacement"
    replacement.write_bytes(raw)
    original = bench.sources / "original"
    os.replace(bench.source, original)
    os.replace(replacement, bench.source)                              # same bytes, different file
    assert error_of(bench.archive.capture, bench.binding()).code == "source_replaced"
    assert bench.archive.latest(bench.binding()) is None

    os.replace(original, bench.source)
    flip_byte(bench.lineage / "payload.raw", 5)                        # the archived bytes no longer match the receipt
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    assert bench.archive.latest(bench.binding()) is None
    assert bench.receipt_files()[0].read_bytes() == published


def test_a_published_receipt_with_a_broken_chain_is_not_adopted(bench):
    first, second = first_batch(bench), second_batch(bench)
    bench.write(first)
    bench.archive.capture(bench.binding())
    bench.append(second)
    crash_during(bench, "receipt_published")
    ahead = bench.receipt_files()[1]
    body = ahead.read_bytes().split(b"\n", 1)[0]
    import json
    fields = json.loads(body)
    fields["predecessor_sha256"] = "0" * 64
    ahead.write_bytes(fmt.encode_receipt(fields))                      # valid record, but not this lineage's chain
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "archive_integrity" and failure.latest.sequence == 1
    assert bench.archive.latest(bench.binding()).sequence == 1

    ahead.write_bytes(b"garbage")
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    assert bench.archive.latest(bench.binding()).sequence == 1


def test_index_bytes_the_payload_does_not_confirm_are_never_adopted(bench):
    first, second, r1 = crashed_tail(bench)
    with (bench.lineage / "index.bin").open("ab") as stream:
        stream.write(b"\x07" * 32)
    failure = error_of(bench.archive.capture, bench.binding())
    assert failure.code == "archive_integrity" and failure.latest == r1
    assert len(bench.payload()) == len(first + second)


def test_inconsistent_stored_state_is_reported_not_repaired(bench):
    first = first_batch(bench)
    bench.write(first)
    r1 = bench.archive.capture(bench.binding())

    with (bench.lineage / "index.bin").open("ab") as stream:
        stream.write(b"\x00" * 32)                                      # an index entry with no payload behind it
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    with (bench.lineage / "index.bin").open("r+b") as stream:
        stream.truncate(32 * r1.index_entries)

    payload = bench.lineage / "payload.raw"
    with payload.open("r+b") as stream:
        stream.truncate(len(first) - 3)                                 # committed bytes missing
    assert error_of(bench.archive.capture, bench.binding()).code == "archive_integrity"
    assert bench.archive.latest(bench.binding()) == r1


def test_stale_temporaries_and_staging_copies_of_a_dead_writer_are_removed_once_nothing_is_uncertain(bench):
    raw = first_batch(bench)
    bench.write(raw)
    crash_during(bench, "staged")                                      # leaves only a staging copy
    assert len(list((bench.lineage / "staging").iterdir())) == 1
    (bench.lineage / ".tmp-deadbeef").write_bytes(b"x")
    bench.archive.capture(bench.binding())
    assert_healthy(bench, raw, 1)


def test_a_rejected_binding_does_not_disturb_an_unresolved_lineage(bench):
    first, second, r1 = crashed_tail(bench)
    before = bench.snapshot()
    assert error_of(bench.archive.capture, bench.binding(agent_id="other-agent")).code == "agent_mismatch"
    assert bench.snapshot() == before
    shutil.rmtree(bench.lineage / "staging")                            # a later recovery tolerates a missing staging dir
    assert bench.archive.capture(bench.binding()).sequence == 2
    assert_healthy(bench, first + second, 2)


# ------------------------------------------------------------ durability barriers before publication

@contextmanager
def barrier_log(bench, fail=None):
    """Ordered ("fsync", target) events of the lineage's files and directories; ``fail=(target, make_error)``
    raises ``make_error()`` from that target's fsync instead of flushing (a crash or an I/O error)."""
    events, real = [], os.fsync
    named = (("payload", "payload.raw"), ("index", "index.bin"), ("receipts", "receipts"), ("lineage", ""))

    def target(fd):
        info = os.fstat(fd)
        for name, child in named:
            try:
                other = (bench.lineage / child).stat()
            except FileNotFoundError:
                continue
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                return name
        return "other"

    def fsync(fd):
        name = target(fd)
        if fail and fail[0] == name:
            events.append(("fsync-failed", name))
            raise fail[1]()
        events.append(("fsync", name))
        real(fd)

    with mock.patch("os.fsync", fsync):
        yield events


def staged_in(bench, events):
    """An archive that records the writer's stage names into the same ordered log."""
    return bench.new_archive(lambda stage: events.append(("stage", stage)))


def before_the_receipt(events, barrier):
    assert barrier in events, events
    assert events.index(barrier) < events.index(("stage", "receipt_temp")), events


def test_recovery_flushes_a_fully_written_payload_before_it_publishes_the_receipt_that_commits_it(bench):
    bench.write(bench.claude("one"))
    with pytest.raises(SimulatedCrash):
        bench.new_archive(crash_at("payload_chunk")).capture(bench.binding())      # the only (last) write, no fsync
    assert bench.payload() == bench.source_bytes() and not bench.receipt_files()

    with barrier_log(bench) as events:
        receipt = staged_in(bench, events).capture(bench.binding())

    before_the_receipt(events, ("fsync", "payload"))                    # recovered bytes cross their barrier first
    assert (receipt.length, receipt.appended) == (len(bench.source_bytes()), len(bench.source_bytes()))
    assert read_all(bench, receipt) == bench.source_bytes() == bench.payload()
    assert temporaries(bench) == []


def test_recovery_also_syncs_the_lineage_directory_before_it_publishes_a_receipt_for_recovered_files(bench):
    bench.write(bench.claude("one"))
    bench.archive.capture(bench.binding())                              # receipts/ exists and is already synced
    bench.append(bench.claude("two", uuid="m2"))
    with pytest.raises(SimulatedCrash):
        bench.new_archive(crash_at("payload_chunk")).capture(bench.binding())

    with barrier_log(bench) as events:
        staged_in(bench, events).capture(bench.binding())

    before_the_receipt(events, ("fsync", "payload"))
    before_the_receipt(events, ("fsync", "lineage"))                    # the entry that names a recovered file


def test_recovery_flushes_index_entries_that_are_already_present_before_it_publishes_their_receipt(bench):
    bench.write(bench.claude("one") + b"{broken one\n")
    with barrier_log(bench, fail=("index", lambda: SimulatedCrash("index fsync"))):
        with pytest.raises(SimulatedCrash):
            bench.archive.capture(bench.binding())                      # entries written, the flush never completed
    assert (bench.lineage / "index.bin").stat().st_size == 32 and not bench.receipt_files()

    with barrier_log(bench) as events:
        receipt = staged_in(bench, events).capture(bench.binding())

    before_the_receipt(events, ("fsync", "index"))
    before_the_receipt(events, ("fsync", "payload"))
    assert (bench.lineage / "index.bin").stat().st_size == 32            # recovered, not appended again
    assert (receipt.malformed, receipt.index_entries) == (1, 1)
    assert [entry.kind for entry in bench.archive.index_entries(receipt)] == ["malformed"]


@pytest.mark.parametrize("target", ["payload", "index"])
def test_a_failed_flush_publishes_nothing_keeps_the_committed_boundary_and_retries_safely(bench, target):
    first = bench.claude("one") + b"{broken one\n"
    bench.write(first)
    committed = bench.archive.capture(bench.binding())
    bench.append(bench.claude("two", uuid="m2") + b"{broken two\n")
    before = bench.snapshot()

    with barrier_log(bench, fail=(target, lambda: OSError(errno.EIO, "injected fsync failure"))) as events:
        failure = error_of(bench.archive.capture, bench.binding())
    assert ("fsync-failed", target) in events
    assert (failure.code, failure.retryable, failure.latest) == ("io_error", True, committed)
    assert bench.archive.latest(bench.binding()) == committed           # the read boundary did not move
    assert [path.name for path in bench.receipt_files()] == [fmt.receipt_name(1)]
    assert read_all(bench, committed) == first
    assert bench.snapshot() != before                                   # the unconfirmed bytes are retained, not hidden

    retried = bench.archive.capture(bench.binding())
    assert retried.sequence == 2 and retried.predecessor_id == committed.receipt_id
    assert bench.payload() == bench.source_bytes() == read_all(bench, retried)          # no duplicate or lost bytes
    assert (retried.records, retried.malformed, retried.index_entries) == (2, 2, 2)
    assert (bench.lineage / "index.bin").stat().st_size == 2 * 32
    assert temporaries(bench) == []

