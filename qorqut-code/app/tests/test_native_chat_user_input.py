"""``request_user_input`` results in derived native Chat.

A successful provider tool-result envelope of ``request_user_input`` is shown as an ordinary agent message whose ``references``
are resolved for the owner at read time. It is the transcript archive projected, nothing else: no stored request, no completed
reply, no user turn, no unread or completion counter. Assistant prose, quoted JSON, user text, failed results and any other shape
never project. The owner's references come from current records and the trusted browser configuration, never from the model.

Synthetic agents, records, surfaces and transcripts only."""

import json
from types import SimpleNamespace

import pytest

from chat_input_support import (TOOL, add_business, context, edit_agent, ordubashy, present, result_record, revise, shown, signoff,
                                task, wire, write_surfaces)
from host_foundation_support import create_worker, historical_business_agent
from native_chat_records import claude_result, claude_results, codex_send_call, send_envelope, text_content
from native_chat_support import now, rows
from qorqut_operations.contracts import new_id

BASE_KEYS = {"id", "kind", "speaker", "text", "timestamp", "position", "partial", "identity", "system", "completion_sequence",
             "completion_id"}
TASK_KEYS = {"type", "id", "business_id", "business_slug", "label", "state", "available"}
SIGNOFF_KEYS = {"type", "id", "business_id", "business_slug", "label", "available"}
BROWSER_KEYS = {"type", "id", "business_id", "label", "viewer_url", "available"}
AGENT_KEYS = {"type", "id", "business_id", "label", "role", "presentation", "available"}
UNAVAILABLE_KEYS = {"type", "id", "business_id", "label", "available"}
NOBODY = {"completion_sequence": 0, "work_sequence": 0, "user_sequence": 0}


def counters(native):
    (row,) = rows(native.runtime, "SELECT completion_sequence,work_sequence,user_sequence FROM native_agent_counters WHERE agent_id=?",
                  (native.agent,))
    return row


def blocks(native, *texts, **options):
    """One native record holding every text as a separate tool result: one block each (Claude) or one call's blocks (Codex)."""
    if native.provider == "claude":
        return claude_results(native.session_id, *(claude_result(text_content(text), **options) for text in texts),
                              uuid_=new_id(), cwd=str(native.workspace))
    return codex_send_call(*texts, thread=native.session_id, tool=TOOL, item_id="call_" + new_id(), **options)


def reply(native, text, key):
    if native.provider == "claude":
        return [native.claude_assistant(text, message_id="msg-" + key)]
    return [native.codex_assistant(text, final=True, native_id="item-" + key), native.codex_event("task_complete")]


def world(native):
    """Real records and a surface registry; ids of a business task, a Global task, an input Signoff and a second business."""
    first, second = native.instance.business, add_business(native, "second")
    write_surfaces(native, business=second, agents={native.agent: ["ordubashy-browser"]})
    return SimpleNamespace(first=first, second=second, task=task(native, "Host task", first), global_task=task(native, "Global task"),
                           other_task=task(native, "Second task", second), signoff=signoff(native, first))


def mixed(w):
    return [{"type": "task", "id": w.task, "business_id": w.first}, {"type": "task", "id": w.global_task},
            {"type": "signoff", "id": w.signoff, "business_id": w.first}, {"type": "browser", "id": "ordubashy-browser"},
            {"type": "browser", "id": "outlook-browser"},
            {"type": "browser", "id": "launch-browser", "business_id": w.second}]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_successful_result_is_one_ordinary_agent_message_with_resolved_references(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    native.client_user_turn("Show me what you found")
    native.write(result_record(native, wire(present(native, "Here is the state of things.", mixed(w)))))
    native.pass_()

    user, agent = state = shown(native)
    assert [e["speaker"] for e in state] == ["user", "agent"]
    assert set(user) == BASE_KEYS and set(agent) == BASE_KEYS | {"references"}
    assert (agent["kind"], agent["text"], agent["partial"], agent["system"], agent["completion_sequence"], agent["completion_id"]) == (
        "message", "Here is the state of things.", False, None, None, None)
    assert user["position"] < agent["position"] and len({e["id"] for e in state}) == 2 and "agent_message" not in agent
    task_view, global_view, signoff_view, own, outlook, business = agent["references"]
    assert task_view == {"type": "task", "id": w.task, "business_id": w.first, "business_slug": "first", "label": "Host task",
                         "state": "open", "available": True}
    assert global_view == {"type": "task", "id": w.global_task, "business_id": None, "business_slug": None, "label": "Global task",
                           "state": "open", "available": True}
    assert signoff_view == {"type": "signoff", "id": w.signoff, "business_id": w.first, "business_slug": "first", "label": "Pick one",
                            "available": True}
    assert own == {"type": "browser", "id": "ordubashy-browser", "business_id": None, "label": "Ordubashy browser",
                   "viewer_url": "https://desktop.example.test/ordubashy", "width": 1440, "height": 900, "available": True}
    assert outlook == {"type": "browser", "id": "outlook-browser", "business_id": None, "label": "Outlook browser",
                         "viewer_url": "https://desktop.example.test/outlook", "available": True}
    assert business == {"type": "browser", "id": "launch-browser", "business_id": w.second, "label": "Launch browser",
                        "viewer_url": "https://desktop.example.test/launch", "available": True}
    assert [set(r) for r in agent["references"]] == [TASK_KEYS, TASK_KEYS, SIGNOFF_KEYS, BROWSER_KEYS | {"width", "height"},
                                                     BROWSER_KEYS, BROWSER_KEYS]

    # Not a reply, a user turn, unread work or an outgoing agent message: the transcript is the only source.
    assert counters(native) == NOBODY and rows(native.runtime, "SELECT * FROM native_replies") == []
    page = native.chat.state(native.owner, native.agent)
    assert page["rendered_boundary"] == 0 and page["entries"] == state
    assert not [e for e in state if e["speaker"] == "notice"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_ids_references_and_order_are_stable_across_pages_restarts_and_repeated_reads(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    native.client_user_turn("one")
    native.write(result_record(native, wire(present(native, "First", [{"type": "task", "id": w.task, "business_id": w.first}]))))
    native.client_user_turn("two")
    native.write(result_record(native, wire(present(native, "Second", [{"type": "browser", "id": "outlook-browser"}]))))
    native.pass_()
    whole = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in whole["entries"]] == ["user", "agent", "user", "agent"]
    assert native.chat.state(native.owner, native.agent)["entries"] == whole["entries"]            # repeated reads: same entries

    page = native.chat.state(native.owner, native.agent, limit=2)
    older = native.chat.history(native.owner, native.agent, before=page["older"], limit=2)
    assert older["older"] is None and older["entries"] + page["entries"] == whole["entries"]
    assert [e.get("references") is not None for e in page["entries"]] == [False, True]
    single = native.chat.history(native.owner, native.agent, before=whole["entries"][3]["position"], limit=1)
    assert single["entries"] == [whole["entries"][2]]

    native.end()
    native.pass_(3)
    native.new_session(same_conversation=True, pid=9001)
    native.pass_(3)
    native.build()
    native.pass_(2)
    resumed = native.chat.state(native.owner, native.agent)
    assert resumed["entries"] == whole["entries"]

    before = resumed["revision"]
    assert native.chat.events(native.owner, native.agent, before) == ([], before)
    native.write(result_record(native, wire(present(native, "Third", [{"type": "task", "id": w.global_task}]))))
    native.pass_()
    events, latest = native.chat.events(native.owner, native.agent, before)
    assert latest > before and [e["kind"] for e in events] == ["changed"]
    after = native.chat.state(native.owner, native.agent)
    assert after["entries"][:4] == whole["entries"] and after["entries"][4]["text"] == "Third"
    assert counters(native) == NOBODY


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_several_results_in_one_record_each_project_and_never_split_across_pages(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    worker = create_worker(native.instance, "atlas")
    with native.runtime.database.transaction() as uow:
        event = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)",
            (None, "agent_messages", new_id(), native.agent, "direct_message", "Synthetic", now())).lastrowid
        message = new_id()
        uow.connection.execute(
            "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,thread_id,body,created_at,current_sender_id,"
            "current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (message, event, None, native.agent, worker, message, "Private body", now(), native.agent, worker))
    native.client_user_turn("go")
    first = wire(present(native, "Alpha", [{"type": "task", "id": w.task, "business_id": w.first}]))
    second = wire(present(native, "Beta", [{"type": "signoff", "id": w.signoff, "business_id": w.first}]))
    third = wire(present(native, "Gamma"))
    sent = send_envelope(message, "recipient-0001")
    ordinary = "ordinary tool text, not an envelope"
    if provider == "claude":
        # One user record can carry the send receipt and three presentations: the notice first, then each message in block order.
        native.write(blocks(native, sent, first, ordinary, second, third))
        groups = [["go"], ["", "Alpha", "Beta", "Gamma"], ["Delta"]]
    else:
        # Codex writes one record per call: the receipt call, then one presentation call with several result blocks.
        native.write(codex_send_call(sent, thread=native.session_id, item_id="call_" + new_id()))
        native.write(blocks(native, first, ordinary, second, third))
        groups = [["go"], [""], ["Alpha", "Beta", "Gamma"], ["Delta"]]
    native.write(result_record(native, wire(present(native, "Delta"))))
    native.pass_()

    state = shown(native)
    assert [e["text"] for e in state] == [text for group in groups for text in group]
    assert [e["speaker"] for e in state] == ["user", "notice", "agent", "agent", "agent", "agent"]
    notice = state[1]
    assert notice["agent_message"]["message_ids"] == [message] and "references" not in notice
    assert [e["references"][0]["type"] for e in state[2:4]] == ["task", "signoff"] and state[4]["references"] == []
    assert len({e["id"] for e in state}) == 6 and [e["position"] for e in state] == sorted(e["position"] for e in state)
    assert counters(native) == NOBODY

    # Paging one entry at a time returns each record whole, so no presentation is dropped or repeated at a page edge.
    pages, page = [], native.chat.state(native.owner, native.agent, limit=1)
    while True:
        pages.insert(0, page["entries"])
        if page["older"] is None:
            break
        page = native.chat.history(native.owner, native.agent, before=page["older"], limit=1)
    assert [[e["text"] for e in entries] for entries in pages] == groups
    assert [e for entries in pages for e in entries] == state


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_presentation_between_a_user_turn_and_its_reply_is_not_the_reply_and_not_unread(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    native.client_user_turn("Please check")
    native.write(result_record(native, wire(present(native, "Looking.", [{"type": "task", "id": w.task, "business_id": w.first}]))))
    native.write(*reply(native, "Checked it.", "1"))
    native.pass_()
    assert counters(native) == {"completion_sequence": 1, "work_sequence": 1, "user_sequence": 0}
    (completed,) = rows(native.runtime, "SELECT origin,user_work FROM native_replies")
    assert (completed["origin"], completed["user_work"]) == ("user", 1)
    state = shown(native)
    assert [e["speaker"] for e in state] == ["user", "agent", "agent"]
    assert [e["completion_sequence"] for e in state] == [None, None, 1] and "references" in state[1] and "references" not in state[2]
    assert native.chat.state(native.owner, native.agent)["rendered_boundary"] == 1

    # Presentations alone, with no turn the owner started, leave an idle agent with nothing unread.
    other = ordubashy(tmp_path / "idle", provider)
    other.write(result_record(other, wire(present(other, "Just so you know."))))
    other.pass_()
    assert counters(other) == NOBODY and other.chat.state(other.owner, other.agent)["rendered_boundary"] == 0
    assert [e["speaker"] for e in shown(other)] == ["agent"]


def test_only_a_successful_provider_tool_result_of_the_exact_shape_projects_in_claude(tmp_path):
    native = ordubashy(tmp_path, "claude")
    w = world(native)
    good = json.loads(wire(present(native, "Real", [{"type": "task", "id": w.task, "business_id": w.first}])))
    text = json.dumps(good, separators=(",", ":"))

    def altered(**changes):
        value = json.loads(text)
        for path, new in changes.items():
            target = value
            *parents, last = path.split(".")
            for part in parents:
                target = target[part]
            if new is KeyError:
                del target[last]
            else:
                target[last] = new
        return json.dumps(value, separators=(",", ":"))

    bad_texts = [
        json.dumps(text),                                                                  # quoted JSON
        "Result: " + text, text + "\n(done)", "```json\n" + text + "\n```",                # prose around the envelope
        altered(**{"status": "error"}), altered(**{"data.extra": 1}),
        altered(**{"data.user_input.version": 2}), altered(**{"data.user_input.extra": 1}),
        altered(**{"data.user_input.message": " padded "}), altered(**{"data.user_input.message": ""}),
        altered(**{"data.user_input.references": [{"type": "browser", "id": "https://viewer.example.test/x", "business_id": None}]}),
        altered(**{"data.user_input.references": [{"type": "task", "id": w.task, "business_id": w.first, "label": "Injected"}]}),
        altered(**{"data.user_input.references": [{"type": "task", "id": w.task, "business_id": w.first}] * 2}),
        altered(**{"data.user_input.references": [{"type": "task", "id": w.task, "business_id": w.first}] * 9}),
        altered(**{"data.user_input.message": "x" * 4001}), json.dumps({"user_input": good["data"]["user_input"]}), "[" + text + "]",
    ]
    native.client_user_turn("hello")
    native.write(native.claude_assistant("Here is what I will send: " + text, message_id="prose"),              # assistant prose
                 native.claude_user(text),                                                                       # user text
                 claude_results(native.session_id, claude_result(text_content(text), is_error=True), uuid_=new_id()))   # a failed result
    native.write(*(claude_results(native.session_id, claude_result(text_content(bad)), uuid_=new_id()) for bad in bad_texts))
    native.pass_()
    state = shown(native)
    # Only the ordinary entries exist: the user's turns, and the assistant's own prose text.
    assert {e["speaker"] for e in state} <= {"user", "agent"}
    assert not [e for e in state if "references" in e or e["speaker"] == "notice"], state
    assert [e["text"] for e in state if e["speaker"] == "agent"] == ["Here is what I will send: " + text]
    assert counters(native)["completion_sequence"] <= 1

    # The genuine result in the very same transcript still projects once.
    native.write(claude_results(native.session_id, claude_result(text_content("prose before", text)), uuid_=new_id()))   # a block per text
    native.pass_()
    projected = [e for e in shown(native) if "references" in e]
    assert [e["text"] for e in projected] == ["Real"] and projected[0]["references"][0]["label"] == "Host task"
    # The same envelope as a plain-string tool result (a client that does not wrap it in text blocks) is the same message kind.
    native.write(claude_results(native.session_id, claude_result(text, is_error=False), uuid_=new_id()))
    native.pass_()
    assert [e["text"] for e in shown(native) if "references" in e] == ["Real", "Real"]


def test_only_a_completed_bazarlyk_request_user_input_call_projects_in_codex(tmp_path):
    native = ordubashy(tmp_path, "codex")
    w = world(native)
    text = wire(present(native, "Real", [{"type": "task", "id": w.task, "business_id": w.first}]))

    def call(**options):
        return codex_send_call(text, thread=native.session_id, tool=options.pop("tool", TOOL), item_id="call_" + new_id(), **options)

    native.client_user_turn("hello")
    native.write(call(tool="agents_message_send"), call(tool="request_user_input_extra"), call(server="other"), call(server=None),
                 call(status="failed"), call(status="inProgress"), call(is_error=True), call(error="boom"),
                 native.codex_assistant("I will send " + text, final=True, native_id="prose"),
                 native.codex_event("agent_message", message=text),
                 {"type": "response_item", "timestamp": now(), "payload": {"type": "custom_tool_call_output", "call_id": "call_x",
                                                                           "output": text_content(text)}},
                 native.codex_event("user_message", message=text))
    native.pass_()
    state = shown(native)
    assert not [e for e in state if "references" in e], state
    native.write(call())
    native.write(call())                                                                  # a repeated call is a second message
    native.pass_()
    projected = [e for e in shown(native) if "references" in e]
    assert [e["text"] for e in projected] == ["Real", "Real"] and projected[0]["id"] != projected[1]["id"]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_references_are_resolved_for_the_owner_at_read_time_and_unavailable_ones_reveal_nothing(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    calls = []
    original = native.runtime.user_input.owner_references

    def counted(principal_id, found, uow):
        calls.append(sorted(found))
        return original(principal_id, found, uow)
    native.runtime.user_input.owner_references = counted

    native.write(result_record(native, wire(present(native, "Live", mixed(w)))))
    forged = {"status": "ok", "data": {"user_input": {"version": 1, "message": "Forged ids", "references": [
        {"type": "task", "id": new_id(), "business_id": None}, {"type": "signoff", "id": new_id(), "business_id": w.first},
        {"type": "browser", "id": "no-such-browser", "business_id": None},
        {"type": "browser", "id": "business-sandbox-browser", "business_id": w.first}]}}}
    native.write(result_record(native, json.dumps(forged, separators=(",", ":"))))
    native.pass_()
    live, fake = shown(native)
    assert len(calls) == 1 and len(set(calls[0])) == 10                                      # one lookup for the page, not one per reference
    assert all(r["available"] for r in live["references"])
    assert [set(r) for r in fake["references"]] == [UNAVAILABLE_KEYS] * 4
    assert [(r["type"], r["label"], r["available"]) for r in fake["references"]] == [
        ("task", "Unavailable task", False), ("signoff", "Unavailable signoff", False), ("browser", "Unavailable browser", False),
        ("browser", "Unavailable browser", False)]
    assert [r["id"] for r in fake["references"]][2:] == ["no-such-browser", "business-sandbox-browser"]
    assert "viewer" not in json.dumps(fake) and "https://" not in json.dumps(fake)

    # The label and state are the current ones; an archived business and a removed registry make the same entries unavailable.
    revise(native, w.task, w.first, title="Renamed task")
    revise(native, w.global_task, None, done=True)
    renamed = shown(native)[0]["references"][0]
    assert renamed["label"] == "Renamed task" and renamed["available"] is True
    assert shown(native)[0]["references"][1]["state"] == "done"
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-17T00:00:00Z' WHERE id=?", (w.second,))
    (native.runtime.paths.runtime / "browser-surfaces.json").unlink()
    gone = shown(native)[0]["references"]
    assert [r["available"] for r in gone] == [True, True, True, False, False, False]
    assert all(set(r) == UNAVAILABLE_KEYS for r in gone[3:]) and gone[2]["label"] == "Pick one"
    assert counters(native) == NOBODY


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_agent_references_show_the_current_registered_identity_and_nothing_private(tmp_path, provider):
    native = ordubashy(tmp_path, provider)
    w = world(native)
    atlas = create_worker(native.instance, "atlas")
    historical = historical_business_agent(native.instance, w.first)
    edit_agent(native, atlas, presentation={"icon": "rocket"})
    native.write(result_record(native, wire(present(native, "The team", [
        {"type": "agent", "id": atlas}, {"type": "task", "id": w.global_task}, {"type": "agent", "id": native.agent}]))))
    native.pass_()
    (agent,) = shown(native)
    worker, global_task, self_view = agent["references"]
    assert set(worker) == AGENT_KEYS and set(self_view) >= {"type", "id", "business_id", "label", "available"}
    assert (worker["type"], worker["id"], worker["business_id"], worker["label"], worker["available"]) == (
        "agent", atlas, None, "Atlas", True)
    assert worker["role"] == {"id": "global-worker"} and worker["presentation"]["icon"] == "rocket"
    assert set(worker["role"]) == {"id"} and set(worker["presentation"]) <= {"icon"}
    assert global_task["type"] == "task" and (self_view["id"], self_view["business_id"], self_view["available"]) == (native.agent, None, True)
    # The projection is the one outgoing-message markers use: the same registered identity, no profile, path, grant or markup.
    text = json.dumps(worker)
    for private in ("scope", "tool", "workspace", "assignment", "runtime", "plugin", "capabilit", "http", "<", "Synthetic"):
        assert private not in text

    # The label is the current display name, not a stored copy.
    edit_agent(native, atlas, display_name="Atlas Prime")
    assert shown(native)[0]["references"][0]["label"] == "Atlas Prime"

    # Forged ids, a human, a business-home agent and an agent with a business_id never name anyone: the first three are shown as
    # identity-only unavailable placeholders and the last is not a presentation at all.
    forged = {"status": "ok", "data": {"user_input": {"version": 1, "message": "Forged agents", "references": [
        {"type": "agent", "id": new_id(), "business_id": None}, {"type": "agent", "id": native.owner, "business_id": None},
        {"type": "agent", "id": historical, "business_id": None}]}}}
    scoped = {"status": "ok", "data": {"user_input": {"version": 1, "message": "Scoped agent", "references": [
        {"type": "agent", "id": atlas, "business_id": w.first}]}}}
    native.write(result_record(native, json.dumps(forged, separators=(",", ":"))))
    native.write(result_record(native, json.dumps(scoped, separators=(",", ":"))))
    native.pass_()
    entries = shown(native)
    assert [e["text"] for e in entries] == ["The team", "Forged agents"]
    unavailable = entries[1]["references"]
    assert [set(r) for r in unavailable] == [UNAVAILABLE_KEYS] * 3
    assert [(r["type"], r["business_id"], r["label"], r["available"]) for r in unavailable] == [("agent", None, "Unavailable agent", False)] * 3
    assert [r["id"] for r in unavailable] == [e["id"] for e in forged["data"]["user_input"]["references"]]
    assert "Onbashy" not in json.dumps(entries[1]) and "role" not in json.dumps(unavailable) and "presentation" not in json.dumps(unavailable)
    assert counters(native) == NOBODY


def test_a_sandboxed_business_browser_is_shown_only_through_its_ready_viewer(tmp_path, monkeypatch):
    from bazarlyk.browser.targets import BrowserTargets
    from bazarlyk.sandbox.state import SandboxStateStore
    native = ordubashy(tmp_path, "claude")
    boxed = add_business(native, "boxed")
    with native.runtime.database.transaction() as uow:
        SandboxStateStore().insert_intent(uow, boxed)
    viewer = SimpleNamespace(config_loader=lambda paths: SimpleNamespace(viewer_port=18080),
                             link=lambda business_id: f"https://sandbox.example.test/viewer/{business_id}")
    monkeypatch.setattr(native.runtime, "previews", SimpleNamespace(browser_viewers=viewer), raising=False)
    reference = {"type": "browser", "id": "business-sandbox-browser", "business_id": boxed}
    pending = present(native, "Sandbox browser", [reference])
    assert pending.status == "ok" and "sandbox.example.test" not in wire(pending)            # the model-visible result never has the URL
    native.write(result_record(native, wire(pending)))
    native.pass_()
    (not_ready,) = shown(native)[0]["references"]
    assert not_ready == {"type": "browser", "id": "business-sandbox-browser", "business_id": boxed, "label": "Unavailable browser",
                         "available": False}
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE business_sandboxes SET state='ready', observed_generation=desired_generation, "
                               "environment_id='synthetic-environment' WHERE business_id=?", (boxed,))
    (ready,) = shown(native)[0]["references"]
    assert ready == {"type": "browser", "id": "business-sandbox-browser", "business_id": boxed, "label": "Boxed",
                     "viewer_url": f"https://sandbox.example.test/viewer/{boxed}", "available": True}
    assert isinstance(native.runtime.browser_access.targets, BrowserTargets)
