# D55 local access UI implementation

2026-10-09, branch `codex/build-local-access-ui-r1`, based on
`b0401eccb2d2aa16e32af3f59c56c8299332d6fe`. Implements the UI portion of the
Manager's frozen [local-access decision](https://github.com/qorqutbot/qorqut-core/blob/main/docs/LOCAL-ACCESS.md).

The store discovers `{actor_id, api_version: 1}` with GET `/api/v1/session`,
then loads schema and workspace. Login/logout methods, authentication state,
token form, sign-in/sign-out controls and obsolete CSS are removed. Fetch omits
credentials and sends no actor header. A 401 is a refused connection with Retry
connection. Native runtime Session presentation keeps its separate meaning.

Connection retry and recovery rediscover identity, abort obsolete reads and
invalidate server rows/cursors. Drafts, pending mutation actors, exact arguments
and idempotency keys remain in page memory. Interrupted mutations stay uncertain;
late confirmations cannot clear newer drafts. A different discovered actor blocks
retry with workspace-identity wording. No mutation is automatically replayed.

Checks passed with Node 22.22.0 and installed Google Chrome 153.0.8010.36:

- `npm run build` (TypeScript and production Vite build).
- `npm test -- --grep 'direct local opening|selection and late|ambiguous send|synthetic runtime inspector|synthetic Terminal protocol'`
  — five existing groups passed in 35.7 seconds.
- `git diff --check`.

The browser checks cover initial connecting/error/direct opening; identity
shape and discovery order; no login controls, Set-Cookie, API cookie, Authorization
or actor header (including an obsolete synthetic cookie); invalid identity and
unsupported schema; draft retention through 401/503 and failed recovery; actor
mismatch; pending same-key retries and late mutation/read fences. Existing native
Session and Terminal control, teardown, queue, geometry and no-replay assertions
remain. The desktop connecting/workspace and mobile error screenshots were
visually inspected; the 390px error panel has no page overflow.

Private evidence root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-local-access-ui-r1/`.
The test log is `checks-r1.log`; screenshots are under `checks-r1/`, including
`shell-direct-local-opening-4c551-nd-recovery-preserve-drafts/` and the existing
runtime/Terminal groups. Original evidence was not overwritten.

These are real Chrome checks against temporary synthetic API/Terminal fixtures,
not paired-core acceptance or native/provider execution. No live test instance,
service, agent data/session or native config was changed. No Core edits, new
dependencies, broad suite, PR, merge or deployment. Manager owns the actual
paired-server UI check and deployment after review.
