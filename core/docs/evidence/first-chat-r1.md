# First Chat without a manual seed: D52 evidence

2026-10-09. Implemented frozen Manager D52 on `codex/build-first-chat-r1`,
based exactly on `d0845674e006fe623d0f3395b81f8707a8f53a4f`.
Manager review remains separate.

`runtime/native_collector.py` now publishes private, five-second
`awaiting_primary` evidence from successful discovery of the bound native UUID.
It retains discovery time rather than refreshing freshness with its cache.
Owned-store/directory/recovery guards, current ownership, no prior local/host
primary across runs, and the bounded discovery-gap allowlist are required.
Missing-root checks inspect only explicitly reported provider roots and refuse
existing or uninspectable roots. Failure, close and restart clear this evidence.
Public collection stays waiting until real source admission.

`runtime/input_dispatch.py` retains accepted-hook precedence. Only an owner
without an accepted hook may use its exactly bound Codex 0.160.0 D51 startup
event. Initial SessionStart/startup may use fresh collector waiting evidence;
Terminal recording, process/configuration/owner checks, human-control exclusion,
fresh empty-composer classification and submission revalidation still apply.
The existing event-consumption barrier, InputTarget, saved JSON and hashes are
unchanged. No dispatcher, runner, classifier, native setup, schema or UI change.

## Focused checks

Private evidence root (`$E`):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-first-chat-r1
```

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests $E/venv/bin/python -m unittest runtime.test_native_collector runtime.test_input_dispatch -v`:
  **38 passed in 74.996s**, recorded in `checks/unit-final.log`. Coverage includes
  cold discovery for both providers, malformed/conflicting/partial inputs,
  unknown errors, exact missing roots, stale/failure/recovery/prior-primary
  refusal, startup provenance, accepted-hook precedence, drafts/human holds,
  Terminal capture, submitted/uncertain event consumption and historical decoding.
- Three completed historical attempt records decode under the installed
  candidate with their original hashes unchanged (`evidence/historical-attempts.json`).
- Wheel build/install and `git diff --check` passed. All 85 installed Python
  sources match the candidate. Wheel SHA-256:
  `f08010e793ee867025513203645a558675099c66c5edd9bc818966f8094d2a55`.
  The initial unit run's two fixture assumptions were corrected; logs remain.

## One installed Codex flow

Copied completed startup/connected-Chat helpers into fresh private state;
their 12 original files still match saved hashes. Used new HOME/native store,
core data, credentials, browser profile and user/network/PID/mount namespace
with loopback only. UI pin `6e3cb819764dd706accb12528d9f40fd4ff70da6`
matched all 17 production inputs of the reused completed compiled artifact;
all four served/reused assets have verified hashes (`provenance.json`).
The older connected-Chat bundle differed from this pin and was not used.

Manually launched pinned Codex 0.160.0 once in actual Terminal using the
existing local canned provider. Completed visible hook trust/warning setup,
then explicitly released control. `09-no-seed` records one startup event 11,
zero official hooks, messages, attempts, native files/sources and provider
requests; collection is `waiting_source`, with healthy Terminal recording.
Sent two distinct Unicode/multiline bodies from real Chat, without a manual seed.

| Evidence | First Chat | Second Chat |
| --- | --- | --- |
| Message | `ef2f78ac-efa9-41ca-9e86-e83f006d1e7b` | `94db98d4-bc24-4c93-a656-8967f6b9dcb5` |
| Attempt | `2cb89536-00a4-49bf-9298-46986e4d9c1c` | `0701f904-2e6e-402c-b20c-cfd850e8d7f3` |
| Input observation | Startup event 11, control epoch 2 | Real Stop event 45, control epoch 4 |
| Delivery | One submitted 51-byte notification/receipt | One submitted 51-byte notification/receipt |
| Agent handling (UTC) | `05:31:04.285009` | `05:32:29.482287` |

The first native `messages_list` completed before Codex showed its ordinary
`messages_acknowledge` permission prompt. The read-only UI reply assertion
timed out there; its failure screenshot/text and response 014 remain intact.
Manager message 346 authorized one-call **Allow** for each exact pending ack.
For each, took Terminal control, verified tool/message, accepted default Allow
once and released control. No permission-mode/configuration change or input
replay occurred. Both actual MCP reads returned the exact bodies; both actual
acknowledgements attributed handling to the bound agent. Delivery remains
`submitted / processing_not_confirmed` separately from those handling records.
Each canned assistant reply appeared in Chat. Native asynchronous completion
timestamps are not used to infer acknowledgement/assistant ordering; durable
handling timestamps precede the replies.

Native UUID `01a11f20-2472-7161-aed9-4e843efb4cbc`, owner/run
`e4f3d215-5ade-4fb3-8452-3e4afe5303c9` (generation 1), source
`c8b2521c-beae-4e0f-9b4e-bb686557c0dc` were produced by the real CLI.
The collector automatically admitted the primary after first input. Before
exit, contiguous host raw capture matched all **89,100 bytes** of that primary,
SHA-256 `0283ac1edee290c6561cbbf1dc94c8947ba2889c52077697f22bcd9cedd87b4e`.
Automatic `2026-10-09.jsonl` contained six entries, **4,812 bytes**, SHA-256
`f4fb6217beb4f5112ba0966363f7af01f4ea80871afa1eaab81f0a1928682535`.
Two owner bodies, two separate native notification echoes and two replies
matched HTTP projection/daily records and raw spans. No manual observer pump,
native file/hook/UUID fabrication or SQL mutation was used.

`evidence/26-two-complete-verified.json`, its primary raw capture, snapshots,
screenshots, input frames, receipts and `checks/actual-flow-audit.log` retain
the successful artifact checks. Two earlier audit-only assumptions (native
completion ordering and receipt field name) were corrected without replaying
the flow; their logs also remain. The provider's automatic title-request
fixture string contains “seed reply”, but occurred after first Chat and was
not a user seed; the primary contains exactly two native UserMessages.

Sent `/exit` once and observed native absence/closed owner. Normal owned-fixture
cleanup left no live saved/namespace processes or launcher and both ports
refused connections. The redundant already-exited-browser stop diagnostic is
preserved; cleanup sent no unverified signal.

This demonstrates actual CLI input, tool processing and automatic capture with
a canned provider, not general model/provider acceptance. Existing parser
coverage gaps and conservative public `input_ready=false` remain; this is not
a complete-transcript claim. No restart/responsiveness matrix, blocked Claude
MCP/browser reviewer retry, VM/browser-service acceptance, external/paid model
call, nested review, PR, merge or deployment was performed.
