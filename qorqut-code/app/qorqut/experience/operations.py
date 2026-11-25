"""The tools of the Learning Applier's two workflows.

Experience extraction (every day): the agent reads the tool-call reports (``experience_tool_problems``,
``experience_tool_patterns``) and the report of conversations (``experience_conversations``) of the run's period, and
files what went wrong with ``experience_observations_submit``. An observation states an issue, a complaint or a problem,
never a change to make. Each is filed under a cluster the agent picks or names, so a problem filed many times ranks high
(``experience_clusters_list``).

Review and application (every week): the agent reads the clusters and their observations and files solution proposals
with ``agentic_delta_proposal_submit``, each naming the observations and clusters it addresses. A proposal is
``waiting_for_approval`` until the owner decides its approval request: a Signoff with the choices Approve and Reject,
created in the same transaction as the proposal. That Signoff is where the owner reads the proposal, and its thread is
where the owner talks about it. Choosing sets the proposal ``approved`` or ``rejected`` (``signoff_updated``). An answer
in words alone decides nothing: the proposal stays waiting, and ``agentic_delta_proposal_revise`` files a new request.
Nothing here applies an approved proposal.

Every agent adds to the same queue with ``experience_observation_file`` (the owner's decision, 23 November 2026): a rule
or correction the owner gave on how work is done, a lesson from a mistake, a tool that failed or misled. It is filed at
once as an observation of no run under the standing cluster ``AGENTS_CLUSTER``, and changes nothing by itself: the weekly
review reads it like any other observation. The filing agent decides only that the point is about how agents work; where a
change belongs is the Learning Applier's to work out.

A rule the owner gave (kind ``owner_complaint``) also gets a card for the owner, in the same transaction: an operation
Signoff of ``experience_rule_check`` (the owner's decision, 24 November 2026), titled "Approve proposed learning", whose one editable
field is the rule as filed. It checks the agent's wording; it approves no change to an instruction. Approving applies the
operation: the rule is marked approved by the owner, with the owner's text if the owner edited it. Rejecting deletes the
rule (``rule_check_updated``, through the Signoff status observer); the owner's note reaches the filing agent as every
decision on its own Signoff does. A rule a delta proposal already names is marked rejected instead of deleted. With no
answer the rule stays in the queue, marked not checked. The operation is the host's own: no agent is offered it as a tool,
and it is always held for the owner.

Every other write tool belongs to the active Learning Applier in person; the lists are also read by the owner and
Ordubashy.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import logging

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, apply_records, ref, schema, timestamp, utc_clock
from qorqut_operations.contracts import (DomainError, FieldChange, InvocationOptions, PreparedChange, PreviewResource,
                                         StructuredPreview, new_id, plain)

from . import conversations, tools
from .access import applier, filer, reader
from .runs import EXTRACTION, REVIEW, TITLES, Runs
from .schema import (APPROVED, CHECK_APPROVED, CHECK_REJECTED, CLUSTERS, LINKS, OBSERVATION_KINDS, OBSERVATIONS, PROPOSALS,
                     PROPOSAL_STATES, REJECTED, RUNS, UNCHECKED, WAITING)

LOG = logging.getLogger(__name__)

REQUEST_PREFIX = 'agentic-delta-proposal:'
INTERN = ('The clusterings were made of observations by an inexperienced intern. You should trust the observations, but the '
          'clustering you see here should act more like a hint rather than a limiting constraint. You do not have to abide '
          'by it: you can merge and disintegrate observations from different clusters. The agentic layer delta proposal '
          'should link to the observations and clusters you think it applies to, fixes, addresses or adjusts.')
# Where what agents file themselves is kept: one standing cluster, made by the first filing. It is not a statement of one
# problem, so a delta proposal links the observations in it and never the cluster (``check_links``).
AGENTS_CLUSTER = 'filed-by-agents'
AGENTS_CLUSTER_LABEL = 'Filed by agents'
AGENTS_CLUSTER_DESCRIPTION = (
    'What agents filed themselves with experience_observation_file: rules and corrections the owner gave on how work is done, '
    'lessons from their own mistakes, tools that failed or misled them. Not sorted: each observation here stands on its own.')
FILED_NOTE = ('Filed for the Learning Applier\'s review. It changes nothing by itself: the Learning Applier reads what was filed '
              'at its weekly review and proposes changes to instructions, skills and tools, which the owner approves.')
CHECK_NOTE = ('Filed for the Learning Applier\'s review, and the owner now has a card to check the rule\'s wording: do not ask the '
              'owner about it in chat, and follow the rule from now on. If the owner edits the wording, the owner\'s text replaces '
              'yours; if the owner rejects the rule, it is removed from the queue and the owner\'s note comes to you. Filing changes '
              'no instruction by itself: the Learning Applier reads what was filed at its weekly review and proposes changes, which '
              'the owner approves.')
# The owner's check of a rule an agent filed from the owner's words: an operation Signoff the filing creates itself.
OWNER_RULE = 'owner_complaint'
RULE_CHECK = 'experience_rule_check'
RULE_CHECK_TITLE = 'Approve proposed learning'
RULE_RESOURCE = 'experience_rule'
RULE_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2000, 'pattern': r'\S'}
EXPLANATION_LIMIT = 8000
RECENT = timedelta(days=30)

GLOBAL = {'scope': {'const': 'global'}}
IDENTITY = {'type': 'string', 'minLength': 1, 'maxLength': 100}
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 200}
CURSOR = {'type': 'string', 'pattern': '^[0-9]{1,7}$'}
IDS = {'type': 'array', 'items': IDENTITY, 'maxItems': 200, 'uniqueItems': True}
EVIDENCE = schema({'ref': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                   'quote': {'type': 'string', 'minLength': 1, 'maxLength': 600}}, ('ref',))
NEW_CLUSTER = schema({'label': {'type': 'string', 'minLength': 3, 'maxLength': 200, 'pattern': r'\S'},
                      'description': {'type': 'string', 'maxLength': 1000}}, ('label',))
OBSERVATION = schema({
    'text': {'type': 'string', 'minLength': 10, 'maxLength': 2000},
    'kind': {'enum': list(OBSERVATION_KINDS)},
    'subject': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'pattern': r'\S'},
    'occurred_at': {'type': 'string', 'minLength': 10, 'maxLength': 40},
    'evidence': {'type': 'array', 'items': EVIDENCE, 'minItems': 1, 'maxItems': 12},
    'cluster_id': IDENTITY, 'new_cluster': NEW_CLUSTER}, ('text', 'kind', 'subject', 'evidence'))
TITLE = {'type': 'string', 'minLength': 3, 'maxLength': 150, 'pattern': r'\S'}
PROBLEM = {'type': 'string', 'minLength': 10, 'maxLength': 2000}
CHANGE = {'type': 'string', 'minLength': 10, 'maxLength': 4000}

DESCRIPTIONS = {
    'experience_run_get': (
        'The Learning Applier\'s scheduled workflow runs: the one running now with its period and what it has filed so far, '
        'the ones waiting, the last ones finished, and the schedule. Call it first in a workflow conversation. Changes nothing.'),
    'experience_run_request': (
        'Queue one run of a workflow now, outside its schedule: kind is extraction (experience extraction) or review (review '
        'and application). It starts in a fresh conversation of the Learning Applier once no other run is running and the '
        'agent is idle. A run of that kind that is already waiting is moved to now instead of a second one being queued.'),
    'experience_run_finish': (
        'Learning Applier only. End the running workflow run: run_id, and summary (a few plain lines on what was read and what '
        'was filed). An experience extraction also gives conversations_end: the end_token on the last page of '
        'experience_conversations, which is there only once the report has been read to its end. Call it once, as the last '
        'step; the next run starts in a fresh conversation.'),
    'experience_conversations': (
        'Learning Applier only. The report of conversations of one extraction run: everything the owner wrote and decided in '
        'the run\'s period in one timeline, with what was answered in the same places: the owner\'s chat with every agent, the '
        'private threads, the owner\'s notes, and the Signoff decisions. Each entry has a time, who wrote it, where, an id and '
        'how to open the record. Nothing is summarised: the owner\'s words are whole, and a long agent reply is cut with the '
        'id that reads the rest (experience_message_get). The report comes in pages: pass next_cursor as cursor until a page '
        'has end_token instead. Read every page in full.'),
    'experience_message_get': (
        'Learning Applier only. One entry of the report of conversations with its whole text: id is the entry\'s id (chat:..., '
        'reply:..., thread:..., task-note:..., signoff-note:..., focus-note:...). A Signoff decision is opened with '
        'signoffs_get instead.'),
    'experience_tool_problems': (
        'Learning Applier only. The tool calls that failed in one extraction run\'s period, read from every agent\'s '
        'conversations: the failures grouped by what the error says, most frequent first, each with its count, the agents it '
        'happened to, how many were recovered by a later call, and examples; and the same call repeated back to back. The '
        'code only counts and groups; what a group means is yours to judge.'),
    'experience_tool_patterns': (
        'Learning Applier only. The pattern analyzer: sequences of tool calls that repeat in one extraction run\'s period, by '
        'tool name and by the shape of the call, with how often each occurred, in how many conversations, for which agents, '
        'and examples. A long sequence repeated many times is work an agent does by hand each time.'),
    'experience_clusters_list': (
        'The clusters of filed observations, ranked: by default the cluster with the most observations first (order: count, '
        'unaddressed or recent). Each has its id, label, description, the number of observations, how many were filed in the '
        'last 30 days, how many no waiting or approved delta proposal addresses yet, and the delta proposals linked to it. '
        'Read it before filing observations, to file under an existing cluster where one states the same problem.'),
    'experience_cluster_get': (
        'One cluster with its observations, newest first: each observation\'s id, kind, subject, text, evidence and the delta '
        'proposals that address it. A rule an agent filed from the owner\'s words also has owner_check: approved (the owner '
        'checked the wording on a card and approved it; the text is the owner\'s if the owner edited it), unchecked (the owner '
        'has not answered the card) or rejected (the owner rejected it after a delta proposal had named it). Pages with cursor.'),
    'experience_observations_submit': (
        'Learning Applier only, in a running experience extraction. File observations: issues, complaints and problems seen in '
        'the period, one each. An observation says what went wrong, to whom or by whom, in plain words, for example "Ordubashy '
        'forgets to get something done" or "Ordubashy offloads work on the owner while he has the tooling to do it himself". '
        'It never says what to change: no file, instruction, tool or wording to edit, and no proposed fix. Each has text, kind '
        '(owner_complaint: the owner said it; agent_failure: an agent did wrong or did not do; tool_problem: a tool failed or '
        'misled; tool_pattern: repeated manual work; other), subject (who or what it is about: an agent\'s name or a tool), '
        'evidence (where it was seen: ref is an entry id of the report or a tool-call group, quote the exact words where '
        'there are any), optionally occurred_at, and its cluster: cluster_id of an existing cluster that states the same '
        'problem (experience_clusters_list), or new_cluster with a label that states the problem in one line and a short '
        'description. File the same problem seen twice as two observations under one cluster: the count is the rank.'),
    'experience_observation_file': (
        'Any agent. Files one point about how agents work for the Learning Applier\'s review, at once and with no Signoff: a '
        'rule or correction the owner gave on how work is done ("never send emails without a Signoff"), a lesson from a '
        'mistake of yours, or a tool that failed or misled you. You do not work out which instruction, skill or tool should '
        'change: the Learning Applier reads what was filed at its weekly review and proposes the changes, which the owner '
        'approves together. Filing changes nothing by itself. Not for work the owner wants done ("make a daily report every '
        'day for the next year"): that is a Task and its timeline. Not for facts about people, companies or the world: those '
        'are Context. text says in plain words what happened and what should hold from now on, with the owner\'s exact words '
        'when the owner said it; name no file or instruction to edit. kind is owner_complaint (the owner said it: a rule, a '
        'correction or a complaint), agent_failure (a lesson from a mistake, yours or another agent\'s), tool_problem (a tool '
        'failed, misled or needed a workaround), tool_pattern (the same manual steps again and again) or other. subject is '
        'who or what it is about: an agent\'s name, a tool, or "all agents". evidence is optional, up to twelve {ref, quote}: '
        'where it was seen (ref: a Task id, a Signoff id or the link to a message) and the exact words. Each point is filed '
        'once. A rule from the owner (owner_complaint) also puts a card before the owner to check its wording: do not ask the '
        'owner about it in chat as well, and follow the rule from then on.'),
    RULE_CHECK: (
        'Not an agent tool. The owner\'s check of a rule an agent filed from the owner\'s words: created by '
        'experience_observation_file itself and always held for the owner. Approving marks the rule approved by the owner, '
        'with the owner\'s wording if the owner edited it; rejecting removes it from the queue.'),
    'agentic_delta_proposal_submit': (
        'Learning Applier only, in a running review and application. File one agentic layer delta proposal: a solution '
        'proposal for the owner to approve or reject. title; problem (what goes wrong, from the observations); change (what to '
        'change in the agentic layer: which instruction, skill, tool, tool name or description, and how); observation_ids and '
        'cluster_ids (everything this proposal applies to, fixes, addresses or adjusts; at least one); optionally replaces_id '
        '(an earlier proposal this one replaces after it was rejected). ' + INTERN + ' Filing creates the approval request '
        'the owner decides; the proposal is waiting_for_approval until then.'),
    'agentic_delta_proposal_revise': (
        'Learning Applier only. Change a delta proposal that is still waiting_for_approval, after the owner asked for a change '
        'in its thread or answered its request in words without choosing: any of title, problem, change, observation_ids, '
        'cluster_ids (a list given replaces the links of that kind), and note (what changed and why, shown to the owner). The '
        'open approval request is withdrawn and a new one filed. An approved or rejected proposal is not revised.'),
    'agentic_delta_proposals_list': (
        'The agentic layer delta proposals, newest first, optionally of one state (waiting_for_approval, approved, rejected): '
        'number, title, state, what each addresses in counts, and whether its approval request is open.'),
    'agentic_delta_proposal_get': (
        'One agentic layer delta proposal in full: its problem and change, the observations and clusters it addresses, its '
        'state, and what the owner said about it: the decision, the notes on its approval requests and the messages of their '
        'threads.'),
}


def fold(text: str) -> str:
    """The key two cluster labels share: SQLite's ``lower()``, which folds ASCII letters only."""
    return ''.join(character.lower() if character.isascii() else character for character in ' '.join(text.split()))


def offset_of(args) -> int:
    return int(args.get('cursor') or 0)


def page(rows, offset, limit):
    """``rows`` holds one more than ``limit`` when another page follows."""
    return {'next_cursor': str(offset + limit)} if len(rows) > limit else {}


def run_view(run):
    return {'id': run['id'], 'kind': run['kind'], 'workflow': TITLES[run['kind']], 'state': run['state'],
            'scheduled_for': run['slot'], 'period': {'start': run['window_start'], 'end': run['window_end']},
            'attempts': run['attempts'], 'started_at': run['started_at'], 'finished_at': run['finished_at'],
            'result': run['result_json']}


class Experience:
    def __init__(self, runtime, *, clock=utc_clock, runs=None):
        self.runtime, self.app, self.clock = runtime, runtime.application, clock
        self.runs = runs or Runs(runtime, clock=clock)

    def register(self):
        # Which role may call which tool is the host's policy (``composition.policy._experience``); each handler below
        # proves the exact identity again.
        base = dict(scope_kinds=frozenset({'global'}), global_capability='read_global_context', supports_signoff=False,
                    resolve_namespace=lambda ctx, args: 'global')
        agent, shared = frozenset({'mcp'}), frozenset({'mcp', 'dashboard'})

        def read(name, properties, required, handler, exposure):
            self.runtime.registry.register(OperationDescriptor(
                name, schema({**GLOBAL, **properties}, required), description=DESCRIPTIONS[name], effect_kind='read',
                read=handler, exposure=exposure, **base))

        def write(name, properties, required, prepare, apply, exposure=agent):
            self.runtime.registry.register(OperationDescriptor(
                name, schema({**GLOBAL, **properties}, required), description=DESCRIPTIONS[name], effect_kind='records',
                prepare=prepare, apply=apply, exposure=exposure, **base))

        read('experience_run_get', {}, (), self.run_get, shared)
        write('experience_run_request', {'kind': {'enum': [EXTRACTION, REVIEW]}}, ('kind',), self.request_prepare,
              self.request_apply, shared)
        write('experience_run_finish', {'run_id': IDENTITY, 'summary': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
                                        'conversations_end': {'type': 'string', 'maxLength': 100}},
              ('run_id', 'summary'), self.finish_prepare, self.finish_apply)
        read('experience_conversations', {'run_id': IDENTITY, 'cursor': CURSOR}, ('run_id',), self.conversations_read, agent)
        read('experience_message_get', {'id': {'type': 'string', 'minLength': 6, 'maxLength': 200}}, ('id',),
             self.message_read, agent)
        read('experience_tool_problems', {'run_id': IDENTITY, 'limit': LIMIT}, ('run_id',), self.tool_problems, agent)
        read('experience_tool_patterns', {'run_id': IDENTITY, 'limit': LIMIT}, ('run_id',), self.tool_patterns, agent)
        read('experience_clusters_list', {'order': {'enum': ['count', 'unaddressed', 'recent']}, 'limit': LIMIT, 'cursor': CURSOR},
             (), self.clusters_list, shared)
        read('experience_cluster_get', {'cluster_id': IDENTITY, 'limit': LIMIT, 'cursor': CURSOR}, ('cluster_id',),
             self.cluster_get, shared)
        write('experience_observations_submit',
              {'run_id': IDENTITY, 'observations': {'type': 'array', 'items': OBSERVATION, 'minItems': 1, 'maxItems': 50}},
              ('run_id', 'observations'), self.observations_prepare, self.observations_apply)
        write('experience_observation_file',
              {'kind': {'enum': list(OBSERVATION_KINDS)}, 'subject': OBSERVATION['properties']['subject'],
               'text': OBSERVATION['properties']['text'], 'occurred_at': OBSERVATION['properties']['occurred_at'],
               'evidence': {'type': 'array', 'items': EVIDENCE, 'maxItems': 12}},
              ('kind', 'subject', 'text'), self.file_prepare, self.file_apply)
        write('agentic_delta_proposal_submit',
              {'run_id': IDENTITY, 'title': TITLE, 'problem': PROBLEM, 'change': CHANGE, 'observation_ids': IDS,
               'cluster_ids': IDS, 'replaces_id': IDENTITY}, ('run_id', 'title', 'problem', 'change'),
              self.proposal_prepare, self.proposal_apply)
        write('agentic_delta_proposal_revise',
              {'proposal_id': IDENTITY, 'title': TITLE, 'problem': PROBLEM, 'change': CHANGE, 'observation_ids': IDS,
               'cluster_ids': IDS, 'note': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}, ('proposal_id',),
              self.revise_prepare, self.revise_apply)
        # The owner's check of a rule: a held operation whose one editable field is the rule's text. It is offered to no
        # agent (``composition.policy.HOST_STAGED_OPERATIONS``); only the filing stages it, in its own transaction.
        self.runtime.registry.register(OperationDescriptor(
            RULE_CHECK, schema({**GLOBAL, 'observation_id': IDENTITY, 'text': RULE_TEXT}, ('observation_id', 'text')),
            description=DESCRIPTIONS[RULE_CHECK], effect_kind='records', supports_signoff=True, scope_kinds=frozenset({'global'}),
            global_capability='read_global_context', resolve_namespace=lambda ctx, args: 'global', exposure=agent,
            prepare=self.check_prepare, apply=self.check_apply, validate_targets=self.check_targets,
            editable_fields=('text',), revise=self.check_revise, rejection_note_optional=True))
        read('agentic_delta_proposals_list', {'state': {'enum': list(PROPOSAL_STATES)}, 'limit': LIMIT, 'cursor': CURSOR}, (),
             self.proposals_list, shared)
        read('agentic_delta_proposal_get', {'proposal_id': IDENTITY}, ('proposal_id',), self.proposal_get, shared)
        self.runtime.experience = self
        return self

    def builder(self, ctx, name, args, title):
        return PlanBuilder(ctx, name, args, title, clock=self.clock, namespace='global', affected_scope={'kind': 'global'})

    # ----- runs -----------------------------------------------------------------------------------------------------------

    @staticmethod
    def run(uow, identity):
        found = uow.repository(RUNS).get(identity)
        if found is None:
            raise DomainError('not_found', 'No workflow run has this id; experience_run_get names the running one')
        return found

    def own_run(self, ctx, uow, identity, kind):
        """The caller's own running run of this kind."""
        found = self.run(uow, identity)
        if found['kind'] != kind:
            raise DomainError('validation_error', f'This tool belongs to the {TITLES[kind]}; run {identity} is the '
                                                  f'{TITLES[found["kind"]]}')
        if found['state'] != 'running' or found['agent_id'] != ctx.principal_id:
            raise DomainError('invalid_transition', f'This run is {found["state"]}, not running: nothing more is filed in it')
        return found

    def extraction(self, uow, identity):
        found = self.run(uow, identity)
        if found['kind'] != EXTRACTION:
            raise DomainError('validation_error', 'This report belongs to an experience extraction run')
        return found

    @staticmethod
    def progress(uow, run):
        """What a run has filed so far."""
        count = lambda table: uow.connection.execute(f'SELECT COUNT(*) FROM {table} WHERE run_id=?', (run['id'],)).fetchone()[0]
        if run['kind'] == EXTRACTION:
            return {'observations_filed': count(OBSERVATIONS), 'clusters_created': count(CLUSTERS)}
        return {'delta_proposals_filed': count(PROPOSALS)}

    def run_get(self, ctx, args, options, uow):
        reader(self.app, ctx, uow)
        repository = uow.repository(RUNS)
        listed = lambda where, order: [run_view(repository.get(row['id'])) for row in uow.connection.execute(
            f'SELECT id FROM {RUNS} WHERE {where} ORDER BY {order} LIMIT 10')]
        running = Runs.running(uow)
        return {'running': {**run_view(running), 'filed_so_far': self.progress(uow, running)} if running else None,
                'waiting': listed("state='queued'", 'slot, id'),
                'finished': listed("state IN ('done','abandoned')", 'finished_at DESC, id'),
                'schedule': {kind: {'workflow': TITLES[kind], 'cron': cron} for kind, cron in self.runs.schedules().items()},
                'time_zone': str(self.runs.zone)}

    def request_prepare(self, ctx, args, options, uow):
        ctx = reader(self.app, ctx, uow)
        builder = self.builder(ctx, 'experience_run_request', args, 'Queue a workflow run')
        kind, now = args['kind'], builder.at
        waiting = uow.connection.execute(f"SELECT id FROM {RUNS} WHERE kind=? AND state='queued' ORDER BY slot LIMIT 1",
                                         (kind,)).fetchone()
        if waiting:
            before = uow.repository(RUNS).get(waiting['id'])
            builder.record(RUNS, before['id'], before, {
                'slot': now, 'window_end': now, 'window_start': self.runs.window_start(uow, kind, now, before['window_start'])},
                label='Workflow run')
        else:
            identity = new_id()
            builder.record(RUNS, identity, None, {
                'id': identity, 'kind': kind, 'state': 'queued', 'slot': now,
                'window_start': self.runs.window_start(uow, kind, now), 'window_end': now,
                'agent_id': None, 'conversation_id': None, 'attempts': 0, 'queued_at': now, 'started_at': None, 'seen_at': None,
                'finished_at': None, 'result_json': None}, label='Workflow run')
        return builder.build()

    def request_apply(self, ctx, plan, uow):
        reader(self.app, ctx, uow)
        change = plan.changes[0]
        if change.action == 'update' and (uow.repository(RUNS).get(change.resource.resource_id) or {}).get('state') != 'queued':
            raise DomainError('revision_conflict', 'That run started meanwhile; ask again to queue another', retryable=True)
        row = apply_records(ctx, plan, uow)[0]
        return {'run': run_view(row), 'running': bool(Runs.running(uow))}

    def finish_prepare(self, ctx, args, options, uow):
        ctx = applier(self.app, ctx, uow)
        run = self.run(uow, args['run_id'])
        run = self.own_run(ctx, uow, run['id'], run['kind'])
        if run['kind'] == EXTRACTION and args.get('conversations_end') != conversations.end_token(run):
            raise DomainError('validation_error', 'An experience extraction is finished only after the report of conversations '
                              'was read to its end: read experience_conversations page by page and pass the end_token of its '
                              'last page as conversations_end')
        builder = self.builder(ctx, 'experience_run_finish', args, 'Finish the workflow run')
        builder.record(RUNS, run['id'], run, {'state': 'done', 'finished_at': builder.at,
                                              'result_json': {'summary': args['summary'], **self.progress(uow, run)}},
                       label='Workflow run')
        return builder.build()

    def finish_apply(self, ctx, plan, uow):
        ctx = applier(self.app, ctx, uow)
        run = self.run(uow, plain(plan.arguments)['run_id'])
        self.own_run(ctx, uow, run['id'], run['kind'])
        row = apply_records(ctx, plan, uow)[0]
        return {'run': run_view(row), 'waiting': uow.connection.execute(
            f"SELECT COUNT(*) FROM {RUNS} WHERE state='queued'").fetchone()[0]}

    # ----- the reports of an extraction -----------------------------------------------------------------------------------

    def conversations_read(self, ctx, args, options, uow):
        applier(self.app, ctx, uow)
        run = self.extraction(uow, args['run_id'])
        return conversations.Report(self.runtime, uow, run, zone=self.runs.zone).page(offset_of(args))

    def message_read(self, ctx, args, options, uow):
        applier(self.app, ctx, uow)
        return conversations.message(self.runtime, uow, args['id'])

    def tool_problems(self, ctx, args, options, uow):
        applier(self.app, ctx, uow)
        return tools.Reading(self.runtime, uow, self.extraction(uow, args['run_id'])).problems(args.get('limit', tools.GROUP_LIMIT))

    def tool_patterns(self, ctx, args, options, uow):
        applier(self.app, ctx, uow)
        return tools.Reading(self.runtime, uow, self.extraction(uow, args['run_id'])).patterns(args.get('limit', tools.PATTERN_LIMIT))

    # ----- observations and clusters --------------------------------------------------------------------------------------

    # Over alias ``o``: a waiting or approved delta proposal names this observation, or named its cluster after it was filed.
    ADDRESSED = (f"EXISTS (SELECT 1 FROM {LINKS} l JOIN {PROPOSALS} p ON p.id=l.proposal_id "
                 f"WHERE p.state IN ('{WAITING}','{APPROVED}') AND ((l.target_type='observation' AND l.target_id=o.id) OR "
                 "(l.target_type='cluster' AND l.target_id=o.cluster_id AND p.created_at>=o.created_at)))")

    def cluster_rows(self, uow, where='', params=(), order='observations DESC, last_filed_at DESC, c.id', limit=None, offset=0):
        recent = timestamp(lambda: self.clock() - RECENT)
        sql = (f"SELECT c.id, c.label, c.description, c.created_at, COUNT(o.id) AS observations, "
               f"COALESCE(SUM(o.created_at>=?),0) AS observations_last_30_days, "
               f"COALESCE(SUM(o.id IS NOT NULL AND NOT {self.ADDRESSED}),0) AS unaddressed, "
               f"MIN(o.created_at) AS first_filed_at, MAX(o.created_at) AS last_filed_at "
               f"FROM {CLUSTERS} c LEFT JOIN {OBSERVATIONS} o ON o.cluster_id=c.id {where} GROUP BY c.id ORDER BY {order}")
        values = [recent, *params]
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            values += [limit + 1, offset]
        return [dict(row) for row in uow.connection.execute(sql, values)]

    def cluster_proposals(self, uow, cluster_id):
        """The delta proposals that name this cluster or one of its observations."""
        return [dict(row) for row in uow.connection.execute(
            f"SELECT DISTINCT p.id, p.number, p.title, p.state FROM {PROPOSALS} p JOIN {LINKS} l ON l.proposal_id=p.id "
            f"WHERE (l.target_type='cluster' AND l.target_id=?) OR (l.target_type='observation' AND l.target_id IN "
            f"(SELECT id FROM {OBSERVATIONS} WHERE cluster_id=?)) ORDER BY p.number", (cluster_id, cluster_id))]

    def clusters_list(self, ctx, args, options, uow):
        reader(self.app, ctx, uow)
        limit, offset = args.get('limit', 100), offset_of(args)
        order = {'count': 'observations DESC, last_filed_at DESC, c.id', 'unaddressed': 'unaddressed DESC, observations DESC, c.id',
                 'recent': 'last_filed_at DESC, c.id'}[args.get('order', 'count')]
        rows = self.cluster_rows(uow, order=order, limit=limit, offset=offset)
        total = uow.connection.execute(f'SELECT (SELECT COUNT(*) FROM {CLUSTERS}), (SELECT COUNT(*) FROM {OBSERVATIONS})').fetchone()
        return {'clusters': [{**row, 'rank': offset + number, 'delta_proposals': self.cluster_proposals(uow, row['id'])}
                             for number, row in enumerate(rows[:limit], 1)],
                'clusters_total': total[0], 'observations_total': total[1], **page(rows, offset, limit)}

    def observation_view(self, uow, row):
        addressed = [dict(found) for found in uow.connection.execute(
            f"SELECT DISTINCT p.id, p.number, p.state FROM {PROPOSALS} p JOIN {LINKS} l ON l.proposal_id=p.id "
            f"WHERE (l.target_type='observation' AND l.target_id=?) OR (l.target_type='cluster' AND l.target_id=? "
            f"AND p.created_at>=?) ORDER BY p.number", (row['id'], row['cluster_id'], row['created_at']))]
        name = uow.connection.execute('SELECT display_name FROM principals WHERE id=?', (row['created_by'],)).fetchone()
        # ``run_id`` is null for what an agent filed itself (``experience_observation_file``); ``filed_by`` names that agent.
        view = {'id': row['id'], 'cluster_id': row['cluster_id'], 'kind': row['kind'], 'subject': row['subject'],
                'text': row['text'], 'evidence': row['evidence_json'], 'occurred_at': row['occurred_at'],
                'filed_at': row['created_at'], 'filed_by': (name['display_name'] if name else None) or row['created_by'],
                'run_id': row['run_id'], 'delta_proposals': addressed}
        if row.get('owner_check'):
            # The owner's check of a rule an agent filed from the owner's words, and the card it was asked on.
            view.update(owner_check=row['owner_check'], checked_at=row['checked_at'], check_signoff_id=row['check_signoff_id'])
        return view

    def cluster_get(self, ctx, args, options, uow):
        reader(self.app, ctx, uow)
        found = self.cluster_rows(uow, where='WHERE c.id=?', params=(args['cluster_id'],))
        if not found:
            raise DomainError('not_found', 'No cluster has this id')
        limit, offset = args.get('limit', 50), offset_of(args)
        rows = uow.connection.execute(f'SELECT id FROM {OBSERVATIONS} WHERE cluster_id=? ORDER BY created_at DESC, id LIMIT ? OFFSET ?',
                                      (args['cluster_id'], limit + 1, offset)).fetchall()
        repository = uow.repository(OBSERVATIONS)
        return {'cluster': {**found[0], 'delta_proposals': self.cluster_proposals(uow, args['cluster_id'])},
                'observations': [self.observation_view(uow, repository.get(row['id'])) for row in rows[:limit]],
                **page(rows, offset, limit)}

    def observations_prepare(self, ctx, args, options, uow):
        ctx = applier(self.app, ctx, uow)
        run = self.own_run(ctx, uow, args['run_id'], EXTRACTION)
        builder = self.builder(ctx, 'experience_observations_submit', args, 'File observations')
        problems, created = [], {}
        for number, item in enumerate(args['observations'], 1):
            if ('cluster_id' in item) == ('new_cluster' in item):
                problems.append(f'Observation {number} names either cluster_id or new_cluster, exactly one of them.')
                continue
            if item.get('occurred_at'):
                try:
                    datetime.fromisoformat(item['occurred_at'].replace('Z', '+00:00'))
                except ValueError:
                    problems.append(f'Observation {number}: occurred_at is not an ISO-8601 date or time.')
                    continue
            if 'cluster_id' in item:
                cluster = item['cluster_id']
                if uow.repository(CLUSTERS).get(cluster) is None:
                    problems.append(f'Observation {number}: no cluster has the id {cluster}.')
                    continue
            else:
                label = ' '.join(item['new_cluster']['label'].split())
                key = fold(label)
                same = uow.connection.execute(f'SELECT id FROM {CLUSTERS} WHERE lower(label)=lower(?)', (label,)).fetchone()
                cluster = same['id'] if same else created.get(key)
                if cluster is None:
                    cluster = created[key] = new_id()
                    builder.record(CLUSTERS, cluster, None, {
                        'id': cluster, 'label': label, 'description': item['new_cluster'].get('description', '').strip(),
                        'run_id': run['id'], 'created_by': ctx.principal_id, 'created_at': builder.at}, label=label)
            identity = new_id()
            builder.record(OBSERVATIONS, identity, None, {
                'id': identity, 'run_id': run['id'], 'cluster_id': cluster, 'kind': item['kind'],
                'subject': ' '.join(item['subject'].split()), 'text': item['text'].strip(), 'evidence_json': item['evidence'],
                'occurred_at': item.get('occurred_at'), 'created_by': ctx.principal_id, 'created_at': builder.at},
                label=item['subject'])
        if problems:
            raise DomainError('validation_error', 'Nothing was filed. ' + ' '.join(problems), details={'problems': problems})
        return builder.build()

    def observations_apply(self, ctx, plan, uow):
        ctx = applier(self.app, ctx, uow)
        self.own_run(ctx, uow, plain(plan.arguments)['run_id'], EXTRACTION)
        rows = apply_records(ctx, plan, uow)
        made = [(change.resource.resource_type, row) for change, row in zip(plan.changes, rows)]
        clusters = {}
        for kind, row in made:
            if kind == OBSERVATIONS and row['cluster_id'] not in clusters:
                found = self.cluster_rows(uow, where='WHERE c.id=?', params=(row['cluster_id'],))[0]
                clusters[row['cluster_id']] = {'id': found['id'], 'label': found['label'], 'observations': found['observations']}
        return {'filed': sum(kind == OBSERVATIONS for kind, _ in made),
                'observations': [{'id': row['id'], 'subject': row['subject'], 'cluster': clusters[row['cluster_id']]}
                                 for kind, row in made if kind == OBSERVATIONS],
                'new_clusters': [{'id': row['id'], 'label': row['label']} for kind, row in made if kind == CLUSTERS]}

    # ----- what agents file themselves ------------------------------------------------------------------------------------

    @staticmethod
    def agents_cluster(uow):
        """The id of the standing cluster of what agents file themselves, or ``None`` before the first filing."""
        found = uow.connection.execute(
            f'SELECT id FROM {CLUSTERS} WHERE id=? OR lower(label)=lower(?) ORDER BY id=? DESC LIMIT 1',
            (AGENTS_CLUSTER, AGENTS_CLUSTER_LABEL, AGENTS_CLUSTER)).fetchone()
        return found['id'] if found else None

    def file_prepare(self, ctx, args, options, uow):
        ctx = filer(self.app, ctx, uow)
        subject, text = ' '.join(args['subject'].split()), args['text'].strip()
        if args.get('occurred_at'):
            try:
                datetime.fromisoformat(args['occurred_at'].replace('Z', '+00:00'))
            except ValueError:
                raise DomainError('validation_error', 'occurred_at is not an ISO-8601 date or time.') from None
        same = uow.connection.execute(f'SELECT id FROM {OBSERVATIONS} WHERE created_by=? AND run_id IS NULL AND text=?',
                                      (ctx.principal_id, text)).fetchone()
        if same:
            raise DomainError('validation_error',
                              f'You filed this point already (observation {same["id"]}). Each point is filed once.')
        builder = self.builder(ctx, 'experience_observation_file', args, 'File a point for the Learning Applier')
        cluster = self.agents_cluster(uow)
        if cluster is None:
            cluster = AGENTS_CLUSTER
            builder.record(CLUSTERS, cluster, None, {
                'id': cluster, 'label': AGENTS_CLUSTER_LABEL, 'description': AGENTS_CLUSTER_DESCRIPTION, 'run_id': None,
                'created_by': ctx.principal_id, 'created_at': builder.at}, label=AGENTS_CLUSTER_LABEL)
        identity = new_id()
        builder.record(OBSERVATIONS, identity, None, {
            'id': identity, 'run_id': None, 'cluster_id': cluster, 'kind': args['kind'], 'subject': subject, 'text': text,
            'evidence_json': args.get('evidence', []), 'occurred_at': args.get('occurred_at'),
            'created_by': ctx.principal_id, 'created_at': builder.at,
            'owner_check': UNCHECKED if args['kind'] == OWNER_RULE else None}, label=subject)
        return builder.build()

    def file_apply(self, ctx, plan, uow):
        ctx = filer(self.app, ctx, uow)
        rows = apply_records(ctx, plan, uow)
        row = next(row for change, row in zip(plan.changes, rows) if change.resource.resource_type == OBSERVATIONS)
        filed = {'id': row['id'], 'kind': row['kind'], 'subject': row['subject'], 'filed_at': row['created_at']}
        if row['kind'] != OWNER_RULE:
            return {'filed': filed, 'note': FILED_NOTE}
        signoff = self.check_request(ctx, row, uow)
        uow.repository(OBSERVATIONS).update(row['id'], {'check_signoff_id': signoff})
        return {'filed': filed, 'owner_check': {'state': UNCHECKED, 'signoff_id': signoff}, 'note': CHECK_NOTE}

    # ----- the owner's check of a rule ------------------------------------------------------------------------------------

    def check_request(self, ctx, observation, uow):
        """Stage the owner's card for a rule just filed, in the filing's transaction, as the filing agent's own proposal of
        ``experience_rule_check`` with the engine's own authorization, policy binding and target checks. Returns the
        Signoff's id. A delegated worker's binding is left out: the card's proposer is rebuilt without one at approval, as
        every proposal's is, and the operation changes only the rule the agent itself just filed."""
        ctx = replace(ctx, delegation_binding_id=None, delegation_assignment_id=None, delegation_generation=None)
        descriptor = self.app.registry.get(RULE_CHECK)
        arguments = self.app.registry.bind_arguments(RULE_CHECK, {
            'scope': 'global', 'observation_id': observation['id'], 'text': observation['text']})
        plan = descriptor.prepare(ctx, arguments, InvocationOptions(), uow)
        decisions = self.app._decisions(ctx, descriptor, plan.expected_targets, plan.changes, uow, namespace=plan.target_namespace)
        if not any(decision.mode == 'signoff' for decision in decisions.values()):
            raise DomainError('operation_unavailable', 'The owner\'s check of a rule is not held for the owner by the current '
                                                       'policy, so the rule was not filed')
        plan = replace(plan, policy_binding=self.app._binding(ctx, decisions), review_project_id=None,
                       review_scope=self.app._review_scope(ctx, plan), encoding_version=2, proposer_agent_id=ctx.agent_id,
                       initiated_by_principal_id=ctx.initiated_by_principal_id)
        self.app.check_targets(ctx, plan, uow)
        descriptor.validate_targets(ctx, plan, uow)
        return self.app.signoffs.stage(ctx, descriptor, plan, InvocationOptions(), uow).signoff['id']

    @staticmethod
    def checked_rule(uow, identity, principal_id):
        """The rule a card checks: an owner rule this agent filed itself, still waiting for the owner's check."""
        row = uow.repository(OBSERVATIONS).get(identity)
        if row is None or row['run_id'] is not None or row['kind'] != OWNER_RULE or row['created_by'] != principal_id:
            raise DomainError('not_found', 'No rule of yours filed for the owner\'s check has this id')
        if row['owner_check'] != UNCHECKED:
            raise DomainError('invalid_transition', f'The owner already checked this rule ({row["owner_check"]})')
        return row

    def check_prepare(self, ctx, args, options, uow):
        ctx = filer(self.app, ctx, uow)
        row = self.checked_rule(uow, args['observation_id'], ctx.principal_id)
        if row['check_signoff_id'] is not None:
            raise DomainError('invalid_transition', 'The owner was already asked to check this rule; filing it asks once')
        return self.check_plan(ctx, row, args['text'].strip(), uow)

    def check_revise(self, ctx, original, changes, uow):
        """The owner's edit of the rule's text on a waiting card: the same check with the owner's wording."""
        ctx = filer(self.app, ctx, uow)
        text = changes['text'].strip() if isinstance(changes.get('text'), str) else ''
        if not text or len(text) > 2000:
            raise DomainError('validation_error', 'The rule is 1 to 2000 characters of text')
        row = self.checked_rule(uow, plain(original.arguments)['observation_id'], ctx.principal_id)
        return self.check_plan(ctx, row, text, uow)

    def check_plan(self, ctx, row, text, uow):
        name = uow.connection.execute('SELECT display_name FROM principals WHERE id=?', (row['created_by'],)).fetchone()
        builder = PlanBuilder(ctx, RULE_CHECK, {'scope': 'global', 'observation_id': row['id'], 'text': text}, RULE_CHECK_TITLE,
                              clock=self.clock, namespace='global', affected_scope={'kind': 'global'})
        resource = builder.expect(ref(OBSERVATIONS, row['id'], None, tables=builder.tables), row)
        builder.changes.append(PreparedChange('record', resource, 'update', {'text': text, 'owner_check': CHECK_APPROVED}))
        # What the owner reads: the rule as filed (the one editable field), whom it applies to, and who filed it.
        label = PreviewResource(RULE_RESOURCE, row['id'], 'Rule')
        builder.resources.append(label)
        builder.previews.append(StructuredPreview(label, (
            FieldChange('text', None, text), FieldChange('applies_to', None, row['subject']),
            FieldChange('filed_by', None, (name['display_name'] if name else None) or row['created_by']))))
        builder.effects.append('After approval, the rule is marked approved by the owner in the Learning Applier\'s queue, with '
                               'this wording. No instruction, skill or tool changes until the weekly review.')
        return builder.build()

    def check_targets(self, ctx, plan, uow):
        """Approval rechecks that the rule is still there, still the proposer's, and still waiting for the check."""
        args = plain(plan.arguments)
        self.checked_rule(uow, args['observation_id'], plan.principal_id)
        changes = [change for change in plan.changes if change.resource.resource_type == OBSERVATIONS]
        if len(plan.changes) != 1 or len(changes) != 1 or plain(changes[0].values) != {'text': args['text'], 'owner_check': CHECK_APPROVED}:
            raise DomainError('invalid_plan', 'Prepared rule check is invalid')

    def check_apply(self, ctx, plan, uow):
        self.check_targets(ctx, plan, uow)
        args = plain(plan.arguments)
        row = uow.repository(OBSERVATIONS).update(args['observation_id'], {
            'text': args['text'], 'owner_check': CHECK_APPROVED, 'checked_at': timestamp(self.clock)})
        return {'rule': {'id': row['id'], 'owner_check': row['owner_check'], 'text': row['text']}}

    def rule_check_updated(self, before, after, uow):
        """A card follows the owner: an edit's new card becomes the rule's card, and a rejection deletes the rule, or marks
        it rejected where a delta proposal already names it. An approval is the operation's own apply."""
        rows = uow.connection.execute(f'SELECT id, owner_check FROM {OBSERVATIONS} WHERE check_signoff_id=?', (after['id'],)).fetchall()
        for row in rows:
            if after.get('superseded_by_id') and not before.get('superseded_by_id'):
                uow.repository(OBSERVATIONS).update(row['id'], {'check_signoff_id': after['superseded_by_id']})
            elif after.get('state') == 'rejected' and row['owner_check'] == UNCHECKED:
                self.reject_rule(uow, row['id'])

    def reject_rule(self, uow, identity):
        named = uow.connection.execute(f"SELECT 1 FROM {LINKS} WHERE target_type='observation' AND target_id=? LIMIT 1",
                                       (identity,)).fetchone()
        if named:
            uow.repository(OBSERVATIONS).update(identity, {'owner_check': CHECK_REJECTED, 'checked_at': timestamp(self.clock)})
        else:
            uow.repository(OBSERVATIONS).delete(identity)

    def settle_checks(self, uow):
        """Follow every edit and rejection the Signoff observer missed. Returns the ids of the rules it rejected."""
        for _ in range(20):                           # an edit's card may itself have been edited; each pass is one step
            moved = uow.connection.execute(
                f"SELECT o.id, s.superseded_by_id FROM {OBSERVATIONS} o JOIN signoffs s ON s.id=o.check_signoff_id "
                f"WHERE o.owner_check='{UNCHECKED}' AND s.superseded_by_id IS NOT NULL AND s.operation_name=?",
                (RULE_CHECK,)).fetchall()
            if not moved:
                break
            for row in moved:
                uow.repository(OBSERVATIONS).update(row['id'], {'check_signoff_id': row['superseded_by_id']})
        rows = uow.connection.execute(
            f"SELECT o.id FROM {OBSERVATIONS} o JOIN signoffs s ON s.id=o.check_signoff_id "
            f"WHERE o.owner_check='{UNCHECKED}' AND s.state='rejected' AND s.operation_name=?", (RULE_CHECK,)).fetchall()
        for row in rows:
            self.reject_rule(uow, row['id'])
        return [row['id'] for row in rows]

    # ----- delta proposals ------------------------------------------------------------------------------------------------

    @staticmethod
    def links_of(uow, proposal_id):
        found = {'observation': [], 'cluster': []}
        for row in uow.connection.execute(f'SELECT target_type, target_id FROM {LINKS} WHERE proposal_id=? ORDER BY target_type, target_id',
                                          (proposal_id,)):
            found[row['target_type']].append(row['target_id'])
        return found

    @staticmethod
    def check_links(uow, observations, clusters):
        problems = [f'No observation has the id {identity}.' for identity in observations
                    if uow.repository(OBSERVATIONS).get(identity) is None]
        problems += [f'No cluster has the id {identity}.' for identity in clusters if uow.repository(CLUSTERS).get(identity) is None]
        if Experience.agents_cluster(uow) in clusters:
            problems.append(f'The cluster "{AGENTS_CLUSTER_LABEL}" holds what agents filed themselves and states no one problem: '
                            'name the observations in it that this proposal addresses, not the cluster.')
        if not observations and not clusters:
            problems.append('A delta proposal names at least one observation or cluster it addresses.')
        if problems:
            raise DomainError('validation_error', 'The proposal was not filed. ' + ' '.join(problems), details={'problems': problems})

    def request_of(self, uow, proposal):
        """The proposal's latest approval request as a decoded Signoff row, or ``None``."""
        return uow.repository('signoffs', project_id=None).get(proposal['signoff_id']) if proposal['signoff_id'] else None

    def addresses(self, uow, links, budget):
        """What a proposal addresses, as lines for the owner; cut to ``budget`` characters with the number left out."""
        lines = []
        for identity in links['cluster']:
            row = self.cluster_rows(uow, where='WHERE c.id=?', params=(identity,))[0]
            lines.append(f'- Cluster "{row["label"]}" ({row["observations"]} observations)')
        for identity in links['observation']:
            row = uow.repository(OBSERVATIONS).get(identity)
            text = ' '.join(row['text'].split())
            lines.append(f'- Observation on {row["subject"]}: {text if len(text) <= 240 else text[:240] + "…"}')
        kept, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget - 80:
                kept.append(f'- and {len(lines) - len(kept)} more (the full list is in the proposal)')
                break
            kept.append(line)
            used += len(line) + 1
        return '\n'.join(kept)

    def form(self, uow, proposal, links, note=None):
        """The approval request the owner decides."""
        head = (f'The Learning Applier proposes this change to the agentic layer (delta proposal {proposal["number"]}). Approve '
                'or reject it here. To ask for a change, answer in words instead of choosing, or write in this request\'s '
                'thread: the proposal stays waiting, and the Learning Applier revises it in its next review.')
        # Plain headings: a Signoff's explanation is shown as written, not as Markdown.
        parts = [head, *([f'What changed in this revision\n{note}'] if note else []), f'Problem\n{proposal["problem"]}',
                 f'Proposed change\n{proposal["change"]}', 'Addresses\n']
        text = '\n\n'.join(parts)
        text += self.addresses(uow, links, EXPLANATION_LIMIT - len(text))
        return {'title': f'Delta proposal {proposal["number"]}: {proposal["title"]}'[:200], 'explanation': text[:EXPLANATION_LIMIT],
                'kind': 'request', 'selection_mode': 'single', 'response_policy': 'owner_only', 'selection_required': True,
                'notes_required': False, 'choices': [
                    {'id': 'approve', 'label': 'Approve', 'explanation': 'The delta proposal becomes approved.'},
                    {'id': 'reject', 'label': 'Reject', 'explanation': 'The delta proposal becomes rejected. Say why in the note, if you like.'}]}

    def file_request(self, ctx, proposal, links, uow, note=None):
        """Create the proposal's approval request in the caller's transaction, as the caller's own ``signoffs_input_create``
        with the engine's own authorization, policy binding and validation. Returns the Signoff's id."""
        descriptor = self.app.registry.get('signoffs_input_create')
        arguments = self.app.registry.bind_arguments('signoffs_input_create', {
            'scope': 'global', 'request_key': f'{REQUEST_PREFIX}{proposal["id"]}:{proposal["requests"] + 1}',
            'form': self.form(uow, proposal, links, note), 'attention': {'notify': True}})
        self.app.authorize(ctx, descriptor, (), uow, namespace='global')
        plan = descriptor.prepare(ctx, arguments, InvocationOptions(), uow)
        decisions = self.app._decisions(ctx, descriptor, plan.expected_targets, plan.changes, uow, namespace=plan.target_namespace)
        if any(decision.mode == 'signoff' for decision in decisions.values()):
            raise DomainError('operation_unavailable', 'The Learning Applier is not allowed to ask the owner for input, so the '
                                                       'proposal has no approval request and was not filed')
        plan = replace(plan, policy_binding=self.app._binding(ctx, decisions), review_project_id=None,
                       review_scope=self.app._review_scope(ctx, plan), encoding_version=2, proposer_agent_id=ctx.agent_id,
                       initiated_by_principal_id=ctx.initiated_by_principal_id)
        return self.app.apply_prepared(ctx, plan, uow).data['signoff']['id']

    def proposal_prepare(self, ctx, args, options, uow):
        ctx = applier(self.app, ctx, uow)
        run = self.own_run(ctx, uow, args['run_id'], REVIEW)
        observations, clusters = args.get('observation_ids', []), args.get('cluster_ids', [])
        self.check_links(uow, observations, clusters)
        if args.get('replaces_id'):
            replaced = uow.repository(PROPOSALS).get(args['replaces_id'])
            if replaced is None:
                raise DomainError('validation_error', 'replaces_id names no delta proposal')
            if replaced['state'] == WAITING:
                raise DomainError('validation_error', 'That proposal is still waiting for approval: revise it with '
                                                      'agentic_delta_proposal_revise instead of replacing it')
        builder = self.builder(ctx, 'agentic_delta_proposal_submit', args, 'File an agentic layer delta proposal')
        identity = new_id()
        number = uow.connection.execute(f'SELECT COALESCE(MAX(number),0)+1 FROM {PROPOSALS}').fetchone()[0]
        builder.record(PROPOSALS, identity, None, {
            'id': identity, 'number': number, 'run_id': run['id'], 'replaces_id': args.get('replaces_id'),
            'title': ' '.join(args['title'].split()), 'problem': args['problem'].strip(), 'change': args['change'].strip(),
            'state': WAITING, 'signoff_id': None, 'requests': 0, 'decided_at': None, 'decided_by': None,
            'created_by': ctx.principal_id, 'created_at': builder.at, 'updated_at': builder.at},
            label=f'Delta proposal {number}')
        return builder.build()

    def proposal_apply(self, ctx, plan, uow):
        ctx = applier(self.app, ctx, uow)
        args = plain(plan.arguments)
        self.own_run(ctx, uow, args['run_id'], REVIEW)
        links = {'observation': args.get('observation_ids', []), 'cluster': args.get('cluster_ids', [])}
        self.check_links(uow, links['observation'], links['cluster'])
        proposal = apply_records(ctx, plan, uow)[0]
        self.write_links(uow, proposal['id'], links)
        proposal = self.issue(ctx, proposal, links, uow)
        return {'proposal': self.proposal_view(uow, proposal, detail=True)}

    @staticmethod
    def write_links(uow, proposal_id, links):
        repository = uow.repository(LINKS)
        for kind, identities in links.items():
            for identity in identities:
                repository.insert({'proposal_id': proposal_id, 'target_type': kind, 'target_id': identity})

    def issue(self, ctx, proposal, links, uow, note=None):
        """File a new approval request for a waiting proposal and record it on the proposal."""
        signoff = self.file_request(ctx, proposal, links, uow, note)
        return uow.repository(PROPOSALS).update(proposal['id'], {'signoff_id': signoff, 'requests': proposal['requests'] + 1})

    def revise_prepare(self, ctx, args, options, uow):
        ctx = applier(self.app, ctx, uow)
        proposal = uow.repository(PROPOSALS).get(args['proposal_id'])
        if proposal is None:
            raise DomainError('not_found', 'No delta proposal has this id')
        if proposal['state'] != WAITING:
            raise DomainError('invalid_transition', f'This proposal is {proposal["state"]}: only one waiting for approval is '
                                                    'revised. File a new proposal that names it as replaces_id.')
        links = self.links_of(uow, proposal['id'])
        wanted = {'observation': args.get('observation_ids', links['observation']), 'cluster': args.get('cluster_ids', links['cluster'])}
        self.check_links(uow, wanted['observation'], wanted['cluster'])
        changes = {key: value for key, value in (('title', ' '.join(args['title'].split()) if 'title' in args else None),
                                                  ('problem', args.get('problem', '').strip() or None),
                                                  ('change', args.get('change', '').strip() or None))
                   if value is not None and value != proposal[key]}
        same_links = all(sorted(wanted[kind]) == links[kind] for kind in links)
        request = self.request_of(uow, proposal)
        if not changes and same_links and request is not None and request['state'] == 'pending':
            raise DomainError('validation_error', 'Nothing changes, and the approval request is still open')
        builder = self.builder(ctx, 'agentic_delta_proposal_revise', args, 'Revise an agentic layer delta proposal')
        builder.record(PROPOSALS, proposal['id'], proposal, {**changes, 'updated_at': builder.at},
                       label=f'Delta proposal {proposal["number"]}')
        return builder.build()

    def revise_apply(self, ctx, plan, uow):
        ctx = applier(self.app, ctx, uow)
        args = plain(plan.arguments)
        before = uow.repository(PROPOSALS).get(args['proposal_id'])
        if before is None or before['state'] != WAITING:
            raise DomainError('invalid_transition', 'This proposal was decided meanwhile and is not revised')
        proposal = apply_records(ctx, plan, uow)[0]
        links = self.links_of(uow, proposal['id'])
        wanted = {'observation': sorted(args.get('observation_ids', links['observation'])),
                  'cluster': sorted(args.get('cluster_ids', links['cluster']))}
        self.check_links(uow, wanted['observation'], wanted['cluster'])
        if wanted != links:
            uow.connection.execute(f'DELETE FROM {LINKS} WHERE proposal_id=?', (proposal['id'],))
            self.write_links(uow, proposal['id'], wanted)
        request = self.request_of(uow, proposal)
        if request is not None and request['state'] == 'pending' and not request['superseded_by_id']:
            self.runtime.signoffs._update(request, {'state': 'cancelled'}, uow)
        proposal = self.issue(ctx, proposal, wanted, uow, args.get('note'))
        return {'proposal': self.proposal_view(uow, proposal, detail=True)}

    # ----- the owner's decision ------------------------------------------------------------------------------------------

    def signoff_updated(self, before, after, uow):
        """Called inside the transaction of every Signoff status update (``SignoffDomains.observe``). A proposal follows the
        owner's choice on its approval request. It never raises: a decision the owner made is never refused because of this
        record, and ``settle`` makes up for a miss. A rule's card is followed the same way (``rule_check_updated``, made up
        for by ``settle_checks``)."""
        if after.get('operation_name') == RULE_CHECK:
            try:
                self.rule_check_updated(before, after, uow)
            except Exception:  # noqa: BLE001 - see above
                LOG.warning('A rule did not follow the decision on its card %s', after.get('id'), exc_info=True)
            return
        if (after.get('category') != 'input_request' or after.get('state') != 'answered'
                or not str(after.get('request_key') or '').startswith(REQUEST_PREFIX)):
            return
        try:
            self.settle(uow, signoff_id=after['id'])
        except Exception:  # noqa: BLE001 - see above
            LOG.warning('A delta proposal did not follow the decision on Signoff %s', after.get('id'), exc_info=True)

    def settle(self, uow, *, signoff_id=None):
        """Set every waiting proposal whose approval request was answered with a choice to that choice. Returns the ids."""
        where, params = ('AND p.signoff_id=?', (signoff_id,)) if signoff_id else ('', ())
        rows = uow.connection.execute(
            f"SELECT p.id, p.signoff_id FROM {PROPOSALS} p JOIN signoffs s ON s.id=p.signoff_id "
            f"WHERE p.state='{WAITING}' AND s.state='answered' {where}", params).fetchall()
        changed = []
        for row in rows:
            decision = (uow.repository('signoffs', project_id=None).get(row['signoff_id']) or {}).get('decision_json') or {}
            state = {('approve',): APPROVED, ('reject',): REJECTED}.get(tuple(decision.get('selected_choices') or ()))
            if state is None:
                continue                              # answered in words: the proposal waits for a revision
            now = timestamp(self.clock)
            uow.repository(PROPOSALS).update(row['id'], {'state': state, 'decided_at': decision.get('at') or now,
                                                         'decided_by': decision.get('by'), 'updated_at': now})
            changed.append(row['id'])
        return changed

    # ----- reading proposals ---------------------------------------------------------------------------------------------

    def proposal_view(self, uow, row, *, detail=False):
        request = self.request_of(uow, row)
        open_request = bool(request and request['state'] == 'pending' and not request['superseded_by_id'])
        links = self.links_of(uow, row['id'])
        view = {'id': row['id'], 'number': row['number'], 'title': row['title'], 'state': row['state'],
                'created_at': row['created_at'], 'updated_at': row['updated_at'], 'decided_at': row['decided_at'],
                'replaces_id': row['replaces_id'], 'run_id': row['run_id'],
                'addresses': {'observations': len(links['observation']), 'clusters': len(links['cluster'])},
                'approval_request': {'signoff_id': row['signoff_id'], 'state': request['state'] if request else None,
                                     'open': open_request, 'requests_filed': row['requests']},
                # Waiting with no open request: the owner answered in words, or the request was withdrawn.
                'needs_revision': row['state'] == WAITING and not open_request}
        if not detail:
            return view
        clusters = [self.cluster_rows(uow, where='WHERE c.id=?', params=(identity,)) for identity in links['cluster']]
        view.update(problem=row['problem'], change=row['change'],
                    observations=[self.observation_view(uow, found) for found in
                                  (uow.repository(OBSERVATIONS).get(identity) for identity in links['observation']) if found],
                    clusters=[found[0] for found in clusters if found], owner=self.owner_words(uow, row))
        return view

    @staticmethod
    def owner_words(uow, proposal):
        """What was said about a proposal on its approval requests, oldest request first: the decision with its note, the
        notes, and the messages of each request's thread."""
        found = []
        for signoff in uow.connection.execute(
                "SELECT id FROM signoffs WHERE proposer_principal_id=? AND request_key LIKE ? ORDER BY created_at, id",
                (proposal['created_by'], f'{REQUEST_PREFIX}{proposal["id"]}:%')).fetchall():
            row = uow.repository('signoffs', project_id=None).get(signoff['id'])
            decision = row['decision_json'] or {}
            names = "(SELECT display_name FROM principals WHERE id={})"
            notes = [dict(note) for note in uow.connection.execute(
                f"SELECT n.id, n.body, n.created_at, {names.format('n.created_by')} AS by FROM signoff_notes n "
                "WHERE n.signoff_id=? ORDER BY n.created_at, n.id", (row['id'],))]
            thread = [dict(message) for message in uow.connection.execute(
                "SELECT m.author_role AS by, m.body, m.created_at FROM owner_conversation_messages m JOIN owner_conversations c "
                "ON c.id=m.conversation_id WHERE c.source_type='signoff' AND c.source_id=? AND c.peer_agent_id IS NULL "
                "ORDER BY m.sequence", (row['id'],))]
            found.append({'signoff_id': row['id'], 'state': row['state'], 'filed_at': row['created_at'],
                          'decision': ({'choices': decision.get('selected_choices') or [], 'at': decision.get('at'),
                                        'note': next((note['body'] for note in notes if note['id'] == decision.get('message_id')), None)}
                                       if decision else None),
                          'notes': notes, 'thread': thread})
        return found

    def proposals_list(self, ctx, args, options, uow):
        reader(self.app, ctx, uow)
        limit, offset = args.get('limit', 50), offset_of(args)
        where, params = ('WHERE state=?', [args['state']]) if args.get('state') else ('', [])
        rows = uow.connection.execute(f'SELECT id FROM {PROPOSALS} {where} ORDER BY number DESC LIMIT ? OFFSET ?',
                                      [*params, limit + 1, offset]).fetchall()
        counts = {state: 0 for state in PROPOSAL_STATES}
        counts.update({row['state']: row['n'] for row in uow.connection.execute(
            f'SELECT state, COUNT(*) AS n FROM {PROPOSALS} GROUP BY state')})
        repository = uow.repository(PROPOSALS)
        return {'proposals': [self.proposal_view(uow, repository.get(row['id'])) for row in rows[:limit]], 'counts': counts,
                **page(rows, offset, limit)}

    def proposal_get(self, ctx, args, options, uow):
        reader(self.app, ctx, uow)
        row = uow.repository(PROPOSALS).get(args['proposal_id'])
        if row is None:
            raise DomainError('not_found', 'No delta proposal has this id')
        return {'proposal': self.proposal_view(uow, row, detail=True)}


__all__ = ['Experience', 'INTERN', 'REQUEST_PREFIX', 'RULE_CHECK']
