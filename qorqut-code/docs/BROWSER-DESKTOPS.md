# Global Browser Desktops

Status: **source and example assets only.** Nothing here installs, migrates or
verifies a host, a Guacamole server or any login.

## What this adds

- `tools/browser/desktop_session.py`: one supervised TigerVNC display, minimal
  window manager and assigned maximized Chrome per validated JSON config
  (`check`, `argv`, `run`). Session lifecycle only; not an installer. Profile
  separation is not an operating-system security boundary.
- `tools/browser/desktops/*.example`: session configs and systemd unit templates
  for two example sessions: `ordubashy` (:20, reservation only, `managed:false`)
  and `outlook` (:22, VNC 5922, CDP 9226).
  The names are examples; each session is one validated config. Profiles, XDG paths,
  keyring/password-store behavior, CDP ports and direct networking are kept.
  No proxy flags or variables are accepted.
- `GET /api/root/agents/<uuid>/browser-surfaces`: installation-owner-only read of
  one active Global agent's own surfaces (a retired agent's registration is not found) from
  `operations/.runtime/browser-surfaces.json` (0600, schema version 2, see
  `browser-surfaces.json.example`). It selects viewer URLs for the owner UI
  only and grants no automation or send rights. Desktop is returned only for the
  configured Ordubashy (explicit binding, else validated `DESKTOP_CONTROL_URL`).
  A missing file is an honest unconfigured state; an invalid file is rejected
  whole, with no fallback to another browser or host. Schema 2 requires
  `schema_version`, `surfaces`, `agents` (Global agents' own bindings) and
  `businesses` (each business UUID to exactly one browser surface); an older
  schema is rejected.
- The named `desktop-servers` endpoint and its environment variable are removed; a browser
  is never inferred from a former desktop-server default.

A business's own isolated desktop is a separate design: it adds a proxy-only network
namespace that this global helper deliberately does not support (`desktop_session.py`
still rejects proxies and is unchanged). A business's one browser is recorded
under `businesses` in the same `browser-surfaces.json` and read through
`GET /api/biz/{slug}/browser-surfaces`, never through an agent binding. See
[isolated business browser](BROWSER-ISOLATED.md#owner-viewer).

## Checks

- Core: `tests/test_browser_surfaces.py`, `tests/test_browser_desktop_session.py`
  (fake executables; validation, collisions, ordered shutdown, restart isolation).
- An opt-in smoke with real Xtigervnc/Chrome on blank temporary profiles, unused displays and
  loopback only (`QORQUT_REAL_DESKTOP_SMOKE=1`): separate displays, one Chrome window per
  display, independent CDP, one restart leaves the other untouched, no orphaned Chrome or X
  server.
- Dashboard: `tests/test_agent_browser_browser.js` uses a stub iframe target. It proves pane
  routing only, not VNC connectivity or Guacamole login.

## Operator setup

No core migration is required. The host changes below are an operator's decision and are not
performed by source changes or by any release step:

1. Install `desktop_session.py` and the session configs. Units run as the service user
   (`<service-user>` below) and every unit loads all configs (its own plus `--sibling`
   reservations, including a `managed:false` one), so each config must be readable by
   `<service-user>`. `load()` accepts an ordinary, non-link file owned by root or the running
   user that is not group/world writable. The configs hold no secrets.
   | Path | Owner:group | Mode |
   |---|---|---|
   | `/opt/qorqut-browser-desktop/` | root:root | 0755 |
   | `/opt/qorqut-browser-desktop/desktop_session.py` | root:root | 0755 |
   | `/etc/qorqut-browser-desktops/` | root:root | 0755 |
   | `.../<session>.json` | root:root | 0644 |
   | each VNC `passwd` file (deployment input, never committed) | <service-user>:<service-user> | 0600 |
   | a keyring credential delivered by systemd `LoadCredential` | root:root | 0600 (not read by `<service-user>`) |
   A root-owned 0600 config would fail with Permission denied before startup.
2. Install the unit templates from `tools/browser/desktops/*.service.example` as needed.
3. Add Guacamole VNC connections for the Chrome desktops with owner access and keep the
   authentication boundary of the network that serves them.
4. Write `browser-surfaces.json` with the exact connection URLs and agent UUIDs.

Run `desktop_session.py check` on all configs and confirm there are no display, VNC or CDP
collisions before starting a unit. Stop a Chrome cleanly before moving its profile to a new unit;
if the same profile is in use by another process or keyring access is missing, stop and report.
Never delete profile locks, copy cookies between profiles, reset logins or change password
stores. To return the UI to the honest unconfigured state, remove or empty
`browser-surfaces.json`.
