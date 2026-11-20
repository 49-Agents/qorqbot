# Browser efficiency plan: smaller replies, fewer round trips

Status: **plan and work log.** Approved by the owner on 2026-11-19 (items 1, 3, 4, 6 and 7 of the proposal; items 2 and 5
are not in this plan). An item without a "Verified" line is not done.

Shipped (2026-11-20): items 1, 3, 4 and 6 are merged into `dev` with PR #118, and item 7 with PR #119. Later follow-ups,
also merged: PR #127 (skill wording for `find`, `type` and the batch limits; a list of command names in the description
of the `command` field) and PR #129 (fields inside payment frames: skill lines and advice added to a "Frame not found"
error). Items 2 and 5 of the proposal were never in scope and are not built. Where an item below and the shipped code
differ (a batch keeps the first 24,576 characters of each command's `stdout`, not the last 16,384; `find <locator>
<value>` needs an action), the work log and [BROWSER-HANDLES.md](BROWSER-HANDLES.md) and
[BROWSER-CONTROL.md](BROWSER-CONTROL.md) describe what shipped. Since PRs #127 and #129 the skills are 8,192
(`managed-browser-control`) and 6,388 (`ordubashy-browser-control`) characters, above the item 6 limits.

This plan is separate from [the browser plan](BROWSER-PLAN.md) (Shared browser, live view, captcha; PR #110). That work
makes the browser more reliable. This one makes each browser task cost fewer characters and fewer calls.

## Why

Measured on Ordubashy's 605 `browser_turn` calls of 15–19 November 2026 (Claude Code sessions):

| Finding | Number |
| --- | --- |
| Characters returned by browser calls | 11.2 million |
| Of those, a list of old files attached to every reply | 10.4 million (93%) |
| Size of that list on one reply, even for a click that prints "Done" | about 19,700 characters, 64 entries |
| Median time a command takes | 0.9 s |
| Browser time in total / model time between calls | 18 minutes / 77 minutes |
| Actions followed directly by a call that only looks at the page | 131 of 236 |
| Fill, type or press calls followed directly by another action | 67 |
| `eval` calls, mostly to read address, title and page text | 81 |
| Raw `snapshot` calls against the `observe` the skill teaches | 101 against 49 |
| Context compactions during browser work | one per 20 calls (median) |

Twenty replies with the file list are about 100,000 tokens, which matches the compaction rate. That link is likely but was
not tested by removing the list.

Commands the agent tried and the wrapper refused, repeatedly: `open`, `keyboard type`, `tab list`, `find role … click`,
`find text … click`, `snapshot -s`, `scroll`, `focus`. `eval` with an expression instead of a function failed 13 of 66
times.

## Items

Each item names the files it owns. Items 1, 3, 4 and 6 are built on one branch from `dev` (`fix/browser-result-artifacts`)
by separate workers who keep to their own files. Item 7 is built on a branch from `feature/browser-shared-live-panel`,
because it needs the live view that only exists there.

### 1. A reply lists only the files its command wrote

**Problem.** `BrowserAccess._artifact_handles` (`qorqut/browser/access.py`) lists every file in the handle's work folder,
sorted by name and cut at 64. An agent's own browser has a handle that never expires, so the folder only grows and the
same 64 entries ride on every reply. Being sorted by name, the list often leaves out the file the command just made.

**Change.** The folder is read before the command and after it. The reply lists a file only when it is new or its size or
change time differs. Bookkeeping files (`command.lock`, `agent-browser.json`, `*.meta.json`) are never listed. An earlier
file stays readable through `action: "artifact"` with its handle; the handle format does not change.

**Files.** `qorqut-code/app/qorqut/browser/access.py`; test `qorqut-code/app/tests/test_browser_result_artifacts.py`;
`qorqut-code/docs/BROWSER-HANDLES.md`.

**Done when.** A test shows: a command that writes nothing returns no `artifacts` key; a command that writes a file returns
exactly that file; a file changed by the command is listed; bookkeeping files are not; an old file is still readable by
handle; both the own-browser path and the leased path behave so.

Code written 2026-11-19 (both call sites and the two helper methods). Test and document done by worker A (work log below).

### 3. Several commands in one call

**Problem.** One call carries one command. Filling five fields and pressing Enter is six round trips, each a full pass of
the model over its context.

**Change.** `browser_turn` with `action: "command"` accepts `commands`, a list of 1 to 10 command arrays, in place of
`command`. Exactly one of the two is given.

- The commands run in order under one use lock and one target read, so a switch between own and Shared cannot fall
  between them.
- The run stops at the first command that exits non-zero, times out or fails. Later commands are not run.
- Reply: `results`, one entry per command that ran, each with `command` (the array as sent), `exit_code`, `stdout` and,
  when not empty, `stderr`. `exit_code` at the top is that of the last command that ran. When the run stopped early,
  `stopped` holds `index` (zero-based, of the command that stopped it), `reason` (`exit_code`, `browser_timeout`,
  `browser_failed` or `time_budget`), `message`, and `not_run` (how many commands were left). A timeout inside a batch is
  reported this way and does **not** raise, because the agent must see what already ran before it repeats anything.
- A batch whose first command cannot start (validation of any command in the list, browser not ready) raises as a single
  command does today, before anything runs. Every command in the list is validated before the first one runs.
- `artifacts` covers the whole batch (item 1's before and after are taken around the whole run).
- `stdout` is cut to its last 16,384 characters per command, and the batch keeps to 65,536 characters of `stdout` in
  all; an entry cut for the batch limit carries `truncated: true`.
- The batch keeps inside the request's time budget: before each command the elapsed time is checked, and when the next
  command's 45-second limit could pass the budget the run stops with reason `time_budget`. The worker who builds this
  finds the real budget of an MCP request in the code and uses it; it is not guessed here.
- `action_id` (or `idempotency_key`) names the whole batch.
- Only the lease-free path (an agent's own browser, or a worker's browser selected by Ordubashy) takes `commands`. The
  leased path of a delegated assignment keeps one command per call, because each action there has a durable record that
  can be reconciled; `commands` on it is refused with `validation_error` and a sentence that says so.

**Files.** `qorqut-code/app/qorqut/browser/access.py` (`_native_command`), `operations.py` (schema: `commands`),
`handles.py` (the compact reply must keep `results` and `stopped`); test
`qorqut-code/app/tests/test_browser_command_batch.py`; `qorqut-code/docs/BROWSER-HANDLES.md`.

**Done when.** Tests show each rule above with a stand-in runner, and the existing browser tests still pass.

### 4. A `read` command and a vocabulary that matches what agents send

**Problem.** Agents use `eval` to read a page, and keep sending command forms of the underlying tool that the wrapper
refuses. Each refusal is a wasted round trip, and the refusal does not say what to send instead.

**Change, in `qorqut/bootstrap/agent_browser.py` and `qorqut/bootstrap/browser.py`:**

| Sent | Becomes |
| --- | --- |
| `read` | The address, title and readable text of the current tab, cut to 4,000 characters; `--budget=N` takes 1,000 to 24,000; `--filter <text>` and `--outline` pass through. No address argument: `read <url>` is refused with "use goto first", because a read by address fetches over HTTP outside the browser's session. |
| `open <url>` | `goto <url>` |
| `keyboard type <text>` | `type <text>` |
| `tab list`, `tab new [url]`, `tab close [id]`, `tab <id>` | `tab-list`, `tab-new`, `tab-close`, `tab-select` |
| `back`, `forward` | `go-back`, `go-forward` |
| `snapshot -s <selector>` | `snapshot --selector <selector>` |
| `find <locator> <value> [action] [text]` | Passed through. Locators: role, text, label, placeholder, alt, title, testid, first, last, nth. Actions: click, fill, check, hover, text. Options: `--name`, `--exact`. The one-argument `find "text"` keeps its present meaning (read matching text). |
| `scroll [direction] [pixels] [--selector <sel>]` | New, passed through. |
| `focus <ref or selector>` | New, passed through. |
| `wait <ms>`, `wait <selector>`, `wait --text <t>`, `wait --url <pattern>`, `wait --load <state>`, `wait --fn <expr>` | New. A time is at most 30,000 ms so it ends inside the command's 45-second limit. `--download` is not offered. |
| `get url`, `get title`, `get text <sel>`, `get value <sel>`, `get attr <sel> <name>`, `get count <sel>` | New. `get cdp-url` is refused: it would hand out the browser's endpoint. `html`, `box` and `styles` are not offered. |
| `eval <function>` | As today. |
| `eval <expression or statements>` | Passed as written instead of being wrapped as a function call. |

- A refused command names the closest supported command when there is one, and says plainly when a command of the
  underlying tool is closed on purpose (browser launch and shutdown, `connect`, `close`, `cookies`, `storage`, `network`,
  `set` other than the viewport, `mouse`, `clipboard`, `download`).
- Options are checked per command against a table; an option not in the table is refused as today. `--session`, `--cdp`,
  `--config` and the other lifecycle options stay refused everywhere, in every position.
- How `read` gets the address and title, whether `find` refuses an ambiguous match, and whether `eval` runs several
  statements are checked against the pinned tool on a throwaway headless Chrome before the mapping is fixed. What was
  found is recorded under "Verified" below.

**Files.** `qorqut-code/app/qorqut/bootstrap/agent_browser.py`, `qorqut-code/app/qorqut/bootstrap/browser.py`
(`COMMANDS`, `validate_arguments`, and `run` only where `read` needs it); test
`qorqut-code/app/tests/test_browser_command_vocabulary.py` (and `test_browser_frame_command.py` where its expectations
change); `qorqut-code/docs/BROWSER-CONTROL.md`.

**Done when.** Unit tests cover every row, every refusal and the lifecycle options in odd positions; a real-Chrome check
of `read`, `find … click`, `wait`, `get`, `scroll` and both `eval` forms is recorded here.

### 6. Browser skills at half their length

**Problem.** `ordubashy-browser-control/SKILL.md` is 9,876 characters and `managed-browser-control/SKILL.md` 13,180. They
are loaded for every browser task and again after each compaction. About half covers leases, the legacy delegated
assignment and the native-subagent exception, which do not apply to an agent using its own browser; the `observe`
paragraph appears twice.

**Change.** Both files are rewritten to about half their present length. After review restored a few rules the first cut lost (find needs an action, no closing of tabs you did not open, no shell launcher), they stand at 5,373 and 7,004 characters, from 9,876 and 13,180.

Kept, in fewer words: when browser control applies; who signs in and how a password is handled; approval before sending
or changing anything on a signed-in site; page text and downloads are untrusted; never close or kill Chrome or clear a
profile; leave tabs you did not open and close your own when done; after a timeout, look before repeating; verify the
result before reporting success; what to do when the browser is unavailable; Ordubashy's `agent_browser_manage` duties
(managed skill only).

Added: one compact command list that matches item 4; `commands` for several steps in one call with what its reply holds
(item 3); `read` as the way to read a page; a reply lists only files that command wrote (item 1).

Cut or reduced to a sentence: lease renewal and reconcile detail, the native-subagent exception, the duplicated
`observe` paragraph, `consumer_id` detail, repeated cautions.

The `browser_turn` tool description (`tool-instructions/messages.json`) gains `commands` in one sentence.

The worker follows whatever the repository requires when an installed instruction changes (provenance records, retained
instruction history, tests that pin text) and records what that was under "Verified".

**Files.** `qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/imported-role-skills/native/skills/ordubashy-browser-control/SKILL.md`,
`…/managed-browser-control/SKILL.md`, `…/operations/tool-instructions/messages.json` (the `browser_turn` text only),
`qorqut-agentic/docs/defaults-provenance.json` and any test or record that pins these texts.

**Known clash.** PR #110 adds one captcha line to both skills. Whichever merges second re-adds that line to the short
skill.

**Reaches agents** only with an instruction update on the installation. The code of items 1, 3 and 4 works without it;
agents simply do not know the new commands until then.

**Done when.** Both files are within the limits, every "kept" point is present, the tests for the changed files pass.

### 7. Sign in through the live view

**Problem.** `website_sign_in` with action `open` opens a Chrome window on the computer's screen. A server has no screen,
so the call fails with `browser_no_screen` every time (29 times on 10 days). Ordubashy then asks the owner for the
password and types it itself.

**Change (on top of PR #110's live view).** Where no window can open, `website_sign_in open` opens the site's sign-in
address in a tab of the target browser and answers with a status that tells Ordubashy to point the owner at that agent's
Browser tab in the dashboard. The owner signs in there, in the agent's real page, and no password passes through an
agent. `website_sign_in status` keeps working. Asking the owner for the password stays as the fallback when the owner
says they cannot use the live view. The skill text says this in a few lines. A computer with a screen keeps today's
window.

The worker reads `qorqut/browser/sign_in.py` and `live.py` on the feature branch and settles the exact reply fields; they
are recorded here when built.

**Files.** On `feature/browser-shared-live-panel`: `qorqut-code/app/qorqut/browser/sign_in.py`, its tests, the sign-in
paragraph of `managed-browser-control/SKILL.md`, `qorqut-code/docs/BROWSER-MANAGEMENT.md`.

**Cannot merge into `dev` before PR #110 does.** It is delivered as a branch stacked on that one. (Both are now merged:
PR #110, then this item as PR #119.)

## Order and workers

| Worker | Item | Works in |
| --- | --- | --- |
| A | 1 (test, document) and 3 | branch `fix/browser-result-artifacts`, files of items 1 and 3 |
| B | 4 | same branch, files of item 4 |
| C | 6 | same branch, files of item 6 |
| D | 7 | a separate checkout of `feature/browser-shared-live-panel` |
| E | Review of A, B and C together, after they finish | read-only |

A, B and C run at the same time and do not edit each other's files. They do not commit; the lead commits, opens the pull
request against `dev` and merges it when the checks pass. Only the tests for changed files are run locally.

## Not in this plan

- Item 2 of the proposal: an action that answers with what changed on the page.
- Item 5: browser tasks in a small separate context.
- Any change to the leased path beyond item 1.

## Work log

(Each worker's "Built" and "Verified" notes go here.)

### Worker A: items 1 and 3 (2026-11-19)

**Built.**

- Item 1: reviewed the code written earlier; no fault found. `access.py` reads the folder before and after each command
  (`_artifact_state`) and `_artifact_handles` lists new or changed files, never `command.lock`, `agent-browser.json` or
  `*.meta.json`. Tests: `app/tests/test_browser_result_artifacts.py`. Documented in `docs/BROWSER-HANDLES.md`.
- Item 3: `BrowserAccess._native_batch` (called from `_native_command` when `commands` is given) and the `commands` field of the
  `browser_turn` schema in `operations.py` (1 to 10 command arrays, each as long as `command` may be, with an inline
  description). The leased path refuses `commands` with `validation_error`. Tests: `app/tests/test_browser_command_batch.py`.
  Documented in `docs/BROWSER-HANDLES.md`. `handles.py` needed no change: its compact reply already keeps `results` and
  `stopped` and drops an empty `artifacts`; a test pins that. The lease-free path does not go through that function at all.
- The time budget is 60 s. Found in `qorqut/bootstrap/mcp_reconnect.py`: the bridge that every prepared agent talks to gives
  each request `RETRY_WINDOW = 60` and each HTTP request `HTTP_REQUEST_TIMEOUT = 60`. The CLI tool timeouts are 240 s
  (`tool_timeout_sec` in `codex_config.py`, `timeout` in `.mcp.json` from `preparation.py`), so they do not bind. A command
  may take 45 s (`browser.run`), and 5 s is kept back for the hop and the reply, so a command after the first starts only
  while the elapsed time is at most 10 s. The first command is never refused for time.
- Decisions the plan left open: a timeout of the first command is reported (`results: []`, no top-level `exit_code`), while a
  `BrowserError` of the first command raises `browser_failed` as a single command does; `stopped` is also present when the
  last command is the one that failed (`not_run: 0`); `not_run` counts commands that never started; `action.state` is
  `uncertain` after a timeout, `failed` after any other stop, `succeeded` otherwise.
- No version bump of `browser_turn` (it stays `version=2`). No test or recorded fingerprint pins its input schema: the
  `crm_*_fingerprint.json` files record database tables and indexes, and `input_schema_version` is only a number in the tool
  inventory and in the agent configuration fingerprint. The last change to this schema (the `browser_id` pattern) did not
  bump it either.

**Verified** (stand-in runner, this checkout's source, interpreter of `/home/alp/qorqbot-releases/browser-1f6119c/venv`
with `PYTHONPATH` pointing at this checkout; run from `qorqut-code/app/tests`):

- `python -B -m unittest test_browser_command_batch test_browser_result_artifacts test_browser_worker_selection`: 56 tests, OK
  (37 + 10 + 9).
- The artifact tests fail when the before-state is dropped (8 of 10) and when the bookkeeping names are not excluded (all 10).
  The time test fails when the reply margin is removed.
- The `commands` schema passes the registry's Draft 2020-12 check, and the tool-instruction projection keeps its description.

**Not verified.**

- Nothing ran against a real Chrome, the real `agent-browser` or a real MCP bridge; the 60 s figure is read from the code.
- Not run with `python -I` as CI does (that needs the packages installed); the module paths were checked to resolve to this
  checkout.
- The rest of the backend suite was not run.
- The leased path is tested with a stand-in turn queue by calling `_leased_command` and `_turn_command` directly, not through
  a durable assignment in a database.

### Worker C: item 6 (2026-11-19)

**Built.**

- `ordubashy-browser-control/SKILL.md` 9,876 to 4,962 characters (limit 5,000); `managed-browser-control/SKILL.md` 13,180 to
  6,593 (limit 6,600). The front matter is unchanged in both, apart from one managed sentence: "Website sign-in with the owner's
  account belongs to Ordubashy and starts with website_sign_in". Both now have one grouped command list (the item 4 forms),
  the `commands` batch with its `results` and `stopped` fields and when to batch, the rule that a reply lists only the files
  that call wrote (and how to read an earlier one with action `artifact`), and the leased path in one sentence (one command per
  call, `commands` refused, `renew`, `release`, `reconcile`). Cut: the native-subagent exception, `consumer_id` detail, the
  duplicated `observe` paragraph, lease renewal and reconcile detail, the legacy-assignment narrative.
- Which skill is for whom: `managed-browser-control` is the skill of Ordubashy and the permanent workers, so the website
  sign-in procedure and the `agent_browser_manage` duties are only there. `ordubashy-browser-control`, despite its name, is
  the skill of the Learning Applier (`roles/imported-roles/native/roles/*/role.yaml`); it only tells the agent to ask Ordubashy
  to sign in. The `dialog-dismiss` / `dialog-accept` line, which only the managed skill had, is now in both.
- Decisions: the "do not ask twice for authorization already given" sentence is dropped from both files to stay within the
  limits; the rule that the owner must have asked for the exact step before registering, purchasing, posting, messaging or
  changing account settings, and the Signoff before sending or changing anything on a signed-in site, are both kept.
  `--depth=N` of `observe`, `truncated: true` on a batch entry and "an invalid command in the list means none runs" are not
  in the skills (the tool message and the `commands` schema description carry them).
- `tool-instructions/messages.json`: the value of `operations-use-a-browser-over-trusted-mcp-native-callers` gained one
  sentence on `commands` (lease-free callers only; the reply holds `results` and, after an early stop, `stopped`; a leased worker
  sends one command per call). The key and `index.json` are unchanged.
- What the repository required for an instruction change: nothing beyond the text. Three recent commits that changed these
  skills (8c09044b, 10519232, d1927201) changed only the two `SKILL.md` files, with a doc or unrelated code; no version bump
  (the `role.yaml` bump in 0d15f1a0 came with a change to the Ordubashy brief). 6f0eeb59 changed the text of an existing key in
  `messages.json` and no index; `tool-instructions/index.json` lists an entry per key with a source line and gains an entry only
  for a new key (719dedaa, ca57833c, 48fd9df2). `qorqut-agentic/docs/defaults-provenance.json` was not edited: its
  `defaults_sha256` for the two skills (`1d266e22...` and `76985aa6...`) was already out of date before this work (last written
  by ca11a798; 8c09044b and 10519232 did not touch it) and no test reads it. No test pins any skill phrase. Existing installations
  receive the new texts only through a reviewed instruction catalog update and agent preparation (see
  `qorqut-agentic/docs/RUNTIME-INSTRUCTIONS.md`), which is a release step, not a repository change.

**Verified** (interpreter of `/home/alp/qorqbot-releases/dev-1d1d389/venv`, `PYTHONPATH` set to this checkout's
`qorqut-agentic/src`, `qorqut-code/app`, `qorqut-operations`, `qorqut-context` and `dependencies/document-history`; the module
paths were printed and resolve to this checkout; run with `python -B -m unittest discover -s qorqut-code/app/tests -p <file>`):

- `wc -c` on both skills (4,962 and 6,593), both all ASCII; both front matters parse as YAML with `name: qorqut-role-browser-control`.
- `tool_help.ToolInstructions` accepts the new `messages.json` and projects the `browser_turn` description unchanged.
- 95 tests, all OK: test_mcp_discovery_cache (4), test_mcp_catalog_cache (8), test_managed_witness_aliases (7),
  test_browser_worker_selection (9), test_browser_sign_in (33), test_claude_startup_context (7), test_first_conversation (12),
  test_qorqbot_guide (6), test_agentic_error_translation (9).

**Not verified.**

- No agent was run with the new skills; whether a model follows the command list and the batch rules is untested.
- The skills describe `read`, `wait`, `get`, `scroll`, `focus` and the other item 4 forms as the plan specifies them, written
  before the code of item 4 existed; they have not been compared with worker B's final code or its refusal messages.
- The full backend suite and `python -I` (as CI runs it) were not run.

### Worker B: item 4 (2026-11-19)

**Built.**

- `agent_browser.py`: `translate(args, endpoint)` is the whole translation with no side effect; `command()` calls it, then writes
  the config and adds the session options. `normalize_arguments` turns the sent spellings (`open`, `back`, `forward`,
  `keyboard type`, `tab list|new|close|<id>`, `snapshot -s`, `scroll -s`, `dialog accept|dismiss`) into the wrapper's own
  names. `_options` splits arguments against a per-command option table; anything else that starts with `-` is refused in
  every position, as an option value and in the `--opt=value` form. `refusal` names the closed commands and the closest
  supported one (difflib, cutoff 0.75). `standard_input` and `reply` carry `read`.
- `browser.py`: `COMMANDS` gains `read wait get scroll focus`. `validate_arguments` calls `agent_browser.translate` after its
  old checks, so it alone refuses every bad `find`, `wait`, `get`, `read`, `scroll` and the rest; nothing runs and no file is
  written. `run` gains `input=agent_browser.standard_input(args)` and one `agent_browser.reply(...)` line. `run_owner` sends
  the normalised command word to the Focus activity record; without that `open` would be refused there, because `focus.py`
  checks `command in COMMANDS`.
- `read`: one `batch --json --bail` of `get url`, `get title` and the tool's `read` (filter and outline passed on), sent as
  JSON on standard input. The reply is `Address:`, `Title:`, then the text, all within the budget (default 4,000,
  `--budget=` 1,000 to 24,000), with a one-sentence note of what was cut and how to get more. `read <anything>` is refused
  with "Send goto <url> first".
- Beyond the table, on purpose: `find <locator> <value>` without an action is refused (the tool would click); `type` takes
  exactly one argument (the tool's `type <sel> <text>` would type "sel text" into the focused field); `--opt=value` is sent as
  two arguments (the tool ignores the equals form of `--name` and `--selector`); `scroll 500` means `scroll down 500`; `goto`,
  `tab-*`, `focus`, `frame` and `get` check their argument counts; `tab-select list|new|close` is refused; CSS-only places
  (`wait`, `get count`, `snapshot --selector`, `find first|last|nth`) refuse a ref; `wait` refuses any time form that starts
  like a number unless it is whole milliseconds up to 30,000 (the tool reads `+2000` as a time); `eval -x...` is refused.
- Documents: `docs/BROWSER-CONTROL.md` (new section "Command vocabulary" and one line under Validation).
- Tests: `app/tests/test_browser_command_vocabulary.py` (new); `test_browser_frame_command.py` changed in one place (`launch`
  is now "closed on purpose", so the unknown-command test uses `teleport` and a new test pins the `launch` sentence).

**Verified.** Real Chrome 153 (throwaway headless, own profile, ports outside the live ranges, removed afterwards), pinned
agent-browser 0.38.1, driven through `qorqut.bootstrap.browser.run(..., capture=True)`:

- (a) `read` with no address prints the text only: no address, no title. `--json` adds `url`/`finalUrl` and no title, plus
  noise; `--outline` prints "Source: <url>". `--json` works on either side of `read`; `--max-output <n>` cuts with a marker. Hence the batch.
  Through the wrapper `read`, `read --outline`, `read --filter x`, `--budget=1000` and a 45,000-character page all came back
  as address, title and text within the budget; `about:blank` gives an empty title and "(no readable text on this page)".
- (b) `find role button click --name Save` and `find text Save click` on two matches ("Save", "Save draft") do not refuse: the
  first match in page order is clicked and the tool prints "Done". `--exact` narrows it. `--name=Save draft` is ignored
  by the tool (it clicked the first button); `--name Save draft` works. With no action the tool clicks. A name with no
  match fails with the names it saw. `find placeholder` needed the whole placeholder, not a part of it.
- (c) `eval document.title` and `eval "const a = 1; const b = 2; a + b"` work without a wrapper and print the last value
  (`"Page One"`, `3`); `return` at the top is a syntax error; a top-level `const` stays in the page, so the same
  `const x = 1; x` fails the second time; `undefined` prints `null`; an async function is awaited; an uncalled function prints
  `{}`, hence functions are still called.
- (d) `wait` (ms, selector, `--text`, `--url`, `--load`, `--fn`), `get` (all six forms), `scroll` (page and `--selector`
  container, `scroll 500`, `-s`), `focus` (selector and ref), `tab` forms, `back`/`forward`, `keyboard type` all behave as the table
  says. `wait 30000` took 30 s and ended inside the limit; a wait that never resolves ends by itself at 25 s. `wait` and
  `get count` do not understand refs (25 s timeout, count 0); `get cdp-url` does print the browser's `ws://` address.
  Lifecycle options after the positional arguments are honoured by the tool (`--cdp http://127.0.0.1:1` after `find ... click`
  tried to connect there), so the refusal has to hold in every position.
- (e) `snapshot -s h1` and `snapshot --selector h2 -i` scope to the element; `--selector=h2` alone gave the whole page.
- `python -B -m unittest tests.test_browser_command_vocabulary tests.test_browser_frame_command` from `qorqut-code/app` with
  `PYTHONPATH` set to that directory and the interpreter of `/home/alp/qorqbot-releases/browser-f0aaeb8/venv`: 83 tests, OK
  (77 + 6). The same files through `unittest discover -s tests -p <file>`: 77 and 6, OK. `tests.test_browser_setup` and
  `tests.test_browser_worker_selection`: 28 tests, OK. Worker A's `test_browser_result_artifacts` and
  `test_browser_command_batch` import the same module and passed (47 tests) against this checkout.

**Not verified.**

- `read` on pages with frames or shadow DOM; the leased path; a Shared (pinned-tab) browser, which this branch does not have.
- The full suite was not run.
- A batch keeps the last 16,384 characters of each command's `stdout` (item 3). A `read --budget=24000` inside a batch is cut
  at the front, which removes the address and title; as a single command (last 65,536) it is whole. Cap the budget at about
  16,000 or move the cut for `read`, whichever the lead prefers; not changed here.
- Merging with PR #110: it changes the `command(...)` signature and the `subprocess.run` block of `run`, which this change also
  touches (`command` now calls `translate`; `run` gains `input=` and the `reply` line). The two compose: `translate` takes no
  `pinned` argument. Expect a small textual conflict in those two places.

- Review (reviewer E) and fixes: a batch now keeps the start of each command's output, up to 24,576 characters, and marks
  `truncated`; an unexpected exception after the first command keeps the earlier results; `eval` again calls an arrow
  function that has a default-parameter call or a leading comment; the skills and the tool text were corrected as above.
