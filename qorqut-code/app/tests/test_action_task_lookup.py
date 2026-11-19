"""actions_record finds the Task it links anywhere in a global recorder's reach and takes its project and category."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_operations.actions.operations import ActionOperations
from qorqut_operations.actions.scope import classification_from_task
from qorqut_operations.contracts import DomainError, InvocationContext, OperationResult, Principal, plain
from qorqut_operations.storage.assembly import AssemblyError

from test_task_project_lookup import Harness as TaskHarness

TEXT = 'Sent the founding email.'


class Harness(TaskHarness):
    """The Task lookup fixture (an ecosystem agent over MCP reaching Alpha and Beta, Gamma outside) with Actions."""

    def setUp(self):
        super().setUp()
        self.admitted = []

        def on_revision(ctx, revision, uow):
            self.admitted.append(revision['action_key'])
            return str(uuid4())
        self.actions = ActionOperations(self.app, self.tasks.note_references(), tasks=self.tasks, on_revision=on_revision)
        contribution = self.actions.contribution()
        for entry in contribution.entries:
            self.registry.declare(entry)
        for descriptor in contribution.descriptors:
            self.registry.register(descriptor)

    def record(self, ctx=None, **arguments):
        return self.call('actions_record', {'text': TEXT, **arguments}, ctx)

    def recorded(self, ctx=None, **arguments):
        result = self.record(ctx, **arguments)
        self.assertEqual(result.status, 'ok', result.to_dict())
        return plain(result.data['action'])

    def classification(self, action):
        return action['category'], [business['id'] for business in action['businesses']]

    def refused(self, result, code, message, field=None):
        self.assertEqual(result.status, 'error', result.to_dict())
        self.assertEqual((result.error['code'], result.error['message']), (code, message))
        if field:
            self.assertIn(field, result.error['field_errors'])

    def stored(self):
        with self.db.read() as uow:
            return uow.connection.execute('SELECT count(*) FROM action_logs').fetchone()[0]


class ActionTaskLookupTests(Harness):
    # -- the Task's own classification -----------------------------------------------------------------------------

    def test_a_project_task_named_alone_makes_a_business_action_of_its_project(self):
        task = self.create(self.alpha)
        key = str(uuid4())
        result = self.call('actions_record', {'text': TEXT, 'task_id': task['id']}, idempotency_key=key)
        self.assertEqual(result.status, 'ok', result.to_dict())
        action = plain(result.data['action'])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))
        self.assertEqual(action['refs'], {'task': task['id']})
        # The Action is still Global-stored, as every global recorder's is; only its link and category come from the Task.
        self.assertEqual(action['legacy']['business_id'], None)
        self.assertEqual(self.receipts(key), ['global'])
        self.assertEqual(self.admitted, [action['action_id']])

    def test_a_global_task_named_alone_makes_a_global_action(self):
        task = self.create(None)
        action = self.recorded(task_id=task['id'])
        self.assertEqual(self.classification(action), ('global-operational', []))
        self.assertEqual(action['refs'], {'task': task['id']})

    def test_a_business_category_without_projects_takes_the_tasks_project(self):
        task = self.create(self.beta)
        action = self.recorded(task_id=task['id'], category='business-operational')
        self.assertEqual(self.classification(action), ('business-operational', [self.beta]))

    def test_what_the_caller_passed_is_never_changed(self):
        task = self.create(self.alpha)
        # A project Task's Action the caller calls Global stays Global and names no project.
        action = self.recorded(task_id=task['id'], category='global-operational')
        self.assertEqual(self.classification(action), ('global-operational', []))
        self.assertEqual(action['refs'], {'task': task['id']})
        # Projects the caller names stand, the Task's own project is not added.
        action = self.recorded(task_id=task['id'], category='business-operational', business_ids=[self.beta])
        self.assertEqual(self.classification(action), ('business-operational', [self.beta]))
        action = self.recorded(task_id=task['id'], business_ids=[self.alpha, self.beta])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha, self.beta]))
        # Every authorization check of the classification still applies to what was passed.
        self.refused(self.record(task_id=task['id'], business_ids=[self.gamma]), 'forbidden',
                     'Every selected project must be currently authorized and active')
        self.refused(self.record(task_id=task['id'], category='global-systems'), 'forbidden',
                     'Global agents may record only global-operational or business-operational actions')
        # A Global Task says global-operational, and that category names no project.
        general = self.create(None, title='Review the week')
        self.refused(self.record(task_id=general['id'], business_ids=[self.alpha]), 'validation_error',
                     'Only business-operational actions name projects', 'business_ids')
        action = self.recorded(task_id=general['id'], category='business-operational', business_ids=[self.alpha])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))
        # A Global Task cannot supply the project a business-operational Action needs.
        self.refused(self.record(task_id=general['id'], category='business-operational'), 'validation_error',
                     'Business-operational actions need at least one project', 'business_ids')

    def test_the_derivation_itself(self):
        project = str(uuid4())
        self.assertEqual(classification_from_task(None, None, project), ('business-operational', [project]))
        self.assertEqual(classification_from_task(None, None, None), ('global-operational', None))
        self.assertEqual(classification_from_task('business-operational', [], project), ('business-operational', [project]))
        self.assertEqual(classification_from_task('business-operational', None, None), ('business-operational', None))
        self.assertEqual(classification_from_task('global-operational', None, project), ('global-operational', None))
        self.assertEqual(classification_from_task(None, ['other'], project), ('business-operational', ['other']))
        self.assertEqual(classification_from_task('global-systems', None, project), ('global-systems', None))

    def test_the_owner_gets_the_same_derivation(self):
        task = self.create(self.gamma)
        action = self.recorded(self.owner_ctx, task_id=task['id'])
        self.assertEqual(self.classification(action), ('business-operational', [self.gamma]))
        self.assertEqual(action['refs'], {'task': task['id']})
        # Without a Task the owner's Action stays unclassified, as before.
        action = self.recorded(self.owner_ctx)
        self.assertEqual(self.classification(action), (None, []))

    def test_an_upper_case_id_links_the_task_as_stored(self):
        task = self.create(self.alpha)
        action = self.recorded(task_id=task['id'].upper())
        self.assertEqual(action['refs'], {'task': task['id']})
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))

    # -- nothing found, several found ------------------------------------------------------------------------------

    def test_an_unknown_task_is_a_validation_error_on_task_id(self):
        refused = self.record(task_id='no-such-task')
        self.refused(refused, 'validation_error', 'Linked task does not exist, or is in a project outside your reach', 'task_id')
        self.assertNotIn('details', refused.error)
        # The Task is looked for before the classification is judged, so a wrong id is named whatever else was passed.
        refused = self.record(task_id='no-such-task', category='business-operational', business_ids=[self.alpha])
        self.assertEqual(refused.error['field_errors'], {'task_id': 'not found'})
        self.assertEqual(self.stored(), 0)

    def test_a_task_outside_the_callers_reach_is_not_found_and_not_disclosed(self):
        hidden = self.create(self.gamma, title='Confidential plan')
        for arguments in ({}, {'category': 'global-operational'}, {'category': 'business-operational', 'business_ids': [self.alpha]}):
            refused = self.record(task_id=hidden['id'], **arguments)
            self.refused(refused, 'validation_error', 'Linked task does not exist, or is in a project outside your reach',
                         'task_id')
            self.assertNotIn('details', refused.error)
            self.assertNotIn('Gamma', str(refused.to_dict()))
        # A project the agent may reach but may not read Tasks in is not searched either.
        denied = self.create(self.beta)
        self.policy.modes[(self.agent, self.beta, 'tasks_get')] = 'deny'
        self.refused(self.record(task_id=denied['id']), 'validation_error',
                     'Linked task does not exist, or is in a project outside your reach', 'task_id')
        self.assertEqual(self.stored(), 0)

    def test_a_reused_id_means_the_scope_the_call_names(self):
        slug = 'founding-email-preparation'
        # New ids are unique across projects; an id held in several scopes predates that rule.
        with patch('qorqut_operations.tasks.operations.taken', return_value=False):
            for business in (None, self.alpha, self.beta, self.gamma):
                self.create(business, id=slug)
        refused = self.record(task_id=slug)
        self.assertEqual(refused.error['code'], 'validation_error', refused.to_dict())
        self.assertEqual(refused.error['field_errors'], {'business_ids': 'required: the task id is in more than one project'})
        self.assertEqual(plain(refused.error['details']), {'task_id': slug, 'candidates': [
            {'business_id': None, 'business_name': 'Global', 'title': 'Prepare the founding email'},
            {'business_id': self.alpha, 'business_name': 'Alpha', 'title': 'Prepare the founding email'},
            {'business_id': self.beta, 'business_name': 'Beta', 'title': 'Prepare the founding email'}]})
        # Two projects named: still not one Task.
        self.assertEqual(self.record(task_id=slug, business_ids=[self.alpha, self.beta]).error['code'], 'validation_error')
        chosen = self.recorded(task_id=slug, business_ids=[self.beta])
        self.assertEqual(self.classification(chosen), ('business-operational', [self.beta]))
        chosen = self.recorded(task_id=slug, category='global-operational')
        self.assertEqual(self.classification(chosen), ('global-operational', []))
        self.assertEqual(chosen['refs'], {'task': slug})

    # -- a staged record ---------------------------------------------------------------------------------------------

    def test_a_staged_record_validates_and_applies_with_the_same_derived_answer(self):
        task = self.create(self.alpha)
        self.policy.modes[(self.agent, None, 'actions_record')] = 'signoff'
        staged = []

        def stage(context, descriptor, plan, options, uow):
            staged.append(plan)
            return OperationResult(context.request_id, 'signoff_required', signoff={'id': str(uuid4())})
        self.app.signoffs = NS(stage=stage)
        result = self.record(task_id=task['id'])
        self.assertEqual(result.status, 'signoff_required', result.to_dict())
        plan = staged[0]
        # The plan keeps exactly what the caller passed; the Task and the derived link are pinned in its targets.
        self.assertEqual(plain(plan.arguments), {'text': TEXT, 'task_id': task['id']})
        self.assertEqual(plan.target_namespace, 'global')
        pinned = {(target.resource.resource_type, target.resource.business_id) for target in plan.expected_targets}
        self.assertIn(('tasks', self.alpha), pinned)
        linked = [plain(change.values) for change in plan.changes if change.resource.resource_type == 'action_businesses']
        self.assertEqual([row['business_id'] for row in linked], [self.alpha])
        with self.db.read() as uow:
            self.app.validate_prepared(self.agent_ctx, plan, uow, require_approval=False)
            # The same lookup decides again: a proposer that lost the Task's project no longer validates.
            narrowed = replace(self.agent_ctx, allowed_business_ids={self.beta})
            with self.assertRaises(DomainError) as lost:
                self.actions.validate_record(narrowed, plan, uow)
            self.assertEqual((lost.exception.code, lost.exception.field_errors), ('validation_error', {'task_id': 'not found'}))
            with self.assertRaises(DomainError) as outside:
                self.app.validate_prepared(narrowed, plan, uow, require_approval=False)
            self.assertEqual(outside.exception.code, 'forbidden')
        self.assertEqual(self.stored(), 0)
        self.policy.modes.clear()
        with self.db.transaction() as uow:
            applied = self.app.apply_prepared(self.agent_ctx, replace(plan, policy_binding=self.binding(plan)), uow)
        action = plain(applied.data['action'])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))
        self.assertEqual(action['refs'], {'task': task['id']})

    def binding(self, plan):
        """The plan's authority binding under the now-permitting policy, as an approved proposal would carry it."""
        with self.db.read() as uow:
            descriptor = self.registry.get(plan.operation_name)
            decisions = self.app._decisions(self.agent_ctx, descriptor, plan.expected_targets, plan.changes, uow,
                                            namespace=plan.target_namespace)
            return self.app._binding(self.agent_ctx, decisions)

    # -- unchanged without a Task, and for callers that record in their own project ---------------------------------

    def test_without_a_task_the_classification_is_still_required(self):
        self.refused(self.record(), 'validation_error', 'Global agents must choose global-operational or business-operational',
                     'category')
        self.refused(self.record(category='business-operational'), 'validation_error',
                     'Business-operational actions need at least one project', 'business_ids')
        self.refused(self.record(category='global-operational', business_ids=[self.alpha]), 'validation_error',
                     'Only business-operational actions name projects', 'business_ids')
        action = self.recorded(category='business-operational', business_ids=[self.alpha])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))
        self.assertEqual(action['refs'], {})

    def test_a_caller_recording_in_its_own_project_links_only_that_projects_tasks(self):
        own, other, general = self.create(self.alpha), self.create(self.beta), self.create(None)
        member = InvocationContext(Principal(self.agent, 'agent'), allowed_business_ids={self.alpha, self.beta},
                                   business_id=self.alpha, profile_revision=1, source='mcp')
        action = self.recorded(member, task_id=own['id'])
        self.assertEqual(self.classification(action), ('business-operational', [self.alpha]))
        self.assertEqual(action['legacy']['business_id'], self.alpha)
        for task in (other, general):
            self.refused(self.record(member, task_id=task['id']), 'validation_error',
                         "Linked task does not exist in this action's workspace", 'task_id')

    def test_actions_need_the_task_service(self):
        with self.assertRaises(AssemblyError):
            ActionOperations(self.app, self.tasks.note_references(), tasks=NS(), on_revision=lambda *a: 'input')
        with self.assertRaises(TypeError):
            ActionOperations(self.app, self.tasks.note_references(), on_revision=lambda *a: 'input')


if __name__ == '__main__':
    unittest.main()
