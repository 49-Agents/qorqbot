# The Learning Applier's two workflows

The Learning Applier (role `learning-applier`, shown as Overseer) finds what goes wrong in how the agents work and
proposes changes to the agentic layer. It does this in two scheduled workflows. The code is in
`qorqut-code/app/qorqut/experience/`; the agent's instructions are the role brief and the skills
`experience-extraction` and `delta-proposals`.

## Experience extraction (daily, 03:00)

The agent reads three reports and files observations.

| Tool | What it returns |
| --- | --- |
| `experience_conversations` | One timeline of everything the owner wrote and decided in the period, and what was answered: the chat with every agent, the private threads, the owner's notes, the Signoff decisions. Each entry has its time, author, place, id and how to open the record. The code organises and never summarises. Paged; the last page carries `end_token`. |
| `experience_tool_problems` | The tool calls that failed in every agent's conversations, grouped by what the error says, with counts and examples, and the same call repeated back to back. |
| `experience_tool_patterns` | Sequences of tool calls that repeat, by tool name and by the shape of the call. |

`experience_observations_submit` files observations: issues, complaints and problems, one per occurrence. An observation
never says what to change. Each one goes under a cluster, an existing one or a new one named by the agent. A cluster's
rank is the number of observations in it (`experience_clusters_list`).

The run cannot be finished before the report of conversations was read to its end (`experience_run_finish` takes the
`end_token`).

## What agents file themselves

Any agent adds to the same queue with `experience_observation_file`: a rule or correction the owner gave on how work is
done, a lesson from a mistake of its own, a tool that failed or misled it. The agent says what happened and what should
hold from now on, and does not work out which instruction, skill or tool should change: that is the review's work. The
point is filed at once, outside any run, and changes nothing by itself.

A rule the owner gave (kind `owner_complaint`) also gets a card for the owner, created in the same transaction: an
operation Signoff of `experience_rule_check` titled "Approve proposed learning". It shows a notice line (which agent filed it, that
it is already in the queue, that no instruction changes until the weekly review), the rule as filed as its one editable
field, and whom it applies to, with the email Signoff's footer (note, Reject, Approve). It checks the agent's wording
only. Approving marks the rule `approved` (`owner_check`), with the owner's text if the owner edited it. Rejecting, with
or without a note, deletes the rule, and the note goes to the filing agent like every decision on its own Signoff; a rule
a delta proposal already names is marked `rejected` instead. With no answer it stays `unchecked`. Every other kind gets
no card. The filing result tells the agent the owner was asked, so the agent does not ask again in chat. The operation
is the host's own: it is always held for the owner, offered to no agent and refused over agent MCP, and only the filing
stages it. A rejection or an edit the Signoff observer missed is followed by `settle_checks` on the inbox reconcile pass.

What agents file is kept under one standing cluster, "Filed by agents" (`AGENTS_CLUSTER`), which the first filing makes.
It is unsorted and states no one problem, so a delta proposal links the observations in it and is refused when it names
the cluster. Each observation shows who filed it (`filed_by`); one an agent filed itself has no `run_id`. The same agent
files the same text once.

This is the third of the three places an agent records what lasts. Work (what the owner wants done or decided for one
Task or Project) is the Task and its timeline, and Context is a fact about a person, a company or the world; the
coordinator files both directly, with no Signoff from the owner. Only a point about how agents work comes here.

## Review and application (weekly, Sunday 06:00)

The agent reads the clusters and their observations, decides which are ready for a solution, and files agentic layer
delta proposals with `agentic_delta_proposal_submit`. A proposal names the observations and clusters it addresses. The
clusters are a hint: a proposal may take observations from several clusters.

Filing a proposal creates its approval request, a Signoff of the Learning Applier with the choices Approve and Reject,
for the owner alone. The proposal's thread is the thread of that Signoff. As in every record's thread, the owner writes
there and Ordubashy answers; the Learning Applier does not write in it and reads what was said through
`agentic_delta_proposal_get` at its next review.

| State | Meaning |
| --- | --- |
| `waiting_for_approval` | Filed; the owner has not chosen. |
| `approved` | The owner chose Approve. |
| `rejected` | The owner chose Reject. |

An answer in words without a choice leaves the proposal waiting and marks it as needing revision
(`needs_revision`). A message in the thread alone does not mark it; the agent reads the thread of every waiting
proposal. `agentic_delta_proposal_revise` changes the proposal and files a new approval request; a request that is
still open is withdrawn. A proposal that is `approved` or `rejected` is final: a new one names it in `replaces_id`.
Nothing is applied on approval.

## Runs

A run is a row of `experience_runs`. `Runs.due` (inbox reconcile pass) queues a run when a scheduled time has passed;
`Runs.step` (native Chat backend pass) starts the oldest waiting run once no run is running and the agent is idle.

- Every run starts in a fresh conversation: the agent's idle conversation is closed and a new one opened whose first
  message names the run.
- One run at a time. A run that comes due while another is running waits until that one is finished.
- A run ends when the agent calls `experience_run_finish`. A run whose conversation ended without it is started once
  more, then given up as `abandoned`. The next extraction reads from the end of the last finished one, at most seven
  days back.
- The first run of a kind is queued only at its scheduled time. Later, a time missed while the service was down is
  caught up.

Settings (environment of the service):

| Variable | Default | Meaning |
| --- | --- | --- |
| `QORQUT_EXPERIENCE_EXTRACTION_AT` | `0 3 * * *` | Five-field cron expression, or `off`. |
| `QORQUT_EXPERIENCE_REVIEW_AT` | `0 6 * * 0` | Five-field cron expression, or `off`. |
| `QORQUT_EXPERIENCE_TIMEZONE` | not set | The zone the two times are in (IANA name). |

The times are in the owner's time zone (`runs.zone_name`). It is the first of these that names a real zone:
`QORQUT_EXPERIENCE_TIMEZONE`, the owner's timezone setting, the installation's `QORQUT_TIMEZONE`, the zone of the computer the service runs on (`TZ`,
then what the system says), and UTC when none does. The Runs tab shows the zone in use.

`experience_run_request` queues a run now, outside the schedule (owner, Ordubashy or the Learning Applier).

## Who can call what

The host policy (`composition/policy.py`, `_experience`) gives `experience_observation_file` to every agent, always holds
`experience_rule_check` for the owner, and gives the tools of
a run to the Learning Applier alone, and the reads (`experience_run_get`, `experience_run_request`,
`experience_clusters_list`, `experience_cluster_get`, `agentic_delta_proposals_list`, `agentic_delta_proposal_get`) to the
Learning Applier and Ordubashy. The owner reads the
same through the dashboard: the Overseer's Changes view has Delta proposals, Observations and Runs. A proposal's page
shows its approval request and its thread in place, so the owner decides and talks there.

## Tables

`experience_runs`, `experience_clusters`, `experience_observations`, `agentic_delta_proposals`,
`agentic_delta_proposal_links` (`experience/schema.py`; added to an existing store by `migration/experience.py`). The owner's
check of a rule (`owner_check`, `check_signoff_id`, `checked_at` on `experience_observations`) is added to an existing
store by the `rule_checks` step of the ordered upgrade (`migration/upgrade.py`, `migration/rule_checks.py`).
