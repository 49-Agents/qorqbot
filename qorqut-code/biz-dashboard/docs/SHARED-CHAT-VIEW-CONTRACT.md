# Shared Chat view contract

This specifies presentation,
not a transport abstraction. The view contains no API, role policy, peer lookup,
native binding, ordering policy, request state, read acknowledgment or subscription.
Use plain scripts and the existing safe-links helpers. No new framework.

## Entry point

`mountChatView(host, options = {})` creates one shared view and returns:

- `render(patch, {scroll = 'auto'} = {})`: patch the supplied presentation fields;
  omitted fields retain their last value, arrays replace their entire field.
- `setDraft(text)`: silent update, no onDraft callback; avoid changing selection or
  caret if text is unchanged. `getDraft()` returns current text.
- `focusComposer()`; `getScrollState()`; `setScrollState(saved)`; `layout()`;
  `scrollToBottom()`; `isAtBottom()`; `getVisibleKeys()`; `dispose()`.
- `root`: view DOM root. `elements`: stable named DOM nodes `scroll`, `log`,
  `pending`, `composer`, `input`, `send`, `older`, `jump`, `banner`, `empty`.
  Controllers may read these for existing integration; view owns their rendering
  and event listeners. No caller builds another message/composer DOM tree.

Options:

- `logLabel`, `composerLabel`, `placeholder`, `maxLength`, `olderLabel` strings/
  number. Defaults: Conversation, Message, Message…, 20000, Load older messages.
- `composerHost`: optional DOM element for the same shared form outside the feed
  root, needed by Focus's existing collapsed inline composer. Omitted means form
  lives in the shared root. This does not select another style or renderer.
- `bannerHost`: optional DOM element that hosts the same single shared banner outside
  the feed root. For a collapsed inline composer use its composerHost, so send/retry
  failures remain visible. Append the banner after the form, remove it on disposal,
  and keep banner styling/ARIA shared; never duplicate the banner DOM.
- `recordReferences`: record threads only. `true` draws related Task/Action
  `referenceOnly` entries as compact attachment rows, grouping Task runs in a left-ruled
  list and Action runs in a collapsed count. A message's own Task/Action references
  remain attachment rows inside that message.
  Omitted keeps the native Chat/typed-reference presentation. Never infer this option from
  entry shape.
- `sourceTitle`, `sourceKey`: presentation-only record-thread fields. The supplied title
  names the record whose thread this is in the list's accessible group name (fallback
  `Related work`); `sourceKey` plus the run's first entry key is the local group identity.
  Neither is fetched, read from the DOM, or persisted.
- `footer`: optional existing DOM element placed immediately before the composer,
  for native session status. Wrapper owns its contents and native session controls.
- `onFiles(files)` optionally enables the file picker beside Send, clipboard image/file
  paste in the input, and file drops on the host or an external composer. It receives browser File objects; the controller owns selection and uploads.
  `onAttachmentRemove(key)` removes one pending choice through the controller.
  `composer.attachments` contains `{key,name}` labels, and `composer.filesDisabled`
  locks their controls during a send. No upload occurs inside the shared view.
- `onDraft(text)`, `onSubmit(text)`, `onOlder()`, `onViewportChange()`,
  `onReference(value, entryKey)`, `onEntryAction(actionKey, entryKey)`,
  `onPendingAction(actionKey, pendingKey)`, `onEmptyAction(actionKey)`.
  The view does not await or interpret transport results. Controllers catch errors.
  onReference receives references[i].value itself, not the descriptor containing
  value. No callback after dispose. Hidden/disconnected feed reports no visible keys.

`elements` may be read by tests or retained wrapper hooks, but controllers should
use setDraft/render for updates. No backend-specific objects are passed as view
options. Initial rendering must not call callbacks during construction.

The composer auto-sizes on input, `setDraft`, reveal and width changes: one line through
200px, then vertical scrolling, without a manual resize handle. Multiline text puts
the existing actions below the input. Unchanged paints retain the caret and input
scroll. Normal Send uses an accessible arrow button; retry labels stay explicit.

## Presentation model (fields accepted by render)

```
{
  entries: [Entry],                    // already in canonical controller order
  pending: [Pending],                  // already filtered to exact conversation
  composer: {
    hidden: false,
    inputDisabled: false,
    sendDisabled: false,
    sendLabel: 'Send',
    placeholder: 'Message…',
    ariaLabel: 'Message',
    maxLength: 20000
  },
  older: {visible: false, disabled: false, label: 'Load older messages'},
  banner: {text: '', role: 'status'},   // role may be alert for thread errors
  empty: {text: '', action: null}      // optional action {key,label}
}
```

Entry:

```
{
  key: 'stable opaque id',
  speaker: 'user' | 'agent' | 'notice',
  text: 'full original body',
  timestamp: 'original ISO timestamp',
  partial: false,
  position: 12,                       // optional native test/DOM metadata, not sorting
  author: {name: 'Marketing', avatarHtml: '<svg …>'}, // optional trusted local glyph
  showAuthorName: true,               // false keeps the avatar and accessible name without a visible name
  showAuthor: true,                   // false can suppress repeated group headers
  title: 'meaningful non-default title', // optional
  notice: {label: 'Inbox nudge', verified: true, inputType: 'inbox_nudge', markerOnly: true},
  references: [{key:'ref0', label:'Task', disabled:false, value: opaqueReference}],
  actions: [{key:'parent', label:'About an earlier entry', disabled:false}],
  typedReferences: [TypedReference],  // optional; agent entries only (see below)
  referenceRow: 'task' | 'action',    // optional controller provenance for a referenceOnly row
  activity: {preview, author, time, dateTime, attribution} // optional canonical progress note; never a message bubble
}
```

Typed references (native Chat `request_user_input`): the controller passes the backend's resolved, identity-only
DTOs (`{type:'signoff'|'browser'|'task'|'agent', id, business_id, label, available, state?, viewer_url?}`), drawn under the
message in order, keyed by `type:business_id:id`. A `browser` becomes a real iframe (360px max, 210px content) on the exact
`http(s)` `viewer_url` with an Expand control that resizes the same iframe in place (Escape or × returns it inline); a resolved
`task` is the same flat attachment row as a record thread — only the shared status ball and the title, with the state word kept
as clipped accessible text rather than a visible label, the whole row opening the Task through `options.onTask` when one is
supplied; consecutive Task references share one `.gchat-attachment` list with the same subtle left rule and inset. An `agent`
(`label`, `role`, `presentation`) is a non-interactive chip of the shared `agentGlyph` icon and current name, nothing else;
anything with `available:false` or a non-http(s) URL is a small inert unavailable chip.
A `signoff` is mounted through `options.mountSignoff(ref, host)` (returns a disposer or `{dispose}`); the controller
(global-chat.js) uses `sgCreateView({inline:true, item})`, the existing single-item Signoff view with its own owner
session, exact proposal, decisions and conflict handling. The view keeps each item's node (so an iframe never reloads)
across repaints, appended messages and unchanged re-reads, and disposes frames and Signoff views on message removal
and `dispose()`. Focused check: `PLAYWRIGHT_MODULE=… CHROMIUM_EXECUTABLE=… node tests/test_chat_references_browser.js`.

A `referenceOnly: true` entry (record threads: related tasks, Signoffs and other records) has no body, author, time or copy
button. It draws only its `typedReferences` (through the same typed components, with the same keyed hosts) and its
`references` buttons. With `recordReferences`, an entry whose controller marks `referenceRow:'task'|'action'` is one flat
attachment row instead: the Task keeps only the Focus status ball, its title and its existing Task opener; the Action keeps the
existing activity icon, shows `Action` plus only a supplied known state (never an invented Done) and opens through the ordinary
reference opener and canonical value. `available:false` stays an inert disabled row.
Consecutive Task rows form a flat attachment list. Consecutive Action references and
canonical `activity` entries form a collapsed **n actions** disclosure, using the same text
styling as Inbox nudge. Other entries end the run, preserving message boundaries. Expanding
reveals compact rows with the original text and authorized reference controls. Disclosure
state survives refresh and pagination; the view never writes a synthetic message or unread state.

Group containers and rows use `.gchat-ref-group`, `.gchat-ref-rows`, `.gchat-ref-row` and `.gchat-reference-row`; action
counts add `.gchat-action-group`. A native message's Task rows use `.gchat-attachment`; message-attached record-thread
rows use `.gchat-ref-group-inline`. Entries stay the same keyed nodes inside their group. The controller mounts the shared
Signoff card through `sgChatMount(ref, host, options)` (js/signoffs.js), which native Chat and `conversationMount` both call;
a thread omits the card's nested channel composer when its channel is the enclosing thread (`omitConversationFor`).

`notice` is supplied only from controller metadata, never inferred from text.
An outgoing agent message adds `notice.agentMessage: {recipients: [{id, name, glyphHtml}]}` with
`label: 'Messaged'` and `markerOnly: true`. The view draws `Messaged [recipient glyphs] N Bots`
(omitting the count for a single recipient) inline in the notice body: glyphs overlap slightly, names are escaped into
`title`/`aria-label`, and `glyphHtml` is only the locally generated agent glyph. The native controller
projects the backend `agent_message` DTO and merges adjacent entries into one row (first key and
position, distinct recipients in first-seen order); the view neither groups nor adds clicks.
All notices share one style: centered, muted, no bubble, border or badge frame; unverified notices
keep their label in the danger colour.
`verified` and `inputType` are native controller provenance; ordinary thread system
notes omit these fields and retain their body. A controller may supply a plain
notice label, but that never becomes verified. The native label
mapping lives in the controller adapter, preserving verified and unverified behavior.
Author strings, titles, text and labels are escaped. avatarHtml is only existing
locally generated glyph markup, never a server-authored HTML fragment.

User text remains plain/pre-wrap. Agent text uses the extracted current main Chat
markdown renderer and safe references/previews, with partial entry chips deferred
until complete. Notice text remains plain. Every valid timestamp uses the same
browser-local format above the first entry of a run; gaps below one hour between consecutive entries suppress repeated times regardless of speaker. Missing/invalid times render nothing and break the run. Grouping is recomputed after history prepends and edits. Message copy controls copy original text only, with a fallback for the private HTTP host. Titles and authors
are optional shared primitives. No grouping container with alternate body styles.
Keys preserve nodes/focus when identical entries replay; changing an entry updates
its contents. View does not sort or infer an entry's authority from its author name.

Pending extends Entry with presentation fields `state`, `stateLabel`, `inputType`,
`resendable` and `actions`. Its key is the input ID; pending actions use
onPendingAction. Native controller alone computes receipt visibility, state labels,
resend/dismiss eligibility and disabled flags. Existing `.gchat-pending`,
`data-input-id`, `data-state`, `data-input-type`, `data-resendable`, `[data-resend]`
and `[data-dismiss]` hooks remain for the native tests and wrapper. This is generic
rendering of normalized state, not native resend policy in the view.

## DOM and styles

Keep the established `.gchat-scroll`, `.gchat-log`, `.gchat-msg`, `.gchat-user`,
`.gchat-agent`, `.gchat-notice`, `.gchat-body`, `.gchat-text`, `.gchat-time`,
`.gchat-composer`, `.gchat-send`, `.gchat-jump`, `.gchat-older`, `.gchat-banner`,
`.gchat-pending-list` and preview class names for shared markup. Shared root also
has `.chat-view`; native outer `.gchat[data-global-chat]` remains controller-owned.
Entries retain data-id=key and optional data-position. Visible UI never prints IDs.
Thread-specific feed/composer/bubble classes are retired; update consumers and tests
rather than maintaining a second set of presentation rules.

Consolidate current global-chat.css message/composer/preview rules plus effective
agent-workspace.css Chat overrides into chat-view.css. Native-only header/session/
Terminal diagnostic shell rules remain in global-chat.css. Shared form typography
also applies when composerHost places it outside `.chat-view`. No ancestor-specific
shrinking in Focus. Preserve channel strip/button styling separately.

Wrappers set container dimensions and optional `--chat-top-inset` (default 0; native
floating header uses its existing reserved space). Jump/preview layout must depend
on the shared view/composer geometry, not native-only hardcoded offsets. Preserve
current main Chat appearance including its boxed composer, gap22px, padded 24px-radius message bubbles, line-height1.65
message text, 13.25px message type, 15.75px composer type and 36px Send button. Parent container may constrain width
and height; text wraps, feed scrolls, composer stays reachable. Do not enlarge or
redesign the outer Focus pane/dock to mask overflow.

## Scrolling, lifecycle, and input

- `render(...,{scroll:'auto'})` follows bottom only when already there (first view
  defaults to bottom); otherwise preserve visible anchor and show new-message jump
  for new appended entries. Updating partial text is not a new appended message.
- `scroll:'preserve'` retains current position; `'prepend'` retains keyed visible
  anchor/offset when loading older; `'bottom'` moves to latest explicitly.
- getScrollState returns opaque `{top, atBottom, anchorKey, anchorOffset}`.
  setScrollState restores it and suppresses initial auto-bottom. Capture actual
  geometry only while visible. Hidden calls retain the last real state and defer
  restoration until `layout()` after expand; use no body-wide observer per view.
- getVisibleKeys returns rendered keys intersecting the visible viewport only when
  connected, shown and not inert. Native controller combines this with its existing
  document/modal/bottom guard and completion sequence. It owns its visibility
  observer. Threads retain existing ack policy; do not wire this callback to ack.
- View owns form submission and keyboard. Enter sends; Shift+Enter is newline;
  Ctrl/Meta+Enter also sends, including with Shift; IME composing never submits.
  inputDisabled locks editing; it does not forbid an enabled Retry Send button.
  Form submission is blocked by hidden/sendDisabled, independently of inputDisabled.
  Honor independent inputDisabled/sendDisabled and hidden states. Controller owns
  whitespace validation, draft storage and pending request semantics.
- Read-only view hides the entire form, never a disabled invitation to write. Hidden
  form DOM may remain for lifecycle consistency. Acceptance checks assert no visible
  composer and no dispatch from a hidden form, not absence of every hidden textarea.
- Preview uses existing safe targets, image/iframe protections, focus restoration,
  close/Escape semantics and iframe release. dispose removes every owned listener,
  preview, callback and external form; never removes wrapper controls.
- No timer for relative timestamps and no endpoint calls from view code.

## Wrapper preservation

Thread wrapper retains its channels and conversationMount public API, source key,
peer pinning, state Map, sibling mounts and load-driven ack. Preserve the optional
openParent affordance only when its existing callback exists. Keep non-chat uses of
messagePreview/conversationAge/name/glyph helpers. Thread errors use fixed text
(Couldn't load. Try again; Couldn't confirm delivery. Retry Send; Conversation
changed. Review it before sending again; Can't send here) selected by existing error
codes; keep exact recovery semantics. Do not expose raw error messages in this UI.

Action wrapper preserves revision selector, Follow, read-only notice and exact
thread cursor. Shared author label includes existing recorder attribution. Register
and dispose each mounted view before list replacement; late responses must not
paint detached/replaced content. Closing/reopening details preserves scroll via
layout, without introducing any new read acknowledgment.

Typed Task references use `onTask(value, entryKey)` when supplied, or `onReference(value, entryKey)` when `onTask` is omitted. Set `onTask: null` to keep Tasks informational while retaining other reference callbacks. The current reference is passed on activation; unavailable references are not actionable.
