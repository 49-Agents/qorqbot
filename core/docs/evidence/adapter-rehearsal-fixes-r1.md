# Adapter rehearsal corrections r1 evidence

2026-10-08. Implementation on `codex/build-adapter-rehearsal-fixes-r1`, exact
base `f4506c705f77f9c1b6b1575f1905b95c77b7c27f`. Executes D37's
[adapter correction assignment](../ADAPTER-REHEARSAL-FIXES.md) and Manager
amendment 162 answering question 160, followed by Manager review 175's final
manifest-write correction. Astra MAX; no delegation or internal reviewers.
Independent review and integration belong to Manager.

## Changes and boundary

Codex re-preparation parses TOML with stdlib `tomllib` and compares a recursively
typed projection of the two owned MCP tables, approval policy and sandbox mode.
A valid prior identity/manifest claim is required to preserve a changed file.
Native/user extras survive byte-for-byte; the manifest records the actual hash.
The amended `codex_config_preserved` boolean distinguishes preserved configs from
original generated files: missing means false, a non-boolean is invalid, true
survives retries and new runs, and only an absent config resets it. Original
unchanged generated files retain endpoint/header rotation; preserved files
refuse changed owned fields before writes. No TOML writer or dependency added.

Preflight still checks every destination. Config bytes are rechecked before
installation, at that destination, and before and after writing the manifest last.
Earlier detected races leave the old manifest unchanged. A race during the final
manifest write raises after the new manifest has been installed: that manifest
records only the last validated hash and preservation mode, never adoption of the
raced bytes, current readiness or a successful operation receipt. Native edits
are not overwritten. There are no rollback writes or new locks; complete per-run
files may remain, and an ordinary retry must revalidate the live config. These
checks do not exclude edits after the final observation.

Composer classification recognizes only the observed lower 120-column/40-row
layouts for Claude 2.1.287 and Codex 0.160.0. SGR-aware prompt slicing preserves
Claude's NBSP/reverse-video blank and Codex's wholly dimmed placeholder. Claude's
rules and Codex's status-row width anchor the captured geometry; no screen-size
or live-process attestation is inferred. Historic prompts do not become current
input. Synthetic layouts retain their evidence label; recognized observations
use `native_snapshot`. Every result still has `native_verified=false` and
`permits_paste=false`; capabilities, callbacks and launch arguments are unchanged.

## Final manifest-write correction: review 175

Revision parent: `5799cf685566ce6c9b7546e25badc9da405727be`. Only
`preparation.py`, the existing adapter test file and this evidence changed.
Composer/types/fixtures and every other package file remain frozen. Manager's
full numbered correction arrived as message 175; its plan clarification
`b39bdda93ed70b475e5a0fb58732d4909b05f137` was not cherry-picked.

The existing grouped config-race test now also intercepts `os.replace` for the
exact owned manifest parent device/inode and destination name. It changes
`approval_policy` inside that final replacement after the preceding config check.
Against the prior production code the regression failed with `AdapterError not
raised`, reproducing the false success. With the added post-write check it proves
the exception, untouched incompatible edit, already-written manifest with only
the earlier validated hash and sticky true mode, and a refusing ordinary retry
with no additional file writes. Restoring the owned projection while retaining
a new harmless user edit permits retry and records its actual preserved bytes/hash.
The earlier two race cases retain their old-manifest and no-partial-write checks.
The final observation applies to all Codex preparation, not only preserved mode.

`$REVISION_CHECKS` denotes the private
`adapter-rehearsal-fixes-r1-validation/review-175` directory;
`$REVISION_PYTHON` is its new private `venv/bin/python`. Python remains 3.12.3;
all 31 exact locked dependencies were copied from the prior private environment,
excluding its installed core and any source path injection.

- Owning source suite, run once after the fix: `PYTHONPATH=src "$REVISION_PYTHON" -m unittest discover -s tests/adapters -p test_adapters.py -v`: **32 passed**.
- Fresh wheel from a new private source copy: `"$REVISION_PYTHON" -m pip wheel --no-deps --no-build-isolation --no-index --no-cache-dir --wheel-dir "$REVISION_CHECKS/wheels" "$REVISION_CHECKS/build-source"`; installed with `pip install --no-deps --no-index --no-cache-dir`.
- From outside the worktree: `"$REVISION_PYTHON" -I -m unittest discover -s installed-checks -p test_adapters.py -k PreparationTests -v`: **13 passed**, including the final-write regression and its recovery assertions.
- SHA-256 verification matched **all 56 package files** across worktree source, fresh wheel and installed files. Core/preparation imports resolve to the new private `site-packages`, and the copied test bytes match the source test file.

Revision wheel SHA-256:
`b9eb023f3e847867164f5c9b1538d35420460abe9ada7ab4480ec3135c86aac0`.
The revision bundle retains `regression-before-fix.log`, `source-adapters.log`,
`installed-preparation.log`, and dependency/source/installed provenance JSON.
No native rehearsal, provider call, original private snapshot/config diagnostic,
other backend suite or owner-store inspection was repeated for this correction.
`native_verified` and `permits_paste` remain false. Independent review is pending.

## Initial candidate checks (5799cf6)

Python 3.12.3, with all 31 distributions from the existing `requirements.lock`
copied at their exact versions into a new private venv. No source `.pth`, editable
install or old core package was copied. `$CHECKS` below denotes the private
`adapter-rehearsal-fixes-r1-validation` bundle beside the authorized build state;
`$VENV` is its `venv` directory.

- From this worktree: `PYTHONPATH=src "$VENV/bin/python" -m unittest discover -s tests/adapters -p test_adapters.py -v`: **32 passed**.
- Wheel: `"$VENV/bin/python" -m pip wheel --no-deps --no-build-isolation --no-index --no-cache-dir --wheel-dir "$CHECKS/wheels" "$CHECKS/build-source"`, then local `pip install --force-reinstall --no-deps --no-index --no-cache-dir` of that wheel. The build source was a private copy of this worktree's `src` and `pyproject.toml`.
- Outside the worktree, with unchanged copies of the existing adapter checks: `"$VENV/bin/python" -I -m unittest discover -s installed-checks -p test_adapters.py -k PreparationTests -v`: **13 passed**; the same command with `-k composer`: **2 grouped checks passed**.
- Installed `python -I -m pip check`: no broken requirements. Module paths resolve to the private venv's `site-packages`; SHA-256 comparison proves all three changed production modules match the wheel members and the worktree source.

Wheel `qorqut_core-0.1.0-py3-none-any.whl` SHA-256:
`50c2dc59bb46a02850c9f07f36290ee7b2b8615d4afe4da6e43f06950a43f328`.
Logs and `dependency-provenance.json`, `source-provenance.json` and
`installed-provenance.json` remain in the private bundle. An incidental pip cache
copy was identified by this private build's origin and wheel hash; only that
wheel and its origin record were removed, retaining the private artifact.

Preparation checks cover the observed eight hook-trust tables plus synthetic
model/provider/effort/user extras, exact bytes/inode/mtime through new-run prepare
and retry, subsequent trust changes, actual manifest hashes, original generated
rotation, preservation mode retention/reset, owned-field/header changes, extra or
missing owned keys, strict boolean-versus-integer comparison, malformed TOML/UTF-8,
invalid/missing claims and identities, other-file conflicts and two concurrent
edit windows. Existing root-lock, immutable-run, size-limit and interrupted-write
checks remain in the owning suite. The new preservation test first reproduced
the original changed-generated-file refusal before implementation.

The original saved `codex-resume/config.toml.native-modified-before-resume-prepare`
was read directly from the authorized private rehearsal evidence. Its 2,847 bytes
have SHA-256 `887944a27806a4571658ad9589e1f09f800856d1da6983c166a913d4676617e2`.
A new diagnostic uses only that file's synthetic endpoint credentials and owned
temporary roots. Source and installed runs preserved all bytes, inode and mtime
through a new run and retry, updated hooks/binding, recorded the actual hash and
refused rotation without partial installation. Eight original hook-trust entries
remain. The original file is unchanged; no config copy, token or native store is
tracked. Diagnostic script/results are private (`saved-config-diagnostic.py`,
`source-saved-config-diagnostic.json`, `saved-config-diagnostic.json`).

## Derived screen fixtures

`tests/adapters/fixtures/native-composers.json` contains four empty observations
and two setup negatives derived from the accepted rehearsal's untouched snapshots.
All originals were read directly; the old native fixture was never rerun.
Source paths below are relative to the authorized rehearsal `evidence` directory.
The ANSI file has suffix `.ansi.txt`; its paired metadata has suffix `.json`.

| Original stem | Original ANSI SHA-256 | Original metadata SHA-256 |
| --- | --- | --- |
| `claude-fresh/snap-t8-before-prompt` | `6b4e07493e51c8c4c681e09c6b3990e4b21617d070e2d28ec968998073475ad9` | `2c1b543b01c4c3af82a2dbbd3f9e4e8a6de4a4a3732dee42395c61da1ade3e53` |
| `claude-resume/snap-r2-before-prompt` | `91fbd61d3bccced392ef02435abf0f45fb28633f7ff894f9d4f16b23663c5a85` | `5671260322dffd94e0f331dde554d73c7d6564553b454c9fc1d0be8c063b2ba4` |
| `codex-fresh/snap-t3-after-hooks` | `1fcb3d2cd910f36ffa9cf667d2fc7aaffdd0ca46e2e6497c1afed16da50b09d5` | `9320e091799ecdcdf221a254427a0e746cccb25f748e6c67973206511228877c` |
| `codex-resume/snap-r2-after-hooks` | `ee575221b031decb2922a52b94394e7a2e65d8d596349e48bfe595697b2fb678` | `61b816c652845826750e7989becaa8bb59dfd1e94fb77a274f19d5e0e7240a29` |
| `claude-fresh/snap-t1-initial` | `1f3c3ad446a10160374eb7fefd0f07e0514776bfc9b6d1d0ba6a18235a79df84` | `a878dbcff9f700d7ba9267eaa182cbdf591434ebb9e458a67dcd08b0dcb9be49` |
| `codex-fresh/snap-t1-initial` | `9a78c5515b0324018b8468fda5f989f0e51dce5799ba244dc251c8b0c9f1b9a1` | `858ee05fcf6b80370059ab19d4a136c7a31e1e87b78fdb3056f87feae78cf66b` |

Mechanical sanitization: replace only private absolute header/status path spans
(starting with the private home prefix or `/…/`, ending before ESC or newline)
with `/synthetic/` followed by enough `x` characters to preserve the original
character width. Preserve a trailing `…` if present. This changes one span in
each Claude capture and two in each empty Codex capture; the Codex setup screen
has none. Claude setup's unchanged continuation `/workspace` remains synthetic.
No prompt, cursor, SGR, box rule, footer wording or geometry is rewritten. The
Codex status path alone is replaced with an equal-width synthetic path. Metadata
is reduced to version/provider, dimensions and cursor, with original hashes;
process records and private metadata are not copied into the fixture.

The JSON also records each derived ANSI hash and replacement count. Generation
checked every row's width; direct diagnostics checked the original hashes and
unchanged SGR sequences. Tests verify derived hashes. The four untouched private
empty screens and their sanitized derivatives all classify empty; both setup
screens remain unknown/blocked. Before correction, the originals/derivatives
reproduced Claude's footer refusal and Codex's styled-placeholder refusal.

Grouped mutations cover inserted text, typed/partially styled placeholders,
nonblank or extra input lines, visible/hidden and position cursor changes, wrong
footer, changed box/status width, missing model/path, setup/busy/dialog text,
ambiguous nearby prompts, historical text, non-SGR escapes and unsupported
versions. None authorizes paste.

## Limits

This is offline compatibility correction and installed adapter verification.
No native CLI, provider, browser, tmux or original rehearsal fixture was executed;
no live stores, credentials, executable pins, services or global configuration
were changed. Existing adapter probe checks run only their own synthetic script.
These results do not establish safe Chat paste, startup/inbox connection,
complete native capture, VM/browser readiness or arbitrary CLI-version support.
No merge or deployment was performed. Independent review remains pending.
