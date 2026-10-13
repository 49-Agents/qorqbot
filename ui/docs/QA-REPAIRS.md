# QA repairs (2026-10-13)

Source: ACLA tester reports under `qorqbot-launch/qa/2026-10-13/acla-sonnet-222355f7`. Their copied UI was
`ui-fd96ee4`; each item below was checked against current source. Terminal internals are out of scope here.

| Area | Behaviour now |
| --- | --- |
| Scrollbars | History, agent list, inspector, composer and expanded native prompt share a 1 CSS px `#414141` thumb, transparent track, no buttons, no horizontal rail. Chromium previously ignored the webkit rules because the standard `scrollbar-color` stayed set; the `@supports selector(::-webkit-scrollbar)` block now resets both standard properties. Other engines keep `scrollbar-width:thin`. |
| Blank messages | `meaningful()` (`trim().length > 0`) gates Send, the form, Enter and `Store.send`. Valid bodies are sent byte-exact; the 64 KiB limit and retry semantics are unchanged. |
| History states | `Chat.loaded` is set only by a successful initial read. "No messages saved yet" needs it; a failed first read shows only the error and retry. `Chat.earlierError` is separate from live `Chat.error`, so polling cannot clear it; retry reuses the same `before_seq` cursor. |
| Copy feedback | A visible `role=status` line under the message shows `Copied` (cleared after 2.5 s) or `Could not copy. Select the text to copy it.` |
| Names | `stripName()` removes Python `str.strip()` whitespace (U+001C–001F and U+0085 included, U+FEFF excluded) and is used for validation and submitted names; `nameProblem()` requires 1–120 code points, UTF-8 encodable. Message bodies are not normalised. Creation and inline rename show the message inline; invalid or empty rename stays editable. Enter commits once, Escape cancels, a valid blur saves. |
| Failures | `ApiError`/`Pending` carry the server's `retryable`. A definitive nonretryable failure (`failed`, no ambiguity, server `retryable: false`) hides "Retry original request" and allows correction with a fresh idempotency key; the attempted text is prefilled. Sending, uncertain and conflict requests stay immutable; exact-key retry and conflict reapply are unchanged. |
| Rename/focus | The sidebar double-click is replaced by a trailing pencil button (`Rename <name>`, visible on hover/focus) that does not select the row. Escape/Enter return focus to the initiating control; blur does not move focus. `Skip to message` (focus-only link) precedes history. |
| Contrast | Message meta/timestamps `#999` and the composer placeholder `#999` (≥ 4.5:1). |

Checks: `npx playwright test tests/qa-repairs.spec.ts` (synthetic API; Chrome with scrollbars enabled).
The creation/rename scenario in `tests/shell.spec.ts` now uses the sidebar Rename control and the inline validation behaviour.
