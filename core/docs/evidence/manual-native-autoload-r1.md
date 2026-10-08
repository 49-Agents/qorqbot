# Manual native standard-config loading r1

2026-10-08 UTC. Factual D40 exploration on base
`a475fa5f87cb05b756c870d761c7ccc5b1bd11f2`, branch
`codex/explore-native-autoload-r1`. This document is the only tracked change.
Production code, host schema 7, sixteen public operations and five handlers are
unchanged. No implementation, architecture or acceptance decision is made here.

`$RUN` is the private directory
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-autoload-r1`.
`$OLD` is its sibling `native-rehearsal-r1-validation`, explicitly identified by
[the accepted rehearsal](native-rehearsal-r1.md) and
[manual-hook source evidence](manual-hook-seams-p0.md).

## Results

| Fixed check | Result |
| --- | --- |
| Claude, genuinely fresh home and standard workspace files | **Blocked at onboarding.** Returned to Bash with `api.anthropic.com: EAI_AGAIN`, before hooks/MCP. This is not evidence that the standard files fail to load after onboarding. |
| Claude, separately authorized fixture seed, standard workspace files | **Pass after onboarding only.** Actual workspace/MCP/key consent, MCP discovery, pre-input SessionStart and native-selected UUID observed. |
| Codex, managed HOME `.codex/config.toml` and `.codex/hooks.json` | **Pass in the isolated fixture**, after the actual native hook-trust dialog. Core `initialize` and `tools/list` succeeded; SessionStart occurred on first submitted input. |
| Codex native-selected UUID and terminal ancestry | **Pass as factual observations**, using the manually supplied `--no-daemon` flag. Not product ownership admission. |

ACLA question 245 reported the initial Claude blocker. Manager decision 246
authorized only the exact previously accepted fixture onboarding seed in a
separate labeled attempt. The unseeded failure is preserved; no product setup
behavior was changed.

## Executables and containment

The actual `--version` outputs and SHA-256 of the resolved native binaries match
the accepted pins:

| CLI | Executable | Version and native SHA-256 |
| --- | --- | --- |
| Claude | `/home/alp/.nvm/versions/node/v22.22.0/bin/claude`, resolving to `../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` | `2.1.287 (Claude Code)`; `3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0` |
| Codex | `$OLD/codex-pkg/shim/codex`, the accepted Node launcher; native `$OLD/codex-pkg/install/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` | `codex-cli 0.160.0`; `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |

No packages were installed or rebuilt. Existing accepted fixture Python
libraries were reused; the recorder and manual-hook preparation import this
worktree's fixed base. No unfinished worker tree was read.

The accepted `fake_anthropic.py`, `fake_openai.py`, `hook_sink.py`, file watcher
and native process/tmux helpers were copied into `$RUN/fixtures`. Provider
protocol helpers and hook sink were reused without alteration. The accepted
SDK `mcp_probe.py` was narrowed to one core endpoint with explicit synthetic
bearer authentication; its extra synthetic target-header requirement was
removed. It records discovery method/status/auth-match, without printing tokens.
No browser endpoint or tool was configured or started.

`unshare` provides private user/network/PID/mount namespaces. The holder brings
up only `lo`; its recorded namespace-local IPv4 routing table has no routes.
The recorded private network namespace differs from the host namespace observed
after cleanup. A private `$RUN/tmp` is mounted at `/tmp` inside the
namespace, so native socket residue cannot collide with the host's `/tmp`.
Every provider has its own fresh `$RUN/native/<provider>/{home,workspace}` and
HOME, CODEX_HOME, CLAUDE_CONFIG_DIR and XDG roots. Child environments are built
from `env -i`, not copied from an owner CLI environment. Endpoint ports inside
that namespace are 18101 (Anthropic fixture), 18102 (OpenAI fixture), and 18111
(core MCP fixture). Keys/bearer are fresh synthetic values kept privately.

The fixture's initial `/sys/class/net` inventory listed host interface names
through the inherited sysfs mount. A post-run assertion treating that inventory
as namespace-local failed; the original result is retained. It is not used to
claim a measured interface count. Namespace creation/identity, the empty
`/proc/net/route`, and the holder's network commands are the containment evidence;
the corrected evidence assertion made no native or production behavior change.

The CLI is typed manually into disposable Bash (`--noprofile --norc -i`) in a
private tmux terminal, recorded by the accepted PTY recorder. Core did not launch
a native CLI. Hook groups were installed using accepted `prepare_manual_hooks`
with the test-only sink, not the production HTTP hook client. No native UUID was
supplied or preallocated. Platform fixture IDs are not native-session evidence.

Original and successive configuration bytes, including absent-file inventory,
are retained under `$RUN/evidence/<provider>/configs-*`. Harnesses, process
fingerprints, terminal recordings, synthetic native stores, payloads and provider
request bodies remain private; none is committed.

## Claude fresh-home observation

Launch at **23:24:41.080 UTC** was the bare Claude executable, with none of
`--mcp-config`, `--settings`, `--strict-mcp-config`, `--session-id`, or permission
flags. The prescribed workspace `.mcp.json` contained only the `qorqut-core`
HTTP entry and its synthetic explicit bearer. Hook groups were in workspace
`.claude/settings.local.json`.

The only provider override inputs were those already observed in the accepted
rehearsal: `ANTHROPIC_BASE_URL` to loopback, a synthetic `ANTHROPIC_API_KEY`, and
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. The existing fake `/api/hello`
behavior was enabled.

The actual native screen said “Unable to connect to Anthropic services” and
“Failed to connect to api.anthropic.com: EAI_AGAIN”, then returned to Bash. No
workspace/MCP/key trust prompt, SessionStart payload, native transcript or core
MCP discovery was observed in this initial fresh-home attempt. Network
containment prevented a real-provider connection. The error does not establish
that `.mcp.json` or the hook path is incorrect: onboarding stopped first.

`evidence/claude/{manual-launch.json,launch.plain.txt,launch.json,configs-original}`
contains the original observation. No extra config flags or pre-trusted hashes
were substituted to manufacture a successful check.

## Claude continuation after the authorized fixture seed

Manager decision 246 permitted the exact test-only
`home/.claude/.claude.json` value `{"hasCompletedOnboarding": true}`. A separate
fresh `native/claude-seeded/{home,workspace}` was created, with configurations
saved both before and after that single seed. No native trust hashes, permission
preferences or additional settings were seeded. This establishes standard-file
loading **after onboarding**, not fresh onboarding, real login, or authorization
for product code to set this value.

The same bare Claude executable was entered at **23:31:54.209 UTC** with the
same accepted local-provider environment. Its actual native dialogs were:

| Dialog | Default | Fixture selection and confirmation UTC |
| --- | --- | --- |
| Workspace trust | No, exit | Yes, I trust this folder; **23:32:04.687** |
| New MCP server found in this project: qorqut-core | Continue without using this MCP server | Use this MCP server; **23:32:14.787**. The broader all-future-servers option was not selected. |
| Detected a custom API key / use this API key? | No (recommended) | Yes, for the synthetic fixture key; **23:32:25.739** |

No separate hook-trust or bypass-permissions dialog appeared. The CLI displayed
its native default auto mode; no permission flag was supplied. Dialog screens
and exact keystrokes are in `evidence/claude-seeded/`; token values are retained
only in the private fixture evidence and are omitted from this report.

SessionStart fired at **23:32:25.909**, before any prompt, with native-selected
UUID **`cba4cb27-1100-416e-aa8d-d9ee95e7ea79`**, `source=startup`, the private cwd,
and the future path
`home/.claude/projects/<encoded-private-workspace>/cba4cb27-1100-416e-aa8d-d9ee95e7ea79.jsonl`.
The transcript was still absent in the live pre-input snapshot. The native
`.claude/sessions/296.json` was read before input: its `sessionId`, `pid=296`,
`procStart=207975723`, version and PID-namespace component matched the independent
process observation. The `pidDomain` boot-hash algorithm was not inferred.

The sink's chain was **hook 346 → sh 345 → Claude 296 → Bash 290 → recorder 287 →
tmux 286 → namespace PID 1**. Exactly one configured native executable occurred
in that ancestry, matching the saved native PID/start fingerprint. This chain
comes from the unchanged test sink's live `/proc` sampling, not from CLI-supplied
fingerprints or product hook admission.

Core MCP saw `server/discover` → 400 at **23:32:26.211**, then `initialize` → 200
at **23:32:26.277**, initialized notification → 202, and `tools/list` → 200 at
**23:32:26.387**; prompts/resources listing also succeeded. All authenticated
with the explicit synthetic bearer and `claude-code/2.1.287 (cli)` user-agent.
The 400 is the accepted SDK fixture's unsupported discovery method, followed by
successful native initialization. No MCP tool was called.

One harmless turn was entered at **23:32:59.923**. UserPromptSubmit arrived at
**23:33:00.085**, two local Messages POSTs returned canned responses, and Stop
arrived at **23:33:00.455**. The visible answer was
`qorqut-native-autoload-complete`. The displayed Opus label was not real inference.
The generated transcript's UUID/primary records matched the hook and native
session file. The live linkage check saw 23 records / 146,434 bytes and one
primary session identity; no cwd-only association was used.

Workspace `.mcp.json` remained byte-identical (**232 bytes**).
`.claude/settings.local.json` grew **5,014 → 5,066 bytes**: native MCP consent
added `enabledMcpjsonServers: ["qorqut-core"]`, with the original hook groups
semantically unchanged. The 33-byte onboarding seed became a 2,998-byte native
`.claude.json`, including project trust and native startup/key bookkeeping.
No home `.claude/settings.json` was created. Original/final full bytes remain
private. This observed native edit is why whole-file equality of the hook
settings is not claimed.

## Codex standard-path observation

The manually entered command at **23:25:06.140 UTC** was the accepted pinned
launcher, `--no-daemon`, and only the previously observed fake-provider overrides:
`-c model_provider="qfake"` and the inline `model_providers.qfake` table naming
`http://127.0.0.1:18102/v1`, `wire_api="responses"` and `env_key="FIXTURE_OPENAI_KEY"`.
There was no forced native UUID, native trust override, sandbox/approval override,
resume argument, or extra config-file argument. `--no-daemon` is a manually
supplied terminal flag in this observation, not a product auto-launcher. The
behavior without that flag was not tested here.

Managed HOME `.codex/config.toml` initially held an authored comment and the
`mcp_servers.qorqut-core` table: URL, `required=false`, and explicit bearer
`http_headers`. `.codex/hooks.json` held the eight accepted hook groups.

The actual native dialog was **“Hooks need review / 8 hooks are new or changed”**,
with Review selected by default. Fixture keystrokes Down at **23:25:24.351** and
Enter at **23:25:24.670** selected **Trust all and continue**. No native workspace
trust or login dialog appeared in this run. Native trust hashes were written by
Codex after that selection; no hash was authored by the fixture.

Core MCP `initialize` returned 200 at **23:25:24.782**, initialized notification
returned 202, and `tools/list` returned 200 at **23:25:24.790**. All used the
explicit synthetic bearer and user-agent `codex-mcp-client/0.160.0`. There was no
`tools/call`: this establishes discovery, not semantic MCP tool use or actual
core attribution. Codex displayed a startup warning that bubblewrap was absent
from the private PATH and that its bundled bubblewrap would be used. The warning
was inspected and dismissed; no install or sandbox override was introduced.

Before first input, native process **PID 75 / start ticks 207934923** held an
exclusive FLOCK on the thread lock for its newly selected UUID
**`01a11dd5-f95f-7ce3-8ddf-b0a50e73a008`**. The snapshot at **23:25:59.357** matched
the process's open descriptor, lock inode **21143305**, and `/proc/locks` holder.
The UUID-bearing lock first appeared at **23:25:24.731** in the 50 ms fixture
watcher. No rollout or SessionStart hook existed in the pre-input snapshots.

One harmless text turn was supplied. An initial immediate Enter at
**23:25:59.588** left the text visibly in the composer, with no provider request,
rollout or hook. A later separate Enter at **23:26:17.737** submitted that same
text; it was not pasted again. The observed order was:

1. Rollout first observed by the fixture watcher at **23:26:17.769**.
2. Native SessionStart recorded by the sink at **23:26:17.850**.
3. UserPromptSubmit at **23:26:17.897**.
4. Local Responses request at **23:26:17.943**, canned reply rendered, Stop at
   **23:26:17.995**. A separate title request followed at **23:26:18.001**.

These are observation times, not a precision claim beyond the recorded sink/
watcher behavior. SessionStart had `source=startup`, the native UUID above, the
private workspace cwd and transcript path
`home/.codex/sessions/2026/10/08/rollout-2026-10-08T19-25-24-01a11dd5-f95f-7ce3-8ddf-b0a50e73a008.jsonl`.
The filename's 19:25 timestamp is native local time; this report's times are UTC.

The sink's live chain was **hook 246 → native Codex 75 → Node 68 → Bash 62 →
recorder 59 → tmux 58 → namespace PID 1**. The configured native executable
occurred exactly once in that chain. Its PID/start matched the independently
saved fingerprint; the namespace inode was **4026532978**, uid 1000. The verified
rollout descriptor, thread-lock descriptor, SessionStart UUID, and first
`session_meta.payload.id` all agreed. The single observed source was therefore
corroborated beyond cwd. This is fixture association evidence, not production
admission or an exhaustive source-discovery result.

The final native file had 15 records / 46,579 bytes. The reply
`qorqut-native-autoload-complete` came from the canned fixture; the displayed
model label was not real inference. `hooks.json` remained byte-identical.
`config.toml` grew **271 → 2,347 bytes**, retaining every original byte as its
prefix, including the authored comment and core table. Codex appended `[tui]`
`screen_reader_detection_done=true` and eight hook `trusted_hash` entries.
Verbatim originals/finals are private, so bearer values are omitted here.

## Evidence and limits

The bounded checks use actual interactive CLIs, the accepted recorder, exact
native executable fingerprints, a local SDK MCP server and local canned-provider
fixtures. `link_facts.py codex` passed live UUID/descriptor/metadata/fingerprint
assertions; `link_facts.py claude-seeded` passed live native-session-file/hook/
transcript/fingerprint checks. No repository test suite or package-manifest
exercise was repeated.

Principal private evidence: `evidence/versions.json`, `containment.json`,
`host-namespace-fingerprints.json`; each provider's manual launch, screen/config
snapshots, `keys.jsonl`, `link-facts.json` and stop records; `logs/hooks-*.jsonl`,
`mcp-core.jsonl`, provider request summaries, and `file-watch.jsonl`.

This does not certify production hook ownership admission, actual core MCP
attribution, capture/ACK/projection/daily publication, native input readiness,
real inference/authentication, resume, browser/VM behavior, deployment, or a
complete G0–G6 gate. No owner homes, sessions, credentials, transcripts, browser
profiles or suppressed browser-review material were used.

## Cleanup

Cleanup completed at **23:34:58 UTC**. Native Codex and Claude were stopped with
SIGTERM through pidfds after revalidating their recorded fingerprints, and both
were observed gone. Claude emitted SessionEnd at **23:33:32.134**; Codex emitted
no SessionEnd or Interrupt during this bounded stop. No resume was attempted.

An initial SIGTERM to Codex's recorder left its interactive Bash child alive
after the four-second bound. That failed cleanup attempt is recorded. Cleanup
then sent SIGHUP to the independently saved Bash fingerprint through its pidfd;
Bash, recorder and tmux exited. The other two recorded shells were stopped the
same way. No broad process-name or PID-only kill was used.

All fake servers/watchers were stopped by verified fingerprints. Before namespace
teardown, every recorded native/recorder/tmux/service PID was absent and all
three fixture ports refused connections. The owned unshare launcher was
fingerprint-revalidated and killed through its pidfd; no process remained in
its recorded network namespace. Four leftover sockets, including the first
Claude attempt's socket, were removed only from `$RUN/tmp`, the newly created
private mount. No host `/tmp` socket was touched. Terminal recordings and all
synthetic evidence are retained privately. `cleanup-before-namespace-stop.json`,
`cleanup-final.json`, service and per-provider stop records document these checks.
