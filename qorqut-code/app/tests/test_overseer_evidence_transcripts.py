"""Synthetic transcript fixtures; no native sessions or provider calls."""
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.native_archive._types import RawRecord
from bazarlyk.native_chat.projection import marker_text
from bazarlyk.overseer.evidence import CaptureJournal, HostScanner


class Access:
    def __init__(self): self.denied_receipts = set()
    def resolve_selection(self, selection): return selection
    def authorize_source(self, reference):
        if reference.get("kind") == "transcript_entry" and reference.get("locator", {}).get("receipt_id") in self.denied_receipts:
            raise DomainError("forbidden", "Synthetic receipt access revoked")


@pytest.mark.parametrize("case", ["valid", "valid_null", "conflicting_native", "missing_source", "wrong_owner", "wrong_agent",
    "sensitive_input", "sensitive_message", "redacted_message", "missing_receipt", "wrong_lineage", "wrong_range",
    "ambiguous", "processing_claimed", "overseer_signoff", "other_signoff", "continuation", "deadline"])
def test_mixed_transcript_filters_input_mirrors_and_retains_unrelated_turns(tmp_path, monkeypatch, case):
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
      CREATE TABLE installation(id TEXT,owner_principal_id TEXT);
      CREATE TABLE native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,updated_at TEXT,complete INTEGER,error_code TEXT,transcript_path TEXT);
      CREATE TABLE native_sessions(id TEXT,agent_id TEXT,provider TEXT,conversation_id TEXT,transcript_path TEXT);
      CREATE TABLE native_inputs(id TEXT,agent_id TEXT,session_id TEXT,principal_id TEXT,input_type TEXT,origin TEXT,state TEXT,sensitive INTEGER,evidence_json TEXT,marker TEXT,source_key TEXT,reason TEXT);
      CREATE TABLE signoffs(id TEXT,proposer_agent_id TEXT,proposer_principal_id TEXT,review_business_id TEXT);
      CREATE TABLE signoff_notes(id TEXT,signoff_id TEXT,body TEXT);
      CREATE TABLE telegram_owner_inputs(id TEXT,purpose TEXT,correlation_id TEXT,signoff_id TEXT,owner_principal_id TEXT,agent_id TEXT,sensitive INTEGER,message_id TEXT);
      CREATE TABLE telegram_messages(id TEXT,body TEXT,direction TEXT,sensitive INTEGER,body_redacted INTEGER,reply_to_id TEXT,agent_id TEXT);
      INSERT INTO installation VALUES('service','owner');
      INSERT INTO native_captures VALUES('agent','claude','conversation','receipt','2026-01-01T00:00:00Z',1,NULL,'/synthetic');
      INSERT INTO native_sessions VALUES('session','agent','claude','conversation','/synthetic');
      INSERT INTO signoffs VALUES('11111111-1111-4111-8111-111111111111','overseer','overseer',NULL);
    """)
    def user(uid, text):
        return {"type": "user", "uuid": uid, "sessionId": "conversation", "message": {"content": text}}
    def agent(uid, text):
        return {"type": "assistant", "uuid": uid, "sessionId": "conversation",
                "message": {"id": uid, "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}}
    values = [user("owner-one", "[quoted overseer feedback]\nowner instruction"), agent("a1", "agent answer"),
              user("nudge", marker_text("abcdefgh", "inbox_nudge", "overseer private feedback")),
              agent("a2", "paraphrased overseer feedback"), user("unknown", "<qorqut-system-input id=\"unknown01\">hidden</qorqut-system-input>"),
              agent("a3", "paraphrase unknown input"), user("unbound", "unbound native user input"),
              agent("a-unbound", "response to unbound input"),
              {"type": "user", "uuid": "tool", "sessionId": "conversation", "message": {"content": [
                  {"type": "tool_result", "content": json.dumps({"status": "ok", "data": {"user_input": {
                      "version": 1, "message": "excluded feedback tool result", "references": [
                          {"type": "signoff", "id": "11111111-1111-4111-8111-111111111111", "business_id": None}]}}})}]}},
              user("owner-two", "unrelated later owner instruction"), agent("a4", "unrelated agent result")]
    records, offset = [], 0
    for value in values:
        data = (json.dumps(value) + "\n").encode()
        records.append(RawRecord(offset, len(data), "json", value))
        offset += len(data)
    for i in (0, 9):
        evidence = json.dumps({"native_input_observed": True, "processing_confirmed": False, "receipt_id": "receipt", "raw_offset": records[i].offset})
        connection.execute("INSERT INTO native_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("owner-input-" + str(i), "agent", "session", "owner", "telegram", "owner", "uncertain", 0, evidence, None,
             "telegram:telegram-" + str(i), "transcript_confirmed"))
        connection.execute("INSERT INTO telegram_owner_inputs VALUES(?,?,?,?,?,?,?,?)",
            ("telegram-" + str(i), "owner_message", "canonical-message-" + str(i), None, "owner", "agent", 0, "message-" + str(i)))
        text = "owner instruction" if i == 0 else "unrelated later owner instruction"
        connection.execute("INSERT INTO telegram_messages VALUES(?,?,?,?,?,?,?)", ("message-" + str(i), text, "inbound", 0, 0, None, "agent"))
    connection.execute("INSERT INTO native_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("nudge-input", "agent", "session", None, "inbox_nudge", "system", "submitted", 0, "{}", "abcdefgh", "nudge:key", None))
    if case in {"valid_null", "conflicting_native", "missing_source", "wrong_owner", "wrong_agent", "sensitive_input", "sensitive_message", "redacted_message", "wrong_range"}:
        connection.execute("UPDATE native_inputs SET principal_id=NULL WHERE id='owner-input-0'")
    if case == "conflicting_native":
        connection.execute("UPDATE native_inputs SET principal_id='not-owner' WHERE id='owner-input-0'")
    if case == "missing_source":
        connection.execute("DELETE FROM telegram_owner_inputs WHERE id='telegram-0'")
    if case == "wrong_owner":
        connection.execute("UPDATE telegram_owner_inputs SET owner_principal_id='not-owner' WHERE id='telegram-0'")
    if case == "wrong_agent":
        connection.execute("UPDATE telegram_owner_inputs SET agent_id='other-agent' WHERE id='telegram-0'")
    if case == "sensitive_input":
        connection.execute("UPDATE native_inputs SET sensitive=1 WHERE id='owner-input-0'")
    if case == "sensitive_message":
        connection.execute("UPDATE telegram_messages SET sensitive=1 WHERE id='message-0'")
    if case == "redacted_message":
        connection.execute("UPDATE telegram_messages SET body_redacted=1 WHERE id='message-0'")
    if case in {"missing_receipt", "wrong_lineage", "wrong_range", "processing_claimed"}:
        observed = {"native_input_observed": True, "processing_confirmed": case == "processing_claimed",
                    "receipt_id": None if case == "missing_receipt" else "bad" if case == "wrong_lineage" else "short" if case == "wrong_range" else "receipt",
                    "raw_offset": records[0].offset}
        connection.execute("UPDATE native_inputs SET evidence_json=? WHERE id='owner-input-0'", (json.dumps(observed),))
    if case == "ambiguous":
        observed = {"native_input_observed": True, "processing_confirmed": False, "receipt_id": "receipt", "raw_offset": records[0].offset}
        connection.execute("INSERT INTO native_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("duplicate", "agent", "session", "owner", "telegram", "owner", "uncertain", 0, json.dumps(observed), None,
             "telegram:telegram-0", "transcript_confirmed"))
    if case in {"overseer_signoff", "other_signoff"}:
        connection.execute("UPDATE telegram_owner_inputs SET purpose='signoff_reply',signoff_id='11111111-1111-4111-8111-111111111111' WHERE id='telegram-0'")
        connection.execute("INSERT INTO signoff_notes VALUES('canonical-message-0','11111111-1111-4111-8111-111111111111','canonical signoff reply')")
        if case == "other_signoff":
            connection.execute("INSERT INTO signoffs VALUES('22222222-2222-4222-8222-222222222222','agent','agent',NULL)")
            connection.execute("UPDATE telegram_owner_inputs SET signoff_id='22222222-2222-4222-8222-222222222222' WHERE id='telegram-0'")
            connection.execute("UPDATE signoff_notes SET signoff_id='22222222-2222-4222-8222-222222222222' WHERE id='canonical-message-0'")
    class Archive:
        @contextmanager
        def iter_records(self, receipt, offset=0):
            yield iter(record for record in records if record.offset >= offset)
        def load_receipt(self, identity):
            if identity == "bad":
                return SimpleNamespace(**{**vars(receipt), "agent_id": "different"})
            if identity == "short":
                return SimpleNamespace(**{**vars(receipt), "record_boundary": 0})
            assert identity == "receipt"
            return receipt
        def read_raw(self, receipt, *, offset, limit):
            return SimpleNamespace(data=b"x" * limit)
    receipt = SimpleNamespace(record_boundary=offset, archive_id="archive", receipt_id="receipt", agent_id="agent",
                              provider="claude", conversation_id="conversation")
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    monkeypatch.setattr(scanner, "_transcript_material", lambda *args: (Archive(), receipt))
    if case == "continuation":
        import bazarlyk.overseer.evidence as evidence_module
        monkeypatch.setattr(evidence_module, "MAX_TRANSCRIPT_RECORDS", 1)
    if case == "deadline":
        monkeypatch.setattr(scanner, "expired", lambda: True)
    refs, gaps = [], []
    for _ in range(20):
        found, page_gaps = scanner._transcript("selection", {"kind": "global", "business_id": None},
                                     ["agent", "claude", "conversation"], SimpleNamespace(connection=connection))
        refs.extend(found)
        gaps.extend(page_gaps)
        if not any(gap["code"] == "transcript_continuation_pending" for gap in page_gaps):
            break
    contents = [journal.get_source(ref) for ref in refs]
    if case in {"valid", "valid_null", "other_signoff", "continuation", "deadline"}:
        first_text = "canonical signoff reply" if case == "other_signoff" else "owner instruction"
        assert [value["text"] for value in contents] == [first_text, "agent answer", "unrelated later owner instruction", "unrelated agent result"]
        assert [ref["actor_kind"] for ref in refs] == ["human", "agent", "human", "agent"]
        assert refs[0]["origin_event_id"] == "canonical-message-0"
        assert refs[0]["provenance"]["association_kind"] == "host_reconciled_observation"
        assert refs[0]["provenance"]["association_method"] == "exact_text_and_session_time_window"
        assert refs[0]["provenance"]["processing_confirmed"] is False
        assert refs[0]["provenance"]["owner_principal_source"] == "telegram_owner_input"
        assert refs[0]["provenance"]["native_principal_id"] == (None if case == "valid_null" else "owner")
        from bazarlyk.native_chat.projection import Lineage
        assert refs[0]["record_id"] == Lineage("agent", "claude", "conversation", "archive").entry_id("native_id", "owner-one")
    else:
        assert [value["text"] for value in contents] == ["unrelated later owner instruction", "unrelated agent result"]
        if case != "overseer_signoff":
            assert any(gap["code"] == "unresolved_provenance" and gap.get("offset") == records[0].offset for gap in gaps)
    assert len({ref["record_id"] for ref in refs}) == len(refs)
    assert any(gap["code"] == "unresolved_provenance" for gap in gaps)
    assert any(gap["code"] == "tool_result_unresolved" for gap in gaps)
    if case == "valid":
        frontier = journal.frontier({"mode": "explicit", "namespaces": [{"kind": "global", "business_id": None}]},
                                     "selection")
        episode_ref = {"id": refs[0]["episode_id"]}
        first = journal.episode(episode_ref, frontier, max_chars=31)
        assert first["next_cursor"] and first["records"]
        access.denied_receipts.add("receipt")
        revoked = journal.episode(episode_ref, frontier, cursor=first["next_cursor"], max_chars=160000)
        assert revoked["records"] == [] and revoked["complete"] is False
        assert revoked["next_cursor"] is None

        access.denied_receipts.clear()
        cursor, pages, offsets, fragments = None, 0, {}, {ref["record_id"]: [] for ref in refs}
        expected = {ref["record_id"]: json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for ref, content in zip(refs, contents)}
        while True:
            page = journal.episode(episode_ref, frontier, cursor=cursor, max_chars=47)
            pages += 1
            for record in page["records"]:
                identity = record["source_ref"]["record_id"]
                assert record["offset"] == offsets.get(identity, 0)
                offsets[identity] = offsets.get(identity, 0) + len(record["content_fragment"])
                fragments[identity].append(record["content_fragment"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert pages > 2
        assert {identity: "".join(parts) for identity, parts in fragments.items()} == expected
    with pytest.raises(DomainError):
        journal.get_source({"provider": "native_archive", "kind": "transcript", "record_id": "parent"})
    connection.close()


def test_projector_reads_real_hash_verified_synthetic_archive(tmp_path, monkeypatch):
    from test_native_archive_support import Workbench
    base = tmp_path / "bench"
    base.mkdir()
    bench = Workbench(base)
    bench.write(bench.claude("synthetic owner input", uuid="owner-message") +
                bench.claude("synthetic agent output", uuid="message", role="assistant"))
    receipt = bench.archive.capture(bench.binding())
    connection = sqlite3.connect(tmp_path / "synthetic.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE installation(id TEXT,owner_principal_id TEXT);
        CREATE TABLE native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,updated_at TEXT,complete INTEGER,error_code TEXT,transcript_path TEXT);
        CREATE TABLE native_sessions(id TEXT,agent_id TEXT,provider TEXT,conversation_id TEXT,transcript_path TEXT);
        CREATE TABLE native_inputs(id TEXT,agent_id TEXT,session_id TEXT,principal_id TEXT,input_type TEXT,origin TEXT,state TEXT,sensitive INTEGER,evidence_json TEXT,marker TEXT,source_key TEXT,reason TEXT);
        CREATE TABLE signoffs(id TEXT,proposer_agent_id TEXT,proposer_principal_id TEXT,review_business_id TEXT);
        CREATE TABLE telegram_owner_inputs(id TEXT,purpose TEXT,correlation_id TEXT,signoff_id TEXT,owner_principal_id TEXT,agent_id TEXT,sensitive INTEGER,message_id TEXT);
      CREATE TABLE telegram_messages(id TEXT,body TEXT,direction TEXT,sensitive INTEGER,body_redacted INTEGER,reply_to_id TEXT,agent_id TEXT);
        INSERT INTO installation VALUES('service','owner');
    """)
    connection.execute("INSERT INTO native_captures VALUES(?,?,?,?,?,?,?,?)",
        (bench.agent, bench.provider, bench.conversation, receipt.receipt_id, "2026-01-01T00:00:00Z", 1, None, str(bench.source)))
    connection.execute("INSERT INTO native_sessions VALUES(?,?,?,?,?)", ("session", bench.agent, bench.provider, bench.conversation, str(bench.source)))
    evidence = json.dumps({"native_input_observed": True, "processing_confirmed": False, "receipt_id": receipt.receipt_id, "raw_offset": 0})
    connection.execute("INSERT INTO native_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("input", bench.agent, "session", "owner", "telegram", "owner", "uncertain", 0, evidence, None, "telegram:input", "transcript_confirmed"))
    connection.execute("INSERT INTO telegram_owner_inputs VALUES(?,?,?,?,?,?,?,?)", ("input", "owner_message", "original-message", None, "owner", bench.agent, 0, "telegram-message"))
    connection.execute("INSERT INTO telegram_messages VALUES(?,?,?,?,?,?,?)", ("telegram-message", "synthetic owner input", "inbound", 0, 0, None, bench.agent))
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    access = Access()
    journal = CaptureJournal(root, access, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    monkeypatch.setattr(scanner, "_transcript_material", lambda *args: (bench.archive, receipt))
    refs, gaps = scanner._transcript("selection", {"kind": "global", "business_id": None},
                                     [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert [journal.get_source(ref)["text"] for ref in refs] == ["synthetic owner input", "synthetic agent output"]
    assert refs[0]["origin_event_id"] == "original-message" and refs[0]["provenance"]["processing_confirmed"] is False
    assert refs[0]["locator"]["archive_id"] == receipt.archive_id
    original_receipt = receipt.receipt_id
    old_refs = list(refs)
    class Coverage:
        completed = []
        def completed_sources(self, requested): return self.completed
    coverage = Coverage()
    coverage.completed = old_refs.copy()
    journal.coverage = coverage
    bench.append(bench.claude("new agent output", uuid="new-message", role="assistant"))
    receipt = bench.archive.capture(bench.binding())
    connection.execute("UPDATE native_captures SET receipt_id=?", (receipt.receipt_id,))
    found, _ = scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert found[:2] == old_refs
    assert [journal.get_source(ref)["text"] for ref in journal.pending("selection")["items"]] == ["new agent output"]
    coverage.completed = found.copy()
    journal = CaptureJournal(root, access, coverage, overseer_ids={"overseer"})
    scanner = HostScanner(None, SimpleNamespace(principal_id="agent"), access, journal)
    monkeypatch.setattr(scanner, "_transcript_material", lambda *args: (bench.archive, receipt))
    repeated, _ = scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert repeated == found and journal.pending("selection")["items"] == []

    # A changed canonical association is a new exact revision despite identical words.
    evidence = json.loads(evidence)
    evidence["receipt_id"] = receipt.receipt_id
    connection.execute("UPDATE native_inputs SET evidence_json=? WHERE id='input'", (json.dumps(evidence),))
    changed, _ = scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert changed[0]["content_hash"] == old_refs[0]["content_hash"] and changed[0]["revision"] != old_refs[0]["revision"]
    assert changed[0]["origin_event_id"] == old_refs[0]["origin_event_id"]
    assert journal.pending("selection")["items"] == [changed[0]]
    coverage.completed.append(changed[0])

    # A provider ID reused at a distinct raw range and with different content is a new revision.
    bench.append(bench.claude("changed agent output", uuid="message", role="assistant"))
    receipt = bench.archive.capture(bench.binding())
    connection.execute("UPDATE native_captures SET receipt_id=?", (receipt.receipt_id,))
    revised, _ = scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert revised[-1]["record_id"] == old_refs[1]["record_id"] and revised[-1]["revision"] != old_refs[1]["revision"]
    assert [journal.get_source(ref)["text"] for ref in journal.pending("selection")["items"]] == ["changed agent output"]
    coverage.completed.append(revised[-1])

    # A formerly unresolved input is revisited once its canonical association becomes available.
    new_offset = receipt.record_boundary
    bench.append(bench.claude("later owner", uuid="later-owner") + bench.claude("later agent", uuid="later-agent", role="assistant"))
    receipt = bench.archive.capture(bench.binding())
    connection.execute("UPDATE native_captures SET receipt_id=?", (receipt.receipt_id,))
    _, gaps = scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert any(gap.get("offset") == new_offset and gap["code"] == "unresolved_provenance" for gap in gaps)
    observed = json.dumps({"native_input_observed": True, "processing_confirmed": False, "receipt_id": receipt.receipt_id, "raw_offset": new_offset})
    connection.execute("INSERT INTO native_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("input2", bench.agent, "session", "owner", "telegram", "owner", "uncertain", 0, observed, None, "telegram:input2", "transcript_confirmed"))
    connection.execute("INSERT INTO telegram_owner_inputs VALUES(?,?,?,?,?,?,?,?)", ("input2", "owner_message", "original-2", None, "owner", bench.agent, 0, "telegram-message2"))
    connection.execute("INSERT INTO telegram_messages VALUES(?,?,?,?,?,?,?)", ("telegram-message2", "later owner", "inbound", 0, 0, None, bench.agent))
    scanner._transcript("selection", {"kind": "global", "business_id": None},
        [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert [journal.get_source(ref)["text"] for ref in journal.pending("selection")["items"]] == ["later owner", "later agent"]

    # Reuse always reauthorizes the saved receipt and its original provenance.
    access.denied_receipts.add(original_receipt)
    with pytest.raises(DomainError) as revoked:
        scanner._transcript("selection", {"kind": "global", "business_id": None},
            [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert revoked.value.code == "forbidden"
    access.denied_receipts.clear()

    # A missing original immutable receipt is a gap, not a new replacement identity.
    load = bench.archive.load_receipt
    def unavailable(identity):
        if identity == original_receipt:
            raise ValueError("receipt removed")
        return load(identity)
    monkeypatch.setattr(bench.archive, "load_receipt", unavailable)
    with pytest.raises(DomainError) as missing:
        scanner._transcript("selection", {"kind": "global", "business_id": None},
            [bench.agent, bench.provider, bench.conversation], SimpleNamespace(connection=connection))
    assert missing.value.code == "evidence_unavailable"
    connection.close()
