---
name: qorqut-role-telegram-owner-communication
description: Talk with the owner in your own Telegram topic, ask for blocking input including secrets, send one authorized file or photo when your role grants telegram_send_file, read your recent topic history and check delivery; how this differs from Signoffs.
---

# Telegram owner communication

You have one private Telegram topic with the owner. The owner's Telegram group is
one private forum with a separate topic for each conversation (yours, the other
agents', Signoffs and Urgent Notifications), so the other topics
are not yours and you never see them. The application chooses your topic;
you never supply a chat, a topic, a token, your identity or a formatting mode, and only
`telegram_send_file` names a source (a file in your own workspace or a document you can read).
Every owner message in your topic reaches you as a direct message in your inbox
(and, when your terminal is idle at an ordinary empty prompt, is typed into your
session). Read it like any direct inbox message and acknowledge it when handled.
A Telegram message is scoped owner input for the matter it addresses. It is not
blanket authorization, does not approve a prepared action and does not widen your
grants.

## Which channel

- Use `telegram_send` to answer the owner, to report progress they asked for, or
  to say you are blocked and cannot continue without them.
- Use `telegram_send_file` only to deliver a finished file or photo the owner
  asked for or your work covers (below); it exists only if your granted tools
  include it. Text, questions and blocking input always use `telegram_send`.
- When the owner asks in a task, Focus, parent Task, Signoff or any other thread,
  post the substantive answer in that original thread using its native
  reply/comment tool. For an `owner_conversations` event, use
  `owner_conversation_send` with the original source; keep that private answer
  out of the object's team discussion. Keep the answer in that thread. When the owner
  needs a separate notification, send a short pointer with a source reference through
  `telegram_send(purpose="reply")`. If the request
  already came through your own topic, reply there once. Never copy secrets.
- Use Signoffs for approvals of prepared actions, durable questions and anything
  that needs an auditable decision. Signoffs keep their own tools and rules
  (owner-input skill). This skill only overrides how the owner may respond to a
  Signoff from Telegram, described below.
- When you are blocked, say so once and continue independent work. Do not repeat
  the same message for each retry, and do not poll.
- Agents do not write to Urgent Notifications; the application posts those.

## telegram_send

Arguments: `text` (plain text, 1-4000 characters, nothing is interpreted as
formatting), `purpose`, and optionally `reply_to`, a local `message_id` from
`telegram_history` in your own topic. A message from any other topic is refused. The framework `idempotency_key` is required
on every send: reuse it only to retry the identical send.

`purpose` is one of:

- `reply`: answers what the owner wrote.
- `update`: reports progress or a result.
- `blocking_input`: you cannot continue without something from the owner. It
  also needs `request_key` (a stable name for this one outstanding need),
  `reason` and `input_mode`; optionally `expires_in_seconds` (60-3600). The
  bridge adds the reason, the expiry and how to answer under your text, so put
  only the question itself in `text`.
  Sending the same pending `request_key` again returns the existing request; a
  materially different question under a pending key is refused, so wait for it to
  end (answered, expired, or cancelled because it could not be sent) and use a new
  key. Respect existing answers: read `telegram_history` before asking again.

`input_mode`:

- `text`: the owner answers in the chat with ordinary text.
- `secret`: the owner will type a secret such as a one-time code. It expires
  within 10 minutes (the default and the maximum) and only one secret request may
  be outstanding in your topic. Ask for exactly one value, never ask for a
  long-lived credential when a login link or an approved flow exists, and never
  repeat a secret you received in any message, note, file or command output.
  The value is delivered straight to your terminal input when it is safe; if it
  cannot be delivered now it waits until it expires. A code the owner sent while
  the request was open but that could only be read after it expired is discarded,
  never delivered late and never treated as an ordinary message. It is never
  shown to you in `telegram_history`. Use it immediately, then discard it.
- `screen_action`: the owner must do something on a screen (for example approve a
  login on a device, or type a code into the actual browser) and reply when done.
  No secret is sent to you. After the owner says done, verify the result yourself
  (the page state, the login, the check you were blocked on) before you continue;
  their reply is not proof.

The result is the local `message_id` and its delivery state. `queued` at return
is normal; the bridge sends shortly after. A file send returns the same receipt with its
attachment metadata (`telegram_send_file`, below).

## Delivery states

Read `telegram_delivery_get` (one `message_id`, or your ten latest sends). States
are `queued`, `dispatching`, `sent`, `failed`, `cancelled` and `uncertain`.
`uncertain` means the request may or may not have reached Telegram (for example a
timeout after sending, or a response that did not confirm your topic, so it may
even sit in another topic). Never assume either. Do not resend the same message (or file):
the owner reconciles it, and a duplicate would be indistinguishable from a real
second message. Report an uncertain or failed send to the owner through the
channel they are watching (a Signoff comment or your next reply) when it matters.

## telegram_send_file

Sends one file to your own topic, as a downloadable document or a displayed photo. Loading
this skill grants no tool: use it only when `telegram_send_file` is among your granted
tools. The application accepts it only from the currently configured, active Global
Ordubashy bound to the owner's topic; a role name or an added permission on any other
agent is not enough, and every other caller is refused. Even then it sends only files and
documents that agent may read. If it is missing or refused for a missing grant, source,
format or size, the refusal is explicit: tell the owner in text what could not be sent
and why. Never look for another route: no owner web link or HTTP call,
no bot token, no URL, no other topic.

The arguments are a closed set; any other field is refused. The framework `idempotency_key`
is required on every call, and a file is sent once per call (no albums, edits or multiple
files). You cannot supply a chat, a topic, raw bytes, base64, a Telegram file id or a URL.

- `source`: exactly one of two shapes.
  - `{"type": "workspace", "path": "exports/report.pdf"}`: a regular file in your own
    workspace, as a relative POSIX path inside the `exports/` or `downloads/` subtree
    only. Absolute paths, `.` or `..` segments, empty segments, backslashes, control
    characters, hidden (dot-leading) names and the application's internal receipt names
    are refused, as are directories, symbolic links, special files and files with more
    than one hard link.
  - `{"type": "document", "document_id": "<document-id>", "business_id": "<project-id>"}`:
    a stored document you can already read through its own tool. `business_id` is
    required: the owning project, or `null` to name Global explicitly. Supported kinds
    are assets (`assets_get`), data uploads (`data_uploads_get`), agent and project
    notes (`notes_get`) and content or context notes (`content_get`). Every other kind is
    refused, including owner journal entries, working memory, raw email
    and Context graph or history data. The project must be active, the document ready and
    not archived, and your read access is checked again when the send is queued and again
    just before upload; lost access cancels the send.
- `expected_sha256`: required, exactly 64 lowercase hexadecimal characters, the SHA-256 of
  the exact bytes you mean to send. Take it from the actual source: for a workspace file,
  the digest of the finished file (for example the output of `sha256sum`); for a
  document, the content hash its reader returns for the version you read, without any
  `sha256:` prefix. The application copies the bytes, compares the digest and refuses a
  mismatch. If the source changed or a hash looks stale, read it again deliberately, confirm
  it is still the deliverable, recompute the digest and send as a new intent (fresh key).
- `send_as`: required, `document` or `photo`. Choose `document` for the original,
  downloadable file or image (1 to 50,000,000 bytes, bytes preserved). Choose `photo` only
  to display a static JPEG or PNG (1 to 10,000,000 bytes, a single frame, width plus height
  at most 10000 pixels, the longer side at most 20 times the shorter); Telegram may process
  a photo. A photo that fails these checks is refused: it is never converted, resized or
  silently sent as a document, so decide the mode yourself.
- `purpose`: required, `reply` or `update`, as for text. `blocking_input` is not available
  for files; ask the question with `telegram_send`.
- `caption`: optional, empty by default. Plain text, nothing is interpreted as formatting,
  valid Unicode with no NUL or inappropriate control characters, at most 1024 characters and at
  most 1024 UTF-16 code units (characters outside the basic plane count as two).
- `reply_to`: optional, a local `message_id` from `telegram_history` in your own topic that
  Telegram has already accepted. Any other message, including one from another topic or one
  not yet sent, is refused before anything is queued.

The filename is the source's own basename (at most 255 UTF-8 bytes, no separators or control
characters); there is no argument to rename it. Examples (the angle-bracket values are
placeholders you replace; the digest is 64 hex characters):

```json
{"source": {"type": "workspace", "path": "exports/report.pdf"}, "expected_sha256": "<sha256-hex>", "send_as": "document", "purpose": "update", "caption": "Weekly report attached."}
```

```json
{"source": {"type": "workspace", "path": "downloads/chart.png"}, "expected_sha256": "<sha256-hex>", "send_as": "photo", "purpose": "reply", "reply_to": "<message-id>"}
```

```json
{"source": {"type": "document", "document_id": "<document-id>", "business_id": null}, "expected_sha256": "<sha256-hex>", "send_as": "document", "purpose": "update"}
```

### Sending a file

1. Finish and check the deliverable the owner asked for or your work covers. Put a file you
   generate under `exports/` (one you fetched belongs under `downloads/`). Sending a file
   needs no extra approval step, but it is not approval of anything either, and it must never
   carry a secret, a credential or content you were not meant to share. A document being
   readable to you does not make it yours to send.
2. Choose the mode: `document` for the original file or an image the owner should download
   unchanged, `photo` for a static JPEG or PNG meant to be seen in the chat.
3. Compute `expected_sha256` from the source as described above.
4. Call `telegram_send_file` once with a stable `idempotency_key` for this intent. Reuse the
   same key only to retry the identical call after a lost response; a duplicate key returns
   the original receipt. Any change of source, digest, mode, purpose, caption or reply target
   is a new intent and needs a fresh key (the same key with different arguments is a conflict).
5. The result gives the local `message_id` and the delivery state, with the attachment
   metadata (filename, mode, size and sha256). `queued` means accepted for sending, not
   sent: tell the owner it is queued, never that it was delivered. When confirmation matters,
   read `telegram_delivery_get` once later instead of polling; history and receipts name the
   file by its filename and never expose its bytes or where the application keeps them.
6. At queueing the application seals a private copy of the validated bytes, so later edits,
   removal or replacement of the original file do not change what is delivered. A corrected
   file is a new send with a fresh key; there is no tool to edit or recall a file already sent.
7. If the outcome is `uncertain`, do not resend it, retry under a new key or switch to the
   other mode: the owner reconciles it and the original bytes and destination stay frozen.
   Tell the owner through the channel they are watching. For `failed` or `cancelled`, report
   that it was not delivered and why; send again only as a new intent with a fresh key once the
   cause is fixed. A refusal for size, format, a missing source or grant, a full media store or
   too many waiting file deliveries affects only that file; `telegram_send` keeps working.

## telegram_history

`telegram_history` returns up to your last 10 messages in your own topic, oldest
first, starting at activation, and only those recorded for you in that topic.
Secret replies are redacted. It never includes Signoffs, reports, notifications,
other agents' topics, what an earlier holder of your topic's binding was sent, or
anything from before the owner moved your conversation into a topic. Use
it to recover the thread when you resume, not as a log.

## What the owner can send

- Text only. A message with an attachment reaches you as its caption plus a marker
  that the attachment was not delivered; files, images and voice are never
  downloaded, so ask for the content as text. File sending is outbound only.
- Text over 16000 characters or containing terminal control characters is refused
  with a notice to the owner and never reaches you.
- A reply to one of your messages carries the excerpt and the local id it answers,
  and the request context when it answers a `blocking_input`. A reply never
  redirects to another agent: every message in your topic is for you, and a
  reply that points at a message from another topic is refused and never
  reaches you. Messages the owner writes in General or in any other topic never
  reach you either.
- Messages can arrive while you are busy or offline; the inbox holds them. A
  terminal you were not running is never started for a Telegram message.

## Signoffs from Telegram

The owner sees each eligible Signoff as its own post in the Signoffs topic and can
react with a thumbs-up or thumbs-down or reply there. A reaction counts only on such
a post, and a reply must be in that topic to that post. Approval and rejection then follow
the normal canonical Signoff rules; you learn the outcome from your normal inbox
and Signoff reads. A reply to a post that is an action approval is an ordinary
comment, not an approval. A free-text reply to an owner-input post is a note routed
to you (your `signoffs_notes_list` and inbox), and the request stays pending unless
the notes alone satisfy it; a question that requires a choice is never answered by
written text alone. The same reply is also typed into your terminal, once, when it
is next idle at an ordinary prompt: this never expires, so a reply can reach you long
after it was sent. Handle the inbox copy once, and treat neither as approval. If you
were not active when the owner replied, the reply is held and delivered when you are.
The link the owner sees only opens the Signoffs view, not one proposal. Never treat the absence of a Telegram reaction as an
answer, and never re-ask the same Signoff in Telegram. Respect snooze: a snoozed
Signoff is not re-announced anywhere until it is due, so do not repeat it in your own
chat while it is snoozed.

## If the owner binding changes

The application pins the installation owner and your topic. If the pinned owner or
installation no longer matches, every Telegram tool refuses with
`owner_binding_mismatch` (or `installation_binding_mismatch`) until the operator
reconfigures it; a message accepted under an older binding is not delivered under a
new one. The same holds when the operator moves your conversation to a different
topic or group: what was queued for the old destination is closed, never redirected,
and your history starts fresh there. Do not retry in a loop and do not look for
another route to the owner's chat or topic (you cannot create, rename or close a
topic, and there is no tool for it): say that Telegram is unavailable through the
channel the owner is watching and continue independent work.

## Limits to respect

- Topics share their group's membership, so the owner and the bot are the only people
  there, but a topic is not a separate room. Telegram is not end-to-end encrypted for
  bot chats and your client may retain
  transcripts of what you type or receive; keep secrets to the minimum, and do not
  ask the owner to paste anything you could obtain through an approved flow.
- The application deletes only messages it knows, within Telegram's 48-hour limit;
  older ones need manual cleanup by the owner.
- A file you send is stored by Telegram like any message. Keep secrets, credentials and content
  outside the authorized deliverable out of files and captions.
- A send is never repeated automatically after an uncertain outcome, and there is no exactly-once
  guarantee: an uncertain send may or may not have reached the owner.
