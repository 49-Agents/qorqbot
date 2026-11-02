# Native release runbook

Build from exact reviewed commits. Do not copy a running installation, developer
HOME, credentials, browser profiles or generated agent workspaces. Keep private
input manifests and evidence outside every source and package output.

1. In the ordinary publication repository, select its full 40-character commit and
   export with `python3 qorqut-code/tools/export-unified.py --repository /absolute/repo --commit COMMIT --with-fastmail --output EXPORT`.
   This exports ordinary component directories, excludes dirty files and Git
   metadata, and records each component subtree and snapshot digest. During the
   private multi-repository transition only, use `tools/export-source.py --manifest INPUT.json --check-only --require-gitlink-pins`,
   then the same command with `--output EXPORT` in place of `--check-only`.
   A changed pin requires affected checks and a new export.
2. Check source tooling from the clean host checkout with
   `python3 -m unittest discover -s tools -p 'test_export_*.py'` and
   `python3 -m pytest -q tools/distribution/test_runtime_source.py tools/distribution/test_native.py tools/distribution/test_native_extension.py tools/distribution/test_native_notices.py`.
   Use the build/test environment containing the declared dependencies. Run the
   affected host, Operations, Agentic and connector behavior tests as well.
   Historical tests using retired APIs are inventoried in
   [historical test scope](HISTORICAL-TEST-SCOPE.md); they do not qualify old-schema
   migration support. Do not replace failed migration assertions with skips.
3. Run `tools/distribution/runtime_source.py project --source EXPORT --output RUNTIME_SOURCE --report PROJECTION.json`.
   Review retained first-party files and privacy findings. Projection removes
   optional payload and developer material; it is not a secret scanner.
4. Run `tools/prepare-local.py --source-root RUNTIME_SOURCE --output PREPARED --with-google-calendar`
   using a disposable build environment. Preserve the resolved dependency inventory.
5. Build native tools with `tools/distribution/native_toolchain.py --output NATIVE --report NATIVE_REPORT.json`.
   Docker is required during construction, not on the customer's machine. Ubuntu,
   Node, Chrome and npm inputs are recorded or pinned; transitive Ubuntu packages
   need review for each release. Do not claim bit-for-bit reproducibility.
6. Run `tools/distribution/runtime_source.py audit --prepared PREPARED --report CONTENTS.json`.
   No optional code, missing runtime resources or unexplained finding may pass.
   Application license absence is an explicit publication blocker.
7. Run `tools/distribution/native_build.py --prepared PREPARED --native NATIVE --output BUILD --report BUILD_REPORT.json --version VERSION`.
   This builds a private candidate, never uploads it, and records archive/wheel hashes.
8. Run `tools/distribution/check_native.py --archive BUILD/ARCHIVE.tar.gz --output NEW_ACCEPTANCE_DIRECTORY`
   on that exact archive. Its pinned Ubuntu container has no network and runs as
   a nonroot user. It checks first setup, startup, UI/owner, task creation/restart,
   update refusal while running or with a bad checksum, and two same-schema updates
   preserving the database inode, task and instance document bytes. It also checks
   that the container wrote nothing outside the mounted installation.
   Separately verify agent preparation and browser capability. Account-dependent
   flows require authenticated acceptance; synthetic provider tests do not prove it.
9. Build optional connector bundles with `tools/distribution/extension_bundle.py --id CONNECTOR --wheel WHEEL --output BUNDLE.tar.gz`
   (repeat `--wheel` for its dependencies). Verify the bundle digest and install it
   into a disposable stopped candidate with `./qorqbot extension-install BUNDLE.tar.gz --sha256 SHA256`,
   supplying `--config CONFIG.json` when required. Check preserved records, the
   selected extension and its resources, startup, interruption/resume and refusal
   cases. Hosting and real-account acceptance are separate release decisions.
10. Audit the archive itself, including third-party wheels and native assets.
    Reject Git history, tests, CI/deployment/build tools, private paths/data, logs,
    caches and unexpected optional dependencies. Preserve redistribution notices.
11. Record final source, dependency inventories, artifact checksums, acceptance
    results and limitations. Any post-test byte change needs a new artifact and
    appropriate verification; an earlier candidate does not qualify the rebuild.

Browser qualification must include a fresh Ubuntu 24.04 host with its default
AppArmor policy. Ubuntu restricts unprivileged user namespaces, so sandboxed Chrome
at an arbitrary extraction path may be denied even when the core app starts.
Browser automation requires an administrator-approved sandbox policy for that
executable or an explicitly configured compatible installed browser; automatic
fallback detection and host-policy changes are not implemented. See the
[Chromium guidance](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)
and [Ubuntu release notes](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).
Configured-host and Docker browser evidence does not qualify a fresh stock host.

Only the owner changes repository visibility. Build tools never push, deploy the
running app, upload artifacts or decide the license or public Git history.

For future core changes, maintain the runtime-content and core-without-optional-
extensions gates. Test optional connector wheels against the selected core runtime.
Database changes need explicit offline migration and recovery evidence; ordinary
startup and the same-schema updater do not migrate arbitrary older databases.
