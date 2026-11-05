# Ordubashy outbound Telegram files and photos

Branch `v1.2/telegram-media-operation-integration`, base `7c2670e`. Integrates the real operation `telegram_send_file` over the
approved foundations (file-source access, sealed payload storage, streaming transport and the Ordubashy role grant). Source
only: nothing here starts a service, adopts a role, changes the live private configuration, calls Telegram or sends a file.
The composed schema is unchanged (the payload table, three indexes and eight triggers came with the payload foundation).

## The tool

`telegram_send_file` is one closed MCP operation: effect `records`, no Signoff, Global scope only, exposure `mcp`, and it needs
the Global capability `read_global_context`. Arguments, plus the framework `idempotency_key` (required):

| Argument | Meaning |
| --- | --- |
| `source` | `{"type":"workspace","path":"exports/…"}` (a regular file below `exports/` or `downloads/` of the caller's own workspace) or `{"type":"document","document_id":…,"business_id":…|null}` (a stored document the caller may read: asset, data upload, agent or business note, content or context note) |
| `expected_sha256` | required, 64 lowercase hex characters, the digest of the exact bytes meant to be sent |
| `send_as` | `document` (1 to 50,000,000 bytes, bytes preserved) or `photo` (a static JPEG or PNG, 1 to 10,000,000 bytes, width plus height at most 10000, long side at most 20 times the short side, one frame; never converted, resized or sent as a document instead) |
| `purpose` | `reply` or `update` (`blocking_input` is for `telegram_send` only) |
| `caption` | optional, default empty; plain text, at most 1024 characters and 1024 UTF-16 code units, no control character other than line feed, carriage return and tab |
| `reply_to` | optional local message UUID in the caller's own topic that Telegram has already accepted |

There is no argument for a chat, topic, file name, bytes, URL or Telegram file id. The file name is the source's own basename.
The result is the same delivery receipt as `telegram_send` plus `attachment: {filename, send_as, size, sha256}` (and `reply_to`).

## Who may call it

Only the exact configured, active Global Ordubashy bound to the owner's Telegram topic. The role grant (Ordubashy 1.63.1) is
necessary but never sufficient: the operation rechecks the configured chat binding, the agent's active status, the caller's
grant and its source read access in `prepare`, in `apply`, in the replay validator and just before every upload. Another global
agent, a business or historical agent, the owner context and a delegated context are refused before anything is copied or
queued. Disabled or not yet configured Telegram refuses with `operation_unavailable` and creates neither a row nor a directory.

## One call

1. `prepare` (read transaction): authority, argument validation, source authorization, the frozen manifest. Nothing is written.
2. `external` (no transaction): the bytes are streamed once through a limited SHA-256 into a private sealed snapshot, the digest
   must equal `expected_sha256`, a photo is validated, the snapshot is sealed. A mismatch, an unsupported kind, a vanished or
   changed source, a bad photo or an oversize file is refused here and spends the key (`staging_failed` on a retry).
3. `apply` (write transaction): rechecks authority and the reply target, inserts the outbound message (its body is the caption,
   possibly empty), enqueues the `agent_file` delivery with the closed envelope `{"version":1,"payload_id":…}`, attaches the
   snapshot and, in the same transaction, the framework saves the receipt. All of it commits or none of it does.

The private manifest `{version:1, source, destination:{chat_name, chat_id, message_thread_id, agent_id, config_generation,
binding_fingerprint}, caption, reply}` is frozen in the payload record. A rebound destination is never retargeted: a sealed
request for the old topic fails `binding_changed`, a fresh key follows the new binding.

## Idempotency and sealed replay

The same key and arguments return the original receipt without copying or queueing again, provided the caller still holds the
authority it was issued under (a revoked grant, an archived document or a lost binding withholds the saved receipt). The same
key with other arguments is `idempotency_conflict`. A request sealed before a crash or a failed apply is completed from its
saved bytes, manifest and file name without reopening the source, even if the source has since changed, moved or vanished. A
copy still in progress answers `busy` (retryable).

## Delivery

The dispatcher admits the delivery inside its claim transaction (database only) and loads the bytes outside any transaction.
Both checks read the current authority; the loader additionally verifies the sealed bytes (size and SHA-256) and, in one fresh
read immediately before it yields the stream, refreshes the whole frozen authority:

- the Telegram binding: the bridge is enabled, the pinned installation and owner still match and the owner is active
  (`bridge.check_binding`), and the current config generation and binding fingerprint equal the ones frozen in the manifest and
  the delivery row;
- the agent, the caller's grant, the source read access, the reply target and the stored caption.

A binding is only ever compared, never inferred or adopted: nothing in this path creates a generation or re-pins an owner.

| Situation | Outcome |
| --- | --- |
| generation or fingerprint changed (rebound owner or destination, changed pin or config), seen at admission | `failed` / `binding_changed`; nothing uploaded. Never a `cancelled` row and never an attachment-integrity error, also when the visible destination is restored later: a generation that moved away and back is a different generation |
| grant revoked, agent inactive, source archived, workspace remapped, business archived | `cancelled` (`grant_revoked` / `source_revoked`); nothing uploaded |
| authority found lost only after the claim (in the pre-upload re-verification or the loader's final read) | no upload starts: the claim is released back to `queued` and the next ordinary admission decides it, so a binding change ends `failed` / `binding_changed` and a revoked grant or source ends `cancelled` |
| bridge disabled, owner no longer active, installation or owner pin changed, found at the loader's final read | the claim is released back to `queued` and the bridge's own refusal (`operation_unavailable`, `binding_invalid`, `installation_binding_mismatch`, `owner_binding_mismatch`, `binding_changed`) is re-raised, so the control pass fails closed exactly as for any other delivery; the row stays queued with that code and no request exists; once the bridge verifies again, the same unchanged generation and fingerprint make the very same frozen send exactly once, and anything else is decided by the admission above |
| source recheck temporarily busy | deferred and retried with backoff, never cancelled on a transient failure |
| reply provider id or the stored caption no longer matches the frozen manifest | `failed` (`reply_changed` / `payload_mismatch`); nothing uploaded |
| sealed bytes missing, unsafe or altered | `failed` (`media_unavailable`); never rebuilt from the source |
| provider refusal | `failed`, final, not retried |
| unknown outcome (timeout after the upload started, unconfirmed topic) | `uncertain`; never resent automatically |

A late binding failure is found before any request exists, so it is never reported as `uncertain` and no provider call is made.
The one remaining window is the interval between the final read and the first byte of the request; no check can close that, and
the response handling after it is the established transport behavior. The one narrow
extension of the dispatcher's authority-moved signal (`AuthorityMoved.fail`): it carries the bridge's own refusal, which the
dispatcher re-raises after the requeue. Only the loader sets it, and only for the binding checks above.

An `uncertain` file is reconciled by the owner with `telegram_delivery_resolve`: `mark_delivered` records the provider id and
sends nothing; `retry` re-queues the same sealed bytes, caption, reply and topic and **can duplicate a send**. The agent is told
never to resend, retry under a new key or switch mode.

## Retention and limits

Sealed snapshots are private files under `<state root>/outbound-media` (0700 directory, 0600 opaque names); the database record
keeps only facts. The bytes of a queued, dispatching or uncertain delivery are kept indefinitely; those of a sent, failed or
cancelled delivery, and of a sealed request that was never queued, are removed 24 hours later. A crashed copy keeps its
record until its 24 hour expiry. History and receipts keep the attachment facts after the bytes are gone. At most 2 copies run
at once, at most 50 files may wait for delivery and the store is limited to 1 GiB of reserved bytes; each limit refuses only
that file (`capacity_exceeded`, retryable) and `telegram_send` keeps working.

## What the owner and the agent see

`attachment: {filename, send_as, size, sha256}` is projected in `telegram_delivery_get`, `telegram_history`, the owner's
uncertain/failed lists and `status`. A captionless file reads `File: name` (or `Photo: name`) in history and in the
"in reply to" excerpt of a later owner message; the message body itself stays the real caption and is never replaced by the
label, which is also never sent as a Telegram caption. No path, workspace location, storage name, payload id, source binding or
byte is projected. Operational-history exports already exclude the payload, message, outbox and receipt tables.

## Bridge lifecycle

`bridge.media` is inert at construction. Startup recovers sealed snapshots and sweeps retention in every mode, including
disabled or not-yet-verified Telegram, and a small watcher thread runs only while a snapshot record still needs upkeep. The
owner `status` always carries a `media` section (counts and bytes by state, last error code). A media problem degrades only
the health component `media` (visible, with recovery on the next successful pass); it never disables text, report or input
delivery.

## Enabling it later (not done here)

Nothing is live. A later authorized release needs: this source deployed with the pinned Pillow (`Pillow==12.3.0` in
`service-constraints.txt`); the Ordubashy role at 1.63.1 (the adopted 1.63.0 role plus this single grant) adopted on the
registered Global agent; and MCP tool discovery refreshed so `tools/list` shows `telegram_send_file` for that agent alone.
The Telegram configuration is unchanged. Until then the tool is simply absent from every agent's inventory.

## Checks

`tests/test_telegram_media_operation.py` (the composed host, a recording fake Bot API, including the late binding moves: a
disabled bridge, an inactive owner, an owner Telegram user rebound and back, a changed owner or installation pin, an unadopted
config change, and the stable-authority control), `tests/test_telegram_media_operation_discovery.py` (approved roles archive,
effective-policy rows, MCP inventory and schema, the skill's own JSON examples validated and executed through `MCPTransport`,
Pillow from an isolated path) and `tests/test_telegram_media_operation_flows.py` (the established report, Signoff and
blocking-input flows on the same composed host, each alone, beside a queued attachment and while media upkeep fails; only the
Bot API and the native terminal's arbiter are replaced by recording fakes).

This change is source only. It adds no schema object (the composed schema stays frozen), starts no service, adopts no role,
touches no live configuration or database and sends nothing to Telegram.
