# Local-agent presentation (D63)

Implements the small presentation correction in `core/docs/LOCAL-AGENT-CAPABILITIES.md`.

- Creation text: "Creates a local folder and terminal. Start and configure your CLI in Terminal."
- Created notice is the neutral `<name> created.`; it makes no lifecycle or Chat-connection claim and
  stays accurate after later status changes.
- Inspector Agent type: host → "Local agent", vm → "Legacy VM agent". Lifecycle value is kept.
- Computer keeps ComputerBrowser (all unavailable/error/retry/viewer states) and the real Terminal
  summary. The inert Machine/Desktop card is removed; this supersedes the D57 placeholder requirement.
- Synthetic `makeAgent` now matches core: kind `host`, `start_requested` false, icon `compass`; the
  seeded `onbash` fixture sets icon `terminal` explicitly.

Checks (synthetic API fixture only; no native CLI, real backend or provider): `npm run build`
(typecheck + vite) and the full Playwright suite, 28/28, in desktop Chrome. Screenshots (creation dialog,
created notice, More details, Computer) are outside Git under
`/home/alp/qorqbot-launch/qa/2026-10-13/acla-fixes-9552beea/local-ui-evidence/`.
