# Browser MCP/HTTP R2 evidence

Bounded M06 increment, 2026-10-07, branch `codex/browser-mcp-r2`. Initial status
was clean at assigned base `9e74c6fb95d5ab99fee59f5e4e3106613d3f1541`.
The accepted local-effect parent is
`a94e537519ba19d484375a7f8fb986339149285a`; its six modules and owning tests are
unchanged. This increment follows `docs/PROTOCOL.md` and the Manager's assignment
against core coordination `8a4b951313fdc750f941f69e7a33880c165d607f`.
Implementation/delivery commit and remote verification are recorded in the
delivery addendum and bound ACLA completion report, after commit/push.

## Manager amendment retained for review

ACLA question 44 was answered by Manager message 47, **browser MCP amendment 1**:
"Private" is a trusted provisioning/deployment requirement for the complete
fixed handoff destination, not a browser-enforced address-class allowlist.
Configured private DNS names are valid. Do not introduce IP classification,
DNS-resolution admission rules, CIDR settings or a network-policy subsystem.

Validate HTTP/HTTPS scheme, nonempty hostname, valid port syntax, no userinfo or
fragment, and the existing private token-file rules. Model arguments cannot
change the URL. Use direct connections without environment proxies, redirects
or insecure TLS; retain ordinary HTTPS certificate verification, finite
timeouts, bounded replies, originating attribution/target/key/payload, conservative
uncertainty and no automatic retry. DNS acceptance is syntactic, without live
DNS or owner-service tests. All effects remain synthetic and test listeners
private. No owner application/account interaction is authorized.

Manager's owning clarification is
`ffd4e693fb391e6b3c7110b59ebc96e38576116d` on `codex/browser-mcp-plan`; core D25
records it at `e1548010941f31e48c8e41884c365b17fda797f3`. Neither revision was
rebased/cherry-picked and no shared plan was edited. The configuration test
accepts `https://core.internal:8443/handoff` without contacting it.

## Implemented boundary

- Official MCP SDK stateless Streamable HTTP at `/mcp`, identity resource,
  `browser_turn` and conditional `browser_request_input`. Input/output schemas
  come from the owning Pydantic models. The optional standalone SSE GET stream
  returns 405; SDK calls use bounded JSON POST responses. No inert tools or relay.
- Private identity/command/receipt/control HTTP routes use the same existing
  `Store`, `Gate` and `AgentBrowser`. Production uses real readiness, existing
  provisioned state, one loopback worker at the configured port and no reload.
  Startup calls `Gate.startup()` and leaves admission closed. Receipt reads
  neither execute nor settle. Saved failed/uncertain/pending receipts remain 200.
- Actor bearer credentials fix identity; application HTTP forwarding requires
  original actor attribution. Application tokens cannot use MCP. Actor-header
  overrides and unknown model fields are rejected. Actor identity comes from
  each SDK HTTP request's scope, never mutable session state.
- Config/credential/token files must be owned private regular files, without
  symlink traversal. Tokens are distinct and at least 32 bearer characters.
  Error messages are static, SDK validation diagnostic prose is removed, and
  SDK log filters suppress raw payload/traceback prose. Identity has no paths
  or secrets. SDK transport validation remains enabled; Host/Origin are exact
  configured loopback values, with no permissive CORS or proxy-header trust.
- JSON bodies, including chunked input, and complete responses are bounded to
  1 MiB. Admission allows at most 32 simultaneous requests; blocking work uses
  eight thread slots. Body reads have a ten-second deadline. Effect work is
  shielded from SDK cancellation and retains the existing inherited file lock.
- The small stdlib HTTP handoff client uses the fixed URL and private bearer
  secret, finite ten-second socket/read limits, no proxy inheritance/redirects
  and a 64 KiB reply limit. Explicit connect separates definite pre-send failure
  from possible sends. Invalid, mismatched, truncated, oversized, redirected or
  ambiguous server replies remain uncertain. The validated receipt must echo
  actor/target/key; ready requires a target-matching binding. No local request
  database, automatic retry or control reconciliation is added.

Owned changed paths:

```text
pyproject.toml
requirements.lock
src/qorqut_browser/config.py
src/qorqut_browser/handoff.py
src/qorqut_browser/server.py
src/qorqut_browser/transport.py
tests/wire_fixture.py
tests/test_server.py
tests/test_handoff.py
tests/test_service_config.py
docs/evidence/browser-mcp-r2.md
```

## Passed checks

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
32 tests passed in 32.870s (20 existing owning tests + 12 new tests).

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_server.py -v
7 tests passed in 12.608s after explicitly disabling optional SSE GET.
```

These are actual official SDK client initialization/discovery/resource/tool
calls and HTTP calls to owned Uvicorn processes on ephemeral IPv4 loopback
ports. Browser effects use the accepted synthetic readiness and executable
helper fixtures, with temporary owned profiles/directories. Assertions cover:

- Discovery with/without bridge, exact action union, identity/resource parity,
  default target and original forwarded actor, rejected/spoofed credentials,
  concurrent actors, validation redaction, Host/Origin and both body encodings.
- Concurrent direct/proxy duplicates produce one saved browser receipt/effect.
  Application control closes both paths; historical retry preserves its binding.
  Request saturation rejects excess admission and recovers after disconnect.
- Client timeout does not cancel a held helper or prematurely confirm control.
  Killing the service leaves the helper's inherited lock held after restart.
  Lookup preserves intent; subsequent reconciliation preserves uncertainty until
  explicit settlement. Previously ready admission stays closed after restart.
- Bridge response loss followed by an explicit identical retry returns one
  saved fixture request. Transmitted bytes, actor, target, key, exact prompt and
  observed tab remain identical despite an intervening control-epoch advance.
  Malformed/mismatched replies, missing ready binding, wrong binding agent,
  oversized/truncated replies, redirects and server failure remain uncertain;
  definite connect refusal is unavailable. Deliberately invalid ambient proxies
  do not affect the fixed direct connection.
- Actual production module entrypoint stays closed when real readiness is
  absent, rejects fake-mode/worker/listen overrides and does not recreate a
  missing database. Private file/token configuration failures prevent startup.

Packaging and isolated installation:

```text
.venv/bin/python -m pip wheel --cache-dir .venv/pip-cache --no-build-isolation --no-deps --wheel-dir dist .
.venv/bin/python -m venv .venv/install-check
.venv/install-check/bin/python -m pip install --cache-dir .venv/pip-cache -c requirements.lock dist/qorqut_browser-0.1.0-py3-none-any.whl
.venv/install-check/bin/python -m pip install --no-index --no-deps --force-reinstall dist/qorqut_browser-0.1.0-py3-none-any.whl
.venv/install-check/bin/python -m pip check
.venv/bin/python -m pip check
```

Both dependency checks reported no broken requirements. The final wheel is
29,258 bytes, SHA-256
`d68a39b11c74f47c74ded0c8f5871e960c7d5bfae06dfa20a3a9c29992644713`.
Build/runtime dependencies were installed only in disposable task virtualenvs.

From the empty worktree-local `.venv/isolated` directory, the installed Python
with `-I` imported all nine package modules from `install-check/lib`, created no
files and loaded no `bazarlyk`, `qorqut_core` or UI modules. The installed wheel
also ran the configuration/actual-entrypoint checks with:

```text
/home/alp/qorqut-browser/worktrees/browser-mcp-r2/.venv/install-check/bin/python -I -m unittest discover -s /home/alp/qorqut-browser/worktrees/browser-mcp-r2/tests -p test_service_config.py -v
```

This exercises the installed production server, including its real readiness
failure and missing-state refusal. Both tests passed. Git whitespace checks
also passed. No native/model client calls were made.

## Versions and source provenance

Observed 2026-10-07 22:49 UTC: Python 3.12.3, SQLite 3.45.1, Linux
6.8.0-138-generic, pip 24.0, MCP 1.30.0, AnyIO 4.15.1, Starlette 1.7.0,
Uvicorn 0.54.0, httpx 0.28.1 and setuptools 84.0.0. Existing Pydantic 2.13.5
and pydantic-core 2.46.5 pins are unchanged; no family migration was necessary.
`requirements.lock` records all 30 tested runtime/build package pins, including
the official SDK's required transitive dependencies. Direct additions are only
the SDK, ASGI server/router and bounded thread support actually used here.

Read-only prerequisite probes found Chrome 153.0.8010.36, TigerVNC 1.13.1
(X server 12101011), and Openbox 3.6.1. `agent-browser` remains absent from PATH.
Binary presence is not browser/desktop acceptance. The unchanged adapter checks
the inspected helper mapping version 0.38.1; the fixture prints that version.

P14 source/failure paths were read at pinned Qorqut core revision
`e71fd1e527368bfd41b9815126f4bf33452aeebb` before this fresh SDK integration:

| Source | SHA-256 |
| --- | --- |
| `bazarlyk/transport/mcp.py` | `ed8be79a7f7dbd9e359bfa6c042ea7bc212857ecefa45a0ab6656daf50ac90be` |
| `bazarlyk/service/web.py` | `f44d5ab0d5981dac15198ecd81f7de193224d94b4ad9d347e87edf646080e0fb` |

No substantial new Qorqut source copy or runtime import was made; only the
ordinary SDK composition/request-context pattern is retained. Those files have
no file-local license/copyright notice. Existing P08/P09 provenance remains in
`browser-r1.md`, with no changes to their translation, gate, state or supervisor.
No live configuration, database, credential, profile or native store was read.

## Still pending

Real agent-browser compatibility, Chrome DOM/cookies and cookie persistence,
actual Chrome/VNC supervision/profile flush, VNC relay and RFB input filtering,
runtime units/cgroups and desktop survival, cross-agent execution routing, core
request/event/job/Done/Cancel/message transactions, machine holds, real VM/onbash
and native CLI integration, and G0–G6 acceptance all remain pending. The bridge
fixture is not the product's durable handoff implementation. No complete M06
or global gate is claimed. There was no system installation, desktop provisioning,
environment selection, PR, merge, rebase, deployment or release, and no nested
agent/self-review. Fresh independent Manager-assigned review is still required.
