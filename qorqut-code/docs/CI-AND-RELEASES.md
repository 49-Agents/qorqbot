# CI and release preparation

Ordinary PR validation and downloadable installer qualification have different
triggers. There is one authoritative copy of each workflow in `.github/workflows`.

| Change | Automatic checks |
| --- | --- |
| Documentation in the explicit documentation directories | Secret scan and CI selection tests |
| Dashboard | Node dashboard tests |
| Application/engine code and managed instructions | Current backend tests |
| HTTP/service interfaces | Backend and dashboard tests |
| Installer, packaging, shared installation contracts or dependency metadata | Backend tests and downloadable archive qualification; all connectors for shared contracts |
| One optional connector | Backend tests, core archive qualification and that connector; unrelated connectors are not rebuilt |
| CI rules or unclassified inputs | All checks |

`Checks` runs on PRs and on pushes to `main` and `dev`. `main` receives only PRs
from `dev`; a PR into `main` from any other branch fails the `base-branch` job.
Feature-branch pushes do not duplicate
the PR run. New commits cancel superseded runs. The selector reads the complete
Git diff (including deleted files and both sides of moves); it does not use a
limited changed-files API. Runtime instruction and prompt Markdown is code,
not a reason to skip tests. Inspect a selection locally with:

```sh
python3 qorqut-code/tools/ci/changes.py qorqut-code/biz-dashboard/app.css
python3 qorqut-code/tools/ci/changes.py qorqut-code/extensions/telegram/pyproject.toml
python3 -B -m unittest discover -s qorqut-code/tools/ci/tests
```

Backend CI installs the current first-party sources and runs the current
`app/tests`, preparation tests and distribution unit tests. Dashboard CI uses
Node's test runner; tests that explicitly require an unavailable local browser
report skips. Browser/provider sign-in acceptance remains a separate check.
No live installation, account, native conversation or provider is a test fixture.

## Download releases

Dispatch `Release checks` with the intended version and connector selection to
qualify a Linux download. Mac qualification remains a separate manual workflow.
The Linux workflow can also be called by ordinary CI for relevant changes. It
retains the exact checked archives, optional bundles, WhatsApp corresponding
source, reports and failure logs as Actions artifacts. It never publishes or
deploys them. Use the retained, checked bytes for publication; rebuilding the
same commit creates a new candidate requiring its own qualification.

Installer restart, self-update preservation, checksum refusal and offline
connector checks remain in this workflow. WhatsApp's offline corresponding-source
rebuild remains required when qualifying WhatsApp. An unrelated Telegram change
does not run the Go builds. First-party license/resource and console-entrypoint
audits run once inside `native_build.py`; their report is in `build.json` under
`source_audit`.

See [the download runbook](distribution/RELEASE-RUNBOOK.md) for platform-specific
commands and publication requirements. Download qualification is not evidence
that an existing production database or managed instruction history can upgrade.

## Faster local preparation and backend releases

`prepare-local.py` caches pure-Python first-party wheels in
`${XDG_CACHE_HOME:-~/.cache}/qorqbot/wheels`. Keys include package source content,
file modes and symlink targets, the Python/platform identity, and the preparation
and snapshot tooling. Unrelated commits reuse identical packages. Changed
resources invalidate them even when size and modification time are unchanged.
Cached wheels have their hashes and ZIP integrity checked before reuse; missing
or damaged entries cause a rebuild. Native wheels are not reused by this cache.

Use `--wheel-cache /absolute/cache` to choose a cache or `--no-wheel-cache` for a
cold build. CI saves this cache and pip's download cache across runs. The
preparation receipt identifies each reused wheel and its build-input digest.
First-party dependency reconciliation, source validation and installed-wheel
provenance checks still run. Prepared environments remain independent; the
cache never copies a live virtualenv, instance configuration or database.

For an ordinary backend release:

1. Choose one reviewed commit and prepare it once, reusing unchanged wheels.
   Apply the current source tests rather than rebuilding every downloadable
   platform and optional connector for the server.
2. Before stopping the service, check that candidate against the existing
   schema, installed extensions and retained managed instruction inventory.
   Use the installation's supported read-only preflight/offline upgrade procedure.
   Startup is not a migration rehearsal.
3. Use the prepared candidate for cutover and check readiness and retained agent
   continuity. Keep configuration, data, helpers and native sessions persistent.
   Do not rebuild the candidate during cutover.

These repository tools prepare artifacts; they do not select a production
service or change the installed deployment worker. The coordinated source
release worker and the downloadable-folder updater have their own documented
scope. Verify the actual installation's deployer and target before using either;
a stale host-specific skill is not a current deployment contract.
