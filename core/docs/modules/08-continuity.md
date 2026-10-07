# M08 — Memory, native resume and provider switching

Current v1 scope: [D36, terminal-owned sessions](../TERMINAL-OWNED-SESSIONS.md)
supersedes earlier automatic CLI start/restart/provider-switch and context-packet
requirements below. Creation establishes an agent and shell; the user starts the
CLI. One logical inbox owner connects Chat. See that plan for the exact current
creation contract and revised acceptance. VM setup remains open.

Status: Planned. Owns R11. Depends on M01/M03/M04/M07/M09. Automatic bounded restoration is the pending-owner default D08.

Repository: `qorqut-core`. Provider switching is backend coordination; its checkpoints never depend on UI state.

## Outcome and boundary

The agent remains the same logical worker when its CLI, model or execution environment changes. Identity, authored files, browser profile, messages and captured history persist. Native resume and cross-provider context restoration are separate mechanisms with different evidence.

Use current authored memory and deterministic context selection. No mandatory summarizer model, background autonomous manager or second conversation runner is needed. The host performs mechanical orchestration; it does not infer that an LLM has understood or completed something.

## Saved context

`workspace/memory.md` is the agent's current authored handoff. Instructions ask the agent to maintain decisions, current work, important findings and next actions in notes/memory. This improves continuity but cannot recover unsaved thoughts after a crash.

Context packets contain current platform identity/instructions, exact authored `persona.md` and memory snapshots, pending human input/message references, explicitly pinned artifacts and a bounded recent conversation selection. Pending message references are context, not another copy of queued input to process. Capture the file/history IDs and hashes used. Use a configured conservative byte/token budget appropriate to the tested target adapter; reserve mandatory identity/instruction budget first, then select whole records. Oversized optional inputs become references with explicit omissions; mandatory content that cannot fit fails preparation visibly. Never silently clip bytes or records.

Plain files have no universal committed revision because native tools can edit them directly. Build packets from checked stable reads (identity/stat/content checks), retaining selected exact bytes in the immutable packet or attachment. Detectable concurrent changes cause bounded retry or an unavailable result. A hash without saved bytes is insufficient when historical retrieval is promised. This does not promise transactional snapshots across arbitrary non-cooperating file writers; stop/quiesce cooperating writers for the normal switch and record any unresolved inconsistency.

Select history against the finalized raw-source checkpoint from the old run. Wait for M07 parsing through those watermarks, including explicit invalid/unknown/unindexed outcomes; FTS completion is unnecessary. A blocked parser waits by default. An explicitly selected degraded restoration records unavailable ranges/reasons and raw retrieval references; it cannot silently claim the last captured turn was included. Archive gaps remain separate from intentional context-budget omissions.

Old system prompts and tool output are labeled historical evidence. They do not become new privileged instructions. Complete prior history remains accessible through `history_search/get/raw_get` and files, even when omitted from startup.

Save the exact packet under `history/context-packets` with recipe version, target configuration, selection budget, source watermarks/hashes, omissions and delivery outcome. Packet generation is deterministic given those inputs. Model-written summaries, if later added, remain source-linked derived artifacts with explicit provenance.

## Switching state machine

Persist a `runtime_switch` operation with old run, target adapter/model config and phase:

1. **requested:** validate target capabilities/config without disrupting current execution.
2. **quiescing:** suspend automatic delivery to the old run and choose a stop boundary. Default waits for an idle safe boundary; explicit force mode records interruption.
3. **draining:** checkpoint/archive known sources and request a normal stop. After verified exit, perform the final drain; a pre-stop drain alone is not final evidence.
4. **old_run_finalized:** record clean or interrupted capture outcome and release old input ownership only after confirmed exit.
5. **preparing_target:** wait for the selected parsing barrier and build a saved packet, or choose exact native resume without a packet. Preserve the VM/profile/workspace and register the next run/generation. Recheck lifecycle intent so archival supersedes a queued switch.
6. **starting_target:** launch through the recorder and verify native binding/capture readiness.
7. **restoring:** for packet mode, deliver the saved packet through the adapter's tested mechanism with an input receipt; uncertain delivery remains uncertain. For native-resume mode, verify the exact resumed native/source binding and record `native_resumed` without another startup prompt. Any additional context injection is a separately requested, recorded delivery.
8. **completed:** admit queued messages only after readiness and restoration outcome meet the contract.

A target launch failure leaves the old history intact and the operation failed/waiting at its saved phase. Do not silently relaunch the old provider or clear pending messages. Explicit recovery can start another chosen configuration with a new run ID. An unknown old process prevents competing launch until reconciled.

Same-provider native resume verifies the exact native session and source lineage; “latest session” selection is forbidden. Same CLI with a changed model may continue or require a new run according to its native contract. Record observed model changes without resetting history.

VM replacement/reboot preserves files/native stores but loses running processes. Recovery decides whether to resume or start with a packet, using the same operation machinery. A host HTTP restart merely reconnects and does not trigger a switch or another prompt.

## Implementation sequence

1. **M08.01 Context reads:** define exact memory/history/artifact snapshot inputs and hash/provenance capture.
2. **M08.02 Packet builder:** implement deterministic record selection, size budgeting, omission metadata and archive installation.
3. **M08.03 Native resume:** implement per-adapter verified session selection and repeated resume reconciliation.
4. **M08.04 Switch orchestration:** persist phases, locks/input gating, stop-confirmation, final drain and replacement launch.
5. **M08.05 Delivery/recovery:** save packet delivery receipts; reconnect without another startup prompt; expose failed/uncertain restoration.
6. **M08.06 Acceptance:** execute A→B→A and interrupted-switch cases with synthetic native sources and real VM/CLI fixtures.

## Source decision and acceptance

P12: use Qorqut's stable shared identity and saved working-memory concepts. Its dual-CLI setup explicitly excluded native conversation transfer; cross-provider restoration and complete-source draining are new code.

Tests verify ID/profile/workspace preservation, pending messages, clean/forced stop, unreadable/concurrently edited memory, retained snapshot bytes, changed artifact hash, parsing lag/gaps, oversized mandatory inputs, long history, target launch failure, crash at every switch phase, archive/switch race, packet size/omissions, historical prompt labeling, repeated same-text turns and exact native resume without duplicate startup input. Delivering a packet is tested separately from a real provider answering a bounded recall question.

**Done:** G5 proves each run's source/restoration lineage and no duplicate automatic startup input. The UI/API accurately distinguishes archive preserved, native resumed, packet delivered and model behavior observed.
