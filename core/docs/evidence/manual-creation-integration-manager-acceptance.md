# Manual creation and paired core/UI acceptance

Manager acceptance, 2026-10-08, under D36 and
[MANUAL-CREATION-INTEGRATION.md](../MANUAL-CREATION-INTEGRATION.md).

Accepted core: `5a50666c981c727c32dad239886ba5fcd7c9f1ac`.
Accepted UI: `6a4841d5830ae3970b492ebf87528de4f94f787c`.
Core's final integration revision changes the historical migration test and its
evidence only; production bytes match the prepared combination at
`12d1139d9319da2cf347d310b4f82409ab65783c`. That combination includes the reviewed
manual-creation correction and schema-6 inbox ownership. Landing preserves the
separately accepted recorded-shell and adapter changes from core `010b551`.

## Accepted behavior

Creation accepts identity and appearance without selecting or starting a CLI.
The actual UI offers a name and twenty icons; its request omits CLI/start fields.
The shared HTTP/MCP operation retains historical committed receipt replay across
the explicit schema-5-to-6 migration without starting a process. Folder
provisioning still reports its real runtime dependency hold.

The installed console server and compiled UI support creation, rename/icons,
durable messaging, cookie sessions and responsive inspection. Two equal-text
messages with different keys remain distinct. Core publishes their exact content
and provenance, including Unicode and newlines, to UTC-dated JSONL files while
the browser is closed. Normal server restart preserves all three tested entries
(including onbash) and both published file hashes. Runtime and Terminal remain
truthfully disconnected/unavailable.

## Independent review

Fresh ACLA reviewer `review-manual-creation-integration-r1` used
`codex / gpt-6-astra / max`, with self-review and delegation disabled. Review
187 found no in-scope defects. Reviewer approval 188 and builder approval 189
are separate acceptance records.

The reviewer exported both exact candidates, built and installed a fresh wheel,
verified all 31 pinned dependencies, and built UI assets from the lockfile.
Historical receipt and shared transport source checks passed; installed shared
transport and real-child ownership checks also passed. Actual Chrome 153 with
Playwright 1.64 passed initial and restarted application flows using the normal
installed console entrypoint, fresh private profiles and synthetic data. No API
response mocks or manual job pumping were used. The reviewer checked server
import provenance, no-CLI creation, automatic publication, exact SQL/API/file
agreement, cookie/logout behavior, desktop/mobile layout and browser errors.
All eleven retained screenshots were inspected by that reviewer.

Two initial harness failures concerned Chrome's Unix-socket path length and a
viewport measurement before rerender. Private harness conditions were corrected;
both final flows passed without product changes. A missing emoji glyph in the
host font environment remains visible; DOM/API/SQL/file checks preserved the
exact Unicode content. Servers, profiles and synthetic data were removed.

## Manager verification

Manager inspected the complete narrow integration diff, historical migration
test, shared HTTP/MCP flow and actual-browser/restart/publication harness.
Independent byte comparisons matched 61 core package files across Git, the
review wheel and installed package, 43 UI source files, all 67 review artifact
checksum entries and 54 staged test files. The reviewed wheel SHA-256 was
`8c20dc3cbfb2d1ab9387c6f4cb03c9aa5d48e491c73c26dba9ec79fec5f44c05`.

Manager separately passed the historical old-source receipt check with no skip:
schema-5 opening refuses, explicit locked migration succeeds, historical tables,
checksums and receipt bytes remain intact, and replay/reopen never launches a CLI.
Manager also passed the installed shared HTTP/MCP identity-only creation,
headless provisioning and reopen check. Both owned console child invocations
used the normal installed entrypoint and installed-only import path. All 31 pins
and import origins matched; owned processes and temporary data were cleaned up.

Manager visually inspected `creation-desktop.png`, `restart-desktop.png` and
`restart-mobile-details.png`, confirming the simplified dialog, truthful runtime
states, distinct equal messages and reachable mobile inspector controls. Manager
did not rerun the full Playwright workflow: the independent browser runs above
and these separate source, artifact, core and visual checks are distinct evidence.

Private evidence under the run directory
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/`:
`reviewer-manual-creation-integration-r1/`, `manager-integration187/`,
`integration187-reviewer-approval.json`, `integration187-builder-approval.json`
and the landing receipt. The [builder record](manual-creation-integration-r1.md)
retains its own checks and limitations.

## Limits

This accepts the paired creation/UI and existing console/message/publication
contract. It does not establish a user-started native CLI connection, assistant
ingestion, actual Chat delivery, host shell activation, Terminal transport/UI,
browser/VNC control, VM readiness or complete source coverage. Shell and adapter
components retain their own bounded acceptance. No owner data or live Qorqut
sessions were fixtures. No deployment or whole G0–G6 acceptance is claimed.
