# Research and verification baseline

Date: 2026-10-07. This is evidence from planning, not a qorqutbot implementation result.

## Inspected source

The primary agent and three Astra Max research agents explored identity/runtime, browser/isolation, and transcript/continuity behavior. Follow-up architecture reviews examined the proposed port, true-VM storage/transport and durable archival contracts.

Current Qorqut release: `20261005T164238Z-a49a9352`.

- Core: `e71fd1e527368bfd41b9815126f4bf33452aeebb`.
- Dashboard: `fd9c1a924db70373eb8d06f3970a87c26bd84452`.
- Recorded database schema: 113.
- Standalone agent-dms source: `22a9aaa484d2d1f20fc0ac2ea30ead706c2784e8`.

Local release source was under `/home/alp/qorqut/components/`, selected by `/home/alp/qorqut/current/manifest.json`. Primary checkouts were on older/unrelated branches; they were not edited. Relevant core docs, migrations, implementation and synthetic tests were read. Personal/global transcripts and browser profile contents were not used as research material.

## Findings that shape the plan

1. One shared Python application already serves HTTP/MCP and registers common operations.
2. UUID identity survives native runtime changes, but current folder/role/environment assumptions are business-oriented.
3. Native tmux sessions survive dashboard mirror/service restarts. Guarded terminal input and uncertain-effect receipts are useful existing mechanisms.
4. Complete messages and exact handling receipts exist. Broad unrestricted terminal-to-terminal coordination needs adaptation.
5. Current activity/status is not permanent status history.
6. Browser profiles, Chrome supervision and human completion machinery are reusable; per-agent true VMs are new work.
7. Current Chat preserves imported visible messages across sessions. Raw snapshots and current-marker collection do not meet the complete-history requirement.
8. Plain notes/workspaces and provider-independent saved working memory already demonstrate the useful ownership pattern.

## Focused checks actually rerun

Six test cases were executed against the selected core code with temporary test data and installed immutable package sources. Four passed and two failed. No live agent messages, production CLI turns, browser logins or VM changes were performed.

| Test | Result |
| --- | --- |
| `tests/test_dev_direct_messages.py::test_direct_message_is_durable_attributed_and_idempotent` — Ordubashy parameter | Passed |
| Same test — Global Marketing parameter | Passed |
| `tests/test_terminal_owned_chat.py::test_new_terminal_session_preserves_history_and_gets_distinct_native_keys` | Passed |
| `tests/test_terminal_owned_chat.py::test_native_history_replay_partial_lines_tools_and_read_position` | Passed |
| `tests/test_agent_messages.py::test_direct_delivery_reply_history_and_exact_ack` | Failed: manager watcher polling lacked a verified native binding (`inbox_binding_required`) |
| `tests/test_agent_activity.py::test_scoped_concurrent_work_presence_and_progress` | Failed: test expected bootstrap result field `activity_available`, which was absent |

The test run used the manifest's Python dependencies plus the already installed development pytest package, with bytecode/cache writing disabled. Initial collection attempts exposed missing pytest in the runtime and missing document-versioning dependency in the older development environment; no packages were installed. The final six-case run completed in approximately 24 seconds.

These failures were not diagnosed or repaired as part of reconnaissance. They do not establish a failure of all messaging/activity behavior, and they also prevent claiming an all-green inherited baseline. New module tests must stand on their own contracts.

Other tests were inspected, not rerun: dual-CLI identity/config, raw snapshot atomicity, browser cookie persistence, independent desktops, sandbox lifecycle, owner-input completion, all-agent Chat isolation and working-memory recovery.

## Read-only installation observations

The existing installation had sandbox provisioning disabled and zero registered business sandboxes. Browser configuration included nine isolated-desktop business entries and a browser-surface map with 13 surfaces/20 agent bindings.

System service metadata showed nine business gateway units failed, eight isolated browser units failed and one inactive; separate remote/Outlook system browsers and a Marketing user browser were active. Viewer connectivity/login was not tested. These are observations at research time, not root-cause findings or future service guarantees. Nothing was restarted or repaired.

For the new VM baseline, this machine is x86_64 and exposes `/dev/kvm`, but the current account did not have direct read/write access; QEMU and virsh were not on PATH. Phase 0 must verify/install the required virtualization stack under the later implementation task. Planning does not install it.

## Distinction between evidence and plan

The Qorqut source supports the extraction decisions in [PORTING.md](PORTING.md). The VM driver, generic input flow, durable all-source archive and cross-provider restoration described here are proposed qorqutbot work. No page in this repository should present those as already implemented or tested.

## Planning consistency review

The same three research agents reviewed the written module/shared contracts, then narrowly rechecked the resulting corrections. The baseline now specifies terminal control epochs, archive/launch precedence, independent systemd runtime cgroups, browser cancellation, onbash spool backup, relocated disk restore, raw/parsing context barriers, retained file snapshots, native resume without duplicate prompts, and recovery holds for restored external effects. Capture transfer consistently uses host pull plus durable ACK.

These were document reviews, with no new runtime tests, service changes or implementation. The acceptance matrix records how the proposed contracts must later be demonstrated.

Initial-baseline documentation checks verified 22 Markdown files, 43 local links, twelve module sequences/done gates, fourteen requirement mappings, seven delivery gates and 71 distinct acceptance cases. Named source-file checks verified 36 core and six standalone agent-dms references against the inspected local baseline. These checks validate planning structure/references, not runtime correctness.

## Browser/chat UI follow-up inspection

The current manifest still selected release `20261005T164238Z-a49a9352` during this read-only follow-up. Inspected the following exact mechanisms at the already pinned core/dashboard revisions:

- Core `tools/browser/desktop_session.py`: header defines one dedicated TigerVNC display with one maximized Chrome; `plan` builds Xtigervnc/Openbox/profile-bound Chrome argv; `start` owns their lifecycle; `stop` closes Chrome before the display for profile flush.
- Dashboard `js/agent-browser.js`: explicit browser versus full-desktop mappings, selected-agent verification, iframe sizing and stale-frame disposal. Existing global/business role conditions do not imply one healthy browser for every agent; qorqutbot replaces those conditions with per-agent surface identity.
- Dashboard `js/global-chat.js` and inspected `tests/test_global_chat_browser.js`: compact attachment chips, no content load before opening, one on-demand in-chat preview and focus/iframe cleanup. These were read, not rerun. The new task defers file opening, so only reference/presentation patterns are relevant now.
- Core `inbox/focus.py`: legacy request creation returns `upgrade_required`; owner Done keeps an exact request check and atomic completion/message receipt. Core `file_viewer/paths.py` and `service.py` supply path-resolution reference points, not a reason to copy the full existing viewer.

A process-name-only inspection observed three Xtigervnc, three Openbox and one guacd processes. This does not verify their agent mappings, connectivity or login state. No profile contents, private chat data or existing sessions were used or changed; no new VNC/browser runtime tests were performed. [UI-PLAN.md](UI-PLAN.md) records the new required behavior rather than claiming it already exists.

The amendment's document checks passed for 23 Markdown files, 59 local links, twelve module plans, sixteen mapped requirements, seven gates, 81 unique acceptance cases and twenty unique icon keys. A focused read-only review checked browser/machine binding and handoff; the resulting contracts include machine control during setup/stopped CLI and a hold covering future managed launches. Runtime/UI acceptance remains not run because these changes are plans only.
