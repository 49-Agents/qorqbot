# Compaction of the coordinator's conversation

When Ordubashy's conversation fills up, Qorqbot does not let the client
summarise it. Ordubashy files a handoff, the conversation is closed, and a fresh
one starts from his instructions, the owner's own messages and that handoff.

This covers Ordubashy only, and only while he runs on Claude Code. Workers and
Codex sessions still use the client's own compaction.

Paths are relative to `qorqut-code/app/qorqut/` unless they say otherwise.

## What is carried over, and what is not

| Kind of context | What happens to it |
|---|---|
| Instructions (role, rules, plugins) | Not carried. The fresh session's own role command loads them again. |
| Skills and tool results read along the way | Not carried. The agent reads them again if it needs them. |
| Operational state (Tasks, Signoffs, Actions) | Not copied. The handoff names the Task that holds each piece of work. |
| The owner's messages | Quoted in their exact words from the records, each with a status the agent gave it. |
| Where the agent stopped | Two or three lines the agent writes, plus the last messages word for word. |
| Who holds which work | One paragraph the agent writes, plus the open Tasks each worker holds, from the records. |

No model writes a summary of the conversation.

## When

`native_chat/compaction.py` asks for the handoff when the context holds
`limit()` tokens: 300,000 (`LIMIT_TOKENS`). A model whose own window is small
gets less: 70% of that window, or 45,000 tokens before its end, whichever
comes first.

The context size comes from the client. The Claude driver reads the `usage` of
every model request of the session itself (not of a subagent) and reports it in
a `usage` event each time it has moved by 10,000 tokens, and again when a turn
ends. The host keeps the last reading in `status.usage`
(`{tokens, window, at}`). A session whose client reports nothing is never
compacted this way.

## The flow

The controller's `step` runs on every pass of the native Chat backend. It never
waits. One row of `compactions` is its whole durable state.

1. **Ask.** `CompactionHandoff.ask` creates the row and fixes the list of owner
   messages the handoff answers for. The request goes to the host as a plain
   message, also while a turn is running; the agent then gets it when its
   current tool call ends.
2. **File.** The agent proposes any standing rule the owner stated
   (`agentic_change_propose`), then calls `compaction_handoff_file` once, from
   memory. The call is refused as a whole, with every reason, if a listed
   message has no status or an open one names no Task.
3. **Stop.** After the filing the controller waits for the turn to end. A turn
   that goes on for two minutes is interrupted.
4. **Replace.** `SessionOpener.replace` closes the host and starts a fresh
   conversation, whose host sends the role command; the opening is its next
   message. Both happen under the agent's one open at a time, so no other input
   can open the agent in between and resume the closed conversation. The
   opening is written after the close, so an owner message that reached the
   closing conversation is in it.
5. **Record.** The row gets `opened_at` and the ids of the closed and the fresh
   conversation.

The replacement is not an input source. It never opens an agent the owner
turned off, never turns one on or off, and a failure is neither paced nor
counted towards the unreachable alert. It closes only the conversation it was
asked to replace. If the fresh conversation cannot be started, the agent is
left asleep with the closed conversation still selected, the controller tries
twice more, and after that the next input resumes the closed conversation and
the flow continues from its row.

If the turn ends without a filing, the request is sent once more in a turn of
its own. If that turn also ends without one, or nothing is filed within 15
minutes, the conversation is replaced anyway. The opening then says no handoff
was filed and gives every message without a status.

The request and the opening both start with a tag (`<qorqut-handoff-request>`,
`<qorqut-handoff>`), so Chat does not show them as the owner's words and the
owner-turn index never holds them.

If the service restarts, the session closes or the owner ends it, the flow in
memory is dropped. It continues from its row when the same conversation runs
again. A request is never sent to another conversation.

## The filing

`compaction_handoff_get` returns the form: the listed messages with handles
`m1`, `m2`, and what the previous handoff left open. `compaction_handoff_file`
takes:

- `messages`: one entry per listed message with `status` (`done`, `partly`,
  `remaining`), a `note`, and for an open one the `task_id` that holds it and an
  optional `next_step`.
- `tasks`: notes for Tasks that no owner message is tied to.
- `stopped`: the step in progress, the last action and its result, the next
  action, and what is being waited on.
- `workers`: who was assigned what.
- `not_recorded`: anything that sits on no record.

Filing is what records it. Each note is written onto the Task it names as a
comment, and a next step is set there, through the Task engine's own
operations. The owner's words are never copied into a Task note.

Both tools are for the configured Ordubashy in person. A worker, a delegated
identity and the owner are refused.

A message that is `partly` or `remaining` is listed again in the next handoff
until one marks it `done`.

## The log

`compactions` has one row per compaction: why and when it was asked for
(`reason`, `requested_at`, `request_json` with the context size and the
conversation), the period it answers for, the filing (`filing_json`), and when
the next conversation was opened from it (`opened_at`, and the messages that got
no status in `opened_json`). `opened_json` also holds the id of the conversation
that was closed (`closed`) and the id of the fresh one (`session`). It holds
message ids, not the owner's text, and it is excluded from the general
operational history.

The table is added by the offline `python -m qorqut.migration.upgrade` transform
`compactions`.

## In Chat

A conversation that a compaction opened has a "Context compacted" marker at its
top. "Load older messages" there continues into the conversation before it, so
the owner reads across a compaction without choosing another conversation. The
request and the opening stay hidden.

The Chat state names the earlier conversation in `conversation.continues`
(`docs/v1.2/NATIVE_CHAT_DTO.md`). `CompactionHandoff.continued` reads it from the
row's `closed` and `session`. It is named only when the earlier conversation was
captured for the same agent and client. A replacement that failed records
neither id.

## Turning it on

`QORQUT_OWN_COMPACTION` in the service's environment decides:

| Value | What runs |
|---|---|
| `0` | Nothing here. The client compacts as before. |
| `1` | This compaction, with the client's own left in place behind it. The client's own compaction has come at about 330,000 tokens, only about 30,000 after this one asks, so a slow filing can be overtaken by it; the handoff is then filed from the client's summary. |
| unset or `only` (the default) | This compaction, and Ordubashy's Claude session is started with `DISABLE_AUTO_COMPACT=1`, so the client never compacts on its own, neither at its limit nor while idle (checked in Claude Code 2.1.328). Its manual `/compact` stays. |

The variable for `only` comes from the client table
(`managed_runtime/clients.py`, `launch_environment`). The managed runtime asks
`CompactionController.replaces_client` at each start of a session, so a change
of the value applies from the session's next start. No other agent's session
gets it.

## Checking a change to the handoff

`app/scripts/compaction_harness/` replays recorded compactions. For each one it
builds what the next conversation would start with under the client's summary
and under a filing, asks the same questions of both, and has a second model mark
the answers. It calls the Claude Code CLI on the installation's own sign-in, so
a run uses plan usage. Its output holds private conversation text and belongs
outside the repository.

## Known limits

- Only the Claude driver reports the context size while a run goes on.
- With `only`, nothing compacts the conversation if this flow keeps failing: it
  grows towards the model's own limit until a replacement succeeds.
- A fresh conversation that came up and bound but did not take the opening (the
  host failed between the two) runs without the handoff. Its row stays
  unopened, the Task notes are already written, and the next handoff lists the
  same messages again.
- Work that is on no Task and that the agent leaves out of `not_recorded` is
  lost, as it would be in any summary.
