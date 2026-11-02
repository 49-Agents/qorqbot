# Agent-browser backend

The prepared browser launcher now uses Vercel `agent-browser` 0.38.1 instead of
Playwright CLI. It attaches to the existing configured CDP endpoint; it does not
provision a replacement Chrome profile. Configuration shape and registered
business-browser ownership checks remain unchanged. The backend's model/chat
features are not exposed: the calling Luna or role agent remains the sole model.

The exact tools/browser lockfile needs Node >=24 and npm >=11, which the package
declares as engines. A runtime built by `tools/browser/build_runtime.py` bundles the
pinned Node 24.21.0/npm 11.19.0 privately (`tools/browser/node/`) and ships the
`run-agent-browser` wrapper; the launcher selects that wrapper and never a host Node
(see BROWSER-CONTROL.md). Older runtime directories without the wrapper are selected
as before. Node 22 is not a supported recipe.
No `agent-browser install` is needed for the existing external Chrome.

The adapter translates the prepared commands to native backend commands. It
removes ambient AGENT_BROWSER/PLAYWRIGHT/AI_GATEWAY settings, supplies an explicit
empty configuration, binds session identity to workspace/target, and disallows
caller-supplied target/lifecycle flags. `detach` maps to backend `close` only in
external CDP mode, which disconnects without closing Chrome. No close-all command
is exposed. Browser control remains a workflow boundary, not an OS sandbox.

`observe` retains budgeted, paginated, immutable evidence using backend snapshots.
`snapshot` supports compact, interactive, selector, depth and delta options. Plain
snapshots may still be large; prefer observe for bounded reads. Ref-based actions
accept eN or @eN. Playwright run-code, old mouse/key subcommands, and request-level
inspection are explicitly unsupported; use ordinary controls or scoped page eval.
Do not load the upstream unrestricted skill as a replacement for the role skill.

Ordubashy commands still require --consumer-id for activity attribution. Native
callers use their own configured browser lease-free, without the queue or action
intents (see BROWSER-COMMAND-RECOVERY.md). Durable workers use the canonical
browser-turn queue: acquire
or read compact state, interact through the pinned command adapter, checkpoint
the tab/step/outcome/next action, then release. A queued durable turn persists a
browser wake condition, releases execution capacity and is resumed by the
dispatcher after release; Astra/model polling is not required. Browser commands
persist an action intent before execution and return structured output; failed
or uncertain actions are never silently marked completed and must be reconciled.
Durable workers cannot use shell, arbitrary HTTP, generic search, OS automation,
or the unrestricted upstream skill. Browser command output is evidence, not a
claim that a form submission succeeded; uncertain outcomes must be checkpointed
and reconciled before retrying.

## Validation and release

Focused queue, command/ambient configuration, and disposable-Chrome checks pass;
no live model provider was used. Existing accepted suites were not rerun as a
release gate. The browser-access integration fixtures require the assembled
delegation runtime/package map and are not evidence of live Luna behavior.
Full source checks are not proof of savings or live readiness.

Fresh local preparation and manual startup are the supported path; this source supplies no automatic host
deployment, upgrade or numbered migration for the browser backend (see [Release worker](release/README.md)).
