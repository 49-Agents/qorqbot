# First connected UI acceptance

Manager acceptance, 2026-10-08. UI candidate
`cbff84d1e92066d13f9ff5322b49a32fc7f0203a`, based on
`2c54899c7e0ea24112aab1e76cb3da9e787fcea9`, passed fresh independent review
and actual compiled-UI/core testing. This accepts the scoped
[first connected interface](../SHELL-IMPLEMENTATION.md), including both review
correction rounds. It does not mark the complete product ready.

Independent ACLA reviewer 141 used Sonnet 5.5 at medium effort under the current
owner policy, with self-review disabled. The reviewer rebuilt exact Git-archive
bytes with locked dependencies and passed all six grouped Chrome checks. No
blocking in-scope defects remained. Desktop, mobile and revised uncertainty
screenshots were inspected. Synthetic checks remain separately attributed in
[builder evidence](ui-shell-r1.md).

Manager inspected mutation/session/recovery/rendering behavior, matched all 40
staged files to the candidate, installed locked dependencies and rebuilt the
same assets. JavaScript SHA-256:
`6c7a54cac5b22f9a1536f3f0c43949d64d4fafe8c311b976943dae8159e42b6e`;
CSS SHA-256:
`05ce6fec2a63ed77f26e7cd0404b8993277409385f4ca37a4c24abe4317ce773`.

Manager then ran installed Chrome against the actual core server at
`faf311d179ba607c65d50157307c1cfde83d2165`, serving that production build from
a disposable installation. All 56 installed core files matched that revision.
There was no API mock or network interception. Both initial and normal-restart
flows passed:

- Console sign-in, HttpOnly/Strict cookie, authenticated page reload and logout.
- Twenty icons, actual registration/provisioning, revision-checked rename and
  icon update with stable agent identity.
- Two identical Unicode messages with distinct request and entry IDs; per-agent
  drafts and an onbash message. Exact text, including literal HTML, was retained.
- Accurate queued/runtime-unavailable state and current publication observation.
- Three immutable entries and both agents' dated JSONL files verified after all
  browser clients closed. Normal server restart preserved full pages and file
  hashes; a fresh browser session rendered the original messages and metadata.
- Desktop/mobile drawers and inspector, no horizontal page overflow, no page
  exceptions or CSP violations, and no token or message body in browser storage.

Manager inspected the 1440×960 desktop and 390×844 mobile screenshots. Private
plan, harness, build/browser logs, screenshots, provenance and results remain
in the run's `manager-ui-core-r1/` directory. Only owned test processes and
temporary installation data were removed; the synthetic token file was deleted.

Two reported low observations follow the intended contract: unresolved prior
ambiguity keeps its original request until success; a user-edited draft remains
whole rather than losing an inferred submitted prefix. Neither behavior
automatically resends. Explicit logout still clears private in-page state.

Native assistant replies and terminal delivery, browser/file cards, noVNC,
VM/browser execution, continuity/export/restore and full release gates remain
outstanding. Nothing was deployed. Reviewer and builder approvals are 142 and
143; Manager owns merge/push under the owner's standing authorization.
