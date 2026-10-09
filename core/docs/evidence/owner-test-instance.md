# Owner test instance

Updated by [D55 acceptance](local-access-manager-acceptance.md): this same URL now
opens directly without console credentials. The record below describes the
initial test deployment before the owner removed authentication.

2026-10-09. At the owner's request to make the build available immediately, a
separate test installation is running on this machine. Core is `95c479b`; UI is
`b0401eccb2d2aa16e32af3f59c56c8299332d6fe`. The reviewed shutdown correction is
included. Source, installed package, UI assets and writable data are separate.

The private Tailscale URL is `https://qorqut.tail740c74.ts.net:8875/`. Installation
files and credentials remain outside Git in
`/home/alp/.local/share/qorqutbot-test`. The `qorqutbot-test` and
`qorqutbot-test-proxy` user units serve this preview; the dedicated TLS route
preserves existing Tailscale routes. Core still binds to loopback. The small
preview proxy checks the external Host/Origin before translating them to Core's
loopback origin. This is a test deployment, not the final installer.

Direct Chrome checks through the HTTPS URL passed: login, real recorded Bash
input over WebSocket, pinned Codex version output, saved Chat after reload, and
automatic publication into onbash's dated transcript file. Desktop/mobile
screenshots were inspected and no page exceptions occurred. Evidence is in the
installation's `checks/` folder. A pending greeting is saved for the first session.

No model session was automatically started, no existing native login was copied,
and no model response is claimed by this smoke check. Start Codex manually in
onbash's Terminal, complete native sign-in/trust as needed, then release terminal
control before using Chat. Ordinary-agent VM runtime, browser takeover,
complete child/media capture and operational restore remain unfinished.
