# Download release runbook

Use this for a downloadable release or installer change. Ordinary PRs and backend
releases follow [CI and release preparation](../CI-AND-RELEASES.md). Run commands
from the repository root, using one reviewed commit. Keep private input manifests,
test instances and reports outside every artifact.

The manual `Release checks` workflow performs Linux preparation and qualification
and retains the checked downloads. Supply the release version and the connectors
being released. Reuse those exact artifacts for publication; do not rebuild them
merely to copy them to the release channel.

For a local build:

1. Run the relevant current application and tooling tests. The `Checks` workflow
   lists their installation requirements and executable commands; obsolete test
   inventories are not release gates.
2. Export the exact commit with `python3 qorqut-code/tools/export-unified.py --repository /absolute/repo --commit COMMIT --with-fastmail --output EXPORT`.
   Git metadata and private working files are never exported.
3. Run `python3 qorqut-code/tools/distribution/runtime_source.py project --source EXPORT --output RUNTIME_SOURCE --report PROJECTION.json`.
4. Run `python3 qorqut-code/tools/prepare-local.py --source-root RUNTIME_SOURCE --output PREPARED --with-google-calendar --with-treg`.
   Unchanged pure-Python first-party wheels reuse the content-checked cache. The
   receipt records source, wheel hashes, reuse and resolved dependencies. No
   native toolchain is built or copied.
5. Run `python3 qorqut-code/tools/distribution/native_build.py --prepared PREPARED --output BUILD --report BUILD_REPORT.json --version VERSION`.
   The builder audits source, wheels, notices/resources and console entry points
   once, retaining the findings in `source_audit`. It assembles the verified
   wheelhouse and archive. Python, Git/tmux, clients and browsers stay external.
6. Run `python3 qorqut-code/tools/distribution/check_native.py --archive BUILD/ARCHIVE.tar.gz --output NEW_ACCEPTANCE_DIRECTORY`.
   The exact archive runs as a nonroot user with networking disabled. Check fresh
   setup, missing-tool messages, restart, supported repeated updates and retained
   task/document/owner/DB identity. Keep the report and failure logs.
7. Build only the optional bundles being released and qualify them against this
   exact core archive with `check_optional_extensions.py`. Include WhatsApp's exact
   corresponding-source archive and offline rebuild evidence when releasing its
   helper. Actual provider sign-in, native clients/browser and stock-host checks
   are separate from synthetic offline qualification.
8. Preserve the verified archives and checksums, source commit, dependency/license
   notices, source artifacts, test evidence and platform limitations. Complete the
   publication-date ledger and release notes. Publishing is a separate maintainer
   action; build commands do not deploy, publish or change repository visibility.

Only the owner changes repository visibility. Existing installations require
explicit database/instruction compatibility checks and migration evidence; a
fresh-install or same-archive update test does not establish those properties.

## Additional platforms

Windows uses the qualified Linux archive in user-installed WSL2 Ubuntu 24.04.
Run `tools/distribution/windows_bundle.py --linux-archive ARCHIVE --sha256 SHA256
--output NEW_WINDOWS.zip` to wrap that exact archive with instructions and its
checksum. Test installation inside the WSL Linux filesystem and dashboard access
from a Windows browser before claiming Windows host acceptance. See WINDOWS.md.

Mac targets native Apple silicon, macOS 14+, with user-installed Python 3.12,
Git and tmux. Follow export, projection and preparation steps above, then add
`--target macos14-arm64` to native_build.py. A Linux assembly can fetch Mac wheels
but cannot qualify them. On an actual Mac run `tools/distribution/check_posix.py
--archive MAC_ARCHIVE --output /tmp/qb-mac-check`; the output must be a new short
path. `.github/workflows/macos-checks.yml` prepares this native build/check on
manual dispatch, on a GitHub-hosted `macos-15` Apple-silicon runner. The package
target remains `macos14-arm64`; native macOS 14 acceptance is a separate check. Supply its optional `version` input (for example,
`1.3.0-preview.2`) to also build a DMG from the checked archive. The job mounts
that DMG, checks its signature and identical embedded archive, and exercises
the packaged launcher with a fresh home folder: install, verify, dashboard,
stop and restart. It saves the DMG, checksum, archive and reports as a GitHub
Actions artifact. Without a version input it only runs preparation checks.

Use a version newer than the existing download: `1.3.0-local.*` sorts below
`1.3.0-preview.1` and will not be offered as an update. Check the actual embedded
version; changing only the filename or release tag is insufficient. Update the
license ledger for the first public distribution date before building a release.
CI artifacts do not prove the downloaded app's Gatekeeper/Open Anyway flow,
a separate macOS user account, or interactive agent sign-in. Record those
checks separately before promoting the candidate to the website.

Record fresh setup, restart, stopped updates, process permissions, agent terminal
and browser results separately. Mac process inspection can be denied or return
an empty environment for another process; update/connector installation must
then refuse rather than infer that the installation's tools are stopped. Native
CLI startup/status/stop inspect their saved PID identities, not all processes.
See MACOS.md for the remaining advanced Linux-only facilities.

Build optional Telegram with a Mac Pillow wheel. Fastmail uses platform-neutral
wheels. The optional WhatsApp helper must be built on an Apple silicon Mac with
`extensions/whatsapp/tools/build.py --target macos-arm64 --work NEW_DIRECTORY`;
install the documented Go and C toolchains first. Include the resulting matching
GPL corresponding-source archive. Do not relabel a Linux helper as a Mac binary.
