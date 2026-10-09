# Connected browser acceptance and owner test deployment

2026-10-09 UTC. The owner waived additional review and authorized merge/deploy.
The combined flow passed with core `8bdde3ed91618d0c11f2575a3c2d6057e788b7c4`,
browser `6ba2831aa52cf4927e5aeb9ff217c7dccb8c164c` and UI
`7ecabdf2f9c988d19434326a4a095fa7b251de22`. These revisions are merged and pushed.

The Manager used installed wheels, the compiled UI, a disposable recorded
onbash shell, actual Chrome/Openbox/TigerVNC, actual noVNC and a synthetic local
form. Core and browser used separate environments for their pinned dependencies.
The final run checked:

- Browser MCP request and exact-key retry produced one durable ready Chat card.
- Actual displayed pixels, pointer/keyboard input and form submission worked;
  expansion retained the same noVNC canvas.
- Core restart retained Chrome, the synthetic profile cookie and the human
  request. Explicit Reconnect restored the viewer; automation remained excluded
  while the human request was active.
- Done and a separate Computer/Open browser/Cancel flow produced exactly two
  durable notifications. Both requests reached their terminal states with no
  pending control intents; the final manual viewer required no extra reconnect.
- Resumed browser MCP saw the exact saved form result. Core published the two
  request entries and notification notes to the UTC-dated transcript file.
- The actual pinned Codex executable loaded the separate browser MCP entry via
  `mcp list --json` in the isolated generated home. This verifies configuration
  discovery, not hot reload of an already-running native agent.

Earlier failures are retained. They exposed an overly short viewer observation
deadline and a normal SQLite sidecar-disappearance race, both fixed in the named
browser revision. The test driver also needed its asynchronous-release wait and
obsolete Inspector selector corrected. The final run used logging-only exception
diagnostics; production behavior was unchanged. No broad suite or review cycle
was added. Cleanup recorded a stopped disposable desktop with no owned children.

Deployment installed those exact artifacts on the existing owner test instance.
The first startup refused the older schema, so the Manager applied the explicit
schema 9→10 migration under the host lock after saving a SQLite backup. Core and
browser MCP restarted; native Bash/Codex fingerprints, inbox owner, terminal ID,
Chrome/TigerVNC lifetime and persistent profile were preserved. Browser attachment
is ready and native browser configuration is installed. The HTTPS UI opens
without application authentication and offers Computer → Open browser. Live MCP
discovery advertises `browser_turn` and `browser_request_input`; this live check
sent no owner terminal input or browser action.

Private evidence is under the run state directory in
`manager-browser-integration/` (`combined-attempt9.log`, `result.json`,
`ui-result.json`, screenshots and cleanup) and
`browser-app-deployment-8bdde3e-6ba2831/` (artifact hashes, migration backup/record,
process fingerprints, attachment, native preparation and live UI/MCP checks).
No owner data, profile, transcript or operational backup is committed here.

This accepts the connected owned-browser increment only. The owner's separately
reported narrow Codex/warning-footer Chat delivery defect remains an active
correction. Existing native sessions may need their own MCP refresh or a manual
next launch to load the browser entry. VM-backed agents, the separate machine
desktop, complete child/media capture and full installation/restore gates remain
open; this is not full-product acceptance.
