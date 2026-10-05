# M04 — Native CLI adapters and preparation

Status: Planned. Owns R05 and native-format parts of R08/R10/R11. Depends on M01/M03; supplies source definitions to M07 and resume/bootstrap behavior to M08.

Repository: `qorqut-core`. Preparation installs separate `qorqut-core` and `qorqut-browser` MCP registrations with bound actor/target configuration. Browser owns its tool implementations and server; core never registers duplicate browser tools.

## Outcome and boundary

Support Claude Code and Codex as interchangeable executors of one logical agent. The CLI owns model/provider authentication and execution. The platform owns agent identity, shared files, coordination and captured history. The model name is configuration/observation, never the agent ID.

Keep two concrete adapters behind the smallest interface their differences justify. Do not build a plugin catalog or import a model SDK runner. Arbitrary command launch can exist as `terminal_only`, but it must advertise its reduced native-history/restoration capability honestly.

## Adapter contract

An adapter provides:

- Compatible CLI version/capability probe without starting a model turn.
- Agent-local preparation of CLI config, separate core/browser MCP endpoints and identities, instructions and hooks.
- Structured launch/resume argv and supported startup-context method.
- Native process/session identification and composer readiness classification.
- Lifecycle/activity hook translation with durable local event submission.
- Exact native parent/child source discovery in this agent's managed HOME, attachment rules and parser selection.
- Stop/finalization/resume checks and known source rewrite/compaction behavior.

Capabilities include verified native transcript capture, child-source coverage, native resume, bootstrap delivery and composer support. Unknown versions can be refused for dependent automated actions or explicitly run with reduced capability; they are never silently labeled fully supported.

## Preparation and ordinary tools

Prepare both adapters in the same agent workspace without changing identity. Generated entrypoints refer to current identity, authored `persona.md`, memory, MCP usage and history retrieval. Preserve authored persona/notes/files on every refresh. Do not create a second authored instruction file. Store generated version/hash metadata separately and install atomically.

Generated instructions describe user-facing intent: call `browser_request_input` when the user must sign in, enter data or act in the browser; call `files_reference` with a path when the user should see/review/be notified about a file. Explain waiting/completion and saved-reference outcomes, not iframe/modal/toast/layout mechanics. The backend resolves agent/conversation/surface bindings. Preserve these instructions consistently across both providers without parsing provider-specific tool prose to trigger UI effects.

Use a dedicated native HOME per agent. Keep each CLI's ordinary tools available; do not strip its shell, file, browser or search facilities to manufacture a new permission layer. Add core MCP and the independent browser MCP endpoint locally. The managed browser helper runs behind browser's service-owned command gate. Do not overwrite the owner's personal global CLI settings or copy personal transcripts.

Native authentication is an onboarding concern. A missing login/trust step is shown through terminal setup state; it is not silently bypassed or treated as platform permission approval. Authentication/browser profiles persist in native HOME/state disk. Any configured credential seeding is an explicit installation input with provenance, never a filesystem-wide credential scan.

Managed `claude`/`codex` launch entrypoints invoke the recorder. The guarantee covers managed, cooperative sessions. An unrestricted shell can deliberately bypass or damage that path; the system must not claim adversarial tamper-proof recording.

## Implementation sequence

1. **M04.01 P0 matrix:** inventory exact native binary versions and observable hook/transcript formats. Use isolated local fake-provider fixtures to test actual CLI transport behavior without owner accounts.
2. **M04.02 Common preparation:** generate small identity/instruction/MCP files and local registration bindings; add compatibility diagnostics and atomic refresh.
3. **M04.03 Claude:** implement launch/resume, process/composer binding, hooks and all owned source/attachment discovery. Verify native child sessions, cwd changes and compaction.
4. **M04.04 Codex:** implement equivalent behavior including supported rollout filename variants, native item IDs, resumed sessions and child-source associations.
5. **M04.05 Capability enforcement:** expose per-run capability/version evidence. Unsupported raw formats remain archived and visibly unparsed; unsupported capture contract blocks autonomous “complete capture” readiness.
6. **M04.06 Provider/model changes:** accept CLI-specific model/provider config without a platform model catalog. Record observed changes in history and coordinate restart/resume through M08 where required.

## Source decision

P06/P11: adapt Qorqut's dual-client generated files, hook payload knowledge, process ancestry checks, composer captures and native parsing fixtures. Do not copy the large role/business bootstrap function, single current transcript pointer, strict original-cwd ownership check, or only-parent/text-only record filtering.

## Tests and acceptance

- Both clients prepare against the same agent UUID/folder; Claude → Codex → Claude refresh preserves authored files and identity.
- Hooks bind exact run/native sources and cannot claim another agent through a caller-supplied cwd.
- Test cwd changes, suffixed rollout names, resumed IDs, tool/user echo distinctions, repeated equal-text messages, child sessions, unknown native types and attachments.
- Verify both generated MCP registrations with each actual pinned CLI, not merely TOML/JSON parsing: browser tools appear on browser MCP only, and a browser outage does not erase core discovery. Native login fixtures and actual provider acceptance are separately labeled.
- Generated tool schemas/instructions expose action intent and required paths/prompts without UI mechanics; native tool replay cannot create another request/reference.
- Model/provider config changes never reset agent history. Unknown model labels are passed according to the adapter configuration contract rather than rejected by a stale platform list.
- Supported versions have real native interaction/recorder evidence, including resize/signals and clean/abrupt termination.

**Done:** the compatibility matrix names exact tested versions and capabilities. Supported adapters pass M03/M07/M08 contracts; “model-agnostic” does not conceal unimplemented source or resume support.
