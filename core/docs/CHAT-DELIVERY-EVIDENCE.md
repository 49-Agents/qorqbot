# Native input observations needed for Chat delivery

Manager assignment, 2026-10-09. Both fixed-version native history workflows now
have bounded evidence. Chat delivery still has no production caller. Before
connecting it, inspect the remaining actual input behavior rather than treating
an idle hook or saved empty screen as a complete input-readiness decision.

This is factual exploration only: no product implementation, new API, schema,
readiness policy or design recommendation. Manager owns the following delivery
design. Use exact core `a2a162ce67f0eccafee537bd75a97069c1280788`, Claude 2.1.287
and Codex 0.160.0 from the accepted native workflow artifacts. Keep code clean.
Only `docs/evidence/native-input-observations-r1.md` is a tracked output.

For each provider, sequentially in its own disposable loopback-only fixture:

1. Reuse the completed workflow's installed core/canned provider/configuration
   and cleanup helpers, copied privately. Use normal core setup, recorded shell,
   production hooks, ownership and collector; manually start the pinned CLI.
   Claude retains the explicitly labeled fixture-only onboarding qualification;
   Codex retains the accepted manual --no-daemon/fake-provider arguments. Do not
   seed native trust or use another actor's running fixture.
2. Save actual tmux screen bytes, dimensions, cursor/flags, native process/owner
   and current hook activity at the initial native consent dialog, at idle, and
   with a short human draft. Run the existing composer classifier read-only on
   those observations and report its literal results/limits. Never promote its
   `permits_paste=false` into a production permission. Clear only the fixture's
   deliberately entered draft with explicit native keyboard input, not a new
   automated clear operation.
3. Observe three exact submitted payloads: a short single line; about 2 KiB with
   Unicode, internal LF and TAB; and exactly 65,536 UTF-8 bytes ending in ordinary
   text. Prefix each with a fresh visible fixture UUID for identification. Use
   the recorded shell's explicitly acquired human fixture control and normal
   write receipts to send bracketed paste, then a separate Enter. If framing
   exceeds the 64-KiB human frame limit, split that raw framed stream into legal
   contiguous frames under the same viewer/control; do not raise the limit.
   This test driver is not production automated delivery or a readiness gate.
4. Save before-paste, after-paste/before-Enter and post-submission observations.
   Record whether long content is collapsed, what exact content is observable
   before Enter, and the actual native user record's complete text/ID/offset/hash
   after Enter. Compare exact UTF-8 bytes; report transformations or absent proof
   instead of accepting substring equality. Record native prompt/turn IDs present
   in the emitted records. Do not invent a platform-message association.
5. During the short turn, capture a genuinely pending native response. A bounded
   five-second delay in that copied fixture's canned response is allowed solely
   to observe busy state; no native executable/product change or model emulator.
   Save one idle observation at a second pane geometry (80x24 versus 100x30).
   Do not paste into the deliberately busy/modal/draft states to test a guess.
6. Exit the native CLI normally, retain raw evidence and perform owned cleanup.

These are three short controlled submissions per provider (one has a large
payload), not a compatibility matrix or real inference. No UI build or browser
QA is required. Do not rerun accepted workflow suites. No generic harness or
usage example belongs in the product repository.

Return exact observed facts for the current empty/busy/modal/draft layouts,
bracketed-paste framing, pre-Enter visibility, native byte preservation and
post-submit identities. Explicitly distinguish original native data from any
derived fixture and state which facts remain unavailable. An actual mismatch or
missing decision goes to Manager; continue only independent authorized cases.
Do not implement a classifier or choose how to correlate/deduplicate messages.
