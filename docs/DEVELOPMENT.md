# Development

Use Linux, Python 3.12 and Node.js 22.12 or newer. Runtime terminals require tmux;
headed browsers additionally require Chrome, TigerVNC, Openbox and the pinned
agent-browser helper described in [browser operations](../browser/docs/OPERATIONS.md).

Run these commands from the repository root. Core and browser deliberately use
separate virtual environments because their dependency locks differ.

```sh
python3.12 -m venv core/.venv
core/.venv/bin/python -m pip install -r core/requirements.lock
core/.venv/bin/python -m pip install --no-build-isolation --no-deps -e ./core

python3.12 -m venv browser/.venv
browser/.venv/bin/python -m pip install -r browser/requirements.lock
browser/.venv/bin/python -m pip install --no-build-isolation --no-deps -e ./browser

npm --prefix ui ci
npm --prefix ui run build
```

The static frontend is built into `ui/dist/`. Core can serve it using `--ui-root`
with an absolute path. `qorqut-core serve` also requires an explicitly initialized
installation; these build commands do not create agents, start a CLI, initialize
a browser profile or configure host services. See the [console contract](../core/docs/CONSOLE-SERVER.md)
and [browser operations](../browser/docs/OPERATIONS.md) for their respective
configuration. Packaging a fresh full installation remains separate work.

Run focused existing tests from their owning module:

```sh
(cd core && .venv/bin/python -m unittest discover -s tests -p test_foundation.py)
(cd browser && .venv/bin/python -m unittest discover -s tests -p test_service_config.py)
npm --prefix ui run typecheck
```

The UI's existing Playwright suite uses disposable synthetic APIs and expects
Google Chrome at `/usr/bin/google-chrome`; run it with `npm --prefix ui test`.
These checks do not use an owner's live sessions or application data.

## Commit dates

Enable the repository hooks once per clone:

```sh
git config core.hooksPath .githooks
python3 scripts/check_commit_dates.py --all
```

Both author and committer dates must be outside 09:30–20:30 inclusive on weekdays
in `America/Puerto_Rico`. The pre-commit hook checks proposed dates, pre-push
checks every outgoing history, and the GitHub workflow checks pushed history.
Do not use GitHub's merge button during the excluded hours; it creates a new
committer timestamp. The imported history uses the owner's requested October
5–9, 2026 morning window; the [migration decision](MONOREPO.md) documents that
rewrite explicitly.
