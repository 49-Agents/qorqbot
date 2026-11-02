# Real propagation

This document specifies the local propagation pipeline: bootstrap, event-bound tools, the Codex app-server runner and its
capability boundary, and durable attempts. The two input modes in [INPUT_MODES.md](INPUT_MODES.md) change source
interpretation, add handoff notes and shared action histories, and preserve this runner's restrictions. Operational
integration belongs to the host (see [Host integration](HOST_INTEGRATION.md)).

## Outcome and boundaries

Provide a callable pipeline and small local command interface for an owner to
initialize a separate shared context repository, submit one Markdown/plain-text
source addition/edit/removal, run bootstrap and propagation, and retrieve the
resulting published graph, supporting commits, and history. Verify a real model
flow, not just controlled output. Engine Git and context-data Git stay separate.

Preserve CORE_CONTRACTS.md semantics. No UI, live
Qorqut deployment, broader product integration, distribution/submodule choice,
vector implementation, backfilling, verifier, contradiction system, automated
questions or direct source edits by the propagator.
A reusable application tool registry is included; a standalone MCP server and
broader centralized MCP integration are later adapters, not required by this
local runner. Do not build a second storage engine or a generic agent platform.

The runner uses prompts/propagator.md and prompts/bootstrap.md within qorqut_context.

## Components and contracts

Add small modules for bootstrap, event-bound context tools, Codex app-server
transport, and orchestration, plus an argparse __main__.py. Keep model/transport
outside Context; inject a runner for focused deterministic checks. Ordinary
Python callables remain usable independently of the local command interface.
Use the standard library and the installed Codex executable; no registry or
new service deployment is needed.

1. The pipeline accepts the same source submission information as Context.begin.
   Validate model-readable UTF-8 and configurable input limits before starting
   a new submission. Do not alter bytes, strip frontmatter, or normalize source
   text. Edits/removals need the exact base before text as well as the after text.
2. Begin/reuse one durable event. A published receipt triggers recovery and
   returns its original commit without a bootstrap/model run. Preserve stable
   event/source IDs and submission-digest checks. Save failures and expose their
   event ID so an explicit retry does not submit another event.
3. Pin all context reads to the event's base revision. Compute
   required_reassessment from Snapshot.supported_by(source_id) for edit/remove.
   This complete kind/ID list is independent of lexical matching and sampling.
4. Prepare bootstrap, then start the propagator with the authored prompt, source
   change, sample, catalogs or their discovery instructions, required list, and
   existing draft summary on retry. Do not expose runtime paths or credentials.
5. Tool operations stage graph changes through Context. Successful completion
   validates the final graph and creates a draft digest. Publication occurs only
   after both the completion tool and the model turn succeed.
6. Return a structured receipt with event/source IDs, base and published commit,
   result counts, model identity, and local diagnostics location. Errors have a
   useful phase/reason and retain recoverable state. Do not turn every error
   into an owner question; report a failed event that can be retried/discarded.

## Bootstrap mechanics

Normalize text with Unicode case folding and alphanumeric tokenization. Match
existing nodes loosely against both source versions using their summaries and
string attribute values: a shared token of at least three characters or a
nonempty complete string value appearing in the source is a candidate. This
is a permissive candidate filter, not ranking. Deduplicate and order by ID only.
Do not introduce similarity scores, embeddings, relevance, or domain-specific
name fields. Generic shared words may overmatch; the model filters candidates.

Give the source change and all candidates to an Astra/max bootstrap turn using
the bootstrap prompt and an output schema {node_ids: [string]}. Validate that
every returned ID belongs to the supplied candidates; deduplicate. An empty
candidate set needs no model call and produces X=[]; an oversized request fails
explicitly instead of silently omitting candidates. The configured prompt-size
limit keeps this first implementation bounded without inventing a ranker.

Find all neighbors of X across either edge direction. Exclude X itself and
uniformly sample without replacement up to 40 distinct additional nodes Y.
Uniformly sample without replacement up to 60 edges with both endpoints in
X union Y, including eligible self/reverse/parallel edges. There is no special
extra edge allowance for edges inside X. Persist X, Y, selected edges, totals,
and truncation flags so a retry can reuse the same sample. Use an injectable
random source for invariant checks, not a fixed production seed.

Include records with support, X/Y IDs, eligible/returned counts, and a clear
notice that more context exists when omitted. Always include the invitation
"Feel free to explore the context base yourself." Source-supported objects
requiring reassessment are separately identified even if absent from X+Y.

## Event-bound tools

One registry holds schemas, descriptions and dispatch, independent of transport.
Bind it in host code to one Context, event ID and immutable base. Arguments
cannot select arbitrary roots, files, events, another context repository, or revisions
outside that base's source history. Validate arguments and return JSON-safe
structured results/errors. Unknown tools and arguments must fail. No eval,
shell, arbitrary Python import, file-writing, foundation-editing or publish tool.

Provide these operations with clear, bounded descriptions:

- search_context: existing text/attribute retrieval at the published base,
  using its revision-bound index. Mark results as the published view. Preserve
  the existing search contract: limit defaults to 20 and is at most 100 for
  the tool; query one extra hit to report has_more and invite a narrower query.
  Do not claim a total count or add search pagination to the core backend.
- read_object and read_draft: current effective object/support/draft details,
  overlaying staged puts/removals. Newly created IDs are immediately readable.
- list_objects and neighbors: paginate the effective graph, including staged
  changes. Return stable IDs, direction/endpoints, revision and pagination data.
  A node's incident edges must remain discoverable while deletion is staged.
- read_catalog: separate node/edge attribute names and categorical values,
  combining published catalogs with draft values. No numeric enumeration/ranges
  or filtering by basic_entity_type. Allow listing names and reading a chosen
  attribute's values so large catalogs need not be included in every prompt.
- list_sources and read_source: managed source metadata/current text at the
  pinned base, using source IDs. The pending incoming source is already in the
  event payload. Do not read arbitrary working-tree paths.
- read_evidence and object_history: resolve a full source-event commit within
  the pinned history to its event and before/after text; use the existing public
  history methods that resolve self references. Page large text/history results.
- put_node, put_edge, remove_node, remove_edge: the existing complete-record
  operations and explanations/citations/support/developments. Creation allocates
  an ID in Context. Validate basic fields immediately; final cross-object
  validation happens at finish, allowing edges/nodes to be composed in a draft.
- finish_propagation: status complete or blocked, brief summary/reason, and
  reassessed [{kind,id}] list. For complete, require coverage of the exact
  required_reassessment set and validate the current draft without publishing.
  Return its digest and seal mutation tools for this attempt. A duplicate finish
  with unchanged input may return the same receipt. Blocked never publishes.

The reassessment list is a coverage check, not proof of semantic correctness.
No new per-statement evidence schema or verifier is introduced. Preserve existing
current-support validation, including removal and support-only history behavior.

Other collection reads use offset/limit with a default 20 and maximum 100;
include total/has_more/next_offset. Source/evidence text reads page by character offset,
default 16,000 and maximum 64,000 characters, with explicit truncation metadata.
A record too large for a configured tool-response limit fails clearly rather
than returning an apparently complete fragment. Defaults: source 256 KiB,
serialized initial model payload 1 MiB, protocol message 8 MiB, and serialized
tool response 256 KiB. Reject oversized bootstrap payloads without losing source
bytes or claiming the sample is complete. Make limits explicit in runner config.

## Codex runner and capability boundary

Use a child `codex app-server --listen stdio://` process with application-owned
JSON-RPC tools. This is the Codex CLI runner expressed through its supported
programmatic protocol. A separate tmux-based propagator harness is unnecessary;
the owner can run the local command in tmux if desired.

Defaults for both stages: gpt-6-astra, effort max, service tier default. Make
configuration explicit and record it; no automatic model/provider fallback.
Use existing Codex authentication, never copy or log credential files. The
application need not know the account's secret. Do not change global Codex
configuration. Isolate each attempt in an otherwise empty scratch cwd outside
the code and data repositories. Do not mount the business repo into that cwd.

Initialize with experimentalApi=true, acknowledge initialized, start an ephemeral
thread with baseInstructions, developerInstructions and dynamicTools, then
turn/start with model, effort and serviceTier. The bootstrap uses no context
mutation tools and a strict final output schema. The propagator uses the registry.
The installed 0.160.0 schema requires dynamicTools entries with type="function",
name, description and inputSchema. Tool requests arrive as item/tool/call and
receive contentItems [{type:"inputText",text:...}] plus success. Validate thread
and turn identity as well as tool name before dispatch. Do not execute duplicates
of the same call ID twice within an attempt; replay the saved result. Unknown
approval/permission/interaction requests must be denied or fail the attempt,
never accepted automatically.

Disable shell through shell_tool, image view/generation, apps, plugins/remote plugins,
browser/computer use, skills/host skill discovery, memories, hooks, goals,
workspace integrations, and delegation for the runner invocation. Disable web
search, notifications and project instruction loading (project_doc_max_bytes=0).
Use read-only/never permissions as a further filesystem-write restriction. Do
not mistake read-only permissions for filesystem read isolation; the model gets
business content exclusively through the bound application tools. Verify no
model-facing filesystem/shell/external tools are usable. Codex code-mode host
must remain enabled for this installed Astra runtime: its JavaScript wrapper
can compose registered tools, but does not provide a Node/network/filesystem API.
Routine clock/orchestration wrappers are not permission to access outside data.

Important observed behavior: `mcp_servers={}` does NOT clear inherited entries.
Before creating a thread, read effective config without logging its contents,
then disable every configured MCP entry individually (`enabled=false`,
`required=false`) in thread config. Keep plugin/app discovery disabled. Record
only nonsecret capability/model/version metadata. Do not copy hardcoded server
names from any particular installation into the implementation. Fail closed on any configuration
or capability limitation that would expose unrelated tools, including when the
documented boundary cannot be enforced.

Consume streamed notifications until the matching turn/completed. Only status
completed is success; failed/interrupted/EOF/timeout or missing finish are
failures. Drain stderr independently so pipes cannot block; save bounded private
diagnostics. Handle malformed responses, explicit tool errors, and protocol EOF.
Default limits are 300 seconds for bootstrap, 1,200 seconds for propagation,
and 200 application tool calls for one propagation attempt; configurable, with
no automatic fresh model-turn retries. Terminate/reap the child process on
timeout, cancellation, exceptions and normal completion. Never kill unrelated
Codex or tmux processes. Do not leave a timed-out turn running behind a retry.

Official protocol reference: https://learn.chatgpt.com/docs/app-server .
Configuration reference: https://learn.chatgpt.com/docs/config-file/config-reference .
Use installed generated schemas when checking experimental field spelling.

### Global configuration and standard tier

The installed runner includes global AGENTS text as a user-role message even
with project_doc_max_bytes=0. Global owner execution preferences are permitted
trusted runner configuration in this increment, never foundation evidence or
an expansion of the tool scope. Keep the empty scratch cwd, replacement base
instructions, explicit developer instructions, project_doc_max_bytes=0 and all
capability restrictions. Only permit instructionSources resolving to AGENTS.md
or AGENTS.override.md in the runner's actual Codex configuration home, derived
from its existing environment/default. Reject unrelated/project instruction
paths. Record source paths and content digests, not their text, in private run
metadata. Do not claim global instruction loading is disabled; do not modify
global files/configuration or copy credentials into another store.

Both prompts begin with a dedicated-context-task paragraph. It
makes generic coding and agent-launch workflows inapplicable to this task,
forbids delegation, and separates trusted runner configuration from business
evidence. Bootstrap still has no tools; propagation uses only the bound registry.
Read-only is not read isolation, and self-described model tools are not proof
of capability enforcement. The other required capability checks remain.

Accept null or "default" in thread/start's serviceTier only with explicitly
requested standard configuration, features.fast_mode=false, and serviceTier=
"default" on both thread/start and turn/start. Also send serviceTierForTurn=
"default" on turn/start. Reject explicit fast/priority/other returned tiers.
Record requested_service_tier="default" separately from the observed value.
Installed 0.160.0 normalizes standard to null and omits the wire service_tier.
Keep Astra/max and allowProviderModelFallback=false unchanged.

Unrecognized
requested restrictions must fail, but unrelated informational warnings need
not fail if the required effective capabilities are verified. The bundled
bubblewrap fallback remains permitted with read-only/network restrictions
verified; never accept weakened effective permissions.

### Shell capability gate

The registered shell_tool feature controls shell-tool availability. unified_exec
selects the execution implementation; installed Codex 0.160.0 keeps it true even
when false is requested. Accept that observed value only with shell_tool=false
and every other required restriction verified. Omit the ineffective unified_exec
override and record its observed value separately, never as a disabled capability.
Preserve loaded-thread experimentalFeature/list verification, disabled MCP
inventory, all other capability restrictions and the prior instruction/tier
amendment. Raw config echoes alone do not establish the capability boundary.

An installation-boundary negative probe with the same runner configuration, a fresh empty scratch cwd and no business
input inspects the callable registry and shell/file-tool types, attempts harmless exec_command and shell_command calls
within try/catch, and inspects require/process/fetch and delegation bindings. Machine-produced results go to the
probe_report tool with the actual call and error evidence. Shell execution must be unavailable or rejected; unexpected
execution blocks business ingestion. This is one installation check, not a repeated model preflight or a general sandbox.
Focused mechanical validation accepts unified_exec=true with shell_tool=false and rejects shell_tool=true.

## Durable attempts and publication

Take a separate nonblocking propagation lock in the Git common directory for
the entire orchestration attempt, before any model call. Keep Context's existing
per-operation repository/runtime locks and their order unchanged. Two pipeline
processes must not edit the same event concurrently. Do not hold a Context lock
across a model call or introduce recursive locking.

Persist attempt metadata and tool-call/result diagnostics under the separate
runtime, not graph/foundation. Include event ID, base revision, submission and
prompt digests, model/effort, stage, thread/turn IDs, sampled bootstrap, completion
status and error. Keep successful run receipts/diagnostics outside the event
directory that Context cleans, using runtime/runs/<event>/<attempt>/; no global
agent transcript scan. Runtime logs contain business content and stay local;
use private directory/file permissions. No sensitive snippets in committed reports.

Add a narrow Context draft-inspection/validation method reusing publication
validation without advancing Git or touching indexes. Return a digest of the
exact pending input/base and final draft. Add an optional expected-draft digest
to publish, checked under the existing writer lock before publication. This
prevents publication of a draft changed after successful model completion.
Avoid duplicating schema/reference validation in the transport.

Persist a ready record only after successful finish and a completed model turn.
It binds the exact draft digest. If interrupted afterward, explicit retry may
publish that unchanged ready draft without another model call. If no completed
ready record exists, retain the draft and start a new explicitly requested
attempt; provide the existing staged IDs/changes to avoid duplicates. Reuse a
completed bootstrap only when its base/input/prompt/model configuration matches.
Do not resume an arbitrary previous Codex thread or silently reset a draft.

Always inspect Git publication and pending recovery before invoking either
model on retry. If published, finish existing recovery/indexing and return the
same commit. A changed base or draft mismatch is a conflict, not an automatic
base adjustment or repropagation. A blocked/error/incomplete attempt cannot publish an
otherwise structurally valid partial graph. Discard remains an explicit owner
operation on an unpublished event. No internal verifier/approval loop is added.

## Usable local interface

Expose init, submit (add/edit/remove), retry, pending, recover, discard, search,
show-object, history and source-inspection operations through
`python -m qorqut_context`. Require explicit data-repository/runtime paths and
source metadata/submitted_by where appropriate. Submit accepts an input-file
path only in host code, never in model tools. Default runtime can retain the
existing sibling convention. Return JSON receipts/errors suitable for scripts;
report the event ID before a long model run on stderr. No new frontend or
background daemon is necessary. Document real commands using synthetic text.
