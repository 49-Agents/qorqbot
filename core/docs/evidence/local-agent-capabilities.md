# Local-agent capabilities r1 evidence

2026-10-13. Implements [D63](../LOCAL-AGENT-CAPABILITIES.md) on
`codex/fix-local-agents-9552beea`, fast-forwarded to the Manager's combined
commit `84da98ed98200bf74e49488c0b78429846631e14`. Disposable data roots, no
owner session, profile, credential, database or live service.

## Change

- `files/references.py`: the reserved-onbash check on absolute paths is gone,
  with its import. Absolute paths still require `kind='host'` (a legacy `vm` row
  stays `unsupported_capability`), and every binding recheck, no-follow and
  metadata-only observation, exact replay and publication is unchanged. A path
  inside the *target* agent's own workspace still becomes a workspace-relative
  reference; any other absolute path, including another agent's workspace, is a
  host reference.
- `browser/service.py`: `target()` accepts any host-kind agent instead of only
  the onbash identity; the lifecycle/workspace, agent, recovery-epoch,
  environment, boot and service identity checks, replacement and control
  semantics are untouched. Error copy now says "prepared local agent". Nothing is
  borrowed from onbash.
- Tests changed in the existing files only: obsolete ordinary-host refusals
  updated (`"/"` is now `invalid_argument`; the vm refusal uses a migrated-style
  `vm` row made in the disposable database), plus one new reference test and one
  new browser class.

## Checks

Shared test venv, `PYTHONPATH=src:tests`, one run of each affected suite.

| Suite | Result |
| --- | --- |
| `files` (12) | 11 pass; `test_saved_entry_publishes_after_restart…` fails exactly as on the base commit (transcript line shape), unrelated |
| `browser` (13, includes the new class) | OK |

- Absolute references for ordinary agents: outside file -> `host`; own
  workspace (with `//` and `./`) -> `workspace` `files/report.txt`; another
  agent's workspace -> `host` under the target agent. Conversation entries,
  events, attribution and each agent's dated transcript carry only that agent's
  references. Exact replay after the source files are deleted and the core is
  reopened returns the saved results without reopening any source.
- Legacy `vm` targets stay refused without observation. Existing invalid-path,
  no-follow, special-file and metadata-only tests are unchanged.
- `LocalAgentPeers` uses two **simulated** HTTP browser peers (the deterministic
  test peer, not Chrome, VNC or a provider): no surface exists for onbash or an
  unattached agent; a peer presenting the other agent's identity is refused with
  no attachment left behind; two distinct surfaces and attachments; concurrent
  requests produce one card each in the right conversation and dated transcript;
  each peer receives control posts for its own agent only; completion and
  cancellation notify only their own agent, with independent notes.

## Limits

No actual Chrome, VNC or provider flow was run. Creation still starts no browser
service; an agent without an attachment stays unavailable.
