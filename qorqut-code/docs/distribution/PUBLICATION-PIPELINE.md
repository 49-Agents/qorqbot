# Publication pipeline

A release is built from exact committed source, projected to a runtime-only tree,
and qualified as an immutable native archive. Source export, runtime filtering,
native assembly, same-schema updates and optional extension installation are
implemented. A passing source or unit check is not proof that a particular
archive works: the final archive must pass the acceptance gates below.

## Source selection

The publication layout is one ordinary Git repository containing core and everyday
connector directories. Paper and content creation remain outside it. From a clean
reviewed tooling checkout, export an exact 40-character commit:

```sh
python3 qorqut-code/tools/export-unified.py --repository /absolute/publication-repo --commit COMMIT --with-fastmail --output /absolute/new-export
```

This reads committed directory trees from that one repository; it needs no
submodules, child repositories or private siblings. Dirty and untracked files
are excluded. Each format-1 `SOURCE-MANIFEST.json` entry records the selected
commit, component subtree and exported snapshot digest. Nested component files
belong to their own snapshot. Git metadata and `.gitmodules` are excluded.

During the private multi-repository transition only, use `tools/export-source.py`
with a reviewed manifest and `--require-gitlink-pins`, first with `--check-only`
and then with `--output`. That gate checks every nested pin against its parent.
Its input manifest contains local paths and belongs outside the publication tree.
The transition tooling does not push source or change repository visibility.

Ordinary PR checks and backend releases use the narrower triggers in
[CI and release preparation](../CI-AND-RELEASES.md). The following gates apply to
download qualification, not to every code change.

## Current gates

1. **Source:** reviewed commits, source privacy review, dependency selection,
   intact supplier instruction digests and the applicable tests. The exporter
   checks exact selection and safe paths; it is not a secret or license scanner.
2. **Runtime projection:** `runtime_source.py project` retains initialization,
   migration, recovery and notices, and excludes tests, development history,
   build/deployment tools and optional connector payloads. Google Calendar is
   bundled but inactive; Fastmail, Telegram and WhatsApp install separately.
3. **Build:** `prepare-local.py` and `native_build.py` build
   the Ubuntu 24.04 x86-64 archive. Docker is a construction and acceptance tool,
   not a customer requirement. System tools are user-installed dependencies; record the resolved Python library
   inputs and hashes. Do not claim bit-for-bit reproducibility.
4. **Contents:** the prepared-source audit inside `native_build.py` plus inspection of the final archive,
   including native assets and third-party wheels. Preserve required attribution.
   Missing or unapproved application licensing blocks publication.
5. **Archive acceptance:** `check_native.py --archive ARCHIVE --output NEW_EVIDENCE`
   uses an Ubuntu image with APT-installed prerequisites, a nonroot user and no network. It checks the actual
   archive's setup, verification, startup, local UI and owner, task creation,
   restart, running/bad-checksum update refusal, two same-schema updates, preserved
   database inode/record/document bytes, and writes confined to the installation.
   Agent preparation, browser operation and optional connector installation need
   their corresponding acceptance evidence too. Synthetic provider tests do not
   establish authenticated real-account connectivity.
6. **Record:** exact source manifest, dependency inventories, archive checksum,
   test results and remaining limitations. Changed bytes need a new artifact and
   appropriate repeated verification. Keep private evidence outside outputs.

The [runbook](RELEASE-RUNBOOK.md) gives the executable sequence. Packaging unit
checks, runtime projection checks and disposable archive acceptance exist; the
presence of these tools is not a claim that final release acceptance has passed.
An earlier candidate's result does not qualify a later rebuild.

## Updates and optional connectors

The `qorqbot/` folder contains `runtime/` and `instance/operations/`. The updater
requires a stopped installation and the expected archive SHA-256, retains instance
data and refuses incompatible schema changes. An interrupted update fences normal
startup until explicit recovery. No automatic conversion of old deployments or
rollback to an incompatible binary is promised.

Optional connector bundles are built with `extension_bundle.py` and installed
through `qorqbot extension-install`. The installer verifies bundle resources and
wheel ownership, performs the supported additive installation, and fences startup
while an interrupted installation requires resume. Extension removal, arbitrary
package installation and general schema migrations are not implied by that path.

Future changes must keep core-without-optional-extensions acceptance, runtime
content checks and relevant extension tests passing. Schema changes need an
explicit migration and preservation/recovery tests before an update is supported.
Current migration and preservation tests live in `app/tests`. Fresh-archive
qualification alone does not establish compatibility with old database formats.

## Publication decisions

The application license is PolyForm Noncommercial 1.0.0, with a separate per-person
commercial license from 49 Agents LLC.
Finalize release dates, dependency obligations, source review and asset hosting. No command here uploads artifacts, pushes source, deploys the
running app or changes repository visibility. Only the owner changes visibility.
