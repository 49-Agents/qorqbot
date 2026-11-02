# Personal WhatsApp sending through owner Signoffs

Branch `v1.2/whatsapp-signoffs`. This is source work only: nothing here installs, upgrades, pairs, starts or sends
anything. The connector's own half is documented in
[`extensions/whatsapp/native/README.md`](../../extensions/whatsapp/native/README.md).

## What an agent can and cannot do

The registered Global Ordubashy, and only the exact UUID named by the installation binding, may propose **one** host
operation, `whatsapp_send`, with exactly two arguments:

| Argument | Rule |
| --- | --- |
| `chat_id` | The exact ID of a conversation already in the connector's index (`whatsapp_read`). No names, new numbers, broadcasts, newsletters or groups the index has not seen. |
| `text` | Non-empty, at most 16,384 **UTF-8 bytes**, plain text. |

There is no caller-supplied request ID, attachment, quote, edit, delete, retry or recipient list. The operation never sends:
it prepares an ordinary `internal_change` Signoff (not a `platform_action`) for the installation owner and returns
`signoff_required`. The preparation reads the connector read-only (`/managed/account`, `/managed/chat`) to freeze the
account identity and to show the chat's display name; it reserves nothing and sends nothing.

The owner's Signoff shows exactly: the chat ID, the display name, the text, and one plain statement that the text will be
sent from the personal WhatsApp and that delivery and reading are not confirmed. The queue row the engine builds for the
approved job (arguments, principals, definition digest) is part of the approved change but deliberately **not** part of the
presentation.

Nothing else can send:

* The owner calling `whatsapp_send` directly is refused (`_access`): the operation exists only to propose.
* Every other agent, role, delegated identity, business scope or unbound Ordubashy is refused by the operation and, one
  layer earlier, by policy.
* The program behind it, `whatsapp.send`, cannot be scheduled: `routines_create` refuses it (new engine flag
  `Program.routine_allowed=False`), and a job row written without a matching applied owner Signoff is refused at dispatch.
* The connector's `whatsapp_read` MCP and its read socket have no send path; a send request on the read socket is refused
  as before.

## Flow

1. **Propose.** `whatsapp_send(chat_id, text)` by the bound agent. Policy always returns *Signoff* (below). The operation
   rechecks the binding, the agent (active, Global, Ordubashy, not delegated), that the trusted program worker is
   registered, and the connector's account reference, then stages the plan: the preview above, an exact `jobs` change for
   `whatsapp.send`, and the frozen `account_ref`.
2. **Decide.** The owner approves or rejects in the dashboard. Rejection, withdrawal-by-replacement, a deny grant or an
   expired/stale proposal queue nothing.
3. **Apply.** The engine revalidates the targets (`validate_targets`): same bound agent, same `account_ref`, same chat and
   text as approved. Application only **queues** the exact approved job; no connector call is made.
4. **Dispatch.** The background service runs `whatsapp.send` under its own system worker. Before any contact it rechecks the
   proposer's current policy, the binding and agent, the live connector account reference and the approved Signoff itself
   (owner decision, category, scope, exact job arguments), then submits once under the stable request ID
   `qorqut-job-<job id>`.
5. **Settle.** The connector reserves that request ID durably before contacting WhatsApp. Its receipt is bound to the
   request ID, chat ID, account reference and a payload fingerprint; the host verifies all four before believing it and stores
   only identity and state (never the chat ID or text) in the job result.

### Recovery and uncertainty

* The job records a durable `dispatch_intent` checkpoint immediately before the submission. After that point **every** later
  attempt, whether a retry, a restart or a recovery, asks the connector for the receipt of the same request ID and never
  submits again. Recovery is decided by that checkpoint, not by the attempt counter.
* A connector that is down, unauthenticated or disconnected **before** the checkpoint proves nothing was sent; the job waits
  (15 s) and may still submit later.
* A response counts as proof that nothing was sent only if it is the connector's explicit refusal body
  (`{"error": ..., "sent": false}`). A bare `503`, a dropped connection, a malformed body, a timeout, or a failure to read
  the receipt back after the send ran are all **unknown outcomes** and are only ever settled by receipt lookup.
* A receipt in state `unknown` is not success. After 8 attempts the job fails with `external_unconfirmed` (not retryable); it
  is never reported as sent and never repeated. The owner checks the conversation and, if needed, proposes a new message.
* A definitive refusal after the checkpoint (for example the connector reports a different account) fails the job with a
  fixed message; nothing is retried.
* Changing the text or chat requires a new proposal and a new approval; there is no edit of an approved job.

## Authority

* **Policy** (`composition/policy.py`): `whatsapp_send` returns *Signoff* only for the `ordubashy` role in the Global scope and
  only when the agent holds a grant: an owner override of the profile's tool policy, or a role default, that is not `deny`.
  An `allow` override cannot remove the Signoff; a `deny` anywhere wins. The role's published defaults do not grant it, so the
  owner must set the Ordubashy profile override to `signoff` (or `allow`, which means the same) as a separate deliberate act.
  This follows the existing `owner_action_propose` grant model; installing a binding does not grant sending authority.
* **Discovery:** the effective-configuration view that feeds `permissions_get` and MCP `tools/list` evaluates `whatsapp_send` in
  both scopes, like the Global table operations (`GLOBAL_DISCOVERY_OPERATIONS`): `scope_modes` shows `business: deny` and
  `global: signoff`, and the tool is listed as available `signoff` only for an ecosystem profile that holds
  `read_global_context`, the `ordubashy` role and a grant. A denied, ungranted, other-role, business or capability-less profile
  sees it unavailable. Discovery grants nothing: the operation still requires the exact bound agent. Other tools keep their
  single business-scope evaluation.
* **Capability floor:** the descriptor requires the existing `read_global_context` global capability (the floor every
  registered Global agent already holds, and what Global Signoff visibility needs); the real gates are the exact UUID, the
  active Global Ordubashy check and the forced Signoff. No new capability was added.
* **Delegation:** `whatsapp_send` is on the operational-delegation exclusion list; no delegated assignment can propose it.
* **Signoff visibility** (new additive engine port, below): the proposal, its notes and its decision are visible only to the
  installation owner and to the agent the binding names *right now*. Any other reader, a former proposer included, sees
  nothing, and listing/ counting/ notification reads apply the same filter. A missing, unreadable or malformed binding hides
  WhatsApp proposals from agents only and leaves every other Signoff read untouched; the owner's reads never consult the
  binding.
* **Rebind semantics.** Preparation freezes the connector's `account_ref` in the plan and job. Apply and dispatch require that
  (a) the binding still names the same agent as the proposer, (b) the binding's `account_ref` still equals the frozen one,
  and (c) the live connector still reports it. Rebinding to the same account but a different agent, or to a different
  account, therefore refuses every unsent approved job. The binding's `binary` and `sha256` are *not* part of the frozen
  identity: a connector upgrade that keeps the agent, socket and account does not invalidate approved jobs.

## Privacy

The exact recipient and text live only in the authoritative Signoff plan/notes and the job's arguments (the records that
make the approval and the single dispatch possible). They are kept out of everything else:

| Channel | Treatment |
| --- | --- |
| Titles, staging replies, errors, job summaries, logs | Generic fixed text (`Send personal WhatsApp message`); fixed error messages; the schema checks types only so validation errors cannot quote the message |
| Telegram Signoff bridge | `whatsapp_send` approvals are skipped (the bridge would render the preview fields); the owner decides in the dashboard |
| Operational Git export | `signoff_notes` of a `whatsapp_send` Signoff are excluded by a table selector; the exported `signoffs` columns are metadata only (no plan, preview, title or decision text) and `jobs` is private-excluded by the existing catalog |
| Learning sources | `whatsapp_send` Signoff responses are not learning inputs |
| Shared Context | Never written; the program has no declared outputs |
| `jobs_get` | The owner gets the job; an agent (the proposer included) gets state, attempts and error codes only |
| Private-DM exclusion | The v1 exclusion of private conversations from operational snapshots is unchanged |

Residual exposure that this change does not remove: the owner's own dashboard views, backups of the operational database, and
the same-UID limitation below.

## Engine ports (qorqut-operations)

Both are additive, generic and default to the previous behavior:

* `SignoffPorts.narrowing` (commit `2975533`): a host-supplied predicate that can only *remove* rows from every Signoff read
  (`AND (<one balanced predicate>)`, validated: no `;`, `--` or `/*`, placeholders match parameters). It grants nothing.
* `Program.routine_allowed` (commit `4dbaebc`): a program that is not routine-allowed cannot be named by `routines_create`;
  it is not part of the program definition digest.

## Transport and the same-UID limitation

The host talks to the connector's separate managed socket with the HMAC scheme described in the connector README. The
operator-created secret file lives at `<instance>/private/whatsapp-personal-send.key` (absolute, regular file, mode 0600,
32–4096 bytes) and is read by the host process only; it is not placed in any agent's MCP configuration. The binding (version 2)
records the socket, the secret file path and the frozen opaque `account_ref`; `config/whatsapp-personal.json` is resolved from
the runtime's instance root, never from the mutable `current` release pointer. Version 1 bindings remain valid and mean
read-only.

This is **not an operating-system sandbox.** The agent's native tools run as the same Unix user as the host and the
connector, so a process that user controls could read the secret file and sign requests. What the design guarantees is that the
product's own agent tooling has no send path, and that the owner's Signoff is the only authorization the product
recognizes. Strong isolation needs a separately configured service identity and socket ACLs, which is outside this change.
Rotating the secret needs a connector restart.

## Deployment prerequisites and order

Nothing below is performed by source work, and nothing is started automatically (no startup writes, no connector upgrade
database mutation). Production adoption is a separately authorized release.

1. **Deploy the host release.** With the existing read-only (version 1) binding the operation refuses with
   `Managed personal WhatsApp sending is not installed`; nothing else changes. Deploy the host first so the program exists
   before any binding offers sending.
2. **Register the trusted program worker (explicit maintenance step).** The `whatsapp.send` program runs as a system
   principal whose identity is derived, never configured: `uuid5(installation_id, "program:whatsapp.send")`. It is created
   by the installation's existing maintenance path, `BackgroundAuthority.register_workers`, like every other new program
   (compare the notes in `docs/TABLES.md` and `docs/BUSINESS-BACKUPS.md`). Run it once with the installed release's Python
   against the instance database, in the maintenance state the release recipe prescribes:

   ```python
   from qorqut_operations.background import BackgroundAuthority, ProgramRegistry
   from bazarlyk.background.integration import register_programs

   programs = register_programs(ProgramRegistry())
   authority = BackgroundAuthority(database, programs)     # `database`: the instance's operational database
   with database.transaction() as uow:
       authority.register_workers(uow, [])                 # whatsapp.send is Global-only: no membership is created
   ```

   It only inserts missing system principals; it never reactivates a revoked one, creates no business membership for this
   program, no Routine and no job, and is safe to repeat. *Verify:* `SELECT kind, active FROM principals WHERE id = '<uuid5
   above>'` returns `system, 1`. *Failure-closed:* until it is done, `whatsapp_send` refuses with the fixed setup error
   `Personal WhatsApp sending is not set up: the trusted program worker is not registered. The installer must run the
   documented worker registration step.` before any connector call, Signoff or job; a worker that is later deactivated stops
   dispatch the same way.
3. **Upgrade the connector installation** with an explicit managed-send upgrade (the final installer pass owns this
   recipe): it validates the existing installation, creates the secret file if absent, restarts the
   receiver with `--managed-send-socket` and `--managed-send-secret-file`, reads the account reference and writes the version 2
   binding. It preserves the data directory, session and send receipts and does not pair or start native sessions.
4. **Owner grant.** Set the Ordubashy profile's `whatsapp_send` override to `signoff` (the policy section above).

The upgrade automatically restores the previous receiver if an upgrade fails, and its explicit recovery command
handles interrupted or failed upgrades only. It does **not** reverse a completed upgrade. Reversing a completed upgrade
requires a separately reviewed procedure that accounts for queued and unconfirmed `whatsapp.send` jobs and restores the
version 1 binding and previous receiver before rolling back the host. An older host rejects a version 2 binding and cannot
run or settle these jobs. Sent messages cannot be recalled by rollback; the registered worker principal may remain inert.

## Known limits and assumptions

* The Go JSON encoder's HTML escaping is mirrored in Python for the receipt fingerprint and locked by shared vectors
  (`<`, `>`, `&`, U+2028/2029, control characters, newline). If the production Go toolchain ever changed that encoding the vector
  tests fail and, in production, receipts would not match and sends would fail closed as `external_result_invalid`.
* The send is bounded by WhatsApp's own acknowledgement: `accepted` is not delivery or reading.
* No UI, native-client, live-account or Telegram behavior was exercised; there was no live WhatsApp use.
