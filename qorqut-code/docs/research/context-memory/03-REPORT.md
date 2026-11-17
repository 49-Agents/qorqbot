# Why the owner has to repeat themselves, and what others do about it

Report, 2026-11-16. Built on [01-CURRENT-SYSTEM.md](01-CURRENT-SYSTEM.md) (how Qorqut works, from code) and
[02-EXTERNAL-RESEARCH.md](02-EXTERNAL-RESEARCH.md) (OpenBot and Hermes Agent, from public pages).

## Conclusion

Qorqut has a careful, well-evidenced knowledge graph and almost no path between it and a conversation, in either
direction. What the owner says in chat is not stored unless an agent chooses to file it, and what is stored is not
shown unless an agent chooses to search for it with the exact words. OpenBot and Hermes have much cruder stores
but close both of those gaps by default.

This is a reading of the code. It has not been confirmed on a live installation, so the ranking below is by
likelihood, not measurement.

## The causes, ranked

### Getting knowledge in

1. **Chat is not captured.** The only automatic prompt covers personal facts about the owner, and only for
   Ordubashy. A statement like "Aziz is our CFO, he signs off anything above 5k" has no route to the graph unless
   the agent decides to call `context_upload` or `context_instruct`.
2. **Only two roles can write.** Ordubashy and Learning Applier hold the grants. A worker that learns something
   must relay it through the inbox.
3. **Filing is slow and serial.** One queue, one item at a time, a `max`-effort model run of up to twenty minutes
   each, and a failed item blocks everything behind it until someone retries or discards it.
4. **No entity or alias layer.** Whether a person gets a node, and whether "Aziz" and "our CFO" are the same node,
   is the filing model's judgment over a token-overlap candidate list.
5. **Working memory is a work handoff.** It is 4,000 characters, written only when the agent remembers, private
   to that agent and scope, and silently dropped when the startup budget is tight.

### Getting knowledge out

6. **No who-is-who in the prompt.** The startup index shows the 12 most connected nodes in 6,000 characters. The
   owner's own profile is a pointer the agent is told to read "on demand".
7. **Nothing is retrieved per turn.** A name in the owner's message triggers nothing.
8. **Search is literal.** `context_find` requires every query word to prefix-match and returns results in ID
   order. A natural question returns nothing; a nickname or a different spelling misses.
9. **Chat history is not searchable** by agents, so "we discussed this last week" has no fallback.
10. **Startup context may not be read.** On Claude Code, above 9,500 characters it is written to a file with a
    notice. Whether models read the whole file is not measured.
11. **Facts published mid-session are not shown** until the next start, resume or compaction.

Causes 1, 6, 7 and 8 are the ones most likely to produce "who is this again?", and each is verified in code.

## How the other two handle the same problem

| Question | Qorqut | OpenBot | Hermes core | Hermes + provider |
|---|---|---|---|---|
| Always-injected memory of facts | Project list and a 12-node index | 8 KB of pinned memories | `MEMORY.md` + `USER.md`, about 3.6k characters | Same, plus a peer card |
| Chat captured without the agent choosing to | No | Background review every 20 turns | Background review every 10 turns | Every turn |
| Recall keyed on the user's message | No | Proposed, not shipped | No | Yes |
| Search | AND-prefix, unranked | bm25 keyword | FTS5 keyword | Semantic or hybrid |
| Chat history searchable | No | Yes | Yes | Yes |
| Entities and aliases | Left to the filing model | None | None | Yes (Honcho, Hindsight) |
| Evidence, provenance, history | Strong | None | None | Varies |

Three things stand out:

- **Both have a small block that is always in the prompt, and a background pass that fills it.** Hermes moved
  that pass out of the working agent's prompt and into a separate run after the reply, because in-prompt nudges
  competed with the task. Qorqut's capture today is exactly such an in-prompt nudge.
- **Neither core product has a who-is-who layer.** That comes only from entity-aware providers. Qorqut's graph is
  already closer to that than either product; it lacks the alias handling and the injection.
- **Their own users complain about what Qorqut would hit next:** caps that are too small, stale and contradictory
  entries, and recall that depends on the agent choosing to search. OpenBot's most-requested fix is per-turn
  automatic recall.

## Directions worth discussing

These are options for the conversation, in the order I would try them. None is designed or costed.

1. **Per-turn recall.** On each owner message, match names and terms against the graph and inject the matching
   node summaries within a small budget. This addresses causes 6, 7 and 8 without touching the write side, and
   the hook that would carry it already runs on every prompt.
2. **An always-injected who-is-who block.** A compact roster of people, organisations and terms with aliases and
   one line each, chosen by importance and recency, not by edge count.
3. **Background capture from chat.** A separate pass after N turns, at idle and before compaction, that extracts
   durable facts and queues them, so capture no longer depends on the working agent.
4. **Ranked, forgiving search.** bm25 ordering and OR fallback are small changes to `search.py`; alias expansion
   and semantic search are larger.
5. **Aliases as a first-class field.** So that entity resolution at filing time and recall at read time both use
   the same list.
6. **A fast lane for simple facts.** A cheap path for "X is Y" statements that does not wait behind a twenty-minute
   propagation run.
7. **Searchable chat history** as the fallback when nothing was filed.

## What would settle the ranking

- Read a live graph: how many person nodes exist, whether they carry aliases, and what the owner's personal node
  contains.
- Take five recent moments where the owner had to re-explain something and trace each: was the fact ever filed,
  was it findable with `context_find`, did the agent search at all.
- Check the queue for blocked or long-waiting items.
- Measure the startup bundle size on a real agent, and whether the spill file is read in full.
