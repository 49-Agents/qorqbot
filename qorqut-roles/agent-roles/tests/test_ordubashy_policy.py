"""Guard the intended boundary of Ordubashy's operational permissions."""
from pathlib import Path
import unittest

import yaml

WORKER_MANAGEMENT = {"agents_create", "agents_update", "agents_deactivate", "permissions_set",
                     "plugins_enable", "plugins_disable"}


class OrdubashyPolicyTest(unittest.TestCase):
    def test_browser_direct_default_and_scoped_skill(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        self.assertEqual(selected['browser-control'], 'skills/managed-browser-control/SKILL.md')
        guidance = (root / selected['browser-control']).read_text()
        self.assertIn('directly by default', guidance)
        self.assertIn('assign already authorized browser work to a permanent worker', ' '.join(guidance.split()))
        # consumer_id is an optional caller-chosen UUID, not a native session identity to look up.
        self.assertIn('`consumer_id` is optional: omit it to begin', guidance)
        self.assertNotIn('session UUID', guidance)
        self.assertIn('`business_id` and `browser_key`', guidance)
        self.assertIn('not the app\'s computer/browser tools', guidance)

    def test_native_browser_context_is_not_target_selection(self):
        root = Path(__file__).resolve().parents[1]
        guidance = (root / 'skills/operations-delegation/SKILL.md').read_text()
        self.assertIn('omit `business_id`', guidance)
        self.assertIn('and `browser_key`', guidance)
        self.assertIn('"action":"acquire","protocol":"handles","consumer_id"', guidance)
        self.assertIn('Project operations still use their required project scope', guidance)
        # consumer_id is an optional caller identifier, never a session UUID to hunt for.
        self.assertNotIn('helper-session-UUID', guidance)
        self.assertIn('never look up or hunt for a session UUID', ' '.join(guidance.split()))
        provisioning = ' '.join((root / 'skills/service-provisioning/SKILL.md').read_text().split())
        self.assertIn('`consumer_id` is optional', provisioning)
        self.assertNotIn('your session UUID', provisioning)

    def test_operational_guidance_preserves_owner_build_boundary(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        brief = (root / manifest['brief']).read_text()
        self.assertNotIn('business-engineering', selected)
        self.assertIn('business-operations', selected)
        self.assertEqual(selected['operations-delegation'], 'skills/operations-delegation/SKILL.md')
        self.assertIn('shared owner/self intake', brief)
        flat = ' '.join(brief.split())
        for text in ("is the owner's by default", 'is yours to carry out fully',
                     'never hand it back for being code', 'never edit Qorqut engine or dashboard code',
                     "Shared role or plugin source is edited only on the owner's direct assignment",
                     "Substantial frontend/UI changes require the owner's approval of the reviewable result before deployment"):
            self.assertIn(text, flat)
        for text in ("Dev builds", "start, stop and restart Dev", "ui-qa-delegation"):
            self.assertNotIn(text, flat)
        self.assertNotIn('ui-qa-delegation', selected)
        guidance = (root / 'skills/ecosystem-review/SKILL.md').read_text()
        for text in ('business_config_update', 'expected_revision',
                     'configuration authority as permission', 'notify:true, urgent:true, blocking:true'):
            self.assertIn(text, guidance)

    def test_operational_writes_allowed_and_other_signoffs_preserved(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / "roles/ordubashy/role.yaml").read_text())
        policy = manifest["default_tool_policy"]
        self.assertLessEqual(len((root / manifest["brief"]).read_text().strip()), 10000)
        self.assertEqual(manifest["version"], "1.72.0")
        self.assertEqual(manifest["default_tool_policy"]["environment_set"], "allow")
        # Owner decision 2026-09-24: Resend verification send and app-target injection read Settings values.
        self.assertEqual(policy["environment_get"], "allow")
        self.assertEqual(policy['owner_action_propose'], 'signoff')
        self.assertEqual(policy['business_archive'], 'signoff')
        for operation in ('focus_get', 'focus_present', 'focus_edit', 'focus_set_archived', 'focus_reply'):
            self.assertEqual(policy[operation], 'allow')
        self.assertEqual(policy['focus_move'], 'allow')
        for operation in ('owner_conversation_get', 'owner_conversation_send'):
            self.assertEqual(policy[operation], 'allow')
        self.assertNotIn('owner_conversation_mark_read', policy)
        for operation in ('tasks_list', 'tasks_overview', 'tasks_create', 'tasks_assign', 'tasks_update', 'tasks_complete', 'signoffs_reject', 'signoffs_attention_set'):
            self.assertEqual(policy[operation], 'allow')
        for operation in ('signoffs_review', 'signoffs_approve', 'signoffs_retry_apply', 'signoffs_respond'):
            self.assertNotIn(operation, policy)
        for operation in ('management_list', 'management_get', 'management_route',
                          'management_resolve', 'signoffs_input_answer_delegated'):
            self.assertEqual(policy[operation], 'allow')
        other_writes = {
            "business_config_update", "business_config_patch",
            "tasks_config_update", "routines_pause", "routines_stop",
        }
        # plugins_enable: owner decision 2026-09-17 — Ordubashy adds plugins without Signoff.
        # Ordubashy manages his permanent workers directly; the host limits the target to workers and the grant to his own.
        for operation in other_writes | WORKER_MANAGEMENT:
            with self.subTest(operation=operation):
                self.assertEqual(policy[operation], "allow")
        for operation in (
            "plugins_publish", "roles_publish",
            "shared_instructions_update",
            "routines_resume", "routines_delete",
            "routines_create", "routines_update", "routines_run",
        ):
            with self.subTest(operation=operation):
                self.assertEqual(policy[operation], "signoff")

    def test_journal_direct_reads_are_owner_requested_and_reconciliation_is_retired(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        policy = manifest['default_tool_policy']
        # The six obsolete journal_* denies were removed together with their catalog entries.
        for suffix in ('create','update','delete','upload','get','list'):
            self.assertNotIn('journal_' + suffix, policy)
        self.assertEqual(policy['journal_entries_list'], 'allow')
        self.assertEqual(policy['journal_entry_get'], 'allow')
        for operation in ('journal_review_mark', 'journal_reconcile_start', 'journal_reconcile_get',
                          'journal_reconcile_report', 'journal_context_find', 'journal_context_object'):
            self.assertNotIn(operation, policy)
        selected = {s['id']: s['path'] for s in manifest['skills']}
        guidance = (root / selected['owner-journal']).read_text()
        for text in ('explicitly requests it', 'only journal operations are', 'current visibility setting on each',
                     'does not change the entry', 'authorize acting on every intention'):
            self.assertIn(text, guidance)

    def test_two_factor_blockers_have_immediate_owner_escalation(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        brief = (root / manifest['brief']).read_text()
        self.assertIn('create an urgent blocking owner-input Signoff immediately', brief)
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        self.assertIn('owner-input', selected)
        guidance = (root / selected['ecosystem-review']).read_text()
        for required in ('signoffs_input_create', 'notify:true, urgent:true, blocking:true',
                         'stable request_key', 'signoffs_attention_set',
                         'Never record one-time codes', 'verify that access works'):
            self.assertIn(required, guidance)
        for operation in ('signoffs_input_create', 'signoffs_input_cancel', 'signoffs_attention_set'):
            self.assertEqual(manifest['default_tool_policy'][operation], 'allow')

    def test_global_tasks_and_attention_do_not_grant_owner_decisions(self):
        root = Path(__file__).resolve().parents[1]
        brief = (root / 'roles/ordubashy/BRIEF.md').read_text()
        guidance = (root / 'skills/ecosystem-review/SKILL.md').read_text()
        for text in ('scope=global', 'signoffs_attention_set', 'approvals are human-only'):
            self.assertIn(text, brief)
        for text in ('never project task lists', 'Owner snoozes are retained',
                     "Do not cancel another agent's input", 'Never mark real-world work complete'):
            self.assertIn(text, guidance)
        for text in ('snooze_filter="snoozed"', 'changes.snooze=', '{"indefinite":true}',
                     'shared by all assignees', 'signoff reminder snoozing'):
            self.assertIn(text, guidance)

    def test_overview_is_ordubashy_only_and_requires_detail_before_acting(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            self.assertEqual(manifest['default_tool_policy'].get('tasks_overview', 'deny'),
                             'allow' if manifest['id'] == 'ordubashy' else 'deny')
            self.assertEqual(manifest['default_tool_policy'].get('owner_action_propose', 'deny'),
                             'signoff' if manifest['id'] == 'ordubashy' else 'deny')
        guidance = (root / 'skills/ecosystem-review/SKILL.md').read_text()
        for text in ('tasks_overview once', 'follow next_cursor', 'read tasks_get',
                     'does not expand your permissions'):
            self.assertIn(text, guidance)

    def test_private_owner_conversation_guidance_and_scope(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        brief = (root / manifest['brief']).read_text()
        guidance = ' '.join((root / selected['owner-focus']).read_text().split())
        self.assertLessEqual(len(brief.strip()), 10000)
        self.assertIn("private-owner text is Ordubashy-only", brief)
        self.assertIn('judgment, never verbatim', brief)
        for text in (
            'Private conversation with the owner',
            'owner_conversation_get(scope=..., source={type, id, business_id?})',
            'owner_conversation_send',
            'business_id: null',
            "owner cannot post to the agents' team discussion",
            'Never quote or copy the private text wholesale',
            'never automate relaying',
            'Formal Signoff approval/rejection feedback still arrives as the shared decision note',
            'Every Claude and Codex session of a registered global peer identity shares',
            "never share history, delivery, counters or read position",
            'Do not treat the private channel itself as approval authority',
        ):
            self.assertIn(text, guidance)
        for text in ('response_detail="summary"', 'limit=20', 'next_offset',
                     'inbox_list` fresh without `after`', 'next_cursor',
                     'next_before', 'explicit defer',
                     'agent_config_get(response_detail="context")` without `sections'):
            self.assertIn(text, guidance)
        self.assertEqual(manifest['version'], '1.72.0')

    def test_focus_clarity_runs_on_start_and_after_compaction(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        brief = ' '.join((root / manifest['brief']).read_text().split())
        guidance = ' '.join((root / selected['owner-focus']).read_text().split())
        self.assertIn("Keep the owner's Focus clear (owner-focus): on start and after compaction", brief)
        for text in ('## Keep Focus clear', 'after every compaction', '`source_state` is done or cancelled',
                     'question for the owner', 'archives itself when the signoff is decided'):
            self.assertIn(text, guidance)
        self.assertNotIn('Source completion never automatically removes a card', guidance)
        self.assertIn('Authorized global peers may manage the contents, framing and order', guidance)
        self.assertIn('manage its contents, framing and order accordingly', brief)
        self.assertIn('Use these six tools', guidance)
        self.assertIn('- `focus_move`: reorder a card', guidance)
        self.assertNotIn('items at the top', guidance)
        self.assertNotIn('campaign', guidance)

    def test_browser_routing_distinguishes_native_tools_from_registered_mcp(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        guidance = (root / selected['operations-delegation']).read_text()
        brief = (root / manifest['brief']).read_text()
        self.assertIn('native tools exposed in the current Codex', guidance)
        self.assertIn('MCP `tools/list` and `operations_describe`', guidance)
        self.assertIn('cannot establish whether native helper spawning', guidance)
        self.assertIn('prepared Codex `[agents]` settings', guidance)
        self.assertIn('could not be verified', guidance)
        self.assertIn('inspection confirms no helper-start', guidance)
        self.assertIn('Do not poll, hunt for instructions, launch a shell helper', guidance)
        self.assertIn('use owner HTTP', guidance)
        self.assertIn('control-via-API.md', guidance)
        self.assertIn('registered-agent setup or a fallback', guidance)
        self.assertIn('prepared identity and authenticated', guidance)
        self.assertIn('external owner API', brief)
        self.assertIn('is not fallback', brief)
        self.assertLessEqual(len(brief.strip()), 10000)

    def test_browser_guidance_is_global_peer_only_and_keeps_existing_authority(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            if manifest['id'] == 'ordubashy':
                continue
            with self.subTest(role=manifest['id']):
                self.assertNotIn('operations-delegation', {
                    skill['id'] for skill in manifest.get('skills', [])})
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        self.assertEqual(manifest['default_tool_policy']['browser_turn'], 'allow')
        self.assertEqual(manifest['default_tool_policy']['delegated_assignments_create'], 'allow')
        self.assertNotIn('manage_delegated_assignments', manifest['default_tool_policy'])

    def test_every_global_uses_business_browsers_for_assigned_work(self):
        # Owner decision 2026-10-14: an assigned business task selects that business's browser,
        # with no membership requirement; own browsers and exclusive operation stay.
        root = Path(__file__).resolve().parents[1]
        for role in ('ordubashy', 'learning-applier'):
            manifest = yaml.safe_load((root / 'roles' / role / 'role.yaml').read_text())
            with self.subTest(role=role):
                selected = {skill['id']: skill['path'] for skill in manifest['skills']}
                # Ordubashy uses the managed copy; Learning Applier keeps the original file.
                self.assertEqual(selected['browser-control'], 'skills/managed-browser-control/SKILL.md' if role == 'ordubashy'
                                 else 'skills/ordubashy-browser-control/SKILL.md')
                self.assertEqual(manifest['default_tool_policy']['browser_turn'], 'allow')
        for name, delegation in (('ordubashy-browser-control', 'Learning Applier has no durable delegation'),
                                 ('managed-browser-control', 'Learning Applier has no durable delegation')):
            guidance = ' '.join((root / 'skills' / name / 'SKILL.md').read_text().split())
            for text in ("when work assigned to you in a project needs that project's browser",
                         "the assignment is the request for that project's browser",
                         'Your own browser stays the default', 'Your role covers every project; no membership is needed',
                         'an exclusive lease, one operator at a time', "the owner's viewer and durable workers",
                         "`consumer_id` stays optional", delegation,
                         'gain no account authority from this skill'):
                with self.subTest(skill=name, text=text[:40]):
                    self.assertIn(text, guidance)
            self.assertNotIn('business agent', guidance.lower())

    def test_push_merge_and_deploy_follow_the_chat_approval_rule(self):
        # Owner decisions G011/G012: clear chat approval needs no repeat Signoff; unclear authority gets one
        # blocking owner-only input request (merge and push combined); deployment is separately scoped.
        root = Path(__file__).resolve().parents[1]
        generic = ' '.join((root / 'skills/owner-input/SKILL.md').read_text().split())
        for text in ("Pushing any branch, merging and deploying need the owner's clear approval for that exact work",
                     'needs no extra Signoff', 'an implementation request alone is not push, merge or deployment approval',
                     '`attention.notify: true`, `blocking: true`, a `reason`',
                     'Combine merge and push in that one request', 'a review-branch push never becomes a merge proposal',
                     'Deployment is its own scope', 'it records the answer and executes nothing',
                     'Use the existing prepared approval for a service-managed protected action'):
            self.assertIn(text, generic)
        for role in ('ordubashy', 'learning-applier'):
            brief = ' '.join((root / 'roles' / role / 'BRIEF.md').read_text().split())
            with self.subTest(role=role):
                if role == 'learning-applier':
                    for text in ("deploying needs the owner's clear approval for that exact work",
                                 'request an owner-only input Signoff naming the action, artifact, repository and target',
                                 'reuse a matching pending request',
                                 'An implementation request alone is not approval to push, merge or deploy',
                                 'changed work requires fresh approval',
                                 'Do not push, publish or deploy repositories as part of this workflow'):
                        self.assertIn(text, brief)
                    continue
                for text in ("Pushing any branch, merging and deploying need the owner's clear approval for that exact work",
                             'an implementation request alone approves none of them',
                             'response_policy=owner_only, attention.notify=true, attention.blocking=true',
                             'ask for both in that one Signoff', 'pushing a review branch never requires proposing a merge',
                             'Deployment is a separate scope', 'Reuse a matching pending Signoff',
                             'Changed work or targets require fresh approval'):
                    self.assertIn(text, brief)

    def test_no_other_role_grants_owner_conversation_operations(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            policy = manifest['default_tool_policy']
            granted = [op for op in policy if op.startswith('owner_conversation_')]
            with self.subTest(role=manifest['id']):
                # Only the coordinator has the owner's isolated channel; the service binds it to the exact agent.
                expected = ['owner_conversation_get', 'owner_conversation_send'] \
                    if manifest['id'] == 'ordubashy' else []
                self.assertEqual(granted, expected)


    def test_retired_product_operations_connections_and_guidance_are_gone(self):
        root = Path(__file__).resolve().parents[1]
        retired_prefixes = ('ledger_', 'metrics_', 'products_', 'subscriptions_', 'daily_report_', 'personal_context_',
                            'campaign_', 'logs_')
        retired_names = {'stripe_reader_get', 'signoffs_reddit_approve', 'signoffs_platform_submit'}
        for path in sorted((root / 'roles').glob('*/role.yaml')):
            manifest = yaml.safe_load(path.read_text())
            with self.subTest(role=manifest['id']):
                policy = manifest['default_tool_policy']
                self.assertFalse([name for name in policy if name.startswith(retired_prefixes)])
                self.assertFalse(retired_names & set(policy))
                self.assertNotIn('external_connections', manifest)
        # The general capabilities that remain are not collateral damage.
        policy = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())['default_tool_policy']
        for name in ('business_create',
                     'cloudflare_api_request', 'notes_create', 'content_get', 'content_list', 'telegram_send',
                     'management_list', 'plans_list', 'environment_get', 'owner_reminder'):
            self.assertIn(policy.get(name), ('allow', 'signoff'), name)

    def test_role_manifests_carry_no_email_or_mailbox_grants(self):
        # v1.32: email is instructions only (Email Management). Role text and manifests grant no mail tool, mailbox
        # route or email approval; the host's plugin defaults and the optional Fastmail package own those.
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'roles').glob('*/role.yaml'):
            manifest = yaml.safe_load(path.read_text())
            email_keys = {key for key in manifest['default_tool_policy']
                          if key.startswith(('email_', 'business_email_', 'account_email_', 'fastmail_'))
                          or key in ('signoffs_email_approve', 'signoffs_platform_submit')}
            self.assertEqual(email_keys, set(), manifest['id'])

    def test_email_is_routed_to_email_management_and_calendar_is_a_role_skill(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        selected = {skill['id']: skill['path'] for skill in manifest['skills']}
        for retired in ('fastmail-account', 'business-email-setup'):
            self.assertNotIn(retired, selected)
            self.assertFalse((root / 'skills' / retired).exists(), retired)
        self.assertEqual(selected['calendar'], 'skills/calendar/SKILL.md')
        brief = ' '.join((root / manifest['brief']).read_text().split())
        self.assertIn('consult Email Management first', brief)
        guidance = ' '.join((root / selected['calendar']).read_text().split())
        for text in ('Desktop app', 'python -m bazarlyk.calendar.connect google', 'QORQUT_CALENDAR_CONFIG',
                     'primary calendar', 'not an API key', 'Gmail authorization and Google Calendar authorization are separate',
                     'The owner does the sign-in, verification and consent', 'calendar_calendars_list',
                     'calendar_events_list', 'Create no test event', '`signoffs_input_create`', '`signoffs_get`',
                     'executes nothing', 'not enforce it', '`form.response_policy: owner_only`', '`form.selection_mode: single`', 'Only an explicit owner instruction waives it',
                     'A private change (no guests, nobody notified) may run when the owner asks'):
            self.assertIn(text, guidance)
        for operation in ('calendar_calendars_list', 'calendar_events_list', 'calendar_event_get', 'calendar_event_create',
                          'calendar_event_update', 'calendar_event_delete', 'calendar_event_respond'):
            self.assertEqual(manifest['default_tool_policy'][operation], 'allow')
        for role in ('global-worker', 'learning-applier'):
            other = yaml.safe_load((root / 'roles' / role / 'role.yaml').read_text())
            self.assertNotIn('calendar', {skill['id'] for skill in other.get('skills', [])})

    def test_works_every_business_directly_without_resident_agents(self):
        root = Path(__file__).resolve().parents[1]
        manifest = yaml.safe_load((root / 'roles/ordubashy/role.yaml').read_text())
        brief = ' '.join((root / manifest['brief']).read_text().split())
        for text in ('Your role capabilities cover every current and future project without membership grants',
                     "work directly with each project's files, resources, accounts and integrations",
                     "Strategy and direction are settled with the owner in conversation; record them in ordinary project documents and assign bounded execution to workers.",
                     "Only you create a project (project-creation; no provisioning); the owner and workers ask you.",
                     'The Global that assigns work keeps its follow-up'):
            self.assertIn(text, brief)
        for text in ('agents_message_recipients', 'business-agent', 'never assign them to business agents',
                     'founding', 'Founder', 'milestone', 'ledger', 'Metrics', 'launchpad', 'daily report', 'outreach'):
            self.assertNotIn(text, brief)
        self.assertNotIn('onbashy', brief.lower())
        self.assertNotIn('staffing', {skill['id'] for skill in manifest['skills']})
        for operation in WORKER_MANAGEMENT:
            self.assertEqual(manifest['default_tool_policy'][operation], 'allow')
        guidance = ' '.join((root / 'skills/ecosystem-review/SKILL.md').read_text().split())
        self.assertIn('Projects have no resident agents', guidance)
        self.assertIn('Never edit Qorqut engine or dashboard code', guidance)
        self.assertIn("Shared role or plugin source is edited only on the owner's direct assignment", guidance)
        for text in ('Each business has two peer agents', 'directly message Dev'):
            self.assertNotIn(text, guidance)
        operations = ' '.join((root / 'skills/business-operations/SKILL.md').read_text().split())
        for text in ('Projects have no resident agents', 'Never create or revive a project agent or project Dev',
                     'plugins_enable', 'business_readiness_get', 'approval of a plugin change never approves message content',
                     'A worker has no automatic plugin',
                     'Ordubashy manages worker plugins (email-management)',
                     'Nothing here grants a plugin or approves a send',
                     "never Qorqut engine or dashboard code", "under owner-input's approval rule",
                     "permission to implement is not deployment approval",
                     "Permission to write code never approves a production migration, service restart or installing unattended execution",
                     "When the owner's clear deployment authorization explicitly includes such a step, do not ask for it again",
                     "a step outside that scope follows owner-input", "This adds no routine executor or system permission",
                     "Preserve existing holds and approvals"):
            self.assertIn(text, operations)
        for text in ('Each business has one business agent and one Dev peer', 'route it to Dev'):
            self.assertNotIn(text, operations)
