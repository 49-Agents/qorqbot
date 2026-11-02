# Installed-source tooling

Small tools that work on a **prepared** installation: the output directory of `tools/prepare-local.py`, with its
`PREPARATION-RECEIPT.json`, `src/` copy (including `SOURCE-MANIFEST.json`), `wheels/` and owned environments. They use that
receipt as the only record of what was selected; nothing is read from sibling checkouts, Git history or an older source receipt.
Creating the source export and the prepared output is the job of `tools/export-source.py` and `tools/prepare-local.py` (see
the installation guide); none of these tools installs, repairs or configures anything, starts a service or contacts a provider.

Commands below use `python3` for the tools that need only the standard library, and the prepared `venv/bin/python` where
`packaging` is needed.

## Verify a prepared installation

```sh
python3 app/tools/check_installed_host.py --prepared-root /absolute/prepared --output /absolute/new-report.json
```

`--output` must be a new file outside the prepared root. For every component, wheel and environment the receipt records, the
checker verifies:

- the `src/SOURCE-MANIFEST.json` components equal the receipt's `source_components`, and each wheel's source directory is owned by
  the component, commit and tree the receipt records; component snapshot digests and the exact manifest digest are revalidated;
- each wheel under `wheels/` has the recorded SHA-256, name and version, and the receipt has the wheels the selection
  (`content`, `paper`, `fastmail`, `google_calendar`) requires and none it does not: a standard preparation records only `venv`,
  with no email engine, no Fastmail package and no Google Calendar package; `--with-fastmail` adds the `qorqut-fastmail` and
  `email-engine` wheels and `email-engine-venv`, and `--with-google-calendar` adds only the `qorqut-google-calendar` wheel to `venv`;
- in each environment (`venv` and, with the selection, `paper-service-venv` and `email-engine-venv`), run with its own `bin/python` as
  `python -I -B`: the interpreter is that virtual environment (no system site-packages), nothing on `sys.path` leads outside it,
  every first-party distribution is one of the receipt's wheels and was installed from exactly that local wheel file (an index
  install, VCS, directory or other URL origin is refused), its installed Python and package-resource files equal the wheel's bytes,
  and its top-level packages import from inside the environment;
- `pip check` passes.

The report names the environments, wheels and components checked and lists every failure; the exit status is 1 if there is any.
It runs no test. The installed behavior is accepted separately by the final assembly.

## Retired

The saved-installation lifecycle helpers (`release_worker.installation` dependency validation and staged-core probe, the
`tools/installation` installer and launcher, and the bootstrap source-map migration) are gone; fresh preparation and the initializer
supersede them.

`app/tools/derive_host_schema.py` and its generated ownership ledger are gone: the committed host and extension storage
contributions (`bazarlyk/composition/schema_ddl.py`, `tables.py` and each extension's own contribution) are authoritative and are
edited directly. The earlier private foundation-receipt installed-host gate and its default pytest sweep are
replaced by the checker above.
