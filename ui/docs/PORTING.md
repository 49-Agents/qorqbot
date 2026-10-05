# Qorqut code extraction and creation plan

This document identifies source references and decisions, not copied implementation. The initial repository contains no Qorqut runtime code or operational data.

## 1. Exact baselines

| Source | Inspected revision | Location |
| --- | --- | --- |
| Qorqut core | `e71fd1e527368bfd41b9815126f4bf33452aeebb` | [biz-core](https://github.com/Qorqut/biz-core/tree/e71fd1e527368bfd41b9815126f4bf33452aeebb) |
| Qorqut dashboard | `fd9c1a924db70373eb8d06f3970a87c26bd84452` | [biz-dashboard](https://github.com/Qorqut/biz-dashboard/tree/fd9c1a924db70373eb8d06f3970a87c26bd84452) |
| Standalone agent-dms | `22a9aaa484d2d1f20fc0ac2ea30ead706c2784e8` | [agent-dms](https://github.com/49-Agents/agent-dms/tree/22a9aaa484d2d1f20fc0ac2ea30ead706c2784e8) |

The core/dashboard revisions came from Qorqut's current release manifest, `20261005T164238Z-a49a9352`, schema 113. Shared primary checkouts were on other branches/revisions and are not this source baseline. Some source documents still describe retired paths; current implementation takes precedence for what exists.

## 2. Per-module matrix

Core package paths below are relative to `bazarlyk/` unless already prefixed; a bare filename continues the immediately preceding directory. Dashboard `pty_broker.py` and core `tools/browser/desktop_session.py` are repository-level paths. Standalone agent-dms package paths are relative to `src/agent_dms/`. All references use the pinned revisions. “Port tests” means retain relevant behavioral assertions with new fixtures and boundaries, not claim the old full test suite validates the new product.

| ID / destination | Source mechanisms and files | Decision | Required change / test |
| --- | --- | --- | --- |
| P00 / M00 storage | `bazarlyk/storage/database.py`, `receipts.py`; `application/engine.py`; agent-dms `storage.py`, `service.py` | Rewrite small core using the transaction/idempotency patterns | No 113-migration/business schema import. Prove operation/event/job atomicity and crash recovery. |
| P01 / M01 agents | `agents/models.py`, `agents/operations.py`, `bootstrap/preparation.py` | Rewrite registry; adapt stable-ID and preparation separation | UUID folder paths; arbitrary agents; no business-agent/Dev quota or role catalog. Creation is followed by explicit provisioning jobs. |
| P02 / M01 activity | `agents/activity.py`, `bootstrap/activity_hook.py`; agent-dms `status.py`, `models.py` | Adapt hook/process evidence; rewrite persistent status state | 100 words, complete old/new history, all-agent directory, no seven-day state pruning or status freshness gate before messaging. |
| P03 / M02 environments | `sandbox/lifecycle.py`, `state.py`, `runtime.py`, `terminal.py` | Reuse lifecycle/fencing ideas; new libvirt implementation | Replace Docker/business ownership, retain desired/observed distinction and actual-writer fencing. Real two-VM tests. |
| P04 / M03 terminals | dashboard `pty_broker.py`; core `sandbox/tmux_session.py`, `sandbox/tmux.py` | Narrow extraction | Remove business naming and dashboard globals; exact run bindings, persistent recorder, runner transport. Port restart/resize/paste fixtures. |
| P05 / M03 delivery | `transport/agent_terminal_control.py`, `transport/terminal_composer.py` | Extract guarded composer/paste behavior | Remove role/owner-command gates. Keep deferred/submitted/uncertain outcomes and no blind retry. Test every pre/post-paste boundary. |
| P06 / M04 adapters | `bootstrap/preparation.py`, `claude_session.py`, `codex_session.py`, `terminal_chat.py` | Adapt individual configuration/hooks; rewrite assembly | Generated agent-local inputs; no mandatory business/role briefs. Every source/run registered, no single current marker. |
| P07 / M05 messages | `inbox/messages.py`, `inbox/service.py`, `agents/terminal_control.py`; agent-dms messaging/inbox/notification receipts | Reimplement small message/outbox domain; reuse proven receipt rules | Full immutable messages; unrestricted actors; stable attempts and explicit handling. No management/workstream/role system. |
| P08 / M06 automation | `bootstrap/agent_browser.py`, selected command handling in `bootstrap/browser.py`, `browser/operations.py` | Narrow extraction/adaptation | Run in each agent guest against its fixed local CDP. Remove business/proxy leases; retain exact browser instance and artifacts. |
| P09 / M06 desktop | `tools/browser/desktop_session.py`, `browser/isolated/session.py` | Extract lifecycle/flush/process-ownership mechanics | VM-local Xvnc/Openbox/Chrome, no business network namespaces or Guacamole registry. Direct noVNC relay. |
| P10 / M06 handoff | `inbox/focus.py` legacy Done and pause guard | Reuse transaction/idempotency pattern; new domain records | Existing request creation is retired. Build generic per-agent request/control epoch, exact Done and completion message. |
| P11 / M07 native formats | `global_chat/terminal.py`, `global_chat/store.py`, `bootstrap/terminal_chat.py`, `bootstrap/transcript_snapshot.py` | Adapt format knowledge/fixtures; rewrite capture/storage | No 8 MiB file limit, cwd ownership rejection, text-only archive or current-marker-only reader. Preserve unknown/child/tool records. |
| P12 / M08 continuity | `domains/working_memory.py`; dual-CLI documentation | Reuse identity/checkpoint concepts; new switch workflow | Plain memory plus exact context packet; cross-provider restore and capture drain are new work. |
| P13 / M09 files | `domains/documents/operations.py`, `storage/safe_io.py`, `storage/recovery.py` | Extract useful atomic/safe I/O; simpler file API | No document role registry or automatic note versioning. Include DB slices and pinned disks in portable export. |
| P14 / M10 transports | `transport/mcp.py`, `service/web.py`; agent-dms `mcp_server.py`, `stdio_bridge.py` | Small fresh SDK integration | Shared domain contracts, all tools allowed, compact paging; no copied policy preparation stack. Skip stdio bridge unless a client actually requires it. |
| P15 / M11 operations | `storage/recovery.py`, release continuity tests and receipts | Reuse recovery principles; new minimal installer/release tooling | No Qorqut release-worker configuration coupling. Pin images/dependencies, preserve running agents across host app update, prove restore. |
| P16 / M06/M09/M10 chat presentation | dashboard `js/agent-browser.js`, `js/agent-focus.js`, `js/global-chat.js`, `tests/test_global_chat_browser.js`, `tests/test_agent_pane_browser_integration.js`; core `file_viewer/paths.py`, `service.py` | Adapt exact surface binding, frame/focus cleanup and compact reference-chip behavior | New intent-only browser/file operations and durable cards. Separate browser/machine, inline expansion, monochrome shell/name/icon controls. File opening/preview deferred; do not import business/role routing or existing renderer stack now. |

## 3. Important source differences to preserve consciously

- Qorqut global folders use names; business folders use fixed role names. Its stable database UUID is the part to retain.
- `agents_create` registers files/records; it does not launch a VM/browser/CLI. The new create operation's asynchronous progress must cover the full requested lifecycle.
- Qorqut supports Claude/Codex and has specialized Codex/Dev launch branches. A generic tmux shell is not complete arbitrary-CLI integration.
- Qorqut current work is a per-native-session list of up to ten 300-character items. The requested agent status is a separate persistent 100-word field.
- Qorqut activity rows are overwritten and older offline observations pruned. This product must append changes permanently.
- Direct terminal messages in Qorqut target a limited coordinator-to-Dev route. The new message domain must not inherit that policy.
- Qorqut's Docker sandbox and isolated desktop are business-owned. The two business agents can share them. Change the ownership key throughout provisioning and profile paths.
- Qorqut browser automation currently uses Vercel `agent-browser` through external CDP, despite older Playwright-oriented documents.
- The old `focus_browser_input` write path returns `upgrade_required`; only its existing-request completion/pause behavior is relevant to extraction.
- Native Chat is a derived visible-text projection. Native raw capture is best-effort; neither is an adequate full-archive subsystem unchanged.
- Standalone agent-dms requires 1–30-word current status, updates it in place, and has no process launcher/UI. Its messaging semantics are useful; adopting it wholesale would still require major integration and status changes.

## 4. Extraction procedure for every source item

1. Identify exact functions/behavior and the smallest necessary fixture. Read surrounding failure/recovery code before extracting a helper.
2. Write the destination contract test using qorqutbot IDs, fake external boundaries and synthetic data.
3. Decide whether a clean implementation is shorter/clearer than adapting the source. Record substantial adaptation in a provenance note with source revision and applicable notice.
4. Copy or implement only that behavior. Remove Qorqut imports by replacing the dependency boundary, not by importing a huge transitive package.
5. Run focused contract tests and the relevant real tmux/browser/VM fixture. Make failure injection part of the port, not a later integration chore.
6. Confirm installation succeeds in an environment without `/home/alp/qorqut`, its package maps, business folders or services.
7. Update the module plan with actual evidence and deviations. Do not call a test “ported” if its assertion was weakened to fit an implementation bug.

A source test that depends on retired fields or missing native bindings is evidence to investigate, not a reason to copy the old assumption. Preserve the intended new contract and make its fixture explicit.

## 5. Source fingerprints from local inspection

These hashes identify selected immutable-release files read during planning. They are not a lockfile for a future fork.

| File | SHA-256 |
| --- | --- |
| core `bazarlyk/inbox/messages.py` | `041cba06ba21328e1c75cb80f21267096a4ccede1f989fcd19bb13888bcec65f` |
| core `bazarlyk/agents/activity.py` | `7547e328e01364641a7f8231cbc7ba9098f554b650883f1e5ccadcbea4f44c16` |
| core `bazarlyk/transport/agent_terminal_control.py` | `b612645137adeeeb7b50314c4899b3d1abde00b8bf0cfe635c8577893e5be5c4` |
| core `bazarlyk/global_chat/terminal.py` | `2ef2d6f1a9752b1756fa4cf08a3817c15dc42fd158c02841e1aea77388871237` |
| core `bazarlyk/bootstrap/transcript_snapshot.py` | `42fafd56d00d773fd11ba3de6793ae35e2c31b61c3e4ff1898a8810351cdf562` |
| core `bazarlyk/bootstrap/agent_browser.py` | `3ca727cc4a19a4d49a08f257f1a75e6fe947484decc6471bbaee9f7a924361e4` |
| core `bazarlyk/sandbox/tmux_session.py` | `c1376b11ea1553ac1329033f388472106a08b3dda32137540de2f8b0e1956dd7` |
| dashboard `pty_broker.py` | `f7bc8b40524c4a76040735869dbcf4a1ca4f20e5c945fdc10e41427313d1b054` |
