# D59 — Show native approvals and questions in Chat

Manager decision, 2026-10-09. The owner observed a running Codex blocked on
`messages_acknowledge` approval while Chat showed no request for input, and
explicitly asked to include questions. Make the current native prompt visible
in Chat and the agent list. Answering remains in the existing Terminal.

## Scope and contract

Add optional nullable `attention` to `runtime_get`'s RuntimeView. No new public
operation, command route, job handler, database or schema migration. The value is:

```text
state: waiting | none | unknown
kind: approval | question | null
prompt: string (1..6000 characters) | null
truncated: boolean
observed_at: UTC timestamp | null
owner_id: UUID | null
terminal_id: UUID | null
```

`waiting` requires kind, prompt, observation time and both IDs, matching the
current connected owner. `none` is a fresh observation without a recognized
active prompt; it is not input readiness. `unknown` covers unavailable or stale
observation and must not be represented as an answered question. Non-waiting
values have null kind/prompt and truncated=false. An absent/null field permits
older-core compatibility and does not claim this capability. `input_ready`
remains false. These observations never authorize paste, accept an approval,
answer a question, change native permissions or launch/restart a CLI.

## Core implementation

Use one small NativeAttention observer with the existing SessionMonitor,
Application assembly and one-second observation loop. Reads return cached
observations only, with five-second monotonic freshness like NativeCollector.
Bind every cached result to the exact current owner, terminal and recovery
generation; owner change/exit cannot reuse a previous session's prompt.
No API read performs tmux I/O inside a SQL transaction.

Observe only the existing verified recorded shell. Reuse RuntimeInput's owned
workspace, saved terminal and current-owner checks and the native ancestry
verification. An additive read-only snapshot helper there may share that
verification, but must not use the idle/composer/input-readiness gate: a real
question/approval is expected to be non-idle, and a human-controlled terminal
must remain observable. Snapshot under the existing terminal gate, validate
ownership again afterward, and never write keys, resize, change control, adopt
a process or refresh hooks as a side effect. Existing dispatch decisions stay
unchanged.

Add a pure pinned-native attention classifier separate from classify_input.
Its input is the verified provider/version and TerminalSnapshot. Recognize
the currently visible interactive form at the bottom of the pane; never match
question marks or approval words in ordinary chat/scrollback. Use recognized
form header, options/cursor presentation and bottom action footer together.
Strip only supported ANSI decoration using the existing observation helpers.
Extract the visible prompt and option labels as plain text, with a 6000-character
bound and explicit truncation. Do not expose surrounding conversation history
as the prompt. Disappearance on a fresh valid snapshot clears the wait; failed
observation expires to unknown instead of claiming completion.

The immediate supported target is Codex 0.160.0. The owner's read-only saved
screen has `Field 1/1`, the MCP approval question, numbered Allow/Cancel choices
with a selected reverse-video row, and final `enter to submit | esc to cancel`.
Use that factual sample plus actual disposable pinned-Codex captures for native
questions (single and multi-question/free-text as available) and shell/MCP
approvals. Capture the actual profile before adding recognition; do not invent
native screen fixtures. Unsupported native layouts/providers remain unknown,
and evidence must name the exact tested coverage. No renewed provider-restricted
Claude or browser probes are part of this work. The [amendment](#amendment--claude-code-question-form)
adds one observed Claude layout to that classifier; everything else stays unknown.

Fresh waiting observations contribute to agents_get/list needs_user_input,
ORed with existing unresolved browser requests. Expose waiting_user and its
observation time while fresh, preserving the authored status text. Keep the
directory read SQL/cache-only. Record bounded native.attention_changed events
on actual state/prompt/owner transitions, not every poll or selected-option
repaint. These invalidate the existing UI directory reads. Raw terminal capture
already retains the source bytes; do not fabricate conversation messages,
manual dated transcript entries or a second durable input-request system.

## UI implementation

The UI accepts both old RuntimeView and the exact additive attention shape;
malformed or mismatched owner/terminal information cannot become a current
notice. Use the existing runtime polling and no terminal-output parser.

In Chat, show a compact notice immediately above the composer when the selected
agent is waiting: `Needs your input`, a subtype `Approval requested` or
`Question from <display name>`, the actual escaped plain prompt and an
`Open Terminal` button. Keep the initial prompt area compact (up to six lines)
with an accessible expand/collapse action when longer. Show truncation if core
reports it. One current notice only, not another transcript message. It has no
Dismiss/Done/Allow actions: only answering/cancelling in Terminal resolves the
native request. The button uses the existing Chat/Terminal switch for the same
agent and does not type or select an answer.

Show a visible `Needs input` label alongside the agent's existing attention dot,
including unselected agents, while preserving browser attention behavior.
Fresh none/disconnected/owner-change observations remove the old current notice.
On a failed read or unknown observation, distinguish stale/unavailable status;
never display the previous prompt as a newly verified wait or as completed.
Keep drafts, conversation scroll, selection and focus stable during polls and
notice changes. Use the current monochrome desktop style and no mobile work.

## Focused acceptance and ownership

Core and UI may be built in parallel against this frozen additive contract.
Core owns observation/classification/wire semantics and its concise evidence.
UI owns validation/presentation and its concise evidence. No unrelated auth,
bubblewrap installation, hook-output repair, VM, browser, message-delivery or
native-input changes. Manager integrates and deploys the completed pair under
the owner's standing authorization; additional review is waived by the owner.

Core: a compact owning classifier/observer group covers actual approval and
question layouts, prompt changes and clearing, normal transcript text containing
the same words, stale/failing observation, owner replacement and no input effects.
Run affected runtime/agent contract checks only. Reuse existing disposable
pinned-Codex/local canned-provider helpers for one question→answer→clear flow,
with actual captured screens and installed candidate bytes; no paid inference,
owner credentials/session input, fake readiness or exhaustive new test harness.

UI: build/typecheck and extend a compact existing test for approval/question,
Open Terminal, clearing, old-core compatibility, stale response, escaping and
draft preservation. The owner's request for hands-on UX testing via Codex's
built-in Chrome control remains separate; those controls currently list no
browser. Do not substitute custom browser tooling or call fixture tests a
hands-on user-session test. A completed pair also needs read-only observation
on the local test instance before claiming deployment success.

## Amendment — Claude Code question form

Manager decision, 2026-10-14. The owner's real Claude Code 2.1.287
`AskUserQuestion` was visible in Terminal while Chat showed nothing, because
the pure classifier recognized only Codex 0.160.0. Add the one observed Claude
form to that classifier. `NativeAttention`, `runtime_get`, the agent projection
and the UI are unchanged: no input request, conversation entry, public
operation, schema, hook, owner-refresh, timeout or cache change, and no second
question system. The notice never answers or selects; answers stay in Terminal.
This amendment does not claim live integrated acceptance.

**Scope.** Exactly provider `claude`, version `2.1.287`; no other provider or
version becomes supported. The geometry, cursor-bounds, normal-pane-flag and
supported-ANSI preconditions are the Codex ones, unchanged. Codex behavior is
unchanged, and a Codex screen passed as Claude is `unknown`.

**Supported form.** One question, drawn at the bottom of the pane, observed at
106x55 with the cursor hidden on the selected row. Rows, top to bottom, with
SGR colors as foreground/background (`gray` is 256-color 246):

| Row | Plain text | Observed style |
| --- | --- | --- |
| upper rule | `─` across every column | gray |
| tab | ` ☐ <header> ` (one tab) | foreground 16 on background 153, including both edge spaces |
| blank | | |
| question | one or more rows | bold, foreground 231 |
| blank | | |
| options 1..k, k >= 2 | `❯ N. label` selected, `  N. label` otherwise | exactly one arrow, foreground 153; optional description rows indented five spaces, gray |
| option k+1 | `N. Type something.` | |
| lower rule | as above | gray |
| option k+2 | `N. Chat about this` | |
| blank | | |
| action footer | `Enter to select · ↑/↓ to navigate · Esc to cancel` | gray |

Header, question, option and description text vary; numbering is consecutive
from 1. Recognition requires the whole joint shape: the footer is the last
non-blank row, the cursor is hidden and inside the form, and each styled row has
exactly the style above. SGR state is tracked row to row, as tmux carries it;
any sequence other than 0, 1, 22, 39, 49 and 38/48;5;n inside the form refuses
it. Question marks, option numbers or the footer words in prose, scrollback,
unstyled copies of the form or a form followed by other rows never match.

**Prompt.** The visible question rows, then every numbered option with its
description rows, including `Type something.` and `Chat about this`. The
selector arrow, tab, rules, footer and all history are excluded, rows are
stripped like Codex's, and the 6000-character bound with `truncated` is shared.
Moving the selection does not change the prompt, hence causes no new
`native.attention_changed` event.

**Clearing.** Any other Claude screen is `unknown`, except that the existing
`classify_input` verdict `empty` (the known ordinary composer) gives
`state=none`. This clears a notice after the question is answered and the
composer returns; it is not input readiness, and the composer classifier and
its input rules are unchanged. While the turn continues after an answer the
state is `unknown`, which also expires the notice, never "answered".

**Not supported (stay `unknown`).** Multi-question tabs or a submit tab, the
free-text editor or visible cursor, tool-permission and other approvals, more
than nine options, wrapped option labels, unstyled or differently
styled variants, and any form without both the Type-something and
Chat-about-this affordances. Only the single captured layout exists as a
sanitized fixture (`claude-attention.json`); the selection of options 2-4 and
every negative variant in the regression group are labelled synthetic edits of
it, not captures. The real open question is validated by the Manager after
integration.
