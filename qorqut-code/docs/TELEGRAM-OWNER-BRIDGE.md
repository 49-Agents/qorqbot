# Telegram owner communication bridge

One service-owned bridge connects the owner's private Telegram conversations to Qorqut. Agents use native tools, other local
programs use a restricted socket, and the owner's replies and reactions reach the existing canonical records. This guide is the operating reference.

The bridge is **inert by default**. Source availability, migrations 106 and 107, and role source changes do not enable it.
Enabling needs a private configuration file, an explicit release, and an owner-approved acceptance test (see
[Activation checklist](#activation-checklist)).

## Layout and routes

The intended layout is **one private forum supergroup with three topics**, containing only the owner and the bot. Topics share
the group's membership; they are not independently private rooms. The three destinations are fixed. A destination is the exact
pair `(chat_id, message_thread_id)` plus its logical name (and, for Ordubashy, its agent binding): everything the bridge
saves, sends, edits, deletes or routes names that pair, never the chat alone. **General (topic 1) is outside every bridge
route**: anything said there is ignored and keeps no text. Hide it during setup if that helps.

The earlier layout of separate chats still loads (a binding with no `message_thread_id` is a whole-chat destination, exactly
as before), and so does a mixture of unrelated chats and topics as long as no chat is used both ways. Converting is an
operator edit of the private file (see [Forum topics](#forum-topics)).

**Ordubashy is the only agent with a Telegram channel**; workers never have one. A configuration that names any other
agent chat is refused, and an owner message in a topic with no bridge route is ignored with no text kept and no other agent
receiving it.

| Topic | Direction | What travels |
| --- | --- | --- |
| Ordubashy | two-way | Owner text to the Global Ordubashy agent; its `telegram_send` replies, updates and blocking requests |
| Signoffs | two-way | One actionable post per pending Signoff; 👍/👎 reactions and replies; separate non-actionable status notices |
| Urgent Notifications | out | Actionable events from trusted programs (`telegram_notify`, gateway) |

Notices and queued-input feedback stay in the topic they belong to. The bridge runs one long-poller (`getUpdates`, `message` and `message_reaction` only) and one control thread. It never
installs or removes a webhook, never creates chats, and never starts, restarts, or kills a terminal session.

## Private configuration

`QORQUT_TELEGRAM_CONFIG_FILE` names a private JSON file (mode 0600, in a 0700 directory, not a symlink). The bot token lives
in a separate 0600 file named by `bot_token_file`. Neither file is ever returned by an operation, status, or log. Print the
redacted schema with:

    python -m bazarlyk.telegram example

| Field | Meaning |
| --- | --- |
| `version` | Always `1` |
| `enabled` | `false` by default. `true` starts the poller and workers |
| `bot_token_file` | Absolute path of the token file |
| `owner_telegram_user_id` | Exact integer user ID of the only accepted owner |
| `expected_bot_id` | Exact integer bot ID; `getMe` must match |
| `expected_installation_id` | Canonical lowercase UUID of this installation. Required when `enabled` is true; never detected |
| `expected_owner_principal_id` | Canonical lowercase UUID of the Qorqut installation owner (a human principal). Required when `enabled` is true; never detected |
| `dashboard_origin` | Origin used for dashboard links in posts |
| `chats` | Keys `ordubashy`, `signoffs`, `urgent` (exactly these; any other key is refused), each `{chat_id, type}` (`private`, `group`, `supergroup`, or `channel` for `urgent` only). `ordubashy` also carries the Global Ordubashy agent's `agent_id`; no other chat may. In the forum layout every binding is `{chat_id, type: "supergroup", message_thread_id}` with the one group's ID and its own topic ID |
| `chats.*.message_thread_id` | Optional, for a forum topic only: the exact integer topic ID (greater than 1; General's ID, 1, is refused) and only with `type: "supergroup"`. A boolean, string, zero, negative, 1 or an explicit `null` is rejected. Absent means a whole-chat destination |
| `activation_at` | RFC 3339 instant with a UTC offset. Earlier messages, reactions, and notifications are ignored |
| `program_gateway` | Optional `{enabled, socket_path, producers[{producer_id, secret_sha256, peer_uid?, enabled?}]}`; a producer with `"enabled": false` is refused at the socket and its queued sends are cancelled |

Validation rejects duplicate keys, non-integer IDs, duplicate chats, duplicate `(chat_id, message_thread_id)` pairs or agent
bindings, a whole-chat binding that shares its chat with any other binding, a topic on anything but a supergroup, and secret
files that are group or world readable. At enabled startup the bridge verifies `getMe`, each **distinct** chat's type and
privacy (no public username), the owner's membership, and the bot's administrator rights (post messages in a channel, delete
messages in a group). A group that carries topics is verified once for all of them, and must also report `is_forum: true`.
It also checks the registered installation owner and that the bound agent is an active Global agent holding the exact
Ordubashy role. A changed owner, bot, agent or topic binding fails closed and never retargets queued
work. Each receipt stores the configuration generation and its exact destination.

    python -m bazarlyk.telegram validate --config /path/to/config.json

`validate` reads the token file only to check its shape and never echoes it. No command writes the live configuration.

### Forum topics

**Setup (by hand, outside the bridge).** Create one private supergroup with only the owner and the bot, enable topics, create
the three topics, make the bot an administrator with the right to delete messages, and capture the group's ID and each topic's
numeric ID in the private file. Runtime needs no right to manage topics. Bot administrator status also delivers ordinary
owner messages and `message_reaction` updates without any change to the bot's privacy mode. The bridge creates, renames,
closes and deletes no topics (there is no tool for it, and `deleteForumTopic` is never used), and renaming a topic in Telegram
changes nothing: the numeric ID is the binding.

**What startup can and cannot prove.** The Bot API has no `getForumTopic` or topic listing, so startup **cannot prove that an
individual topic ID exists**, is open, or is the topic you meant; it proves the group is a private forum with the right
members and rights. A wrong, closed or deleted topic ID shows up only when a send is refused or its response does not name
the topic (below), so each route is checked once during live acceptance. Do not read a clean startup as proof that all three
topics are right.

**Destination rules.**

- *Inbound* messages route by their own validated `message_thread_id` and `is_topic_message`, never by chat alone. General,
  an unbound topic, an unconfigured chat and a message with malformed topic fields (for example a string ID, or a topic
  marker with no ID) are ignored, keep only a reason and never fall back to another topic. The automatic reply to a topic's
  own creation message that Telegram attaches to ordinary topic messages is not treated as a reply.
- A reply whose saved parent is at a different destination is refused unread, with one short notice in the topic it was sent
  in. The parent supplies no excerpt, request context or Signoff authority, and the reply cannot reach another agent. A reply
  to a message the bridge has no record of keeps the earlier ordinary-message behavior.
- *Reactions* carry no topic. They are resolved through the bridge's own stored message and accepted only for a known Signoff
  post at exactly the current Signoffs destination; a reaction on any other topic's message decides nothing and says nothing.
- *Sends* name `message_thread_id` explicitly (text and documents), and a response that does not name the intended chat and
  topic is **uncertain**: it may have landed in General or elsewhere, so it is never resent automatically. Resolve it like any
  uncertain send (below). Whole-chat sends keep their existing contract.
- *Edits, replies and deletions* require the target's saved destination to equal the currently configured destination of its
  logical name. An urgent event whose post sits at a different destination than the current Urgent topic is posted afresh, never
  edited there.
- *History, pending requests and secret requests* are per destination: `telegram_history` and request lookups filter by logical
  name, chat, topic and agent, and each topic can have its own outstanding secret request.

**Converting from separate chats, or moving a topic.** Edit the private file (new `chat_id`s and `message_thread_id`s) and let
the bridge verify it. The change is a **new generation** whose effective time is the next whole UTC second, exactly like a
repin, and the cursor and original `activation_at` carry over. Only the IDs matter: the fingerprint is unchanged for a
configuration with no topics, so upgrading the code alone resets no generation. Then:

- queued sends, stored updates, saved intents and queued terminal input of the old layout are closed (`binding_changed`),
  never retargeted or sent to the new destination;
- a pending blocking request at a destination that is no longer the configured one is cancelled (its record, text and times
  are kept) so it cannot block the new destination;
- old Signoff posts are retired as `chat_changed`, nothing said at the old destination is routed, reactions on an old post
  decide nothing, and pending Signoffs are posted again in the new Signoffs topic as a fresh generation;
- a cleanup for a message at a destination that is no longer configured **is not deleted** (nothing is sent to the old
  destination or to the new one). It is a failed cleanup (`unknown_chat`) whose `cleanup_failures` entry carries the saved
  logical chat, its saved `message_thread_id`, `still_configured: false` and a link to the original message;
- old messages keep their own destination. Nothing is backfilled, copied or moved to a topic: the six old chats remain as
  historical conversations and their history is not imported.

An owner repin with every topic unchanged behaves as described in the next section.

### The pinned installation and Qorqut owner

Two identities are pinned, so that no update, reply, decision or terminal input is ever attributed to whoever happens to be
the owner *now*:

- **Installation.** `expected_installation_id` must equal the authoritative installation record. It is saved on first
  activation and never changes for that state. A different value (a typo, or a restored database from another installation)
  is refused as `installation_binding_mismatch`; it is not a repin and never adopts this state's cursor, receipts or history.
  Another installation needs its own separately configured fresh state.
- **Qorqut owner.** `expected_owner_principal_id` must equal the active human installation owner. At first activation both
  values are matched against the authoritative records and saved as the pin. On every later start *and on every use* (a poll,
  a send, a deletion, a terminal delivery, a Signoff decision, an agent tool) the pin is read against the installation record
  again. The same file with a different database owner is `owner_binding_mismatch`. The bridge never adopts a new owner by
  itself, on restart or otherwise.

**On a mismatch the bridge fails closed.** There is no polling or ingestion, no tool or gateway send, no outbox effect, no
Telegram deletion, no direct terminal input and no Signoff decision, and agent tools (including `telegram_history`) return
`owner_binding_mismatch`. Local spool expiry and orphan cleanup, and body-free `telegram_status` (`binding.state`, health
component `binding`), keep running. Status only reports; it repairs nothing. Effects already on the wire cannot be recalled:
their receipts keep the identity they were bound to and never authorize anything under another one, and a decision that was
committed before the change keeps its canonical attribution.

**Repinning is a file edit by the trusted operator, nothing else** (no HTTP operation or CLI writes it). Discover the
authoritative identifiers without touching the token: the owner principal is `data.principal_id` of `GET /api/session` on the
dashboard origin (`data.is_installation_owner` is `true` for the installation owner), and the installation ID is the
`installation_id` of the `installation` record, which the operator can read on the dashboard host with a read-only
`sqlite3 -readonly <state database> "SELECT installation_id, owner_principal_id FROM installation"`. Neither is a credential.
Then edit `expected_owner_principal_id` (and `owner_telegram_user_id`, chats or agents if they also changed) in the private
file. Once the file validates against the authoritative owner the bridge starts a **new generation**:

- its **binding-effective time** is the next whole UTC second after the reconfiguration. A Telegram update whose `sent_at` is
  earlier can never gain the new generation's authority, even when it is fetched only afterwards (Telegram dates have second
  precision, so the reconfiguration second itself is excluded too). Only its metadata and a reason are kept, never its text;
- the poll cursor and the original `activation_at` carry over; the effective time is an additional cutoff, not a reset;
- every older queued send, edit, stored update, saved decision intent and terminal input keeps the
  identity and generation it was accepted under and is closed (`binding_changed`), never upgraded, retargeted or
  reattributed. What was saved earlier keeps its original attribution.

Unchanged restarts and unrelated settings (for example `dashboard_origin`) never change the generation or the effective time.

**Outstanding Signoff posts across a repin.** A Signoff post that is still active is an open question,
not queued authority, so a repin does not retire or re-post it, provided its Telegram chat is still the configured Signoffs
chat and its canonical proposal mapping is still exact and current. A fresh 👍/👎 or reply from the newly configured owner,
dated after the new generation's effective time, can answer it under that owner's current canonical permissions (an owner
without the scope gets the usual refusal notice); the decision is frozen under the new generation and owner. Anything
accepted earlier (stored updates, saved intents, held replies, queued inputs) stays fenced under its own identity. If the
**Signoffs chat itself** is changed in the file, posts of the old chat are not inherited: they are retired as `chat_changed`
without any Telegram call in the old chat (its cleanup job is reported as `unknown_chat` with its message link, for manual
deletion), nothing said there is routed, and the pending Signoffs are published again in the new chat as a fresh generation.

A post carried across a repin belongs to a delivery of the previous owner, which the newly pinned owner cannot see. It is kept
while that delivery is undismissed, notifications are still on, the Signoff is not snoozed and its proposal mapping is unchanged;
snoozing, clearing notifications, resolution, supersession and a replacement by the new owner's own next reminder or summary end it
as usual. Telegram stops allowing deletion after 48 hours and nothing of the new owner can refresh it, so it ends at its 24-hour
refresh time as `owner_changed` (deleted), and the new owner's next delivery publishes a fresh generation.

## Native operations

Agents call these through MCP. Identity always comes from the authenticated context. There are no chat, token, agent,
file-path, or parse-mode arguments.

    telegram_send {"text": "Deploy finished; nothing else needs you.", "purpose": "update"}
    telegram_send {"text": "Please reply with the SMS code Google just sent.", "purpose": "blocking_input",
                   "request_key": "google-login", "reason": "Signing in to the ads account",
                   "input_mode": "secret", "expires_in_seconds": 300}
    telegram_history {"limit": 5}
    telegram_delivery_get {"message_id": "<local message id>"}

- `telegram_send` is plain text, up to 4000 characters. `blocking_input` needs `request_key`, `reason`, and `input_mode`
  (`text`, `secret`, `screen_action`). Reusing a pending `request_key` returns the existing request; a materially
  different request conflicts. Secret requests expire within 10 minutes.
- The owner sees a blocking request as the agent's text followed by `Why it is needed`, `Expires` and one line saying how to
  answer (reply to the message; for `secret`, that Telegram chats are not end-to-end encrypted; for `screen_action`, do it
  on screen and reply "done"). The request with those lines must fit one message.
- `telegram_history` returns at most the last ten messages of the caller's own topic since activation, filtered to that
  destination's exact binding (chat name, provider chat, topic and the caller's own agent identity). A message an earlier
  binding of the same logical chat recorded for another agent or at another topic, and a message saved before topics existed,
  stays in the record but is never shown to this one.
- `telegram_delivery_get` reads the caller's own receipt states: `queued`, `dispatching`, `uncertain`, `sent`, `failed`,
  `cancelled`.

Owner-only (dashboard HTTP, never MCP):

    telegram_status {}
    telegram_delivery_resolve {"outbox_id": "<id>", "action": "mark_delivered", "provider_message_id": 812, "expected_revision": 4}
    telegram_delivery_resolve {"outbox_id": "<id>", "action": "retry", "expected_revision": 4}

One resolution names one exact delivery: the `outbox_id` of an uncertain send **or edit**, as listed in `uncertain_sends` of
`telegram_status` (`failed_sends` entries carry the same fields for inspection) and in the `deliveries` of the owner's
`telegram_delivery_get`. A message can have several deliveries (its send and later edits), so a message ID alone never selects
one. `expected_revision` is that delivery's `receipt_revision`; a receipt that changed since is refused with
`revision_conflict`, so a stale inspection can never authorize another send. `mark_delivered` for an edit needs the ID of the
Telegram message that was edited and records the confirmed text. Resolving an old receipt never authorizes obsolete content:
an edit that a newer update of its event already replaced is closed instead of sent.

Trusted registered workers and the owner may call `telegram_notify` (see below). Only the exact configured Ordubashy
agent may use the conversation tools; every other agent is refused ("no Telegram chat"), and delegated identities never
borrow its chat.
An explicit deny override or a revoked grant is rechecked before every send.

Offline equivalents:

    python -m bazarlyk.telegram status --url http://127.0.0.1:1071
    python -m bazarlyk.telegram resolve --url http://127.0.0.1:1071 --outbox-id ID --expected-revision R --action mark_delivered --provider-message-id N
    python -m bazarlyk.telegram composer-check --client claude --screen saved-screen.txt [--cursor X,Y] [--pasted-file text.txt]

## Urgent notifications and the local program gateway

`telegram_notify` posts to Urgent Notifications only for program failures that need action, availability or data-loss
risks, and meaningful time-sensitive changes. Each needs what happened, the impact, and the needed action. It stays quiet
for unchanged state, normal completions, routine progress, and periodic "still running" reports.

    telegram_notify {"event_key": "db-down", "title": "Database unreachable",
                     "text": "Writes fail; customers cannot log in. Fail over to the replica.",
                     "reason": "Data-loss risk", "link": "https://example.internal/runbook"}
    telegram_notify {"event_key": "db-down", "state": "resolved", "title": "Database recovered",
                     "text": "Writes succeed again.", "reason": "Recovery"}

An identical active event (same producer, `event_key`, and content) is suppressed, unless its earlier post failed or was
cancelled, because the owner never saw it. One whose send is `uncertain` stays suppressed. A changed event edits the known post. A
recovery edits it once to a `RESOLVED` update. Each transition carries a persisted event sequence: a queued original or an edit
waiting to retry is cancelled when a newer transition arrives, and dispatch rechecks the sequence, so a delayed older update never
overwrites a newer change or a recovery. A recovery for a condition that was never reported is ignored. The
first event is never rate-limited away. At most 100 urgent events per producer (the owner counts as one) may be unsent at once, and beyond that the caller receives
retryable `busy` backpressure instead of a silent drop. Never use it to duplicate an actionable Signoff.

The bridge's own Telegram outage is not an urgent event. It is visible in `telegram_status` (`state`, `health`, `outage`,
`last_error`, backlog counts) and in the service log, so a failed channel never notifies itself through that channel.

### Producer enrollment

A local program calls a Unix-domain socket owned by the service (0600 in a 0700 directory, newline-delimited JSON, at most
64 KiB). Requests are authenticated by Linux peer UID (optional `peer_uid`) plus a per-producer secret; the secret maps to
the stable producer ID, and callers cannot choose a producer or target.

    python -m bazarlyk.telegram enroll --producer-id backup-monitor --secret-file /home/USER/.config/qorqut/telegram/backup-monitor.secret

`enroll` writes a new random secret to its own 0600 file, never overwriting one, and prints the object to add to
`program_gateway.producers` (it holds only the SHA-256). Then send events without putting the secret in argv:

    python -m bazarlyk.telegram notify --socket /home/USER/.local/state/qorqut/telegram/gateway/notify.sock \
        --secret-file /home/USER/.config/qorqut/telegram/backup-monitor.secret --payload-file - <<'JSON'
    {"event_key": "backup-failed", "title": "Nightly backup failed", "reason": "No restorable copy since Monday",
     "text": "The 03:00 backup exited non-zero. Run the recovery guide."}
    JSON

The payload is one JSON object (`event_key`, `title`, `text`, `reason`, optional `link` and `state`) from a file or stdin, at most
64 KiB; `--event-key/--title/--reason` flags with `--text-file` remain as an alternative for scripts. The credential is only ever a file.

    from bazarlyk.telegram.client import notify
    notify("/path/to/notify.sock", "/path/to/producer.secret", event_key="db-down", title="Database unreachable",
           text="Writes fail. Fail over.", reason="Data-loss risk")

Internal Qorqut workers use their existing trusted WorkerProfile grants instead. Disabling (`"enabled": false`) or removing a producer, or disabling the gateway, blocks its
queued sends too: they are cancelled as `producer_revoked` before any send. There is no TCP listener and no general-purpose Telegram proxy.

## Owner messages and guarded input

Any text from the exact owner user in an agent chat is a durable canonical owner-to-agent inbox message with one shared
correlation, then a direct delivery to that agent's own terminal when it is safe. Forwarded text is content, never
authorization. Bots, other users, anonymous senders, edits, channel posts, and reaction counts are ignored.

Each accepted message is attributed to the pinned Qorqut owner and freezes the identity it was accepted under: installation,
owner, configuration generation and binding, and the target's registration (agent, home scope, workspace, role and runtime).
It is checked again when the delivery is loaded and inside the very transaction that commits the input, after any terminal
wait. A change to the installation owner, the Telegram owner, the chat-agent binding, the role or home scope, or the target's
registration fails that delivery (`binding_changed`, `target_changed`); it is never retargeted or reattributed. The canonical
inbox message stays.

Direct delivery reuses the registered terminal binding and process fingerprint, and rechecks them immediately before the
paste. It finds the actual composer first, from the cursor and the client's own layout, and reads the client chrome around
it (a running-turn indicator, a modal, a shell) separately from ordinary transcript and from whatever the owner typed, so a
message that itself says "sign in" or ends in a question never blocks delivery. A real tool, trust or authentication menu, a
shell, a busy turn or partially typed text still stops it: Telegram input is idle-only, and a running turn queues it. (Terminal
Chat alone opts in to steering a running turn, through an explicit option this bridge never passes; see
`v1.2/NATIVE_CHAT_CHECKPOINT.md`.) Each of its three observations must show the ordinary empty
composer on the same terminal and client; other output, such as a timer, the footer or new transcript text, may change between
them. After the paste it requires evidence that the intended literal
text occupies that same composer before pressing Enter. Otherwise the input stays queued, with one status notice and a
status diagnostic. Once a
write may have happened, the delivery is `uncertain` and is never replayed. Once input was attempted, the row keeps a
terminal receipt (`receipt_json`: the client, a hash of the observed terminal identity and the composer fingerprint; never screen
text or the typed text). Submission is not acknowledgement; the agent still handles the inbox event once.

Limits: text over 16 000 characters gets a clear failure and is never truncated. Photos, documents, voice, and other
media are not downloaded, and the owner gets one reply asking for text or the dashboard upload flow. The composer
recognizers are fixture-based and have not been validated against live clients; use `composer-check` on saved
`tmux capture-pane -p -e` screens (with `--pasted-file` for a capture taken after a paste) during the activation checklist,
and repeat it whenever a client version changes. Nothing here removes the possibility of a human typing at
the same instant.

### Sensitive input

A pending `input_mode=secret` request marks every owner text in that topic as sensitive until its first response, expiry,
or cancellation (one outstanding secret request per topic; another topic's text is never sensitive because of it, and a reply
from another topic to a secret prompt is refused unread). Whether a text is sensitive is judged by when the owner *sent* it
(its Telegram date) against the request's lifetime, which keeps its closing time, never by when the bridge read it. A text
sent while a secret request was open, or in reply to a secret prompt at any time, stays sensitive forever, however late polling
is and whatever later happens to the request; if its delivery window has passed its body is discarded and only a refusal is
recorded. The first text that can still be delivered answers the request, and that holds inside one fetched batch too: a later
text of the same batch is ordinary, while one sent in the same second stays inside, as after a commit. A Telegram date has
only second precision, so the request's start is compared as the second it falls in: a text dated in the second the request
was queued is treated as sensitive even if it was sent a moment before it (classification only; delivery still needs a pending,
unexpired request, and owner and binding checks are unchanged). Updates that are not routed (not the owner, not an agent topic or Signoffs, General or an unbound or malformed topic, before
activation or before the current binding took effect, unsupported) are stored without their text. A request whose prompt could not be sent (it failed or was
cancelled before reaching Telegram) is cancelled, so it stops governing the chat and its key can be asked again. Secret bodies live only in short-lived 0600 spool files under the
state directory (`.../qorqut/telegram/spool`, outside the database, history, backups, and exports), with a TTL of at most
10 minutes. They are deleted after confirmed submission or expiry, and a crash never resurrects an expired one. Expiry and orphan
cleanup are local: a sweep runs before any Telegram verification at startup and then about every minute for as long as the
process lives, whether or not Telegram is reachable or the integration is disabled or in a binding mismatch. A process that
started disabled or with an unreadable configuration runs no control loop, so a small watch thread keeps sweeping for as long as
a still-valid file exists and then ends; an installation without one has no such thread. A still-valid
file that a queued input or an unrouted stored update owns is kept until it is routed. The canonical redacted inbox text says
the value is queued, not that it was delivered. Everything else stores a redaction marker. The known request and reply posts are deleted after submission or expiry, within the API's limits.

Exact limits: this is limited retention, not secure erasure. Bot chats are not end-to-end encrypted. A code submitted
directly to a model client can remain in that client's transcript, which this bridge does not erase or change. Ordinary
unsolicited secrets cannot be detected, and nothing here advertises that it does. Prefer `screen_action`: the owner enters the
code in the real browser, replies "done", and the agent verifies the result. Never request a code in Signoffs or
Urgent Notifications.

## Signoffs in Telegram

The bridge reads canonical Signoff state and `SignoffNotifications`; it is not another approval engine. Each eligible
pending item (respecting owning scope, visibility, `attention.notify`, urgent/blocking, snooze, next reminder, and
supersession) gets its own post in Signoffs, bound to the exact proposal revision, payload hash, and status revision. A
truncated preview says so. There is no deep link to one proposal, so every link in a post or notice only opens the
Signoffs view of the owning workspace and is worded that way ("Open the Signoffs view"), never as the exact current record.

The bridge notices Signoff changes by **polling**: each control cycle (about a second when idle) it compares the committed
Signoff change sequence and the notification and attention markers, and a 15-minute reconciliation repairs anything missed. It
does not hook into Signoff transactions, so a change appears in Telegram on the next cycle rather than instantly.

- 👍 approves and 👎 rejects a prepared action (👎 carries the factual note "Owner reacted 👎 in Telegram"). For an input request
  they map only for an optional-notes binary form whose two choice IDs are exactly `yes`/`no`; otherwise the bot explains
  and links the canonical form.
- Reaction updates name no topic, so a reaction is matched to the post the bridge itself stored for that message, and only when
  that post is at the current Signoffs destination. Both thumbs, removed reactions, reactions by other users or anonymous
  actors, reactions on another topic's message, and reactions on stale or unknown posts are never decisions. The first accepted canonical decision wins; duplicates reuse a deterministic idempotency key.
- A reply is always a canonical comment (or a `respond` only when free text alone satisfies the input form, which means the
  form requires no choice), then a durable handoff to the proposer through the existing inbox event and the guarded input
  path. A reply to a question that requires a choice is saved as a comment and leaves it open. Both thumbs (or an extra
  reaction) get one explanation. A reply must be inside the Signoffs topic and to a post in it; a reply pointing at another
  topic's message is refused unread and cannot decide or reach any agent. A reply to a post that is no longer current is saved as a comment and says so, with the link
  that opens the Signoffs view. An inactive proposer gets an explicit notice, never a silent reassignment to Ordubashy.
- The direct terminal input of a reply is queued exactly like an ordinary owner message and **never expires**: an agent that is
  offline receives it, once, whenever its terminal is next available, however long that is. It becomes `failed` only on an
  explicit loss of authority or target (a rebinding, a changed registration) and `uncertain` after possible input; neither is
  retried or reassigned. The canonical note and inbox event stay durable throughout.
- If the proposer cannot receive the handoff when the reply is saved, the reply and the post it answered are **held**
  (`held_replies` in status) and nothing is deleted or retired for good; reconciliation finishes only once a valid handoff
  exists. A reply to a post that is no longer current is held the same way when its proposer is inactive, and its note is still
  saved as a comment. A hold made under an older binding generation ends without queuing terminal input under the new one.
- Every accepted 👍/👎 or reply intent freezes the pinned owner and generation it was saved under. It is verified before its
  owner context is built and again immediately before the canonical call, and under a later generation it fails
  (`binding_changed`) without deciding anything. During an owner mismatch it stays pending and is never applied.
- A decision or reply that cannot be applied (a hard canonical failure, or an owner context that can no longer be rebuilt)
  is reported, and the post and the owner's reply stay in the chat: nothing is deleted before a confirmed save and handoff.
- Decisions, replies, snooze, cleared notify, supersession, and dashboard/API decisions retire the post and queue deletion of
  that known post and the owner's correlated reply. An ordinary reply leaves the Signoff pending, and the next due
  reminder posts a fresh generation. Hourly reminders replace the previous post.
- At 24 hours a still-pending post is refreshed with a new generation (Telegram cannot delete messages older than 48 hours).
- Approved-but-failed, conflicted, or unconfirmed executions get a separate non-actionable status notice with a link. They are
  never retried automatically.

## Delivery guarantees, status, and recovery

Every external effect commits an intent first and a receipt afterward. The Bot API has no client idempotency key, so a
timeout after a possible send is `uncertain` and is never resent automatically. Known pre-send failures and provider refusals
retry with bounded backoff (429 obeys `retry_after`). There is no exactly-once claim.

`telegram_status` reports: state (`disabled`, `starting`, `running`, `degraded`, `stopped`), health components, last poll,
activation time, configuration generation, the pin check (`binding`: `state` `matched`, `owner_binding_mismatch`,
`installation_binding_mismatch` or `binding_changed`, plus the generation and its effective time; never an identifier), outage
and last error, update backlog, outbox states, uncertain and failed sends (each with its `outbox_id`, `receipt_revision`, event
sequence, logical `chat` and `message_thread_id`), oldest queued owner input, unavailable targets, undelivered (failed or expired) direct inputs, cleanup states, and Signoff post counts (including held replies).
It never shows bodies, tokens, or provider chat identifiers (only a manual-cleanup link carries its group's number, and a topic
number is shown only where it resolves a receipt); an unavailable target is named by its registered agent ID and a reason.

- **Uncertain send or edit.** Look in the chat. If the message is there, `mark_delivered` with its exact provider message ID
  (`Forward → message link`). If it is not, `retry` (this can duplicate). Both name the delivery's `outbox_id`, take the receipt revision you inspected and apply only to an `uncertain` send or edit. A retry keeps the saved target and binding, so if the bot, owner or chat/agent binding changed since, the send fails closed again as `binding_changed`; a `failed` send is never retried by the owner, the agent sends again. Neither changes a Signoff decision.
- **Failed cleanup.** `cleanup_failures` lists the logical chat, its saved `message_thread_id`, whether that destination is
  still the configured one, a link to the original message (`https://t.me/c/<group>/<topic>/<message>` for a topic message) and
  manual instructions for each message the bridge could not delete (older than 48 hours, missing admin right, outage, or a
  destination that is no longer configured). The link is built from what was saved with the message, never from the current
  configuration.
- **Uncertain topic send.** A topic send whose response is missing or names another chat or topic is `uncertain` like any other
  possibly-delivered send: look in the topic and, if it is in the wrong place, in General; `mark_delivered` with the exact
  message ID if it is where it should be, otherwise `retry`. A retry keeps the saved destination: after a topic was reassigned
  it fails closed as `binding_changed` and the agent sends again.
- **Extended outage.** Telegram keeps pending updates for about 24 hours. The bridge reports the outage but cannot recover
  older messages. The Bot API cannot fetch arbitrary earlier history.
- **Restart.** The cursor, receipts, and unfinished intents survive. Stored unfinished updates process first (an update
  fetched under an older binding generation is discarded, never routed late), a send that was in flight becomes `uncertain`,
  and Signoff posts reconcile from canonical state. An unchanged restart keeps the generation and effective time.
- **Writer quiescence.** The bridge is included in maintenance and recovery quiescence and stops within a bounded time (35 s).

State (poll lock, spool, gateway socket) lives under `QORQUT_TELEGRAM_STATE_DIR`, else
`$XDG_STATE_HOME/qorqut/telegram` (default `~/.local/state/qorqut/telegram`).

## Credential rotation

1. Create the new token in BotFather and write it to a new 0600 file. Do not paste it into a command line.
2. Run `python -m bazarlyk.telegram validate --config <candidate config with the new bot_token_file>`.
3. Set `enabled` to `false` in the live file and restart the service, so the poller stops and releases its lock. A
   changed `expected_bot_id` needs the same treatment and fails closed for queued work, so drain or cancel first.
4. Switch `bot_token_file` (and, only if the bot changed, `expected_bot_id`), set `enabled` back to `true`, and restart. The
   cursor and receipts are retained.
5. Revoke the old token. To rotate a producer secret, `enroll` a new file, replace its `secret_sha256`, and remove the old
   file.

## Retention and deletion limits

- Signoff posts and short-lived authentication requests are deleted after decisions or expiry, when Telegram allows it
  (within 48 hours of the message). Edits do not reset a message's age.
- Urgent messages and ordinary conversations have no general nightly delete policy.
- The bridge deletes only messages it knows. It never sweeps unknown posts, unrelated owner messages, or a chat's history.
- Conversation history, receipts, and cleanup records stay in the authoritative database with the rest of the operational
  records. Secret bodies never do.

## Deployment, migration, and rollback

Migration 106 adds eight tables (`telegram_state`, `telegram_updates`, `telegram_messages`, `telegram_outbox`,
`telegram_signoff_posts`, `telegram_owner_inputs`, `telegram_cleanup_jobs`, `telegram_events`) and no behavior change while
`enabled` is false. `telegram_state` holds the installation and owner pins, the generation, its effective time, the
activation cutoff and the poll cursor; each owner input freezes its owner, generation, binding and target fingerprints.

Migration 107 is additive and leaves migration 106 byte for byte as it was. It adds a nullable `message_thread_id` to
`telegram_messages` and `telegram_outbox` (an integer greater than 1, immutable once saved, like the chat it belongs to) and an
index on each destination. Every existing row stays null, meaning a whole-chat destination: nothing is backfilled to a topic, so
a legacy message or receipt keeps the destination it really had, and provider IDs, the cursor, the generation and the
stored history are not touched. Provider `(chat_id, message_id)` uniqueness stays group-wide. A database at 106 upgrades with
the release; a configuration that names no topics keeps its binding fingerprint, so the upgrade alone is not a rebind.
Deployment order is core (migrations and release) first, then the role source, then configuration.

Rollback is to set `enabled: false` (or unset `QORQUT_TELEGRAM_CONFIG_FILE`) and restart. This preserves the poll cursor,
receipts, and history. There is no automatic database restore and no destructive down-migration. If
the bridge is re-enabled later, it resumes from the saved cursor; `activation_at` does not move, so old messages are still
not replayed.

## Activation checklist

For a **future, separately approved** release only. None of these steps is performed by the source change.

1. Release core through the release worker (migrations 106 and 107), following the common release lifecycle.
2. Publish, adopt, and prepare the updated Ordubashy role (it selects the `telegram-owner-communication` skill). This is
   explicit and separate.
3. Create the private forum group and its three topics by hand (see [Forum topics](#forum-topics)), then write the private
   configuration and token files with the group and topic IDs and the two pins (`expected_installation_id`,
   `expected_owner_principal_id`, discovered as described above), then run `validate`.
4. Save composer screens for the two native clients and run `composer-check` on each. The recognizers are unvalidated
   until then.
5. Set `enabled: true` with the intended `activation_at`, and restart under the approved release.
6. Confirm `telegram_status` shows `running` and no unavailable targets.
7. With the owner, run an end-to-end acceptance test. That covers one agent conversation both ways in Ordubashy's topic,
   one Signoff post with a reaction and with a reply, and one urgent event and its recovery,
   each confirmed to appear in its own topic (startup cannot prove a topic ID exists). Check that a reaction, and a reply
   across topics, decide nothing. Do not run real decisions without the owner.
