# Correct adapter behavior exposed by the native rehearsal

Manager decision D37, 2026-10-08. This independent lane owns two demonstrated
compatibility defects. Use the accepted report and original private synthetic
evidence; do not rerun native clients, access personal native stores, or turn
screen recognition into live input authorization.

## Preserve native Codex configuration on re-preparation

The pinned native Codex rewrites `.codex/config.toml`, adding its choices and
hook trust metadata. Repeating prepare for a new run currently rejects it as a
changed generated file. Re-preparation must preserve those native/user choices
byte-for-byte when Qorqut's owned configuration is still exactly the requested
configuration. Model/provider/effort choices remain the user's in Terminal.

Use stdlib tomllib, no new TOML writer/dependency. Keep fresh-file generation
unchanged. For this one destination only, an existing valid identity and
preparation manifest must already claim the generated file. When its byte hash
changed, parse it and the newly generated desired TOML. Compare a strictly typed
projection of `approval_policy`, `sandbox_mode`, and the complete named
`mcp_servers.qorqut-core` and `mcp_servers.qorqut-browser` tables. Missing,
changed or additional keys inside the two owned tables conflict. Other top-level
keys/tables and unrelated MCP registrations belong to native/user configuration
and are preserved. Compare canonical typed data, not Python equality that
equates booleans with integers. A malformed file, changed owned projection or
an authored/unclaimed destination remains an explicit conflict before writes.

If that projection equals the desired one, retain the existing file exactly;
record its actual preserved hash in the new manifest and update the remaining
per-run binding/hooks normally. A concurrent file change detected after
preflight must abort rather than record a hash it did not validate. Existing
root locks, immutable run bindings, atomic writes and all-destination preflight
remain. Do not broaden changed-file adoption for CLAUDE.md, AGENTS.md, hooks,
identity files, run files or other destinations. Do not modify credentials or
automatically rewrite changed owned configuration. A requested endpoint/header
change on a native-modified file can still report this explicit conflict; owned
configuration rotation is outside this narrow correction.

Extend the existing preparation tests with the observed native rewrite shape,
including hook approval tables and model/effort/user extras. Prove bytes survive
new-run prepare and ordinary retries; malformed/changed owned fields and mixed
destination conflicts cause no partial installation. Exercise the original
saved Codex rewrite in a private diagnostic, using only its synthetic credentials
and owned disposable roots. Do not commit private paths/tokens or native stores.

## Recognize the captured empty composer layouts honestly

The existing offline classifier fails the actual 120-column/40-row empty screens
recorded for Claude 2.1.287 and Codex 0.160.0. Correct classification for those
exact captured layouts while preserving fail-closed behavior elsewhere. The
result remains unsuitable for automated input: `native_verified` and
`permits_paste` stay false; add `native_snapshot` to its evidence enum for these
recognized observations. Do not change capability readiness, runner callbacks
or add a paste verifier based on a single canned prompt.

Use SGR-aware prompt matching: ANSI styling before/between the prompt glyph,
separator and content must not shift the content slice. Continue rejecting other
escape sequences. Require one unambiguous current composer at its expected lower
screen position; earlier prompts in conversation history are not current input.

- Claude capture: forty rows, lower box rules at rows 36 and 38, prompt at 37,
  glyph `❯` followed by NBSP and the observed reverse-video blank cursor cell;
  cursor x=2,y=37,hidden. Its only final row is the observed bypass-permissions
  footer (`⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents`).
  Both box rules span 120 columns. Accept the hidden cursor only with that
  blank-cell representation and exact cursor position. Nonblank box content,
  extra input lines or an unrecognized footer cannot be empty.
- Codex capture: forty rows, current prompt at 36 and cursor x=2,y=36,visible.
  The prompt glyph may be bold and reset before the separator. The observed
  placeholder `Ask Codex to do anything` is wholly dimmed after the separator
  and reset afterward; identical user-typed unstyled text is not a placeholder.
  Row 37 is blank; rows 38-39 are the observed model/path status row (nonempty
  model and path portions separated by ` · `) and `? for shortcuts`. Treat the
  status row as display metadata, not model configuration. Other footer/menu
  text, multiline content or a cursor mismatch remains unknown/blocked.

Keep existing synthetic layout handling and its evidence label. For these new
rules, require the known layout/geometry and exact provider profile; unknown
versions remain unknown. Busy/dialog indications in the composer region must
not pass as empty. No substring search across historic assistant text may be
used as proof of an empty composer.

Create a small sanitized fixture set derived from the four original fresh/resume
empty snapshots plus existing setup screens for negative checks. Record original
file hashes and the mechanical sanitization in the evidence, labeling derived
fixtures versus untouched private snapshots. Preserve prompt/cursor/SGR/box and
footer geometry; sanitize only irrelevant private header paths (the Codex status
path may become an equal-width synthetic path). Extend the existing grouped
composer tests, covering inserted text, typed placeholder, hidden/visible cursor
change, altered footer, setup/busy and unsupported version. Avoid another matrix.

## Ownership and acceptance

Own `adapters/preparation.py`, `composer.py`, the minimal evidence-enum change in
`types.py`, existing adapter tests/fixtures, and
`docs/evidence/adapter-rehearsal-fixes-r1.md`. No runtime, runner, recorder, host
schema/app/transport, UI, dependencies, launch argv, global capability or global
plan changes. Ask Manager about missing decisions rather than redesigning.

Run the owning adapter suite and installed focused preparation/composer checks
from a newly built wheel. Record source/installed provenance. Independently
review the completed revision. This fixes repeat preparation and observed
classification, not actual safe Chat paste, startup/inbox connection, complete
native capture, or arbitrary CLI-version support.
