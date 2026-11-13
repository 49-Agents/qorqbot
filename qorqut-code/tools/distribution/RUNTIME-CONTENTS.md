# Native runtime source selection

`runtime_source.py` prepares the source subset used to build the Linux download.
It is a build tool and is excluded from its own output. Run it after exporting
the selected commit with `tools/export-unified.py`; never point it at a worktree
or an installed instance.

```sh
python3 -B tools/distribution/runtime_source.py project \
  --source /absolute/build/export \
  --output /absolute/build/runtime-source \
  --report /absolute/build/reports/runtime-projection.json

python3 -B tools/distribution/runtime_source.py audit \
  --source /absolute/build/runtime-source \
  --wheels /absolute/build/first-party-wheels \
  --report /absolute/build/reports/runtime-audit.json
```

The output directory and report file must be new. Their parent directories must
already exist. Reports stay outside both source and delivered output and are
created with owner-only permissions. The full original manifest and every
excluded or modified file appear in the projection report. The delivered
manifest contains no local repository paths: commit/tree IDs identify the input
history, while recomputed snapshot digests identify the selected runtime bytes.

The default selection contains host, Operations, Context, dashboard, Agentic,
roles, document history and provider-neutral Email Management. Google Calendar
remains under the host's `extensions/google-calendar` directory. Selecting its
source does not configure an account or activate a provider. The treg toolkit
is under the host's `extensions/treg` directory in the same way; the launcher
selects it for every installation, and it contacts nothing until the owner adds
a treg key in the dashboard. Fastmail, Telegram,
WhatsApp, email engine, Paper, content and Cloudflare components are excluded.
Host-owned `extensions/fastmail`, `extensions/telegram` and
`extensions/whatsapp` directories are also excluded, so an ordinary-directory
source repository can contain optional installation packages.

`native_build.py` calls `audit_prepared` once, including wheel resources and
console entry points, and embeds its findings in the build report. A separate
CLI audit is available for investigation; it is not a prerequisite to repeat
before every native build.

The filter removes development tests, CI, Git control files, private reports,
build/deployment tools and the developer release executor. It preserves runtime
initialization, migrations, recovery, native helpers, instruction resources and
notices. Template `.gitignore` files are runtime data and stay. The only byte
edits remove developer release console scripts/package data from the host
metadata and replace the release package initializer so the retained lifetime
helpers do not import the removed executor.

The audit checks the snapshot again, required runtime assets, notices,
first-party Python import closure, optional payload paths and package data.
`--wheels` is the **first-party wheel directory**, with exactly one wheel for
every Python project in the projected source. Missing wheels, changed or absent
resources, optional dependencies/payload, development entry points, unsafe
members and duplicate members fail. Build wheels in a separate copy, because
build backends can write generated files into their source directories.

Projection deliberately leaves still-imported provider modules visible. Moving
an extension out of core must remove its startup/import dependency; deleting
its files during packaging would produce a broken download. Audit reports name
retained importers for any optional payload still present. An unknown manifest
component also requires an explicit policy choice; it is not silently dropped.

Exit codes are `0` for successful projection or a clean audit, `1` for content
audit findings, and `2` for an invalid input/refused operation. A build must stop
on either nonzero audit result. `audit --prepared /absolute/prepared` accepts the
existing prepared layout (`src/`, `wheels/`, `*venv/`) and additionally checks
installed release console scripts.

This gate does not choose the application license, perform a
personal-data review, audit third-party dependencies or prove clean-machine
operation. Those release checks remain required. A missing notice is reported;
the tool does not invent a license to make a build pass.

Run the offline regression checks with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
  -s tools/distribution -p test_runtime_source.py -v
```
