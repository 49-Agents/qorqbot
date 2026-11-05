"""Provider record rules: what is a visible entry, what is a completed reply, what carries application provenance.

Pure functions over parsed records; no archive, database or process."""

import json

from bazarlyk.native_archive import RawRecord
from bazarlyk.native_chat.projection import (KIND_NATIVE, KIND_RAW, MAX_RECEIPTS, Projector, ProjectorState, SPEAKER_AGENT,
                                             SPEAKER_NOTICE, SPEAKER_USER, marker_text, unwrap_claude_paste, unwrap_marker)
from native_chat_records import (claude_result, claude_results, codex_send_call, codex_tool_output, send_envelope, text_content)

CONVERSATION = "11111111-2222-4333-8444-555555555555"


def receipts(stored):
    """The typed marker resolver the capture boundary supplies: a marker id the application stored, answered with the type it
    stored it under when the text declared that very type; anything else proves nothing."""
    return lambda marker, declared: stored.get(marker) if stored.get(marker) == declared else None


def run(projector, *values):
    results, offset = [], 0
    for value in values:
        results.append(projector.feed(RawRecord(offset, 100, "json", value)))
        offset += 100
    return results


def completions(results):
    return [r.completion for r in results if r.completion]


def claude(kind, text, **extra):
    message = {"role": kind, "content": [{"type": "text", "text": text}]}
    message.update(extra.pop("message", {}))
    return {"type": kind, "uuid": extra.pop("uuid", f"u-{abs(hash((kind, text, tuple(sorted(extra)))))}"), "sessionId": CONVERSATION,
            "timestamp": "2026-10-11T10:00:00Z", "message": message, **extra}


# ----- Claude Code ------------------------------------------------------------------------------------------------------

def test_claude_completes_only_at_the_provider_end_of_turn_and_keys_by_message_id():
    projector = Projector("claude", CONVERSATION)
    results = run(projector,
                  claude("user", "do it"),
                  claude("assistant", "Looking now.", message={"id": "msg-1", "stop_reason": "tool_use"}),
                  claude("assistant", "Done.", message={"id": "msg-2", "stop_reason": "end_turn"}),
                  claude("assistant", "Done., more", message={"id": "msg-2", "stop_reason": "end_turn"}, uuid="other"))
    assert [r.entry.speaker for r in results] == [SPEAKER_USER, SPEAKER_AGENT, SPEAKER_AGENT, SPEAKER_AGENT]
    assert [r.entry.partial for r in results[1:]] == [True, False, False]
    done = completions(results)
    assert [(c.identity_kind, c.key) for c in done] == [(KIND_NATIVE, "msg-2"), (KIND_NATIVE, "msg-2")]   # same logical reply


def test_claude_ignores_other_conversations_sidechains_meta_and_internal_notices():
    projector = Projector("claude", CONVERSATION)
    foreign = claude("assistant", "Foreign.", message={"stop_reason": "end_turn"})
    foreign["sessionId"] = "someone-else"
    side = claude("assistant", "Subagent.", message={"stop_reason": "end_turn"}, isSidechain=True)
    meta = claude("user", "Caveat", isMeta=True)
    notice = claude("assistant", "<task-notification>done</task-notification>", message={"stop_reason": "end_turn"})
    command = claude("user", "<command-name>/clear</command-name>")
    reminder = claude("user", "<system-reminder>x</system-reminder>")
    tool_only = {"type": "assistant", "uuid": "t", "sessionId": CONVERSATION,
                 "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Read", "input": {}}]}}
    results = run(projector, foreign, side, meta, notice, command, reminder, tool_only)
    assert [r.entry for r in results] == [None] * 7 and completions(results) == []


def test_a_record_without_a_native_id_is_identified_by_its_raw_position_never_by_time_or_text():
    projector = Projector("claude", CONVERSATION)
    record = {"type": "assistant", "sessionId": CONVERSATION, "timestamp": "2026-10-11T10:00:00Z",
              "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Same words."}]}}
    first, second = run(projector, record, dict(record))
    assert first.completion.identity_kind == KIND_RAW and first.completion.key == "0#0" and second.completion.key == "100#0"


def test_a_human_prompt_folded_into_a_running_turn_is_a_user_entry_but_other_queued_commands_are_not():
    projector = Projector("claude", CONVERSATION)
    human = {"type": "attachment", "uuid": "q1", "sessionId": CONVERSATION,
             "attachment": {"type": "queued_command", "prompt": "Also check the tests", "commandMode": "prompt",
                            "origin": {"kind": "human"}}}
    task = {"type": "attachment", "uuid": "q2", "sessionId": CONVERSATION,
            "attachment": {"type": "queued_command", "prompt": "background task output", "commandMode": "prompt",
                           "origin": {"kind": "task-notification"}}}
    first, second = run(projector, human, task)
    assert first.entry.speaker == SPEAKER_USER and first.entry.text == "Also check the tests" and second.entry is None


# ----- provenance ------------------------------------------------------------------------------------------------------

def test_only_a_leading_application_marker_makes_a_notice_with_its_id_and_type():
    projector = Projector("claude", CONVERSATION)
    typed = marker_text("a" * 32, "inbox_nudge", "Check the inbox.")
    results = run(projector, claude("user", typed), claude("user", "  \n" + typed), claude("user", "Quote: " + typed),
                  claude("user", "Please review"))
    first, padded, quoted, plain = (r.entry for r in results)
    assert first.speaker == SPEAKER_NOTICE and first.marker == "a" * 32 and first.marker_type == "inbox_nudge"
    assert padded.speaker == SPEAKER_NOTICE and padded.marker == "a" * 32
    assert quoted.speaker == SPEAKER_USER and quoted.marker is None          # a quotation is the owner's own text
    assert plain.speaker == SPEAKER_USER and plain.marker is None
    assert unwrap_marker(typed) == "Check the inbox." and unwrap_marker("Plain text") == "Plain text"


# ----- Codex ------------------------------------------------------------------------------------------------------------

def codex(kind, **payload):
    return {"type": kind, "timestamp": "2026-10-11T10:00:00Z", "payload": payload}


def assistant(text, **extra):
    return codex("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": text}], **extra)


META = codex("session_meta", id=CONVERSATION, cwd="/w")


def test_codex_binds_only_to_its_own_session_meta():
    projector = Projector("codex", CONVERSATION)
    results = run(projector, codex("event_msg", type="user_message", message="before meta"),
                  codex("session_meta", id="other", cwd="/w"), assistant("Wrong conversation.", phase="final_answer"))
    assert all(r.entry is None and r.completion is None for r in results)
    bound = run(Projector("codex", CONVERSATION), META, assistant("Right.", phase="final_answer", id="item-1"))
    assert bound[1].completion.key == "item-1"


def test_codex_completes_at_turn_end_from_the_last_candidate_and_not_at_narration():
    projector = Projector("codex", CONVERSATION)
    results = run(projector, META, codex("event_msg", type="task_started"), codex("event_msg", type="user_message", message="go"),
                  assistant("Thinking out loud."), assistant("Final words."), codex("event_msg", type="task_complete"))
    [done] = completions(results)
    assert done.preview == "Final words." and done.identity_kind == KIND_RAW and results[3].completion is None
    assert results[-1].entry is None           # the candidate was shown at its own record; the turn end adds no second copy


def test_codex_final_answer_completes_once_even_if_the_turn_end_follows():
    results = run(Projector("codex", CONVERSATION), META, codex("event_msg", type="task_started"),
                  assistant("The answer.", phase="final_answer", id="item-9"), codex("event_msg", type="task_complete"))
    assert [c.key for c in completions(results)] == ["item-9"]
    assert results[-1].entry is None           # the turn end repeats nothing that was already shown


def test_codex_aborted_turns_and_reasoning_never_complete():
    results = run(Projector("codex", CONVERSATION), META, codex("event_msg", type="task_started"),
                  assistant("Hidden reasoning.", channel="analysis"), assistant("Half an answer."),
                  codex("event_msg", type="turn_aborted"), codex("event_msg", type="task_complete"))
    assert completions(results) == []


def test_codex_turn_end_message_is_used_when_no_item_was_seen():
    results = run(Projector("codex", CONVERSATION), META, codex("event_msg", type="task_started"),
                  codex("event_msg", type="task_complete", last_agent_message="Summary from the turn end."))
    [done] = completions(results)
    assert done.preview == "Summary from the turn end." and done.key.endswith("#end")
    # The reply has no assistant record of its own, so the turn-end record is its one visible entry, with the full text.
    entry = results[-1].entry
    assert entry.turn_end and entry.speaker == SPEAKER_AGENT and entry.text == "Summary from the turn end."
    assert (entry.identity_kind, entry.key, entry.offset) == (done.identity_kind, done.key, done.offset)


def test_codex_turn_state_survives_a_receipt_boundary():
    first = Projector("codex", CONVERSATION)
    run(first, META, codex("event_msg", type="task_started"), assistant("Candidate reply."))
    saved = first.state.dump()
    resumed = Projector("codex", CONVERSATION, ProjectorState.load(saved))
    [end] = run(resumed, codex("event_msg", type="task_complete"))
    assert end.completion.preview == "Candidate reply."
    assert ProjectorState.load("garbage").dump() == ProjectorState().dump()


def test_a_codex_user_message_is_an_entry_and_a_marker_in_it_is_a_notice():
    projector = Projector("codex", CONVERSATION)
    typed = marker_text("b" * 32, "inbox_nudge", "Please look")
    _, plain, marked = run(projector, META, codex("event_msg", type="user_message", message="Hello"),
                           codex("event_msg", type="user_message", message=typed))
    assert plain.entry.speaker == SPEAKER_USER and plain.entry.text == "Hello"
    assert marked.entry.speaker == SPEAKER_NOTICE and marked.entry.marker == "b" * 32


# ----- who started the turn a reply completed, and whether it included real user work ------------------------------------

NUDGE, RECOVERY = "inbox_nudge", "bootstrap_recovery"


def origins(results):
    return [c.origin for c in completions(results)]


def work(results):
    return [c.user_work for c in completions(results)]


def test_claude_reply_origin_is_the_first_input_of_its_turn_and_a_steered_notice_does_not_change_it_or_clear_its_work():
    nudge = marker_text("c" * 32, NUDGE, "notice")
    projector = Projector("claude", CONVERSATION, resolver=receipts({"c" * 32: NUDGE, "d" * 32: NUDGE, "e" * 32: RECOVERY}))
    results = run(projector,
                  claude("user", "do it"),                                       # the owner's turn is running ...
                  claude("assistant", "Looking.", message={"id": "m1", "stop_reason": "tool_use"}),
                  claude("user", nudge),                                         # ... a nudge is steered into it ...
                  claude("assistant", "Done.", message={"id": "m2", "stop_reason": "end_turn"}),    # ... the owner's work finishes
                  claude("user", nudge.replace("c" * 32, "d" * 32)),             # a later turn the nudge itself started
                  claude("assistant", "Checked.", message={"id": "m3", "stop_reason": "end_turn"}),
                  claude("user", marker_text("e" * 32, RECOVERY, "x")),          # and one the recovery wake started
                  claude("assistant", "Ok.", message={"id": "m4", "stop_reason": "end_turn"}))
    assert origins(results) == ["user", "nudge", "system"]
    assert work(results) == [True, False, False]


def test_a_nudge_the_owner_steers_into_is_still_the_turns_origin_and_the_turn_is_work_in_either_order():
    nudge = marker_text("c" * 32, NUDGE, "notice")
    proven = receipts({"c" * 32: NUDGE})
    nudge_then_user = run(Projector("claude", CONVERSATION, resolver=proven),
                          claude("user", nudge), claude("user", "also this"),
                          claude("assistant", "Both.", message={"id": "m1", "stop_reason": "end_turn"}))
    user_then_nudge = run(Projector("claude", CONVERSATION, resolver=proven),
                          claude("user", "this"), claude("user", nudge),
                          claude("assistant", "Both.", message={"id": "m2", "stop_reason": "end_turn"}))
    assert (origins(nudge_then_user), work(nudge_then_user)) == (["nudge"], [True])
    assert (origins(user_then_nudge), work(user_then_nudge)) == (["user"], [True])
    # The next turn starts clean: a reply to a nudge alone is not work, whatever came before.
    alone = run(Projector("claude", CONVERSATION, resolver=proven), claude("user", "this"),
                claude("assistant", "One.", message={"id": "m3", "stop_reason": "end_turn"}), claude("user", nudge),
                claude("assistant", "Two.", message={"id": "m4", "stop_reason": "end_turn"}))
    assert (origins(alone), work(alone)) == (["user", "nudge"], [True, False])


def test_text_that_only_looks_like_a_marker_is_user_work_and_a_foreign_or_mistyped_marker_proves_nothing():
    nudge = marker_text("c" * 32, NUDGE, "notice")
    stored_as_recovery = receipts({"c" * 32: RECOVERY})                     # the application stored another type under that id
    for resolver in (None, receipts({}), stored_as_recovery):
        results = run(Projector("claude", CONVERSATION, resolver=resolver), claude("user", nudge),
                      claude("assistant", "Hi.", message={"id": "m1", "stop_reason": "end_turn"}))
        assert results[0].entry.speaker == SPEAKER_NOTICE and results[0].entry.marker == "c" * 32    # it still displays as a notice
        assert (origins(results), work(results)) == (["user"], [True])


def test_a_turn_whose_start_was_not_seen_is_unknown_and_an_empty_end_of_turn_clears_the_open_origin_and_work():
    projector = Projector("claude", CONVERSATION, resolver=receipts({"f" * 32: NUDGE}))
    results = run(projector, claude("assistant", "Late.", message={"id": "m1", "stop_reason": "end_turn"}),
                  claude("user", "first"), {**claude("assistant", "", message={"id": "m2", "stop_reason": "end_turn"}),
                                           "message": {"id": "m2", "stop_reason": "end_turn", "role": "assistant", "content": []}},
                  claude("user", marker_text("f" * 32, NUDGE, "n")),
                  claude("assistant", "Yes.", message={"id": "m3", "stop_reason": "end_turn"}))
    assert (origins(results), work(results)) == (["unknown", "nudge"], [False, False])


def test_claude_turn_work_survives_a_restart_between_the_input_and_the_completion():
    nudge = marker_text("c" * 32, NUDGE, "notice")
    proven = receipts({"c" * 32: NUDGE})
    first = Projector("claude", CONVERSATION, resolver=proven)
    run(first, claude("user", nudge), claude("user", "and this"))
    saved = first.state.dump()
    assert saved["origin"] == "nudge" and saved["work"] is True
    resumed = Projector("claude", CONVERSATION, ProjectorState.load(saved), proven)
    done = run(resumed, claude("assistant", "Done.", message={"id": "m1", "stop_reason": "end_turn"}))
    assert (origins(done), work(done)) == (["nudge"], [True])
    assert resumed.state.dump()["work"] is False and resumed.state.dump()["origin"] is None
    # A nudge alone, restarted before its reply, is still no work.
    solo = Projector("claude", CONVERSATION, resolver=proven)
    run(solo, claude("user", nudge))
    quiet = run(Projector("claude", CONVERSATION, ProjectorState.load(solo.state.dump()), proven),
                claude("assistant", "Checked.", message={"id": "m2", "stop_reason": "end_turn"}))
    assert (origins(quiet), work(quiet)) == (["nudge"], [False])


def test_projector_origin_work_and_binding_survive_a_receipt_boundary_for_codex():
    nudge = marker_text("a" * 32, NUDGE, "notice")
    proven = receipts({"a" * 32: NUDGE})
    first = Projector("codex", CONVERSATION, resolver=proven)
    run(first, META, codex("event_msg", type="task_started"), codex("event_msg", type="user_message", message="go"))
    resumed = Projector("codex", CONVERSATION, ProjectorState.load(first.state.dump()), proven)
    mid = run(resumed, codex("event_msg", type="user_message", message=nudge),            # steered into the owner's open turn
              assistant("All done.", phase="final_answer", id="item-1"), codex("event_msg", type="task_complete"))
    assert (origins(mid), work(mid)) == (["user"], [True])
    # A later batch starts after a finished turn: the rollout's binding is remembered, so the next turn is still its own.
    later = Projector("codex", CONVERSATION, ProjectorState.load(resumed.state.dump()), proven)
    results = run(later, codex("event_msg", type="task_started"), codex("event_msg", type="user_message", message=nudge),
                  assistant("Checked.", phase="final_answer", id="item-2"), codex("event_msg", type="task_complete"))
    assert (origins(results), work(results)) == (["nudge"], [False])
    stranger = Projector("codex", CONVERSATION, ProjectorState.load(resumed.state.dump()), proven)
    other = run(stranger, {"type": "session_meta", "payload": {"id": "someone-else"}}, codex("event_msg", type="task_started"),
                assistant("Foreign.", phase="final_answer", id="item-3"), codex("event_msg", type="task_complete"))
    assert completions(other) == []


def test_codex_steering_in_either_order_and_a_restart_before_the_turn_end_keep_the_turn_work():
    nudge = marker_text("a" * 32, NUDGE, "notice")
    proven = receipts({"a" * 32: NUDGE})
    # nudge -> owner -> end, restarted between the inputs and the turn end (the reply is only a turn-end candidate)
    first = Projector("codex", CONVERSATION, resolver=proven)
    run(first, META, codex("event_msg", type="task_started"), codex("event_msg", type="user_message", message=nudge),
        codex("event_msg", type="user_message", message="please also"), assistant("Narrated answer."))
    resumed = Projector("codex", CONVERSATION, ProjectorState.load(first.state.dump()), proven)
    ended = run(resumed, codex("event_msg", type="task_complete"))
    assert (origins(ended), work(ended)) == (["nudge"], [True])
    # owner -> nudge -> end, with a final answer item that completes before the turn end
    other = run(Projector("codex", CONVERSATION, resolver=proven), META, codex("event_msg", type="task_started"),
                codex("event_msg", type="user_message", message="please"), codex("event_msg", type="user_message", message=nudge),
                assistant("Done.", phase="final_answer", id="item-4"), codex("event_msg", type="task_complete"))
    assert (origins(other), work(other)) == (["user"], [True])
    # a nudge alone, restarted before its end
    solo = Projector("codex", CONVERSATION, resolver=proven)
    run(solo, META, codex("event_msg", type="task_started"), codex("event_msg", type="user_message", message=nudge))
    alone = run(Projector("codex", CONVERSATION, ProjectorState.load(solo.state.dump()), proven),
                assistant("Checked.", phase="final_answer", id="item-5"), codex("event_msg", type="task_complete"))
    assert (origins(alone), work(alone)) == (["nudge"], [False])


def test_projector_state_loads_a_state_saved_before_the_work_flag_as_no_work():
    legacy = {"turn_open": True, "turn_done": False, "candidate": None, "origin": "user", "bound": True}
    assert ProjectorState.load(legacy).work is False
    assert ProjectorState.load({**legacy, "work": True}).work is True


def test_codex_turn_end_without_an_item_carries_the_turn_origin_and_work_too():
    results = run(Projector("codex", CONVERSATION), META, codex("event_msg", type="task_started"),
                  codex("event_msg", type="user_message", message="please"),
                  codex("event_msg", type="task_complete", last_agent_message="Summary."))
    assert (origins(results), work(results)) == (["user"], [True])


def test_claude_pasted_owner_text_is_visible_but_quoted_or_mismatched_wrappers_are_not_unwrapped():
    text = '\n\n<pasted_content id="8788">\nOwner message\n</pasted_content id="8788">\n'
    exact, quote, bad = run(Projector('claude', CONVERSATION), claude('user', text),
                           claude('user', 'Quoted: ' + text), claude('user', text.replace('</pasted_content id="8788">',
                                                                                           '</pasted_content id="9999">')))
    assert exact.entry.speaker == SPEAKER_USER and exact.entry.text == 'Owner message'
    assert quote.entry.text != 'Owner message'
    assert bad.entry is None


def pasted(text, opening, closing=None):
    return f'\n\n<pasted_content id="{opening}">\n{text}\n</pasted_content id="{closing or opening}">\n'


def test_claude_pasted_owner_text_with_a_numeric_or_hexadecimal_id_is_one_visible_user_message_and_owner_work():
    for identifier in ('8788', '4fa4', '4FA4', '0aB9c'):
        inner = 'Owner message\n\nsecond paragraph'
        typed = run(Projector('claude', CONVERSATION), claude('user', pasted(inner, identifier)),
                    claude('assistant', 'Done.', message={'id': 'm1', 'stop_reason': 'end_turn'}))
        queued = run(Projector('claude', CONVERSATION),
                     {'type': 'attachment', 'uuid': 'q1', 'sessionId': CONVERSATION,
                      'attachment': {'type': 'queued_command', 'prompt': pasted(inner, identifier), 'commandMode': 'prompt',
                                     'origin': {'kind': 'human'}}},
                     claude('assistant', 'Done.', message={'id': 'm1', 'stop_reason': 'end_turn'}))
        for results in (typed, queued):
            assert results[0].entry.speaker == SPEAKER_USER and results[0].entry.text == inner, identifier
            assert results[0].entry.marker is None and not results[0].hidden, identifier
            assert (origins(results), work(results)) == (['user'], [True]), identifier


def test_claude_paste_wrappers_are_unwrapped_only_when_whole_and_with_the_same_exact_id():
    owner = 'Owner message'
    cases = {
        'hex ids that differ': pasted(owner, '4fa4', '4fa5'),
        'ids that differ only by letter case': pasted(owner, '4fa4', '4FA4'),
        'a non-hexadecimal id': pasted(owner, 'zz12'),
        'an empty id': pasted(owner, ''),
        'text after the wrapper': pasted(owner, '4fa4') + 'Afterwards',
    }
    for name, text in cases.items():
        for record in (claude('user', text),
                       {'type': 'attachment', 'uuid': 'q1', 'sessionId': CONVERSATION,
                        'attachment': {'type': 'queued_command', 'prompt': text, 'commandMode': 'prompt',
                                       'origin': {'kind': 'human'}}}):
            [result] = run(Projector('claude', CONVERSATION), record)
            assert result.entry is None and result.hidden, name              # still the generic internal notice, not the owner
    [quoted] = run(Projector('claude', CONVERSATION), claude('user', 'Quoted: ' + pasted(owner, '4fa4')))
    assert quoted.entry.speaker == SPEAKER_USER and quoted.entry.text != owner
    assert unwrap_claude_paste(pasted(owner, '4fa4')) == owner and unwrap_claude_paste(pasted(owner, '4fa4', '4fa5')) != owner


def test_claude_hexadecimal_paste_wrapper_does_not_surface_ignored_metadata_or_system_records():
    wrapped = pasted('Owner message', '4fa4')
    results = run(Projector('claude', CONVERSATION),
                  claude('user', wrapped, isMeta=True), claude('user', wrapped, isSidechain=True),
                  claude('user', pasted('<system-reminder>x</system-reminder>', '4fa4')),
                  {'type': 'attachment', 'uuid': 'q2', 'sessionId': CONVERSATION,
                   'attachment': {'type': 'queued_command', 'prompt': wrapped, 'commandMode': 'prompt',
                                  'origin': {'kind': 'task-notification'}}},
                  {'type': 'attachment', 'uuid': 'q3', 'sessionId': CONVERSATION,
                   'attachment': {'type': 'queued_command', 'prompt': wrapped, 'commandMode': 'prompt', 'isMeta': True,
                                  'origin': {'kind': 'human'}}})
    assert [r.entry for r in results] == [None] * 5


# ----- Successful outgoing agent messages -------------------------------------------------------------------------------

def receipts_of(results):
    return [r.entry.receipts if r.entry else () for r in results]


def sent(*message_ids, **options):
    """A Claude tool-result block holding one compact send result per ID, each in its own text block."""
    return claude_result(text_content(*(send_envelope(identity, **options) for identity in message_ids)))


def test_a_claude_send_result_is_a_text_free_notice_of_its_own_record_that_names_the_message():
    projector = Projector("claude", CONVERSATION)
    compact = claude_results(CONVERSATION, sent("m-1"), uuid_="r-1")
    full = claude_results(CONVERSATION, claude_result(text_content(send_envelope("m-2", full=True))), uuid_="r-2")
    plain = claude_results(CONVERSATION, claude_result(send_envelope("m-3")), uuid_="r-3")
    results = run(projector, compact, full, plain)
    assert receipts_of(results) == [("m-1",), ("m-2",), ("m-3",)]
    first = results[0].entry
    assert (first.speaker, first.text, first.marker, first.marker_type) == (SPEAKER_NOTICE, "", None, None)
    assert (first.identity_kind, first.key, first.offset, first.length) == (KIND_NATIVE, "r-1", 0, 100)
    assert first.timestamp == "2026-10-16T10:00:00.000Z" and first.partial is None and first.turn_end is False
    assert completions(results) == [] and projector.state.dump() == ProjectorState().dump()


def test_a_claude_record_collects_unique_receipts_in_block_order_and_skips_errors_lists_and_other_output():
    projector = Projector("claude", CONVERSATION)
    failure = json.dumps({"status": "error", "error": {"code": "not_found", "message": "Recipient is unavailable"}})
    listing = json.dumps({"status": "ok", "data": {"items": [json.loads(send_envelope("listed"))["data"]["message"]]}})
    record = claude_results(CONVERSATION,
                            sent("m-1"), claude_result(failure, is_error=True), claude_result(send_envelope("flagged"), is_error=True),
                            claude_result(text_content(listing)), claude_result("plain output"), sent("m-2", "m-1"), sent("m-3"))
    assert receipts_of(run(projector, record)) == [("m-1", "m-2", "m-3")]
    # Nothing successful in the record: no entry at all, whatever else it carries.
    nothing = claude_results(CONVERSATION, claude_result(failure, is_error=True), claude_result(text_content(listing)))
    assert run(Projector("claude", CONVERSATION), nothing)[0].entry is None


def test_claude_receipts_are_read_only_from_the_json_of_tool_results_never_from_text_or_quotes():
    envelope = send_envelope("m-1")
    quoted = [
        claude_results(CONVERSATION, claude_result(f"Result: {envelope}")),                                  # prefixed
        claude_results(CONVERSATION, claude_result(json.dumps(envelope))),                                   # a JSON string
        claude_results(CONVERSATION, claude_result(envelope + "\nmore")),                                    # trailing text
        claude_results(CONVERSATION, claude_result(json.dumps({"note": json.loads(envelope)}))),             # nested elsewhere
        claude_results(CONVERSATION, claude_result(json.dumps([json.loads(envelope)]))),                     # a list
        claude_results(CONVERSATION, claude_result(text_content(envelope)[0]["text"].replace("{", "{ ", 1) + "}")),   # not JSON
        {**claude_results(CONVERSATION), "toolUseResult": text_content(envelope)},                            # client metadata only
        claude_results(CONVERSATION, {"type": "tool_use", "id": "t", "name": "agents_message_send", "input": {"message": envelope}}),
        claude_results(CONVERSATION, {"type": "image", "source": envelope}),
        claude_results(CONVERSATION, claude_result([{"type": "image", "text": envelope}])),
        claude_results(CONVERSATION, claude_result(None)),
    ]
    assert receipts_of(run(Projector("claude", CONVERSATION), *quoted)) == [()] * len(quoted)
    # Ordinary user and assistant text that merely contains a result stays what it is.
    typed = run(Projector("claude", CONVERSATION), claude("user", envelope),
                claude("assistant", envelope, message={"id": "msg-1", "stop_reason": "end_turn"}))
    assert [(r.entry.speaker, r.entry.text, r.entry.receipts) for r in typed] == [(SPEAKER_USER, envelope, ()), (SPEAKER_AGENT, envelope, ())]


def test_a_send_result_needs_the_status_and_every_receipt_field():
    base = json.loads(send_envelope("m-1"))
    broken = []
    for name in ("id", "event_id", "thread_id", "created_at", "recipient_id"):
        value = json.loads(send_envelope("m-1"))
        del value["data"]["message"][name]
        broken.append(value)
    for name, bad in (("id", ""), ("id", 7), ("event_id", True), ("event_id", "3"), ("thread_id", None), ("recipient_id", ["x"])):
        value = json.loads(send_envelope("m-1"))
        value["data"]["message"][name] = bad
        broken.append(value)
    broken += [{**base, "status": "error"}, {**base, "status": "ok-ish"}, {"data": base["data"]}, {**base, "data": {"result": 1}},
               {**base, "data": {"message": "m-1"}}, {**base, "data": [base["data"]]}]
    records = [claude_results(CONVERSATION, claude_result(json.dumps(value))) for value in broken]
    assert receipts_of(run(Projector("claude", CONVERSATION), *records)) == [()] * len(records)


def test_output_that_is_not_a_bounded_json_object_is_refused_without_an_error():
    deep, large = "[" * 90_000, send_envelope("m-1", note="x" * 200_000)
    records = [claude_results(CONVERSATION, claude_result(text)) for text in (deep, "{" * 90_000, large, "", "{}", "null")]
    assert receipts_of(run(Projector("claude", CONVERSATION), *records)) == [()] * len(records)


def test_visible_user_text_in_a_tool_result_record_is_never_lost_to_a_receipt():
    projector = Projector("claude", CONVERSATION)
    mixed = claude_results(CONVERSATION, {"type": "text", "text": "Please continue"}, sent("m-1"))
    (result,) = run(projector, mixed)
    assert (result.entry.speaker, result.entry.text, result.entry.receipts) == (SPEAKER_USER, "Please continue", ())


def test_hidden_harness_text_beside_a_tool_result_does_not_hide_its_receipt():
    reminder = {"type": "text", "text": "<system-reminder>Synthetic reminder</system-reminder>"}
    projector = Projector("claude", CONVERSATION)
    (result,) = run(projector, claude_results(CONVERSATION, sent("m-1"), reminder))
    assert result.entry.receipts == ("m-1",) and projector.state.dump() == ProjectorState().dump()
    (hidden,) = run(Projector("claude", CONVERSATION), claude_results(CONVERSATION, reminder))
    assert hidden.entry is None and hidden.hidden is True


def test_claude_receipts_follow_the_conversation_sidechain_and_meta_boundaries():
    foreign = claude_results("someone-else", sent("m-1"))
    side = claude_results(CONVERSATION, sent("m-2"), isSidechain=True)
    meta = claude_results(CONVERSATION, sent("m-3"), isMeta=True)
    assert receipts_of(run(Projector("claude", CONVERSATION), foreign, side, meta)) == [()] * 3


def test_a_claude_record_without_a_native_id_is_identified_by_its_raw_position():
    record = claude_results(CONVERSATION, sent("m-1"))
    del record["uuid"]
    first, second = run(Projector("claude", CONVERSATION), record, dict(record))
    assert [(r.entry.identity_kind, r.entry.key) for r in (first, second)] == [(KIND_RAW, "0#0"), (KIND_RAW, "100#0")]


def test_a_codex_completed_bazarlyk_send_is_a_notice_and_its_duplicate_output_envelope_is_ignored():
    projector = Projector("codex", CONVERSATION)
    meta = {"type": "session_meta", "payload": {"id": CONVERSATION}}
    envelope = send_envelope("m-1")
    results = run(projector, meta, codex_send_call(envelope, item_id="call_1", thread=CONVERSATION), codex_tool_output(envelope, call_id="call_1"))
    assert receipts_of(results) == [(), ("m-1",), ()]
    entry = results[1].entry
    assert (entry.speaker, entry.text, entry.marker, entry.identity_kind, entry.key) == (SPEAKER_NOTICE, "", None, KIND_NATIVE, "call_1")
    assert completions(results) == [] and projector.state.dump() == {**ProjectorState().dump(), "bound": True}
    unnamed = codex_send_call(envelope)
    unnamed["payload"]["item"].pop("id")
    (raw,) = run(Projector("codex", CONVERSATION, ProjectorState(bound=True)), unnamed)
    assert (raw.entry.identity_kind, raw.entry.key) == (KIND_RAW, "0#0")


def test_codex_refuses_failed_calls_other_tools_foreign_threads_and_unbound_rollouts():
    envelope = send_envelope("m-1")
    refused = [codex_send_call(envelope, status="failed"), codex_send_call(envelope, status="inProgress"),
               codex_send_call(envelope, is_error=True), codex_send_call(envelope, error={"message": "failed"}),
               codex_send_call(envelope, server="other"), codex_send_call(envelope, tool="agents_messages_list"),
               codex_send_call(envelope, thread="another-conversation"), codex_send_call(envelope, thread=7),
               codex_send_call(json.dumps({"status": "error", "error": {"code": "not_found"}})),
               codex_send_call(f"sent: {envelope}"), codex_send_call(), codex_send_call(envelope, result=None)]
    refused[-1]["payload"]["item"]["result"] = None
    bound = Projector("codex", CONVERSATION, ProjectorState(bound=True))
    assert receipts_of(run(bound, *refused)) == [()] * len(refused)
    # An explicit matching thread and an absent one are both this conversation's own call.
    assert receipts_of(run(Projector("codex", CONVERSATION, ProjectorState(bound=True)),
                           codex_send_call(envelope, thread=CONVERSATION), codex_send_call(envelope))) == [("m-1",)] * 2
    # A rollout that never named this conversation, or named another one, is not attributed to it.
    unbound = Projector("codex", CONVERSATION)
    other = {"type": "session_meta", "payload": {"id": "someone-else"}}
    assert receipts_of(run(unbound, codex_send_call(envelope), other, codex_send_call(envelope))) == [(), (), ()]


def test_a_codex_call_collects_unique_receipts_from_its_result_blocks_in_order():
    projector = Projector("codex", CONVERSATION, ProjectorState(bound=True))
    call = codex_send_call(send_envelope("m-1"), "not json", send_envelope("m-2"), send_envelope("m-1"), json.dumps({"status": "error"}))
    assert receipts_of(run(projector, call)) == [("m-1", "m-2")]


def test_receipts_per_record_are_bounded():
    many = [f"m-{index}" for index in range(MAX_RECEIPTS + 5)]
    (claude_result_entry,) = run(Projector("claude", CONVERSATION), claude_results(CONVERSATION, sent(*many)))
    (codex_result_entry,) = run(Projector("codex", CONVERSATION, ProjectorState(bound=True)),
                                codex_send_call(*(send_envelope(identity) for identity in many)))
    assert claude_result_entry.entry.receipts == codex_result_entry.entry.receipts == tuple(many[:MAX_RECEIPTS])


def test_a_send_notice_is_not_an_input_a_reply_a_turn_origin_or_user_work():
    def turn(*middle):
        projector = Projector("claude", CONVERSATION)
        results = run(projector, claude("user", "do it"), *middle,
                      claude("assistant", "Done.", message={"id": "msg-1", "stop_reason": "end_turn"}))
        return projector, completions(results)

    plain, plain_done = turn()
    sending, sending_done = turn(claude_results(CONVERSATION, sent("m-1")), claude_results(CONVERSATION, sent("m-2"), uuid_="r-2"))
    assert [(c.origin, c.user_work, c.key) for c in sending_done] == [(c.origin, c.user_work, c.key) for c in plain_done]
    assert (sending_done[0].origin, sending_done[0].user_work) == ("user", True)
    assert sending.state.dump() == plain.state.dump()

    # A turn only the application started stays work-free: sending messages is not a person steering it.
    stored = {"nudge-0001": "inbox_nudge"}
    projector = Projector("claude", CONVERSATION, resolver=receipts(stored))
    results = run(projector, claude("user", marker_text("nudge-0001", "inbox_nudge", "Inbox has work")),
                  claude_results(CONVERSATION, sent("m-1")),
                  claude("assistant", "Done.", message={"id": "msg-2", "stop_reason": "end_turn"}))
    assert [(c.origin, c.user_work) for c in completions(results)] == [("nudge", False)]

    # Codex: the receipt neither opens nor closes a turn and leaves a candidate and its work flag alone.
    codex = Projector("codex", CONVERSATION, ProjectorState(bound=True))
    run(codex, {"type": "event_msg", "payload": {"type": "user_message", "message": "go"}}, codex_send_call(send_envelope("m-1")))
    assert codex.state.dump() == {"turn_open": True, "turn_done": False, "candidate": None, "origin": "user", "work": True, "bound": True}
