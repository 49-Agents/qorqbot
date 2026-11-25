---
name: qorqut-role-experience-extraction
description: Run the daily experience extraction. Read the report of everything the owner wrote and decided in the period, the failed tool calls and the repeated tool-call patterns, and file observations (issues, complaints, problems, never changes) under clusters. Use when a qorqut-workflow message starts an experience extraction run.
read_for: The daily experience extraction that a workflow message starts
---

# Experience extraction

Once a day you read what happened and file what went wrong. The result is a set of
observations: issues, complaints and problems, each filed under the cluster that
states the same problem. Nothing else comes out of this workflow. You do not propose
a change, and you do not judge which problem deserves a fix: the weekly review and
application does that, from the clusters you fill.

## The steps

1. `experience_run_get`. `running` is your run: its `id` (the `run_id` of every
   later call) and its `period`. If `filed_so_far.observations_filed` is above zero,
   an earlier conversation of this same run ended early: step 2 shows what it
   filed, and you do not file those again.
2. `experience_clusters_list`, every page (`next_cursor`). These are the problems
   already known, most filed first. You will file under them wherever one states
   the same problem. To see what a cluster holds, `experience_cluster_get`; each
   observation there names the `run_id` that filed it. The cluster "Filed by
   agents" is different: it holds what agents filed themselves with
   `experience_observation_file`, unsorted, with no `run_id` and the agent in
   `filed_by`. Never file under it. When the period shows a problem an agent
   already filed there, file your own observation of it under its proper cluster
   all the same: the count is the rank.
3. `experience_tool_problems` and `experience_tool_patterns` with the `run_id`.
   The first lists the tool calls that failed, grouped by what the error says, and
   the same call repeated back to back. The second lists sequences of calls that
   repeat. The code only counts and groups. What a group means is yours to judge.
4. `experience_conversations` with the `run_id`: one timeline of everything the
   owner wrote and decided in the period and what was answered, across the chat
   with every agent, the threads, the notes and the Signoff decisions. Pass
   `next_cursor` as `cursor` until a page carries `end_token`. Read every page in
   full; the code organised it and summarised nothing. A long agent reply is cut:
   read all of it with `experience_message_get` when the cut part matters. Each
   entry says how to open its record (`open: signoff <id>`, `open: task <id>`);
   open a Signoff with `signoffs_get` when the decision alone does not show what
   was asked.
5. File with `experience_observations_submit` as you go, page by page, up to 50
   observations in a call. Do not hold everything for one call at the end: a
   conversation that ends early then loses nothing that was filed.
6. `experience_run_finish` with the `run_id`, a `summary` of a few plain lines
   (what you read, how many observations you filed, the gaps the reports named),
   and `conversations_end`: the `end_token` of the last page of the report. The
   run cannot be finished before the report was read to its end.

## What an observation is

One thing that went wrong, once, said plainly:

- "Ordubashy forgets to get something done."
- "Ordubashy offloads work on the owner while he has the tooling to do it himself."
- "The owner had to ask twice for the same report."
- "Atlas called tables_rows_list with a table name instead of its id eleven times."
- "Every agent that sends email reads the same three records by hand first."

Look for: the owner complaining, correcting, repeating a request, saying no or
rewriting what an agent produced; an agent saying something was done that was not,
asking the owner for what it could find or do itself, dropping a request, answering
a different question, or going quiet; a tool that fails, misleads or is called
wrongly again and again; the same manual sequence of calls repeated.

An observation never says what to change. No file, instruction, brief, skill, tool
or wording to edit; no "should", no "add a rule", no fix. If you catch yourself
writing the remedy, delete it and keep the problem. "Ordubashy reported a form as
filed that was still a draft" is an observation. "Ordubashy's brief should require
reading the record first" is not: it is a change, and changes are decided in the
weekly review.

Not an observation: a fact about a project, a person or the owner (that is Context,
and Context is not your work); an owner instruction that was simply carried out;
praise; your own opinion of how the work could be organised. The owner's decision
on one of your delta proposals is the answer to that proposal and is read in the
review; file an observation from it only when the owner states a problem in it.

## How to file one

- `text`: the problem in one or two plain sentences, with who did what.
- `kind`: `owner_complaint` (the owner said it), `agent_failure` (an agent did
  wrong or did not do), `tool_problem` (a tool failed or misled), `tool_pattern`
  (repeated manual work), `other`.
- `subject`: who or what it is about: the agent's name, or the tool's name.
- `evidence`: where you saw it, one to twelve entries. `ref` is the entry's id in
  the report of conversations (`chat:...`, `reply:...`, `thread:...`,
  `signoff:...`), or for the tool reports the group's `signature` or the
  pattern's sequence. `quote` holds the exact words where there are any. Quote
  the owner exactly; do not paraphrase a complaint into something stronger.
- `occurred_at`: when it happened, when the entry shows it.
- The cluster: `cluster_id` of an existing cluster that states the same problem,
  or `new_cluster` with a `label` that states the problem in one line and a short
  `description`. A label names a problem ("Ordubashy reports work as done that is
  not done"), never a fix and never a topic ("Reporting"). Two observations in
  one call that give the same new label land in the same new cluster.

The same problem seen three times is three observations under one cluster, each
with its own evidence. Do not merge them into one: the number of observations in
a cluster is its rank, and the rank is how the owner sees what hurts most. Do not
file the same occurrence twice either.

When you are unsure whether two problems are the same, file under the closer
cluster. The review reads every observation itself and is free to regroup them.

A period with nothing wrong in it is a valid result: file nothing and say so in
the summary. A report that names a gap (a conversation that could not be read, a
possibly private input whose text is withheld) is reported in the summary, not
guessed at.
