# Codex empty-placeholder reset placement correction

Manager decision after connected-flow blocker 309, 2026-10-09. The pinned actual
Codex 0.160.0 screen at 104x50 has a visually empty composer with the exact dim
placeholder, but tmux emits its SGR reset at the start of the model/footer row,
not the end of the prompt row. The existing exact-tail check refuses it before
any attempt. Manager independently replayed the unchanged snapshot and obtained
blocked/composer_contains_text_or_unknown_placeholder; adding only a trailing
reset to a derived prompt row makes the same classifier return empty. Original
snapshot SHA-256: c08212819b46cdab5e543f6005abf8678925ea3257c4888018fc2e5f5d576332.
This is a formatting variation, not evidence authorizing broader input.

Change only the pinned Codex empty-placeholder check in adapters/input_layout.py.
Preserve the original accepted exact dim/text/reset tail. Also accept the exact
raw tail `ESC[2mAsk Codex to do anything` with no trailing reset if and only if
its next raw row is exactly empty and the raw model/footer row starts exactly
`ESC[0m  `. Existing row count, bottom-relative prompt, cursor=(2,rows-4,true),
version, geometry, flags, prompt-decoration, footer/path/shortcut, busy/dialog and
control checks all still apply. Do not strip arbitrary styling, infer plaintext
emptiness, implement a generic style engine or weaken expected ASCII matching.
The new form is only for expected=None/classify_input. matches_input remains
unchanged, and native_verified/permits_paste remain false. No runtime readiness,
recorder, dispatcher, owner, UI, schema or native-configuration change.

Own input_layout.py, only the existing InputLayoutTests in
tests/adapters/test_adapters.py, and docs/evidence/codex-placeholder-reset-r1.md.
Use a compact sanitized lower-row fixture matching the observed 104x50 layout.
Cover the new positive form and unchanged existing form; reject the incomplete
form without the later full reset, a nonblank intermediate row, plain/styled
human text or altered placeholder, and a mismatched cursor. Existing shared
refusal tests cover other mode/geometry/dialog cases; do not duplicate a matrix.
Run the owning InputLayoutTests group. Privately replay the exact original
snapshot unchanged and confirm its new empty observation; separately retain
that no permission is granted and expected notification matching remains false
on that empty snapshot. No actual CLI, live input, full suite or UI build is
needed in this correction assignment.

Original private evidence is under
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/connected-chat-codex-r1/evidence/
(blocked-classifier.json, blocked-screen.sgr, blocked-flags.raw). These named
blocker artifacts are completed evidence; do not inspect the verifier's evolving
harness/worktree. Manager replay is under manager-review309/replay.json in the
same run-state directory. Never edit original evidence or commit private native
history. Report precise scope, commands/results, preserved original/derived
provenance and the clean immutable pushed commit. No merge, deployment, plan or
design changes, delegation or internal reviewers. Manager reviews the completed
correction before repinning/repeating the connected Codex flow. This correction
alone is not actual Chat-delivery acceptance.
