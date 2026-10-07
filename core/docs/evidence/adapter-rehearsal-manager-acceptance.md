# Adapter rehearsal corrections: Manager acceptance

Accepted on 2026-10-08: `9737e2a7096f4e471e224dcaa7cd90c4d4bc9f3c`, based on
`f4506c705f77f9c1b6b1575f1905b95c77b7c27f`. This accepts D37's offline config
preservation and observed composer compatibility, including amendments 162/175.
The six reviewed source, test/fixture and evidence files are integrated unchanged.

Codex re-preparation preserves claimed native/user config bytes when Qorqut's
strictly typed owned fields and complete MCP tables match. The preserved marker
stays true across retries and generated-looking rewrites; genuine absence alone
resets it. Unmodified generated files retain their earlier rotation behavior.
Unclaimed/malformed/changed-owned configuration and conflicts at other managed
destinations still refuse during preflight.

The final correction checks config bytes immediately after writing the manifest,
before returning success. A concurrent edit raises and survives intact. In that
last-write window the manifest may already record the previously validated hash
and marker; it is not a success/readiness receipt. Earlier race windows retain
the previous manifest. Retry validates again, and no rollback or exclusion of
edits after the last observation is promised.

The captured Claude 2.1.287 and Codex 0.160.0 composer layouts now account for
SGR styling, exact observed lower-screen geometry, placeholder/cursor and footer.
Unknown layouts remain conservative. Recognized captures use `native_snapshot`;
`native_verified` and `permits_paste` remain false.

Fresh independent Astra MAX review 184 found no in-scope defects against the
original plan and both amendments. It verified all 168 tracked candidate files,
the three-path final revision and all 56 package files. The owning source suite
passed 32 checks; five focused installed checks passed. Its additional installed
diagnostic covered a final-write edit during original-generated rotation with
false preservation mode, subsequent refusal and compatible recovery. Offline
comparison reproduced the documented fixture sanitization and original hashes;
no native clients or provider calls were rerun.

Manager independently inspected preparation, composer, the result type and the
owning preservation/race/composer checks. Clean worktrees and the exact pushed
candidate were verified. All 56 package files matched candidate Git, the fresh
reviewer wheel and installed bytes; all 31 dependency pins matched. Manager
passed two focused installed checks for native choices/sticky retries/rotation
and captured composers with negative mutations. The original independent
final-manifest race diagnostic was rerun against this revision: it now reports
conflict, preserves the concurrent native edit, and leaves the manifest recording
only the prior validated hash/mode. That same diagnostic previously demonstrated
the old candidate returning success incorrectly.

Manager commands, scripts, byte/import provenance, test results and cleanup are
retained in `manager-adapter184/` in the recorded build run. The independent
review bundle is `review-adapter-rehearsal-fixes-r2-validation/`; its wheel SHA-256
is `e00abcd669b3302825755090a349eb7e671c2d77197da8c698e39441365a7a8e`.
Reviewer and builder approvals are separate ACLA messages 185 and 186.

This is offline compatibility evidence. It does not establish live automated
paste, startup/inbox/Chat connection, native ingestion, arbitrary CLI-version
support or VM/browser readiness. Manager's replay used only disposable synthetic
configuration; temporary fixtures were removed. No owner stores, live Qorqut
sessions, provider calls or global service changes were used. Nothing was
deployed and no full G0–G6 gate advanced.
