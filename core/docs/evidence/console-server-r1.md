# Console server r1 evidence

Implemented D35 / `CONSOLE-SERVER.md` on `codex/console-server-r1`, based on
`055cbd48071d28c03cde3daf1a08521875c130a3`. The bound completion report records
the final full commit SHA, push result and post-commit clean status; this evidence
is committed with the implementation and tests. No merge or deployment performed.

## Delivered behavior

- Explicit ASGI lifespan starts one sequential job thread after installation and
  credential validation. Imports and application construction remain passive.
  A process-lifetime worker UUID attributes claims. Idle waits are one second;
  recoverable iteration failures use bounded one-to-five-second waits and fixed
  diagnostics. Readiness checks both `config_get` and the functioning worker.
- A bounded batch of original-epoch uncertain `file_install` and
  `conversation_publish` receipts is inspected before queued work. Failed or
  unresolved inspection receives at least a five-second durable delay. Lifetime
  and effect gates fence selection/inspection against recovery. Stale epochs,
  recovery holds, terminal effects and message dependency holds are excluded.
- Shutdown closes request admission, drains admitted invocations, stops/joins the
  worker after its current effect, and then releases the installation lock.
  Cancellation at application open, worker launch, invocation wait and shutdown
  retains ownership until durable work settles.
- `/api/v1/session` supports sign-in, identity and repeatable sign-out. Cookies
  are HttpOnly, SameSite=Strict, Path=/, Max-Age=28800; only random-token digests
  are stored, with an absolute eight-hour lifetime and a 256-session bound.
  Successful rotation revokes the prior session; restart clears all sessions.
  Capacity refusal does not evict another live user.
- Exact allowed Origin is mandatory for sign-in/sign-out and cookie operation
  POSTs. Duplicate cookies/credential headers and mixed API credentials fail
  before dispatch. MCP retains bearer attribution. Token input uses bounded,
  sanitized JSON parsing without credential-bearing Pydantic errors.
- Optional `--ui-root` / `ui_root=None` leaves configuration-file contracts and
  existing call signatures compatible. Startup checks a real absolute build root
  and regular index. Descriptor walks reject symlinks; only `/` and `/assets/`
  files are served. There is no application-route fallback. Responses use
  no-store, nosniff and a same-origin CSP including `frame-ancestors 'self'`.

## Verification

Python 3.12; all dependencies installed exactly from the unchanged
`requirements.lock`. Temporary installations, credentials, logs, wheel and venvs
are outside Git under the private run directory. The commands below abbreviate
that directory as `$RUN` and this checkout as `$WORKTREE`.

| Actual check | Result |
| --- | --- |
| `TMPDIR=$RUN/console-tmp PYTHONPATH=src:tests $RUN/console-test-venv/bin/python -m unittest discover -s tests/transports -v` | 28 passed, 50.375 s |
| After the final per-inspection shutdown and HEAD adjustments: `TMPDIR=$RUN/console-tmp PYTHONPATH=src:tests $RUN/console-test-venv/bin/python -m unittest transports.test_console transports.test_worker -v` | 13 passed, 20.686 s |
| `$RUN/console-test-venv/bin/python -m pip wheel --no-build-isolation --no-deps --wheel-dir $RUN/console-dist .` | Wheel built successfully |
| From outside the checkout, after installing the wheel in a separate pinned venv: `TMPDIR=$RUN/console-tmp PYTHONPATH=$WORKTREE/tests $RUN/console-installed-venv/bin/python -m unittest discover -s $WORKTREE/tests/transports -v` | 28 passed, 50.129 s |
| Installed imports for all eight changed/new Python modules compared byte-for-byte with source | All match; all imported from installed venv |
| Installed `pip check`; `git diff --check` | Passed |

Wheel: `qorqut_core-0.1.0-py3-none-any.whl`, SHA-256
`b311d825e73589fdc74340e540e1272ca22664c86c9a8c1fbf57851d8b10dda7`.
Logs: `console-source-tests.log`, `console-final-focused-tests.log`,
`console-installed-tests.log`, `console-build.log` in the private run directory.

The real loopback HTTP/official SDK flows cover sign-in/read/mutate/sign-out,
rotation, restart, accelerated absolute expiry, HTTP/MCP/operator parity,
idempotency, Origin/Host/ambiguous-credential refusal without mutations, framing,
body limits, disconnects and sanitized SDK errors. Source uses `python -m`;
installed flows use the generated `qorqut-core` entry point. Static HTTP checks
cover assets, headers, traversal, symlinked roots/files/directories, special
files, post-start replacement and headless serving. In-process HTTP checks cover
capacity, digest storage, startup failure, cancellation, admission drain,
recoverable worker error and lost-lock/dead-worker readiness.

Real existing workspace jobs run through the server and preserve persona bytes,
while remaining `waiting/dependency_unavailable` and lifecycle `provisioning`.
The existing file handler is crashed after rename, reopened, and automatically
reconciled from its durable receipt before subsequent queued work. Recovery-held
file work stays uninstalled. Fixed synthetic conversation/message handlers test
only worker eligibility, finite delay, missing-handler and exclusion boundaries.

## Remaining dependencies

No real browser was controlled; browser-session HTTP behavior and static serving
are verified, while frontend interaction belongs to the UI lane. No real
`conversation_publish` implementation exists at this base: the separate chat
publisher must be integrated and tested with this headless worker after review.
No native provider turn, terminal delivery, VM/runtime readiness, browser service,
TLS/public hosting, full archive acceptance or deployment is claimed. Loopback
HTTP intentionally has no Secure cookie attribute. Source event-baseline checks
were updated for the newly automatic workspace progress events; final discovery
does not duplicate the foundation suite.
