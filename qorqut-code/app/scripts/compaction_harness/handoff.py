"""The compaction handoff under test: the request sent to the agent, and the context a fresh conversation starts with.

The agent files the handoff instead of writing it as text. For every owner message it gives a status, a note, the Task
that holds the work and that Task's next step; it fills in a fixed form on where it stopped; and it says which worker
holds what. Filing is what writes the records: each note and next step is added to the Task named, so the pointer the
next conversation gets is a record that was written, never one the agent typed from memory.

The host adds four things the agent does not write: the owner's exact words next to the agent's status for each
message, the list of the other Tasks the filing wrote a note on with each one's next step, the rules the owner stated
(which the agent proposed as instruction changes and the owner has not decided yet), and the last few messages of the
closed conversation word for word. A message stays listed until a handoff marks it done; the conversation after that
one no longer gets it.

In the replay a model on the agent's model reads the recorded segment and produces the filing the agent would have
made. Nothing is written to real records: the filing's writes are kept in memory as a stand-in for them.
"""
import re

from scripts.compaction_harness import model, transcripts

MESSAGE_CAP = 3_000
STATUSES = ("done", "partly", "remaining")
OPEN = ("partly", "remaining")
STOPPED = (("task", "Task"), ("doing", "Step in progress"), ("last_action", "Last action and its result"),
           ("next_action", "Next action"), ("waiting_on", "Waiting on"))
TAIL_EVENTS, TAIL_RESULT, TAIL_CAP = 12, 1_000, 8_000
START = {"entries": {}, "listed": [], "records": {}, "rules": [], "handoff": {}, "missed": [], "unfiled": [],
         "filed": [], "tail": ""}

ROLE = ("You are {agent}, an agent in the Qorqut system that works for one owner. You are being shown your own "
        "conversation as a transcript. OWNER lines are the owner's messages to you, YOU lines are what you said, "
        "TOOL CALL and TOOL RESULT lines are your tool use, with long results cut short.")

REQUEST = """Your context is nearly full. Stop the work you are doing. This conversation will be closed and a fresh one \
started for you: same instructions, same tools, same access. It gets your instructions, then the owner's messages \
listed below in their exact words, then the handoff you write now. It can read every record you can. Nothing that \
exists only in this conversation will reach it.

File the handoff now. Filing writes the records for you: each note and next step you give is added to the Task you \
name, and the next conversation is pointed at exactly those Tasks. Write from memory, without looking anything up. Do \
not message the owner, send anything outside, or continue the work.

Reply with one JSON object with these keys:
- "messages": one entry for each owner message listed below: {{"id", "status", "note", "task", "next_step"}}. Status is \
done, partly or remaining. "note" is one or two lines on what was done and what remains. Write the note afresh for a \
message that was already open when this conversation started: say what changed since and what stands now, and say so \
if a later message or decision replaced what it asked. "task" is the Task that holds this work, by the title and id \
used in the conversation; an open message must have one. "next_step" is what happens next on that Task and who does \
it, or "" when the message is done.
- "tasks": for every other Task you worked on in this conversation whose record does not yet show what you found, \
decided, promised or assigned: {{"task", "note", "next_step"}}.
- "stopped": where you stopped, each value one line, "none" where it does not apply: {{"task": the Task you were \
working on when stopped, "doing": the step that was in progress, "last_action": the last call you made, its target \
and its result, with the error text if it failed, "next_action": the one next call or step with its exact target \
(record id, revision, worker, wording), "waiting_on": who or what must happen before the next action, "also_open": \
other things you started in this conversation and did not finish, as a list of {{"task", "pending"}}, at most five}}.
- "rules": where the owner stated a rule or preference that should keep applying and it is not yet among your proposed \
instruction changes: {{"owner_message": its id, "rule": the proposed instruction}}. Only what the owner said, not how \
tools behave.
- "workers": one paragraph. Which worker holds which assignment from you now, the state of each, what you are waiting \
for from each, and the Task for each.
- "not_recorded": anything that belongs on a record that is not a Task, or that no record can hold, and why. Normally \
empty.

Leave out your role and rules, how tools work, and the owner's words.

Owner messages to give a status: {ids}"""


def _text(value):
    return value if isinstance(value, str) else "; ".join(str(part) for part in value or [])


def owner_lines(owner, entries=None):
    """The owner's messages, exact words and oldest first, each with the agent's status for it where one is given."""
    lines = []
    for item in owner:
        text = item["text"] if len(item["text"]) <= MESSAGE_CAP else item["text"][:MESSAGE_CAP] + " …[cut]"
        lines.append(f"[{item['id']} {item['at']}] {text}")
        if item["reply"]:
            lines.append(f"    you replied: {item['reply']}")
        entry = (entries or {}).get(item["id"])
        if entry:
            lines.append(f"    {entry['status'].upper()}: {entry['note']}"
                         + (f" Read: {entry['records']}" if entry["records"] else ""))
    return "\n".join(lines)


def stopped(form):
    """The "where you stopped" form as labelled lines; "Recorded at" comes from the filing, not from the agent."""
    if not isinstance(form, dict):
        return _text(form)
    lines = [f"{label}: {_text(form.get(key)) or 'none'}" for key, label in STOPPED]
    lines.append(f"Recorded at: {form.get('recorded_at') or 'no record'}")
    pending = [f"{item.get('task')}: {item.get('pending')}" for item in form.get("also_open") or []
               if isinstance(item, dict)]
    return "\n".join(lines + [f"Also started and not finished: {'; '.join(pending) or 'none'}"])


def tail(segment):
    """The last messages of a conversation word for word, as the host would paste them. No model writes this."""
    lines = []
    for kind, text, _ in [event for event in segment.events if event[0] != "notice"][-TAIL_EVENTS:]:
        if kind == "result" and len(text) > TAIL_RESULT:
            text = text[:TAIL_RESULT] + " …[cut]"
        lines.append(f"{ {'owner': 'OWNER', 'agent': 'YOU', 'call': 'TOOL CALL', 'result': 'TOOL RESULT'}[kind] }: {text}")
    return "\n".join(lines)[-TAIL_CAP:]


def wake_context(owner, state):
    """What a fresh conversation gets after its instructions."""
    listed = [item for item in owner if item["id"] in state["listed"]]
    parts = []
    if state["rules"]:
        parts.append("RULES THE OWNER STATED TO YOU. They are in force: follow them. You proposed each as a change to your "
                     "written instructions, and only that writing-down still waits for the owner's decision.\n"
                     + "\n".join(f"- {rule}" for rule in state["rules"]))
    if listed:
        parts.append("THE OWNER'S MESSAGES TO YOU (exact words) AND WHERE EACH STANDS, BY YOUR HANDOFF\n"
                     + owner_lines(listed, state["entries"]))
    if state["handoff"].get("stopped_at"):
        parts.append("WHERE YOU STOPPED\n" + stopped(state["handoff"]["stopped_at"]))
    if state.get("filed"):
        parts.append("OTHER TASKS YOU WROTE A NOTE ON WHEN YOU STOPPED (the note is on each Task)\n" + "\n".join(
            f"- {item['task']}" + (f". Next step: {item['next_step']}" if item["next_step"] else "")
            for item in state["filed"]))
    for key, title in (("workers", "YOUR WORKERS"), ("not_recorded", "NOT ON ANY RECORD")):
        if state["handoff"].get(key):
            parts.append(f"{title}\n{_text(state['handoff'][key])}")
    if state["tail"]:
        parts.append("THE LAST MESSAGES OF THE CLOSED CONVERSATION, WORD FOR WORD\n" + state["tail"])
    return "\n\n".join(parts)


def records_found(state, context):
    """The reconciliation writes on the records that the wake-up context points to."""
    lowered, found = context.lower(), []
    for name, writes in state["records"].items():
        ids = re.findall(r"[0-9a-f]{8}", name.lower())
        if any(part in lowered for part in ids) or (not ids and name.lower() in lowered):
            found.append(name + "\n" + "\n".join(f"  - {write}" for write in writes))
    return "\n".join(found)


def write(agent, segment, owner_before, state):
    """One compaction: the state after it and the cost. ``state`` is not changed."""
    carried = [item for item in owner_before if state["entries"].get(item["id"], {}).get("status") in OPEN]
    listed = carried + segment.owner
    started = wake_context(owner_before, state)
    asked = owner_lines([item for item in carried if item["id"] not in state["listed"]])
    prompt = "\n\n".join(part for part in (
        started and "WHAT YOU STARTED THIS CONVERSATION WITH\n" + started,
        asked and "EARLIER OWNER MESSAGES STILL OPEN\n" + asked,
        "THE CONVERSATION SINCE\n" + transcripts.render(segment),
        REQUEST.format(ids=", ".join(item["id"] for item in listed) or "none"),
    ) if part)
    reply, cost = model.ask_json(ROLE.format(agent=agent), prompt)
    if not isinstance(reply, dict):
        raise model.ModelError("handoff is not an object")
    records = {name: list(writes) for name, writes in state["records"].items()}

    def add(task, text):
        """One write to a Task. Returns the Task as the pointer, or "" when no Task was named."""
        task = _text(task).strip()
        if not task or task.lower().startswith("none") or not text:
            return ""
        records.setdefault(task, []).append(text)
        return task

    def step(item):
        return f" Next step: {_text(item.get('next_step'))}" if item.get("next_step") else ""

    entries, given = dict(state["entries"]), set()
    for entry in reply.get("messages") or []:
        if isinstance(entry, dict) and entry.get("status") in STATUSES:
            key, note = str(entry.get("id")), _text(entry.get("note"))
            given.add(key)
            entries[key] = {"status": entry["status"], "note": note, "records": add(
                entry.get("task"), f"Owner message {key}, {entry['status']}: {note}{step(entry)}")}
    missed = [item["id"] for item in listed if item["id"] not in given]        # a host would send the filing back
    for key in missed:
        entries.setdefault(key, {"status": "remaining", "note": "(the handoff gave this message no status)",
                                 "records": ""})
    filed = []
    for item in reply.get("tasks") or []:
        if isinstance(item, dict) and add(item.get("task"), f"{_text(item.get('note'))}{step(item)}"):
            filed.append({"task": _text(item.get("task")).strip(), "next_step": _text(item.get("next_step"))})
    form = reply.get("stopped") if isinstance(reply.get("stopped"), dict) else {}
    form["recorded_at"] = add(form.get("task"), "Where the last conversation stopped. " + " ".join(
        f"{label}: {_text(form.get(key))}." for key, label in STOPPED[1:] if form.get(key)))
    for item in form.get("also_open") or []:
        if isinstance(item, dict):
            add(item.get("task"), f"Started and not finished: {_text(item.get('pending'))}")
    return {"cost": cost, "state": {
        "entries": entries, "listed": [item["id"] for item in listed], "records": records,
        "rules": state["rules"] + [rule["rule"] if isinstance(rule, dict) and rule.get("rule") else _text(rule)
                                   for rule in reply.get("rules") or []],
        "handoff": {"stopped_at": form, "workers": reply.get("workers"), "not_recorded": reply.get("not_recorded")},
        "missed": missed, "filed": filed, "tail": tail(segment),
        "unfiled": [key for key in given if entries[key]["status"] in OPEN and not entries[key]["records"]]}}
