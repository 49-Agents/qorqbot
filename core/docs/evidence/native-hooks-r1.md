# Manual native hooks R1

2026-10-08. Implementation against `205ad76d811ff1e6233c568b19b2f813b36b3560`
on `codex/build-native-hooks-r1`, following D39 and its explicit-provider handoff.

`Application.native_hooks` now admits explicitly configured Claude/Codex hooks
through the existing `SessionOwners` service. It checks the exact hook start
fingerprint, walks at most 32 parents to the supplied live shell, matches the
configured native `/proc/<pid>/exe`, and rechecks process/parent/executable facts.
Child metadata and a second matching native ancestor are refused. A wrapper is
not selected as the configured native process. Unsupported or unknown evidence
returns unavailable. The incumbent-wins behavior is unchanged, including a
different process presenting the same native UUID.

Only SessionStart may connect; repeats reconnect to the same owner. Stop records
idle and SessionEnd records unknown activity without releasing ownership.
Attributed `native.hook` events contain bounded identity, event/activity, verified
path/process metadata and a payload hash; no prompt/tool body or credential is
saved. Missing fresh transcript files are permitted; their contents are not
certified. `input_ready` remains false.

`adapters.manual_hooks.prepare_manual_hooks` accepts explicit roots, ManualBinding,
core endpoint/headers, NativePrograms and sink argv. It merges owned hook groups
into workspace `.claude/settings.local.json` and managed HOME `.codex/hooks.json`,
preserves unrelated settings/hooks and MCP files, and detects changed owned
entries. Private bindings are workspace `.qorqut/manual-hooks/<provider>.json`.
Commands append `--binding <absolute-file> --provider claude|codex`; preparation
does not launch a CLI or read global authentication. Existing managed-tree
dirfd/locking/atomic-write machinery supplies the filesystem boundary.

`Terminals.native_context` inspects only an existing current shell and rechecks
the registered target after inspection. The bearer-authenticated internal route
`POST /api/v1/native/hook` derives the private configuration path from the
registered workspace and validated provider, then verifies terminal/recovery/
environment/roots. It shares Dispatcher capacity and cancellation/shutdown
tracking, with no new thread, public operation, MCP tool or schema migration.
HTTP conflict is 409; unavailable is 503. Malformed requests return a fixed 400
diagnostic. Request input is bounded to 1 MiB.

The module client uses explicit headers, a bounded HTTP timeout and 16-KiB
response limit, with no proxy environment, redirects or retries. Only a valid
409 conflict response yields the exact incumbent instruction, printed to stderr
and native additional context. Other failures give a fixed could-not-connect
diagnostic; hook failures exit zero and never signal the CLI.

Checks on final source:

```sh
PYTHONPATH=src:tests /home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/console-test-venv/bin/python -m unittest -v runtime.test_native_hooks
git diff --check
```

**6 tests passed, 4.547 seconds; whitespace check passed.** Grouped checks cover
real disposable Bash/wrapper/native/hook chains, reconnect, both conflict forms,
exit/replacement, child/wrong-ancestry/start-identity rejection, unknown incumbent,
Stop/unknown events, setup preservation/idempotence/conflicting edits, HTTP/client
refusals and redaction, read-only terminal lookup/target change, and cancelled
waiter shutdown drain. An initial client test used an already-consumed mock HTTP
response; correcting its stream fixture resolved that test failure.

The copied Python executables are **synthetic native stand-ins**. ShellViews and
recorder observations in these tests are synthetic; HTTP uses the ASGI boundary
and client responses use a mock transport. No actual Claude/Codex model execution,
native authentication, recorder/native combination, browser, VM, owner session or
operational store was used. Actual supported CLI startup, automatic installation
composition, admitted-source collection and Chat dispatch remain the subsequent
Manager integration gate. The callable requires supplied per-agent credentials;
it does not retrieve a token from core's digest-only credentials map. Hook setup
uses individually atomic files, not a transaction across all configuration files.
This lane neither merges nor deploys.
