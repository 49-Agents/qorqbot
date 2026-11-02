# Linux release runbook

Build from one reviewed commit in the ordinary publication repository. Keep
private input manifests, test instances and reports outside every artifact.

1. Export the exact commit with `python3 qorqut-code/tools/export-unified.py --repository /absolute/repo --commit COMMIT --with-fastmail --output EXPORT`.
   During the private multi-repository transition, use `tools/export-source.py`
   with its reviewed manifest and `--require-gitlink-pins`. Git metadata/private
   working files are never exported.
2. Run `python3 -m unittest discover -s tools -p 'test_export_*.py'` and
   `python3 -m pytest -q tools/distribution/test_runtime_source.py tools/distribution/test_native.py tools/distribution/test_native_extension.py` from the host component, plus affected feature tests.
3. Run `tools/distribution/runtime_source.py project --source EXPORT --output RUNTIME_SOURCE --report PROJECTION.json`.
4. Run `tools/prepare-local.py --source-root RUNTIME_SOURCE --output PREPARED --with-google-calendar`.
   Standard Python build/pip produce the first-party wheels and the resolved
   application dependency inventory. No native toolchain is built or copied.
5. Run `tools/distribution/runtime_source.py audit --prepared PREPARED --report CONTENTS.json`.
   Require a clean result, including first-party license/resource coverage.
6. Run `tools/distribution/native_build.py --prepared PREPARED --output BUILD --report BUILD_REPORT.json --version VERSION`.
   This assembles the app and verified wheelhouse. The system Python interpreter,
   Git/tmux, clients, browser and desktop stack are external requirements.
7. Run `tools/distribution/check_native.py --archive BUILD/ARCHIVE.tar.gz --output NEW_ACCEPTANCE_DIRECTORY`.
   Its test-only Ubuntu image installs ordinary Python/Git/tmux packages using
   APT, then runs the exact download as a nonroot user with networking disabled.
   Core must run without Chrome/Node/Codex/Claude. Check missing-tool messages,
   restart, repeated supported updates and preservation of task/document/owner/DB
   identity. The receipt records the exact test image and archive hashes.
8. Verify agent preparation, user-installed clients/browser and optional connector
   installation separately. Stock-host AppArmor/browser acceptance and actual
   account/provider acceptance are distinct from Docker and synthetic tests.
9. Build connectors with `tools/distribution/extension_bundle.py`, using exact
   wheels plus required dependency wheels. It does not install account/vendor
   software. Test the final bundle against the final core download. Include any
   corresponding-source artifact required by a helper's license next to it.
   For Fastmail and Telegram, run `tools/distribution/check_optional_extensions.py
   --archive BUILD/ARCHIVE.tar.gz --bundle telegram=TELEGRAM.tar.gz
   --bundle fastmail=FASTMAIL.tar.gz --report-directory NEW_CONNECTOR_EVIDENCE`.
   The CI workflow builds these bundles and runs this offline preservation check.
10. Audit final archives for private data/history/tests/build tools, unexpected
    external executable payloads and notices. Record sizes, checksums, resolved
    libraries, source commit, test results and real limitations. Any changed bytes
    require a new artifact and appropriate acceptance.
11. Complete the publication-date/license ledger and source privacy review.
    Prepare the release notes, app/connector/source assets and checksum file.
    Build/check commands never upload, push, deploy or change visibility.

Only the owner may change repository visibility. Historical legacy-migration
suites are documented in HISTORICAL-TEST-SCOPE.md and do not qualify conversion
of the old private deployment. Data migrations remain app-specific work even
when package installation uses standard tools.
