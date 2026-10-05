# Acceptance and release evidence

Status: planned tests, not execution results. No qorqutbot runtime exists in this baseline. The exploratory Qorqut results in [RESEARCH.md](RESEARCH.md) are source-selection evidence only; they do not pass any gate below.

This document defines how to verify the requirements and gates in the [global plan](IMPLEMENTATION-PLAN.md). The [shared contracts](CONTRACTS.md) define the expected behavior. A test that only reproduces an implementation's output is insufficient: assert durable invariants, externally observed effects and recovery outcomes.

The matrix is one integrated set of guarantees across `qorqut-core`, `qorqut-browser` and `qorqutbot-ui`, not three copies of a test suite. Keep each behavior's focused checks with its owner and combine related cases where one fixture proves them. Core tests coordination/storage/API, browser tests local lifecycle/tools/control with a small host binding, and UI tests interactions. No usage examples, example apps or tests that merely mirror implementation are required.

## 1. Test layers and fixtures

| Layer | Environment | What it proves |
| --- | --- | --- |
| L1 Contract/storage | Temporary SQLite databases, temporary data roots, fake clock/external boundaries | Transactions, validation, states, retries, cursors, packet selection and shared transport behavior |
| L2 Real processes | Isolated tmux socket, real PTY/recorder, deterministic producers and process trees | Interactive transport, process binding, durable-before-display ordering, stop/reconnect and input uncertainty |
| L3 Real VM/browser | Disposable KVM guests, isolated libvirt network, synthetic local web apps, real Chrome/noVNC | VM/storage/network identity, isolation, browser persistence, takeover and reboot behavior |
| L4 Native CLI | Exact pinned Claude/Codex binaries, separate native HOME, controlled fake provider endpoint where supported | Real native source/hook/resume formats, children, composer behavior and version compatibility |
| L5 Real provider | New disposable agents and a small real-provider task for each supported adapter | Actual authentication/startup, MCP interaction, transcript capture, peer messaging and native resume |

L1 cannot establish tmux or disk behavior; L2 cannot establish guest reboot behavior; fake providers cannot establish actual hosted-provider behavior. Record unsupported fixture capabilities instead of labeling them passed. Capture/durability failure campaigns use deterministic producers, not nondeterministic model output.

Every fixture owns its temporary data root, ports, tmux socket, VM/domain IDs, network and test credentials. It refuses to target an existing Qorqut installation or owner session. Test cleanup acts only on recorded fixture-owned resources. A failed test retains a small synthetic evidence bundle for diagnosis; credentials and real private histories stay outside the source repository.

## 2. Requirement traceability

All acceptance groups below are mandatory for the first complete v1 release. Individual gates select the evidence needed at that stage; a deferred integrated test is not a pass.

| Requirement | Acceptance IDs | Primary owners | Completion gate |
| --- | --- | --- | --- |
| R01 Stable identity and folder | A01.1–A01.3, A08.1, A09.2 | M01, M09 | G1, G5 |
| R02 Creation by owner or agents | A01.1, A01.4, A05.1, A10.1 | M00–M05, M10 | G1, G4 |
| R03 Independent VM/desktop | A02.1–A02.4, A03.1, A06.1 | M02, M03, M06 | G0, G2, G4 |
| R04 Host-global onbash | A01.3, A02.5, A03.1, A06.1, A09.3 | M01–M04, M06, M11 | G2, G6 |
| R05 CLI/model independence | A04.1–A04.4, A08.1–A08.3 | M03, M04, M08 | G2, G5 |
| R06 Persistent browser and intervention | A06.1–A06.8 | M02, M06, M10 | G4, G6 |
| R07 Durable coordination | A03.2–A03.4, A05.1–A05.5 | M03, M05 | G4 |
| R08 Advertised status and state | A01.5–A01.7, A10.1–A10.3 | M01, M04, M10 | G1, G4 |
| R09 Permanent change/message/creation history | A00.1–A00.3, A01.1, A01.5–A01.7, A05.1–A05.2 | M00, M01, M05 | G1, G4 |
| R10 All supported captured history | A04.2–A04.3, A07.1–A07.9, C01–C11 | M03, M04, M07 | G3 |
| R11 Continuity across providers | A08.1–A08.4, A09.1–A09.2 | M08, M09 | G5 |
| R12 All public operations allowed | A00.4, A10.1 | M00, M10 | G1, G4 |
| R13 Single-page chat UI | A06.2–A06.8, A10.2–A10.9 | M02, M06, M07, M10 | G4, G6 |
| R14 Recoverable operation | A02.2–A02.4, A07.1–A07.4, A09.2–A09.4, A11.1–A11.5, C01–C11 | M02, M07, M09, M11 | G6 |
| R15 Intent-based user presentation | A06.7, A09.5, A10.6, A10.8–A10.9 | M04, M06, M07, M09, M10 | G4, G6 |
| R16 Display names and twenty icons | A01.8, A10.7 | M01, M10 | G1, G4 |

## 3. Foundation, identity and state

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A00.1 | Crash before/after operation transaction commit; retry through HTTP and MCP | Mutation, event, initial receipt and typed job are all committed or all absent; identical retry returns the original receipt |
| A00.2 | Concurrent same-key requests; same key with changed arguments; stale agent/default-config revision | One committed effect; deterministic conflicts; no effect before rejected validation; core default changes leave existing agents intact and configuration reads expose no secrets |
| A00.3 | Restart mid-job, expired worker lease, long external call | Reconciliation uses persisted progress; external lifecycle lock prevents competing writers; SQLite transactions do not span external calls |
| A00.4 | Owner and two agent identities invoke the implemented public operation inventory | Same validation/operation set for every actor; actual caller attribution retained; no hidden role, business-scope or signoff branch |
| A01.1 | Owner/agent create, repeated and concurrent requests, crash during folder/VM provisioning | One UUID/folder/creation event per accepted create key; progress recoverable; partial registration is never advertised as ready |
| A01.2 | Rename, duplicate names, archive/restore, config change, changed cwd and concurrent archive/start/switch | IDs/paths stable; archive invalidates queued launch intent and verifies all execution stopped; explicit restore required to reopen admission; unregistered folders not silently adopted |
| A01.3 | Repeated/concurrent onbash initialization and archive/restore | Exactly one reserved host identity after bootstrap; same ID/history on restore; ordinary creates cannot create a second host-global identity |
| A01.4 | Capacity exhausted or CLI authentication missing during start=true creation | Durable accepted operation reports waiting/setup reason; no silent host fallback, uncontrolled launch loop or false ready state |
| A01.5 | 1/100/101-word statuses, Unicode whitespace, empty/over-4096-character text | Shared normalization and limits across transports; invalid input changes nothing; every real change has old/new text, actor and revision |
| A01.6 | Same-text status confirmation, repeated heartbeat, disconnect and recovery | No invented status-change events; review/contact times separate; one freshness transition to unknown, then one verified recovery transition |
| A01.7 | Delayed, duplicate and out-of-order activity events from current/retired runs | Every valid distinct observation retained historically; stale observations cannot replace current state; running process is not assumed working |
| A01.8 | Create/rename/icon changes from owner or agent, duplicate names, revision race and onbash rename | Single canonical display_name, twenty validated icon keys/defaults; IDs/paths/run unchanged; attributed changes; stale revision rejected; onbash reserved identity remains unique |

## 4. Environments, runner and native adapters

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A02.1 | Two real guests use the same guest ports and different workspaces/profiles | Distinct domain/agent identities, disks, cookies, native stores and files; mounted paths/UIDs are correct; host routes by verified identity |
| A02.2 | Actual systemd core/runner/browser-MCP service restart, then guest reboot | Independently supervised runtime cgroups preserve tmux/recorder/Chrome across service restart; browser reconciles persisted control/receipts; guest reboot changes boot ID and records lost processes while preserving files/profile |
| A02.3 | Missing state/workspace mount, stale IP, recycled PID/tmux name, protocol mismatch | Readiness or exact-target checks reject affected action; no temporary empty HOME/profile; an unknown old run continues to block competing start |
| A02.4 | Crash during domain creation, stop, image replacement and resource reconciliation | Repeated operations converge on owned resources; no duplicate disks/VMs; image pins and prior root retained; no unsafe qcow2 rebase |
| A02.5 | onbash beside real VM agents and existing unrelated host services | Same runner/capture/browser contracts, its own HOME and persisted unused ports; no VM created and no existing Qorqut session reused |
| A03.1 | Recorder-wrapped CLI in real tmux with attach/detach, resize, Unicode, paste and signals | Native interaction preserved; visible output does not precede local spool commit; multiple viewers do not restart/terminate execution |
| A03.2 | Idle, busy, nonempty, modal, login and shell composers | Automated input is submitted only to a verified ready native composer; all other cases defer or expose setup; never paste into an unintended shell |
| A03.3 | Kill at dispatch intent, before/after paste, before/after Enter and before/after receipt | Positive no-input evidence permits retry; any possibly performed effect is uncertain; stable attempt retry never pastes a second time |
| A03.4 | Concurrent manual input/automated delivery, stale same-run viewer epoch, disconnect/restart/takeover | One input owner; observer cannot type; stale frames rejected; human mode persists without auto-expiry; explicit take/release reconciles epoch and rechecks composer without clearing text |
| A04.1 | Preparation repeated after authored persona/notes change | `persona.md` and workspace data survive retries/provider preparation; generated entrypoints refer to the same authored source; argv/env/cwd are structured; secrets absent from general logs |
| A04.2 | Pinned Claude and Codex with exact native session bindings and emitted hooks | Real native file/event fixtures captured, version/capability inventory saved, observed state tied to exact run/process; ambiguous binding blocks dependent guarantees |
| A04.3 | Native child sessions, native resume, cwd change, native filename/schema variation | All owned parent/child sources retained; source ownership does not depend on cwd/current marker; unsupported format remains raw and visibly unparsed |
| A04.4 | Model/provider selection changes and unsupported CLI/native version | Agent identity/history unchanged; supported behavior follows adapter capability; terminal-only evidence is labeled without claiming semantic restoration |

P0 runs A02.1, A03.1 and the discovery portions of A04.2/A04.3 as feasibility rehearsals. Save actual observed limitations before freezing adapter and recorder contracts. These prototypes do not count as production implementation.

## 5. Messaging and browser control

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A05.1 | Three independent agents plus onbash discover/contact peers; agent creates a peer | Directory includes every active agent's IDs/status/state/freshness; full messages and creation attribution persist; delivery targets exact run |
| A05.2 | Offline recipient, repeated send, restart and reply in a thread | One immutable message per send key, durable queue, full body/timestamps/relationships retained; reply/read does not imply handling |
| A05.3 | Busy/human-controlled recipient then release; 64-KiB message and overlimit input | Deferred message survives and later submits once; limits are explicit without truncation; queued order is stable |
| A05.4 | Lost runner response/receipt and explicitly resolved uncertain delivery | Saved/submitted/handled are independently inspectable; no automatic uncertain replay; explicit retry records rationale and duplicate-processing risk |
| A05.5 | Exact acknowledgment, duplicate acknowledgment, alternate actor acknowledgment | Handling receipt names message and actual actor; retries are idempotent; all-allowed v1 does not introduce a recipient-only permission check |
| A06.1 | Synthetic login in two guests and onbash, browser restart and VM reboot | Separate cookies/profiles; each agent retains its own login under the synthetic site's rules; viewer reconnect uses existing browser |
| A06.2 | Request human input while a slow browser MCP command is in flight; concurrent runner-proxied command | Separate browser server calls core's handoff API without core/UI imports; core commits atomically; direct/proxied input uses one gate; requested/yielding differs from ready/human; automation drains before human input |
| A06.3 | Concurrent/repeated Done/Cancel, cancel while yielding/disconnected, manual takeover/release, stale target, offline CLI | One terminal request transition and one correctly typed completion or cancellation message; stale input rejected; automation waits for settled command/new epoch; no success claim on cancellation |
| A06.4 | View-only connection sends keyboard/mouse/clipboard input, then human takeover and release | Server-side relay enforces current control mode/epoch; client-side noVNC options alone are insufficient; obsolete input connections lose control |
| A06.5 | Core/runner/browser-MCP/Chrome restart during handoff; handoff HTTP response lost | Request/actor/key and local control receipts persist; same-key retry retrieves one request; no false notification, stale-tab reuse or silent release; direct MCP cannot bypass a hold; unknown browser effects are not blindly repeated |
| A06.6 | Browser and Machine in two guests plus onbash | Dedicated TigerVNC Chrome and separate usable machine desktop resolve to exact distinct surfaces; no fallback/extra Chrome; VM console is private and actual desktop usable; onbash does not reuse owner display |
| A06.7 | Inline card, top-right expansion, collapse and inspector transfer | Direct pointer/keyboard/touch works inline and expanded, scaling coordinates correct; same profile/browser/request; one active UI input connection; no resize race or focus theft; closing does not complete request |
| A06.8 | Machine takeover during setup/stopped CLI, existing human ownership, partial failure, new run and reboot | Agent/environment hold covers future launches/input; verified-absent participants quiescent, unknown participants reconciling; no fake browser card; conflicts explicit; partial holds recover; only owned holds released; old viewers invalid after reboot |

## 6. Archive matrix

Use known byte streams and saved hashes as the oracle. Compare reconstructed source bytes to the producer's emitted/committed data, and report the precise boundary of any unknowable producer buffer. Native and terminal streams have distinct expected inventories. A rendered Chat screenshot is never the archive oracle.

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A07.1 | Crash campaign C01–C11, duplicates and conflicting coordinate | No acknowledged byte disappears; duplicate receipt is stable; conflicts preserve existing bytes and explicit conflict evidence |
| A07.2 | Host/runner outage, partition, guest abrupt shutdown and reconnect | Persistent spool survives; collector resumes host pulls and ACKs only archived ranges; backlog drains without duplication; unknown unflushed producer bytes remain disclosed |
| A07.3 | Host archive or guest spool near/full disk and write/fsync errors | Stop admission with reserved headroom; pause producer subtree while recorder/runner remain runnable; no silent rotation/drop/false ACK; recovery resumes saved lineage |
| A07.4 | Truncate/rewrite/rename/unlink/replacement, VM rollback and late retired source | Old archive retained; held descriptor drained where possible; new epoch/incarnation and gaps recorded; old bytes cannot overwrite current coordinates |
| A07.5 | Partial JSONL at restart/exit; transcript >8 MiB; giant record spanning chunks | Entire raw stream retained; partial suffix is not parsed early; large record cannot block later records; bounded UI/index has raw retrieval links |
| A07.6 | Native children, attachments, missing/deleted attachment, current run changes | All registered sources discoverable by owning agent; emitted attachment bytes preserved when available; gaps explicit; current UI selection cannot hide archive inventory |
| A07.7 | Native resume, identical repeated text, known native echoes, terminal/native overlap | Source reused through explicit run link; deduplication uses proven coordinates/native IDs; distinct equal-text turns stay distinct; terminal not guessed into semantic Chat |
| A07.8 | Unknown/invalid schema, parser crash, parser version upgrade and FTS rebuild | Raw hashes unchanged; outcomes/counts/lag visible; rebuilding gives equivalent expected records; unindexed content remains retrievable |
| A07.9 | Large-history list/search/raw downloads under ongoing capture | Stable bounded cursors and exact byte ranges; reads do not change capture; memory bounded by chunks, not whole transcript size |

### Crash boundaries

Inject failure immediately before and after each durable boundary, not just at a convenient high-level function entry. Use both deterministic process failure and selected real guest reboot cases. Assert invariants after a new process reopens storage.

| ID | Boundary | Expected recovery |
| --- | --- | --- |
| C01 | Run/source registration before native launch/discovery | No unregistered managed launch; incomplete preparation inspectable; exact source registration retry is idempotent |
| C02 | Read native bytes / local chunk-and-cursor transaction | Cursor cannot advance without bytes; file sources are reread from committed offset; committed PTY output survives; read-but-uncommitted PTY bytes are an explicit interrupted-capture boundary |
| C03 | Local commit / forward PTY output to tmux | Every displayed byte has a local durable record; committed but undisplayed bytes remain available after crash |
| C04 | Host pull / chunk verification / temporary-object write | No ACK or accepted watermark before valid durable bytes; pending chunk stays in runner spool |
| C05 | Object fsync / immutable install / parent-directory fsync | Installed orphan is verified and reconciled or quarantined; retry cannot overwrite accepted history |
| C06 | Host chunk receipt-and-watermark commit | Receipt never points to an unflushed accepted object; DB failure retains pending local bytes |
| C07 | Host commit / ACK request lost in transit | Same chunk can be pulled again; existing receipt determines ACK; no second raw/semantic record |
| C08 | Runner ACK commit / spool payload reclamation | Locally uncommitted ACK causes harmless repeat; only acknowledged ranges may be reclaimed |
| C09 | Parser record/checkpoint/active-projection switch | No raw mutation; resume/rebuild restores projection; partial rebuild is never advertised current |
| C10 | Activity/receipt event local commit / host commit / event ACK | Valid changes survive reconnect; deduplicated by source sequence; current projection obeys run/sequence fencing |
| C11 | Final drain / source checkpoint / stop/switch/backup checkpoint | Completion claims match observed exit and offsets; forced/unknown stop records gaps; no premature release of replacement input |

The fault model assumes storage honors fsync. Losing the only physical storage device is handled by a separately measured backup recovery point. Tests must not turn unknowable data into fabricated “complete” records.

## 7. Continuity, files and recovery

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A08.1 | Claude → Codex → Claude with authored files, synthetic cookie, pending/uncertain messages | Same agent/conversation, workspace/profile and full raw inventory; distinct runs; captured context/native resume lineage; no silent replay or identity reset |
| A08.2 | Long history, oversized mandatory/optional input, parsing lag/gaps and pinned facts | Selection waits for recorded raw/parsing checkpoint; explicit degraded ranges if chosen; deterministic bytes/hash; mandatory overflow fails visibly, optional omissions have references; no historical prompt promotion |
| A08.3 | Kill at each switch checkpoint; old run unreachable; target launch/setup failure | One live input owner; old run verified stopped before replacement; saved packet and frozen queues recover; failed target is visible; no hidden rollback/model turn |
| A08.4 | Same-client native resume or explicitly selected memory-only recipe | Exact resume binding verified without another automatic startup prompt; fallback is recorded; recipe follows D08; packet transport differs from observed recall |
| A09.1 | Direct CLI edits plus API atomic file/memory writes, conflict, packet read during edit and restart | Plain files usable; API atomicity/hash checks hold; detected unstable reads retry/fail; selected bytes retained with hashes; no clobber or universal direct-edit version claim |
| A09.2 | Stopped full-agent export/import with cross-agent messages and reused names | UUID/history/source hashes retained; central-record slice/provenance included; destination-local event cursors remapped; missing peers remain historical references; unfinished effects held |
| A09.3 | Stopped full-installation backup of VM agents and onbash, restore under another absolute root | All owned writers verified stopped; onbash WAL-backed spool and separate browser receipt store included; exact disk backing images/paths and hashes verify; history/holds/queues survive; no automatic launch or replay |
| A09.4 | Online archive backup while capture continues, then isolated archive restore/reindex | Snapshot's immutable inventory verifies; archive queries rebuild read-only; operational queues never resume; no full browser/VM/workspace recovery claim |
| A09.5 | File reference with relative/absolute guest/onbash path, missing source, retry, changed/deleted file and import | Correct environment resolution; one reference/entry/event; no guest path read on host; actual observed metadata, no invented snapshot; source errors explicit; references/provenance survive provider changes/export |
| A11.1 | Fresh headless installation of separate core and browser services; attach independent UI build afterward | No Qorqut checkout/config/data needed; pinned services/image install; browser MCP runs without core/UI imports and advertises supported tools; native CLIs discover both servers; UI attaches without migrating backend state |
| A11.2 | Host schema/runner/CLI/browser/UI update and unsupported downgrade | Explicit core migrations/backups; pinned browser/runner/API compatibility checked; unsupported UI API fails clearly; live clients not assumed reloaded; rollback cannot silently discard work/profile changes |
| A11.3 | Invalid backup, identity collision, second host clone, or original performs input after snapshot | Invalid activation rejected; fresh recovery epoch/tokens fence old control; restored unfinished effects held for reconciliation; missing newer receipt never proves no effect; onbash clone rejected |
| A11.4 | Measured multi-agent capacity, queue/capture load and disk recovery | Publish tested CPU/RAM/disk/version envelope, response/catch-up observations and admission thresholds; no invented scalability claim |
| A11.5 | Logs/diagnostics/operator recovery for uncertain input, stopped capture and unavailable guest | Stable IDs and actionable typed causes; no secrets/private content in general logs; operator can inspect/reconcile without editing database rows blindly |

## 8. Transport and single-page chat workspace

| ID | Scenario | Required evidence |
| --- | --- | --- |
| A10.1 | Core HTTP/MCP/operator parity and independent browser MCP calls with UI absent | Shared per-domain schemas/results/idempotency/state rules and actor attribution; browser tools only on browser MCP; core owns coordination; no inert tools or duplicate browser execution path |
| A10.2 | Agent list, terminal, desktop and request/Done flow | Correct target IDs and observed status; queued/uncertain/error states remain visible; connect/disconnect does not launch/kill processes |
| A10.3 | Events reconnect, pagination and stale client selection | Durable cursor replay without dropped changes; target checks prevent actions on a replacement run/browser; bounded results link to full artifacts |
| A10.4 | Real browser console against disposable VM agents and onbash | Terminal/VNC viewers work through host proxy; no guest tokens in frontend state/logs; escaping handles arbitrary agent/status/message text |
| A10.5 | Wide/narrow viewport, select agent, collapse inspector, scroll and draft | One page: left agents, center chat, right collapsible inspector; narrow drawer behavior preserves usable chat; selected IDs/draft/scroll stay correct; minimal black/white/gray styling and visible focus |
| A10.6 | Browser/file tool retry, refresh/SSE replay, crash at record transaction and late native parse | One durable card per request/reference; atomic entry/event/domain record; no duplicate toast on replay and no tool re-execution from transcript parsing; completed cards do not reconnect input |
| A10.7 | Twenty-icon create picker, single-select/double-click rename, Enter/Escape/blur, keyboard/touch and conflict | Icons separate from text; display_name shown while IDs route; Enter saves, Escape/blur cancels unsaved edit; accessible rename alternative; validation/conflict inline; no runtime restart |
| A10.8 | Actual native MCP descriptions/calls for browser action and file reference | Instructions explain when to request user action/show a file, without iframe/modal/toast mechanics; prompt/path sufficient domain inputs; backend resolves binding; saved result does not claim user viewed it; no file preview/Open control/content fetch in initial UI |
| A10.9 | Owner composer, native echoes, delayed history, agent switch and projection rebuild | Shared durable message delivery; exact proven echo linking only; stable entry/source coordinates and bounded paging; late capture retains original times; raw archive unchanged and separate from Chat |

The included chat scope is in [UI-PLAN.md](UI-PLAN.md). No large design system or file-rendering subsystem is required. Browser/file cards, icons/rename and the three-column shell are required; direct opening/preview of referenced files is explicitly deferred. Routing stubs do not replace real interactive VNC tests.

## 9. Gate evidence and release decision

| Gate | Required evidence |
| --- | --- |
| G0 Feasibility | A02.1/A03.1 rehearsal, synthetic-cookie restart A06.1, distinct browser/machine surface rehearsal A06.6, source observations A04.2/A04.3 and stopped restore rehearsal; exact pins/limits |
| G1 Durable identity | A00.1–A00.4, A01.1–A01.8 with fake executors where necessary; HTTP/MCP parity for completed operations |
| G2 Execution | A02.1–A02.5, A03.1–A03.4, A04.1/A04.2 on real processes/guests; early recorder active |
| G3 History | A04.3, A07.1–A07.9 and C01–C11, including real VM/network/disk cases; C11 uses fake switch/backup consumers until integrated G5/G6; complete supported-source inventory |
| G4 Coordination/chat | A05.1–A05.5, A06.1–A06.5/A06.7, A09.5, A10.1–A10.3/A10.6–A10.9; real inline browser interaction and durable cards; earlier guarantees still hold |
| G5 Continuity | A04.4, A08.1–A08.4, A09.1–A09.2 on both supported native adapters |
| G6 Deployable release | A06.6–A06.8 integrated surfaces/control, A09.3–A09.4, A10.4–A10.5, A11.1–A11.5 and L5 smoke for each adapter; all R01–R16 traced to passing evidence for the candidate build |

For each result, save: acceptance ID; owning repository/commit and integrated core/browser/UI pins; UTC execution time; command/reproduction steps; test layer; fixture seed/data manifest; host/kernel/filesystem and dependency versions; guest image digest; CLI/model/provider and API/runner protocol versions where applicable; expected invariant; observed outcome; injected failure point; before/after IDs/watermarks/hashes; artifact references; known limitations. Use explicit `passed`, `failed`, `blocked` or `not_run`, not an ambiguous checkmark. A skipped test is not passing evidence.

Keep synthetic reports and concise release summaries under a future `docs/evidence/<release>/` directory. Large raw fixture bundles and operational evidence live outside Git with checksums/retrieval locations in the report. Never commit credentials, private transcripts, browser profiles or VM images as test evidence.

Release review resolves every failed critical invariant before calling v1 complete. Reuse earlier evidence only when the candidate changes cannot affect that contract and the reviewed dependency/image pins still match; record that reasoning. Run the fresh-install/restore and real-provider smoke on the release candidate. Publishing this planning repository satisfies none of these runtime gates.
