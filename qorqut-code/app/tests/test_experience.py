"""The Learning Applier's two workflows over the real Signoff service: observations and their clusters, delta proposals
and the approval request each one gets, and what the owner's answer does to a proposal."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal, new_id, plain

from qorqut.domains.owner_inputs import OwnerInputs
from qorqut.experience.operations import Experience
from qorqut.experience.runs import Runs
from qorqut.signoffs.ports import HostFocus, SignoffDomains

from private_thread_runtime import build_runtime

SUNDAY = datetime(2026, 11, 22, 3, 0, 30, tzinfo=timezone.utc)      # half a minute past the daily 03:00


class Opener:
    """The session opener and its managed runtime, as ``Runs`` uses them: one agent, one conversation at a time."""

    def __init__(self):
        self.managed, self.wake, self.phase, self.conversation, self.opened, self.fail = self, True, 'idle', None, [], None

    def handle(self, agent_id):
        if self.conversation is None:
            return None
        return NS(client=NS(status=lambda: {'status': 'ok', 'state': 'ready', 'phase': self.phase, 'session_id': self.conversation}))

    def wakeable(self, agent_id):
        return self.wake

    def replace(self, profile, closing, compose, key):
        if self.fail:
            raise DomainError(self.fail, 'The session did not open')
        self.opened.append({'closing': closing, 'text': compose(), 'key': key})
        self.conversation = f'conversation-{len(self.opened)}'
        return self.conversation


class ExperienceCase(unittest.TestCase):
    """The tools registered on the private-thread test runtime, with a Learning Applier added. Holds no test of its own."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        self.now, self.tick, self.archives, self.opener = SUNDAY, 0.0, {}, Opener()
        runtime.native_chat = NS(indexer=NS(archive=lambda profile: self.archives[profile['agent_id']]), opener=self.opener)
        runtime.application.attention = NS(legacy=NS(resolve=lambda ctx, found, uow: {**found, 'label': 'A record', 'project_slug': None}))
        OwnerInputs(runtime, clock=lambda: self.now).register()
        self.owner, self.lead, self.worker = (ctx.principal_id for ctx in (runtime.owner, runtime.coordinator, runtime.worker))
        self.applier = self.agent('learning-applier', 'Overseer')
        contexts = {ctx.principal_id: ctx for ctx in (runtime.owner, runtime.coordinator, runtime.worker, self.applier)}
        runtime.application.policy.context_for = lambda agent, **_: contexts[agent]
        # As the host's policy does (``composition.policy._experience``), the owner's check of a rule is always held.
        allow, self.held = runtime.application.policy.resolve, {'experience_rule_check'}
        runtime.application.policy.resolve = lambda ctx, descriptor, targets, uow: (
            PolicyDecision('signoff', 'test', {'global_capability': descriptor.global_capability})
            if descriptor.name in self.held else allow(ctx, descriptor, targets, uow))
        # The Signoff service tells the Focus port of every status change; the host adds the proposals' observer to it.
        domains = SignoffDomains().bind(focus_board=NS(release_signoff=lambda *a: None))
        runtime.signoffs.ports = replace(runtime.signoffs.ports, focus=HostFocus(domains))
        self.runs = Runs(runtime, clock=lambda: self.now, monotonic=lambda: self.tick,
                         spawn=lambda target, *args: target(*args), environ={}, zone='UTC')
        self.experience = Experience(runtime, clock=lambda: self.now, runs=self.runs).register()
        domains.observe(self.experience.signoff_updated)
        profile = patch('qorqut.native_chat.store.active_profile', lambda conn, agent, *a, **k: {'agent_id': agent})
        profile.start()
        self.addCleanup(profile.stop)

    # ----- fixtures --------------------------------------------------------------------------------------------------

    def agent(self, role, name):
        identity = new_id()
        with self.runtime.database.transaction() as uow:
            uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': name})
            uow.repository('agent_profiles', project_id=None).insert({
                'agent_id': identity, 'principal_id': identity, 'name': role, 'workspace': '/' + role,
                'status': 'active', 'profile_json': {'name': role, 'status': 'active', 'role': {'id': role}, 'runtime': 'claude-code',
                                                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
        return InvocationContext(Principal(identity, 'agent'), source='mcp', profile_revision=1,
                                 global_capabilities={'read_global_context'})

    def call(self, operation, args=None, *, ctx=None):
        return self.runtime.application.invoke(ctx or self.applier, operation, {'scope': 'global', **(args or {})},
                                               InvocationOptions(idempotency_key=new_id()))

    def ok(self, operation, args=None, *, ctx=None):
        result = self.call(operation, args, ctx=ctx)
        self.assertEqual(result.status, 'ok', result.error and plain(result.error))
        return plain(result.data)

    def refused(self, operation, args=None, *, ctx=None, code='validation_error', says=''):
        result = self.call(operation, args, ctx=ctx)
        self.assertEqual((result.status, result.error and result.error['code']), ('error', code), result.error and plain(result.error))
        self.assertIn(says, result.error['message'])
        return plain(result.error)

    def pass_(self, seconds=100):
        """One look of the backend at the queue, later."""
        self.tick += seconds
        self.runs.step()

    def start(self, kind):
        """Queue a run of this kind now and start it; returns its id."""
        run = self.ok('experience_run_request', {'kind': kind}, ctx=self.runtime.owner)['run']
        self.pass_()        # marks it running and opens the fresh conversation
        self.pass_()        # records the conversation
        self.assertEqual(self.row(run['id'])['state'], 'running')
        return run['id']

    def row(self, run_id):
        with self.runtime.database.read() as uow:
            return uow.repository('experience_runs').get(run_id)

    def observation(self, text='Ordubashy said the invoice was sent; it was not.', **more):
        return {'text': text, 'kind': 'agent_failure', 'subject': 'Ordubashy', 'evidence': [{'ref': 'chat:abc123', 'quote': 'sent'}],
                **more}

    def file(self, run, *observations):
        return self.ok('experience_observations_submit', {'run_id': run, 'observations': list(observations)})

    def signoff(self, identity, ctx=None):
        return self.ok('signoffs_get', {'signoff_id': identity}, ctx=ctx or self.runtime.owner)['signoff']

    def answer(self, signoff_id, **response):
        signoff = self.signoff(signoff_id)
        result = self.runtime.signoffs.respond(
            self.runtime.owner, signoff_id, proposal_revision=signoff['proposal_revision'], payload_hash=signoff['payload_hash'],
            expected_status_revision=signoff['status_revision'], idempotency_key=new_id(), **response)
        self.assertEqual(result.status, 'ok', result.error and plain(result.error))

    def proposal(self, identity):
        return self.ok('agentic_delta_proposal_get', {'proposal_id': identity})['proposal']

    def proposed(self, **more):
        """An extraction that filed two observations under one cluster, then a review that proposed a change for it."""
        extraction = self.start('extraction')
        filed = self.file(extraction, self.observation(new_cluster={'label': 'Ordubashy reports work done that is not done'}),
                          self.observation('Ordubashy told the owner the form was filed; the form was still a draft.',
                                           new_cluster={'label': 'ordubashy reports work done that is not done'}))
        end = self.ok('experience_conversations', {'run_id': extraction})['end_token']
        self.ok('experience_run_finish', {'run_id': extraction, 'summary': 'Read one day.', 'conversations_end': end})
        self.now += timedelta(hours=3)
        review = self.start('review')
        cluster = filed['observations'][0]['cluster']['id']
        made = self.ok('agentic_delta_proposal_submit', {
            'run_id': review, 'title': 'Check the record before reporting work as done',
            'problem': 'Ordubashy reports work as done from memory, and twice it was not done.',
            'change': 'In the Ordubashy brief, require reading the Task or the sent item before telling the owner it is done.',
            'cluster_ids': [cluster], 'observation_ids': [filed['observations'][1]['id']], **more})['proposal']
        return review, cluster, made


class ObservationTests(ExperienceCase):
    def test_observations_are_filed_under_a_cluster_and_clusters_rank_by_how_often(self):
        run = self.start('extraction')
        first = self.file(run, self.observation(new_cluster={'label': 'Ordubashy reports work done that is not done',
                                                             'description': 'Says done before checking the record.'}),
                          self.observation('The owner had to send the file himself.', kind='owner_complaint',
                                           new_cluster={'label': 'Ordubashy hands work back to the owner'}))
        self.assertEqual((first['filed'], [cluster['label'] for cluster in first['new_clusters']]),
                         (2, ['Ordubashy reports work done that is not done', 'Ordubashy hands work back to the owner']))
        done, handed = (item['cluster']['id'] for item in first['observations'])
        # The same problem again: under the existing cluster by its id, and by a label that differs only in case and spacing.
        again = self.file(run, self.observation('Again reported as sent.', cluster_id=done),
                          self.observation('And again.', new_cluster={'label': 'ordubashy  reports work DONE that is not done'}))
        self.assertEqual((again['new_clusters'], [item['cluster'] for item in again['observations']][-1]),
                         ([], {'id': done, 'label': 'Ordubashy reports work done that is not done', 'observations': 3}))
        listed = self.ok('experience_clusters_list', ctx=self.runtime.coordinator)
        self.assertEqual([(cluster['rank'], cluster['id'], cluster['observations'], cluster['unaddressed']) for cluster in listed['clusters']],
                         [(1, done, 3, 3), (2, handed, 1, 1)])
        self.assertEqual((listed['clusters_total'], listed['observations_total']), (2, 4))
        one = self.ok('experience_cluster_get', {'cluster_id': handed}, ctx=self.runtime.owner)
        self.assertEqual([(item['kind'], item['subject'], item['text'], item['evidence']) for item in one['observations']],
                         [('owner_complaint', 'Ordubashy', 'The owner had to send the file himself.', [{'ref': 'chat:abc123', 'quote': 'sent'}])])
        self.assertEqual(self.ok('experience_run_get')['running']['filed_so_far'], {'observations_filed': 4, 'clusters_created': 2})

    def test_a_filing_is_refused_whole_with_every_reason(self):
        run = self.start('extraction')
        error = self.refused('experience_observations_submit', {'run_id': run, 'observations': [
            self.observation(), self.observation(cluster_id='missing'),
            self.observation(cluster_id='missing', new_cluster={'label': 'Both at once'}),
            self.observation(new_cluster={'label': 'A fine one'}, occurred_at='last Tuesday')]}, says='Nothing was filed.')
        self.assertEqual(len(error['details']['problems']), 4)
        self.assertEqual(self.ok('experience_clusters_list')['clusters'], [])

    def test_only_the_learning_applier_files_and_only_in_a_running_extraction(self):
        run = self.start('extraction')
        arguments = {'run_id': run, 'observations': [self.observation(new_cluster={'label': 'A problem'})]}
        for ctx in (self.runtime.owner, self.runtime.coordinator, self.runtime.worker):
            self.refused('experience_observations_submit', arguments, ctx=ctx, code='forbidden')
        self.refused('experience_clusters_list', ctx=self.runtime.worker, code='forbidden')
        # An extraction ends only once the report of conversations was read to its last page.
        self.refused('experience_run_finish', {'run_id': run, 'summary': 'Done.'}, says='read to its end')
        self.refused('experience_run_finish', {'run_id': run, 'summary': 'Done.', 'conversations_end': 'guess'}, says='read to its end')
        end = self.ok('experience_conversations', {'run_id': run})['end_token']
        finished = self.ok('experience_run_finish', {'run_id': run, 'summary': 'Read one quiet day.', 'conversations_end': end})['run']
        self.assertEqual((finished['state'], finished['result']),
                         ('done', {'summary': 'Read one quiet day.', 'observations_filed': 0, 'clusters_created': 0}))
        self.refused('experience_observations_submit', arguments, code='invalid_transition', says='not running')
        review = self.start('review')
        self.refused('experience_observations_submit', {**arguments, 'run_id': review}, says='belongs to the experience extraction')


class ProposalTests(ExperienceCase):
    def test_a_delta_proposal_links_what_it_addresses_and_gets_an_approval_request_for_the_owner(self):
        review, cluster, made = self.proposed()
        self.assertEqual((made['number'], made['state'], made['addresses'], made['needs_revision']),
                         (1, 'waiting_for_approval', {'observations': 1, 'clusters': 1}, False))
        self.assertEqual(([item['label'] for item in made['clusters']], [item['text'] for item in made['observations']]),
                         (['Ordubashy reports work done that is not done'],
                          ['Ordubashy told the owner the form was filed; the form was still a draft.']))
        request = made['approval_request']
        self.assertEqual((request['state'], request['open'], request['requests_filed']), ('pending', True, 1))
        # The request is a Signoff of the Learning Applier's that the owner can open and decide.
        signoff = self.signoff(request['signoff_id'])
        self.assertEqual((signoff['category'], signoff['state'], signoff['proposer_principal_id'], signoff['actionable']),
                         ('input_request', 'pending', self.applier.principal_id, True))
        self.assertEqual(signoff['title'], 'Delta proposal 1: Check the record before reporting work as done')
        self.assertEqual(([choice['id'] for choice in signoff['input']['choices']], signoff['input']['response_policy']),
                         (['approve', 'reject'], 'owner_only'))
        self.assertEqual((signoff['attention']['notify'], signoff['attention']['urgent'], signoff['attention']['blocking']),
                         (True, False, False))
        for part in ('Problem\nOrdubashy reports work as done from memory', 'Proposed change\nIn the Ordubashy brief',
                     '- Cluster "Ordubashy reports work done that is not done" (2 observations)',
                     '- Observation on Ordubashy: Ordubashy told the owner the form was filed'):
            self.assertIn(part, signoff['input']['explanation'])
        # Both observations of the cluster are now addressed, and the cluster names the proposal.
        listed = self.ok('experience_clusters_list')['clusters'][0]
        self.assertEqual((listed['unaddressed'], [(item['number'], item['state']) for item in listed['delta_proposals']]),
                         (0, [(1, 'waiting_for_approval')]))
        self.assertEqual(self.ok('experience_run_get')['running']['filed_so_far'], {'delta_proposals_filed': 1})
        self.assertEqual(self.ok('experience_run_finish', {'run_id': review, 'summary': 'One proposal.'})['run']['state'], 'done')

    def test_the_owners_choice_sets_the_proposal_approved_or_rejected(self):
        _, _, first = self.proposed()
        self.answer(first['approval_request']['signoff_id'], selected_choices=['approve'], notes='Yes, do it.')
        approved = self.proposal(first['id'])
        self.assertEqual((approved['state'], approved['needs_revision'], approved['approval_request']['state']),
                         ('approved', False, 'answered'))
        self.assertTrue(approved['decided_at'])
        self.assertEqual(approved['owner'][0]['decision'], {'choices': ['approve'], 'at': approved['decided_at'], 'note': 'Yes, do it.'})
        with self.runtime.database.read() as uow:
            self.assertEqual(uow.repository('agentic_delta_proposals').get(first['id'])['decided_by'], self.owner)
        # A decided proposal is not revised.
        self.refused('agentic_delta_proposal_revise', {'proposal_id': first['id'], 'title': 'Another title'},
                     code='invalid_transition', says='approved')
        review = self.row(first['run_id'])['id']
        second = self.ok('agentic_delta_proposal_submit', {
            'run_id': review, 'title': 'A second one', 'problem': 'The same problem, seen from the tool side.',
            'change': 'Rename the tool so its name says what it does.', 'observation_ids': [first['observations'][0]['id']]})['proposal']
        self.answer(second['approval_request']['signoff_id'], selected_choices=['reject'])
        self.assertEqual([(item['number'], item['state']) for item in self.ok('agentic_delta_proposals_list', ctx=self.runtime.owner)['proposals']],
                         [(2, 'rejected'), (1, 'approved')])
        self.assertEqual(self.ok('agentic_delta_proposals_list', {'state': 'rejected'})['counts'],
                         {'waiting_for_approval': 0, 'approved': 1, 'rejected': 1})
        # A rejected proposal no longer addresses anything; a new proposal may name it as the one it replaces.
        self.refused('agentic_delta_proposal_submit', {
            'run_id': review, 'title': 'Third', 'problem': 'The problem again.', 'change': 'Another way to solve it.',
            'observation_ids': [first['observations'][0]['id']], 'replaces_id': 'missing'}, says='names no delta proposal')

    def test_an_answer_in_words_leaves_the_proposal_waiting_until_it_is_revised(self):
        _, cluster, made = self.proposed()
        old = made['approval_request']['signoff_id']
        self.answer(old, notes='Not the brief. Put it in the skill instead.')
        waiting = self.proposal(made['id'])
        self.assertEqual((waiting['state'], waiting['needs_revision'], waiting['approval_request']['open']),
                         ('waiting_for_approval', True, False))
        self.assertEqual(waiting['owner'][0]['decision']['note'], 'Not the brief. Put it in the skill instead.')
        revised = self.ok('agentic_delta_proposal_revise', {
            'proposal_id': made['id'], 'change': 'In the reporting skill, require reading the Task before telling the owner it is done.',
            'cluster_ids': [cluster], 'observation_ids': [], 'note': 'Moved from the brief to the skill, as the owner asked.'})['proposal']
        fresh = revised['approval_request']
        self.assertEqual((fresh['open'], fresh['requests_filed'], fresh['signoff_id'] != old, revised['addresses']),
                         (True, 2, True, {'observations': 0, 'clusters': 1}))
        explanation = self.signoff(fresh['signoff_id'])['input']['explanation']
        self.assertIn('What changed in this revision\nMoved from the brief to the skill, as the owner asked.', explanation)
        self.assertIn('In the reporting skill', explanation)
        self.assertEqual([entry['signoff_id'] for entry in revised['owner']], [old, fresh['signoff_id']])
        # Revising while the request is open withdraws it and files another; nothing to change is refused.
        self.refused('agentic_delta_proposal_revise', {'proposal_id': made['id']}, says='Nothing changes')
        again = self.ok('agentic_delta_proposal_revise', {'proposal_id': made['id'], 'title': 'Check the record first'})['proposal']
        self.assertEqual((self.signoff(fresh['signoff_id'])['state'], again['approval_request']['requests_filed']), ('cancelled', 3))
        self.answer(again['approval_request']['signoff_id'], selected_choices=['approve'])
        self.assertEqual(self.proposal(made['id'])['state'], 'approved')

    def test_a_proposal_names_real_observations_and_is_filed_only_in_a_running_review(self):
        review, cluster, made = self.proposed()
        base = {'run_id': review, 'title': 'Another', 'problem': 'A problem that was observed.', 'change': 'A change to make to it.'}
        self.refused('agentic_delta_proposal_submit', base, says='at least one observation or cluster')
        self.refused('agentic_delta_proposal_submit', {**base, 'cluster_ids': ['missing'], 'observation_ids': ['gone']},
                     says='No observation has the id gone. No cluster has the id missing.')
        self.refused('agentic_delta_proposal_submit', {**base, 'cluster_ids': [cluster], 'replaces_id': made['id']},
                     says='still waiting for approval')
        self.refused('agentic_delta_proposal_submit', {**base, 'cluster_ids': [cluster]}, ctx=self.runtime.coordinator, code='forbidden')
        self.ok('experience_run_finish', {'run_id': review, 'summary': 'One proposal.'})
        self.refused('agentic_delta_proposal_submit', {**base, 'cluster_ids': [cluster]}, code='invalid_transition')
        self.assertEqual(len(self.ok('agentic_delta_proposals_list')['proposals']), 1)

    def test_a_decision_the_observer_missed_is_made_up_for(self):
        _, _, made = self.proposed()
        with patch.object(self.experience, 'settle', side_effect=RuntimeError('storage hiccup')), \
                self.assertLogs('qorqut.experience.operations', 'WARNING'):
            self.answer(made['approval_request']['signoff_id'], selected_choices=['reject'], notes='No.')
        # The owner's answer stood although the proposal did not follow it.
        self.assertEqual((self.signoff(made['approval_request']['signoff_id'])['state'], self.proposal(made['id'])['state']),
                         ('answered', 'waiting_for_approval'))
        with self.runtime.database.transaction() as uow:
            self.assertEqual(self.experience.settle(uow), [made['id']])
        self.assertEqual(self.proposal(made['id'])['state'], 'rejected')


class PolicyTests(ExperienceCase):
    def decision(self, name, role, *, defaults=None, overrides=None):
        from qorqut.composition.policy import resolve_operation_policy
        return resolve_operation_policy(name, defaults or {}, overrides or {}, self.runtime.registry, role_id=role, scope='global').mode

    def test_the_tools_of_a_run_are_the_learning_appliers_and_the_reads_also_the_coordinators(self):
        from qorqut.composition.policy import EXPERIENCE_FILING, EXPERIENCE_READS, EXPERIENCE_RULE_CHECK, EXPERIENCE_WORK
        registered = {entry.name for entry in self.runtime.registry.entries()
                      if entry.name.startswith(('experience_', 'agentic_delta_proposal'))}
        self.assertEqual(registered, EXPERIENCE_WORK | EXPERIENCE_READS | EXPERIENCE_FILING | {EXPERIENCE_RULE_CHECK})
        for name in EXPERIENCE_WORK:
            self.assertEqual([self.decision(name, role) for role in ('learning-applier', 'ordubashy', 'global-worker', None)],
                             ['allow', 'deny', 'deny', 'deny'], name)
        for name in EXPERIENCE_READS:
            self.assertEqual([self.decision(name, role) for role in ('learning-applier', 'ordubashy', 'global-worker', None)],
                             ['allow', 'allow', 'deny', 'deny'], name)
        # No role file and no override gives a tool to another role; a deny in either is respected.
        self.assertEqual(self.decision('experience_conversations', 'ordubashy', overrides={'experience_conversations': 'allow'}), 'deny')
        self.assertEqual(self.decision('experience_clusters_list', 'global-worker', defaults={'experience_clusters_list': 'allow'}), 'deny')
        self.assertEqual(self.decision('experience_clusters_list', 'ordubashy', overrides={'experience_clusters_list': 'deny'}), 'deny')
        self.assertEqual(self.decision('experience_run_finish', 'learning-applier', defaults={'experience_run_finish': 'deny'}), 'deny')


class FilingTests(ExperienceCase):
    def point(self, text='The owner said: never send an email without a Signoff.', **more):
        return {'scope': 'global', 'kind': 'owner_complaint', 'subject': 'all agents', 'text': text, **more}

    def stored(self, **where):
        with self.runtime.database.read() as uow:
            rows = uow.connection.execute('SELECT * FROM experience_observations').fetchall()
        return [row for row in rows if all(row[key] == value for key, value in where.items())]

    def count(self, table):
        with self.runtime.database.read() as uow:
            return uow.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def clusters(self):
        with self.runtime.database.read() as uow:
            return [dict(row) for row in uow.connection.execute('SELECT * FROM experience_clusters').fetchall()]

    def test_a_worker_the_coordinator_and_the_learning_applier_each_file_a_point_at_once(self):
        from qorqut.experience.operations import AGENTS_CLUSTER, FILED_NOTE
        for ctx, text, extra in ((self.runtime.worker, '  Rule number one.  ', {}), (self.runtime.coordinator, 'Rule number two.', {}),
                                 (self.applier, 'Rule number three.', {'evidence': [{'ref': 'chat:abc123', 'quote': 'never'}]})):
            data = self.ok('experience_observation_file', self.point(text, kind='agent_failure', subject='  all   agents ', **extra), ctx=ctx)
            row = self.stored(created_by=ctx.principal_id)[0]
            self.assertEqual(data, {'filed': {'id': row['id'], 'kind': 'agent_failure', 'subject': 'all agents',
                                              'filed_at': row['created_at']}, 'note': FILED_NOTE})
            self.assertEqual((row['run_id'], row['cluster_id'], row['kind'], row['subject'], row['text'], json.loads(row['evidence_json'])),
                             (None, AGENTS_CLUSTER, 'agent_failure', 'all agents', text.strip(), extra.get('evidence', [])))
            self.assertEqual((row['owner_check'], row['check_signoff_id']), (None, None))

    def test_the_first_filing_creates_the_standing_cluster_and_a_later_one_by_another_agent_reuses_it(self):
        from qorqut.experience.operations import (AGENTS_CLUSTER, AGENTS_CLUSTER_DESCRIPTION, AGENTS_CLUSTER_LABEL)
        self.assertEqual(self.clusters(), [])
        self.ok('experience_observation_file', self.point('First point filed.'), ctx=self.runtime.worker)
        (cluster,) = self.clusters()
        self.assertEqual((cluster['id'], cluster['label'], cluster['description'], cluster['run_id']),
                         (AGENTS_CLUSTER, AGENTS_CLUSTER_LABEL, AGENTS_CLUSTER_DESCRIPTION, None))
        self.ok('experience_observation_file', self.point('Second point filed.'), ctx=self.runtime.coordinator)
        self.assertEqual(([item['id'] for item in self.clusters()], len(self.stored(cluster_id=AGENTS_CLUSTER))), ([AGENTS_CLUSTER], 2))

    def test_a_cluster_with_the_same_label_in_another_letter_case_is_used_instead_of_a_second_one(self):
        extraction = self.start('extraction')
        made = self.file(extraction, self.observation(new_cluster={'label': 'filed by agents'}))['new_clusters'][0]['id']
        self.ok('experience_observation_file', self.point(), ctx=self.runtime.worker)
        self.assertEqual([item['id'] for item in self.clusters()], [made])
        self.assertEqual([row['run_id'] for row in self.stored(cluster_id=made, created_by=self.runtime.worker.principal_id)], [None])

    def test_the_same_agent_filing_the_same_text_twice_is_refused_but_another_agent_may_file_it(self):
        self.ok('experience_observation_file', self.point(), ctx=self.runtime.worker)
        self.refused('experience_observation_file', self.point(' ' + self.point()['text'] + ' '), ctx=self.runtime.worker,
                     says='filed this point already')
        self.ok('experience_observation_file', self.point(), ctx=self.runtime.coordinator)
        self.assertEqual(len(self.stored()), 2)

    def test_the_owner_does_not_file_a_point(self):
        from qorqut.experience.access import filer
        result = self.call('experience_observation_file', self.point(), ctx=self.runtime.owner)
        self.assertEqual((result.status, result.error['code']), ('error', 'forbidden'), result.error)
        self.assertEqual(self.stored(), [])
        with self.runtime.database.read() as uow, self.assertRaises(DomainError) as raised:
            filer(self.runtime.application, self.runtime.owner, uow)
        self.assertEqual(raised.exception.code, 'forbidden')

    def test_an_occurred_at_that_is_not_an_iso_date_is_refused_and_a_valid_one_is_stored(self):
        self.refused('experience_observation_file', self.point(occurred_at='last Tuesday'), ctx=self.runtime.worker, says='ISO-8601')
        self.assertEqual(self.stored(), [])
        self.ok('experience_observation_file', self.point(occurred_at='2026-11-20T10:00:00Z'), ctx=self.runtime.worker)
        self.assertEqual([row['occurred_at'] for row in self.stored()], ['2026-11-20T10:00:00Z'])

    def test_filing_anything_but_an_owner_rule_creates_no_signoff_and_needs_no_run(self):
        before = self.count('signoffs')
        for kind in ('agent_failure', 'tool_problem', 'tool_pattern', 'other'):
            self.ok('experience_observation_file', self.point(f'A point of kind {kind}.', kind=kind), ctx=self.runtime.worker)
        self.assertEqual((self.count('signoffs'), self.count('experience_runs')), (before, 0))

    def test_the_standing_cluster_shows_who_filed_each_observation_and_the_run_of_those_filed_in_one(self):
        from qorqut.experience.operations import AGENTS_CLUSTER
        self.ok('experience_observation_file', self.point(), ctx=self.runtime.worker)
        shown = self.ok('experience_cluster_get', {'cluster_id': AGENTS_CLUSTER}, ctx=self.runtime.coordinator)['observations']
        with self.runtime.database.read() as uow:
            name = uow.connection.execute('SELECT display_name FROM principals WHERE id=?', (self.worker,)).fetchone()[0]
        self.assertEqual([(item['filed_by'], item['run_id']) for item in shown], [(name, None)])
        extraction = self.start('extraction')
        filed = self.file(extraction, self.observation(new_cluster={'label': 'A problem'}))['observations'][0]['cluster']['id']
        item = self.ok('experience_cluster_get', {'cluster_id': filed}, ctx=self.runtime.coordinator)['observations'][0]
        self.assertEqual((item['filed_by'], item['run_id']), ('Overseer', extraction))

    def test_a_delta_proposal_names_filed_observations_but_never_the_standing_cluster(self):
        from qorqut.experience.operations import AGENTS_CLUSTER
        extraction = self.start('extraction')
        end = self.ok('experience_conversations', {'run_id': extraction})['end_token']
        self.ok('experience_run_finish', {'run_id': extraction, 'summary': 'Read one day.', 'conversations_end': end})
        observation = self.ok('experience_observation_file', self.point(), ctx=self.runtime.worker)['filed']['id']
        self.now += timedelta(hours=3)
        review = self.start('review')
        base = {'run_id': review, 'title': 'Ask for a Signoff before sending mail', 'change': 'In the mail skill, require a Signoff first.',
                'problem': 'Agents sent mail the owner had not approved.'}
        self.refused('agentic_delta_proposal_submit', {**base, 'cluster_ids': [AGENTS_CLUSTER]}, says='not the cluster')
        before = self.ok('experience_clusters_list')['clusters'][0]['unaddressed']
        self.ok('agentic_delta_proposal_submit', {**base, 'observation_ids': [observation]})
        self.assertEqual(self.ok('experience_clusters_list')['clusters'][0]['unaddressed'], before - 1)

    def test_every_role_may_file_unless_a_deny_says_otherwise_and_the_other_write_tools_stay_denied(self):
        from qorqut.composition.policy import resolve_operation_policy

        def mode(name, role, **settings):
            return resolve_operation_policy(name, settings.get('defaults', {}), settings.get('overrides', {}), self.runtime.registry,
                                            role_id=role, scope='global').mode
        for role in ('global-worker', 'ordubashy', 'learning-applier'):
            self.assertEqual(mode('experience_observation_file', role), 'allow', role)
            self.assertEqual(mode('experience_observation_file', role, overrides={'experience_observation_file': 'deny'}), 'deny', role)
        self.assertEqual(mode('experience_observations_submit', 'global-worker'), 'deny')



class RuleCheckTests(ExperienceCase):
    """The owner's check of a rule an agent filed from the owner's words: the card, and what each answer does."""

    RULE = 'Never send an email without a Signoff.'

    def file_rule(self, text=RULE, ctx=None, **more):
        args = {'kind': 'owner_complaint', 'subject': 'all agents', 'text': text, **more}
        return self.ok('experience_observation_file', args, ctx=ctx or self.runtime.worker)

    def rule(self, identity):
        with self.runtime.database.read() as uow:
            row = uow.connection.execute('SELECT * FROM experience_observations WHERE id=?', (identity,)).fetchone()
        return dict(row) if row else None

    def decide(self, signoff_id, decision, feedback=None):
        signoff = self.signoff(signoff_id)
        result = self.runtime.signoffs.review(
            self.runtime.owner, signoff_id, decision=decision, proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'], idempotency_key=new_id(),
            feedback=feedback)
        self.assertEqual(result.status, 'accepted' if decision == 'approve' else 'ok', result.error and plain(result.error))
        return result

    def edit(self, signoff_id, text):
        signoff = self.signoff(signoff_id)
        result = self.runtime.signoffs.amend(
            self.runtime.owner, signoff_id, changes={'text': text}, proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'], idempotency_key=new_id())
        self.assertEqual(result.status, 'ok', result.error and plain(result.error))
        return plain(result.data)['signoff']['id']

    def test_an_owner_rule_gets_one_card_in_the_same_filing_and_the_agent_is_told(self):
        from qorqut.experience.operations import CHECK_NOTE
        data = self.file_rule()
        row = self.rule(data['filed']['id'])
        self.assertEqual(data['owner_check'], {'state': 'unchecked', 'signoff_id': row['check_signoff_id']})
        self.assertEqual(data['note'], CHECK_NOTE)
        self.assertIn('do not ask the owner about it in chat', CHECK_NOTE)
        self.assertEqual((row['owner_check'], row['checked_at']), ('unchecked', None))
        signoff = self.signoff(row['check_signoff_id'])
        self.assertEqual((signoff['operation_name'], signoff['state'], signoff['category'], signoff['proposer_agent_id']),
                         ('experience_rule_check', 'pending', 'internal_change', self.worker))
        self.assertEqual(signoff['preview']['title'], 'Approve proposed learning')
        (preview,) = signoff['preview']['changes']
        self.assertEqual(preview['resource'], {'resource_type': 'experience_rule', 'resource_id': row['id'], 'label': 'Rule'})
        with self.runtime.database.read() as uow:
            name = uow.connection.execute('SELECT display_name FROM principals WHERE id=?', (self.worker,)).fetchone()[0]
        self.assertEqual({field['path']: field['after'] for field in preview['fields']},
                         {'text': self.RULE, 'applies_to': 'all agents', 'filed_by': name})
        # A rejection needs no note on this card.
        self.assertFalse(signoff['rejection_requires_message'])
        self.assertIn('No instruction, skill or tool changes until the weekly review', ' '.join(signoff['preview']['effects']))
        self.assertEqual(signoff['editable_fields'], ['text'])

    def test_approving_marks_the_rule_and_changes_nothing_else(self):
        filed = self.file_rule()['filed']['id']
        card = self.rule(filed)['check_signoff_id']
        self.decide(card, 'approve')
        self.runtime.signoffs.run_pending()
        row = self.rule(filed)
        self.assertEqual((row['owner_check'], row['text']), ('approved', self.RULE))
        self.assertIsNotNone(row['checked_at'])
        self.assertEqual(self.signoff(card)['state'], 'applied')
        shown = self.ok('experience_cluster_get', {'cluster_id': row['cluster_id']}, ctx=self.runtime.coordinator)['observations'][0]
        self.assertEqual((shown['owner_check'], shown['text'], shown['check_signoff_id']), ('approved', self.RULE, card))

    def test_an_edited_rule_replaces_the_agents_wording_on_approval(self):
        filed = self.file_rule()['filed']['id']
        first = self.rule(filed)['check_signoff_id']
        self.assertEqual(self.runtime.signoffs.amend(
            self.runtime.owner, first, changes={'text': '   '}, **{key: self.signoff(first)[key] for key in (
                'proposal_revision', 'payload_hash')}, expected_status_revision=self.signoff(first)['status_revision'],
            idempotency_key=new_id()).error['code'], 'validation_error')
        second = self.edit(first, '  Never send any email or message without a Signoff.  ')
        self.assertNotEqual(second, first)
        self.assertEqual((self.signoff(first)['state'], self.rule(filed)['check_signoff_id']), ('conflicted', second))
        self.assertEqual(self.rule(filed)['text'], self.RULE)        # nothing changes before the approval
        self.decide(second, 'approve')
        self.runtime.signoffs.run_pending()
        row = self.rule(filed)
        self.assertEqual((row['owner_check'], row['text']), ('approved', 'Never send any email or message without a Signoff.'))

    def test_rejecting_removes_the_rule_and_the_note_is_the_filing_agents_to_read(self):
        filed = self.file_rule()['filed']['id']
        card = self.rule(filed)['check_signoff_id']
        self.decide(card, 'reject', feedback='That is not what I said.')
        self.assertIsNone(self.rule(filed))
        signoff = self.signoff(card, ctx=self.runtime.worker)
        self.assertEqual((signoff['state'], signoff['proposer_agent_id']), ('rejected', self.worker))
        self.assertIn('That is not what I said.', json.dumps(signoff))
        # Without a note the rule is removed the same way.
        other = self.file_rule('Always reply in English.')['filed']['id']
        self.decide(self.rule(other)['check_signoff_id'], 'reject')
        self.assertIsNone(self.rule(other))

    def test_rejecting_a_rule_a_delta_proposal_already_names_marks_it_instead(self):
        extraction = self.start('extraction')
        end = self.ok('experience_conversations', {'run_id': extraction})['end_token']
        self.ok('experience_run_finish', {'run_id': extraction, 'summary': 'Read one day.', 'conversations_end': end})
        filed = self.file_rule()['filed']['id']
        self.now += timedelta(hours=3)
        review = self.start('review')
        self.ok('agentic_delta_proposal_submit', {'run_id': review, 'title': 'Ask for a Signoff before sending mail',
                                                  'change': 'In the mail skill, require a Signoff first.',
                                                  'problem': 'Agents sent mail the owner had not approved.', 'observation_ids': [filed]})
        self.decide(self.rule(filed)['check_signoff_id'], 'reject')
        row = self.rule(filed)
        self.assertEqual((row['owner_check'], row['text']), ('rejected', self.RULE))
        self.assertIsNotNone(row['checked_at'])

    def test_with_no_answer_the_rule_stays_in_the_queue_unchecked(self):
        from qorqut.experience.operations import AGENTS_CLUSTER
        filed = self.file_rule()['filed']['id']
        self.runtime.signoffs.run_pending()
        self.assertEqual(self.rule(filed)['owner_check'], 'unchecked')
        shown = self.ok('experience_cluster_get', {'cluster_id': AGENTS_CLUSTER}, ctx=self.runtime.coordinator)['observations']
        self.assertEqual([(item['id'], item['owner_check']) for item in shown], [(filed, 'unchecked')])

    def test_a_repeat_filing_is_still_refused_and_asks_no_second_card(self):
        self.file_rule()
        before = self.count_signoffs()
        self.refused('experience_observation_file', {'kind': 'owner_complaint', 'subject': 'all agents', 'text': self.RULE},
                     ctx=self.runtime.worker, says='filed this point already')
        self.assertEqual(self.count_signoffs(), before)

    def count_signoffs(self):
        with self.runtime.database.read() as uow:
            return uow.connection.execute("SELECT count(*) FROM signoffs WHERE operation_name='experience_rule_check'").fetchone()[0]

    def test_no_agent_calls_the_check_itself(self):
        filed = self.file_rule()['filed']['id']
        before = self.count_signoffs()
        self.refused('experience_rule_check', {'observation_id': filed, 'text': 'My own better wording.'}, ctx=self.runtime.worker,
                     code='invalid_transition', says='already asked')
        self.refused('experience_rule_check', {'observation_id': filed, 'text': 'My own better wording.'},
                     ctx=self.runtime.coordinator, code='not_found')
        self.assertEqual(self.count_signoffs(), before)
        from qorqut.transport.mcp import RegisteredAgentCaller
        self.assertFalse(RegisteredAgentCaller.exposes(self.runtime.registry.get('experience_rule_check')))
        self.assertTrue(RegisteredAgentCaller.exposes(self.runtime.registry.get('experience_observation_file')))
        from qorqut.agents.resolver import effective_tool_policy
        profile = NS(data={'tool_policy': {'overrides': {}}, 'plugins': [], 'role': {'id': 'global-worker'}}, revision=1,
                     scope=NS(global_capabilities={'read_global_context'}))
        offered = {tool['canonical_operation'] for tool in effective_tool_policy(profile, NS(manifest={'default_tool_policy': {}}, ref='role'),
                                                                   self.runtime.registry)}
        self.assertIn('experience_observation_file', offered)
        self.assertNotIn('experience_rule_check', offered)

    def test_a_policy_that_does_not_hold_the_check_files_nothing(self):
        self.held = set()
        self.refused('experience_observation_file', {'kind': 'owner_complaint', 'subject': 'all agents', 'text': self.RULE},
                     ctx=self.runtime.worker, code='operation_unavailable', says='not filed')
        with self.runtime.database.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM experience_observations').fetchone()[0], 0)

    def test_the_host_policy_always_holds_the_check_and_offers_it_to_no_agent(self):
        from qorqut.composition.policy import resolve_operation_policy
        for role in ('global-worker', 'ordubashy', 'learning-applier', None):
            for overrides in ({}, {'experience_rule_check': 'allow'}, {'experience_rule_check': 'deny'}):
                self.assertEqual(resolve_operation_policy('experience_rule_check', overrides, overrides, self.runtime.registry,
                                                          role_id=role, scope='global').mode, 'signoff', (role, overrides))

    def test_an_edit_and_a_rejection_the_observer_missed_are_settled_later(self):
        filed = self.file_rule()['filed']['id']
        first = self.rule(filed)['check_signoff_id']
        with patch.object(self.experience, 'rule_check_updated', lambda *a: None):
            card = self.edit(first, 'A rule in the owner\'s own words.')
            self.assertEqual(self.rule(filed)['check_signoff_id'], first)
            self.decide(card, 'reject')
        self.assertIsNotNone(self.rule(filed))
        with self.runtime.database.transaction() as uow:
            self.assertEqual(self.experience.settle_checks(uow), [filed])
        self.assertIsNone(self.rule(filed))


if __name__ == '__main__':
    unittest.main()
