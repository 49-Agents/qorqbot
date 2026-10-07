# Adapter rehearsal corrections r1 evidence

2026-10-08. Implementation on `codex/build-adapter-rehearsal-fixes-r1`, exact
base `f4506c705f77f9c1b6b1575f1905b95c77b7c27f`. Executes D37's
[adapter correction assignment](../ADAPTER-REHEARSAL-FIXES.md) and Manager
amendment 162 answering question 160. Astra MAX; no delegation or internal
reviewers. Independent review and integration belong to Manager.

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
installation, at that destination, and before committing the manifest last.
Concurrent-edit injections abort without overwriting the native edit or committing
its unvalidated hash. A change after other writes can leave complete per-run
files, as with the existing interrupted-install contract; retry revalidates them.
These observations do not lock a native writer against later edits.

Composer classification recognizes only the observed lower 120-column/40-row
layouts for Claude 2.1.287 and Codex 0.160.0. SGR-aware prompt slicing preserves
Claude's NBSP/reverse-video blank and Codex's wholly dimmed placeholder. Claude's
rules and Codex's status-row width anchor the captured geometry; no screen-size
or live-process attestation is inferred. Historic prompts do not become current
input. Synthetic layouts retain their evidence label; recognized observations
use `native_snapshot`. Every result still has `native_verified=false` and
`permits_paste=false`; capabilities, callbacks and launch arguments are unchanged.

## Checks

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
