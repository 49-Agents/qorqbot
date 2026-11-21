"""The coordinator's own compaction: the handoff it files when its conversation is nearly full, and what the next
conversation starts with.

A conversation that nears its limit is closed and a fresh one opened. The fresh one gets the agent's instructions as
always, and nothing of the old conversation is summarised for it. The agent files a handoff first
(``compaction_handoff_file``): for every message the owner sent it, a status (done, partly, remaining), a short note, the
Task that holds the work and that Task's next step; for any other Task it worked on, a note and next step; a fixed form
on where it stopped; and a paragraph on its workers. Filing writes the records: each note and next step goes onto the
Task named, through the Task service's own write and in the same transaction. What the next conversation is pointed at
is therefore a record that was written, never one typed from memory.

``opening`` is what the fresh conversation starts with after its instructions: the owner's messages in their exact
words, each with the filed status and the Task to read; where the agent stopped; the other Tasks it wrote on; its
workers; and the rules the owner stated that the agent proposed as instruction changes. A message is listed again in
every handoff until one marks it done.

One row of ``compactions`` per compaction is the log. The row waiting to be opened is the one a filing fills, so filing
again before the fresh conversation starts replaces the earlier filing. For the coordinator only.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, apply_records, ref, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.types import LINE_LIMIT

from qorqut.composition.compactions import TABLE
from qorqut.management.identity import configured_ordubashy

NAME = "compaction_handoff_file"
STATUSES = ("done", "partly", "remaining")
OPEN = ("partly", "remaining")
STOPPED = (("doing", "Step in progress"), ("last_action", "Last action and its result"), ("next_action", "Next action"),
           ("waiting_on", "Waiting on"))
FIRST_PERIOD = timedelta(hours=24)      # what a first handoff, with none before it, answers for
MAX_MESSAGES = 100                      # per channel and period; older ones of a longer period are not listed
MESSAGE_CAP = 3000                      # characters of one owner message in the opening; the rest is read by its id
PREVIEW = 200
WORKER_LINES = 40

HANDLE = {"type": "string", "pattern": "^m[1-9][0-9]{0,3}$"}
TASK = {"type": "string", "minLength": 1, "maxLength": 100}
NOTE = {"type": "string", "minLength": 1, "maxLength": 2000}
LINE = {"type": "string", "minLength": 1, "maxLength": 600}
STEP = {"type": "string", "maxLength": LINE_LIMIT}
GET_SCHEMA = schema({"scope": {"const": "global"}})
FILE_SCHEMA = schema({
    "scope": {"const": "global"},
    "messages": {"type": "array", "maxItems": 4 * MAX_MESSAGES, "items": schema(
        {"id": HANDLE, "status": {"enum": list(STATUSES)}, "note": NOTE, "task_id": TASK, "next_step": STEP},
        ("id", "status", "note"))},
    "tasks": {"type": "array", "maxItems": 20, "items": schema({"task_id": TASK, "note": NOTE, "next_step": STEP},
                                                               ("task_id", "note"))},
    "stopped": schema({"task_id": TASK, **{key: LINE for key, _ in STOPPED},
                       "also_open": {"type": "array", "maxItems": 5, "items": schema({"task_id": TASK, "pending": LINE},
                                                                                     ("task_id", "pending"))}},
                      tuple(key for key, _ in STOPPED)),
    "workers": {"type": "string", "maxLength": 3000},
    "not_recorded": {"type": "string", "maxLength": 2000},
}, ("messages", "stopped", "workers"))

GET_DESCRIPTION = (
    "Coordinator only. The form of your next handoff: the owner's messages it must give a status, oldest first, each with its "
    "id (m1, m2, ...), time, where it was written, the start of its text and, where your last handoff left it open or gave it "
    "no status, what that handoff said. Changes nothing.")
FILE_DESCRIPTION = (
    "Coordinator only. File your handoff when you are asked to because your conversation is nearly full: the conversation is "
    "then closed and a fresh one opened for you from what you file. Write from memory, without looking anything up. "
    "messages: one entry for every listed owner message (compaction_handoff_get lists them): id, status (done, partly or "
    "remaining), note (one or two lines on what was done and what remains, written afresh for a message that was already "
    "open), task_id (the Task that holds the work; needed unless done; create the Task first if none holds it) and "
    "next_step (what happens next on that Task and who does it). tasks: every other Task you worked on whose record does "
    "not yet show what you found, decided, promised or assigned: task_id, note, next_step. stopped: where you stopped, one "
    "line each: task_id (the Task you were on; leave out if none), doing (the step in progress), last_action (the last call, "
    "its target and its result, with the error text if it failed), next_action (the one next call or step with its exact "
    "target), waiting_on (who or what must happen first), also_open (up to five other things started and not finished: "
    "task_id, pending). workers: one paragraph on which worker holds which Task from you, the state of each and what you "
    "wait for from each. not_recorded: only what no record can hold. Filing writes each note onto its Task and sets its "
    "next step, so the next conversation is pointed at records that exist; do not write those notes yourself as well. It is "
    "refused, saying why, when a listed message has no entry, an open message names no Task or a Task is not found: fix "
    "that and file again. A rule the owner stated that should keep applying is not filed here: propose it with "
    "agentic_change_propose before you file.")
ASK = """Your context is nearly full. Stop the work you are doing now: do not start another step, message the owner or send \
anything outside. This conversation will be closed and a fresh one opened for you with the same instructions, tools and \
access. It gets your instructions, then the owner's messages listed below in their exact words, then the handoff you file \
now. Nothing else of this conversation reaches it.

First, for each rule or preference the owner stated in this conversation that should keep applying and that you have not \
proposed yet, call agentic_change_propose.

Then call compaction_handoff_file once, from memory, without looking anything up. It writes every note and next step you \
give onto the Task you name, so name the Task that really holds the work, and create one first where an open message has \
none. After it answers that the handoff is filed, stop.

The owner's messages to give a status:
{messages}"""
OPENING = ("This conversation continues your work. Your previous conversation was closed because its context was nearly full, "
           "and nothing of it is carried over except what follows. You filed this handoff yourself before it closed, and every "
           "note in it is also on the Task it names: read a Task before you act on it.")


def stamp(moment: datetime) -> str:
    return timestamp(lambda: moment)


def when(at):
    return f"{at[:10]} {at[11:16]} UTC"


def where(message):
    place = message["place"]
    if message["channel"] == "chat" or not place:
        return message["channel"]
    return f"thread on {place['type'].replace('_', ' ')} \"{place['title']}\"" if place.get("title") else "thread"


def cut(text, limit=PREVIEW):
    if text is None:
        return "(text not readable now)"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def named(task):
    return f"\"{task['title']}\" ({task['id']})"


class CompactionHandoff:
    def __init__(self, runtime, *, clock=utc_clock):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock

    def register(self):
        common = dict(scope_kinds=frozenset({"global"}), global_capability="read_global_context", supports_signoff=False,
                      exposure=frozenset({"mcp"}), resolve_namespace=lambda ctx, args: "global", default_mode="allow")
        self.runtime.registry.register(OperationDescriptor(
            "compaction_handoff_get", GET_SCHEMA, description=GET_DESCRIPTION, effect_kind="read", read=self.get, **common))
        self.runtime.registry.register(OperationDescriptor(
            NAME, FILE_SCHEMA, description=FILE_DESCRIPTION, effect_kind="records", prepare=self.prepare, apply=self.apply,
            **common))
        self.runtime.compaction_handoff = self
        return self

    # ----- who and what ----------------------------------------------------------------------------------------------

    def coordinator(self, ctx, uow):
        """The configured coordinator in person, at Global: never a delegated identity, another agent or the owner."""
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        if (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None
                or ctx.principal_id != configured_ordubashy(uow)):
            raise DomainError("forbidden", "A compaction handoff is filed by the coordinator in person")
        return ctx

    @staticmethod
    def rows(uow, agent_id):
        """``(the row waiting to be opened or None, the last one opened or None)``."""
        def newest(condition, order):
            found = uow.connection.execute(f"SELECT id FROM {TABLE} WHERE agent_id=? AND opened_at IS {condition} "
                                           f"ORDER BY {order} DESC, id DESC LIMIT 1", (agent_id,)).fetchone()
            return uow.repository(TABLE).get(found["id"]) if found else None
        return newest("NULL", "requested_at"), newest("NOT NULL", "opened_at")

    def form(self, ctx, uow):
        """What a handoff filed now answers for: the row it fills (``waiting``, None for a new one), the period, and the
        owner's messages, oldest first, each with its handle and what the last opened handoff left of it (``open``)."""
        waiting, before = self.rows(uow, ctx.principal_id)
        now = stamp(self.clock())
        since = waiting["listed_from"] if waiting else before["listed_until"] if before else stamp(self.clock() - FIRST_PERIOD)
        # A period is fixed when the handoff was asked for; one the agent files unasked runs up to what it answers.
        asked = bool(waiting) and waiting["reason"] != "agent"
        until = waiting["listed_until"] if asked else now
        carried = {}
        if before:
            for entry in (before["filing_json"] or {}).get("messages", ()):
                if entry["status"] in OPEN:
                    carried[entry["message_id"]] = {key: entry.get(key) for key in ("status", "note", "task", "next_step")}
            for identity in (before["opened_json"] or {}).get("unanswered", ()):
                carried[identity] = {"status": "remaining", "note": "(your last handoff gave this message no status)",
                                     "task": None, "next_step": ""}
        found = self.runtime.owner_inputs.addressed(ctx, uow, since=since, until=until, ids=tuple(carried), limit=MAX_MESSAGES)
        messages = [{**message, "handle": f"m{number}", "open": carried.get(message["id"])}
                    for number, message in enumerate(found, 1)]
        return {"waiting": waiting, "asked": asked, "since": since, "until": until, "messages": messages}

    @staticmethod
    def line(message):
        """One listed message as the agent is shown it when asked for a status."""
        text = f"{message['handle']} [{when(message['at'])}, {where(message)}] {cut(message['text'])}"
        left = message["open"]
        if left:
            text += f"\n    your last handoff: {left['status']}. {left['note']}" + (
                f" Task {named(left['task'])}." if left.get("task") else "")
        return text

    def get(self, ctx, args, options, uow):
        form = self.form(self.coordinator(ctx, uow), uow)
        return {"asked": form["asked"], "messages": [
            {"id": message["handle"], "at": message["at"], "where": where(message), "text": cut(message["text"]),
             "chars": len(message["text"] or ""), "open": message["open"]} for message in form["messages"]]}

    # ----- filing ----------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        ctx = self.coordinator(ctx, uow)
        form = self.form(ctx, uow)
        listed, problems, given = form["messages"], [], {}
        handles = {message["handle"] for message in listed}
        for entry in args["messages"]:
            if entry["id"] not in handles:
                problems.append(f"{entry['id']} is not a listed message.")
            elif entry["id"] in given:
                problems.append(f"{entry['id']} has two entries.")
            else:
                given[entry["id"]] = entry
                if entry["status"] in OPEN and not entry.get("task_id"):
                    problems.append(f"{entry['id']} is {entry['status']} and names no Task: give task_id, and create the Task "
                                    "first if none holds this work.")
        answered = [message for message in listed if message["handle"] in given]
        # A handoff that was asked for answers its whole fixed list. One filed unasked answers up to the newest message it
        # gives a status; anything the owner wrote after that reaches the next conversation as a new message.
        last = None if form["asked"] or not answered else max(message["at"] for message in answered)
        missing = [message for message in listed if message["handle"] not in given and (last is None or message["at"] <= last)]
        if missing:
            problems.append("No entry for: " + "; ".join(
                f"{message['handle']} ({when(message['at'])}, {where(message)}: {cut(message['text'], 80)})" for message in missing) + ".")

        builder = PlanBuilder(ctx, NAME, args, "File the compaction handoff", clock=self.clock, namespace="global",
                              affected_scope={"kind": "global"})
        writes = self.writes(args, [(message, given[message["handle"]]) for message in answered])
        tasks = {}
        for key, write in writes.items():
            try:
                tasks[key] = self.compose(ctx, builder, key, write, options, uow)
            except DomainError as error:
                if error.code not in {"not_found", "validation_error", "record_moved", "forbidden"}:
                    raise
                problems.append(f"Task {key}: {error.message}")
        if problems:
            raise DomainError("validation_error", "The handoff was not filed. " + " ".join(problems), details={"problems": problems})

        def task(identity):
            return tasks[identity.strip()] if identity else None

        stopped = args["stopped"]
        filing = {
            "messages": [{"handle": message["handle"], "message_id": message["id"], "at": message["at"],
                          "status": entry["status"], "note": entry["note"], "task": task(entry.get("task_id")),
                          "next_step": entry.get("next_step", "")} for message, entry in ((m, given[m["handle"]]) for m in answered)],
            "tasks": [{"task": task(item["task_id"]), "note": item["note"], "next_step": item.get("next_step", "")}
                      for item in args.get("tasks", ())],
            "stopped": {"task": task(stopped.get("task_id")), **{key: stopped[key] for key, _ in STOPPED},
                        "also_open": [{"task": task(item["task_id"]), "pending": item["pending"]}
                                      for item in stopped.get("also_open", ())]},
            "workers": args["workers"], "not_recorded": args.get("not_recorded", ""),
        }
        until = form["until"] if last is None else min(form["until"], stamp(datetime.fromisoformat(last.replace("Z", "+00:00"))
                                                                              + timedelta(microseconds=1)))
        waiting = form["waiting"]
        if waiting:
            builder.record(TABLE, waiting["id"], waiting, {"filed_at": builder.at, "filing_json": filing, "listed_until": until},
                           label="Compaction handoff")
        else:
            identity = new_id()
            builder.record(TABLE, identity, None, {
                "id": identity, "agent_id": ctx.principal_id, "reason": "agent", "requested_at": builder.at, "request_json": {},
                "listed_from": form["since"], "listed_until": until, "filed_at": builder.at, "filing_json": filing,
                "opened_at": None, "opened_json": None}, label="Compaction handoff")
        return builder.build()

    @staticmethod
    def writes(args, answered):
        """What the filing puts on each Task it names: ``{task id: {notes, next_step}}`` in the order first named. A Task
        named more than once gets one note; the last next step given for it stands."""
        found = {}

        def add(task_id, note, step=""):
            slot = found.setdefault(task_id.strip(), {"notes": [], "next_step": ""})
            slot["notes"].append(note)
            slot["next_step"] = step or slot["next_step"]

        for message, entry in answered:
            if entry.get("task_id"):
                # The owner's words stay where they were written: a Task note is read by the Task's workers.
                add(entry["task_id"], f"Handoff: the owner's message of {when(message['at'])} is {entry['status']}. {entry['note']}",
                    entry.get("next_step", ""))
        for item in args.get("tasks", ()):
            add(item["task_id"], "Handoff: " + item["note"], item.get("next_step", ""))
        stopped = args["stopped"]
        if stopped.get("task_id"):
            add(stopped["task_id"], "Handoff: the conversation stopped here. " + " ".join(
                f"{label}: {stopped[key].rstrip('.')}." for key, label in STOPPED))
        for item in stopped.get("also_open", ()):
            add(item["task_id"], "Handoff: started and not finished: " + item["pending"])
        return found

    def compose(self, ctx, builder, task_id, write, options, uow):
        """Prepare one Task's note and next step with the Task service's own validation and adopt its effects into the
        filing's plan. The Task is found by its id in whatever scope it is. A closed Task gets the note only."""
        tasks = self.runtime.domains.tasks
        located, namespace = tasks.lookup(ctx, {"task_id": task_id}, uow)
        if namespace is None:
            raise DomainError("not_found", "no Task has this id")
        project = None if namespace == "global" else namespace.removeprefix("project:")
        identity, scope = located["task_id"], ({} if project else {"scope": "global"})
        row = uow.repository("tasks", project_id=project).get(identity, required=True)
        note = "\n".join(write["notes"])
        if write["next_step"] and row["status"] not in CLOSED_STATES:
            operation, arguments = "tasks_update", {"task_id": identity, "changes": {"next_step": write["next_step"]}, "note": note, **scope}
        else:
            operation, arguments = "tasks_comment", {"task_id": identity, "text": note, **scope}
        prepared = tasks.prepare(operation, ctx.select_project(project) if project else ctx, arguments,
                                 replace(options, expected_revision=None), uow)
        for target in prepared.expected_targets:
            if builder.targets.get(target.resource, target) != target:
                raise DomainError("invalid_plan", "Prepared snapshots disagree")
            builder.targets[target.resource] = target
        builder.changes.extend(prepared.changes)
        preview = plain(prepared.preview)
        builder.resources.extend(preview["resources"])
        builder.previews.extend(preview["changes"])
        builder.effects.extend(preview["effects"])
        # The Task write is judged by the Task service's own policy too, and this marker ends the Task's effects in the plan.
        builder.policy_effect(builder.expect(ref("tasks", identity, project), row), operation, "Writes the handoff note on the Task")
        return {"id": identity, "project_id": project, "title": row["title"]}

    def apply(self, ctx, plan, uow):
        if plan.principal_id != configured_ordubashy(uow):
            raise DomainError("forbidden", "A compaction handoff is filed by the coordinator in person")
        tasks, group, own, written = self.runtime.domains.tasks, [], [], []
        for change in plan.changes:
            if change.resource.resource_type == TABLE:
                own.append(change)
            elif change.action == "policy_effect" and change.operation_name in {"tasks_update", "tasks_comment"}:
                # One Task's effects, applied by the Task service: its write, follows, thread entry and event.
                project, identity = change.resource.project_id, change.resource.resource_id
                arguments = {"task_id": identity, **({} if project else {"scope": "global"})}
                tasks.apply(ctx.select_project(project) if project else replace(ctx, project_id=None),
                            replace(plan, operation_name=change.operation_name, arguments=arguments, changes=tuple(group)), uow)
                written.append({"task_id": identity, "set": "note and next step" if change.operation_name == "tasks_update" else "note"})
                group = []
            else:
                group.append(change)
        if group or len(own) != 1:
            raise DomainError("invalid_plan", "The handoff plan does not match its Task writes")
        apply_records(ctx, replace(plan, changes=tuple(own)), uow)
        filing = plain(own[0].values)["filing_json"]
        return {"filed": True, "compaction_id": own[0].resource.resource_id, "messages": len(filing["messages"]), "tasks": written,
                "next": "The handoff is filed. Stop here: this conversation will be closed and a fresh one opened from it."}

    # ----- the host's side: asking for a handoff and opening the next conversation -----------------------------------

    def ask(self, ctx, uow, *, reason="context", context=None):
        """Ask for a handoff: fix the period it answers for and return ``(row id, the request to send the agent)``. The row
        waiting to be opened is reused, so asking again asks for the same handoff. The caller owns the write transaction."""
        ctx = self.coordinator(ctx, uow)
        waiting, _ = self.rows(uow, ctx.principal_id)
        now = stamp(self.clock())
        if waiting:
            uow.repository(TABLE).update(waiting["id"], {"reason": reason, "requested_at": now, "request_json": context or {},
                                                         "listed_until": now})
            identity = waiting["id"]
        else:
            identity = new_id()
            uow.repository(TABLE).insert({"id": identity, "agent_id": ctx.principal_id, "reason": reason, "requested_at": now,
                                          "request_json": context or {}, "listed_from": self.form(ctx, uow)["since"],
                                          "listed_until": now, "filed_at": None, "filing_json": None, "opened_at": None,
                                          "opened_json": None})
        listed = self.form(ctx, uow)["messages"]
        return identity, ASK.format(messages="\n".join(self.line(message) for message in listed) or "(none in this period)")

    def opening(self, ctx, uow, *, tail=""):
        """What the fresh conversation starts with after its instructions, from the row waiting to be opened:
        ``{id, text, unanswered}``, or None when no row waits. ``tail`` is the last messages of the closed conversation,
        word for word, which only the caller holds. Changes nothing: ``opened`` records that it was used."""
        ctx = self.coordinator(ctx, uow)
        waiting, _ = self.rows(uow, ctx.principal_id)
        if waiting is None:
            return None
        form = self.form(ctx, uow)
        filing = waiting["filing_json"] or {}
        entries = {entry["message_id"]: entry for entry in filing.get("messages", ())}
        listed = [message for message in form["messages"] if message["at"] < waiting["listed_until"] or message["id"] in entries]
        later = self.runtime.owner_inputs.addressed(ctx, uow, since=waiting["listed_until"], until=stamp(self.clock()),
                                                    limit=MAX_MESSAGES)
        known = {message["id"] for message in listed}
        parts = [OPENING]
        rules = self.rules(uow, ctx.principal_id)
        if rules:
            parts.append("RULES THE OWNER STATED TO YOU. They are in force: follow them. You proposed each as a change to your "
                         "written instructions, and only that writing-down still waits for the owner's decision.\n"
                         + "\n".join(f"- {rule}" for rule in rules))
        if listed:
            parts.append("THE OWNER'S MESSAGES TO YOU, IN THEIR EXACT WORDS, AND WHERE EACH STANDS BY YOUR HANDOFF\n"
                         + "\n".join(self.quoted(message, entries.get(message["id"])) for message in listed))
        arrived = [message for message in later if message["id"] not in known]
        if arrived:
            parts.append("THE OWNER'S MESSAGES THAT ARRIVED WHILE YOU WERE FILING. You have not answered or acted on them.\n"
                         + "\n".join(self.quoted(message, None, pending=False) for message in arrived))
        stopped = filing.get("stopped")
        if stopped:
            lines = [f"Task: {named(stopped['task']) if stopped['task'] else 'none'}",
                     *(f"{label}: {stopped[key]}" for key, label in STOPPED)]
            if stopped["also_open"]:
                lines.append("Also started and not finished: " + "; ".join(
                    f"{named(item['task'])}: {item['pending']}" for item in stopped["also_open"]))
            parts.append("WHERE YOU STOPPED\n" + "\n".join(lines))
        others = [item for item in filing.get("tasks", ())]
        if others:
            parts.append("OTHER TASKS YOU WROTE A NOTE ON WHEN YOU STOPPED (the note is on each Task)\n" + "\n".join(
                f"- {named(item['task'])}" + (f". Next step: {item['next_step']}" if item["next_step"] else "") for item in others))
        held = self.held(uow, ctx.principal_id)
        if filing.get("workers") or held:
            parts.append("YOUR WORKERS\n" + "\n".join(part for part in (
                filing.get("workers"), held and "Open Tasks with a worker now, from the records:\n" + "\n".join(held)) if part))
        if filing.get("not_recorded"):
            parts.append("NOT ON ANY RECORD\n" + filing["not_recorded"])
        if not waiting["filed_at"]:
            parts.append("NO HANDOFF WAS FILED before the previous conversation closed. The statuses above are missing: find "
                         "where each message stands from the records before you act on it.")
        elif waiting["filed_at"] < waiting["requested_at"]:
            # Filed unasked, then asked again when the context filled, and the conversation closed without a newer filing.
            parts.append(f"THIS HANDOFF WAS FILED AT {when(waiting['filed_at'])}, BEFORE THE CONVERSATION ENDED. What you did "
                         "after that time is not in it: check the records for it before you act.")
        if tail:
            parts.append("THE LAST MESSAGES OF THE CLOSED CONVERSATION, WORD FOR WORD\n" + tail)
        return {"id": waiting["id"], "text": "\n\n".join(parts),
                "unanswered": [message["id"] for message in listed if message["id"] not in entries]}

    def opened(self, uow, identity, unanswered=(), *, closed=None, session=None):
        """Record that the fresh conversation was started from this row: ``closed`` is the id of the conversation that was
        closed and ``session`` the id of the fresh one, which is what ``continued`` reads. The caller owns the write
        transaction."""
        uow.repository(TABLE).update(identity, {"opened_at": stamp(self.clock()), "opened_json": {
            "unanswered": list(unanswered), "closed": closed, "session": session}})

    @staticmethod
    def continued(uow, agent_id, conversation_id):
        """The conversation that was closed to open ``conversation_id`` for this agent: ``{conversation_id, at}`` with when
        the fresh one was opened, or None when no compaction opened it. Read from the newest opened row that names both; a
        row opened without the ids names neither."""
        found = uow.connection.execute(
            f"SELECT json_extract(opened_json,'$.closed') AS closed, opened_at FROM {TABLE} WHERE agent_id=? "
            "AND opened_at IS NOT NULL AND json_extract(opened_json,'$.session')=? AND json_type(opened_json,'$.closed')='text' "
            "AND json_extract(opened_json,'$.closed') NOT IN ('', ?) ORDER BY opened_at DESC, id DESC LIMIT 1",
            (agent_id, conversation_id, conversation_id)).fetchone()
        return {"conversation_id": found["closed"], "at": found["opened_at"]} if found else None

    @staticmethod
    def quoted(message, entry, *, pending=True):
        """One owner message in its exact words with where it stands."""
        text = message["text"]
        if text is None:
            text = "(text withheld: possibly a private input)" if message["redacted"] else "(text not readable now)"
        elif len(text) > MESSAGE_CAP:
            text = text[:MESSAGE_CAP] + f" …[cut: read all of it with owner_input_get {message['id']}]"
        lines = [f"[{when(message['at'])}, {where(message)}] {text}"]
        if entry:
            lines.append(f"    {entry['status'].upper()}: {entry['note']}"
                         + (f" Read: Task {named(entry['task'])}." if entry["task"] else "")
                         + (f" Next step: {entry['next_step']}" if entry["next_step"] else ""))
        elif pending:
            lines.append("    NO STATUS: your handoff did not say where this stands.")
        return "\n".join(lines)

    @staticmethod
    def rules(uow, agent_id):
        """The summaries of the agent's instruction-change proposals the owner has not decided."""
        return [row["title"] for row in uow.connection.execute(
            "SELECT json_extract(preview_json,'$.input.title') AS title FROM signoffs WHERE proposer_principal_id=? "
            "AND category='input_request' AND request_key LIKE 'direct-instruction:%' AND state='pending' "
            "AND superseded_by_id IS NULL ORDER BY created_at, id", (agent_id,)) if row["title"]]

    @staticmethod
    def held(uow, agent_id):
        """Who holds which open Task now, one line per worker, read from the Tasks themselves."""
        names = {row["id"]: row["display_name"] for row in uow.connection.execute("SELECT id, display_name FROM principals")}
        by_worker = {}
        for row in uow.connection.execute(
                "SELECT t.id, t.title, w.value AS worker FROM tasks t, json_each(t.worker_ids_json) w WHERE t.archived=0 "
                f"AND t.status NOT IN ({','.join('?' * len(CLOSED_STATES))}) AND w.value<>? ORDER BY t.updated_at DESC LIMIT ?",
                (*CLOSED_STATES, agent_id, WORKER_LINES)):
            by_worker.setdefault(names.get(row["worker"]) or row["worker"], []).append(f"\"{row['title']}\" ({row['id']})")
        return [f"- {worker}: " + "; ".join(titles) for worker, titles in sorted(by_worker.items())]
