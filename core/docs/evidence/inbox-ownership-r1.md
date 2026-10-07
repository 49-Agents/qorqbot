# Inbox ownership r1 evidence

Implementation of D37 on `codex/build-inbox-ownership-r1`, based exactly on
`f4506c705f77f9c1b6b1575f1905b95c77b7c27f`. Submitted for independent review;
this is not deployment or connected-native-system acceptance.

## Implemented boundary

`qorqut_core.runtime` supplies the trusted `OwnerCandidate`, `OwnerConnection`,
`SessionConflict`, three-state local process observer, `SessionOwners`, and
one sequential `SessionMonitor`. Claim admission compares the complete binding,
retains live/unknown incumbents, and fences observation/write races with at most
three attempts. UUID ownership is also the run UUID. Exit, replacement, and
recovery transitions commit with attributed events. Recovery retires ownership
without rewriting old connection identities or operation receipts.

Migration 6 adds the typed owner history, foreign keys, active-owner/generation
uniqueness and immutable identity/history guards. Migrations 1–5 match the base
byte-for-byte and retain their checksums. Assembly adds only `runtime_get`
(15 public operations, five unchanged handlers). The read does no process I/O,
projects live evidence for at most five seconds, and always reports
`input_ready: false`. Server readiness includes monitor health; shutdown joins
it before application storage closes, including cancelled partial startup.

Other production edits are limited to application assembly/recovery and server
lifespan. Existing test edits are mechanical inventory/schema expectations.
Agent creation, AgentView/workspace fields, dependencies, adapters, runner,
capture, messaging delivery and UI are unchanged.

## Checks

All execution used Python 3.12, private environments with `requirements.lock`,
temporary installations and positively owned children/ports. Commands below ran
from the worktree unless noted. Logs and wheel are under ignored `artifacts/`.

| Check | Result |
| --- | --- |
| `PYTHONPATH=src:tests .venv/bin/python -m unittest -v runtime.test_monitor runtime.test_owners runtime.test_process runtime.test_storage runtime.test_transports test_foundation.FoundationCase.test_config_validation_secrets_and_snapshot_defaults archive.test_inventory.InventoryTests.test_legacy_v3_migration_preserves_data_and_prior_checksums` | **14 passed**, 11.733 s; final source |
| `PYTHONPATH=src:tests .venv/bin/python -m unittest -v test_foundation agents.test_agent_flow archive.test_passive archive.test_inventory transports.test_console transports.test_worker transports.test_transports` | 62 checks: 59 passed; two inventory assertions corrected and passed in the final command above; one unchanged base failure below |
| Exact-base archive: `PYTHONPATH=artifacts/base/src:artifacts/base/tests .venv/bin/python -m unittest -v agents.test_agent_flow` | Reproduces the unchanged provisioning assertion failure, 1.871 s |
| `.venv/bin/python -m pip wheel --no-build-isolation --no-deps --wheel-dir artifacts/wheels .` followed by install into `artifacts/installed-venv` | Build/install succeeded; all locked dependency versions checked |
| From `artifacts/outside`, `PYTHONPATH=$WT/tests $WT/artifacts/installed-venv/bin/python -m unittest -v runtime.test_owners.Owners.test_owned_children_race_reconnect_reopen_exit_and_stale_release` (equivalent unittest loader with provenance assertions) | **1 passed**, 0.707 s; 61 source/installed files identical; all 46 imported core modules came only from the installed package |
| Base migration source-prefix comparison; `git diff --check` | Both passed |

The owning checks cover actual child-process claim races, reconnect, two processes
resuming one synthetic native UUID, competing terminal/provider identities,
reopen, exit, stale release/reconciliation, three-attempt fencing, unknown and
unreadable observations, malformed `/proc`, PID start/boot changes, five-second
freshness, atomic rollback/recovery, immutable history, monitor failure/readiness
and shutdown, plus real HTTP/official MCP SDK/operator reads. VM candidates are
rejected before observation, including with an injected observer.

The migration fixture constructs a genuine schema-5 database, saves a message
and file/operation receipts, upgrades explicitly, compares all old table rows
and checksums, and replays the original message receipt unchanged. Its initial
workspace/publication ordering was corrected before the final passing run.

Wheel: `qorqut_core-0.1.0-py3-none-any.whl`, SHA-256
`349bbb0b82c525c55053e4161736711dfcc55a5e0e249e09a2f20b158b572bdc`.
The implementation reuses the repository's existing process fingerprints and
exact compatibility profiles; no additional upstream extraction or dependencies.

## Remaining limitations

`agents.test_agent_flow` line 40 expects onbash's workspace timestamp to remain
`None` after starting the real job worker. Both this branch and the exact base
observe a completed workspace instead. Only its operation-count assertion was
updated here; combined-version test corrections remain with integration.

Native UUIDs/provider/version fields in these fixtures are supplied metadata.
Real children are disposable Python processes, not native model sessions. This
increment does not establish hook-to-process/native-file association, start a
CLI, consume an inbox, submit terminal input, capture native assistant history,
authenticate a provider, or provide UI Terminal/browser/VM readiness. Local
binding supports reserved onbash only. The future startup bridge and integrated
delivery/capture flow remain required. No native turns, owner stores, live
Qorqut data, merges or deployments were used.
