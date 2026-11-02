# qorqut-context

[INPUT_MODES.md](INPUT_MODES.md) defines the implemented context-only
increment: GENERAL sources file ordinary state and prepare one main-agent
handoff note; controlled OPERATIONAL_ACTION submissions file accepted action
effects and shared Action history, with optional object timelines. The handoff
note is operational only. Format 3 adds atomic **notes** (separate Context records
for reported happenings, intentions and qualified information), controlled
**supervised edits** (a preserved supporting document plus explicit node/edge/note
operations, no model) and add-only **question output** (runtime material outside
the Context, delivered only after publication). Shared context adds immutable revision tags and history without a graph subject.
Operational queue and transport integration are host-owned; see
[host integration](HOST_INTEGRATION.md). The new notes, supervised-edit and
external-question workflow still requires its corresponding host adapters.

The local core of Qorqut's shared context backend. One Git repository contains
original foundation files, current graph objects, immutable events and shared
vocabulary catalogs across businesses and global activity. This source-code
repository holds the engine; working data belongs outside it.

The engine exposes ordinary Python operations and a local source-to-graph
pipeline: lexical/bootstrap model selection, event-bound graph tools, an
ephemeral Codex app-server propagator, validated atomic Git publication,
histories, catalogs, SQLite retrieval, and durable explicit retry. The model
interprets source meaning; structural checks do not verify factual correctness.
The host composes this library with its operational queue, HTTP/MCP and UI consumers. Those adapters live outside this library. A library-owned standalone MCP adapter and vector search remain outside this increment.

Read [CORE_CONTRACTS.md](CORE_CONTRACTS.md) for exact records and operation
semantics, [INPUT_MODES.md](INPUT_MODES.md) for the two input modes and shared
action histories, [PROPAGATION.md](PROPAGATION.md) for the model pipeline and its
capability boundary, and [HOST_INTEGRATION.md](HOST_INTEGRATION.md) for the host boundary.

## Local use

Run from this repository with Python and Git available. Real propagation also
requires an authenticated installed Codex app-server. The backend uses the
standard library, Linux file locking, and SQLite with FTS5; development checks
use Python 3.12. The library is packaged as `qorqut-context==0.1.0` (`qorqut-context[integration]==0.1.0` adds the Qorqut Operations integration, which needs `qorqut-operations`; see [INTEGRATION.md](INTEGRATION.md)); the host builds it from an exact selected Git commit and composes it through the host. A public package-registry distribution is not implied.

This example uses disposable directories:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from qorqut_context import Context

with TemporaryDirectory() as temporary:
    context = Context.initialize(Path(temporary) / "shared-context")
    submission = context.begin(
        "add", "foundation/meeting.md", content=b"Jesse is a person.",
        metadata={"kind": "meeting", "title": "People", "origin": "example"},
        submitted_by="example-owner",
    )
    node_id = context.put_node(
        submission.event_id,
        {"summary": "Jesse is a person.",
         "attributes": {"basic_entity_type": ["person"], "name": ["Jesse"]}},
        explanation="The source identifies Jesse as a person.",
        citations=["self"], support_after=["self"],
    )
    context.stage_handoff_note(submission.event_id, "")  # Explicitly nothing to hand off.
    commit = context.publish(submission.event_id)
    node = context.snapshot().read("node", node_id)
    history = context.snapshot().history(node_id)
    hits = context.search("Jesse", kind="node")
```

`begin` always creates GENERAL input. Text/frontmatter/metadata cannot select
action mode. A claim such as "we hired Jesse" or a request to build a landing
page belongs in the consolidated note; it cannot establish graph effects,
action developments, or deliverable/task entities. Independent ordinary state
in that source remains eligible. Retrieved evidence keeps its original mode,
including when encountered by an action turn. These are model/caller semantic
responsibilities; structural validation is not a semantic verifier.

`begin` also accepts optional trusted-caller `record_context={"scope": ..., "provenance":
{...}}` (global or one business, plus JSON attribution) and `expected_revision=<full
published commit>`. The context is recorded in the event beside, never inside, the
unchanged source bytes; the expectation must be the published head when a new event
is staged. See [CORE_CONTRACTS.md](CORE_CONTRACTS.md), "Controlled record context and
expected base". `begin_action`/`submit_action` take `record_context` too and keep it in
the canonical Action envelope; neither grants permission or ownership.

`Pipeline.submit_edit(plan, *, submitted_by, event_id, record_context=None)` is a
trusted-caller library entry (no CLI command or model tool) that executes a fixed
`qorqut-graph-edit-v1` plan of explicit `put_node`/`put_edge`/`remove_node`/`remove_edge`
operations as one controlled GENERAL event, with no model: it needs the published head as
`expected_revision`, caller-supplied IDs and no cascade, publishes the plan as the add-only
source `foundation/edits/{event_id}.txt` with an execution marker, and returns the ordinary
receipt plus that `execution` marker and `model: null`. The reserved prefix is refused by
`begin`/`submit`. See [CORE_CONTRACTS.md](CORE_CONTRACTS.md), "Controlled deterministic
graph edits", for the plan and receipt schema and the trusted-caller boundary.

Notes are three-field records `{id, text, objects}` stored at `notes/<id>.json`
beside, never inside, the graph: `objects` links zero or more nodes or edges that
are published (including since removed) or staged in the same draft. They are staged
with `Context.put_note(event_id, record, explanation=, citations=, support_after=)`
and `remove_note(event_id, id, explanation=, citations=)`, have the same evidence,
history, search, snapshot and reassessment behavior as other records, and are read
with `snapshot.notes`, `read("note", id)`, `history(id, kind="note")` and
`linked_notes(kind, id)`. See [CORE_CONTRACTS.md](CORE_CONTRACTS.md), "Notes".

`Pipeline.submit_supervised(plan, *, content, submitted_by, event_id, record_context=None)`
(preview first with `Context.preview_supervised(...)`, which changes nothing) is the
trusted-caller entry for manually authored batches: a `qorqut-supervised-edit-v1` plan of
`put_`/`remove_` `node`, `edge` and `note` operations plus the supporting Markdown
`content`, which is preserved byte for byte as `foundation/supervised-edits/{event_id}.md`.
It publishes one ordinary event with node/edge/note changes and `model: null`, never calls a
model, and never rebases a stale plan. See "Supervised edits".

Automatic filing may call the add-only `emit_question(text, related=[])` tool.
`Pipeline.question_output(event_id)` returns `{event_id, published_commit, questions}` for a
published event and `Pipeline.acknowledge_question_output(event_id, published_commit)`
removes the delivered payload. Questions never enter Context records or events. See
"Question output" in [CORE_CONTRACTS.md](CORE_CONTRACTS.md); the external question
collection is not part of this engine.

`begin` returns a stable event/source receipt. If a repeated submission has
already published, its `published_commit` is set: reuse that result rather than
run propagation again. Edit/removal retains source identity. Graph changes are
staged through `put_node`, `put_edge`, `put_note`, `remove_node`, `remove_edge`, and
`remove_note`; callers provide explanations and the complete desired supporting-commit set.

`snapshot(revision)` pins a published Git revision. `read`, `event`, `history`,
`neighbors`, `supported_by`, `linked_notes`, and `source_change` inspect its data.
`Snapshot.notes` maps note IDs to records. `supported_by`
finds current nodes, edges and notes to reassess after a source changes, including associations
created by support-only updates. `handoff_notes`, `handoff_note`, `action`,
`actions_history`, and `action_timeline` read committed mode outputs at that
same pinned revision.
Raw `Snapshot.events` holds
stored immutable records, which can contain internal `self` references; public
`event`/`history`/`source_change` reads resolve them to full commit IDs.

`pending()` inspects durable unfinished work. After interruption, `recover()`
completes an already published event or reports `needs_retry` for an unpublished
draft. An explicit `publish(event_id)` retries that saved draft. `discard` only
discards unpublished work; `recover()` finishes an interrupted discard without
retrying the source. A changed publication base or external checkout edits
are reported instead of overwritten. Every new write is format 3. A format-2 history
(published before notes) is read as it is: its sources, graph records, support
citations and history keep working unchanged, and nothing has to be refiled or
backfilled; notes and questions arise from future filings. The first publication
on a format-2 head writes `graph/config.json` as format 3 in the same atomic commit as
that event. Earlier commits and events remain unchanged, and reads,
previews and abandoned or invalid drafts upgrade nothing. After that first format-3
write the older engine can no longer read the head, so complete any pending work
with the older engine before switching. Pending jobs and undelivered question
payloads are format 3 only: an unfinished format-2 job is refused with that
instruction, never converted, discarded or replayed. Any other repository
configuration (an unsupported or non-integer version, or extra keys), a published
event whose format differs from its publishing commit's configuration or that lacks a
supported input mode (`GENERAL` or `OPERATIONAL_ACTION`), notes or supervised edits in a
format-2 history, and a pending job of another format are rejected with a clear error;
nothing is read as history, converted or changed.
Recovery also recognizes exact published
files left by a partial checkout and reconciles their base index entries under
Git's index lock, preserving unrelated staged work. Initialization uses the same
non-destructive checkout; conflicting files and existing Git locks are retained.
The repository/runtime directories must remain separate. Each local repository
binds to one resolved runtime path and shares a
writer lock in Git's common directory; a different runtime path is a conflict.
The runtime also checks its owning root. These local ownership markers stay
outside published commits; corrupt or conflicting markers require explicit
resolution, with no automatic rebinding or repair.

## Source-to-graph command interface

Use a separate empty data directory and a separate runtime. For example, with
synthetic text saved in `/tmp/context-note.md`:

```sh
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime init
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime submit add foundation/note.md --input-file /tmp/context-note.md --kind note --title "Project note" --origin local-example --submitted-by owner
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime search "project" --kind node
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime pending
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime notes --object-kind node --object-id <node-id>
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime search "coffee" --kind note
```

The runtime defaults to the repository's sibling `<repo>.runtime` if omitted.
Commands print JSON results/errors to stdout. Submit/retry prints the stable
event ID to stderr before a model run. Retain that ID after failure:

```sh
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime retry <event-id>
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime recover
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime discard <event-id>
```

`recover` performs only mechanical publication/index recovery. `retry` first
checks Git, then an exact completed ready draft, before starting a fresh model
attempt. A partial, blocked, or incomplete turn cannot publish. Failed input and
staged objects remain available for explicit retry or discard; there are no
automatic new model attempts. The lower-level `Context.publish` remains a
controlled-caller API and does not itself require a model completion.

Use `submit edit` with the same foundation path and a new input file, or
`submit remove` without an input file. Both require `--submitted-by`; omitted
metadata inherits the existing source metadata. Full supplied metadata requires
kind/title/origin and can include origin-ref/created-at. Source bytes, including
frontmatter and line endings, are preserved. Sources must be UTF-8 Markdown/text.

`show-object node <id>` (or `note <id>`), `history <id> --kind node` (or `note`), and
`source --commit <full-commit> --version before` inspect graph, notes and evidence.
These and `search` accept `--revision` for an old published snapshot. Source
inspection also accepts `--id <source-id>` for current text; source text and
history have explicit offset/limit pagination.

Supervised edits take explicit files and never call a model. Preview changes nothing and
prints the normalized plan, the document digest and a `submission_digest`; apply submits the
files you name with the identity you name. The approval boundary is yours: a preview and its
`submission_digest` are informative and never an authorization, and the engine cannot establish
approval or tell whether the files changed between the preview and the apply. Review the exact
files, then submit them. These commands and the question-output commands require an explicit
`--runtime`; omitting it is refused before any Context or file is touched. Legacy commands keep
their default runtime.

```sh
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime preview-supervised --plan-file plan.json --document-file correction.md --submitted-by owner --event-id <event-id>
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime submit-supervised --plan-file plan.json --document-file correction.md --submitted-by owner --event-id <event-id>
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime question-output <event-id>
python -m qorqut_context --repo /tmp/acme-context --runtime /tmp/acme-runtime ack-question-output <event-id> <published-commit>
```

The callable equivalent is `Pipeline(context).submit(...)` or
`Pipeline(context).retry(event_id)`. `EventTools(context, event_id)` exposes
transport-independent schemas and dispatch. `Pipeline` accepts an injected
runner for focused deterministic checks; production uses `AppServerRunner`.

The default model is `gpt-6-astra`, effort `max`, standard service tier, with
existing Codex authentication. Submit/retry accepts explicit `--model`,
`--effort`, and `--codex`; there is no automatic provider/model fallback.
The runner uses ephemeral threads in an empty scratch directory, application
tools, disabled inherited MCP entries and native external capabilities, and
read-only/never permissions. Read-only alone does not isolate filesystem reads.
Code-mode host stays enabled to compose registered tools without Node, network,
or filesystem APIs. Configuration/capability failures stop the attempt.
The loaded thread's registered feature state must report `shell_tool=false`.
`unified_exec` selects the shell execution implementation and may remain true;
its observed value is recorded separately and is not a shell-capability gate.
Global owner AGENTS preferences from the actual Codex configuration home remain
trusted execution configuration; their paths/digests are recorded, and they
cannot become business evidence or expand tool access. Project instructions
are disabled. Standard-tier requests may be reported as null by Codex 0.160.0;
the receipt metadata records requested and observed tiers separately.

`RunnerConfig` and matching submit/retry flags expose these defaults:

| Limit | Default |
| --- | --- |
| Source bytes, per version | 256 KiB |
| Initial serialized model payload | 1 MiB |
| Protocol message | 8 MiB |
| Tool response | 256 KiB |
| Bootstrap / propagation timeout | 300 / 1,200 seconds |
| Application calls per attempt | 200 |
| Private protocol diagnostics / stderr | 16 MiB / 64 KiB |

For example, `--source-bytes`, `--payload-bytes`, `--bootstrap-timeout`, and
`--propagation-timeout` override limits. Oversized bootstrap requests fail
without silently dropping candidates. A response that is too large requests a
smaller page rather than returning an apparently complete fragment.

Every actual model call also leaves a private usage receipt under
`<runtime>/runs/<event>/<attempt>/calls/`, written before the call starts and settled afterwards;
`Pipeline.usage_receipts(event_id, limit=100, cursor=None)` reads them page by page without
mutating anything (observed token counters and cost are `null` when unknown, never zero; see
`CORE_CONTRACTS.md`, "Model-call usage receipts").

Receipts and private model/tool diagnostics remain under
`<runtime>/runs/<event>/<attempt>/` after the transient draft is cleaned. They
contain source/graph data: keep runtime and data repositories outside engine
Git. Never copy credential files into either location. The pipeline takes a
separate nonblocking propagation lock for its entire attempt; core operations
retain their existing short-lived locks.

## Accepted actions and handoff inspection

The local action adapter is a controlled-caller boundary, not an authentication
or authorization service. A future owning adapter authorizes the recorder and
supplies canonical accepted text and immutable operational-revision `tags`: a
list of `{id, name, business_id}` snapshots, where `business_id` may be null.
Tags default to `[]`. No graph entity is required, and tags never create nodes.

```python
from qorqut_context import Pipeline

receipt = Pipeline(context).submit_action(
    "add", "upstream:hiring:17", text="Northstar Studio hired Jesse on 2026-10-07.",
    revision_ref="accepted-revision-a",
    tags=[{"id": "tag-northstar", "name": "Northstar", "business_id": "business-northstar"}],
    related_objects=[{"kind": "node", "id": person_id}], submitted_by="accepted-recorder",
)
snapshot = context.snapshot(receipt["published_commit"])
current = snapshot.action("upstream:hiring:17")
audit = snapshot.action("upstream:hiring:17", audit=True, offset=0, limit=20)
timeline = snapshot.action_timeline("node", person_id, audit=True)
```

`Context.begin_action` accepts the same action inputs without a model. Stage the
complete graph change and `stage_action_associations(event_id, date, summary,
associations)`, plus `stage_handoff_note(event_id, text)` before publication.
Use an empty note when there is nothing operational to hand off.
Each association has `kind`, `id`, and an
explanation; the host cites `self`. Summary and each explanation have a 150-word
limit. Add/edit permits an explicit empty list when no suitable subject exists.
Supplied targets must be known historical or draft objects. History-only links
need no graph put or artificial support. Removal requires an empty association set,
the prior date, and a withdrawal explanation.

Action and optional revision references are opaque, nonempty, at most 256 UTF-8
bytes. They are never paths or evidence citations. Optional pointers have
`kind` (`node`/`edge`), a five-character `id`, and optional `explanation`.
Unknown IDs are unresolved suggestions; the model chooses the final associations.
The deterministic JSON text envelope round-trips exact accepted text and stores
pointers/revision/tags under `foundation/actions/<source-id>.txt`. General entry
points cannot write there. Text and the serialized envelope each obey the
configured source limit. Real evidence and diagnostics stay outside engine Git.

Edit/remove preserves the action/source identity. `expected_previous_commit`
optionally checks the last action ingestion; revision references have no implied
ordering. Tag-only revisions change the canonical source bytes and can be retried
exactly. Removal supplies no text or pointers; its tag snapshot is still recorded
in the immutable event and bound by the submission digest. Re-adding a withdrawn
reference is a conflict. Exact `event_id` replay returns the original publication even
after later edits or withdrawal. Action correction reassesses both current
source support and every previous association, including removed objects.
Current timelines show the latest still-associated revision; audit timelines
retain corrections/withdrawals after a subject was first associated. Graph
maintenance history stays separately available through `history`.

`Context.actions_history(*, tag_id=None, global_only=False, audit=False,
offset=0, limit=20, revision=None)` and `Snapshot.actions_history(...)` list
published Actions independently of object histories, in publication order.
Current reads return the latest revision of each non-withdrawn Action; audit
reads return all revisions including withdrawals. Filters combine and use each
selected revision's immutable tag snapshot: `tag_id` matches the ID, while
`global_only` requires no linked business tag (ordinary tags are allowed).
The collection page includes source IDs, full commits and recording times.
The `actions_history` event tool reads the pinned published base, excluding drafts.

```sh
python -m qorqut_context --repo /tmp/acme-context submit-action add hiring-17 --input-file /tmp/accepted-action.txt --tags '[{"id":"tag-northstar","name":"Northstar","business_id":"business-northstar"}]' --submitted-by accepted-recorder
python -m qorqut_context --repo /tmp/acme-context action hiring-17 --audit
python -m qorqut_context --repo /tmp/acme-context actions-history --tag-id tag-northstar --audit
python -m qorqut_context --repo /tmp/acme-context actions-history --global-only
python -m qorqut_context --repo /tmp/acme-context action-timeline node def34 --audit
python -m qorqut_context --repo /tmp/acme-context handoff-notes --include-empty
python -m qorqut_context --repo /tmp/acme-context handoff-note <event-id> --offset 0 --limit 16000
```

Replace example IDs with actual IDs from that context. All inspection commands
accept `--revision`; collection pages default to 20, maximum 100. Note text uses
character pages (default 16,000, maximum 64,000). `handoff_notes` lists metadata
without note text; `handoff_note` reads the selected event's note. Empty
`stage_handoff_note` text explicitly records nothing to hand off; repeated
staging replaces the one draft. Publication includes it atomically with its
source event and graph changes. Its identity is that event ID within the selected
context. A prepared note is never sent or marked delivered. There is no delivery
state, consumer cursor, operational database, task creation, or message adapter.

The handoff note is the operational hand-off for the eventual main agent: requested
or proposed work and claims of completed operational changes it may need to verify or
act on (pointing at the relevant note IDs), and the reason for a blocked finish. It
never carries questions. Uncertain identities, relationships, attributes, dates and
interpretations are kept as qualified Context **notes** (reported actions and requested
work are attributed notes, never attributes or Actions), and what the owner could resolve
is filed with `emit_question`. An action-mode note does not ask for routine reapproval
of accepted facts. Unknown details can remain unknown while supported work completes;
ambiguity must not become an invented graph fact. A blocked model finish requires a
nonempty note, retained in `pending()` / `inspect_draft` without publishing or
delivering it. Every publication requires an explicit note disposition.

## Verification

Focused tests use temporary repositories, injected model outputs and a synthetic
app-server process. They do not call a model or touch live business data. Run
the relevant named modules/cases, such as
`python -m unittest tests.test_shared_actions`, rather than broad suites. Bounded real-model acceptance is separate; its private sources,
graph, receipts and observations stay outside this repository. Passing structural
checks does not establish the factual accuracy of model interpretations.

The search interface exposes a searchable-record projection and change/removal/
rebuild inputs. A future embedding index can use those inputs and the same
record identities without changing source history or graph records. The initial
SQLite implementation does not generate embeddings.
