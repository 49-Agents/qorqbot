# Native prefix r1 implementation evidence

Verification: 2026-10-08 UTC. Branch `codex/native-prefix-r1`; clean accepted
source/design base `617a12f1db3f0026108b9e679766eea50d67dd3d`.
Implementation, tests and initial evidence are committed at
`ecbae7dc086349a27a1436086434f94dd98be35e`. This is the exact tested code/test
revision; the subsequent delivery update changes only this evidence paragraph.
Final delivery and verified remote SHAs are reported through ACLA without a
self-referential commit hash.
Run `649fed24-a7af-40d7-9d41-382ab248483a`; worker
`3ec0003a-125a-40fe-94c1-5b2e8ebd8c68`; bound executor conversation
`01a11985-0d82-7560-b7a6-e55d8a34d08b`; review thread
`2a83e62c-87fa-457f-a353-b7cb60a2d15d`.
The saved Codex/Astra executor identity was retained. No delegation or internal
reviewer was launched. Fresh independent review and Manager acceptance remain
pending; this report does not approve a runtime or G0–G6 gate.

## Owned implementation

- `transcripts/spool.py`: frozen `ChunkReceipt`/`ReceiptPage` and the read-only
  `CaptureSpool.receipts` method. Each bounded page uses one short SQL snapshot,
  keeps the selected target, and includes original receipts after ACK/reclaim,
  epoch retirement or historical blocking. Endpoint lookups seek the existing
  coordinate index rather than scanning receipt history. Invalid stored
  receipts/gaps/captured endings fail without updating health or conflict rows.
- `transcripts/prefix.py`: frozen `FilePrefixProof`, a fixed read-only
  `FilePrefixChanged.reason`, and `verify_file_prefix`. It checks a borrowed
  regular descriptor against declared native receipts, reading at most 64 KiB
  with `pread`, preserving the descriptor position and ownership. It hashes
  each receipt, the selected prefix and the final at-most-64-KiB tail. Both stat
  observations compare device/inode, size, mtime_ns and ctime_ns. I/O diagnostics
  are fixed and exclude OS exception text. There is no open, close or retry.
- `tests/transcripts/test_prefix.py`: nine owning synthetic tests using the
  unchanged accepted `registered` fixture and real SQLite/file descriptors.
- This evidence file. No other tracked path changes.

A static AST comparison against the exact base, removing only the two new
receipt classes and new method, found the complete existing spool AST equal.
The schema and every existing method therefore remain unchanged. No migration,
package/lock/NOTICE, recorder, provider, archive, transport or assembly edit was
needed. This is fresh implementation of the supplied NATIVE-PREFIX/D32 design,
with no new upstream extraction.

## Owning checks

The suite covers:

- Canonical identities, strict coordinates/page limits, exact receipt endpoints,
  empty/earlier prefixes, immutable results, unknown lineage and non-native
  rejection. A real second SQLite connection appends, ACKs and reclaims between
  the first reader's epoch observation and chunk query; the page retains one
  consistent snapshot. Later pages retain its original target. Retired and
  blocked epochs remain readable without clearing either saved or local health.
- A binary stream containing every byte value, a giant record over 9 MiB and
  partial UTF-8/JSON. After actual ACK/reclaim/close/reopen, every payload is NULL
  and all 39 original receipt coordinates/hashes are verified through the held
  descriptor, across bounded pages/reads. Prefix, tail and stat proof fields
  match the synthetic input. Real connection `in_transaction` is asserted false
  during `pread` and `fstat`; no mocked spool supplies success.
- Missing middle/ending receipts, invalid stored length/hash and a gap at the
  next page fail. The narrowly injected SQL faults use only the owned database;
  a missing saved ending also blocks an explicit zero target. Read-only SQL
  authorizers, full logical dumps, `total_changes`, transaction state and the
  in-memory error latch establish unchanged capture state on success/failure.
- Earlier-byte rewrite with an unchanged 64-KiB tail, truncation, mismatched
  inode (including unsigned-sized identity input), zero read and failed read
  produce the specified failures. Negative/boolean/noninteger descriptor and
  identity arguments, an actually closed descriptor and an owned pipe are
  rejected. Borrowed regular descriptors retain their position/identity; the
  rejected nonregular descriptor stays open. Synthetic private text in an I/O
  exception is absent from the exposed diagnostic traceback.
- A renamed and then unlinked held file remains verifiable. A replacement with
  identical bytes cannot satisfy the old identity. An initially empty capture
  has empty hashes even with a preexisting suffix. Positive three-byte short
  reads preserve exact coverage; a real concurrent spool append during those
  reads cannot extend the selected target or cause suffix reads. Observed file
  growth and same-size suffix mutation during reads fail final stat comparison.

These are declared native fixtures, not provider ownership or native-session
evidence. Test ACKs are explicit synthetic assertions, not new host durability
or collector evidence.

## Commands and results

Checkout `R` is `/home/alp/qorqut-core/worktrees/native-prefix-r1`.
Private validation directory `V` is
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-prefix-r1-validation`.
Observed Python 3.12.3, SQLite 3.45.1 and Linux 6.8.0-138-generic.

Both source suites ran once on the final production/test bytes, in independent
processes with cwd `R` and this exact environment/command prefix:

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  TMPDIR="$V/tmp" PYTHONPATH="$R/src" /usr/bin/python3.12 -P \
  -m unittest discover -s tests/transcripts -p test_prefix.py -v
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  TMPDIR="$V/tmp" PYTHONPATH="$R/src" /usr/bin/python3.12 -P \
  -m unittest discover -s tests/transcripts -p test_spool.py -v
```

Results: **9 prefix tests passed in 1.346 s** and **9 unchanged spool tests
passed in 2.412 s**. No failures or skips. Existing tests/assertions were not
edited. The old spool suite retains its actual owned abrupt-exit checks; the
new prefix suite creates no child processes. No recorder/provider/whole-package
test repetition was performed.

Copied `src/`, `pyproject.toml` and `requirements.lock` into `V/build-source`;
copied only byte-identical `test_prefix.py` and `support.py` into
`V/staging/tests/transcripts`. The build and staging copies contain no fixture
data or operational inputs. The venv/build/install commands were:

```sh
/usr/bin/python3.12 -m venv "$V/env"
# cwd V/build-source
"$V/env/bin/python" -I -B -m pip install --no-cache-dir -r requirements.lock
"$V/env/bin/python" -I -B -m pip wheel --no-cache-dir --no-deps \
  --no-build-isolation --wheel-dir "$V/wheel" .
# cwd V/staging
"$V/env/bin/python" -I -B -m pip install --no-cache-dir --no-deps \
  "$V/wheel/qorqut_core-0.1.0-py3-none-any.whl"
"$V/env/bin/python" -I -B -m pip check
"$V/env/bin/python" -I -B "$V/verify_installed.py"
```

Each ran with a fresh environment containing only `PATH=/usr/bin:/bin`,
`LC_ALL=C.UTF-8`, `TMPDIR=V/tmp`, `PIP_CONFIG_FILE=/dev/null` and
`PIP_DISABLE_PIP_VERSION_CHECK=1`. All exited 0; `pip check` reported no broken
requirements. No global environment or installation was changed.

The private inventory verifier matched **43 Python files plus NOTICE**, both
staged test/support files and build metadata byte-for-byte against source
SHA-256, and verified **all 31 locked versions**. Actual root, spool and prefix
imports resolve under `V/env/lib/python3.12/site-packages/qorqut_core`.
Isolated Python's search path contains no checkout, build source, current
directory or user-site entry. Staging has no `src` or product package.

From that tests-only cwd, the new suite ran once against the installed wheel:

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 TMPDIR="$V/tmp" \
  "$V/env/bin/python" -I -B -m unittest discover \
  -s tests/transcripts -p test_prefix.py -v
```

Result: **9 passed in 0.905 s**, no failures/skips. Final source/test/metadata
hash checks still matched the tested manifest. `git diff --check` and the
base-relative owned-path/static-spool checks passed.

No product, test, build or install failure occurred and no post-test code
correction was needed. During initial source navigation, a `wc` command exited
1 because the guessed `transcripts/recording.py` name does not exist; `rg`
located the actual `recorder.py`, which was read. Large combined document
output was truncated by the display and recovered through focused reads.

| Final artifact | SHA-256 |
| --- | --- |
| `transcripts/spool.py` | `499ead964a9aef62d20fe3fbf942201e0c3c945facb31117a925e2fb62bd2023` |
| `transcripts/prefix.py` | `74aa5665ce4abb7211965217b68cb330e71c4d269b5d1ffe97cb3ef752fadf5e` |
| `tests/transcripts/test_prefix.py` | `70cd3980660e6c055241cebe18b1f69fa73f31823eac7057d672c8903f565ec2` |
| Installed wheel | `10ce004253faaadfda89095463cad911793014e963180ffeaba5e7b788a499d9` |
| Unchanged `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Unchanged `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |
| Unchanged `storage/NOTICE` | `c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d` |

Private evidence: `V/logs/{prefix-source,spool-source,prefix-installed}.log`,
venv/lock/build/install/pip/inventory logs, `manifest.json`,
`verify_installed.py` and inspection notes. The environment, wheel, build copy
and tests-only staging remain there for review, outside Git.

## Cleanup and limits

All owned temporary databases/files/descriptors were removed/closed by fixture
cleanup, and `V/tmp` is empty. The existing spool suite's subprocesses were
waited/reaped by its unchanged `subprocess.run`; no new long-lived process,
endpoint or service was created. No live Qorqut data, native CLI session/store,
owner authentication/profile, unrelated credentials or another lane's unfinished
material was used. No provider turn/probe, service/cgroup/VM/network change,
browser/UI change, global installation, merge, rebase, force push, PR/release or
deployment occurred.

Hashes cannot recover overwritten originals. Stat checks are finite observations,
not a filesystem snapshot or protection against a hostile writer. A proof
certifies only the selected prefix even when the spool has advanced; it makes
no promise about future bytes, intermediate versions, append-only behavior,
liveness, native ownership, the latest capture cursor or ACK eligibility.
Native source/run attestation, safe managed-path opening, retained ownership,
ingestion/discontinuity handling, durable checkpoints, collection transport and
provider evidence remain subsequent Manager-owned work. The accepted host
archive does not establish that those missing integrations exist.

Final commit, clean-checkout and ordinary dedicated-branch remote SHA verification
are reported through ACLA. No runtime readiness or G0–G6 acceptance is claimed.
