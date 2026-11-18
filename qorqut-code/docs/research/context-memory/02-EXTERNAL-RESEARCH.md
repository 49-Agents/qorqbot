# How OpenBot and Hermes Agent handle persistent context

Research snapshot, 2026-11-16. Companion to [01-CURRENT-SYSTEM.md](01-CURRENT-SYSTEM.md) and
[03-REPORT.md](03-REPORT.md).

**How far to trust this.** Both sections come from one research pass each, reading public pages through a
summarising fetch tool. Figures and config keys are as those pages state them; wording may be paraphrased, and
nothing was checked against source code or a running install. Vendor blog posts are marked as secondary.

## 1. OpenBot (openbot.run)

### What it is

- A local-first desktop app (macOS, Windows, Linux, mobile companions) for running a team of AI agents, by
  Synthetify Labs. Repo: https://github.com/nightly-labs/openbot (Electron, SolidJS, SQLite event log).
- It drives other runtimes (Codex, Claude Code, Gemini, Cursor, ACP agents, OpenAI-compatible endpoints) and is
  not a fork or rebrand of OpenClaw, Clawdbot or the robotics OpenBot.
- The marketing site says nothing technical about memory. The detail below comes from
  `docs/architecture/providers.md`, PR #1779 and issues #1836, #2463, #2535, #2884, #3134, #3682, #6438.

### Stores

| Store | Detail |
|---|---|
| Agent memories | SQLite, per agent. Each entry is 500 characters at most. Default cap 64 entries per agent (64, 128, 256 or 512). Agent-scoped or user-scoped. |
| Inclusion mode per entry | "Always included", "Search when needed", or "Let the agent decide". A user's choice beats the model's. |
| Conversation history | SQLite event log that survives provider switches. Tools `history_search` (5 results, 400-character excerpts) and `history_read`. |
| Skills | Agent-written procedures (`create_skill`, `revise_skill`). |
| Not built in | No vector index, no entity or knowledge files, no daily logs. |

### Write path

- **Explicit:** the agent calls `remember`, `forget_memory` or `set_memory_inclusion`. Changes are staged and
  committed only if the turn completes. At the cap, `remember` fails and tells the agent to merge or forget.
- **Background review:** after 20 completed turns (10, 40 or Off), or 15 idle minutes with three or more new
  turns, or when a chat is cleared. One tool-less LLM call, on a cheaper model where available, returns JSON
  `add` / `update` / `forget` operations. It never overwrites entries the user wrote or pinned.
- **Before a session is rotated:** nothing. Issue #3134 proposes extracting up to six durable facts first; it is
  open.

### Read path

- **Always injected:** the "always included" memories, in an `<openbot_saved_memories>` block capped at 8,192
  bytes, placed last so the stable prefix stays cacheable. CI fails if instruction and tool bytes grow.
- **On demand:** `search_memories`, local FTS5 with bm25 ranking. Keyword only; vector search is explicitly out of
  scope.
- **Per-turn automatic recall:** not shipped. Issue #3134 says the agent rarely searches mid-task and proposes
  running the incoming message through FTS and injecting the top five results within about 1.5 KB. Open, with a
  working version on a fork.

### Who is who

No entity, person, organisation or alias model, and no structured user profile. The expectation is that this
lives in the always-included memories as free text.

### Compaction

A replacement session starts from a handoff of 32,000 characters at most: 24k of recent messages, 4k of older
excerpts, 2k of work steps, 2k of instructions. The excerpts are not semantic summaries.

### Complaints from its own users

- **Drift (#2463):** one user counted 15 contradictions, 31 stale entries and 21 orphaned entries among 177
  memories across 17 agents.
- **Small cap (#1836):** merge-or-forget loses detail; one agent reached 58 of 64 entries in under three weeks.
- **Lost chat facts (#3134):** facts that were only in the chat are lost when sessions are retired.
- **Secondary:** Hindsight (Vectorize) markets an MCP memory add-on for it, describing built-in memory as "a few
  kilobytes per agent". https://hindsight.vectorize.io/blog/2026/10/29/openbot-persistent-memory

## 2. Hermes Agent (Nous Research)

### What it is

"Hermes harness" is taken to mean Hermes Agent, https://github.com/NousResearch/hermes-agent: an MIT-licensed
agent with a CLI and a messaging gateway (Telegram, Discord, Slack, WhatsApp, Signal, email). No other project
matched better. Docs: https://hermes-agent.nousresearch.com/docs/

### Stores

| Store | Detail |
|---|---|
| `MEMORY.md` | The agent's own notes. 2,200 characters (`memory.memory_char_limit`). |
| `USER.md` | Free-text user profile. 1,375 characters (`memory.user_char_limit`). |
| Session store | `~/.hermes/state.db`, SQLite with an FTS5 table over all messages. Pruning off by default. |
| Skills | Procedural memory as `SKILL.md` folders, written by the agent. |
| Context files | `SOUL.md`, `AGENTS.md` and similar, for identity and project context. |
| External memory provider | One at a time, alongside the built-in memory: Honcho, Hindsight, Mem0, Supermemory, OpenViking, Holographic and others. |

### Write path

- **Explicit:** a `memory` tool with `add`, `patch` and `remove`. When the store is full the tool returns an
  error and the agent must shorten or remove entries in the same batch.
- **Background review:** PR #2235 removed in-prompt nudges. A forked agent now runs after the reply is delivered,
  with only the memory and skill tools and five iterations, by default every ten user turns. It can use a cheaper
  model.
- **Optional approval gate:** `memory.write_approval` stages writes for `/memory approve`.
- **Curator:** on inactivity, marks agent-written skills stale after 14 unused days and archives them after 30.
  Nothing is deleted.
- **Providers** get hooks after each turn, at session end, and before compression.

### Read path

- **Always injected:** identity, the skills index, one project context file, then snapshots of `MEMORY.md` and
  `USER.md` with a usage meter.
- **Frozen per session:** the snapshot does not change mid-session, to protect prompt caching. A fact saved now
  appears in the prompt next session.
- **On demand:** `session_search`, FTS5 over past messages, returning real excerpts. No embeddings in core.
- **Per-turn automatic recall:** only through an external provider. Its `prefetch(query)` runs before each model
  call and the result is appended to the current user message, leaving the cached prefix untouched.

### Who is who

- Core has no entity model. `USER.md` is one free-text profile.
- **Honcho** adds the identity layer: gateway users map to "peers" through a resolver with an alias map (several
  IDs to one person), each turn is credited to its sender, and each turn gets a peer card, a user representation
  and a short dialectic summary injected.
- **Hindsight** adds a knowledge graph with entity resolution, with automatic retain and recall.
- **Mem0** and **Supermemory** add server-side fact extraction, dedupe and semantic search with automatic recall.

### Compaction

- Fires at 50% of the context window, with a gateway hygiene pass at 85%.
- Large tool outputs are stubbed, the first three and last twenty messages are protected, and the middle is
  summarised by an auxiliary model into a fixed template (Goal, Constraints & Preferences, Progress, Key
  Decisions, Relevant Files, Next Steps, Critical Context).
- Later compactions update the earlier summary. Identifiers are extracted by regex and kept, user messages are
  kept verbatim, and archived turns stay searchable.

### Complaints from its own users

- **The cap is the loudest complaint** (issues #5563, #5320): users reach 96-97% quickly and consolidation loses
  detail. The default had not changed as of this research.
- **Search is keyword-only and the agent must choose to call it.** The official troubleshooting guide tells users
  to ask the agent to search past sessions or paste the detail again.
- **Secondary, unverified:** vendor posts report stale facts accumulating.
  https://vectorize.io/articles/hermes-agent-memory-not-working

## 3. What the two have in common

| Question | OpenBot | Hermes core | Hermes with a provider |
|---|---|---|---|
| Small always-injected memory | Yes, 8 KB | Yes, about 3.6k characters | Yes |
| Split into "about the user" and "about the work" | By scope | Yes, two files | Yes |
| Background extraction after N turns | Yes, 20 | Yes, 10 | Yes, per turn |
| Chat history searchable by the agent | Yes | Yes | Yes |
| Automatic recall keyed on the user's message | Proposed, not shipped | No | Yes |
| Entity and alias layer | No | No | Yes (Honcho, Hindsight) |
| Ranked search | bm25 | FTS5 | Semantic or hybrid |

Both products solve "remember what I told you" with the same three parts: a small block that is always in the
prompt, a background pass that fills it without relying on the working agent, and searchable chat history as a
fallback. Neither core product solves "who is who"; that comes only from the entity-aware providers. Both sets of
users complain about the same two things: caps that are too small, and recall that depends on the agent choosing
to search.
