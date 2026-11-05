"""The native capture idle path: an unchanged observation records nothing.

Counted by write units and by the SQL statements that touch ``native_captures`` (never by wall-clock): after a capture row exists, a
step that finds the transcript, receipt, binding, cursor, error and finalization state unchanged opens no write unit and updates no
timestamp, while growth, a changed binding, an earlier error, receipt growth with no completed reply and the final capture each
still advance exactly once. Synthetic agents, panes and transcripts only."""

from contextlib import contextmanager
import json
import os
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, utc_now

from bazarlyk.native_archive import CaptureError, TranscriptArchive
from bazarlyk.native_chat import captures as captures_module, store
from native_chat_support import Native, count, rows

CAPTURE_UNITS = {"native_chat.capture_cursor", "native_chat.capture_error", "native_chat.index"}
CAPTURE_SQL = ("UPDATE native_captures", "INSERT OR IGNORE INTO native_captures")


class Writes:
    """Counts the write units opened on the database and the SQL statements that change ``native_captures``."""

    def __init__(self, native):
        self.database, self.units, self.statements = native.runtime.database, [], []

    def __enter__(self):
        original = self.database.transaction

        @contextmanager
        def counted(*args, **kwargs):
            self.units.append(kwargs.get("operation"))
            with original(*args, **kwargs) as uow:
                uow.connection.set_trace_callback(self.statements.append)
                try:
                    yield uow
                finally:
                    uow.connection.set_trace_callback(None)

        self.database.transaction = counted
        return self

    def __exit__(self, *exc):
        del self.database.transaction

    def capture_units(self):
        return [name for name in self.units if name in CAPTURE_UNITS]

    def capture_statements(self):
        return [text for text in self.statements if text.lstrip().startswith(CAPTURE_SQL)]


def captures(native):
    return rows(native.runtime, "SELECT * FROM native_captures")


def snapshot(native):
    return captures(native), rows(native.runtime, "SELECT * FROM native_agent_counters")


def converse(native, provider, label):
    """One finished turn the provider's own way (a Codex rollout starts with its session metadata)."""
    if provider == "claude":
        native.write(native.claude_user(f"question {label}"),
                     native.claude_assistant(f"answer {label}", message_id=f"msg-{label}"))
    else:
        native.write(native.codex_session_meta(), native.codex_event("task_started"),
                     native.codex_event("user_message", message=f"question {label}"),
                     native.codex_assistant(f"answer {label}", final=True, native_id=f"item-{label}"),
                     native.codex_event("task_complete"))


def start(tmp_path, provider="claude"):
    native = Native(tmp_path, provider)
    if provider == "codex":
        native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    return native


# ----- the idle path ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_repeated_unchanged_observations_write_nothing_to_the_capture(tmp_path, provider):
    native = start(tmp_path, provider)
    converse(native, provider, "one")
    native.pass_(2)
    assert count(native.runtime, "native_replies") == 1
    before = snapshot(native)
    with Writes(native) as writes:
        native.pass_(6)
    assert writes.capture_units() == [] and writes.capture_statements() == []
    assert snapshot(native) == before                       # not even the timestamp of the cursor or the visible-change counter


def test_a_restart_over_an_unchanged_capture_writes_nothing_to_it(tmp_path):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    before = snapshot(native)
    native.build()                                          # a new indexer remembers nothing about the transcript's stamp
    with Writes(native) as writes:
        native.pass_(3)
    assert writes.capture_units() == [] and writes.capture_statements() == []
    assert snapshot(native) == before and count(native.runtime, "native_replies") == 1


def test_an_empty_transcript_error_is_recorded_once(tmp_path):
    native = start(tmp_path)                                # the hook published a transcript that holds nothing yet
    with Writes(native) as writes:
        native.pass_(1)
    assert [row["error_code"] for row in captures(native)] == ["no_identity_evidence"]
    assert writes.capture_units().count("native_chat.capture_error") == 1
    again = snapshot(native)
    with Writes(native) as writes:
        native.pass_(4)
    assert writes.capture_units() == [] and writes.capture_statements() == [] and snapshot(native) == again


def test_a_capture_that_does_not_exist_yet_is_created_by_the_guarded_write(tmp_path):
    native = start(tmp_path)
    observation = SimpleNamespace(agent_id=native.agent, provider="claude", conversation_id=native.session_id, transport="terminal",
                                  pid=native.pid, started=native.started, owner=None, transcript_path=str(native.transcript_path()))
    with native.runtime.database.transaction() as uow:
        session = store.create_session(uow.connection, observation, utc_now())
    assert captures(native) == []
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    with Writes(native) as writes:
        native.indexer.step(native.profile, session)
    assert writes.capture_units() == ["native_chat.capture_cursor", "native_chat.index"]
    assert count(native.runtime, "native_captures") == 1 and count(native.runtime, "native_replies") == 1
    settled = snapshot(native)
    with Writes(native) as writes:
        native.indexer.step(native.profile, session)
        native.indexer.step(native.profile, session)
    assert writes.units == [] and snapshot(native) == settled


def test_the_creation_rechecks_that_no_other_step_created_the_capture_first(tmp_path, monkeypatch):
    native = start(tmp_path)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_(2)
    [existing] = captures(native)
    with native.runtime.database.read() as uow:
        session = store.get_session(uow.connection, existing["session_id"])
    real, reads = store.get_capture, []

    def first_read_misses(conn, *identity):
        reads.append(identity)
        return None if len(reads) == 1 else real(conn, *identity)

    created = []
    monkeypatch.setattr(store, "get_capture", first_read_misses)
    monkeypatch.setattr(store, "ensure_capture", lambda *args: created.append(args))
    assert native.indexer._cursor(session) == existing
    assert created == [] and len(reads) == 2                # the guarded write found it and wrote nothing
    assert captures(native) == [existing]


# ----- growth, receipts and finalization still advance ------------------------------------------------------------------

def test_growth_advances_the_capture_once_and_then_it_is_idle_again(tmp_path):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    [first] = captures(native)
    for label in ("two", "three"):
        before = snapshot(native)
        converse(native, "claude", label)
        with Writes(native) as writes:
            native.pass_(1)
        assert writes.capture_units() == ["native_chat.index"]
        [grown] = captures(native)
        assert grown["receipt_sequence"] == before[0][0]["receipt_sequence"] + 1
        assert grown["captured_length"] > before[0][0]["captured_length"] and grown["indexed_offset"] == grown["captured_length"]
        assert grown["updated_at"] > before[0][0]["updated_at"]
        assert rows(native.runtime, "SELECT revision FROM native_agent_counters")[0]["revision"] > before[1][0]["revision"]
        settled = snapshot(native)
        with Writes(native) as writes:
            native.pass_(3)
        assert writes.capture_units() == [] and snapshot(native) == settled
    assert count(native.runtime, "native_replies") == 3 and first["receipt_sequence"] == 1


def test_receipt_growth_without_a_completed_reply_is_recorded_once(tmp_path):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    before = snapshot(native)
    pending = json.dumps(native.claude_user("a second question")).encode()
    half = len(pending) // 2
    with open(native.transcript_path(), "ab") as handle:    # the next record is still being written: no newline yet
        handle.write(pending[:half])
    with Writes(native) as writes:
        native.pass_(1)
    assert writes.capture_units() == ["native_chat.index"]
    [grown] = captures(native)
    assert grown["receipt_sequence"] == before[0][0]["receipt_sequence"] + 1 and grown["captured_length"] > before[0][0]["captured_length"]
    assert grown["indexed_offset"] == before[0][0]["indexed_offset"] and count(native.runtime, "native_replies") == 1
    assert rows(native.runtime, "SELECT revision FROM native_agent_counters")[0]["revision"] > before[1][0]["revision"]
    settled = snapshot(native)
    with Writes(native) as writes:
        native.pass_(3)
    assert writes.capture_units() == [] and snapshot(native) == settled
    with open(native.transcript_path(), "ab") as handle:
        handle.write(pending[half:] + b"\n")                # the record completes; indexing continues from the same cursor
    native.pass_(2)
    [done] = captures(native)
    assert done["indexed_offset"] > grown["indexed_offset"] and count(native.runtime, "native_replies") == 1


def test_the_final_capture_is_recorded_once_even_though_the_bytes_did_not_change(tmp_path):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    [live] = captures(native)
    assert live["finalized"] == 0
    native.end()
    with Writes(native) as writes:
        native.pass_(4)
    [final] = captures(native)
    assert writes.capture_units() == ["native_chat.index"]
    assert final["finalized"] == 1 and final["complete"] == 1 and final["captured_length"] == live["captured_length"]
    assert rows(native.runtime, "SELECT final_captured_at IS NOT NULL AS done FROM native_sessions") == [{"done": 1}]
    settled = snapshot(native)
    with Writes(native) as writes:
        native.pass_(4)
    assert writes.capture_units() == [] and snapshot(native) == settled
    assert count(native.runtime, "native_replies") == 1


def test_a_resumed_conversation_records_its_new_binding_once_and_finalizes_its_end_once(tmp_path):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    first_session = captures(native)[0]["session_id"]
    native.end()
    native.pass_(4)
    assert captures(native)[0]["finalized"] == 1
    native.new_session(same_conversation=True, pid=9001)    # `--resume`: the same conversation, a new process
    with Writes(native) as writes:
        native.pass_(3)
    [resumed] = captures(native)
    second_session = rows(native.runtime, "SELECT id FROM native_sessions WHERE generation=2")[0]["id"]
    assert resumed["session_id"] == second_session != first_session and resumed["finalized"] == 1
    assert writes.capture_units() == ["native_chat.index"]
    settled = snapshot(native)
    with Writes(native) as writes:
        native.pass_(3)
    assert writes.capture_units() == [] and snapshot(native) == settled

    native.end()                                            # the second binding's own end: its final capture is still recorded
    with Writes(native) as writes:
        native.pass_(4)
    assert writes.capture_units() == ["native_chat.index"]
    assert rows(native.runtime, "SELECT generation,final_captured_at IS NOT NULL AS done FROM native_sessions ORDER BY generation") == [
        {"generation": 1, "done": 1}, {"generation": 2, "done": 1}]
    assert count(native.runtime, "native_replies") == 1 and captures(native)[0]["finalized"] == 1


# ----- an earlier error is repaired, a repeated error is not rewritten --------------------------------------------------

def test_an_error_is_recorded_once_and_repaired_by_one_write_when_the_capture_works_again(tmp_path, monkeypatch):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    healthy = native.indexer._capture
    broken = {"on": True}

    def capture(*args):
        if broken["on"]:
            raise CaptureError("source_changed", "The transcript changed during capture", retryable=True)
        return healthy(*args)

    monkeypatch.setattr(native.indexer, "_capture", capture)
    os.utime(native.transcript_path(), ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))   # the stamp changes
    with Writes(native) as writes:
        native.pass_(1)
    assert [row["error_code"] for row in captures(native)] == ["source_changed"]
    assert writes.capture_units() == ["native_chat.capture_error"]
    failing = snapshot(native)
    with Writes(native) as writes:
        native.pass_(4)                                     # the same failure again: the row already says so
    assert writes.capture_units() == [] and writes.capture_statements() == [] and snapshot(native) == failing

    broken["on"] = False
    with Writes(native) as writes:
        native.pass_(1)
    [repaired] = captures(native)
    assert repaired["error_code"] is None and writes.capture_units() == ["native_chat.index"]
    assert repaired["indexed_offset"] == failing[0][0]["indexed_offset"] and count(native.runtime, "native_replies") == 1
    # Only the changed column (and the timestamp that goes with a real change) is written.
    assert {text.split(" WHERE")[0].rsplit("updated_at=", 1)[0] for text in writes.capture_statements()} == {
        "UPDATE native_captures SET error_code=NULL,"}
    settled = snapshot(native)
    with Writes(native) as writes:
        native.pass_(3)
    assert writes.capture_units() == [] and snapshot(native) == settled


def test_a_changed_failure_replaces_the_recorded_one(tmp_path, monkeypatch):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    code = {"value": "archive_busy"}

    def capture(*args):
        raise CaptureError(code["value"], "refused", retryable=True)

    monkeypatch.setattr(native.indexer, "_capture", capture)
    os.utime(native.transcript_path(), ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    native.pass_(2)
    assert [row["error_code"] for row in captures(native)] == ["archive_busy"]
    code["value"] = "source_changed"
    with Writes(native) as writes:
        native.pass_(2)
    assert [row["error_code"] for row in captures(native)] == ["source_changed"]
    assert writes.capture_units() == ["native_chat.capture_error"]


# ----- competing indexers and the maintenance gate ----------------------------------------------------------------------

def test_a_competing_step_that_already_advanced_the_cursor_is_not_applied_twice(tmp_path):
    native = start(tmp_path)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_(1)
    [advanced] = captures(native)
    assert advanced["indexed_offset"] > 0 and count(native.runtime, "native_replies") == 1
    with native.runtime.database.read() as uow:
        session = store.live_session(uow.connection, native.agent)
    archive = native.indexer.archive(native.profile)
    receipt = archive.load_receipt(advanced["receipt_id"])
    stale = {**advanced, "indexed_offset": 0, "receipt_sequence": 0, "captured_length": 0, "receipt_id": None,
             "index_state_json": "{}", "complete": 0}        # what a competing step read before the first one committed
    competitor = captures_module.Indexer(native.runtime)
    result = captures_module.StepResult()
    before = snapshot(native)
    with Writes(native) as writes:
        competitor._index(archive, stale, session, receipt, False, result)
    assert result.more is True and result.completions == 0
    assert writes.capture_units() == ["native_chat.index"] and writes.capture_statements() == []
    assert snapshot(native) == before and count(native.runtime, "native_replies") == 1


def test_closed_writes_stop_a_step_before_anything_is_archived(tmp_path, monkeypatch):
    native = start(tmp_path)
    converse(native, "claude", "one")
    native.pass_(2)
    converse(native, "claude", "two")                       # a changed transcript that would be archived
    before = snapshot(native)
    archived = []

    def refuse(uow):
        raise DomainError("maintenance", "Writes are unavailable during maintenance", retryable=True)

    monkeypatch.setattr(native.runtime.application, "ensure_writes_open", refuse)
    monkeypatch.setattr(TranscriptArchive, "capture", lambda *args, **kwargs: archived.append(args))
    with native.runtime.database.read() as uow:
        session = store.live_session(uow.connection, native.agent)
    with pytest.raises(DomainError) as error:
        native.indexer.step(native.profile, session)
    assert error.value.code == "maintenance" and archived == [] and snapshot(native) == before
    monkeypatch.undo()
    native.pass_(2)                                         # open again: the pending reply is archived and indexed
    assert count(native.runtime, "native_replies") == 2
