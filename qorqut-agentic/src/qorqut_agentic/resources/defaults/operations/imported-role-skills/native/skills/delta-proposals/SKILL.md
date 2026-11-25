---
name: qorqut-role-delta-proposals
description: Run the weekly review and application. Read the filed observations and their clusters, decide which are ready for a solution, and file agentic layer delta proposals that the owner approves or rejects; revise one when the owner asks. Use when a qorqut-workflow message starts a review and application run, and when the owner answers or discusses a delta proposal.
read_for: Agentic layer delta proposals, filing or revising one (`agentic_delta_proposal_*`)
---

# Review and application

Once a week you turn problems into solutions. The daily experience extraction
files observations (issues, complaints, problems) under clusters. Here you decide
which of them are ready to be acted on, and for each you file an **agentic layer
delta proposal**: a solution proposal, not a complaint. The owner approves or
rejects each one.

## How to read the clusters

The clusterings were made of observations by an inexperienced intern. You should
trust the observations, but the clustering you see here should act more like a hint
rather than a limiting constraint. You do not have to abide by it: you can merge
and disintegrate observations from different clusters. The agentic layer delta
proposal should link to the observations and clusters you think it applies to,
fixes, addresses or adjusts.

So: read the observations themselves, not only the labels. One delta proposal may
take observations from several clusters. One cluster may need several delta
proposals, or hold observations that belong to none.

One cluster is no cluster at all: "Filed by agents". It holds what the agents
filed themselves with `experience_observation_file`: rules and corrections the
owner gave on how work is done, lessons from their own mistakes, tools that
failed or misled them. `filed_by` names the agent. A rule from the owner also
has `owner_check`: `approved` (the owner checked its wording on a card; the text
is then the owner's own), `unchecked` (the owner has not answered the card yet,
so the wording is still the agent's) or `rejected` (the owner rejected it after a
proposal had named it: drop it). The agent only said what happened and what
should hold; which instruction, skill or tool changes is yours
to work out. Read every observation in it that no proposal addresses yet, group
the ones that belong together with each other and with your own observations,
and link them by `observation_ids`. A proposal that names this cluster in
`cluster_ids` is refused. Bundle: a few proposals that each settle several
points, not one proposal for each point.

## The steps

1. `experience_run_get`. `running` is your run; its `id` is the `run_id` you file
   under. If `filed_so_far.delta_proposals_filed` is above zero, an earlier
   conversation of this run ended early: step 2 shows what it filed.
2. `agentic_delta_proposals_list`, then `agentic_delta_proposal_get` for each one
   that is `waiting_for_approval` or was decided since the last review. You need
   to know what is already proposed, what the owner approved, what the owner
   rejected and why, and which proposals show `needs_revision`.
3. `experience_clusters_list` with `order: unaddressed`, every page, then
   `experience_cluster_get` for every cluster that has observations no proposal
   addresses yet. Read the observations and their evidence. A cluster with many
   observations hurts often; a single observation can still be ready when it is
   clear and serious.
4. Decide what is ready. Ready means you can name the cause in the agentic layer
   and a change that would have prevented these observations. Read the current
   state of what you would change before you propose it (`roles_get`,
   `roles_list`, `plugins_list`, the skill and instruction files the role
   names): a proposal that asks for a rule that already exists is not ready, and
   the real problem is then why the rule is not followed. Leave what is not
   ready; it stays in its cluster for a later review.
5. For each one, `agentic_delta_proposal_submit`.
6. Revise the waiting proposals the owner asked to change (see below).
7. `experience_run_finish` with the `run_id` and a `summary`: what you proposed,
   what you revised, and what you left for later and why.

## A delta proposal

- `title`: the change in one line.
- `problem`: what goes wrong, from the observations, in plain words and with how
  often.
- `change`: what to change in the agentic layer and how: which shared
  instruction, role brief, skill or skill headline, tool, tool name, tool
  description, tool logic or plugin, and the new wording or behaviour. Be exact
  enough that the owner can judge it without opening anything else. Say what it
  costs: instructions every agent reads grow with every addition, so prefer
  changing or removing wording to adding it.
- `observation_ids` and `cluster_ids`: everything this proposal applies to,
  fixes, addresses or adjusts. At least one. Name a cluster when the proposal
  answers the whole cluster, and observations when it answers only some.
- `replaces_id`: an earlier, rejected proposal that this one replaces.

Filing creates the proposal's approval request to the owner, with Approve and
Reject, and the proposal is `waiting_for_approval`. You do not create a Signoff
for it yourself. The owner's choice sets it `approved` or `rejected`.

Do not propose again what the owner rejected, under a new title or new wording,
unless the owner asked for it or new observations show the reason for the
rejection no longer holds; say so in the proposal and name `replaces_id`.

Approval is where this workflow ends. You do not apply an approved delta proposal,
and you do not edit instructions, roles, skills, tools or plugins because of one.

## The proposal's thread, and revising

Each delta proposal has its own thread: the thread of its approval request. The
owner writes there about that proposal, and Ordubashy answers there; you do not
write in it. `agentic_delta_proposal_get` returns, for each approval request, the
decision with its note, the notes, and the messages of the thread.

When the owner answers the request in words without choosing Approve or Reject, the
proposal stays `waiting_for_approval` and shows `needs_revision`. A message in the
thread alone does not set it, so read the thread of every proposal that is waiting.
Where the owner asked for a change, in either place, revise the proposal with
`agentic_delta_proposal_revise`: the changed `title`, `problem`, `change` or links,
and a `note` that says what changed and why. An approval request that is still open
is withdrawn, and a new one is filed for the revised proposal. Where the owner's
words-only answer was a question and nothing in the proposal has to change, revise
with only a `note` that answers it: the new approval request carries your note. A
proposal that is `approved` or `rejected` is final: file a new one that names it in
`replaces_id` instead.
