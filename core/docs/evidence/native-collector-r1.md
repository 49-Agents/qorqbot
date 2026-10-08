# D40 native collector service evidence

Date: 2026-10-08. Branch: `codex/build-native-collector-r1`.
Base: `a475fa5f87cb05b756c870d761c7ccc5b1bd11f2`.

This increment implements the passive `NativeCollector(application)` callable.
Application/server/monitor composition is reserved for Manager integration. These
are service-level checks, not normal-server or actual-native-provider acceptance.

## Implemented boundary

`runtime/native_collector.py` composes the existing discovery, NativeCapture,
Archive and NativeProjector callables. Construction performs no I/O; one caller
thread owns `tick()`/`close()` and the local capture connections. `view(agent_id)`
copies frozen cached observations without SQL or filesystem I/O and reports stale
observations after five seconds. Unconfigured collection is disabled and ordinary
agent targets are unsupported.

Only the reserved onbash workspace and its private `runtime/home` are inspected.
The collector verifies the complete workspace marker and opened directory/store
identities under the existing lifetime/effects guards, and uses saved admitted
session owners and the actual registered agent Invocation. It does not start a
CLI, require an admitted process to remain live, manufacture owners, write hooks,
or grant input readiness.

Discovery retries missing/delayed files at most once per second per owner and
caches successful admitted mappings. Each tick attempts at most sixteen owners
and services at most sixteen sources, with one MiB fresh polling, sixteen
transfer chunks and one MiB total projection allowance per source. Active/new
work gets priority while continuing rings retain historical progress. Prior
source epochs remain in the inventory and parsing ring. Resume links preserve
the earliest compatible host-linked parsing context and original provenance.

Manager decision 248 clarifies recovery eligibility: new owner/source discovery
requires the current epoch, while existing admitted mappings retain historical
owner/run recovery values as provenance. Verified historical reobservation is
restricted to those already admitted paths. There is no extra current-run-epoch
fence on polling, transfer or projection. Workspace/provisioning guards, Archive
registered-epoch/conflict holds, exact capture intent/receipt recovery checks and
the existing projector checks retain their authority; no hold is released.

The only changed existing seam is read-only `NativeCapture.list_states()`, which
uses its existing state reader for every admitted source epoch. No existing
capture/admission/parser/archive/publication behavior, host schema, public
operation or job-handler inventory is changed. There remain schema 7, sixteen
public operations and five handlers.

Status aggregates all saved epochs: captured minus acknowledged bytes, archived
minus handled-record bytes, capture discontinuities, parser gaps and fixed
operational diagnostics. A successful source cannot hide another source's lag
or gap. Operational source failures leave independent work eligible. Unexpected
service failures can propagate to monitor readiness.

## Focused checks

The four grouped tests in `tests/runtime/test_native_collector.py` passed:

- Both pinned providers (Claude 2.1.287 and Codex 0.160.0): delayed discovery,
  bounded missing retries, immediate raw partial-UTF-8/JSONL capture, completion
  after core/collector reopen, late bytes after verified process exit, a resumed
  native UUID under a newly admitted owner, stable run/source links and no
  duplicate ledger/history. Host chunk receipt coordinates/hashes, immutable
  stored object bytes, local receipt watermarks, agent attribution and actual
  dated JSONL publication bytes were inspected.
- Lost response after the real archive receipt commit followed by actual recovery
  activation and reopen: unchanged pending local payload and transfer intent,
  no ACK/reclaim, recovery-held host archive and honest raw/parse lag.
- Eighteen positively admitted owners, including seventeen already closed before
  the first tick: discovery/source budgets and continuation, draining over one
  MiB through a retained descriptor after replacement, verified divergent-epoch
  admission, fair projection across old/new epochs, independent-source progress
  during an injected failure and persisted gaps at zero lag after restart.
- Disabled collection, changed workspace ownership stopping effects, saved
  parser gaps at zero lag after reopen, frozen thread-safe memory-only views,
  stale observation handling and unchanged host/public inventories.

Publication checks explicitly pump this fixture's existing job queue. They do
not demonstrate scheduling by the normal server. Owned disposable Python child
fingerprints use the real SessionOwners process admission; provider records are
synthetic and reuse the contract-exact pinned parser fixtures. No native
executable, model call, owner session, ambient HOME or browser service is used.
Temporary installations and raw fixture files live under the private build state
directory and are cleaned up by the fixtures; child cleanup targets only owned
Popen objects.

Command (existing private test environment, no install):

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04 \
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/console-test-venv/bin/python \
  -m unittest -v runtime.test_native_collector
```

Result: four tests passed (19.468 seconds), including the final recovery-eligibility
correction. Private output:
`native-collector-r1-tests.log` under that build state directory. Development
checks corrected the test fixture's unused socket-path length and a fixture
helper name collision, and caught projection allowance accounting across epochs;
allowance is now reserved before consumption even when an operation fails.
`git diff --check` passed.

## Limits and provenance

These observations are not completeness receipts. Existing child/attachment and
unsupported/malformed-record limitations remain explicit; partial records remain
raw and pending until completed. Saved capture gaps are not cleared by observing
an equal watermark. NativeCapture retains its accepted full-prefix verification
per poll; the collector avoids repeat discovery scans on ordinary append/reopen.
It does not repair recovery holds, conflicts, changed ownership or missing files.

Normal-server scheduling, hook/setup composition, actual manually started CLI
flow, Chat dispatch, VM/browser/backup acceptance and complete G0–G6 remain open.
No deployment, main merge or PR is part of this assignment. New composition code
uses existing repository primitives; their source notices are unchanged.

## Manager acceptance

Manager reviewed the completed implementation at
`2b9e310eeb8292cf6b2b63b8b9af21cc3fc19fea` against D40 and decision 248,
including admission/recovery boundaries, continuing work budgets, stable parse
provenance, passive views and the sole read-only capture seam. I independently
ran the four owning checks; all passed in 20.982 seconds. Private command, log
and result: `manager-review251/` under the build state directory. The branch
was clean and its remote matched the reviewed commit. This accepts the service
composition only; normal-server wiring and the actual manual CLI flow remain
separate acceptance work.
