# How the Qorqut context base works today

Research snapshot, 2026-11-16, at commit `c67543ca`. This maps the system as the code has it, to support the
discussion of why agents make the owner repeat who is who and what is what. The diagnosis is in
[03-REPORT.md](03-REPORT.md); the outside comparison is in [02-EXTERNAL-RESEARCH.md](02-EXTERNAL-RESEARCH.md).

Evidence tags: **[C]** read in code, **[D]** read in docs only (may be stale), **[I]** inference. The claims marked
"re-checked" were read a second time directly, after the exploring agents reported them. Nothing here was observed
on a live installation: no real graph content, queue state or transcripts were read.

Path abbreviations:

- `CTX` = `qorqut-code/qorqut-context/qorqut_context`
- `INT` = `CTX/integration`
- `APP` = `qorqut-code/app/qorqut`
- `AG` = `qorqut-agentic/src/qorqut_agentic/resources/defaults`

## 1. The stores

There are four places where knowledge can live. Only the first is "the context base".

| Store | What it holds | Who writes | How it reaches an agent |
|---|---|---|---|
| **Shared Context graph** | Nodes, edges, notes, sources, events. One Git repo per installation. | A single serial filing queue (LLM propagator), plus explicit `context_edit`. | 6,000-character "Context Home" index at session start; everything else by tool call. |
| **Working memory** | One private handoff per agent per scope, 4,000 characters at most. | The agent itself (`working_memory_update`), or the owner in the dashboard. | Injected at session start, resume, clear, compact, fork. |
| **Project directory** | Per project: name, ID, stage, aliases, one-liner (300 characters). | Project settings. | Always injected at session start. |
| **Older item stores** | Documents, Materials, People documents, notes (`context_list/get/search`, `context_items_list`, `context_people_*`, `notes_*`). | Various explicit tools. | Tool call only. Not part of the graph. |

Chat transcripts are archived but agents have no tool to search them. Only the learnings pipeline reads them [C].
`owner_conversation_get` pages the private owner thread, 100 messages at most per call [C].

`docs/CONTEXT-RELATIVITY.md` describes a retired engine (people, topics, filer, Story) and is stale;
`docs/v1.2/CONTEXT_COMPOSITION.md:26` records its removal [D].

## 2. Data model of the graph

All [C], from `CTX/schema.py` and `qorqut-context/CORE_CONTRACTS.md`.

| Unit | Shape | Location | Notes |
|---|---|---|---|
| Node | `{id, summary, attributes}` | `graph/nodes/<id>.json` | Attributes are flat lists. `basic_entity_type` is required. |
| Edge | node fields plus `from_id`, `to_id` | `graph/edges/<id>.json` | Directed, free-form, no mandatory relationship type. |
| Note | `{id, text, objects}` | `notes/<id>.json` | Not a graph object. Linked to nodes or edges for relevance only. |
| Source | raw submitted bytes | `foundation/**` | Immutable evidence. 256 KiB limit per source. |
| Event | one per publication | `graph/events/<id>.json` | Each change carries explanation, citations, `support_after`, developments. |
| Catalogs | attribute names and values in use | `graph/catalogs/` | Suggestions, not closed sets. |

- IDs are random five-character strings in one namespace, never reused.
- There is one graph for the whole installation. Business or Global scope exists only as event metadata
  (`record_context`), so reads return the same content whoever asks (`INT/reading/operations.py`,
  `INT/reading/text.py:5-7`).
- There is no entity-type schema and no alias machinery. A grep for "alias" over the engine's Python returns
  nothing (re-checked). "Aliases" is only an attribute name the filing prompt suggests.
- The owner has one canonical person node, seeded blank, pointed to by `personal_context_node`
  (`CTX/personal.py`). Only Ordubashy edits it.

## 3. Storing: how information gets in

| Path | Automatic? |
|---|---|
| `context_upload` (raw text) | No. An agent or the owner must call it. |
| `notes_upload`, `journal_upload_to_filer` | No. |
| `context_instruct` (focused, attributed correction) | No. |
| Accepted learning (owner accepts a Learning in the context bucket) | Automatic once the owner accepts. |
| Business creation (founding brief) | Automatic [D]. |
| Accepted Actions (`actions_record`, 20 words at most) | Agents are instructed to record completed work. |
| `context_edit` (deterministic or owner-supervised graph edit) | No. |
| Answers to Context questions | No answer operation exists. Needs a manual supervised edit [D]. |
| **Ordinary chat** | **No filing path.** See below. |

What happens to something the owner says in chat (re-checked):

- The only hook is a reminder injected on every prompt to Ordubashy
  (`AG/operations/runtime-instructions/messages/operational-intake.md`). It asks for two things: requests,
  decisions and commitments go on their work item, and "a clear new personal fact about the user goes to the
  canonical personal context node".
- Facts about the business, its people, customers, suppliers or terms are not covered by that reminder. They
  reach the graph only if an agent decides to call `context_upload` or `context_instruct`.
- Only two roles hold those grants: Ordubashy and Learning Applier (`role.yaml:147-148`, `role.yaml:41-42`).
  Every other agent must relay through the inbox.
- The tool guidance steers `context_instruct` towards "concrete, source-backed corrections", not broad
  extraction.

## 4. Filing: the write pipeline

```
context_upload | context_instruct | notes_upload | accepted learning | actions_record
  -> one SQLite transaction: frozen source + filing row + job + queue input      (INT/filing/admission.py)
  -> single global ordered queue, one head
  -> ContextQueueDriver (polls every 2 s; needs a configured coordinator)        (INT/driver.py)
       -> bootstrap: lexical candidate nodes -> LLM picks node_ids
                     -> sample of neighbours, edges, 40 notes                     (CTX/bootstrap.py)
       -> propagator LLM turn with tools: search_context, read_object,
          put_node / put_edge / put_note, emit_question, stage_handoff_note      (prompts/propagator.md)
       -> validate, seal, atomic Git commit, advance ref
  -> published or blocked; handoff note -> coordinator inbox; questions -> host question store
```

- **An LLM decides where every fact goes.** Nothing in code routes facts. The prompts say stable states become
  node attributes and edges; reported happenings, intentions and uncertainty become notes; operational claims go
  to the handoff note.
- **Cost and speed** (re-checked, `runner.py`): default model `gpt-6-astra` at `max` effort, timeouts of 300 s
  for bootstrap and 1,200 s for propagation, up to 200 tool calls, no automatic retry. Inputs are processed
  strictly one at a time. Expect minutes per input [I].
- **Failure:** a failed attempt blocks the head of the queue, and everything behind it waits for an explicit
  `context_queue_retry` or discard. Nothing is re-filed automatically.
- **Dedupe** is by exact source digest only. The same fact arriving in two sources is caught only if the
  propagator happens to find the existing record.

## 5. Linking and entity resolution

- Candidate nodes for a new source are found by token overlap (words of three or more characters) or a full
  attribute string appearing in the source (`CTX/bootstrap.py:20-34`, re-checked).
- The propagator prompt then says to search names and aliases and reuse an existing ID when the evidence
  identifies the same entity. Whether "Aziz" and "our CFO" land on one node is the model's judgment over lexical
  search. If the two share no token, nothing surfaces the existing node [I].
- A person becomes a node only if the model judges the mention "a clearly identified person". Otherwise the
  mention stays inside a note or an attribute string [I from the prompt].
- There is no merge operation. Merging is a manual put and remove.

## 6. Developments

- A development is `{date, description, citations}` attached to one node or edge change inside an event.
- Only OPERATIONAL_ACTION events may carry them. GENERAL input and notes are rejected in code
  (`CTX/engine.py:568-571, 608-611`, re-checked). Ordinary conversation is GENERAL, so it can never produce a
  development.
- They are not in the search index. They appear only in an object's history read.
- There is no automatic supersession. A put replaces the current record; the old value stays in Git history.
  Contradictions are a model judgment, and conflicting accounts are kept with attribution plus a question.

## 7. Retrieval: how information gets out

What the agent has without asking, at session start, resume, clear, compact or fork (`APP/agents/context.py`):

1. Static instructions: core rules, foundation, response style, role brief, toolkit and so on.
2. Identity and assignment, the project directory, the selected project.
3. Context Home, rendered by `INT/reading/text.py` (re-checked): 12 nodes at most, most connected first, 240
   characters per summary, three relationships per node, 6,000 characters in total. The owner's personal node is
   shown only as a pointer, and edges touching it are hidden.
4. The working-memory handoff and up to ten attention items. This is dropped silently when the budget is tight.

On a normal turn the hook adds only fixed reminders. Nothing is derived from the text the owner typed
(`APP/bootstrap/claude_session.py:153-158`, re-checked).

Everything else is pull-only, through four tools:

| Tool | Takes | Does |
|---|---|---|
| `context_home` | page cursor | Paged node index. |
| `context_find` | query, kind, attributes | Keyword search. |
| `context_object` | `node:ID` and similar | Reads one record. |
| `context_expand` | ref, depth 1-3 | Neighbourhood traversal. |

`context_find` is SQLite FTS5 with these properties (`CTX/search.py:151-171`, re-checked):

- Every word in the query must prefix-match (`"word"*` joined with AND). A natural-language question usually
  returns nothing.
- Results are ordered by record key, not by relevance.
- No stemming, synonyms, fuzzy matching, embeddings or reranking.

Two further properties matter:

- **Staleness within a session.** The configuration hash that triggers re-delivery does not include Context Home
  text, so facts published mid-session are not shown until the next lifecycle event [C].
- **Spill on Claude Code.** Startup context above 9,500 characters is written to `.qorqut/startup-context.md` and
  the model gets a notice to read it (`claude_session.py:196-199`, re-checked). Whether models reliably read the
  whole file was not measured.

## 8. Session continuity

- After compaction the agent gets the static bundle, Context Home and its saved handoff again. Anything that was
  only in the chat is gone unless the agent saved it [C].
- Working memory is private per agent and per scope, with no inheritance from a business to Global. A fact one
  worker learned does not appear in another agent's prompt [C/D].
- Nothing writes working memory automatically. The instruction is "Save meaningful progress during work" [C].
- Cross-agent sharing happens only through the shared graph, tasks, notes and the inbox.
