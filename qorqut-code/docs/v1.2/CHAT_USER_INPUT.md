# Chat user input: `request_user_input` and browser selection

The backend and role contract for the Ordubashy-only `request_user_input` tool and browser selection. The
dashboard half of the feature is not described here beyond the transport contract it consumes.

## Contract

### Product decisions

- One new agent tool: `request_user_input(message, references=[])`.
- References are existing Signoffs, browsers, or Tasks. This is a presentation tool,
  returning immediately. There is no request record, lifecycle, queue, answer token,
  Done button, handoff, lease for viewing, or new approval mechanism.
- Signoff is an interactive card directly below the message using existing Signoff
  rendering and authoritative decision actions. Browser is an actual small iframe
  directly in the chat, not a chip that must be opened. Its optional expand control
  expands that same viewer. Task is a noninteractive title/status chip.
- Ordubashy may reference any configured browser, not just her own. She must also
  be able to control those browsers with the existing `browser_turn` commands.
- Preserve the current dark UI, fonts, Tiny Spirit identities, bubbles, composer,
  navigation and Focus. No wider redesign or browser-engine replacement.

### Minimal transport contract (shared by backend and UI workers)

Tool input is `{message: string, references: [{type, id, business_id?}]}`.
Use the existing UUID format for Task/Signoff IDs; browser `id` is a validated,
configured surface ID. `business_id` is the record's owning/review scope (null or
omitted for Global). For a sandbox business browser whose synthetic surface ID
is shared, its business_id disambiguates it. Reject arbitrary URLs, labels, HTML,
status/decision fields and unrelated fields. Bound message to 4000 characters and
references to 8; a message must not be blank. Keep reference order; coalesce exact
duplicates. No new discovery tool: existing Task/Signoff reads and the browser
tool's list action provide IDs.

Successful tool data is `{user_input: {version: 1, message, references}}`, with
normalized identity-only references. It contains no viewer URL, CDP endpoint,
credential, approval or pretend owner answer. This successful tool result is the
archived presentation source. Native Chat projects it as an ordinary agent message
with the tool's message and `entry.references` below, never as a completed native
reply or a new user turn. Use existing transcript identity, ordering and pagination;
no schema or second message store. Accept provider tool-result envelopes only,
not assistant prose, quoted JSON, failed tool results or arbitrary user text.
Support Claude and Codex, bounded windows, repeated reads and multiple result
blocks without dropping the presentation. Existing outgoing-agent-message entries
and reply/unread accounting must stay correct.

The owner Chat DTO includes resolved `references`:

```
{type:'task', id, business_id:null|UUID, business_slug:null|string,
 label:string, state:string, available:true}
{type:'signoff', id, business_id:null|UUID, business_slug:null|string,
 label:string, available:true}
{type:'browser', id, business_id:null|UUID, label:string,
 viewer_url:string, width?:number, height?:number, available:true}
```

Unavailable/deleted/unauthorized references retain type/id and
`available:false,label:'Unavailable …'` without target details or URL. A missing
reference must not break the rest of the transcript. Resolve current records with
existing permission/read helpers; viewer URLs come only from trusted configured
surface reads at the owner boundary. No model-supplied URL ever creates an iframe.
Do not add per-reference polling. Task metadata can update on normal Chat reads;
Signoffs keep their own existing read/decision refresh behavior.

### Backend and browser selection

Register the presentation operation in the host's existing operation registry,
with normal policy authorization, `read_global_context`, no Signoff support, and
no domain mutation. Restrict the new presentation tool to the configured active
Ordubashy native agent; role policy must grant it. Validate referenced records
through their existing read authorization, including exact ownership/review scope.
Use an ordinary read handler returning the envelope above. The tool itself does
not modify Tasks, Signoffs, Focus, chat inputs or browser state.

Extend existing `browser_turn`, rather than introducing another browser tool:
`action:'list'` returns a small catalog (id, label, business_id) and
`browser_id` selects that exact surface on `acquire` with `protocol:'handles'`.
New cross-surface selection is for the configured active Ordubashy; preserve
existing peers' and assignment-bound access. Unknown/mismatched targets fail.
Subsequent commands use a target-bound handle and cannot silently switch browser.
Reuse existing stateless native handles for configured desktops, including target
identity in their binding/resolution. No new handle store/table/expiry for them.
Business selection resolves to the existing business-browser path and keeps its
current automation rules; adding a chat reference never acquires/releases a turn.

Existing configuration facts: browser-surfaces schema 2 holds trusted viewer URLs,
agents and business mappings. Global configured IDs include `ordubashy-browser`,
`example-browser`, `outlook-browser`. Trusted desktop configs already exist at
`/etc/qorqut-browser-desktops/{ordubashy,marketing,outlook}.json`, with
`chrome.cdp_port`; `app/tools/browser/desktop_session.py` has a safe `load()`
validator. Read those existing operator configs for these managed desktop targets
(test with temporary files); never hardcode observed CDP ports or derive CDP from
a viewer URL. Reuse the installation's browser runtime. Business surfaces use
their existing configured/sandbox resolver. No production config changes,
new registry, service restart, browser launch or real browser interaction.

Add the tool grant and concise usage/examples to Ordubashy's role sources; bump
the Ordubashy package version. Document browser list/selection for Ordubashy in
an Ordubashy-owned brief/skill so other role packages need not change. Preserve
the shared managed browser skill if possible. No publication or adoption.

### UI integration

Carry the backend DTO references through `global-chat.js` into the shared
`chat-view.js` renderer. Mount typed reference content under its message, keyed
by reference identity. Existing file chips/previews must continue working.

Reuse `sgCreateView`'s single-item mode, already used by
`agent-focus.js` around its Signoff detail path. Use a compact inline presentation
matching the approved card; any needed compact option must be narrowly scoped.
Retain the full existing Signoff contract: owner session, fetched exact proposal,
revision/hash, pending state, rejection feedback when required, idempotent decisions,
conflict and lost-response handling, authoritative saved status. Never implement
a second decision client or approve from transcript data alone. Inputs and
nonpending Signoffs still use supported existing behavior.

Browser card: compact current-style frame, about 360px wide/max-width100%, about
210px content height, small label and expand control. Load the exact backend-resolved
viewer URL in a real iframe, with existing browser-frame permissions, sizing and
referrer policy. TigerVNC/Guacamole provides interaction and login. No simulated
browser, screenshot, new input relay, handoff/Done state, or extra authentication
flow. Expanding must preserve the same iframe/session (resize in place is fine);
closing the expanded view returns it inline. Ordinary chat refreshes, Signoff
updates and appending messages must not reload an unchanged iframe. Dispose frames
and handlers on message removal, conversation/agent switch and view teardown.

Task chip has title and state only, no action, click handler or navigation. Missing
references show a small unavailable state. Keep layout usable at narrow widths.

### Focused acceptance

- Tool registration/discovery/policy, valid mixed references, bad IDs/scopes/URLs,
  unauthorized callers; no writes to requests, Signoffs, Tasks or browser turns.
- Synthetic Claude and Codex tool results project once with stable identity;
  errors/plain prose do not. Reload/history preserve references; native completions,
  outgoing messages and unread sequences are not fabricated.
- Mock browser dispatch proves Ordubashy selects own/desktop/business
  targets, commands keep that selection, invalid targets and other caller/handle
  substitutions fail, and iframe presentation does not call browser acquisition.
- Real Chromium with synthetic APIs: mixed reference render; native Signoff
  approve/reject/status/conflict behavior; direct inline iframe; target preserved
  through refresh/append/expand; Task noninteractive; unavailable targets; teardown;
  narrow layout and existing file reference behavior.
- Only focused relevant tests. No full suite, product-agent session, real Signoff
  decision, login, native chat message, live DB read fixture or live API mutation.

## Agent references

- **Agent references.** `{type: "agent", id: "<registered agent UUID>"}` is a fourth reference type. `business_id` is absent or null;
  a non-null `business_id` and any model-supplied name, icon, presentation, HTML or URL are refused. The transcript reference stays
  identity-only. The owner Chat DTO is `{type: "agent", id, business_id: null, label: <current display name>, role: <existing role
  metadata>, presentation: <existing safe agent presentation>, available: true}`; an unknown or unreadable agent is the usual
  unavailable placeholder with no role or presentation. The noninteractive chip belongs to the dashboard worker.
- **Ordubashy reuse guidance** (`agent-roles` `roles/ordubashy/BRIEF.md`): see the Roles notes below.

## Implementation notes

### The tool

`request_user_input(message, references=[])` is one operation in the host's existing registry
(`bazarlyk/native_chat/user_input.py`, registered after Focus in `service/runtime.py`; catalog line in
`composition/catalog.py`). It is an ordinary Global read: `effect_kind="read"`, `read_global_context`, `exposure={"mcp"}`, no
Signoff support, no domain handler that writes. Policy: the host rule `_user_input` (`composition/policy.py`) denies every role
but Ordubashy before overrides, so an explicit grant to a worker or peer is still refused; Ordubashy's role default (`1.63.0`)
allows it. The handler additionally requires the configured active Ordubashy agent and refuses assignment-bound connections.

```
request_user_input(message="Please sign in below, then reply here.",
                   references=[{"type": "browser", "id": "example-browser"},
                               {"type": "signoff", "id": "<uuid>", "business_id": "<business uuid>"},
                               {"type": "task", "id": "<uuid>"},
                               {"type": "agent", "id": "<registered agent uuid>"}])
-> {"status": "ok", "data": {"user_input": {"version": 1, "message": "...", "references": [
     {"type": "browser", "id": "example-browser", "business_id": null}, ...]}}}
```

The contract lives once in `native_chat/references.py` (message 1-4000 characters after trimming, at most 8 references, strict
`{type, id, business_id}` keys, lowercase canonical UUIDs for Tasks and Signoffs, configured surface ids for browsers, order
kept, exact duplicates coalesced). Every reference is resolved through an existing read before the call returns: Tasks and
Signoffs through `FocusService.resolve` (exact business and review scope; Signoffs exist only in a business, so a Signoff
reference needs its `business_id`), browsers through `BrowserTargets.resolve`, agents through the existing `agents_get` read
authorization (`UserInput.agent`: the caller must be allowed that read, a Global agent needs the Global capabilities, a
business-home agent is outside Global; an agent reference takes no `business_id`). Any target that is missing, unreadable or in the
wrong scope is `not_found` ("Reference N (type) is not available"), with no detail about the target. The result carries no URL,
label, state, endpoint, credential, approval or answer, and the call writes nothing (every table is unchanged).

### Chat projection

The successful tool result in the agent's own transcript is the only source (`native_chat/projection.py`):

- Claude: a `user` record's `tool_result` blocks that are not errors; Codex: a completed `McpToolCall` item of server `bazarlyk`,
  tool `request_user_input`, with no error. The text must itself be the JSON success envelope (`status` ok, `data` exactly
  `{user_input}`) and must re-validate to its own normalized form. Assistant prose, user text, quoted or embedded JSON, failed
  results, other tools and any other shape do not project.
- Each presentation is an ordinary `agent` message (`partial: false`, `completion_sequence: null`) with `references` on the
  entry. It is not a completed reply, a user turn, unread work or an outgoing agent-message notice: reply, work and user
  counters never move. Identity is the existing record identity plus `#input<n>` for the n-th result of a record, so ids,
  order, pagination and restarts are the transcript's own. There is no table, store or schema.
- Several results in one record each project, after that record's send notice. `Result` therefore carries `extra` entries and the
  reader never starts or ends a page inside one record offset (`reader._read`), so a one-entry page still returns the whole
  record.
- The owner DTO (`entry.references`) is resolved in `chat.py` inside the one read transaction of the page, once per page, by
  `UserInput.owner_references` with the installation owner's own reach. Task, Signoff and browser entries have the plan's shapes;
  an agent entry is `{type, id, business_id: null, label, role?, presentation?, available: true}` from `store.agent_identity`, the
  very projection (`_recipient`) that names the recipient of an outgoing agent message: the current display name, the role id and
  the safe `icon` presentation, and nothing else (no profile, path, scope, grant or markup);
  a browser's `viewer_url` (and `width`/`height`) comes only from `browser-surfaces.json`, or from a sandboxed business's ready
  owner viewer. Anything missing, archived or unreadable becomes `{type, id, business_id, label: "Unavailable <type>",
  available: false}` and never fails the page.

### Browser list and exact selection

`browser_turn` gains `action: "list"` and `browser_id` (`bazarlyk/browser/access.py`, `targets.py`, `handles.py`,
`coordinator.py`, `operations.py`). `list` returns `{"browsers": [{id, label, business_id}]}`: the managed Global desktops the
registry configures (`ordubashy-browser`, `example-browser`, `outlook-browser`), then every reachable business's one browser
(the sandbox business uses its synthetic id with its `business_id`). It takes no turn, lease or handle.

`acquire` with `protocol: "handles"` and `browser_id` selects that browser:

- Own browser: the same lease-free handle as the default. A named desktop browser: a stateless native handle in which the surface id is
  bound into both hex halves (`native_handle`/`native_target`), so the handle itself names the browser; there is no store,
  table or expiry. A different `browser_id` on a later call is refused, as is a handle of another principal.
- Business browser: `browser_id` plus `business_id` is verified against the registry or sandbox, then the existing coordinator
  acquisition runs unchanged (exclusive lease, automation rules, `renew`/`release`).
- Only the configured active Ordubashy may list or select by id (proved afresh on every call, including later commands on a
  desktop handle); peers keep their own-browser and business-by-id access, and assignment-bound workers never pass `browser_id`.
- A desktop's CDP endpoint is `http://127.0.0.1:<chrome.cdp_port>` from `/etc/qorqut-browser-desktops/<name>.json`, read at each
  command with the operator helper's own `load()` (`app/tools/browser/desktop_session.py`, executed in memory so nothing is
  written beside an immutable source). Nothing is hardcoded and a viewer URL is never a CDP source. The browser runtime is the
  installation's. A chat reference never calls any of this.

### Roles

`agent-roles` (`v1.2/chat-user-input`, commits `68198fc` and `044e007`): Ordubashy `1.63.0` (unreleased; not published or adopted)
grants `request_user_input`, selects the new Ordubashy-owned `skills/chat-references/` skill (usage, examples, agent references,
`browser_turn` list/selection) and adds a one-line brief pointer. The shared managed/original browser skills and every other role
package are untouched.

**Worker reuse guidance.** `roles/ordubashy/BRIEF.md` opens its team guidance with a compact
replacement of the old team opening (it replaces that opening and is not an added paragraph):

> Ordubashy owns owner dialogue, plans, escalations and delivery. You represent the owner. Check agents_activity_list first. Reuse suitable
> workers across kinds of work; names and past work do not fix specialties. Give each one atomic active assignment at a time, then
> choose its next. Create workers only when reuse is unsuitable or more parallel capacity is needed.

The access, grant, owner-authorization, delegation, protected-action and stop/pause/reassignment guidance after it is unchanged, so
the instruction grants no new permission. The brief was already at its 10,000-character limit (base 9,998); the new opening and the
brief pointer for agent references fit it (now 9,995) through wording-only trims of the Focus and Calendar paragraphs. The
limit (`catalog/packages.py`) is unchanged and no Calendar skill was added. The three existing role tests that pinned the old opening
(`test_global_worker_role.py`, `test_founding_plugin.py`) now pin the new one; no permission or approval assertion changed and no
prose-mirroring test was added. The README release note records the change.

## Focused checks

Run against the assembled synthetic host with a task-owned virtual environment outside Git. No browser, model, provider,
service or network is started and no operational data is read.

- `tests/test_request_user_input.py`: registration, policy denial for every other role, valid mixed and agent references,
  bad/extra fields, unavailable targets that reveal nothing, bounds, no browser turn, no table written.
- `tests/test_native_chat_user_input.py`: Claude and Codex projection, identity, pagination and repeated reads, several
  results per record, no reply/unread accounting, exact-shape only, read-time owner DTOs for Task, Signoff, browser and agent,
  unavailable placeholders, sandbox viewer.
- `agent-roles` `tests/test_chat_references.py` for the role-side reference format.
- `tests/test_browser_target_selection.py`: `action=list`, exact selection, target-bound handles, Ordubashy-only
  cross-surface access, peers and assignment-bound access preserved, operator desktop configs read from temporary files.
- Directly affected integrations: `test_native_chat_projection.py`, `test_native_chat_chat.py`, `test_native_chat_reader_cache.py`,
  `test_native_chat_lineage.py`, `test_native_chat_transcripts.py`, `test_native_chat_capture.py`,
  `test_browser_turn_global_eligibility.py`, `test_browser_desktop_session.py`, `test_host_foundation_policy.py`.

## Limitations

- The operator desktop helper (`app/tools/browser/desktop_session.py`) is loaded by executing its source in memory, because the
  immutable source must not gain bytecode; only its `load()` is used, and tests give it temporary configs.
- `browser_turn action=list` names only the browsers that can be controlled: the configured Global desktops and each reachable
  business's browser. A reference may still name any configured surface Chat can show.
- A retired Global agent still resolves under `agents_get` semantics (the archival profile is readable), so a reference to it shows its last
  name; a business-home historical agent is unavailable at Global scope.
- This repository change has no dashboard or browser rendering work; the dashboard worktree and the gitlinks are untouched.
- The test environment is not part of the repository.
